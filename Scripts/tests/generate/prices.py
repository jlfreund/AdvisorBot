#!/usr/bin/env python3
"""
Deterministic synthetic price history for fixture portfolios.

Fixtures must never reach the network, and snapshots must not move between runs, so each
symbol's history is a geometric random walk seeded from `(spec.seed, symbol)` using
Python's own Mersenne Twister. Same spec in, byte-identical prices out, on any machine
and any Python 3.11+.

Weekly Friday closes are emitted (what `market_prices` stores for the timeline) from the
symbol's `start` date through the spec's `as_of`, plus the exact statement dates so a
statement's reported value always has a price to reconcile against.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta
from typing import Any

WEEK = timedelta(days=7)


def symbol_seed(spec_seed: int, symbol: str) -> int:
    """A stable per-symbol seed, independent of dict ordering."""
    digest = hashlib.sha256(f"{int(spec_seed)}:{symbol.upper()}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def to_friday(day: date) -> date:
    """The Friday of `day`'s week (matches `market_data.to_friday`)."""
    return day + timedelta(days=(4 - day.weekday()) % 7 - (7 if day.weekday() > 4 else 0))


def fridays(start: date, end: date) -> list[date]:
    out: list[date] = []
    cursor = start + timedelta(days=(4 - start.weekday()) % 7)
    while cursor <= end:
        out.append(cursor)
        cursor += WEEK
    return out


def build_series(
    symbol: str,
    plan: dict[str, Any],
    *,
    spec_seed: int,
    as_of: date,
    extra_dates: list[date] | None = None,
) -> dict[str, float]:
    """
    Builds `{"YYYY-MM-DD": close}` for one symbol.

    `plan` fields: `p0` (opening close), `start` (first date), `mu` (annual drift) and
    `sigma` (annual volatility). A `sigma` of 0 gives a smooth compounding path, which is
    what the deterministic-engine fixtures want.
    """
    p0 = float(plan.get("p0", 100.0))
    start = date.fromisoformat(str(plan.get("start") or f"{as_of.year - 7}-01-02"))
    mu = float(plan.get("mu", 0.07))
    sigma = float(plan.get("sigma", 0.0))

    dates = sorted({*fridays(start, as_of), *(extra_dates or [])})
    if not dates:
        dates = [as_of]
    rng = random.Random(symbol_seed(spec_seed, symbol))

    dt = 1.0 / 52.0
    drift = (mu - 0.5 * sigma * sigma) * dt
    shock = sigma * math.sqrt(dt)

    series: dict[str, float] = {}
    price = p0
    prev = dates[0]
    for idx, day in enumerate(dates):
        if idx > 0:
            steps = max(1, round((day - prev).days / 7))
            for _ in range(steps):
                price *= math.exp(drift + shock * rng.gauss(0.0, 1.0))
            prev = day
        series[day.isoformat()] = round(price, 4)
    return series


def price_on(series: dict[str, float], day: date) -> float:
    """Most recent close on or before `day` (falls forward to the first close if earlier)."""
    want = day.isoformat()
    best: float | None = None
    for iso in sorted(series):
        if iso <= want:
            best = series[iso]
        else:
            break
    if best is None:
        best = series[sorted(series)[0]]
    return float(best)
