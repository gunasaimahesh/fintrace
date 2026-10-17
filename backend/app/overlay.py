"""The challenge overlay: 16 alert cases planted outside NovaPay's 6-hop neighborhood.

Each case is a few planted accounts (with their devices and IPs) and one PENDING alert on its subject. The
graph never says how a case should come out: expected outcomes live only in backend/eval/cases.json, which
agent code must not read.

Edges to existing data touch only anchors, IBM accounts at least 7 undirected hops from NovaPay, so NovaPay's
1..6-hop neighborhood is unchanged. Every anchor is at least ANCHOR_MARGIN hops, directed and undirected, from
every planted sanctioned account; a detour through IBM data needs at least one hop between two anchors, so it
cannot bring a sanctioned account within 6 hops of a subject. `_settle` enforces this and derives each case's
route and distances from the planted edges alone.

Usage (from the repo root):
    python -m backend.app.overlay --graph fintrace:overlay-copy --write-cases
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from falkordb import Graph

from .config import ROOT, get_settings
from .contracts import MAX_CAPACITY, PRESCRIBED_DEPTH, AlertStatus
from .db import get_db, graph_key
from .queries import RISK_RELS, dataset_meta, internal_id
from .scenario import ACCOUNT, DAY, HOUR, MINUTE, NOVAPAY, GraphSpec
from .seed_data import DEVICE_OS, WRITE_TIMEOUT_MS, load_spec

OVERLAY_VERSION = "challenge-1"
SEED = 2026
ANCHOR_COUNT = 80
MIN_ANCHOR_DISTANCE = 7
ANCHOR_MARGIN = 5
TX_PREFIX = "ov1-"
CASES_PATH = ROOT / "backend" / "eval" / "cases.json"
RULES = ("large_outbound_wire", "new_counterparty_burst", "velocity_spike", "dormant_reactivation",
         "cross_border_wire", "merchant_volume_jump")
REVEALING = ("mule", "shell", "sdn", "launder", "ring", "fraud", "risk", "sanction", "suspicious", "clean",
             "positive", "negative", "control", "expected", "heldout", "test", "case", "layer", "exit",
             "nova", "vortex", "volkov", "baltic", "orion", "8821")
DEFINITIONS = {
    "eligible_route": ("FLOWS_TO edges in their own direction, plus Account -USED_DEVICE-> Device <-USED_DEVICE- "
                       "Account (two hops). LOGGED_FROM never counts."),
    "verdict": "SUSPICIOUS iff an eligible route reaches a :Sanctioned account within prescribed_depth hops.",
    "eligible_hops": "Length of the shortest eligible route to sanctioned_target at any depth; null if there is none.",
    "shortest_undirected_hops_to_sanctioned": (
        "Hops over FLOWS_TO (either direction), USED_DEVICE and LOGGED_FROM from the subject to sanctioned_target, "
        "or to associated_sanctioned for an association; null when the case plants no sanctioned account. No other "
        "sanctioned account is within prescribed_depth undirected hops of any subject."),
    "associated_sanctioned": "A sanctioned account reachable only over an undirected path; the case is CLEAR.",
    "route": "One shortest eligible route, subject first.",
}

_PLACES = ("Halden", "Corvel", "Brantham", "Kestrel", "Marlow", "Tavira", "Vesterby", "Arnholt", "Caldera",
           "Norrland", "Selby", "Aurex", "Quillon", "Belmont", "Fenwick", "Ivers", "Juniper", "Kalmar", "Lowther",
           "Meridian", "Northgate", "Pemberton", "Ravel", "Solent", "Thorne", "Varden", "Westmark", "Yardley",
           "Ardent", "Calloway", "Dunmore", "Falkner", "Garrow", "Hallam", "Jarrow", "Keswick", "Larkin", "Maddox",
           "Nesbit", "Oakley", "Prentice", "Rowan", "Stanmore", "Tolland", "Upton", "Whitlow", "Ashby", "Brenner",
           "Cortland", "Delmar", "Everly", "Foxley", "Grantham", "Holloway", "Kinross", "Lysander", "Alder",
           "Bexley", "Carrow", "Dalton", "Easton", "Farrell", "Gilmore", "Harlan", "Ingle", "Jessop", "Kerrigan",
           "Lindell", "Morrow", "Ormond", "Pryce", "Quarles", "Radley", "Sutton", "Tanner", "Ulric", "Vance",
           "Wexford", "Yates", "Zeller", "Ambler", "Barrow", "Colby", "Denholm", "Elstree", "Fairley", "Glenmore",
           "Hartwell", "Ilford", "Jardine", "Kemble", "Langford", "Medway", "Newbury", "Oldham", "Penrose",
           "Redwood", "Sheldon", "Templar", "Underhill", "Verity", "Waverly", "Bramley", "Chalmers", "Darby",
           "Elwood", "Forsyth", "Galloway", "Hendry", "Islay", "Kingsley", "Lomond", "Merrick", "Norwood", "Osgood",
           "Pickett", "Rutland", "Thursby", "Wardell", "Ackroyd", "Blythe", "Dorset", "Emery", "Granger", "Haywood")
_TRADES = ("Freight", "Logistics", "Trading", "Imports", "Textiles", "Metals", "Commodities", "Supply", "Marine",
           "Agro", "Foods", "Pharma", "Electronics", "Components", "Timber", "Minerals", "Energy", "Shipping",
           "Packaging", "Tools", "Optics", "Media", "Software", "Consulting", "Services", "Systems", "Wholesale",
           "Distribution", "Apparel", "Furniture", "Ceramics", "Paper", "Chemicals", "Coffee", "Seafood", "Grain",
           "Machinery", "Motors", "Aviation", "Analytics", "Ventures", "Partners", "Capital", "Holdings",
           "Properties", "Interiors", "Automotive", "Hardware", "Biotech", "Cosmetics", "Print")
_FORMS = ("Ltd", "GmbH", "LLC", "B.V.", "S.A.", "AG", "SRL", "Oy", "AB", "Kft", "Pte Ltd", "FZE", "Inc", "LLP",
          "Limited", "A/S", "SAS", "s.r.o.")
_STORES = ("Games", "Ticketing", "Digital", "Travel", "Electronics", "Marketplace", "Gift Cards", "Streaming",
           "Home Goods", "Software", "Mobile Top-Up", "Outfitters")
_FIRST_NAMES = ("Ilse", "Marta", "Tomas", "Elena", "Jonas", "Sofia", "Lukas", "Anika", "Pavel", "Irene", "Oskar",
                "Leena", "Mateo", "Hana", "Viktor", "Noemi", "Arjun", "Dalia", "Felix", "Greta")
_SURNAMES = ("Marquardt", "Kowalczyk", "Lindgren", "Haddad", "Brandt", "Castell", "Duarte", "Eklund", "Ferreira",
             "Gallo", "Horvath", "Jensen", "Kaur", "Larsen", "Moreau", "Nakamura", "Ortega", "Petrov", "Quinn",
             "Reyes", "Sorensen", "Tanaka", "Varga", "Weber", "Zielinski")
_COUNTRIES = ("Germany", "Netherlands", "Poland", "Portugal", "Malta", "Cyprus", "UAE", "Singapore", "Hong Kong",
              "Panama", "Estonia", "Lithuania", "Turkey", "Georgia", "Mauritius", "Ireland", "Luxembourg",
              "Switzerland", "Austria", "Hungary", "Spain", "Italy", "France", "UK", "Canada", "Mexico")
_FLAGGED_OS = {"headless_browser": "Headless Linux Chromium", "emulator": "Android 14 WebView"}
_FIRST_OCTETS = tuple(n for n in range(11, 224) if n not in (127, 169, 172, 185, 192))
HOME_BANK = "FinTrace Demo Bank"

_NEIGHBORS = f"""
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[:{RISK_RELS}]-(b)
RETURN collect(DISTINCT ID(b))
"""

_SUMMARIZE = """
UNWIND $pairs AS p
MATCH (a:Account {id: p[0]})-[t:TRANSFERRED]->(b:Account {id: p[1]})
WITH a, b, toInteger(sum(t.amount)) AS amount, count(t) AS transfers, min(t.ts) AS first_ts, max(t.ts) AS last_ts
WITH a, b, amount, transfers, first_ts, last_ts, toInteger(round(amount / 100.0)) AS dollars
MERGE (a)-[f:FLOWS_TO]->(b)
SET f.amount = amount, f.transfers = transfers, f.first_ts = first_ts, f.last_ts = last_ts,
    f.capacity = CASE WHEN dollars < 1 THEN 1 WHEN dollars > $max THEN $max ELSE dollars END,
    f.weight = 1
"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _revealing(text: str) -> bool:
    return any(word in text.lower() for word in REVEALING)


@dataclass
class _Case:
    case_id: str
    split: str
    category: str
    pattern: str
    rule: str
    priority: int
    related_case_id: str | None = None
    subject_id: str = ""
    verdict: str = ""
    notes: str = ""
    target: str | None = None
    associated: str | None = None
    hops: int | None = None
    undirected: int | None = None
    route: list[str] | None = None
    alert_at: int = 0
    planted_ids: list[str] = field(default_factory=list)
    anchor_ids: list[str] = field(default_factory=list)

    def record(self, alert_id: str) -> dict:
        return {
            "case_id": self.case_id, "alert_id": alert_id, "subject_id": self.subject_id, "split": self.split,
            "category": self.category, "pattern": self.pattern, "rule": self.rule, "priority": self.priority,
            "related_case_id": self.related_case_id,
            "expected": {
                "verdict": self.verdict, "sanctioned_target": self.target, "eligible_hops": self.hops,
                "shortest_undirected_hops_to_sanctioned": self.undirected,
                "associated_sanctioned": self.associated, "route": self.route, "notes": self.notes,
            },
            "planted_ids": self.planted_ids, "anchor_ids": self.anchor_ids,
        }


class _Overlay:
    """Plants one case at a time. Transfers get their tx_ids in time order once every case is planted."""

    def __init__(self, anchors: list[str], window: tuple[int, int], loaded_at: int, seed: int) -> None:
        self.rng = random.Random(seed)
        self.start, self.end = window
        self.loaded_at = loaded_at
        self.anchors = list(anchors)
        self.taken = 0
        self.spec = GraphSpec()
        self.payments: list[tuple[int, str, str, int, str]] = []
        self.cases: list[_Case] = []
        self.used: set[str] = set()
        self.planted: set[str] = set()
        # Each place name and surname is used once, so a shared word never hints that two accounts are related.
        self.places = self.rng.sample(_PLACES, len(_PLACES))
        self.surnames = self.rng.sample(_SURNAMES, len(_SURNAMES))

    @property
    def case(self) -> _Case:
        return self.cases[-1]

    def begin(self, pattern: str, split: str, category: str, rule: str, priority: int,
              related_case_id: str | None = None) -> _Case:
        if rule not in RULES:
            raise ValueError(f"{rule} is not one of the neutral alert rules")
        self.cases.append(_Case(f"c{len(self.cases) + 1:02d}", split, category, pattern, rule, priority,
                                related_case_id))
        return self.case

    def expect(self, subject: str, verdict: str, notes: str, target: str | None = None, hops: int | None = None,
               associated: str | None = None) -> None:
        """`hops` is the designed eligible route length; `_settle` checks the planted edges agree."""
        c = self.case
        c.subject_id, c.verdict, c.notes = subject, verdict, notes
        c.target, c.hops, c.associated = target, hops, associated

    def alert(self, at: int) -> None:
        self.case.alert_at = min(at, self.end)

    # Time and money

    def minute(self, lo: int, hi: int) -> int:
        return self.rng.randrange(lo, hi) // MINUTE * MINUTE

    def pivot(self, room: int = 4 * DAY) -> int:
        """When a case's route starts: after six days of history, with `room` left for its legs."""
        return self.minute(self.start + 6 * DAY, self.end - room)

    def usd(self, lo: int, hi: int, cents: bool = True) -> int:
        return self.rng.randint(lo * 100, hi * 100) if cents else self.rng.randint(lo, hi) * 100

    def share(self, amount: int, lo: float, hi: float) -> int:
        return int(amount * self.rng.uniform(lo, hi)) // 100 * 100

    def pay(self, src: str, dst: str, amount: int, ts: int, channel: str) -> None:
        if not self.start <= ts <= self.end:
            raise ValueError(f"{src} -> {dst} at {ts} is outside the data window")
        self.payments.append((ts, src, dst, amount, channel))

    def take(self, count: int) -> list[str]:
        if self.taken + count > len(self.anchors):
            raise ValueError(f"the overlay needs more than {len(self.anchors)} anchors")
        chosen = self.anchors[self.taken:self.taken + count]
        self.taken += count
        self.case.anchor_ids.extend(chosen)
        return chosen

    def _with_anchors(self, account: str, count: int, lo: int, hi: int, paying: bool, since: int | None,
                      until: int | None, channel: str, per: tuple[int, int]) -> None:
        for anchor in self.take(count):
            src, dst = (anchor, account) if paying else (account, anchor)
            for _ in range(self.rng.randint(*per)):
                self.pay(src, dst, self.usd(lo, hi), self.minute(since or self.start, until or self.end), channel)

    def inflows(self, account: str, count: int, lo: int, hi: int, *, since: int | None = None,
                until: int | None = None, channel: str = "ACH", per: tuple[int, int] = (1, 2)) -> None:
        """`count` IBM anchors pay `account` between `lo` and `hi` dollars, `per` times each."""
        self._with_anchors(account, count, lo, hi, True, since, until, channel, per)

    def outflows(self, account: str, count: int, lo: int, hi: int, *, since: int | None = None,
                 until: int | None = None, channel: str = "ACH", per: tuple[int, int] = (1, 2)) -> None:
        self._with_anchors(account, count, lo, hi, False, since, until, channel, per)

    def uses(self, account: str, device: str, first_seen: int, last_seen: int) -> None:
        self.spec.identity("USED_DEVICE", account, device, first_seen, min(last_seen, self.end))

    def logs(self, account: str, ip: str, first_seen: int, last_seen: int) -> None:
        self.spec.identity("LOGGED_FROM", account, ip, first_seen, min(last_seen, self.end))

    # Nodes

    def _unique(self, make: Callable[[], str]) -> str:
        while True:
            value = make()
            if value not in self.used and not _revealing(value):
                self.used.add(value)
                return value

    def _plant(self, labels: str, node_id: str, **props) -> str:
        if node_id in self.planted:
            raise ValueError(f"{node_id} is planted twice")
        self.planted.add(node_id)
        self.case.planted_ids.append(node_id)
        self.spec.node(labels, id=node_id, **props, topology_updated_at=self.loaded_at)
        return node_id

    def _account(self, labels: str, node_id: str, name: str, kind: str, bank: str, volume: int,
                 tier: str = "standard", **props) -> str:
        return self._plant(labels, node_id, name=name, bank=bank, kind=kind, tier=tier, monthly_volume=volume,
                           status="ACTIVE", source="planted", **props)

    def bank(self) -> str:
        if self.rng.random() < 0.2:
            return HOME_BANK
        return f"{self.rng.choice(_COUNTRIES)} Bank #{self.rng.randint(1, 250)}"

    def _place(self) -> str:
        if not self.places:
            raise ValueError("the overlay ran out of place names")
        return self.places.pop()

    def corp(self, listed: str | None = None) -> str:
        """A company account; `listed` makes it :Sanctioned on that list."""
        rng = self.rng
        stem = f"{self._place()} {rng.choice(_TRADES)}"
        name = f"{stem} {rng.choice(_FORMS)}"
        if rng.random() < 0.6:
            node_id = f"acct-{_slug(stem)}"
        else:
            node_id = self._unique(lambda: f"acct-{rng.randint(4_000_000, 9_999_999)}")
        volume = 0 if listed and rng.random() < 0.3 else rng.randrange(1_500_000, 30_000_000, 10_000)
        labels, extra = (f"{ACCOUNT}:Sanctioned", {"list": listed}) if listed else (ACCOUNT, {})
        return self._account(labels, node_id, name, "Corporation", self.bank(), volume, **extra)

    def person(self, volume: int | None = None, **props) -> str:
        rng = self.rng
        if not self.surnames:
            raise ValueError("the overlay ran out of surnames")
        name = f"{rng.choice(_FIRST_NAMES)} {self.surnames.pop()}"
        node_id = self._unique(lambda: f"acct-ind-{rng.randint(300_000, 999_999)}")
        volume = rng.randrange(250_000, 1_500_000, 5_000) if volume is None else volume
        return self._account(ACCOUNT, node_id, name, "Individual", self.bank(), volume, **props)

    def brand(self) -> str:
        return f"{self._place()} {self.rng.choice(_STORES)}"

    def merchant(self, brand: str, mcc: str, onboarded_at: int) -> str:
        return self._account(f"{ACCOUNT}:Merchant", f"merch-{_slug(brand)}", brand, "Corporation", HOME_BANK,
                             self.rng.randrange(12_000_000, 60_000_000, 100_000), tier="enterprise", mcc=mcc,
                             onboarded_at=onboarded_at)

    def treasury(self, brand: str) -> str:
        role = self.rng.choice(("Treasury", "Settlements", "Payouts"))
        return self._account(ACCOUNT, f"acct-{_slug(brand)}-{role.lower()}", f"{brand} {role}", "Corporation",
                             HOME_BANK, self.rng.randrange(6_000_000, 25_000_000, 50_000))

    def device(self, flags: list[str] | None = None) -> str:
        device_id = self._unique(lambda: f"dev-{self.rng.getrandbits(40):010x}")
        os = _FLAGGED_OS[flags[0]] if flags else self.rng.choice(DEVICE_OS)
        return self._plant("Entity:RiskNode:Device", device_id, os=os, **({"risk_flags": flags} if flags else {}))

    def ip(self, kind: str) -> str:
        rng = self.rng
        address = self._unique(lambda: f"{rng.choice(_FIRST_OCTETS)}.{rng.randint(0, 255)}."
                                        f"{rng.randint(0, 255)}.{rng.randint(1, 254)}")
        return self._plant("Entity:RiskNode:IP", "ip-" + address.replace(".", "-"), address=address, kind=kind)

    # Positives

    def layering_five(self) -> tuple[_Case, str]:
        case = self.begin("device_bridge_layering", "heldout", "positive", "merchant_volume_jump", 5)
        t = self.pivot()
        brand = self.brand()
        merchant = self.merchant(brand, "5816", onboarded_at=t - 42 * DAY)
        treasury = self.treasury(brand)
        device = self.device(["emulator"])
        far, side, payer = self.corp(), self.corp(), self.corp()
        target = self.corp(listed="EU-CFSP")
        self.inflows(merchant, 4, 25, 850, until=t - 2 * HOUR, channel="Credit Card", per=(1, 3))
        self.outflows(merchant, 2, 2_000, 30_000)
        self.outflows(treasury, 1, 1_000, 12_000, until=t)
        sweep = self.usd(95_000, 160_000, cents=False)
        self.pay(merchant, treasury, sweep, t, "Wire")
        self.uses(treasury, device, t - 6 * DAY, t + 80 * MINUTE)
        self.uses(far, device, t - 3 * DAY, t + 7 * HOUR)
        self.pay(far, side, self.share(sweep, 0.40, 0.48), t + 3 * HOUR, "Wire")
        self.pay(far, payer, self.share(sweep, 0.48, 0.55), t + 5 * HOUR, "Wire")
        self.pay(side, payer, self.share(sweep, 0.38, 0.45), t + 7 * HOUR, "Wire")
        self.pay(payer, target, self.share(sweep, 0.88, 0.93), t + 11 * HOUR, "Wire")
        self.expect(merchant, "SUSPICIOUS", target=target, hops=5, notes=(
            "Card revenue is swept to the merchant's treasury account. The treasury's flagged device is also used by "
            "an account that wires the funds, directly and through a second account, to the account that pays the "
            "sanctioned target."))
        self.alert(t + 13 * HOUR)
        return case, far

    def layering_six(self) -> None:
        self.begin("device_bridge_layering", "heldout", "positive", "new_counterparty_burst", 3)
        t = self.pivot()
        subject = self.corp()
        first, second, pool = self.corp(), self.corp(), self.corp()
        device = self.device()
        far, payer = self.corp(), self.corp()
        target = self.corp(listed="OFAC-SDN")
        self.inflows(subject, 2, 8_000, 40_000, until=t - DAY)
        self.outflows(subject, 2, 1_500, 9_000)
        a, b = self.usd(60_000, 110_000, cents=False), self.usd(30_000, 55_000, cents=False)
        self.pay(subject, first, a, t, "Wire")
        self.pay(subject, second, b, t + 40 * MINUTE, "Wire")
        self.pay(first, pool, self.share(a, 0.95, 0.98), t + DAY, "Wire")
        self.pay(second, pool, self.share(b, 0.94, 0.97), t + DAY + 2 * HOUR, "ACH")
        self.uses(pool, device, t - 10 * DAY, t + DAY + 3 * HOUR)
        self.uses(far, device, t - 2 * DAY, t + 2 * DAY)
        out = self.share(a + b, 0.88, 0.92)
        self.pay(far, payer, out, t + DAY + 9 * HOUR, "Wire")
        self.pay(payer, target, self.share(out, 0.95, 0.98), t + DAY + 15 * HOUR, "Wire")
        self.expect(subject, "SUSPICIOUS", target=target, hops=6, notes=(
            "Two payments from the subject to new counterparties are pooled by one account, whose unflagged device "
            "is shared with the account that pays the sanctioned target's payer. The device bridge is three hops in."))
        self.alert(t + DAY + 18 * HOUR)

    def direct_chain(self) -> None:
        self.begin("direct_chain", "dev", "positive", "large_outbound_wire", 4)
        t = self.pivot()
        client, subject, supplier = self.corp(), self.corp(), self.corp()
        first, second = self.corp(), self.corp()
        target = self.corp(listed="UK-HMT")
        self.inflows(client, 2, 10_000, 60_000, until=t - 2 * DAY)
        for _ in range(3):
            self.pay(client, subject, self.usd(8_000, 30_000), self.minute(self.start, t - DAY), "ACH")
        for _ in range(2):
            self.pay(subject, supplier, self.usd(2_000, 9_000), self.minute(self.start, t), "ACH")
        self.outflows(supplier, 2, 1_000, 6_000)
        amount = self.usd(180_000, 260_000, cents=False)
        self.pay(subject, first, amount, t, "Wire")
        self.pay(first, second, self.share(amount, 0.97, 0.99), t + 4 * HOUR, "Wire")
        self.pay(second, target, self.share(amount, 0.94, 0.97), t + 20 * HOUR, "Wire")
        self.expect(subject, "SUSPICIOUS", target=target, hops=3, notes=(
            "A large wire from the subject passes through two accounts within a day and reaches the sanctioned "
            "target."))
        self.alert(t + 22 * HOUR)

    def double_bridge(self) -> None:
        self.begin("double_device_bridge", "heldout", "positive", "dormant_reactivation", 2)
        t = self.pivot()
        subject = self.corp()
        first_device = self.device(["headless_browser"])
        operator = self.person()
        second_device = self.device()
        payer = self.corp()
        target = self.corp(listed="UN-1267")
        self.inflows(subject, 2, 20_000, 70_000, since=t - 2 * DAY, until=t - 2 * HOUR, channel="Wire",
                     per=(1, 1))
        self.outflows(subject, 1, 3_000, 8_000, since=t - HOUR, until=t + 3 * HOUR, per=(1, 1))
        self.uses(subject, first_device, t - 30 * DAY, t + 2 * HOUR)
        self.uses(operator, first_device, t - 5 * DAY, t + 6 * HOUR)
        self.uses(operator, second_device, t - 4 * DAY, t + 9 * HOUR)
        self.uses(payer, second_device, t - DAY, t + 14 * HOUR)
        self.pay(payer, target, self.usd(70_000, 120_000, cents=False), t + 13 * HOUR, "Wire")
        self.expect(subject, "SUSPICIOUS", target=target, hops=5, notes=(
            "A dormant account that suddenly receives wires shares a flagged device with an individual, whose "
            "second device is used by the account that pays the sanctioned target. Two device bridges and no money "
            "between them."))
        self.alert(t + 15 * HOUR)

    def ip_shortcut(self) -> None:
        self.begin("ip_shortcut_with_device_route", "heldout", "positive", "cross_border_wire", 3)
        t = self.pivot()
        client, subject, supplier = self.corp(), self.corp(), self.corp()
        proxy = self.ip("corporate_proxy")
        neighbor = self.corp()
        agent = self.corp()
        device = self.device()
        far, middle, payer = self.corp(), self.corp(), self.corp()
        target = self.corp(listed="OFAC-SDN")
        self.inflows(client, 2, 10_000, 50_000, until=t - 2 * DAY)
        for _ in range(2):
            self.pay(client, subject, self.usd(15_000, 45_000), self.minute(self.start, t - DAY), "ACH")
        self.pay(subject, supplier, self.usd(3_000, 12_000), self.minute(self.start, t), "ACH")
        self.outflows(supplier, 1, 1_000, 5_000)
        self.logs(subject, proxy, self.start - 60 * DAY, self.end - DAY)
        self.logs(neighbor, proxy, t - 12 * DAY, t + 2 * DAY)
        self.pay(neighbor, target, self.usd(20_000, 45_000, cents=False), t - 2 * DAY, "Wire")
        amount = self.usd(120_000, 200_000, cents=False)
        self.pay(subject, agent, amount, t, "Wire")
        self.uses(agent, device, t - 8 * DAY, t + 2 * HOUR)
        self.uses(far, device, t - 3 * DAY, t + DAY)
        self.pay(far, middle, self.share(amount, 0.90, 0.95), t + 5 * HOUR, "Wire")
        self.pay(middle, payer, self.share(amount, 0.87, 0.89), t + 9 * HOUR, "Wire")
        self.pay(payer, target, self.share(amount, 0.84, 0.86), t + 20 * HOUR, "Wire")
        self.expect(subject, "SUSPICIOUS", target=target, hops=6, notes=(
            "The subject logs in through the same corporate proxy as an account that pays the sanctioned target "
            "(3 undirected hops, not eligible). The eligible route is 6 hops: a wire to the subject's agent, whose "
            "device is shared with the account that starts a three-wire chain to the target."))
        self.alert(t + 22 * HOUR)

    def fan_out(self) -> None:
        self.begin("fan_out_aggregator", "dev", "positive", "velocity_spike", 1)
        t = self.pivot()
        client, subject, supplier = self.corp(), self.corp(), self.corp()
        payees = [self.person() for _ in range(4)]
        collector = self.corp()
        target = self.corp(listed="EU-CFSP")
        self.inflows(client, 2, 10_000, 40_000, until=t - 2 * DAY)
        self.pay(client, subject, self.usd(40_000, 60_000, cents=False), t - DAY, "Wire")
        self.pay(subject, supplier, self.usd(2_000, 8_000), self.minute(self.start, t - 2 * DAY), "ACH")
        self.outflows(supplier, 1, 1_000, 5_000)
        total = 0
        for i, payee in enumerate(payees):
            amount = self.usd(9_200, 9_950)
            self.pay(subject, payee, amount, t + i * 17 * MINUTE, "ACH")
            forwarded = amount - self.usd(40, 180)
            self.pay(payee, collector, forwarded, t + 6 * HOUR + i * 41 * MINUTE, "Wire")
            total += forwarded
        self.pay(collector, target, self.share(total, 0.96, 0.98), t + 30 * HOUR, "Wire")
        self.expect(subject, "SUSPICIOUS", target=target, hops=3, notes=(
            "Four payouts just under $10,000 to individuals within an hour. Each forwards to one collector account, "
            "which wires the total to the sanctioned target."))
        self.alert(t + 31 * HOUR)

    def second_alert(self, first: _Case, far: str) -> None:
        self.begin("shared_route_second_alert", "dev", "coordination", "large_outbound_wire", first.priority,
                   related_case_id=first.case_id)
        self.expect(far, "SUSPICIOUS", target=first.target, hops=2, notes=(
            f"A second alert on the account just past {first.case_id}'s device. It reaches the same sanctioned target, "
            f"so a coordinator can merge it into {first.case_id}'s investigation. It plants nothing of its own."))
        self.alert(first.alert_at + 7 * MINUTE)

    # Negative controls

    def ip_only(self) -> None:
        self.begin("ip_only_association", "dev", "negative_control", "cross_border_wire", 4)
        t = self.pivot()
        client, subject, supplier = self.corp(), self.corp(), self.corp()
        office = self.ip("office")
        neighbor = self.corp()
        listed = self.corp(listed="OFAC-SDN")
        self.inflows(client, 2, 10_000, 50_000, until=t - 2 * DAY)
        for _ in range(2):
            self.pay(client, subject, self.usd(12_000, 40_000), self.minute(self.start, t - DAY), "ACH")
        self.pay(subject, supplier, self.usd(60_000, 140_000, cents=False), t, "Wire")
        self.outflows(supplier, 1, 2_000, 9_000)
        self.logs(subject, office, self.start - 45 * DAY, self.end - 2 * DAY)
        self.logs(neighbor, office, self.start - 20 * DAY, self.end - DAY)
        self.pay(neighbor, listed, self.usd(40_000, 90_000, cents=False), t + 3 * HOUR, "Wire")
        self.expect(subject, "CLEAR", associated=listed, notes=(
            "The subject shares an office IP with an account that pays a sanctioned account (3 undirected hops). "
            "IP links never make a route eligible, and the subject's own money goes elsewhere."))
        self.alert(t + 4 * HOUR)

    def reverse_direction(self) -> None:
        self.begin("reverse_direction_association", "dev", "negative_control", "velocity_spike", 2)
        t = self.pivot()
        client, subject, supplier = self.corp(), self.corp(), self.corp()
        partner_a, partner_b, relay = self.corp(), self.corp(), self.corp()
        listed = self.corp(listed="UK-HMT")
        self.inflows(client, 1, 10_000, 40_000, until=t - 3 * DAY)
        self.pay(client, subject, self.usd(10_000, 30_000), self.minute(self.start, t - 2 * DAY), "ACH")
        for _ in range(2):
            self.pay(subject, supplier, self.usd(2_000, 9_000), self.minute(self.start, t), "ACH")
        self.outflows(supplier, 2, 1_000, 6_000)
        self.pay(subject, partner_a, self.usd(4_000, 12_000), t - 4 * DAY, "ACH")
        amount = self.usd(80_000, 150_000, cents=False)
        a, b = self.share(amount, 0.50, 0.58), self.share(amount, 0.35, 0.42)
        self.pay(listed, relay, amount, t, "Wire")
        self.pay(relay, partner_a, a, t + 6 * HOUR, "Wire")
        self.pay(relay, partner_b, b, t + 8 * HOUR, "Wire")
        self.pay(partner_a, subject, self.share(a, 0.95, 0.98), t + DAY, "ACH")
        self.pay(partner_b, subject, self.share(b, 0.95, 0.98), t + DAY + 3 * HOUR, "ACH")
        self.expect(subject, "CLEAR", associated=listed, notes=(
            "A sanctioned account pays a relay that pays two of the subject's counterparties, who then pay the "
            "subject (3 undirected hops). Every transfer on that path points toward the subject, so no route from "
            "the subject is eligible."))
        self.alert(t + DAY + 5 * HOUR)

    def busy_merchant(self) -> None:
        self.begin("busy_legit_merchant", "dev", "negative_control", "merchant_volume_jump", 3)
        t = self.pivot()
        alert_at = self.minute(self.end - 10 * HOUR, self.end - 2 * HOUR)
        brand = self.brand()
        merchant = self.merchant(brand, "5999", onboarded_at=self.start - 400 * DAY)
        treasury = self.treasury(brand)
        device = self.device()
        office = self.ip("office")
        self.inflows(merchant, 10, 15, 600, until=alert_at, channel="Credit Card", per=(1, 3))
        self.outflows(merchant, 4, 2_000, 18_000, until=alert_at)
        for when in (t - 5 * DAY, t + DAY):
            self.pay(merchant, treasury, self.usd(25_000, 60_000, cents=False), when, "Wire")
        self.outflows(treasury, 2, 3_000, 12_000, since=t - 5 * DAY, until=alert_at)
        self.uses(merchant, device, self.start - 200 * DAY, alert_at)
        self.uses(treasury, device, self.start - 200 * DAY, alert_at - HOUR)
        self.logs(merchant, office, self.start - 300 * DAY, alert_at)
        self.logs(treasury, office, self.start - 300 * DAY, alert_at - 2 * HOUR)
        self.expect(merchant, "CLEAR", notes=(
            "An established merchant with ten card customers and four suppliers from the IBM data. It sweeps to its "
            "own treasury account, which shares its device and office IP and pays two more IBM accounts."))
        self.alert(alert_at)

    def benign_device(self) -> None:
        self.begin("benign_shared_device", "dev", "negative_control", "new_counterparty_burst", 1)
        t = self.pivot()
        subject = self.corp()
        device = self.device(["headless_browser"])
        [partner] = self.take(1)
        self.inflows(subject, 2, 5_000, 30_000, until=t - DAY)
        self.outflows(subject, 3, 1_500, 9_000, since=t, until=t + 5 * HOUR, per=(1, 1))
        self.uses(subject, device, t - 40 * DAY, t + 6 * HOUR)
        self.uses(partner, device, t - 90 * DAY, self.end - DAY)
        self.expect(subject, "CLEAR", notes=(
            "The subject shares a flagged device with an established IBM account, and pays three new counterparties "
            "within five hours. Neither side of the device reaches a sanctioned account."))
        self.alert(t + 7 * HOUR)

    def ip_then_device(self) -> None:
        self.begin("ip_then_device_association", "heldout", "negative_control", "dormant_reactivation", 4)
        t = self.pivot()
        subject = self.person()
        home = self.ip("residential")
        housemate = self.person()
        device = self.device(["emulator"])
        payer = self.corp()
        listed = self.corp(listed="EU-CFSP")
        self.inflows(subject, 2, 3_000, 15_000, since=t - DAY, until=t, per=(1, 1))
        self.outflows(subject, 2, 1_000, 6_000, since=t, until=t + 6 * HOUR, per=(1, 1))
        self.logs(subject, home, self.start - 120 * DAY, self.end - DAY)
        self.logs(housemate, home, self.start - 90 * DAY, self.end - 2 * DAY)
        self.uses(housemate, device, t - 20 * DAY, t + 2 * DAY)
        self.uses(payer, device, t - 6 * DAY, t + DAY)
        self.pay(payer, listed, self.usd(30_000, 70_000, cents=False), t + 5 * HOUR, "Wire")
        self.expect(subject, "CLEAR", associated=listed, notes=(
            "The subject shares a home IP with an individual whose flagged device is also used by an account that "
            "pays a sanctioned account (5 undirected hops). The device bridge is real but the subject can only reach "
            "it through the IP."))
        self.alert(t + 8 * HOUR)

    def isolated(self) -> None:
        self.begin("isolated_new_account", "dev", "negative_control", "large_outbound_wire", 2)
        opened = self.minute(self.end - 3 * DAY, self.end - DAY)
        subject = self.person(volume=0, onboarded_at=opened)
        self.expect(subject, "CLEAR", notes=(
            "Opened shortly before the end of the data window: no transfers, devices or IPs, so the search frontier "
            "is exhausted at depth 1."))
        self.alert(opened + 5 * HOUR)

    def lookalike(self) -> None:
        self.begin("device_bridge_lookalike", "heldout", "negative_control", "velocity_spike", 5)
        t = self.pivot()
        brand = self.brand()
        merchant = self.merchant(brand, "7372", onboarded_at=t - 60 * DAY)
        treasury = self.treasury(brand)
        device = self.device(["headless_browser"])
        far, side, payer = self.corp(), self.corp(), self.corp()
        [supplier] = self.take(1)
        self.inflows(merchant, 4, 20, 700, until=t - 2 * HOUR, channel="Credit Card", per=(1, 3))
        self.outflows(merchant, 1, 2_000, 15_000)
        sweep = self.usd(80_000, 140_000, cents=False)
        self.pay(merchant, treasury, sweep, t, "Wire")
        self.uses(treasury, device, t - 5 * DAY, t + HOUR)
        self.uses(far, device, t - 4 * DAY, t + 8 * HOUR)
        self.pay(far, side, self.share(sweep, 0.40, 0.48), t + 2 * HOUR, "Wire")
        self.pay(far, payer, self.share(sweep, 0.48, 0.55), t + 4 * HOUR, "Wire")
        self.pay(side, payer, self.share(sweep, 0.38, 0.45), t + 6 * HOUR, "Wire")
        self.pay(payer, supplier, self.share(sweep, 0.85, 0.92), t + 10 * HOUR, "Wire")
        self.expect(merchant, "CLEAR", notes=(
            "The same shape as c01: merchant, treasury, flagged shared device, a two-account wire split. But the "
            "funds end at an ordinary IBM account, and nothing sanctioned is within reach."))
        self.alert(t + 12 * HOUR)

    # Out-of-scope positives

    def beyond_seven(self) -> None:
        self.begin("device_bridge_beyond_depth", "heldout", "out_of_scope_positive", "merchant_volume_jump", 3)
        t = self.pivot()
        brand = self.brand()
        merchant = self.merchant(brand, "4722", onboarded_at=t - 75 * DAY)
        treasury = self.treasury(brand)
        device = self.device()
        chain = [self.corp() for _ in range(4)]
        target = self.corp(listed="UK-HMT")
        self.inflows(merchant, 3, 40, 1_200, until=t - 2 * HOUR, channel="Credit Card", per=(1, 3))
        sweep = self.usd(90_000, 150_000, cents=False)
        self.pay(merchant, treasury, sweep, t, "Wire")
        self.uses(treasury, device, t - 9 * DAY, t + HOUR)
        self.uses(chain[0], device, t - 2 * DAY, t + 16 * HOUR)
        amount = self.share(sweep, 0.93, 0.96)
        for i, (src, dst) in enumerate(pairwise([*chain, target])):
            self.pay(src, dst, amount, t + (4 + 6 * i) * HOUR, "Wire")
            amount = self.share(amount, 0.96, 0.99)
        self.expect(merchant, "CLEAR", target=target, hops=7, notes=(
            "c01's pattern stretched: after the device bridge, four wires reach the sanctioned target. The route is "
            "7 hops, so nothing sanctioned is within 6."))
        self.alert(t + 26 * HOUR)

    def beyond_eight(self) -> None:
        self.begin("peel_chain_beyond_depth", "dev", "out_of_scope_positive", "dormant_reactivation", 1)
        t = self.pivot(room=5 * DAY)
        subject = self.corp()
        chain = [self.corp() for _ in range(7)]
        target = self.corp(listed="UN-1267")
        self.inflows(subject, 2, 30_000, 90_000, since=t - 3 * DAY, until=t - 2 * HOUR, channel="Wire", per=(1, 1))
        amount, when = self.usd(150_000, 240_000, cents=False), t
        for src, dst in pairwise([subject, *chain, target]):
            self.pay(src, dst, amount, when, "Wire")
            amount = self.share(amount, 0.90, 0.97)
            when += self.rng.randint(5, 11) * HOUR
        self.expect(subject, "CLEAR", target=target, hops=8, notes=(
            "Eight wires in a chain, each passing on most of the amount, end at a sanctioned account. The route is "
            "8 hops."))
        self.alert(when)

    def finish(self) -> tuple[GraphSpec, list[dict]]:
        for n, (ts, src, dst, amount, channel) in enumerate(sorted(self.payments), 1):
            self.spec.transfer(src, dst, f"{TX_PREFIX}{n:05d}", amount, ts, channel)
        _settle(self.spec, self.cases, self.anchors[:self.taken])
        records = {}
        for n, case in enumerate(sorted(self.cases, key=lambda c: (c.alert_at, c.case_id)), 1):
            alert_id = f"alert-{1000 + n}"
            self.spec.node("Entity:Alert", id=alert_id, status=AlertStatus.PENDING.value, priority=case.priority,
                           created_at=case.alert_at, rule=case.rule, subject_id=case.subject_id,
                           fixture_version=OVERLAY_VERSION)
            self.spec.edge("ALERT_ON", alert_id, case.subject_id)
            records[case.case_id] = case.record(alert_id)
        _check_wording(self.spec)
        return self.spec, [records[case.case_id] for case in self.cases]


def _hops(adjacency: dict[str, set[str]], start: str) -> dict[str, tuple[int, str | None]]:
    """Hop count and BFS parent of everything reachable from `start`."""
    seen: dict[str, tuple[int, str | None]] = {start: (0, None)}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in sorted(adjacency.get(node, ())):
            if nxt not in seen:
                seen[nxt] = (seen[node][0] + 1, node)
                queue.append(nxt)
    return seen


def _trail(reach: dict[str, tuple[int, str | None]], node: str) -> list[str]:
    trail = [node]
    while (parent := reach[trail[-1]][1]) is not None:
        trail.append(parent)
    return trail[::-1]


def _settle(spec: GraphSpec, cases: list[_Case], anchors: list[str]) -> None:
    """Fill in each case's route and undirected distance from the planted edges alone.

    Raises ValueError if a case does not come out as designed, or if a detour through IBM data could change it.
    Anchors have no IBM edges here, so a detour costs at least one more hop between two anchors.
    """
    eligible: dict[str, set[str]] = defaultdict(set)
    undirected: dict[str, set[str]] = defaultdict(set)
    for rel, src, dst, _ in spec.edges:
        if rel not in ("TRANSFERRED", "USED_DEVICE", "LOGGED_FROM"):
            continue
        undirected[src].add(dst)
        undirected[dst].add(src)
        if rel == "TRANSFERRED":
            eligible[src].add(dst)
        elif rel == "USED_DEVICE":
            eligible[src].add(dst)
            eligible[dst].add(src)
    sanctioned = sorted(props["id"] for labels, props in spec.nodes if labels.endswith(":Sanctioned"))
    for anchor in anchors:
        for adjacency in (eligible, undirected):
            reach = _hops(adjacency, anchor)
            if close := [z for z in sanctioned if reach.get(z, (ANCHOR_MARGIN,))[0] < ANCHOR_MARGIN]:
                raise ValueError(f"anchor {anchor} is within {ANCHOR_MARGIN - 1} hops of {close}")

    def nearest_anchor(reach: dict[str, tuple[int, str | None]]) -> int | None:
        return min((reach[a][0] for a in anchors if a in reach), default=None)

    for case in cases:
        reach = _hops(eligible, case.subject_id)
        found = sorted((reach[z][0], z) for z in sanctioned if z in reach)
        if case.target:
            hops, nearest = found[0] if found else (None, None)
            tied = len(found) > 1 and found[1][0] == hops
            if nearest != case.target or tied or hops != case.hops:
                raise ValueError(f"{case.case_id}: planted as {found[:2]}, designed as {case.hops} hops to "
                                 f"{case.target}")
            if (hops <= PRESCRIBED_DEPTH) != (case.verdict == "SUSPICIOUS"):
                raise ValueError(f"{case.case_id}: {hops} hops contradicts {case.verdict}")
            case.route = _trail(reach, case.target)
        elif found:
            raise ValueError(f"{case.case_id}: {case.verdict} but reaches {found[0][1]} in {found[0][0]} hops")
        leave = nearest_anchor(reach)
        if leave is not None and leave + 1 + ANCHOR_MARGIN <= max(case.hops or 0, PRESCRIBED_DEPTH):
            raise ValueError(f"{case.case_id}: a route through IBM data could be {leave + 1 + ANCHOR_MARGIN} hops")
        own = case.target or case.associated
        if own:
            near, back = _hops(undirected, case.subject_id), _hops(undirected, own)
            case.undirected = near[own][0]
            leave, enter = nearest_anchor(near), nearest_anchor(back)
            if leave is not None and enter is not None and leave + 1 + enter <= case.undirected:
                raise ValueError(f"{case.case_id}: an undirected detour through IBM data could undercut "
                                 f"{case.undirected} hops")


def _check_wording(spec: GraphSpec) -> None:
    """IDs, names, tx_ids and alert rules must not give the answer away (the sanctions `list` is evidence)."""
    texts = [value for _, props in spec.nodes for key, value in props.items() if key != "list"]
    texts += [value for *_, props in spec.edges for value in props.values()]
    texts = [t for value in texts for t in (value if isinstance(value, list) else [value]) if isinstance(t, str)]
    if leaks := sorted({text for text in texts if _revealing(text)}):
        raise ValueError(f"overlay text gives the answer away: {leaks[:5]}")


def build_overlay(anchors: list[str], window: tuple[int, int], loaded_at: int,
                  seed: int = SEED) -> tuple[GraphSpec, list[dict]]:
    """The overlay's nodes and edges, and its cases with their expected outcomes. Pure and deterministic."""
    o = _Overlay(anchors, window, loaded_at, seed)
    first, far = o.layering_five()
    o.layering_six()
    o.direct_chain()
    o.double_bridge()
    o.ip_shortcut()
    o.fan_out()
    o.second_alert(first, far)
    o.ip_only()
    o.reverse_direction()
    o.busy_merchant()
    o.benign_device()
    o.ip_then_device()
    o.isolated()
    o.lookalike()
    o.beyond_seven()
    o.beyond_eight()
    return o.finish()


def find_anchors(graph: Graph, count: int, seed: int, avoid_id: str = NOVAPAY,
                 min_distance: int = MIN_ANCHOR_DISTANCE) -> list[str]:
    """`count` IBM accounts at least `min_distance` undirected hops from `avoid_id` over the exploration edges."""
    start = internal_id(graph, avoid_id)
    if start is None:
        raise ValueError(f"{avoid_id} is not in the graph")
    near, frontier = {start}, [start]
    for _ in range(min_distance - 1):
        if not frontier:
            break
        reached = graph.ro_query(_NEIGHBORS, {"frontier": frontier}).result_set[0][0]
        frontier = [nid for nid in reached if nid not in near]
        near.update(frontier)
    ids, keys = graph.ro_query(
        "MATCH (a:Account) WHERE a.source = 'ibm-aml' RETURN collect(ID(a)), collect(a.id)").result_set[0]
    candidates = sorted(key for nid, key in zip(ids, keys) if nid not in near)
    if len(candidates) < count:
        raise ValueError(f"only {len(candidates)} IBM accounts are {min_distance}+ hops from {avoid_id}")
    return random.Random(seed).sample(candidates, count)


def _check_loaded(graph: Graph, spec: GraphSpec) -> None:
    planned = Counter(rel for rel, *_ in spec.edges)
    rows = graph.ro_query("""
        UNWIND $ids AS eid
        MATCH (n:Entity {id: eid})-[r]-()
        WITH DISTINCT r
        RETURN type(r), count(r)
    """, {"ids": [props["id"] for _, props in spec.nodes]}).result_set
    if dict(rows) != dict(planned):
        raise RuntimeError(f"overlay edges did not all load: planned {dict(planned)}, found {dict(rows)}")


def apply_overlay(graph: Graph) -> list[dict]:
    """Load the overlay into `graph` and return its cases. Refuses a graph that already has it.

    Meta.rev stays put: the overlay is part of the fixture, not a live change.
    """
    meta = dataset_meta(graph)
    if not meta:
        raise RuntimeError("the graph has no :Meta {key: 'dataset'} node; seed it first")
    version = meta.get("fixture_version") or ""
    if OVERLAY_VERSION in version.split("+"):
        raise RuntimeError(f"{OVERLAY_VERSION} is already applied (fixture_version {version!r})")
    anchors = find_anchors(graph, ANCHOR_COUNT, SEED)
    spec, cases = build_overlay(anchors, (meta["window_start"], meta["as_of_ts"]), meta["loaded_at"], SEED)
    ids = [props["id"] for _, props in spec.nodes]
    addresses = [props["address"] for labels, props in spec.nodes if labels.endswith(":IP")]
    taken = graph.ro_query("UNWIND $ids AS eid MATCH (n:Entity {id: eid}) RETURN collect(eid)",
                           {"ids": ids}).result_set[0][0]
    taken += graph.ro_query("MATCH (ip:IP) WHERE ip.address IN $addresses RETURN collect(ip.address)",
                            {"addresses": addresses}).result_set[0][0]
    if taken:
        raise RuntimeError(f"overlay IDs or addresses already in the graph: {sorted(taken)[:5]}")

    load_spec(graph, spec)
    _check_loaded(graph, spec)
    pairs = sorted({(src, dst) for rel, src, dst, _ in spec.edges if rel == "TRANSFERRED"})
    graph.query(_SUMMARIZE, {"pairs": [list(p) for p in pairs], "max": MAX_CAPACITY}, timeout=WRITE_TIMEOUT_MS)
    graph.query("MATCH (m:Meta {key: 'dataset'}) SET m.fixture_version = $version",
                {"version": f"{version}+{OVERLAY_VERSION}" if version else OVERLAY_VERSION})
    return cases


def write_cases(cases: list[dict], base_fixture_version: str | None, path: Path = CASES_PATH) -> None:
    doc = {"overlay_version": OVERLAY_VERSION, "base_fixture_version": base_fixture_version, "seed": SEED,
           "prescribed_depth": PRESCRIBED_DEPTH, "definitions": DEFINITIONS, "cases": cases}
    path.write_text(json.dumps(doc, indent=2) + "\n")


def main() -> None:
    protected = {graph_key("acme-pay"), graph_key(get_settings().default_tenant)}
    parser = argparse.ArgumentParser(description="Apply the challenge overlay to a FinTrace graph.")
    parser.add_argument("--graph", required=True, help="graph key, e.g. fintrace:overlay-copy")
    parser.add_argument("--write-cases", action="store_true", help=f"write {CASES_PATH.relative_to(ROOT)}")
    parser.add_argument("--allow-main", action="store_true", help=f"allow changing {', '.join(sorted(protected))}")
    args = parser.parse_args()
    if args.graph in protected and not args.allow_main:
        raise SystemExit(f"refusing to change {args.graph}; apply the overlay to a copy or pass --allow-main")
    db = get_db()
    if args.graph not in db.list_graphs():
        raise SystemExit(f"{args.graph} does not exist")
    graph = db.select_graph(args.graph)
    base = dataset_meta(graph).get("fixture_version")
    cases = apply_overlay(graph)
    if args.write_cases:
        write_cases(cases, base)
    for c in cases:
        e = c["expected"]
        print(f"  {c['case_id']} {c['split']:<8} {c['category']:<22} {c['pattern']:<30} {e['verdict']:<10} "
              f"eligible={e['eligible_hops']} undirected={e['shortest_undirected_hops_to_sanctioned']}")
    print(f"Applied {OVERLAY_VERSION} to {args.graph}: {len(cases)} cases"
          + (f", wrote {CASES_PATH.relative_to(ROOT)}" if args.write_cases else ""))


if __name__ == "__main__":
    main()
