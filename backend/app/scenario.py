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
VOLKOV = "acct-volkov-clearing"
ORION = "acct-orion-logistics"
BALTIC = "acct-baltic-trade"
VORTEX = "acct-vortex-holdings"
PROXY_IP = "ip-185-220-101-7"
VIP = "acct-tier1-payroll"
LUMEN = "merch-lumen-games"
ISOLATED = "acct-ind-20118"
UNKNOWN = "acct-ghost-999"

RENAMED = {
    "acct-mule-volkov": VOLKOV,
    "acct-mule-orion": ORION,
    "acct-shell-baltic": BALTIC,
    "acct-vortex-sdn": VORTEX,
    "acct-vip-enterprise-payroll": VIP,
    "acct-clean-isolated": ISOLATED,
}

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
    account(f"{ACCOUNT}:Sanctioned", VORTEX, "Vortex Holdings Ltd", kind="Corporation",
            bank="Seychelles Bank #44", list="OFAC-SDN", monthly_volume=0)

    spec.transfer(NOVAPAY, NOVA_OPS, next(tx), 145_000_00, base_ts + 100 * MINUTE, "Wire")
    spec.identity("USED_DEVICE", NOVA_OPS, SHARED_DEVICE, base_ts - 6 * DAY, base_ts + 90 * MINUTE)
    spec.identity("USED_DEVICE", VOLKOV, SHARED_DEVICE, base_ts - 3 * DAY, base_ts + 400 * MINUTE)
    spec.transfer(VOLKOV, ORION, next(tx), 64_000_00, base_ts + 300 * MINUTE, "Wire")
    spec.transfer(VOLKOV, BALTIC, next(tx), 138_000_00, base_ts + 400 * MINUTE, "Wire")
    spec.transfer(ORION, BALTIC, next(tx), 61_500_00, base_ts + 600 * MINUTE, "Wire")
    spec.transfer(BALTIC, VORTEX, next(tx), 132_000_00, base_ts + 900 * MINUTE, "Wire")

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

    account(ACCOUNT, ISOLATED, "Retail Individual Account", kind="Individual", bank="FinTrace Demo Bank",
            monthly_volume=0)

    return spec
