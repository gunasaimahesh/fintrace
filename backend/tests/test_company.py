"""Company source files: they build the company layer with provenance, and sync edits as revisions."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from backend.app import company as c
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.contracts import current_rev
from backend.app.db import get_db

DELETE_COMPANY_LAYER = "MATCH (n) WHERE " + " OR ".join(f"n:{label}" for label in c.KINDS) + " DETACH DELETE n"
POLICY_FILE = "policies/pol-aml-sec-4.md"


class WriteLog:
    """Passes reads through and records every write query."""

    def __init__(self, graph):
        self.graph, self.writes = graph, []

    def ro_query(self, *args, **kwargs):
        return self.graph.ro_query(*args, **kwargs)

    def query(self, cypher, *args, **kwargs):
        self.writes.append(cypher)
        return self.graph.query(cypher, *args, **kwargs)


def node(graph, entity_id: str) -> dict:
    return dict(graph.ro_query("MATCH (n:Entity {id: $id}) RETURN n {.*}", {"id": entity_id}).result_set[0][0])


def edit(path: Path, *replacements: tuple[str, str]) -> None:
    """Replace the first occurrence of each `old` text."""
    text = path.read_text()
    for old, new in replacements:
        assert old in text, f"{old!r} is not in {path.name}"
        text = text.replace(old, new, 1)
    path.write_text(text)


def comparable(governance: dict) -> dict:
    """trace_governance output without timing, its lists in a stable order."""
    out = {k: v for k, v in governance.items() if k != "elapsed_ms"}
    out["decisions"] = sorted(({**d, "sources": sorted(d["sources"], key=lambda x: x["id"])}
                               for d in out["decisions"]), key=lambda d: d["id"])
    out["approver_other_decisions"] = sorted(out["approver_other_decisions"], key=lambda d: d["decision_id"])
    out["on_call_approvers"] = sorted(out["on_call_approvers"], key=lambda e: e["id"])
    return out


@pytest.fixture
def graph(fresh_key):
    """A private copy whose company layer was deleted and loaded again from the source files."""
    graph = get_db().select_graph(fresh_key)
    graph.query(DELETE_COMPANY_LAYER)
    c.load_company(graph)
    return graph


@pytest.fixture
def company_dir(tmp_path) -> Path:
    return Path(shutil.copytree(c.COMPANY_DIR, tmp_path / "company"))


def test_every_company_node_carries_its_provenance():
    for _, props in c.build_company_spec().nodes:
        assert props["source_path"].startswith("backend/data/company/") and len(props["content_hash"]) == 64


@pytest.mark.parametrize("path, old, new, error", [
    ("org.toml", 'clearance = "L2"', 'clearance = "L3"',
     "org.toml#emp-rohan-mehta: clearance 'L3' does not match clearance_level 2"),
    ("decisions.toml", 'governed_by = "pol-aml-sec-4"', 'governed_by = "pol-aml-sec-9"',
     "decisions.toml#dec-waiver-77: governed_by 'pol-aml-sec-9' is not a PolicyDoc in the company files"),
    (POLICY_FILE, "waiver_threshold_monthly", "waiver_treshold_monthly",
     f"{POLICY_FILE}: missing waiver_threshold_monthly"),
    ("tickets/jira-export.json", '"2026-09-15T08:00:00Z"', '"2026-09-15 08:00"',
     "tickets/jira-export.json#tick-risk-408: opened_at must be an ISO-8601 timestamp with an offset"),
])
def test_mistakes_in_the_source_files_fail_loudly(company_dir, path, old, new, error):
    edit(company_dir / path, (old, new))
    with pytest.raises(ValueError, match=re.escape(error)):
        c.build_company_spec(company_dir)


@pytest.mark.graph
def test_a_company_layer_loaded_from_files_answers_governance_like_the_seeded_one(fresh_key):
    graph = get_db().select_graph(fresh_key)
    seeded = q.trace_governance(graph, s.NOVAPAY)
    graph.query(DELETE_COMPANY_LAYER)
    assert q.trace_governance(graph, s.NOVAPAY)["decisions"] == []

    counts = c.load_company(graph)
    assert counts["nodes"] == {"Team": 3, "Employee": 5, "PolicyDoc": 1, "Ticket": 2, "Message": 1, "Decision": 2}
    assert sum(counts["edges"].values()) == 21
    loaded = q.trace_governance(graph, s.NOVAPAY)
    assert comparable(loaded) == comparable(seeded)
    [waiver] = loaded["decisions"]
    assert (waiver["id"], waiver["approved_by"]["id"]) == (s.WAIVER, s.ROHAN)
    assert [v["policy_id"] for v in waiver["violations"]] == [s.POLICY]
    assert sorted(src["id"] for src in waiver["sources"]) == sorted([s.TICKET, s.MESSAGE])
    assert [e["id"] for e in loaded["on_call_approvers"]] == [s.PRIYA]

    hashes = {record.id: record.content_hash for record in c.read_records()}
    expected = {
        s.WAIVER: ("decisions.toml", "decisions.toml#dec-waiver-77"),
        s.ROHAN: ("org.toml", "org.toml#emp-rohan-mehta"),
        s.PRIYA: ("org.toml", "org.toml#emp-priya-nair"),
        s.POLICY: (POLICY_FILE, POLICY_FILE),
        s.TICKET: ("tickets/jira-export.json", "tickets/jira-export.json#tick-risk-408"),
        s.MESSAGE: ("slack/merchant-onboarding.jsonl", "slack/merchant-onboarding.jsonl:1"),
    }
    for entity_id, (path, ref) in expected.items():
        props = node(graph, entity_id)
        assert {k: props[k] for k in c.PROVENANCE} == {"source_path": f"backend/data/company/{path}",
                                                       "source_ref": ref, "content_hash": hashes[entity_id]}
    with pytest.raises(ValueError, match="already in the graph"):
        c.load_company(graph)


@pytest.mark.graph
def test_unchanged_files_cause_no_writes(fresh_key, company_dir):
    graph = WriteLog(get_db().select_graph(fresh_key))
    rev = current_rev(graph)
    unchanged = {"changed": [], "unsupported": [], "rev": rev, "updates": []}
    assert c.sync_company(graph) == unchanged

    graph.graph.query(DELETE_COMPANY_LAYER)
    c.load_company(graph.graph)
    assert c.sync_company(graph) == unchanged
    edit(company_dir / POLICY_FILE, ("per month\nrequires approval", "per\nmonth requires approval"))
    assert c.sync_company(graph, company_dir) == unchanged
    assert graph.writes == [] and current_rev(graph) == rev


@pytest.mark.graph
def test_raising_the_waiver_threshold_clears_the_breach_in_one_revision(graph, company_dir):
    before = q.trace_governance(graph, s.NOVAPAY)
    decision_node = node(graph, s.WAIVER)
    rev = current_rev(graph)
    edit(company_dir / POLICY_FILE, ('version = "2026.2"', 'version = "2026.3"'),
         ("waiver_threshold_monthly = 25_000_000", "waiver_threshold_monthly = 50_000_000"))

    result = c.sync_company(graph, company_dir)
    assert (result["changed"], result["unsupported"], result["rev"]) == ([s.POLICY], [], rev + 1)
    assert result["updates"] == [{"id": s.POLICY, "source_ref": POLICY_FILE, "rev": rev + 1, "fields": {
        "version": ["2026.2", "2026.3"], "waiver_threshold_monthly": [25_000_000, 50_000_000]}}]
    policy = node(graph, s.POLICY)
    assert current_rev(graph) == policy["topology_rev"] == rev + 1
    assert (policy["version"], policy["waiver_threshold_monthly"]) == ("2026.3", 50_000_000)
    assert policy["content_hash"] == next(r.content_hash for r in c.read_records(company_dir) if r.id == s.POLICY)

    [waiver_before], [waiver_after] = before["decisions"], q.trace_governance(graph, s.NOVAPAY)["decisions"]
    assert [v["policy_id"] for v in waiver_before["violations"]] == [s.POLICY]
    assert waiver_after["violations"] == []
    assert (waiver_after["merchant_monthly_volume_usd"], waiver_after["policy"]["version"]) == (410_000, "2026.3")
    decision_fields = [k for k in waiver_before if k not in ("policy", "violations")]
    assert {k: waiver_after[k] for k in decision_fields} == {k: waiver_before[k] for k in decision_fields}
    assert node(graph, s.WAIVER) == decision_node

    log = WriteLog(graph)
    assert c.sync_company(log, company_dir)["changed"] == [] and log.writes == []


@pytest.mark.graph
def test_editing_only_the_policy_text_is_an_edit_too(graph, company_dir):
    rev, old_hash = current_rev(graph), node(graph, s.POLICY)["content_hash"]
    edit(company_dir / POLICY_FILE,
         ("must be excluded from the freeze.", "must be excluded from the freeze and named in the request."))

    result = c.sync_company(graph, company_dir)
    assert (result["changed"], result["rev"]) == ([s.POLICY], rev + 1)
    assert list(result["updates"][0]["fields"]) == ["text"]
    policy = node(graph, s.POLICY)
    assert policy["text"].endswith("must be excluded from the freeze and named in the request.")
    assert policy["content_hash"] != old_hash and policy["topology_rev"] == rev + 1
    assert policy["version"] == "2026.2"


@pytest.mark.graph
def test_relationship_edits_and_added_or_removed_records_are_reported_not_applied(graph, company_dir):
    rev = current_rev(graph)
    edit(company_dir / "decisions.toml",
         ('summary = "Device attestation waived for NovaPay onboarding"',
          'summary = "Device attestation waived for NovaPay go-live"'),
         ('approved_by = "emp-rohan-mehta"', 'approved_by = "emp-meera-iyer"'),
         ('sourced_from = ["tick-risk-408", "msg-slack-9921"]', 'sourced_from = ["tick-risk-408"]'))
    (company_dir / "slack/merchant-onboarding.jsonl").write_text("")
    tickets_file = company_dir / "tickets/jira-export.json"
    tickets = json.loads(tickets_file.read_text())
    tickets_file.write_text(json.dumps([*tickets, {**tickets[0], "id": "tick-risk-999"}]))

    result = c.sync_company(graph, company_dir)
    assert (result["changed"], result["rev"]) == ([s.WAIVER], rev + 1)
    assert result["unsupported"] == [
        {"id": "tick-risk-999", "change": "added", "source_ref": "tickets/jira-export.json#tick-risk-999"},
        {"id": s.WAIVER, "change": "relationship", "field": "approved_by", "graph": s.ROHAN, "file": "emp-meera-iyer"},
        {"id": s.WAIVER, "change": "relationship", "field": "sourced_from", "graph": sorted([s.TICKET, s.MESSAGE]),
         "file": [s.TICKET]},
        {"id": s.MESSAGE, "change": "removed", "source_ref": "slack/merchant-onboarding.jsonl:1"},
    ]
    [waiver] = q.trace_governance(graph, s.NOVAPAY)["decisions"]
    assert waiver["summary"] == "Device attestation waived for NovaPay go-live"
    assert waiver["approved_by"]["id"] == s.ROHAN
    assert sorted(src["id"] for src in waiver["sources"]) == sorted([s.TICKET, s.MESSAGE])

    log = WriteLog(graph)
    again = c.sync_company(log, company_dir)
    assert (again["changed"], again["unsupported"], log.writes) == ([], result["unsupported"], [])


@pytest.mark.graph
def test_the_cli_refuses_the_main_graph_and_prints_what_changed(graph, fresh_key, company_dir, capsys):
    with pytest.raises(SystemExit):
        c.main(["--graph", "fintrace:acme-pay", "--sync"])
    assert "pass --allow-main" in capsys.readouterr().err

    rev = current_rev(graph)
    edit(company_dir / "org.toml", ('clearance = "L2"', 'clearance = "L3"'),
         ("clearance_level = 2", "clearance_level = 3"))
    c.main(["--graph", fresh_key, "--sync", "--root", str(company_dir)])
    assert capsys.readouterr().out.splitlines() == [
        f"updated {s.ROHAN} from org.toml#{s.ROHAN} (topology_rev {rev + 1})",
        '    clearance: "L2" -> "L3"',
        "    clearance_level: 2 -> 3",
        f"{fresh_key} evidence revision: {rev + 1}",
    ]
    assert q.trace_governance(graph, s.NOVAPAY)["decisions"][0]["violations"] == []
