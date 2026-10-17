"""Company knowledge (org chart, policies, tickets, chat, decisions) built from source files.

The files in `backend/data/company/` are synthetic demo data:
- `org.toml`: `[[teams]]` and `[[employees]]`; an employee's `team` and optional `reports_to` become edges.
- `policies/*.md`: TOML front matter between `+++` lines; the body is the policy text.
- `tickets/*.json`: a list of tickets. `slack/*.jsonl`: one message per line.
- `decisions.toml`: `[[decisions]]`; `approved_by`, `applies_to`, `sourced_from` and `governed_by` become edges.
Timestamps are ISO-8601 with an offset; amounts are in cents.

Every node built from a record carries `source_path` (the file, relative to the repo), `source_ref` (the record
in it) and `content_hash`: the sha256 of the record's canonical JSON (properties and relationship fields, epoch
timestamps, sorted keys). Whitespace in a policy body is collapsed, so rewrapping it is not an edit.

Usage (from the repo root):
    python -m backend.app.company --graph fintrace:my-copy --sync
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from falkordb import Graph

from .config import DATA_DIR, ROOT, get_settings
from .contracts import BUMP_REV, current_rev
from .db import get_db, graph_exists, graph_key
from .scenario import GraphSpec
from .tools import ID_RE

COMPANY_DIR = DATA_DIR / "company"
PROVENANCE = ("source_path", "source_ref", "content_hash")

# Label -> (properties, relationship fields) of its records; every record also has an `id`.
KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Team": (("name",), ()),
    "Employee": (("name", "title", "clearance", "clearance_level", "on_call"), ("team", "reports_to")),
    "PolicyDoc": (("title", "version", "waiver_threshold_monthly", "required_clearance", "required_clearance_level",
                   "required_clearance_for_freeze", "required_clearance_level_for_freeze", "text"), ("owned_by",)),
    "Ticket": (("system", "status", "opened_at", "title", "text"), ("opened_by",)),
    "Message": (("system", "channel", "sent_at", "text"), ("author",)),
    "Decision": (("kind", "control", "status", "approved_at", "merchant_monthly_volume", "summary"),
                 ("approved_by", "applies_to", "sourced_from", "governed_by")),
}
# Relationship field -> (edge type, direction seen from the record's node, labels the other end must have).
# `applies_to` names an account, which the company files don't define.
LINKS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "team": ("MEMBER_OF", "out", ("Team",)),
    "reports_to": ("REPORTS_TO", "out", ("Employee",)),
    "owned_by": ("OWNED_BY", "out", ("Team",)),
    "opened_by": ("OPENED", "in", ("Employee",)),
    "author": ("AUTHORED", "in", ("Employee",)),
    "approved_by": ("APPROVED", "in", ("Employee",)),
    "applies_to": ("APPLIES_TO", "out", ()),
    "sourced_from": ("SOURCED_FROM", "out", ("Ticket", "Message")),
    "governed_by": ("GOVERNED_BY", "out", ("PolicyDoc",)),
}
MANY = {"sourced_from"}
OPTIONAL = {"reports_to"}
TIMESTAMPS = {"opened_at", "sent_at", "approved_at"}
INTS = {"clearance_level", "waiver_threshold_monthly", "required_clearance_level",
        "required_clearance_level_for_freeze", "merchant_monthly_volume"}
BOOLS = {"on_call"}
CLEARANCES = {"clearance": "clearance_level", "required_clearance": "required_clearance_level",
              "required_clearance_for_freeze": "required_clearance_level_for_freeze"}
FRONT_MATTER = re.compile(r"\A\+\+\+[ \t]*\r?\n(.*?)^\+\+\+[ \t]*\r?$(.*)\Z", re.S | re.M)

_NODES = " UNION ALL ".join(f"MATCH (n:{label}) RETURN n.id AS id, labels(n) AS labels, n {{.*}} AS props"
                            for label in KINDS)
_OUT_RELS = "|".join(rel for rel, way, _ in LINKS.values() if way == "out")
_IN_RELS = "|".join(rel for rel, way, _ in LINKS.values() if way == "in")
_EDGES = f"""
UNWIND $ids AS nid
MATCH (:Entity {{id: nid}})-[r:{_OUT_RELS}]->(b:Entity)
RETURN nid AS id, type(r) AS rel, 'out' AS way, b.id AS other
UNION ALL
UNWIND $ids AS nid
MATCH (:Entity {{id: nid}})<-[r:{_IN_RELS}]-(a:Entity)
RETURN nid AS id, type(r) AS rel, 'in' AS way, a.id AS other
"""
_UPDATE = BUMP_REV + " MATCH (n:Entity {id: $id}) SET n += $props, n.topology_rev = rev, n.content_hash = $hash"
_RESTAMP_APPROVAL = " WITH n, rev OPTIONAL MATCH (:Employee)-[a:APPROVED]->(n) SET a.at = n.approved_at"


@dataclass(frozen=True)
class Record:
    """One record from a source file: graph properties (including `id`), relationship fields and location."""

    kind: str
    props: dict[str, Any]
    links: dict[str, Any]
    source_path: str
    source_ref: str

    @property
    def id(self) -> str:
        return self.props["id"]

    @property
    def content_hash(self) -> str:
        return hash_content({**self.props, **self.links})


def hash_content(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_records(root: Path = COMPANY_DIR) -> list[Record]:
    """Every record in the source files, validated, in file order. Raises ValueError naming the bad record."""
    root = Path(root)
    records = [*_org(root), *_policies(root), *_tickets(root), *_messages(root), *_decisions(root)]
    by_id: dict[str, Record] = {}
    for record in records:
        if (first := by_id.setdefault(record.id, record)) is not record:
            raise ValueError(f"{record.source_ref}: {record.id} is already defined in {first.source_ref}")
    for record in records:
        for name, value in record.links.items():
            targets = LINKS[name][2]
            for target in _ids(value) if targets else []:
                if target not in by_id or by_id[target].kind not in targets:
                    raise ValueError(f"{record.source_ref}: {name} {target!r} is not a "
                                     f"{' or '.join(targets)} in the company files")
    return records


def _org(root: Path) -> list[Record]:
    path = root / "org.toml"
    data = _toml(path.read_text(encoding="utf-8"), _rel(path, root), {"teams", "employees"})
    return [*_listed("Team", data.get("teams", []), path, root),
            *_listed("Employee", data.get("employees", []), path, root)]


def _policies(root: Path) -> list[Record]:
    records = []
    for path in sorted((root / "policies").glob("*.md")):
        rel = _rel(path, root)
        match = FRONT_MATTER.match(path.read_text(encoding="utf-8"))
        if not match:
            raise ValueError(f"{rel}: expected TOML front matter between +++ lines")
        meta = _toml(match[1], rel)
        if "text" in meta:
            raise ValueError(f"{rel}: the policy text belongs in the body, not the front matter")
        records.append(_record("PolicyDoc", {**meta, "text": " ".join(match[2].split())}, path, rel))
    return records


def _tickets(root: Path) -> list[Record]:
    records = []
    for path in sorted((root / "tickets").glob("*.json")):
        records += _listed("Ticket", _json(path.read_text(encoding="utf-8"), _rel(path, root)), path, root)
    return records


def _messages(root: Path) -> list[Record]:
    records = []
    for path in sorted((root / "slack").glob("*.jsonl")):
        rel = _rel(path, root)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                records.append(_record("Message", _json(line, f"{rel}:{n}"), path, f"{rel}:{n}"))
    return records


def _decisions(root: Path) -> list[Record]:
    path = root / "decisions.toml"
    data = _toml(path.read_text(encoding="utf-8"), _rel(path, root), {"decisions"})
    return _listed("Decision", data.get("decisions", []), path, root)


def _toml(text: str, where: str, tables: set[str] | None = None) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{where}: {exc}") from None
    if tables is not None and (unknown := sorted(data.keys() - tables)):
        raise ValueError(f"{where}: unknown table {', '.join(unknown)}")
    return data


def _json(text: str, where: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: {exc}") from None


def _listed(kind: str, items: Any, path: Path, root: Path) -> list[Record]:
    rel = _rel(path, root)
    if not isinstance(items, list):
        raise ValueError(f"{rel}: expected a list of {kind} records")
    records = []
    for n, raw in enumerate(items, 1):
        rid = raw.get("id") if isinstance(raw, dict) else None
        records.append(_record(kind, raw, path, f"{rel}#{rid}" if isinstance(rid, str) else f"{rel} record {n}"))
    return records


def _record(kind: str, raw: Any, path: Path, ref: str) -> Record:
    props, links = KINDS[kind]
    if not isinstance(raw, dict):
        raise ValueError(f"{ref}: expected a table of fields, got {raw!r}")
    fields = {"id", *props, *links}
    if missing := sorted(fields - OPTIONAL - raw.keys()):
        raise ValueError(f"{ref}: missing {', '.join(missing)}")
    if unknown := sorted(raw.keys() - fields):
        raise ValueError(f"{ref}: unknown field {', '.join(unknown)}")
    values = {name: _value(name, raw[name], ref) for name in ("id", *props)}
    for name, level in CLEARANCES.items():
        if name in values and values[name] != f"L{values[level]}":
            raise ValueError(f"{ref}: {name} {values[name]!r} does not match {level} {values[level]}")
    return Record(kind, values, {name: _link(name, raw.get(name), ref) for name in links}, _repo_path(path), ref)


def _value(name: str, value: Any, ref: str) -> Any:
    if name in TIMESTAMPS:
        return _epoch(value, f"{ref}: {name}")
    expected = bool if name in BOOLS else int if name in INTS else str
    # type(), not isinstance(): bool is a subclass of int.
    if type(value) is not expected:
        raise ValueError(f"{ref}: {name} must be a {expected.__name__}, got {value!r}")
    if name == "id" and not ID_RE.match(value):
        raise ValueError(f"{ref}: {value!r} is not a valid entity ID")
    return value


def _link(name: str, value: Any, ref: str) -> str | list[str] | None:
    if value is None and name in OPTIONAL:
        return None
    many = name in MANY
    ids = value if many and isinstance(value, list) else [value]
    if many != isinstance(value, list) or not all(isinstance(i, str) and ID_RE.match(i) for i in ids):
        raise ValueError(f"{ref}: {name} must be {'a list of entity IDs' if many else 'an entity ID'}, got {value!r}")
    return sorted(set(ids)) if many else value


def _epoch(value: Any, where: str) -> int:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            pass
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{where} must be an ISO-8601 timestamp with an offset (e.g. Z), got {value!r}")
    return int(value.timestamp())


def _ids(value: str | list[str] | None) -> list[str]:
    return [] if value is None else value if isinstance(value, list) else [value]


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _repo_path(path: Path) -> str:
    path = path.resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def build_company_spec(root: Path = COMPANY_DIR) -> GraphSpec:
    """The company layer, with provenance on every node. Reads files only."""
    spec = GraphSpec()
    for record in read_records(root):
        spec.node(f"Entity:{record.kind}", **record.props, source_path=record.source_path,
                  source_ref=record.source_ref, content_hash=record.content_hash)
        for name, value in record.links.items():
            rel, way, _ = LINKS[name]
            for other in _ids(value):
                src, dst = (record.id, other) if way == "out" else (other, record.id)
                spec.edge(rel, src, dst, **({"at": record.props["approved_at"]} if rel == "APPROVED" else {}))
    return spec


def load_company(graph: Graph, root: Path = COMPANY_DIR) -> dict:
    """Create the company layer in a graph that has the accounts but no company nodes. No revision bump."""
    from .seed_data import load_spec  # imported here so seed_data can import this module

    spec = build_company_spec(root)
    ids = [props["id"] for _, props in spec.nodes]
    if present := _present(graph, ids):
        raise ValueError(f"already in the graph: {', '.join(present)}; use sync_company to update it")
    outside = sorted({end for _, src, dst, _ in spec.edges for end in (src, dst)} - set(ids))
    if missing := sorted(set(outside) - set(_present(graph, outside))):
        raise ValueError(f"not in the graph: {', '.join(missing)}; load the accounts first")
    load_spec(graph, spec)
    return {"nodes": dict(Counter(labels.split(":")[-1] for labels, _ in spec.nodes)),
            "edges": dict(Counter(rel for rel, *_ in spec.edges))}


def _present(graph: Graph, ids: list[str]) -> list[str]:
    rows = graph.ro_query("UNWIND $ids AS eid MATCH (n:Entity {id: eid}) RETURN DISTINCT eid", {"ids": ids})
    return sorted(row[0] for row in rows.result_set)


def sync_company(graph: Graph, root: Path = COMPANY_DIR) -> dict:
    """Apply edited source records to a graph that has the company layer.

    For each record whose content hash differs from its node's, the properties that differ from the file are
    written in one query that also bumps the evidence revision and stamps the node's `topology_rev`. Unchanged
    records are not written. Relationship changes and added or removed records are listed in `unsupported`,
    not applied; they need a reload. `updates` holds the old and new value of every property written.
    """
    records = read_records(root)
    nodes = {nid: (labels, dict(props)) for nid, labels, props in graph.ro_query(_NODES).result_set}
    edges: dict[str, dict[tuple[str, str], list[str]]] = defaultdict(lambda: defaultdict(list))
    for nid, rel, way, other in graph.ro_query(_EDGES, {"ids": list(nodes)}).result_set:
        edges[nid][rel, way].append(other)

    changed, updates, unsupported = [], [], []
    for record in records:
        if record.id not in nodes:
            unsupported.append({"id": record.id, "change": "added", "source_ref": record.source_ref})
            continue
        labels, props = nodes[record.id]
        if props.get("content_hash") == record.content_hash:
            continue
        if record.kind not in labels:
            unsupported.append({"id": record.id, "change": "kind", "graph": [x for x in labels if x != "Entity"],
                                "file": record.kind})
            continue
        linked = {name: _linked(edges[record.id], name) for name in record.links}
        unsupported += [{"id": record.id, "change": "relationship", "field": name, "graph": linked[name], "file": value}
                        for name, value in record.links.items() if linked[name] != value]
        fields = {name: [props.get(name), value] for name, value in record.props.items()
                  if not _identical(props.get(name), value)}
        if fields:
            # The hash describes what the node now holds, so pending relationship changes keep being reported.
            rev = _update(graph, record, {name: new for name, (_, new) in fields.items()},
                          hash_content({**record.props, **linked}))
            changed.append(record.id)
            updates.append({"id": record.id, "source_ref": record.source_ref, "rev": rev, "fields": fields})
    known = {record.id for record in records}
    unsupported += [{"id": nid, "change": "removed", "source_ref": nodes[nid][1].get("source_ref")}
                    for nid in sorted(nodes.keys() - known)]
    return {"changed": changed, "unsupported": unsupported, "rev": current_rev(graph), "updates": updates}


def _linked(edges: dict[tuple[str, str], list[str]], name: str) -> str | list[str] | None:
    """A relationship field as the graph has it, in the shape `_link` gives the file's."""
    rel, way, _ = LINKS[name]
    ids = sorted(set(edges.get((rel, way), [])))
    if name in MANY:
        return ids
    return ids[0] if len(ids) == 1 else ids or None


def _identical(a: Any, b: Any) -> bool:
    """Equal and of the same type: Python treats True and 1 as equal, Cypher does not."""
    return type(a) is type(b) and a == b


def _update(graph: Graph, record: Record, props: dict, applied_hash: str) -> int:
    query = _UPDATE + (_RESTAMP_APPROVAL if "approved_at" in props else "") + " RETURN rev"
    rows = graph.query(query, {"id": record.id, "hash": applied_hash,
                               "props": {**props, "source_path": record.source_path,
                                         "source_ref": record.source_ref}}).result_set
    if not rows:
        raise RuntimeError(f"could not update {record.id}: the graph needs a :Meta {{key: 'dataset'}} node")
    return rows[0][0]


def _short(value: Any, width: int = 72) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= width else text[:width - 3] + "..."


def _shown(old: Any, new: Any, width: int = 64) -> tuple[str, str]:
    """Both values as JSON; two long strings are cut to the same window around their first difference."""
    if not (isinstance(old, str) and isinstance(new, str)) or max(len(old), len(new)) <= width:
        return _short(old, width), _short(new, width)
    first = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
    start = old.rfind(" ", 0, max(first - width // 3, 0)) + 1
    old, new = (("..." if start else "") + text[start:start + width] + ("..." if len(text) > start + width else "")
                for text in (old, new))
    return json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False)


def _explain(item: dict) -> str:
    if item["change"] == "added":
        return f"{item['id']} is new in {item['source_ref']}; reload the company layer to add it"
    if item["change"] == "removed":
        return f"{item['id']} is no longer in the source files; reload the company layer to remove it"
    if item["change"] == "relationship":
        return (f"{item['id']} {item['field']}: {_short(item['graph'])} -> {_short(item['file'])}; "
                "relationship changes need a reload")
    return f"{item['id']} is a {'/'.join(item['graph'])} in the graph but a {item['file']} in the source files"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Apply edited company source files to a FinTrace graph.")
    parser.add_argument("--graph", required=True, help="graph key, e.g. fintrace:my-copy")
    parser.add_argument("--sync", action="store_true", help="update the nodes whose source records changed")
    parser.add_argument("--root", type=Path, default=COMPANY_DIR, help="company source directory")
    parser.add_argument("--allow-main", action="store_true", help="allow writing to the main demo graph")
    args = parser.parse_args(argv)
    if not args.sync:
        parser.error("nothing to do; pass --sync")
    if args.graph in {graph_key("acme-pay"), graph_key(get_settings().default_tenant)} and not args.allow_main:
        parser.error(f"{args.graph} is the main demo graph; sync a copy or pass --allow-main")
    if not graph_exists(args.graph):
        parser.error(f"{args.graph} does not exist")

    result = sync_company(get_db().select_graph(args.graph), args.root)
    for update in result["updates"]:
        print(f"updated {update['id']} from {update['source_ref']} (topology_rev {update['rev']})")
        for name, (old, new) in update["fields"].items():
            old, new = _shown(old, new)
            if len(old) + len(new) <= 60:
                print(f"    {name}: {old} -> {new}")
            else:
                print(f"    {name}:\n      - {old}\n      + {new}")
    for item in result["unsupported"]:
        print(f"not applied: {_explain(item)}")
    if not result["updates"] and not result["unsupported"]:
        print("no changes")
    print(f"{args.graph} evidence revision: {result['rev']}")


if __name__ == "__main__":
    main()
