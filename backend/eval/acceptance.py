"""The C6 acceptance checks: one row per check, each backed by named tests, measured results or repository
facts, recorded with its evidence in backend/eval/acceptance.json.

    python -m backend.eval.acceptance                        # every row; writes acceptance.json
    python -m backend.eval.acceptance --clean-start          # also: fresh clone from origin, compose up, smoke, tests
    python -m backend.eval.acceptance --confirmed cursor-mcp # items a person has checked by hand
    python -m backend.eval.acceptance --dry-run              # print, write nothing

A row passes when every test it names passed and every check it makes holds. A named test the suite did not
run counts as failed, so renaming a test cannot pass a row. A row whose automated checks pass but which lists
items only a person can confirm stays pending until they are confirmed with --confirmed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from backend.app import company
from backend.app.config import ROOT, get_settings
from backend.app.db import get_db, graph_key
from backend.app.tenants import pristine_key
from backend.eval.common import EVAL_DIR, RESULTS, RUNS, now
from backend.eval.run_eval import MEMORY

OUT = EVAL_DIR / "acceptance.json"
README = ROOT / "README.md"
CHECKPOINT_REPORT = ROOT / "backend" / "tests" / "checkpoint_report.json"
PLANNING_DOCS = {"MASTER_PLAN.md", "BUILD_PLAN.md", "FINAL_PLAN.md", "PRIZE_EXECUTION_PLAN.md",
                 "PRIZE_EXECUTION_PLAN_REVIEW.md", "CODEX_IMPLEMENTATION_REVIEW.md"}
NEVER_TRACKED = (".env", "data/raw/", ".venv/", "frontend/node_modules/", "frontend/dist/")
BANNED_WORDS = ("100%", "undeniable")
TRACKS = ("Track 1: Connected Data", "Track 2: Memory & Coordination", "Track 3: Company Brain")
README_SECTIONS = ("## Verify our claims", "## Graph data model", "## Core Cypher", "## Evaluation",
                   "## Limitations", "## AI assistance", "## Data and license")
PLACEHOLDER = "link added at submission"
CLEAN_START_PORTS = {"APP_PORT": 8100, "FALKORDB_PUBLISHED_PORT": 6390, "FALKORDB_BROWSER_PORT": 3100}


@dataclass
class Context:
    results: dict
    tests: dict[str, str]
    clean_start: bool
    confirmed: set[str]
    readme: str = field(default_factory=lambda: README.read_text() if README.exists() else "")


Check = Callable[[Context], "tuple[bool | None, str]"]


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _counts(counter: Counter) -> str:
    return ", ".join(f"{n} {k}" for k, n in counter.most_common())


def model_runs(ctx: Context) -> tuple[bool, str]:
    llm = ctx.results.get("controlled", {}).get("llm")
    if not llm:
        return False, "no LLM runs in results.json"
    used = Counter(r["planner_used"] for r in llm["runs"])
    live = sorted(RUNS.glob("*.json"))
    return (used["llm"] > 0 and bool(live),
            f"{len(llm['runs'])} controlled runs with {llm['config']['model']} ({_counts(used)}); "
            f"{len(live)} live run logs in backend/eval/runs/")


def failures_visible(ctx: Context) -> tuple[bool, str]:
    llm = ctx.results["controlled"]["llm"]
    failed = sum(1 for r in llm["runs"] if r.get("failure"))
    listed = sum(len(c["failures"]) for c in llm["summary"]["conditions"].values())
    recorded = all("planner_used" in r and "provider_retries" in r for r in llm["runs"])
    retries = sum(r["provider_retries"] or 0 for r in llm["runs"])
    return (recorded and failed == listed,
            f"every run records its planner and provider retries; {failed} failed run(s), {listed} listed in the "
            f"summary; {retries} provider retries")


def procedure_deltas(ctx: Context) -> tuple[bool, str]:
    llm = ctx.results["controlled"]["llm"]
    warm = llm["summary"]["warm_vs_cold"]
    split = {c["case_id"]: c["split"] for c in json.loads((EVAL_DIR / "cases.json").read_text())["cases"]}
    used = warm["playbook_used_on"]
    deltas = all({"llm_requests", "llm_tokens"} <= warm["cases"][c].keys() for c in used)
    memory = llm["config"].get("memory", {})
    caching_off = memory.get("cold") == memory.get("warm") == MEMORY["warm"] == "procedures"
    return (bool(used) and all(split[c] == "heldout" for c in used) and deltas and caching_off,
            f"playbook applied to {', '.join(used)} (all held out), with cold and warm requests and tokens "
            f"published; cold and warm read memory in '{memory.get('warm')}' mode, never the subject's findings")


def contention_sound(ctx: Context) -> tuple[bool, str]:
    s = ctx.results["contention"]["summary"]
    return (s["runs"] >= 5 and s["violations"] == 0 and s["late_commits_accepted"] == 0,
            f"{s['runs']} runs: {s['violations']} invariant violations, {s['late_commits_refused']} late commits "
            f"refused and {s['late_commits_accepted']} accepted, {s['crashed_claims_recovered']} crashed claims "
            "recovered")


def company_agrees(ctx: Context) -> tuple[bool, str]:
    files = {r.id: r.content_hash for r in company.read_records()}
    tenant, notes, ok = get_settings().default_tenant, [], True
    for key in (graph_key(tenant), pristine_key(tenant)):
        rows = get_db().select_graph(key).ro_query(
            "MATCH (n:Entity) WHERE n.content_hash IS NOT NULL RETURN n.id, n.content_hash").result_set
        graph = dict(rows)
        differ = sorted(i for i, h in files.items() if graph.get(i) != h)
        extra = sorted(graph.keys() - files.keys())
        ok = ok and not differ and not extra
        notes.append(f"{key}: {len(files) - len(differ)}/{len(files)} source records match"
                     + (f", differ: {differ}" if differ else "") + (f", not in the files: {extra}" if extra else ""))
    return ok, "; ".join(notes)


def approval_labelled(ctx: Context) -> tuple[bool, str]:
    inbox = (ROOT / "frontend" / "src" / "components" / "ApprovalsView.tsx").read_text().lower()
    ok = "simulated" in inbox and "simulated, not authenticated" in ctx.readme
    return ok, "the Approval Inbox and its sign-off button say sign-off is simulated; so does the README"


def eval_recorded(ctx: Context) -> tuple[bool, str]:
    r, problems = ctx.results, []
    if not (r.get("controlled") and r.get("retrieval", {}).get("all")):
        return False, "controlled and IBM retrieval results must both be saved, under separate keys"
    for planner, res in r["controlled"].items():
        missing = [k for k in ("graph", "dataset", "commit", "max_steps", "result_chars", "search_node_budget",
                               "search_time_budget_s", "lookup_page_size", "cases_sha256", "memory", "scoring")
                   if k not in res["config"]]
        problems += [f"controlled.{planner} config lacks {', '.join(missing)}"] if missing else []
        if any("failures" not in c for c in res["summary"]["conditions"].values()):
            problems.append(f"controlled.{planner} does not list its failures")
    llm = r["controlled"].get("llm")
    if llm and not all(c.get("llm_tokens") and c.get("llm_requests") for c in llm["summary"]["conditions"].values()):
        problems.append("LLM runs lack token or request counts")
    retrieval = r["retrieval"]["all"]["config"]
    missing = [k for k in ("graph", "dataset", "commit", "output_limits", "seed_rule", "instances_sha256")
               if k not in retrieval]
    problems += [f"retrieval config lacks {', '.join(missing)}"] if missing else []
    dirty = [name for name, cfg in [*((f"controlled.{p}", v["config"]) for p, v in r["controlled"].items()),
                                    ("retrieval.all", retrieval)] if cfg.get("uncommitted_backend_changes")]
    note = (f"; recorded with uncommitted backend changes: {', '.join(dirty)}" if dirty
            else "; every result was recorded from a committed backend")
    return not problems, ("; ".join(problems) if problems else
                          "controlled (per planner) and IBM retrieval saved separately, each with graph, dataset "
                          "version, commit, output limits, seed rule, failures and, for the LLM, requests and "
                          "tokens") + note


def readme_complete(ctx: Context) -> tuple[bool, str]:
    text = ctx.readme
    missing = [t for t in TRACKS if t not in text] + [s for s in README_SECTIONS if s not in text]
    banned = [w for w in BANNED_WORDS if w.lower() in text.lower()]
    ok = bool(text) and not missing and not banned
    return ok, ("README names all three tracks and has every required section; none of "
                f"{', '.join(BANNED_WORDS)}" if ok else f"missing: {missing}; banned words: {banned}")


def repository_clean(ctx: Context) -> tuple[bool, str]:
    tracked = _git("ls-files").splitlines()
    leaks = [p for p in tracked if p in PLANNING_DOCS or p.endswith(".tsbuildinfo")
             or any(p == n or (n.endswith("/") and p.startswith(n)) for n in NEVER_TRACKED)]
    return not leaks, (f"none of {len(tracked)} tracked files is a planning doc, .env, raw data, virtualenv, "
                       "node_modules, build output or tsbuildinfo" if not leaks else f"tracked by mistake: {leaks}")


def clean_start(ctx: Context) -> tuple[bool | None, str]:
    if not ctx.clean_start:
        return None, "not run this time (pass --clean-start)"
    return run_clean_start(_git("remote", "get-url", "origin"))


def links_ready(ctx: Context) -> tuple[bool | None, str]:
    if PLACEHOLDER in ctx.readme:
        return None, f"the README still says '{PLACEHOLDER}'"
    return True, "the README links the video and the live demo"


@dataclass
class Row:
    check: str
    required: str
    tests: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    manual: dict[str, str] = field(default_factory=dict)


ROWS = [
    Row("Real agent",
        "Actual model and external MCP runs; planner mode and all failed/retried runs visible; no schema examples "
        "point at the hidden answer.",
        ["test_agent.py::test_tool_calls_go_back_with_their_provider_signature",
         "test_agent.py::test_an_llm_failure_hands_over_to_the_fallback_without_repeating_calls",
         "test_agent.py::test_a_per_minute_rate_limit_is_waited_out_but_a_daily_quota_falls_back",
         "test_mcp_stdio.py::test_the_stdio_server_lists_the_eleven_tools_and_answers",
         "test_leaks.py::test_tool_schemas_and_the_prompt_name_nothing_from_the_scenario",
         "test_leaks.py::test_renamed_ids_are_gone_from_the_graph",
         "test_graph_checkpoints.py::test_no_ground_truth_in_graph"],
        [model_runs, failures_visible],
        {"cursor-mcp": "A tool call from Cursor's MCP client against the seeded graph returns a result"}),
    Row("Route and coverage",
        "Alternate valid route found despite a shorter IP path; device hops counted correctly; timeout/cap/budget "
        "and out-of-scope targets do not become unqualified CLEAR results.",
        ["test_agent.py::test_a_valid_route_is_found_despite_a_shorter_ip_path",
         "test_agent.py::test_a_device_bridge_costs_two_hops_so_four_hops_clears_nobody",
         "test_tools.py::test_eligible_routes_keep_direction_and_a_device_bridge_costs_two_hops",
         "test_agent.py::test_a_capped_or_timed_out_search_is_insufficient_not_clear",
         "test_agent.py::test_a_sanctioned_account_beyond_six_hops_gets_a_qualified_clear",
         "test_agent.py::test_sharing_only_a_proxy_ip_with_a_sanctioned_route_is_association_not_exposure",
         "test_guardrails.py::test_clear_needs_the_prescribed_depth_or_an_exhausted_frontier",
         "test_guardrails.py::test_an_eligible_route_is_suspicious_even_when_another_search_was_cut_short",
         "test_guardrails.py::test_the_card_states_the_scope_behind_clear_and_insufficient"]),
    Row("Bound claims",
        "Swapping monthly volume with exposure, another subject's path length, or another approval's status is "
        "rejected. Authoritative output cites the right structured result.",
        ["test_guardrails.py::test_swapping_monthly_volume_for_exposure_is_rejected",
         "test_guardrails.py::test_another_subjects_path_length_is_rejected",
         "test_guardrails.py::test_another_approvals_status_is_rejected",
         "test_guardrails.py::test_facts_cite_their_tool_result_and_route_dependencies",
         "test_guardrails.py::test_statements_that_restate_a_fact_are_bound_to_it",
         "test_guardrails.py::test_every_statement_cites_facts_and_unknown_entities_say_nothing_else",
         "test_agent.py::test_model_statements_are_bound_to_facts_or_rejected",
         "test_agent.py::test_the_model_verdict_never_overrides_the_facts"]),
    Row("Mutation consistency",
        "A transfer add/update/delete changes raw and summary edges consistently; same-second changes invalidate; "
        "deletion and changed aggregate findings cannot retain stale CURRENT facts.",
        ["test_memory.py::test_transfer_writes_keep_raw_and_summary_edges_consistent",
         "test_memory.py::test_a_change_in_the_same_second_as_the_fact_still_stales_it",
         "test_memory.py::test_deleting_a_route_transfer_updates_its_summary_and_stales_the_route",
         "test_memory.py::test_deleting_a_route_node_stales_the_route_through_a_missing_dependency",
         "test_memory.py::test_an_unrelated_transfer_keeps_the_route_and_refreshes_only_the_aggregates",
         "test_memory.py::test_a_clear_finding_goes_stale_on_any_change",
         "test_memory.py::test_identity_and_sanction_changes_bump_the_revision"]),
    Row("Persistence and concurrency",
        "A new session recalls an episode; mutation produces STALE and SUPERSEDES; a concurrent change cannot be "
        "persisted as a fresh fact; duplicate completion is prevented.",
        ["test_memory.py::test_a_new_session_answers_from_memory",
         "test_memory.py::test_an_unrelated_transfer_keeps_the_route_and_refreshes_only_the_aggregates",
         "test_guardrails.py::test_stale_memory_says_what_changed_and_fresh_facts_decide",
         "test_memory.py::test_a_change_during_the_investigation_is_not_cached",
         "test_memory.py::test_sign_off_is_refused_once_the_evidence_changed",
         "test_coordination.py::test_an_expired_claim_goes_back_to_the_queue_and_its_token_can_no_longer_commit",
         "test_coordination.py::test_twenty_claimants_and_a_sweeper_lose_nothing_and_complete_each_alert_once",
         "test_coordination.py::test_two_cases_racing_to_freeze_the_same_accounts_cannot_both_file"]),
    Row("Procedural memory",
        "A different held-out subject uses a learned, parameterized procedure; cold/warm comparisons disable "
        "subject-answer caching and publish actual deltas.",
        ["test_playbook.py::test_a_playbook_is_learned_from_an_approved_run_and_holds_roles_not_ids",
         "test_playbook.py::test_it_matches_held_out_subjects_by_topology_alone",
         "test_playbook.py::test_a_held_out_positive_is_investigated_on_its_own_evidence",
         "test_playbook.py::test_on_a_lookalike_it_stops_where_the_evidence_does_and_the_answer_stays_clear",
         "test_playbook.py::test_remembered_findings_take_precedence_and_procedures_mode_never_reads_them"],
        [procedure_deltas]),
    Row("Coordination and tenants",
        "Three workers demonstrate a real graph-state handoff; contention check has no lost work or duplicate "
        "committed completion; tenant A's facts/actions are absent from B.",
        ["test_coordination.py::test_three_hunters_drain_the_queue_without_losing_or_duplicating_work",
         "test_coordination.py::test_a_later_hunter_reuses_an_earlier_hunters_route_and_defers_to_its_freeze",
         "test_coordination.py::test_cases_merge_only_on_a_high_risk_entity_or_two_shared_entities",
         "test_coordination.py::test_failed_alerts_are_retried_until_the_last_attempt_then_stay_failed_and_visible",
         "test_tenants.py::test_memory_queue_approvals_revision_and_reset_stay_inside_their_tenant",
         "test_tenants.py::test_tenant_names_are_checked_before_any_graph_is_touched"],
        [contention_sound]),
    Row("Company knowledge",
        "Policy source and graph version agree; waiver compliance updates independently of sanctions risk; "
        "simulated approval is clearly identified.",
        ["test_company.py::test_every_company_node_carries_its_provenance",
         "test_company.py::test_unchanged_files_cause_no_writes",
         "test_company.py::test_raising_the_waiver_threshold_clears_the_breach_in_one_revision",
         "test_company.py::test_editing_only_the_policy_text_is_an_edit_too",
         "test_company.py::test_mistakes_in_the_source_files_fail_loudly",
         "test_memory.py::test_a_policy_edit_refreshes_the_waiver_check_and_keeps_the_route",
         "test_tools.py::test_governance_finds_the_breach_and_the_on_call_approver",
         "test_tools.py::test_a_freeze_needs_an_on_call_l3_and_happens_only_on_sign_off"],
        [company_agrees, approval_labelled]),
    Row("Evaluation",
        "Controlled case results and IBM retrieval results are labelled separately; output limits, graph version, "
        "seeds, failures, and cost metrics are recorded.",
        ["test_eval.py::test_an_incomplete_run_is_correct_if_insufficient_or_if_it_established_the_subjects_own_route",
         "test_eval.py::test_the_lookup_baseline_follows_money_out_and_shared_devices_but_never_ips",
         "test_eval.py::test_the_lookup_baseline_clears_only_once_every_page_within_depth_is_read",
         "test_eval.py::test_the_eval_endpoint_serves_summaries_without_the_per_run_records"],
        [eval_recorded]),
    Row("Submission",
        "Clean-start reproduction, usable repository/video links, required README/AI disclosure, accurate "
        "selected-track claims, and saved submission confirmation.",
        [],
        [readme_complete, repository_clean, clean_start, links_ready],
        {"ai-disclosure": "The participant has reviewed the AI-assistance section and their contributions in it",
         "video": "The demo video is public and linked from the README and the submission",
         "submitted": "The submission form is filed and its confirmation saved"}),
]


def run_tests() -> tuple[dict[str, str], dict]:
    """The whole suite once, outcomes keyed 'test_file.py::test_name'; a parametrized test passes only if
    every case did. The checkpoint report the suite rewrites is put back, so a run leaves the tree as it found it."""
    report = CHECKPOINT_REPORT.read_bytes() if CHECKPOINT_REPORT.exists() else None
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        started = time.perf_counter()
        subprocess.run([sys.executable, "-m", "pytest", "backend/tests", "-q", "-p", "no:cacheprovider",
                        f"--junitxml={junit}"], cwd=ROOT, capture_output=True, text=True)
        seconds = round(time.perf_counter() - started, 1)
        cases = Counter()
        outcomes: dict[str, str] = {}
        for case in ET.parse(junit).getroot().iter("testcase"):
            kinds = {child.tag for child in case}
            outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                       else "skipped" if "skipped" in kinds else "passed")
            cases[outcome] += 1
            name = f"{case.get('classname', '').rsplit('.', 1)[-1]}.py::{case.get('name', '').split('[')[0]}"
            if outcomes.get(name, "passed") == "passed":
                outcomes[name] = outcome
    if report is not None:
        CHECKPOINT_REPORT.write_bytes(report)
    return outcomes, {"seconds": seconds, **cases}


def _wait_for_health(base: str, deadline: float) -> dict:
    while True:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as reply:
                health = json.loads(reply.read())
                if health.get("status") == "ok":
                    return health
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"{base}/api/health did not answer in time")
        time.sleep(2)


def _investigate(base: str, entity_id: str) -> str:
    url = f"{base}/api/investigate?entity_id={entity_id}&mode=fallback"
    with urllib.request.urlopen(url, timeout=180) as stream:
        for raw in stream:
            line = raw.decode().strip()
            if line.startswith("data:"):
                event = json.loads(line[5:])
                if event.get("type") == "final":
                    return event["verdict"]
    raise RuntimeError("the investigation ended without a final event")


def run_clean_start(repo: str, timeout_s: float = 900) -> tuple[bool, str]:
    """Clone the published repository, start it with Docker Compose on spare ports, investigate NovaPay with the
    deterministic planner, run the tests inside the container, and tear everything down."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fintrace-acceptance-") as tmp:
        clone = Path(tmp) / "fintrace"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", repo, str(clone)], check=True)
        commit = _git("rev-parse", "--short", "HEAD", cwd=clone)
        env = {**os.environ, "COMPOSE_PROJECT_NAME": "fintrace-acceptance",
               **{k: str(v) for k, v in CLEAN_START_PORTS.items()}}
        compose = ["docker", "compose"]
        try:
            subprocess.run([*compose, "up", "--build", "-d"], cwd=clone, env=env, check=True, capture_output=True,
                           text=True, timeout=timeout_s)
            base = f"http://localhost:{CLEAN_START_PORTS['APP_PORT']}"
            health = _wait_for_health(base, started + timeout_s)
            up = round(time.monotonic() - started)
            verdict = _investigate(base, "merch-nova-pay")
            tests = subprocess.run([*compose, "exec", "-T", "app", "python", "-m", "pytest", "backend/tests", "-q",
                                    "-p", "no:cacheprovider"], cwd=clone, env=env, capture_output=True, text=True,
                                   timeout=900)
            last = (tests.stdout.strip().splitlines() or [""])[-1]
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            detail = getattr(exc, "stderr", None) or ""
            return False, f"clean start of {repo} at {commit} failed: {exc} {str(detail)[-400:]}".strip()
        finally:
            subprocess.run([*compose, "down", "-v", "--remove-orphans"], cwd=clone, env=env, capture_output=True,
                           timeout=300)
    ok = verdict == "SUSPICIOUS" and tests.returncode == 0
    return ok, (f"fresh clone of {repo} at {commit}: up and seeded in {up} s ({health['nodes']:,} nodes), "
                f"NovaPay investigated as {verdict} by the deterministic planner, container tests: {last}")


def evaluate_row(row: Row, ctx: Context) -> dict:
    tests = [{"test": t, "outcome": ctx.tests.get(t, "missing")} for t in row.tests]
    checks = []
    for check in row.checks:
        try:
            ok, evidence = check(ctx)
        except Exception as exc:  # a check that cannot run is a failed check, with the reason recorded
            ok, evidence = False, f"{type(exc).__name__}: {exc}"
        checks.append({"check": check.__name__, "ok": ok, "evidence": evidence})
    manual = [{"item": key, "required": text, "confirmed": key in ctx.confirmed} for key, text in row.manual.items()]
    failed = any(t["outcome"] != "passed" for t in tests) or any(c["ok"] is False for c in checks)
    pending = any(c["ok"] is None for c in checks) or any(not m["confirmed"] for m in manual)
    status = "fail" if failed else "pending" if pending else "pass"
    return {"check": row.check, "required": row.required, "status": status, "tests": tests, "checks": checks,
            "manual": manual}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clean-start", action="store_true", help="also reproduce from a fresh clone (Docker)")
    parser.add_argument("--confirmed", nargs="*", default=[], help="manual items a person has checked")
    parser.add_argument("--dry-run", action="store_true", help="print the rows; leave acceptance.json alone")
    args = parser.parse_args()
    known = {key for row in ROWS for key in row.manual}
    if unknown := set(args.confirmed) - known:
        parser.error(f"unknown manual items {sorted(unknown)}; known: {sorted(known)}")

    started = now()
    tests, suite = run_tests()
    ctx = Context(json.loads(RESULTS.read_text()), tests, args.clean_start, set(args.confirmed))
    rows = [evaluate_row(row, ctx) for row in ROWS]
    meta = get_db().select_graph(graph_key(get_settings().default_tenant)).ro_query(
        "MATCH (m:Meta {key: 'dataset'}) RETURN m.fixture_version, m.rev").result_set
    result = {
        "started_at": started, "finished_at": now(),
        "commit": _git("rev-parse", "--short", "HEAD"),
        "uncommitted_changes": [line[3:] for line in _git("status", "--porcelain").splitlines()],
        "graph": {"key": graph_key(get_settings().default_tenant), "fixture_version": meta[0][0] if meta else None,
                  "rev": meta[0][1] if meta else None},
        "suite": suite,
        "summary": dict(Counter(r["status"] for r in rows)),
        "rows": rows,
    }
    for row in rows:
        print(f"{row['status'].upper():<8} {row['check']}")
        for t in row["tests"]:
            if t["outcome"] != "passed":
                print(f"           {t['outcome']}: {t['test']}")
        for c in row["checks"]:
            print(f"           {'ok' if c['ok'] else 'not run' if c['ok'] is None else 'FAILED'}: {c['evidence']}")
        for m in row["manual"]:
            if not m["confirmed"]:
                print(f"           to confirm by hand ({m['item']}): {m['required']}")
    print(f"suite: {suite}; rows: {result['summary']}")
    if not args.dry_run:
        OUT.write_text(json.dumps(result, indent=1) + "\n")
        print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
