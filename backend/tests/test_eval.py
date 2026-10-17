"""The evaluator's scoring, the lookup baseline's verdict rule and the eval endpoint, without a model or a graph."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.eval import lookup, run_eval


def page(entity: str, labels: list[str], rows: list[tuple], direction: str = "out", number: int = 0,
         total: int | None = None) -> dict:
    return {"ok": True, "error": None, "args": {"entity_id": entity, "direction": direction, "page": number},
            "data": {"entity": {"id": entity, "labels": labels}, "direction": direction, "page": number,
                     "total_rows": len(rows) if total is None else total,
                     "rows": [{"id": i, "labels": ls, "rel": rel, "direction": d} for i, ls, rel, d in rows]}}


def outcome(verdict: str, target: str | None = None, hops: int | None = None, qualified: bool = False) -> dict:
    return {"verdict": verdict, "target": target, "hops": hops, "qualified": qualified,
            "answer_state": "answered_with_path"}


def test_an_incomplete_run_is_correct_if_insufficient_or_if_it_established_the_subjects_own_route():
    starved = next(i for i in run_eval.items() if i["case_id"] == "i01")
    truth = starved["also_correct"]
    assert truth["verdict"] == "SUSPICIOUS"
    assert run_eval.score(starved, outcome("INSUFFICIENT"))["correct"]
    assert run_eval.score(starved, outcome("SUSPICIOUS", truth["target"], truth["hops"]))["correct"]
    assert not run_eval.score(starved, outcome("SUSPICIOUS", truth["target"], truth["hops"] + 1))["correct"]
    assert not run_eval.score(starved, outcome("CLEAR", qualified=True))["correct"]
    capped = next(i for i in run_eval.items() if i["case_id"] == "i02")
    assert "also_correct" not in capped
    assert not run_eval.score(capped, outcome("CLEAR", qualified=True))["correct"]


def test_the_lookup_baseline_follows_money_out_and_shared_devices_but_never_ips():
    subject = "acct-a"
    seen = lookup.Lookups(subject)
    seen.add(page(subject, ["Account"], [("dev-1", ["Device"], "USED_DEVICE", "out"),
                                         ("ip-1", ["IP"], "LOGGED_FROM", "out"),
                                         ("acct-payer", ["Account"], "FLOWS_TO", "in")]))
    assert seen.successors[subject] == {"dev-1"}
    assert lookup.derive(seen)["verdict"] == "INSUFFICIENT", "the device's other users are unread"
    seen.add(page("dev-1", ["Device"], [("acct-b", ["Account"], "USED_DEVICE", "in")], direction="in"))
    seen.add(page("acct-b", ["Account"], [("acct-x", ["Account", "Sanctioned"], "FLOWS_TO", "out")]))
    found = lookup.derive(seen)
    assert found["verdict"] == "SUSPICIOUS"
    assert found["route"] == [subject, "dev-1", "acct-b", "acct-x"] and found["hops"] == 3


def test_the_lookup_baseline_clears_only_once_every_page_within_depth_is_read():
    subject = "acct-a"
    seen = lookup.Lookups(subject)
    payees = [(f"acct-{n}", ["Account"], "FLOWS_TO", "out") for n in range(lookup.PAGE_SIZE + 5)]
    seen.add(page(subject, ["Account"], payees[:lookup.PAGE_SIZE], total=len(payees)))
    assert seen.next_page(subject) == 1
    seen.add(page(subject, ["Account"], payees[lookup.PAGE_SIZE:], number=1, total=len(payees)))
    assert seen.next_page(subject) is None
    assert lookup.derive(seen, depth=1)["verdict"] == "CLEAR", "nothing past the prescribed depth needs reading"
    for payee, *_ in payees:
        assert lookup.derive(seen)["verdict"] == "INSUFFICIENT"
        seen.add(page(payee, ["Account"], []))
    assert lookup.derive(seen)["verdict"] == "CLEAR"

    ghost = lookup.Lookups("acct-ghost")
    ghost.add({"ok": False, "error": "acct-ghost is not in the graph", "data": None,
               "args": {"entity_id": "acct-ghost", "direction": "out", "page": 0}})
    assert (lookup.derive(ghost)["verdict"], lookup.derive(ghost)["answer_state"]) == ("INSUFFICIENT", "unknown_entity")


def test_the_eval_endpoint_serves_summaries_without_the_per_run_records(tmp_path, monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(main, "EVAL_RESULTS", tmp_path / "results.json")
    assert client.get("/api/eval").status_code == 404
    (tmp_path / "results.json").write_text(json.dumps({
        "updated_at": "2026-10-17T00:00:00Z",
        "controlled": {"llm": {"summary": {"conditions": {}}, "stages": [{"runs": 38}], "runs": [{"case_id": "c01"}]}},
        "retrieval": {"all": {"summary": {"strategies": {}}, "instances": [{}], "seeds": {"cycle-001": "acct-1"}}},
        "contention": {"summary": {"runs": 5}, "runs": [{}]},
    }))
    body = client.get("/api/eval").json()
    assert body["controlled"]["llm"] == {"summary": {"conditions": {}}, "stages": [{"runs": 38}]}
    assert body["retrieval"]["all"] == {"summary": {"strategies": {}}}
    assert body["contention"] == {"summary": {"runs": 5}}
