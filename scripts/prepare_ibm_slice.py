#!/usr/bin/env python3
"""Build the IBM AML HI-Small slice that FinTrace loads into FalkorDB.

The slice keeps every account and transfer of all labeled laundering instances in
HI-Small_Patterns.txt, all of their direct counterparties, and a capped second ring of
counterparties as background traffic. Every transfer between two selected accounts is
kept, so pattern instances stay complete. Ground-truth labels are written to
backend/eval/ and are never loaded into the graph.

Usage:
    python scripts/prepare_ibm_slice.py --raw-dir data/raw
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "backend" / "data"
EVAL_DIR = ROOT / "backend" / "eval"

TRANS_COLUMNS = [
    "timestamp", "from_bank", "from_account", "to_bank", "to_account",
    "amount_received", "receiving_currency", "amount_paid", "payment_currency",
    "payment_format", "is_laundering",
]
STRING_COLUMNS = {c: str for c in ("timestamp", "from_bank", "from_account", "to_bank",
                                   "to_account", "receiving_currency", "payment_currency",
                                   "payment_format")}
TIME_FORMAT = "%Y/%m/%d %H:%M"
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
DAY = 86400
WEEK = 7 * DAY


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw-dir", type=Path, default=ROOT / "data" / "raw")
    p.add_argument("--target-accounts", type=int, default=40_000,
                   help="upper bound on accounts in the slice")
    p.add_argument("--hop2-cap", type=int, default=10,
                   help="max new second-ring counterparties sampled per first-ring account")
    p.add_argument("--as-of", default="2026-10-12T00:00:00+00:00",
                   help="timestamps are shifted by whole weeks so the last transfer lands just before this")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def to_epoch(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, format=TIME_FORMAT, utc=True)
    return (parsed - EPOCH) // pd.Timedelta(seconds=1)


def account_keys(bank: pd.Series, account: pd.Series, ambiguous: set[str]) -> pd.Series:
    keys = "acct-" + account
    clash = account.isin(ambiguous)
    return keys.where(~clash, keys + "-" + bank.astype(str))


def load_transactions(path: Path) -> tuple[pd.DataFrame, set[str]]:
    df = pd.read_csv(path, header=0, names=TRANS_COLUMNS, dtype=STRING_COLUMNS)
    df["row"] = np.arange(len(df))
    df["from_bank"] = df["from_bank"].astype(int)
    df["to_bank"] = df["to_bank"].astype(int)
    df["ts"] = to_epoch(df["timestamp"])

    pairs = pd.concat([
        df[["from_bank", "from_account"]].set_axis(["bank", "account"], axis=1),
        df[["to_bank", "to_account"]].set_axis(["bank", "account"], axis=1),
    ]).drop_duplicates()
    ambiguous = set(pairs.loc[pairs["account"].duplicated(keep=False), "account"])

    df["src"] = account_keys(df["from_bank"], df["from_account"], ambiguous)
    df["dst"] = account_keys(df["to_bank"], df["to_account"], ambiguous)
    return df, ambiguous


def usd_rates(df: pd.DataFrame) -> dict[str, float]:
    """Exchange rates (units per USD) implied by the dataset's own cross-currency rows."""
    usd_out = df[(df["payment_currency"] == "US Dollar") & (df["receiving_currency"] != "US Dollar")]
    ratio = usd_out["amount_received"] / usd_out["amount_paid"]
    rates = ratio.groupby(usd_out["receiving_currency"]).median().to_dict()
    rates["US Dollar"] = 1.0
    missing = set(df["payment_currency"].unique()) - set(rates)
    if missing:
        raise SystemExit(f"No USD cross-currency rows to derive a rate for: {sorted(missing)}")
    return {k: float(v) for k, v in sorted(rates.items())}


def parse_patterns(path: Path, ambiguous: set[str]) -> list[dict]:
    header = re.compile(r"BEGIN LAUNDERING ATTEMPT - ([A-Z-]+)(?::\s*(.*))?")
    instances: list[dict] = []
    counters: dict[str, int] = {}
    current: dict | None = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("BEGIN LAUNDERING ATTEMPT"):
            match = header.match(line)
            ptype = match.group(1)
            counters[ptype] = counters.get(ptype, 0) + 1
            current = {
                "instance_id": f"{ptype.lower()}-{counters[ptype]:03d}",
                "pattern_type": ptype,
                "pattern_detail": (match.group(2) or "").strip(),
                "rows": [],
            }
        elif line.startswith("END LAUNDERING ATTEMPT"):
            instances.append(current)
            current = None
        elif line and current is not None:
            current["rows"].append(line.split(","))

    for inst in instances:
        rows = pd.DataFrame(inst.pop("rows"), columns=TRANS_COLUMNS)
        src = account_keys(rows["from_bank"].astype(int), rows["from_account"], ambiguous)
        dst = account_keys(rows["to_bank"].astype(int), rows["to_account"], ambiguous)
        inst["accounts"] = sorted(set(src) | set(dst))
        inst["n_transfers"] = len(rows)
    return instances


def select_accounts(transfers: pd.DataFrame, core: set[str], target: int, hop2_cap: int,
                    seed: int) -> tuple[set[str], set[str]]:
    touch_core = transfers[transfers["src"].isin(core) | transfers["dst"].isin(core)]
    hop1 = (set(touch_core["src"]) | set(touch_core["dst"])) - core

    touch_hop1 = transfers[transfers["src"].isin(hop1) | transfers["dst"].isin(hop1)]
    directed = pd.concat([
        touch_hop1[["src", "dst"]],
        touch_hop1[["dst", "src"]].set_axis(["src", "dst"], axis=1),
    ])
    taken = core | hop1
    candidates = directed[directed["src"].isin(hop1) & ~directed["dst"].isin(taken)].drop_duplicates()
    candidates = candidates.sample(frac=1.0, random_state=seed).groupby("src").head(hop2_cap)
    room = max(target - len(taken), 0)
    hop2 = set(pd.unique(candidates["dst"])[:room])
    return hop1, hop2


def pick_anchors(accounts: pd.DataFrame, slice_degree: pd.Series, seed: int) -> dict[str, list[str]]:
    """Clean, moderately connected IBM accounts the planted scenario attaches to."""
    pool = accounts[(accounts["laundering_any"] == 0) & (accounts["ring"] != "core")]
    degree = slice_degree.reindex(pool["account_id"]).fillna(0).to_numpy()
    pool = pool[(degree >= 3) & (degree <= 40)]
    chosen = pool.sample(n=66, random_state=seed)["account_id"].tolist()
    return {
        "novapay_customers": chosen[0:18],
        "novapay_vendors": chosen[18:30],
        "vip_payroll_payees": chosen[30:52],
        "lumen_customers": chosen[52:60],
        "nova_ops_vendors": chosen[60:66],
    }


def main() -> None:
    args = parse_args()
    raw = args.raw_dir
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading transactions...")
    df, ambiguous = load_transactions(raw / "HI-Small_Trans.csv")
    rates = usd_rates(df)
    df["amount"] = (df["amount_paid"] / df["payment_currency"].map(rates) * 100).round().astype("int64")

    as_of = int(datetime.fromisoformat(args.as_of).timestamp())
    shift = ((as_of - int(df["ts"].max())) // WEEK) * WEEK
    df["ts"] = df["ts"] + shift

    laundering_accounts = set(df.loc[df["is_laundering"] == 1, "src"]) | set(df.loc[df["is_laundering"] == 1, "dst"])
    transfers = df[df["src"] != df["dst"]]

    print("Parsing pattern instances...")
    instances = parse_patterns(raw / "HI-Small_Patterns.txt", ambiguous)
    core = set().union(*(set(i["accounts"]) for i in instances))

    hop1, hop2 = select_accounts(transfers, core, args.target_accounts, args.hop2_cap, args.seed)
    selected = core | hop1 | hop2
    slice_tx = transfers[transfers["src"].isin(selected) & transfers["dst"].isin(selected)]
    slice_tx = slice_tx.sort_values(["ts", "row"])

    out_tx = pd.DataFrame({
        "tx_id": "ibm-" + slice_tx["row"].astype(str),
        "src": slice_tx["src"],
        "dst": slice_tx["dst"],
        "amount": slice_tx["amount"],
        "ts": slice_tx["ts"],
        "channel": slice_tx["payment_format"],
        "currency": slice_tx["payment_currency"],
    })

    print("Joining account owners...")
    owners = pd.read_csv(raw / "HI-Small_accounts.csv", dtype=str)
    owners["account_id"] = account_keys(owners["Bank ID"].astype(int), owners["Account Number"], ambiguous)
    owners = owners.drop_duplicates("account_id").set_index("account_id")

    span_days = (int(df["ts"].max()) - int(df["ts"].min())) / DAY
    monthly = (transfers.groupby("src")["amount"].sum() * (30 / span_days)).round().astype("int64")

    ids = sorted(selected)
    ring = pd.Series("hop2", index=ids)
    ring[ring.index.isin(hop1)] = "hop1"
    ring[ring.index.isin(core)] = "core"
    accounts = pd.DataFrame({"account_id": ids})
    accounts["bank"] = owners["Bank Name"].reindex(ids).fillna("Unknown Bank").to_numpy()
    accounts["owner_id"] = owners["Entity ID"].reindex(ids).fillna("").to_numpy()
    accounts["owner_name"] = owners["Entity Name"].reindex(ids).fillna("Unknown Owner").to_numpy()
    accounts["owner_kind"] = accounts["owner_name"].str.replace(r" #\d+$", "", regex=True)
    accounts["monthly_volume"] = monthly.reindex(ids).fillna(0).astype("int64").to_numpy()
    accounts["laundering_any"] = accounts["account_id"].isin(laundering_accounts).astype(int)
    accounts["ring"] = ring.reindex(ids).to_numpy()

    membership: dict[str, list[dict]] = {}
    for inst in instances:
        for acct in inst["accounts"]:
            membership.setdefault(acct, []).append(inst)
    accounts["instance_ids"] = accounts["account_id"].map(
        lambda a: "|".join(i["instance_id"] for i in membership.get(a, [])))
    accounts["pattern_types"] = accounts["account_id"].map(
        lambda a: "|".join(sorted({i["pattern_type"] for i in membership.get(a, [])})))

    pairs = pd.concat([out_tx[["src", "dst"]], out_tx[["dst", "src"]].set_axis(["src", "dst"], axis=1)]).drop_duplicates()
    slice_degree = pairs.groupby("src").size()
    anchors = pick_anchors(accounts, slice_degree, args.seed)

    tx_path = DATA_DIR / "ibm_aml_hi_small_slice.csv.gz"
    acct_path = DATA_DIR / "ibm_aml_hi_small_accounts.csv.gz"
    out_tx.to_csv(tx_path, index=False, compression={"method": "gzip", "mtime": 0})
    accounts[["account_id", "bank", "owner_id", "owner_name", "owner_kind", "monthly_volume"]].to_csv(
        acct_path, index=False, compression={"method": "gzip", "mtime": 0})
    (DATA_DIR / "scenario_anchors.json").write_text(json.dumps(anchors, indent=2) + "\n")

    accounts[["account_id", "laundering_any", "instance_ids", "pattern_types", "ring"]].to_csv(
        EVAL_DIR / "labels.csv", index=False)
    pd.DataFrame([{
        "instance_id": i["instance_id"],
        "pattern_type": i["pattern_type"],
        "pattern_detail": i["pattern_detail"],
        "n_accounts": len(i["accounts"]),
        "n_transfers": i["n_transfers"],
        "accounts": "|".join(i["accounts"]),
    } for i in instances]).to_csv(EVAL_DIR / "instances.csv", index=False)

    manifest = {
        "source": "IBM Transactions for Anti Money Laundering (AML), HI-Small",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"target_accounts": args.target_accounts, "hop2_cap": args.hop2_cap,
                   "as_of": args.as_of, "seed": args.seed},
        "time_shift_seconds": int(shift),
        "time_shift_days": int(shift // DAY),
        "currency_units_per_usd": rates,
        "counts": {
            "source_transactions": int(len(df)),
            "source_self_transfers_dropped": int(len(df) - len(transfers)),
            "pattern_instances": len(instances),
            "accounts_core": len(core),
            "accounts_hop1": len(hop1),
            "accounts_hop2": len(hop2),
            "accounts_total": len(selected),
            "accounts_laundering_any": int(accounts["laundering_any"].sum()),
            "transfers": int(len(out_tx)),
            "transfers_laundering": int(slice_tx["is_laundering"].sum()),
        },
        "ts_range": [int(out_tx["ts"].min()), int(out_tx["ts"].max())],
    }
    (DATA_DIR / "slice_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["counts"], indent=2))
    print(f"Wrote {tx_path.relative_to(ROOT)} ({tx_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
