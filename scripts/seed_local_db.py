#!/usr/bin/env python3
"""Seed a LOCAL ArcticDB (LMDB) with sample `futures` + `market_data` libraries.

Dev convenience so the ArcticDB tab is browsable without S3.

  * market_data/<TICKER>  — single DatetimeIndex, daily OHLCV (stock charting).
  * futures/<ROOT>        — MultiIndex (date, localsymbol) with OHLCV + dte,
                            i.e. several live contracts per day — exactly the
                            structure the viewer's contract mode expects, so
                            continuous curves / spreads / overlays work.

    LANGE_DB_URI=lmdb:///tmp/lange_db python scripts/seed_local_db.py
"""
from __future__ import annotations

import math
import os
import random
from datetime import date, timedelta

import pandas as pd

URI = os.getenv("LANGE_DB_URI", "lmdb:///tmp/lange_db")

FUT_ROOTS = {"ES": 5000.0, "NQ": 18000.0, "CL": 75.0, "GC": 2300.0, "ZN": 110.0, "6E": 1.08}
STOCKS = {"AAPL": 180.0, "MSFT": 420.0, "NVDA": 120.0, "AMZN": 185.0, "GOOGL": 165.0, "META": 500.0, "JPM": 200.0}

MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
START = date(2022, 1, 3)
END = date(2025, 6, 1)


def _bdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def stock_ohlcv(seed: int, px: float) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    for d in _bdays(START, END):
        o = px
        c = max(0.5, o * (1 + rng.gauss(0.0004, 0.014)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
        rows.append((pd.Timestamp(d), round(o, 2), round(h, 2), round(l, 2), round(c, 2),
                     int(abs(rng.gauss(2e6, 5e5)))))
        px = c
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).set_index("date")


def _contracts(root: str):
    """Quarterly expiries (3rd Friday-ish) across the sample window + a year out."""
    out = []
    for y in range(2022, 2027):
        for m in (3, 6, 9, 12):
            exp = date(y, m, 15)
            out.append((f"{root}{MONTH_CODE[m]}{str(y)[2:]}", exp))
    return out


def futures_multiindex(seed: int, base: float, root: str) -> pd.DataFrame:
    """MultiIndex (date, localsymbol) with OHLCV + dte; ~3 live contracts/day."""
    rng = random.Random(seed)
    contracts = _contracts(root)
    # One underlying random walk; each contract = spot * (1 + carry*dte).
    spot = base
    carry = rng.uniform(-0.00004, 0.00008)  # per-day-of-dte term-structure slope
    idx, recs = [], []
    for d in _bdays(START, END):
        spot = max(0.01, spot * (1 + rng.gauss(0.0002, 0.012)))
        live = [(ls, exp) for ls, exp in contracts if 0 < (exp - d).days <= 270]
        live.sort(key=lambda t: t[1])
        for ls, exp in live[:3]:
            dte = (exp - d).days
            mid = spot * (1 + carry * dte)
            o = mid * (1 + rng.gauss(0, 0.002))
            c = mid * (1 + rng.gauss(0, 0.004))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.003)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.003)))
            vol = int(abs(rng.gauss(5e5, 1e5)) * (1.6 if ls == live[0][0] else 0.5))
            idx.append((pd.Timestamp(d), ls))
            recs.append((round(o, 4), round(h, 4), round(l, 4), round(c, 4), vol, dte))
    mi = pd.MultiIndex.from_tuples(idx, names=["date", "localsymbol"])
    return pd.DataFrame(recs, index=mi, columns=["open", "high", "low", "close", "volume", "dte"])


def main() -> None:
    import arcticdb as adb

    ac = adb.Arctic(URI)
    for libname in ("market_data", "futures"):
        if ac.has_library(libname):
            ac.delete_library(libname)
        ac.create_library(libname)

    md = ac["market_data"]
    for i, (sym, px) in enumerate(STOCKS.items()):
        md.write(sym, stock_ohlcv(seed=100 + i, px=px))
    print(f"market_data: {list(STOCKS)}")

    fut = ac["futures"]
    for i, (root, base) in enumerate(FUT_ROOTS.items()):
        fut.write(root, futures_multiindex(seed=200 + i, base=base, root=root))
    print(f"futures (MultiIndex): {list(FUT_ROOTS)}")
    print(f"done → {URI}")


if __name__ == "__main__":
    main()
