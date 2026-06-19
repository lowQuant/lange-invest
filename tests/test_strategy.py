"""Unit tests for the combined-universe strategy signal + the compute cache.

These exercise the pure, data-only pieces (no ArcticDB required): the Donchian
state machine with the optional trend gate, the spread firing count, and the
disk-cache validity rule.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app import futures_overview as fo
from app import strategy_cache


def _series(values):
    idx = pd.date_range("2018-01-01", periods=len(values), freq="B")
    return pd.Series([float(v) for v in values], index=idx)


# ── signal: trend gate on/off ────────────────────────────────────────────────

def test_signal_enters_long_on_breakout_without_trend_gate():
    # Flat for the channel warm-up, then a clean new 50-day high.
    vals = [100.0] * 60 + [101.0, 102.0, 103.0]
    target = fo._strategy_signal(_series(vals), use_trend=False)
    assert target[-1] == 1  # holding long after the breakout


def test_trend_gate_blocks_entry_below_sma():
    # A long downtrend, then a recovery that prints a fresh 50-day high while
    # still well below the 200-day SMA: trend-off goes long, trend-on must not.
    down = list(np.linspace(300, 150, 200))
    up = list(np.linspace(151, 175, 60))  # rallies to a new 50-day high (175)
    s = _series(down + up)
    # Sanity: the breakout level is genuinely below the long-run mean.
    assert s.iloc[-1] < s.tail(fo.STRAT_SMA).mean()
    off = fo._strategy_signal(s, use_trend=False)
    on = fo._strategy_signal(s, use_trend=True)
    assert off[-1] == 1
    assert on[-1] == 0


def test_signal_short_warmup_returns_flat():
    # Fewer bars than the entry channel needs → all flat, never crashes.
    target = fo._strategy_signal(_series([100.0] * 10), use_trend=False)
    assert target.tolist() == [0] * 10


# ── firing count ─────────────────────────────────────────────────────────────

def test_count_firings_counts_fresh_entries_and_flips():
    # 0→long (1), long→0 (no), 0→short (1), short→long (1) = 3 firings.
    target = np.array([0, 0, 1, 1, 0, -1, -1, 1], dtype=np.int8)
    assert fo._count_firings(target) == 3


def test_count_firings_empty():
    assert fo._count_firings(np.array([], dtype=np.int8)) == 0


# ── states_both ──────────────────────────────────────────────────────────────

def test_states_both_returns_both_variants_and_firings():
    vals = [100.0] * 60 + [101.0, 102.0, 103.0]
    out = fo._states_both(_series(vals))
    assert set(out) == {"notrend", "trend", "firings"}
    assert out["notrend"]["dir"] == 1
    assert isinstance(out["firings"], int)


# ── disk cache validity ──────────────────────────────────────────────────────

@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(strategy_cache, "_CACHE_DIR", tmp_path)
    return tmp_path


def test_cache_computes_then_reuses_on_matching_fingerprint(cache_dir):
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"instruments": [{"id": "CL"}]}

    first = strategy_cache.get_or_compute("u", "fp1", compute)
    assert first["source"] == "computed"
    assert calls["n"] == 1

    second = strategy_cache.get_or_compute("u", "fp1", compute)
    assert second["source"] == "cache"
    assert calls["n"] == 1  # not recomputed
    assert second["instruments"] == [{"id": "CL"}]


def test_cache_recomputes_on_new_data(cache_dir):
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"instruments": []}

    strategy_cache.get_or_compute("u", "fp1", compute)
    out = strategy_cache.get_or_compute("u", "fp2", compute)  # fingerprint changed
    assert out["source"] == "computed"
    assert calls["n"] == 2


def test_cache_force_bypasses(cache_dir):
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"instruments": []}

    strategy_cache.get_or_compute("u", "fp1", compute)
    strategy_cache.get_or_compute("u", "fp1", compute, force=True)
    assert calls["n"] == 2


def test_cache_same_day_reuse_when_fingerprint_unavailable(cache_dir):
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"instruments": []}

    strategy_cache.get_or_compute("u", "fp1", compute)
    # Fingerprint can't be computed (engine hiccup) but it was computed today.
    out = strategy_cache.get_or_compute("u", None, compute)
    assert out["source"] == "cache"
    assert calls["n"] == 1
