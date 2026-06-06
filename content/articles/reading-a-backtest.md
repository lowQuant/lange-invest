---
title: How to Read One of Our Backtests
date: 2026-05-12
summary: A short guide to the equity curve, drawdown subplot, and the stat tiles on every strategy page.
tags: process, guide
---

Every strategy page carries the same furniture: an equity curve, a drawdown
subplot beneath it, and a row of stat tiles. Here's how to read them.

## The equity curve

The main line is **cumulative growth of $1** following the model's rules, net of
estimated costs. It's plotted on the precomputed snapshot — the public page does no
per-request computation, so it loads instantly regardless of how much history sits
behind it.

## The drawdown subplot

Underneath sits **drawdown**: the percentage decline from the prior peak. This is the
honest part of any backtest — it's where you decide whether you could actually have
held the position.

## The stat tiles

| Tile | Meaning |
|------|---------|
| CAGR | Compound annual growth rate |
| Sharpe | Risk-adjusted return |
| Max DD | Worst peak-to-trough decline |
| Hit rate | Share of winning periods |

Numbers are rendered in a monospaced, tabular figure so columns line up and decimals
align — the same typographic discipline used across the signals and portfolio tables.
