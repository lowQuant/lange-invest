#!/usr/bin/env python3
"""IBKR report ingestion → PRIVATE portfolio store.

Fetches/parses the latest subscribed IBKR Flex statement, normalises positions
and P&L, and writes them to ``PRIVATE_DIR/real_portfolio.json`` — a private store
read ONLY by the gated Portfolio route (``app.portfolio_store``). This data is
real-account sensitive: it is NEVER written to public snapshots and NEVER added
to the public allowlist. The private dir is git-ignored.

Usage:
    python scripts/ingest_ibkr.py            # fetch via Flex Web Service (.env)
    python scripts/ingest_ibkr.py --file q.xml   # parse a delivered Flex XML file
    python scripts/ingest_ibkr.py --sample   # write a synthetic fixture (dev/gated demo)

Flex Web Service env vars: IBKR_FLEX_TOKEN, IBKR_FLEX_QUERY_ID.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import PRIVATE_DIR  # noqa: E402

FLEX_BASE = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
OUT_FILE = PRIVATE_DIR / "real_portfolio.json"


# ── Flex Web Service (two-step) ──────────────────────────────────────────────
def fetch_flex_xml(token: str, query_id: str, attempts: int = 8) -> str:
    send = f"{FLEX_BASE}.SendRequest?t={token}&q={query_id}&v=3"
    root = ET.fromstring(urllib.request.urlopen(send, timeout=30).read())
    if root.findtext("Status") != "Success":
        raise RuntimeError(f"Flex SendRequest failed: {root.findtext('ErrorMessage')}")
    ref, url = root.findtext("ReferenceCode"), root.findtext("Url")
    get = f"{url}?t={token}&q={ref}&v=3"
    for _ in range(attempts):
        data = urllib.request.urlopen(get, timeout=30).read().decode()
        if "<FlexQueryResponse" in data:
            return data
        time.sleep(3)  # statement still generating
    raise RuntimeError("Flex statement not ready after retries.")


# ── Normalisation ────────────────────────────────────────────────────────────
def normalise(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)

    def fnum(el, attr, default=0.0):
        try:
            return float(el.get(attr) or default)
        except (TypeError, ValueError):
            return default

    positions = []
    total_value = 0.0
    unrealized = realized = 0.0
    as_of = ""
    base_ccy = "USD"

    for pos in root.iter("OpenPosition"):
        val = fnum(pos, "positionValue") or (fnum(pos, "position") * fnum(pos, "markPrice"))
        total_value += val
        unrealized += fnum(pos, "fifoPnlUnrealized")
        as_of = pos.get("reportDate") or as_of
        base_ccy = pos.get("currency") or base_ccy
        positions.append({
            "symbol": pos.get("symbol", ""),
            "asset_class": pos.get("assetCategory", ""),
            "quantity": fnum(pos, "position"),
            "mark_price": fnum(pos, "markPrice"),
            "position_value": round(val, 2),
            "unrealized_pnl": round(fnum(pos, "fifoPnlUnrealized"), 2),
        })

    for trade in root.iter("Trade"):
        realized += fnum(trade, "fifoPnlRealized")

    for el in root.iter("EquitySummaryByReportDateInBase"):
        as_of = el.get("reportDate") or as_of

    denom = total_value or 1.0
    for p in positions:
        p["weight"] = round(p["position_value"] / denom, 4)
    positions.sort(key=lambda p: abs(p["position_value"]), reverse=True)

    # Allocation by asset class.
    alloc: dict[str, float] = {}
    for p in positions:
        alloc[p["asset_class"]] = alloc.get(p["asset_class"], 0.0) + p["weight"]
    allocation = [{"name": k or "Other", "weight": round(v, 4)} for k, v in sorted(alloc.items(), key=lambda kv: -kv[1])]

    return {
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "as_of": as_of,
        "base_currency": base_ccy,
        "nav": round(total_value, 2),
        "pnl": {"unrealized": round(unrealized, 2), "realized": round(realized, 2),
                "total": round(unrealized + realized, 2)},
        "positions": positions,
        "allocation": allocation,
    }


def sample() -> dict:
    rows = [
        ("AAPL", "STK", 320, 214.5, 1820.0),
        ("MSFT", "STK", 140, 470.2, 980.0),
        ("ES", "FUT", 2, 5500.0, -340.0),
        ("GC", "FUT", 3, 2350.0, 612.0),
        ("NVDA", "STK", 90, 128.7, 1240.0),
        ("CL", "FUT", -4, 71.2, 205.0),
    ]
    positions, total = [], 0.0
    for sym, ac, qty, px, upnl in rows:
        val = abs(qty) * px * (50 if ac == "FUT" else 1)
        total += val
        positions.append({"symbol": sym, "asset_class": ac, "quantity": qty,
                          "mark_price": px, "position_value": round(val, 2),
                          "unrealized_pnl": upnl})
    for p in positions:
        p["weight"] = round(p["position_value"] / total, 4)
    positions.sort(key=lambda p: abs(p["position_value"]), reverse=True)
    alloc: dict[str, float] = {}
    for p in positions:
        alloc[p["asset_class"]] = alloc.get(p["asset_class"], 0.0) + p["weight"]
    return {
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "as_of": dt.date.today().isoformat(), "base_currency": "USD",
        "nav": round(total, 2),
        "pnl": {"unrealized": sum(p["unrealized_pnl"] for p in positions),
                "realized": 1430.0, "total": sum(p["unrealized_pnl"] for p in positions) + 1430.0},
        "positions": positions,
        "allocation": [{"name": k, "weight": round(v, 4)} for k, v in sorted(alloc.items(), key=lambda kv: -kv[1])],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest IBKR Flex report to the private store.")
    ap.add_argument("--file", help="Parse a delivered Flex XML file instead of fetching.")
    ap.add_argument("--sample", action="store_true", help="Write a synthetic fixture (dev only).")
    args = ap.parse_args()

    if args.sample:
        payload = sample()
    elif args.file:
        payload = normalise(Path(args.file).read_text())
    else:
        token, qid = os.getenv("IBKR_FLEX_TOKEN"), os.getenv("IBKR_FLEX_QUERY_ID")
        if not token or not qid:
            raise SystemExit("Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID, pass --file, or --sample.")
        payload = normalise(fetch_flex_xml(token, qid))

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT_FILE} — {len(payload['positions'])} positions, NAV {payload['nav']}")


if __name__ == "__main__":
    main()
