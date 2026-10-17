"""Facts, the verdict rule, statement binding and the supplementary text checks, without a database."""

from __future__ import annotations

from backend.app import guardrails as gr
from backend.app.guardrails import AnswerState, EvidenceLedger, Verdict

NOVA, OPS, DEVICE, VOLKOV, BALTIC, VORTEX = ("merch-nova-pay", "acct-nova-ops", "dev-fp-8821", "acct-volkov-clearing",
                                             "acct-baltic-trade", "acct-vortex-holdings")


def ledger() -> EvidenceLedger:
    ledger = EvidenceLedger()
    ledger.add({
        "nodes": {
            NOVA: {"labels": ["Account", "Merchant"], "name": "NovaPay Digital Goods"},
            OPS: {"labels": ["Account"], "name": "NovaPay Ops"},
            VORTEX: {"labels": ["Account", "Sanctioned"], "name": "Vortex Holdings Ltd"},
            "emp-rohan-mehta": {"labels": ["Employee"], "name": "Rohan Mehta"},
            "dec-waiver-77": {"labels": ["Decision"], "name": None},
        },
        "edges": [[NOVA, "FLOWS_TO", OPS], ["emp-rohan-mehta", "APPROVED", "dec-waiver-77"]],
    })
    return ledger


def step(src: str, dst: str, rel: str, forward: bool = True) -> dict:
    return {"from": src, "to": dst, "rel": rel, "forward": forward}


def envelope(tool: str, data: dict) -> dict:
    return {"tool": tool, "ok": True, "args": {}, "data": data, "evidence": {"nodes": {}, "edges": []},
            "error": None, "stop": None, "meta": {}}


def scope(requested: int, completed: int | None = None, exhausted: bool = False, truncated: bool = False,
          timed_out: bool = False, reached: int = 100) -> dict:
    completed = requested if completed is None else completed
    return {"requested_depth": requested, "completed_depth": completed, "exhausted": exhausted, "reached": reached,
            "truncated": truncated, "timed_out": timed_out,
            "complete": exhausted or (completed == requested and not timed_out)}


ROUTE = {
    "entity": {"id": VORTEX, "labels": ["Account", "Sanctioned"], "name": "Vortex Holdings Ltd"},
    "hops": 5,
    "path": {"nodes": [NOVA, OPS, DEVICE, VOLKOV, BALTIC, VORTEX],
             "steps": [step(NOVA, OPS, "FLOWS_TO"), step(OPS, DEVICE, "USED_DEVICE"),
                       step(DEVICE, VOLKOV, "USED_DEVICE", forward=False), step(VOLKOV, BALTIC, "FLOWS_TO"),
                       step(BALTIC, VORTEX, "FLOWS_TO")]},
    "identity_hops": [{"id": DEVICE, "type": "Device", "accounts_on_path": [OPS, VOLKOV]}],
}


def explore(depth: int, routes: tuple = (), search: dict | None = None, flagged: tuple = ()) -> dict:
    search = search or scope(depth)
    return envelope("explore_neighborhood", {
        "entity_id": NOVA, "depth": depth, "levels": [], "total_reached": search["reached"], "flagged": list(flagged),
        "scope": search, "eligible_routes": {"rule": "", "scope": search, "routes": list(routes)}})


def verdict_of(*results: dict) -> Verdict:
    return gr.derive(gr.extract_facts(list(results)), unknown=False)[1]


def run_facts() -> list:
    return gr.extract_facts([
        envelope("resolve_entity", {"found": True, "id": NOVA, "status": "ACTIVE",
                                    "profile": {"monthly_volume_usd": 410_000.0}}),
        explore(5, (ROUTE,)),
        envelope("exposure_flow", {"source_ids": [OPS, VOLKOV], "target_id": VORTEX, "max_flow_usd": 132_000,
                                   "added_via_shared_device": {}}),
        envelope("request_action", {"request_id": "apr-1", "status": "PENDING_L3", "case_id": "case-1",
                                    "target_ids": [OPS, VOLKOV], "excluded_ids": [], "policy_id": "pol-aml-sec-4",
                                    "approver": {"id": "emp-priya-nair", "clearance": "L3"}}),
    ], rev=7)


def test_an_eligible_route_is_suspicious_even_when_another_search_was_cut_short():
    assert verdict_of(explore(6, (ROUTE,), scope(6, completed=5, truncated=True))) == Verdict.SUSPICIOUS


def test_clear_needs_the_prescribed_depth_or_an_exhausted_frontier():
    assert verdict_of(explore(6)) == Verdict.CLEAR
    assert verdict_of(explore(1, search=scope(1, exhausted=True, reached=0))) == Verdict.CLEAR
    assert verdict_of(explore(4)) == Verdict.INSUFFICIENT
    assert verdict_of(explore(6, search=scope(6, completed=3, truncated=True))) == Verdict.INSUFFICIENT
    assert verdict_of(explore(6, search=scope(6, completed=2, timed_out=True))) == Verdict.INSUFFICIENT
    assert verdict_of() == Verdict.INSUFFICIENT


def test_facts_cite_their_tool_result_and_route_dependencies():
    facts = run_facts()
    route = next(f for f in facts if f.predicate == "eligible_route_hops")
    assert (route.subject, route.object, route.value, route.unit) == (NOVA, VORTEX, 5, "hops")
    assert (route.source.tool, route.source.step, route.source.result_key) == (
        "explore_neighborhood", 2, "data.eligible_routes.routes[0]")
    assert route.evidence_rev == 7 and not route.aggregate
    assert f"{OPS}|USED_DEVICE|{DEVICE}" in route.depends_on and f"{VOLKOV}|USED_DEVICE|{DEVICE}" in route.depends_on
    assert next(f for f in facts if f.predicate == "max_flow_capacity").aggregate


def test_swapping_monthly_volume_for_exposure_is_rejected():
    [result] = gr.bind([{"subject": OPS, "predicate": "max_flow_capacity", "object": VORTEX, "value": 410_000}],
                       run_facts())
    assert result["status"] == "rejected"
    assert result["reason"].startswith(f"max_flow_capacity for {OPS} → {VORTEX} is $132,000")
    assert f"$410,000 is {NOVA}'s monthly_volume" in result["reason"]


def test_another_subjects_path_length_is_rejected():
    [result] = gr.bind([{"subject": "merch-lumen-games", "predicate": "eligible_route_hops", "object": VORTEX,
                         "value": 5}], run_facts())
    assert result["status"] == "rejected"
    assert result["reason"] == (f"no tool result gives eligible_route_hops for merch-lumen-games → {VORTEX}; "
                                f"5 is {NOVA}'s eligible_route_hops (F3)")


def test_another_approvals_status_is_rejected():
    wrong_status, other_request = gr.bind([
        {"subject": "apr-1", "predicate": "action_status", "value": "APPROVED"},
        {"subject": "apr-2", "predicate": "action_status", "value": "PENDING_L3"},
    ], run_facts())
    assert wrong_status["reason"] == "action_status for apr-1 is PENDING_L3 (F5), not APPROVED"
    assert other_request["reason"] == "no tool result gives action_status for apr-2"


def test_statements_that_restate_a_fact_are_bound_to_it():
    results = gr.bind([
        {"subject": NOVA, "predicate": "eligible_route_hops", "object": VORTEX, "value": 5},
        {"subject": VOLKOV, "predicate": "max_flow_capacity", "object": VORTEX, "value": "$132,000"},
        {"subject": "apr-1", "predicate": "action_status", "value": "pending_l3"},
        {"subject": NOVA, "predicate": "owns", "value": 1},
        "not a statement",
    ], run_facts())
    assert [r.get("fact") for r in results[:3]] == ["F3", "F4", "F5"]
    assert [r["reason"] for r in results[3:]] == ["unknown predicate owns", "needs a subject and a predicate"]


def test_no_route_binds_only_to_a_complete_search():
    statement = {"subject": NOVA, "predicate": "no_eligible_route", "value": 6}
    [complete] = gr.bind([statement], gr.extract_facts([explore(6)]))
    [capped] = gr.bind([statement], gr.extract_facts([explore(6, search=scope(6, completed=3, truncated=True))]))
    assert complete == {"statement": statement, "status": "bound", "fact": "F1"}
    assert capped["reason"] == f"no tool result gives no_eligible_route for {NOVA}"


def test_text_checks_catch_unknown_ids_and_invented_edges():
    claim = {"subject": NOVA, "relation": "TRANSFERRED", "object": VORTEX}
    check = gr.check_text(f"{NOVA} paid {VORTEX} directly, as did acct-made-up-77.", ledger(), [claim])
    assert not check.passed
    assert check.unknown_ids == ["acct-made-up-77"]
    assert check.rejected_claims == [{"claim": claim, "reason": "no such edge in this run's tool results"}]
    given = gr.check_text(f"Unlike acct-ghost-999, {NOVA} paid {OPS}.", ledger(), [[NOVA, "transferred", OPS]],
                          given=["acct-ghost-999"])
    assert given.passed and given.ids_checked == 3
    odd = gr.check_text("", ledger(), [[NOVA, "OWNS", OPS], [OPS, "FLOWS_TO"]])
    assert [r["reason"] for r in odd.rejected_claims] == ["unknown relation OWNS", "malformed claim"]


def test_mentioned_ids_skip_prose_that_looks_like_an_id():
    text = "acct-nova-ops paid merch-nova-pay after a team-based review; see dev-fp-8821."
    assert gr.mentioned_ids(text) == ["acct-nova-ops", "merch-nova-pay", "dev-fp-8821"]


def test_a_shared_device_route_with_forward_money_follows_the_rule():
    assert gr.route_breaks(ROUTE["path"]) == []


def test_a_shared_ip_or_money_flowing_back_is_only_association():
    via_ip = {"nodes": ["acct-payroll", "ip-185-220-101-7", VOLKOV, VORTEX],
              "steps": [step("acct-payroll", "ip-185-220-101-7", "LOGGED_FROM"),
                        step("ip-185-220-101-7", VOLKOV, "LOGGED_FROM", forward=False),
                        step(VOLKOV, VORTEX, "FLOWS_TO")]}
    assert gr.route_breaks(via_ip) == ["ip-185-220-101-7 is a shared IP, not a shared device"]
    paid_by_ops = {"nodes": ["acct-vendor-1", OPS], "steps": [step("acct-vendor-1", OPS, "FLOWS_TO", forward=False)]}
    assert gr.route_breaks(paid_by_ops) == [
        f"{OPS} paid acct-vendor-1, so that leg carries money toward acct-vendor-1, not away from it"]


def render(*results: dict) -> list[str]:
    facts = gr.extract_facts(list(results))
    state, verdict, _ = gr.derive(facts, unknown=False)
    return [s.text for s in gr.render(NOVA, state, verdict, facts, ledger())]


def test_the_card_states_the_scope_behind_clear_and_insufficient():
    [clear] = render(explore(6, search=scope(6, reached=193)))
    assert clear == ("No eligible route from NovaPay Digital Goods (merch-nova-pay) to a sanctioned entity was found "
                     "within 6 hops (193 entities reached). That is not proof the account is safe.")
    [shallow] = render(explore(4))
    assert "covered 4 of the 6 hops required to clear an account because it was only asked to search 4 hops" in shallow
    [capped] = render(explore(6, search=scope(6, completed=3, truncated=True)))
    assert "covered 3 of the 6 hops required to clear an account because it hit the node budget" in capped


def test_every_statement_cites_facts_and_unknown_entities_say_nothing_else():
    facts = run_facts()
    state, verdict, basis = gr.derive(facts, unknown=False)
    statements = gr.render(NOVA, state, verdict, facts, ledger())
    assert (state, verdict, basis) == (AnswerState.ANSWERED_WITH_PATH, Verdict.SUSPICIOUS, ["F3"])
    assert all(s.facts and set(s.facts) <= {f.id for f in facts} for s in statements)
    unknown = gr.extract_facts([envelope("resolve_entity", {"entity_id": "acct-ghost-999", "found": False})])
    state, verdict, _ = gr.derive(unknown, unknown=True)
    assert [s.text for s in gr.render("acct-ghost-999", state, verdict, unknown, EvidenceLedger())] == [
        "acct-ghost-999 is not in the graph, so nothing else was queried."]


def remembered(predicate: str, status: str = "CURRENT", **fields) -> dict:
    base = {"id": f"fact-{predicate}", "kind": "sanctions_exposure", "subject": NOVA, "predicate": predicate,
            "object": VORTEX, "value": 5, "unit": "hops", "scope": {"path": ROUTE["path"],
                                                                     "identity_hops": ROUTE["identity_hops"]},
            "depends_on": ROUTE["path"]["nodes"], "aggregate": False, "valid_rev": 3, "episode_id": "ep-1",
            "claim": "", "rerun": {"tool": "explore_neighborhood", "args": {"entity_id": NOVA, "depth": 6}},
            "status": status, "why_stale": None, "changed": []}
    return {**base, **fields}


def recalled(*facts: dict) -> dict:
    return envelope("recall_memory", {"entity_id": NOVA, "facts": list(facts), "approvals": []})


def test_a_remembered_route_answers_from_memory_and_renders_like_a_fresh_one():
    facts = gr.extract_facts([recalled(remembered("eligible_route_hops"))], rev=4)
    state, verdict, basis = gr.derive(facts, unknown=False)
    texts = [s.text for s in gr.render(NOVA, state, verdict, facts, ledger())]
    assert (state, verdict, basis) == (AnswerState.ANSWERED_FROM_MEMORY, Verdict.SUSPICIOUS, ["F1"])
    assert facts[0].evidence_rev == 3 and facts[0].source.tool == "recall_memory"
    assert texts[0] == ("From memory: 1 finding recorded by ep-1 still holds, because nothing it depends on has "
                        "changed since graph revision 3.")
    assert texts[1].startswith("NovaPay Digital Goods (merch-nova-pay) has an eligible route to sanctioned")


def test_stale_memory_says_what_changed_and_fresh_facts_decide():
    why = "acct-volkov-clearing changed after graph revision 3"
    facts = gr.extract_facts([recalled(remembered("eligible_route_hops", "STALE", why_stale=why)),
                              explore(6, (ROUTE,))])
    state, verdict, basis = gr.derive(facts, unknown=False)
    texts = [s.text for s in gr.render(NOVA, state, verdict, facts, ledger())]
    assert (state, verdict, basis) == (AnswerState.MEMORY_STALE_REINVESTIGATED, Verdict.SUSPICIOUS, ["F2"])
    assert texts[0] == f"The remembered route went stale ({why}), so it could not be reused."
