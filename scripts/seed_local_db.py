#!/usr/bin/env python3
"""Seed a LOCAL ArcticDB (LMDB) with sample `futures` + `market_data` libraries.

Dev convenience so the public Database tab is browsable without S3. Writes daily
OHLCV symbols with a DatetimeIndex.

    LANGE_DB_URI=lmdb:///tmp/lange_db python scripts/seed_local_db.py
    # then run the app with the same LANGE_DB_URI
"""
from __future__ import annotations

import math
import os
import random
import sys
from datetime import date, timedelta

import pandas as pd

URI = os.getenv("LANGE_DB_URI", "lmdb:///tmp/lange_db")

FUTURES = ["ES", "NQ", "CL", "GC", "ZN", "6E"]
STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM"]


def ohlcv(seed: int, start_px: float, n: int = 760) -> pd.DataFrame:
    rng = random.Random(seed)
    rows, day, px = [], date(2022, 1, 3), start_px
    for _ in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        o = px
        ret = rng.gauss(0.0003, 0.013)
        c = max(0.01, o * (1 + ret))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
        v = int(abs(rng.gauss(1_000_000, 250_000)))
        rows.append((day, round(o, 2), round(h, 2), round(l, 2), round(c, 2), v))
        px = c
        day += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def main() -> None:
    import arcticdb as adb

    ac = adb.Arctic(URI)
    for libname, syms, base in (("futures", FUTURES, 100.0), ("market_data", STOCKS, 150.0)):
        if ac.has_library(libname):
            ac.delete_library(libname)
        ac.create_library(libname)
        lib = ac[libname]
        for i, sym in enumerate(syms):
            lib.write(sym, ohlcv(seed=hash((libname, sym)) & 0xFFFF, start_px=base + i * 17))
        print(f"seeded {libname}: {syms}")
    print(f"done → {URI}")


if __name__ == "__main__":
    main()
