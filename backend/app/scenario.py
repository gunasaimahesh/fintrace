"""The planted NovaPay / Rohan / VIP scenario and the org layer around it.

Everything here is synthetic and written for the demo. It attaches to clean IBM accounts
picked by `scripts/prepare_ibm_slice.py` (`backend/data/scenario_anchors.json`). The ring
side (Volkov, Orion, Baltic, Vortex) only connects through planted edges, so the IBM data
cannot create a shortcut to the sanctioned account.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

NOVAPAY = "merch-nova-pay"
NOVA_OPS = "acct-nova-ops"
SHARED_DEVICE = "dev-fp-8821"
VOLKOV = "acct-mule-volkov"
ORION = "acct-mule-orion"
BALTIC = "acct-shell-baltic"
VORTEX = "acct-vortex-sdn"
PROXY_IP = "ip-185-220-101-7"
VIP = "acct-vip-enterprise-payroll"
LUMEN = "merch-lumen-games"
ISOLATED = "acct-clean-isolated"
UNKNOWN = "acct-ghost-999"

ROHAN = "emp-rohan-mehta"
PRIYA = "emp-priya-nair"
WAIVER = "dec-waiver-77"
POLICY = "pol-aml-sec-4"
TICKET = "tick-risk-408"
MESSAGE = "msg-slack-9921"

RING_CORE = (VOLKOV, ORION, BALTIC, VORTEX)
LAUNDERING_ACCOUNTS = (NOVAPAY, NOVA_OPS, VOLKOV, ORION, BALTIC, VORTEX)

MINUTE = 60
HOUR = 3600
DAY = 86400

ACCOUNT = "Entity:RiskNode:Account"


@dataclass
class GraphSpec:
    nodes: list[tuple[str, dict]] = field(default_factory=list)
    edges: list[tuple[str, str, str, dict]] = field(default_factory=list)

    def node(self, labels: str, **props) -> None:
        self.nodes.append((labels, props))

    def edge(self, rel: str, src: str, dst: str, **props) -> None:
        self.edges.append((rel, src, dst, props))

    def transfer(self, src: str, dst: str, tx_id: str, amount: int, ts: int, channel: str) -> None:
        self.edge("TRANSFERRED", src, dst, tx_id=tx_id, amount=amount, ts=ts, channel=channel, weight=1)

    def identity(self, rel: str, account: str, target: str, first_seen: int, last_seen: int) -> None:
        self.edge(rel, account, target, first_seen=first_seen, last_seen=last_seen, weight=1)


def build_scenario(anchors: dict[str, list[str]], window: tuple[int, int], base_ts: int,
                   loaded_at: int, seed: int = 42) -> GraphSpec:
    """`window` is the IBM data's (first, last) transfer time; `base_ts` is when the ring moves money."""
    rng = random.Random(seed)
    spec = GraphSpec()
    start, end = window
    tx = iter(f"nova-{n:04d}" for n in range(1, 10_000))

    def account(labels: str, node_id: str, name: str, **props) -> None:
        props.setdefault("status", "ACTIVE")
        props.setdefault("tier", "standard")
        props.setdefault("source", "planted")
        spec.node(labels, id=node_id, name=name, topology_updated_at=loaded_at, **props)

    def when(lo: int, hi: int) -> int:
        return rng.randrange(lo, hi) // MINUTE * MINUTE

    # Merchant side: NovaPay and its treasury account look like an ordinary digital-goods merchant.
    account(f"{ACCOUNT}:Merchant", NOVAPAY, "NovaPay Digital Goods", tier="enterprise",
            kind="Corporation", bank="FinTrace Demo Bank", mcc="5816",
            monthly_volume=41_000_000, onboarded_at=base_ts - 20 * DAY)
    account(ACCOUNT, NOVA_OPS, "NovaPay Treasury Ops", kind="Corporation",
            bank="FinTrace Demo Bank", monthly_volume=18_500_000)
    for cust in anchors["novapay_customers"]:
        for _ in range(rng.randint(1, 3)):
            spec.transfer(cust, NOVAPAY, next(tx), rng.randint(250_00, 8_500_00), when(start, end), "Credit Card")
    for vendor in anchors["novapay_vendors"]:
        for _ in range(rng.randint(1, 2)):
            spec.transfer(NOVAPAY, vendor, next(tx), rng.randint(2_000_00, 45_000_00), when(start, end), "ACH")
    for vendor in anchors["nova_ops_vendors"]:
        spec.transfer(NOVA_OPS, vendor, next(tx), rng.randint(1_000_00, 12_000_00), when(start, base_ts), "ACH")

    # The ring: a sweep into treasury, then the same operator's device moves funds through Volkov.
    spec.node("Entity:RiskNode:Device", id=SHARED_DEVICE, os="Headless Linux Chromium",
              fingerprint="fp-8821", risk_flags=["headless_browser"], topology_updated_at=loaded_at)
    account(ACCOUNT, VOLKOV, "Volkov Clearing LLC", kind="Corporation", bank="Cyprus Bank #212",
            monthly_volume=22_000_000)
    account(ACCOUNT, ORION, "Orion Logistics FZE", kind="Corporation", bank="UAE Bank #57",
            monthly_volume=9_000_000)
    account(ACCOUNT, BALTIC, "Baltic Trade Settlement", kind="Corporation", bank="Latvia Bank #9",
            monthly_volume=26_000_000)
    account(f"{ACCOUNT}:Sanctioned", VORTEX, "Vortex Lazarus Syndicate", kind="Corporation",
            bank="Offshore Bank #44", list="OFAC-SDN", monthly_volume=0)

    spec.transfer(NOVAPAY, NOVA_OPS, "nova-sweep-01", 145_000_00, base_ts + 100 * MINUTE, "Wire")
    spec.identity("USED_DEVICE", NOVA_OPS, SHARED_DEVICE, base_ts - 6 * DAY, base_ts + 90 * MINUTE)
    spec.identity("USED_DEVICE", VOLKOV, SHARED_DEVICE, base_ts - 3 * DAY, base_ts + 400 * MINUTE)
    spec.transfer(VOLKOV, ORION, "nova-layer-01", 64_000_00, base_ts + 300 * MINUTE, "Wire")
    spec.transfer(VOLKOV, BALTIC, "nova-layer-02", 138_000_00, base_ts + 400 * MINUTE, "Wire")
    spec.transfer(ORION, BALTIC, "nova-layer-03", 61_500_00, base_ts + 600 * MINUTE, "Wire")
    spec.transfer(BALTIC, VORTEX, "nova-exit-01", 132_000_00, base_ts + 900 * MINUTE, "Wire")

    # Collateral: Volkov logs in through a corporate proxy that a legitimate VIP payroll account also uses.
    spec.node("Entity:RiskNode:IP", id=PROXY_IP, address="185.220.101.7", kind="corporate_proxy",
              topology_updated_at=loaded_at)
    account(ACCOUNT, VIP, "Tier-1 Enterprise Payroll", tier="vip", kind="Corporation",
            bank="FinTrace Demo Bank", monthly_volume=240_000_000)
    spec.identity("LOGGED_FROM", VOLKOV, PROXY_IP, base_ts - 3 * DAY, base_ts + 380 * MINUTE)
    spec.identity("LOGGED_FROM", VIP, PROXY_IP, start, end)
    payday_1 = start + 6 * DAY + 6 * HOUR
    payday_2 = payday_1 + 9 * DAY
    for payee in anchors["vip_payroll_payees"]:
        salary = rng.randint(3_000_00, 12_000_00)
        spec.transfer(VIP, payee, next(tx), salary, payday_1, "ACH")
        spec.transfer(VIP, payee, next(tx), salary, payday_2, "ACH")

    # A second, compliant merchant approved by the same employee, for the governance blast radius.
    account(f"{ACCOUNT}:Merchant", LUMEN, "Lumen Games Studio", tier="standard", kind="Corporation",
            bank="FinTrace Demo Bank", mcc="5816", monthly_volume=18_000_000,
            onboarded_at=base_ts - 35 * DAY)
    for cust in anchors["lumen_customers"]:
        for _ in range(rng.randint(1, 2)):
            spec.transfer(cust, LUMEN, next(tx), rng.randint(150_00, 4_000_00), when(start, end), "Credit Card")

    account(ACCOUNT, ISOLATED, "Isolated Test Account", kind="Individual", bank="FinTrace Demo Bank",
            monthly_volume=0)

    _add_org_layer(spec, base_ts)
    return spec


def _add_org_layer(spec: GraphSpec, base_ts: int) -> None:
    teams = {
        "team-merchant-onboarding": "Merchant Onboarding",
        "team-aml-compliance": "AML Compliance",
        "team-fraud-ops": "Fraud Operations",
    }
    for tid, name in teams.items():
        spec.node("Entity:Team", id=tid, name=name)

    employees = [
        (ROHAN, "Rohan Mehta", "Merchant Onboarding Manager", 2, False, "team-merchant-onboarding"),
        ("emp-meera-iyer", "Meera Iyer", "Head of Merchant Growth", 3, False, "team-merchant-onboarding"),
        (PRIYA, "Priya Nair", "AML Compliance Lead", 3, True, "team-aml-compliance"),
        ("emp-daniel-okafor", "Daniel Okafor", "Head of Financial Crime", 3, False, "team-aml-compliance"),
        ("emp-aisha-khan", "Aisha Khan", "Fraud Analyst", 1, False, "team-fraud-ops"),
    ]
    for eid, name, title, level, on_call, team in employees:
        spec.node("Entity:Employee", id=eid, name=name, title=title, clearance=f"L{level}",
                  clearance_level=level, on_call=on_call)
        spec.edge("MEMBER_OF", eid, team)
    for report, manager in [(ROHAN, "emp-meera-iyer"), (PRIYA, "emp-daniel-okafor"),
                            ("emp-aisha-khan", PRIYA)]:
        spec.edge("REPORTS_TO", report, manager)

    spec.node("Entity:PolicyDoc", id=POLICY, title="AML Policy Section 4: Waivers and Account Restrictions",
              version="2026.2", waiver_threshold_monthly=25_000_000,
              required_clearance="L3", required_clearance_level=3,
              required_clearance_for_freeze="L3", required_clearance_level_for_freeze=3,
              text=("4.2 Waiving an onboarding control for a merchant that processes more than "
                    "$250,000 per month requires approval from an officer with L3 clearance. "
                    "4.5 Freezing a live merchant or a linked ring of accounts requires L3 approval, "
                    "and infrastructure shared with unrelated customers must be excluded from the freeze."))
    spec.edge("OWNED_BY", POLICY, "team-aml-compliance")

    spec.node("Entity:Ticket", id=TICKET, system="Jira", status="CLOSED", opened_at=base_ts - 24 * DAY,
              title="NovaPay onboarding: device attestation failing",
              text=("Attestation fails for most NovaPay admin sessions: headless Chromium fingerprints "
                    "(fp-8821). Recommend holding go-live until resolved."))
    spec.edge("OPENED", "emp-aisha-khan", TICKET)
    spec.node("Entity:Message", id=MESSAGE, system="Slack", channel="#merchant-onboarding",
              sent_at=base_ts - 21 * DAY - 2 * HOUR,
              text=("Approving the device attestation waiver for NovaPay so they can go live this week. "
                    "I'll own the risk."))
    spec.edge("AUTHORED", ROHAN, MESSAGE)
    spec.node("Entity:Decision", id=WAIVER, kind="onboarding_waiver", control="device_attestation",
              status="ACTIVE", approved_at=base_ts - 21 * DAY, merchant_monthly_volume=41_000_000,
              summary="Device attestation waived for NovaPay onboarding")
    spec.edge("APPROVED", ROHAN, WAIVER, at=base_ts - 21 * DAY)
    spec.edge("APPLIES_TO", WAIVER, NOVAPAY)
    spec.edge("SOURCED_FROM", WAIVER, TICKET)
    spec.edge("SOURCED_FROM", WAIVER, MESSAGE)
    spec.edge("GOVERNED_BY", WAIVER, POLICY)

    spec.node("Entity:Ticket", id="tick-risk-415", system="Jira", status="CLOSED",
              opened_at=base_ts - 37 * DAY, title="Lumen Games onboarding: KYB document refresh pending",
              text="Lumen's incorporation certificate is 13 months old; refresh requested after go-live.")
    spec.edge("OPENED", ROHAN, "tick-risk-415")
    spec.node("Entity:Decision", id="dec-waiver-81", kind="onboarding_waiver", control="kyb_document_refresh",
              status="ACTIVE", approved_at=base_ts - 35 * DAY, merchant_monthly_volume=18_000_000,
              summary="KYB document refresh deferred for Lumen Games onboarding")
    spec.edge("APPROVED", ROHAN, "dec-waiver-81", at=base_ts - 35 * DAY)
    spec.edge("APPLIES_TO", "dec-waiver-81", LUMEN)
    spec.edge("SOURCED_FROM", "dec-waiver-81", "tick-risk-415")
    spec.edge("GOVERNED_BY", "dec-waiver-81", POLICY)
