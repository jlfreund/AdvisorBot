#!/usr/bin/env python3
"""Pluggable valuation models for Live Tickers, Statement-Anchored Holdings, and GSU Forecasts."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from typing import Any


class PriceBook:
    """Fast in-memory sorted lookup over cached `market_prices` rows."""

    def __init__(self, rows: list[tuple[Any, ...]]):
        self._dates: dict[str, list[str]] = {}
        self._prices: dict[str, list[float]] = {}
        self._adj_prices: dict[str, list[float]] = {}
        for row in rows:
            sym = str(row[0])
            dt_s = str(row[1])
            pr = float(row[2])
            adj = float(row[3]) if len(row) > 3 and row[3] is not None else pr
            self._dates.setdefault(sym, []).append(dt_s)
            self._prices.setdefault(sym, []).append(pr)
            self._adj_prices.setdefault(sym, []).append(adj)

    def price_on_or_before(self, quote_symbol: str, target_iso: str) -> float | None:
        dts = self._dates.get(quote_symbol)
        if not dts:
            return None
        idx = bisect_right(dts, target_iso) - 1
        if idx >= 0:
            return self._prices[quote_symbol][idx]
        return None

    def price_with_date_on_or_before(self, quote_symbol: str, target_iso: str) -> tuple[str, float] | None:
        dts = self._dates.get(quote_symbol)
        if not dts:
            return None
        idx = bisect_right(dts, target_iso) - 1
        if idx >= 0:
            return dts[idx], self._prices[quote_symbol][idx]
        return None

    def adj_price_with_date_on_or_before(self, quote_symbol: str, target_iso: str) -> tuple[str, float] | None:
        dts = self._dates.get(quote_symbol)
        if not dts:
            return None
        idx = bisect_right(dts, target_iso) - 1
        if idx >= 0:
            return dts[idx], self._adj_prices[quote_symbol][idx]
        return None

    def earliest_price(self, quote_symbol: str) -> tuple[str, float] | None:
        dts = self._dates.get(quote_symbol)
        if not dts:
            return None
        return dts[0], self._prices[quote_symbol][0]

    def earliest_adj_price(self, quote_symbol: str) -> tuple[str, float] | None:
        dts = self._dates.get(quote_symbol)
        if not dts:
            return None
        return dts[0], self._adj_prices[quote_symbol][0]


def resolve_scrapeable_quantity(
    bar_iso: str,
    snapshots: list[dict[str, Any]],
    adjustments: list[dict[str, Any]],
    backcast_before_earliest: bool = True,
) -> tuple[float, dict[str, Any] | None]:
    """
    Determines share count for `(account_id, security_id)` at `bar_iso` (where `bar_iso <= today_iso`),
    respecting point-in-time statement snapshots, closed status, pre-split adjustments, and intra-quarter releases.
    """
    if not snapshots:
        return 0.0, None

    # Snapshots are sorted ascending by `as_of_date`
    chosen = None
    for snap in snapshots:
        if snap["as_of_date"] <= bar_iso:
            chosen = snap
        else:
            break

    if chosen is not None:
        if chosen["status"] == "CLOSED":
            return 0.0, chosen
        qty = float(chosen["quantity"])
        post_steps = sorted(
            [
                a for a in adjustments
                if a["adj_type"] in ("GSU_RELEASE_STEP", "POST_STMT_GSU_VEST")
                and chosen["as_of_date"] < a["effective_date"] <= bar_iso
            ],
            key=lambda x: x["effective_date"],
        )
        if post_steps:
            qty = float(post_steps[-1]["post_quantity"])
        return qty, chosen

    if not backcast_before_earliest:
        return 0.0, None

    # `bar_iso` precedes the earliest ingested statement for this security
    earliest = snapshots[0]
    if earliest["status"] == "CLOSED":
        return 0.0, earliest

    qty = float(earliest["quantity"])

    # Apply adjustments whose `effective_date > bar_iso` in reverse chronological order:
    #   - PRE_SPLIT_SHARES (restores pre-split share count prior to reverse/forward split date)
    #   - GSU_RELEASE_STEP (restores pre-vest share count prior to each release step date)
    gsu_steps = [a for a in adjustments if a["adj_type"] == "GSU_RELEASE_STEP"]
    if gsu_steps:
        # Find earliest step > bar_iso -> use its pre_quantity; else last step's post_quantity
        gsu_steps_sorted = sorted(gsu_steps, key=lambda x: x["effective_date"])
        matched_step = False
        for st in gsu_steps_sorted:
            if bar_iso < st["effective_date"]:
                qty = float(st["pre_quantity"])
                matched_step = True
                break
        if not matched_step:
            qty = float(gsu_steps_sorted[-1]["post_quantity"])

    split_adjs = sorted(
        [a for a in adjustments if a["adj_type"] == "PRE_SPLIT_SHARES"],
        key=lambda x: x["effective_date"],
        reverse=True,
    )
    for sp in split_adjs:
        if bar_iso < sp["effective_date"]:
            qty = float(sp["pre_quantity"])

    return qty, earliest


def value_scrapeable_holding(
    quote_symbol: str,
    qty: float,
    ref_snap: dict[str, Any],
    eval_price_iso: str,
    price_book: PriceBook,
) -> tuple[float, float, str]:
    """
    Computes `(market_value, unit_price_used, provenance)` for a scrapeable ticker.
    If a recent spin-off/IPO had not yet begun trading on `eval_price_iso`, smoothly scales
    the statement value by `SPY` so historical UMA bars don't suffer pre-spinoff dropouts.
    """
    if qty <= 0:
        return 0.0, 0.0, "CLOSED"

    pr = price_book.price_on_or_before(quote_symbol, eval_price_iso)
    if pr is not None:
        return round(qty * pr, 2), pr, "LIVE_MARKET"

    # Pre-IPO / pre-spinoff fallback using SPY ratio relative to statement date
    stmt_dt = ref_snap["as_of_date"]
    stmt_mv = float(ref_snap["stmt_market_value"])
    spy_then = price_book.price_on_or_before("SPY", eval_price_iso)
    spy_stmt = price_book.price_on_or_before("SPY", stmt_dt)
    if spy_then and spy_stmt and spy_stmt > 0:
        est_mv = round(stmt_mv * (spy_then / spy_stmt), 2)
        return est_mv, round(est_mv / qty, 4), "PRE_SPINOFF_SPY_PROXY"

    stmt_pr = float(ref_snap["stmt_price"] or 0.0)
    return round(stmt_mv, 2), stmt_pr, "STATEMENT_FALLBACK"


def value_anchored_holding(
    bar_iso: str,
    today_iso: str,
    anchors: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    interpolate_between_anchors: bool = True,
) -> tuple[float, str]:
    """
    Values a non-scrapeable holding (annuity, municipal bond, cash sweep, or private contract)
    purely from its database-copied `valuation_anchors`:
      - If closed in `position_snapshots` on or before `bar_iso`, returns 0.0.
      - On `bar_iso >= latest_anchor.value_date` (including Today and all Future Forecast bars),
        carries forward the exact latest statement-copied `market_value`.
      - Between two historical statement anchors `a1 <= bar_iso <= a2`, interpolates smoothly.
      - Before `earliest_anchor.value_date` (back-cast history when older PDFs aren't yet on disk),
        uses `(issue_date, initial_premium)` compound curve if reported on the contract (e.g. annuities),
        otherwise carries `earliest_anchor.market_value`.
    """
    if not anchors:
        return 0.0, "NO_ANCHOR"

    # Check if position was closed on a newer statement on or before `min(bar_iso, today_iso)`
    eff_check_iso = min(bar_iso, today_iso)
    latest_snap = None
    for s in snapshots:
        if s["as_of_date"] <= eff_check_iso:
            latest_snap = s
        else:
            break
    if latest_snap is not None and latest_snap["status"] == "CLOSED":
        return 0.0, "CLOSED"

    # Anchors sorted ascending by `value_date`
    if bar_iso >= anchors[-1]["value_date"]:
        return round(float(anchors[-1]["market_value"]), 2), "DB_ANCHOR"

    if bar_iso < anchors[0]["value_date"]:
        first = anchors[0]
        v0 = float(first["market_value"])
        issue_iso = first.get("issue_date")
        prem = first.get("initial_premium")
        if issue_iso and prem and float(prem) > 0 and issue_iso < first["value_date"]:
            d_bar = date.fromisoformat(bar_iso)
            d_iss = date.fromisoformat(issue_iso)
            d_anc = date.fromisoformat(first["value_date"])
            if d_bar <= d_iss:
                # Prior to contract issue date, hold initial premium principal
                return round(float(prem), 2), "PRE_ISSUE_PRINCIPAL"
            total_days = (d_anc - d_iss).days
            elapsed = (d_bar - d_iss).days
            if total_days > 0:
                # Smooth exponential interpolation from initial premium at issue_date to exact observed anchor
                ratio = v0 / float(prem)
                val = float(prem) * (ratio ** (elapsed / total_days))
                return round(val, 2), "CONTRACT_CURVE_BACKCAST"
        return round(v0, 2), "EARLIEST_ANCHOR_BACKCAST"

    # Bracketed between two anchors `a_prev <= bar_iso < a_next`
    a_prev = anchors[0]
    a_next = anchors[-1]
    for i in range(len(anchors) - 1):
        if anchors[i]["value_date"] <= bar_iso < anchors[i + 1]["value_date"]:
            a_prev = anchors[i]
            a_next = anchors[i + 1]
            break

    if not interpolate_between_anchors or a_prev["value_date"] == a_next["value_date"]:
        return round(float(a_prev["market_value"]), 2), "DB_ANCHOR"

    d1 = date.fromisoformat(a_prev["value_date"])
    d2 = date.fromisoformat(a_next["value_date"])
    db_dt = date.fromisoformat(bar_iso)
    span = (d2 - d1).days
    if span <= 0:
        return round(float(a_prev["market_value"]), 2), "DB_ANCHOR"
    frac = (db_dt - d1).days / span
    v = float(a_prev["market_value"]) + frac * (float(a_next["market_value"]) - float(a_prev["market_value"]))
    return round(v, 2), "INTERPOLATED_ANCHOR"
