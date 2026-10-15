"""The claim checker, the route rule and the memory answer states, without a database."""

from __future__ import annotations

from backend.app import guardrails as gr
from backend.app.guardrails import AnswerState, EvidenceLedger


def ledger() -> EvidenceLedger:
    ledger = EvidenceLedger()
    ledger.add({
        "nodes": {
            "merch-nova-pay": {"labels": ["Account", "Merchant"], "name": "NovaPay Digital Goods"},
            "acct-nova-ops": {"labels": ["Account"], "name": "NovaPay Ops"},
            "acct-vortex-sdn": {"labels": ["Account", "Sanctioned"], "name": "Vortex Lazarus Syndicate"},
            "emp-rohan-mehta": {"labels": ["Employee"], "name": "Rohan Mehta"},
            "dec-waiver-77": {"labels": ["Decision"], "name": None},
        },
        "edges": [["merch-nova-pay", "FLOWS_TO", "acct-nova-ops"], ["emp-rohan-mehta", "APPROVED", "dec-waiver-77"]],
    })
    return ledger


def step(src: str, dst: str, rel: str, forward: bool = True) -> dict:
    return {"from": src, "to": dst, "rel": rel, "forward": forward}


def test_mentioned_ids_skip_prose_that_looks_like_an_id():
    text = "acct-nova-ops paid merch-nova-pay after a team-based review; see dev-fp-8821."
    assert gr.mentioned_ids(text) == ["acct-nova-ops", "merch-nova-pay", "dev-fp-8821"]


def test_an_invented_edge_between_real_entities_is_rejected():
    claim = {"subject": "merch-nova-pay", "relation": "TRANSFERRED", "object": "acct-vortex-sdn"}
    result = gr.verify("merch-nova-pay paid acct-vortex-sdn directly.", [claim], ledger())
    assert not result.verified
    assert result.unknown_ids == []
    assert result.rejected_claims == [{"claim": claim, "reason": "no such edge in this run's tool results"}]


def test_an_id_no_tool_returned_is_caught():
    result = gr.verify("Funds also reached acct-made-up-77.", [], ledger())
    assert not result.verified
    assert result.unknown_ids == ["acct-made-up-77"]
    assert result.badge == "Rewritten from graph results (0 unsupported claims, 1 unknown ID in the model's answer)"


def test_transfers_match_flow_summaries_and_the_alert_id_counts_as_given():
    claims = [["merch-nova-pay", "transferred", "acct-nova-ops"],
              {"subject": "emp-rohan-mehta", "relation": "APPROVED", "object": "dec-waiver-77"}]
    result = gr.verify("Unlike acct-ghost-999, merch-nova-pay paid acct-nova-ops.", claims, ledger(),
                       given=["acct-ghost-999"])
    assert result.verified
    assert (result.claims_checked, result.ids_checked) == (2, 5)
    assert result.badge == "Verified against graph (2 claims, 5 entity IDs checked)"


def test_unknown_relations_and_malformed_claims_are_rejected():
    result = gr.verify("", [["merch-nova-pay", "OWNS", "acct-nova-ops"], ["acct-nova-ops", "FLOWS_TO"]], ledger())
    assert [r["reason"] for r in result.rejected_claims] == ["unknown relation OWNS", "malformed claim"]


def test_a_shared_device_route_with_forward_money_is_exposure():
    path = {"nodes": ["merch-nova-pay", "acct-nova-ops", "dev-fp-8821", "acct-mule-volkov", "acct-vortex-sdn"],
            "steps": [step("merch-nova-pay", "acct-nova-ops", "FLOWS_TO"),
                      step("acct-nova-ops", "dev-fp-8821", "USED_DEVICE"),
                      step("dev-fp-8821", "acct-mule-volkov", "USED_DEVICE", forward=False),
                      step("acct-mule-volkov", "acct-vortex-sdn", "FLOWS_TO")]}
    assert gr.route_breaks(path) == []


def test_a_shared_ip_or_money_flowing_back_is_only_association():
    via_ip = {"nodes": ["acct-vip", "ip-185-220-101-7", "acct-mule-volkov", "acct-vortex-sdn"],
              "steps": [step("acct-vip", "ip-185-220-101-7", "LOGGED_FROM"),
                        step("ip-185-220-101-7", "acct-mule-volkov", "LOGGED_FROM", forward=False),
                        step("acct-mule-volkov", "acct-vortex-sdn", "FLOWS_TO")]}
    assert gr.route_breaks(via_ip) == ["ip-185-220-101-7 is a shared IP, not a shared device"]
    paid_by_ops = {"nodes": ["acct-vendor-1", "acct-nova-ops"],
                   "steps": [step("acct-vendor-1", "acct-nova-ops", "FLOWS_TO", forward=False)]}
    assert gr.route_breaks(paid_by_ops) == [
        "acct-nova-ops paid acct-vendor-1, so that leg carries money toward acct-vendor-1, not away from it"]


def test_unknown_entity_summary_mentions_nothing_else():
    summary, claims = gr.summarize("acct-ghost-999", [], AnswerState.UNKNOWN_ENTITY, EvidenceLedger())
    assert (summary, claims) == ("acct-ghost-999 is not in the graph, so nothing else was queried.", [])


def test_memory_summaries_cite_the_fact_and_what_changed():
    fact = {"id": "fact-1", "claim": "merch-nova-pay reaches acct-vortex-sdn through dev-fp-8821",
            "verdict": "SUSPICIOUS", "episode_id": "ep-1", "evidence_ids": ["dev-fp-8821"], "changed_evidence": []}
    recall = {"tool": "recall_memory", "ok": True, "data": {"facts": [{**fact, "status": "CURRENT"}]}}
    summary, _ = gr.summarize("merch-nova-pay", [recall], AnswerState.ANSWERED_FROM_MEMORY, EvidenceLedger())
    assert summary.startswith("Answered from memory: merch-nova-pay reaches acct-vortex-sdn")
    assert "(dev-fp-8821) has changed since" in summary

    stale = {"tool": "recall_memory", "ok": True,
             "data": {"facts": [{**fact, "status": "STALE", "changed_evidence": ["acct-mule-volkov"]}]}}
    summary, _ = gr.summarize("merch-nova-pay", [stale], AnswerState.MEMORY_STALE_REINVESTIGATED, EvidenceLedger())
    assert summary.startswith("Memory held an earlier SUSPICIOUS finding, but acct-mule-volkov changed")
