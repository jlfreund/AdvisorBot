#!/usr/bin/env python3
"""Portfolio valuation & timeline engine supporting Refresh, Historical, and Future Forecast modes."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from db.database import PortfolioDB, utc_now_iso
from ingest.account_masking import resolve_target_account_id
from valuation.market_data import sync_exact_dates_for_symbol, sync_market_prices
from valuation.models import (
    PriceBook,
    resolve_scrapeable_quantity,
    value_anchored_holding,
    value_scrapeable_holding,
)


def first_friday_on_or_after(d: date) -> date:
    days_ahead = (4 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def load_price_book(db: PortfolioDB) -> PriceBook:
    rows = db.conn.execute(
        "SELECT quote_symbol, price_date, close_price, adj_close FROM market_prices ORDER BY quote_symbol, price_date ASC"
    ).fetchall()
    return PriceBook(
        [
            (
                r["quote_symbol"],
                r["price_date"],
                float(r["close_price"]),
                float(r["adj_close"]) if r["adj_close"] is not None else None,
            )
            for r in rows
        ]
    )


def _load_portfolio_state(db: PortfolioDB, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    accounts = {
        r["account_id"]: dict(r)
        for r in db.conn.execute("SELECT * FROM accounts ORDER BY display_order ASC, account_id ASC").fetchall()
    }
    securities = {
        r["security_id"]: dict(r)
        for r in db.conn.execute("SELECT * FROM securities").fetchall()
    }

    snapshots_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in db.conn.execute("SELECT * FROM position_snapshots ORDER BY as_of_date ASC").fetchall():
        key = (r["account_id"], r["security_id"])
        snapshots_by_pair.setdefault(key, []).append(dict(r))

    adjustments_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in db.conn.execute("SELECT * FROM position_adjustments ORDER BY effective_date ASC").fetchall():
        key = (r["account_id"], r["security_id"])
        adjustments_by_pair.setdefault(key, []).append(dict(r))

    anchors_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in db.conn.execute("SELECT * FROM valuation_anchors ORDER BY value_date ASC").fetchall():
        key = (r["account_id"], r["security_id"])
        anchors_by_pair.setdefault(key, []).append(dict(r))

    # Latest vesting schedule per equity grant symbol
    latest_sym_rows = db.conn.execute(
        "SELECT symbol, MAX(report_date) AS rd FROM gsu_tranches GROUP BY symbol"
    ).fetchall()
    # DESIGN INTENT (`DA2-16` / `LL-24`): Accept portable `grant_forecast` first while preserving `gsu_forecast` fallback.
    gsu_cfg = (cfg or {}).get("grant_forecast") or (cfg or {}).get("gsu_forecast") or {}
    cfg_prim_sym = str(gsu_cfg.get("symbol") or (cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    latest_gsu_date: str | None = None
    gsu_tranches: list[dict[str, Any]] = []
    for sr in latest_sym_rows:
        sym_u = (sr["symbol"] or cfg_prim_sym).upper()
        rd = sr["rd"]
        if not rd:
            continue
        if latest_gsu_date is None or rd > latest_gsu_date:
            latest_gsu_date = rd
        gsu_tranches.extend(
            dict(r)
            for r in db.conn.execute(
                "SELECT * FROM gsu_tranches WHERE report_date = ? AND symbol = ? ORDER BY vest_date ASC, grant_id ASC",
                (rd, sr["symbol"]),
            ).fetchall()
        )
    gsu_tranches.sort(key=lambda r: (r["vest_date"], r.get("symbol", cfg_prim_sym), r["grant_id"]))

    # Bridge inter-quarter equity vests when the unvested schedule (`latest_gsu_date`)
    # is newer than the latest stock-plan quarterly statement (`as_of_date`).
    primary_gsu_sym = cfg_prim_sym
    if not primary_gsu_sym and latest_sym_rows:
        primary_gsu_sym = str(latest_sym_rows[0]["symbol"] or "").strip().upper()
    primary_gsu_tranches = [t for t in gsu_tranches if (t.get("symbol") or cfg_prim_sym).upper() == primary_gsu_sym] if primary_gsu_sym else []
    primary_gsu_rd = next((sr["rd"] for sr in latest_sym_rows if (sr["symbol"] or cfg_prim_sym).upper() == primary_gsu_sym), None) if primary_gsu_sym else None
    db_p = Path(db.db_path) if str(getattr(db, "db_path", "")) != ":memory:" else None
    cfg_target_acct = resolve_target_account_id(
        gsu_cfg.get("target_vested_account_id") or "",
        accounts.keys(),
        cfg=cfg,
        db_path=db_p,
    )
    # PORTABILITY DO: Identify the stock-plan account via `target_vested_account_id`, `is_stock_plan`, or generic `STOCK_PLAN` account type (`LL-01`, `LL-03`).
    # PORTABILITY DON'T: Match a specific brokerage institution name or hardcode a default grant ticker.
    cfg_accts = (cfg or {}).get("accounts") or {}
    plan_acct = cfg_target_acct or next(
        (
            aid for aid, a in accounts.items()
            if (cfg_accts.get(aid) or {}).get("is_stock_plan")
            or "STOCK_PLAN" in str(a.get("account_type") or "").upper()
            or "STOCK PLAN" in str(a.get("account_type") or "").upper()
            or "STOCK PLAN" in str(a.get("account_name") or "").upper()
        ),
        gsu_cfg.get("target_vested_account_id") or "",
    )
    plan_sec = gsu_cfg.get("target_vested_security_id") or (f"TICKER:{primary_gsu_sym}" if primary_gsu_sym else "")
    net_rate = float(gsu_cfg.get("default_net_retention_rate", 0.5))
    verified_iq_anchors: dict[str, float] = gsu_cfg.get("inter_quarter_share_anchors", {})
    # PORTABILITY DO (SA2-19): Keep `synthesize_monthly_vests` opt-in (`False` by default) so portfolios
    # without monthly-25th vesting schedules never get synthetic inter-statement vest steps injected.
    synthesize_vests = bool(gsu_cfg.get("synthesize_monthly_vests", False))
    vest_day = max(1, min(28, int(gsu_cfg.get("monthly_vest_day", 25))))
    award_label = str(gsu_cfg.get("award_type") or "equity award").strip()

    plan_snaps = snapshots_by_pair.get((plan_acct, plan_sec), [])
    if synthesize_vests and primary_gsu_rd and plan_snaps:
        latest_plan_snap = plan_snaps[-1]
        plan_asof = latest_plan_snap["as_of_date"]
        if plan_asof < primary_gsu_rd and primary_gsu_tranches:
            # First monthly tranche per grant in `gsu_tranches` gives each grant's monthly share rate
            first_tranche_by_grant: dict[str, dict[str, Any]] = {}
            for tr in primary_gsu_tranches:
                first_tranche_by_grant.setdefault(tr["grant_id"], tr)

            # Enumerate monthly vest dates strictly in `(plan_asof, primary_gsu_rd]`
            d_cur = date.fromisoformat(plan_asof)
            d_end = date.fromisoformat(primary_gsu_rd)
            yr, mo = d_cur.year, d_cur.month
            running_qty = float(latest_plan_snap["quantity"])
            pair_adjs = adjustments_by_pair.setdefault((plan_acct, plan_sec), [])

            existing_step_dates = {
                a["effective_date"]
                for a in adjustments_by_pair.get((plan_acct, plan_sec), [])
                if a["adj_type"] == "GSU_RELEASE_STEP"
            }

            while (yr, mo) <= (d_end.year, d_end.month):
                vest_iso = f"{yr:04d}-{mo:02d}-{vest_day:02d}"
                if plan_asof < vest_iso <= primary_gsu_rd and vest_iso not in existing_step_dates:
                    pre_q = running_qty
                    if vest_iso in verified_iq_anchors:
                        running_qty = float(verified_iq_anchors[vest_iso])
                        note = f"Post-statement {award_label} vest on {vest_iso} (+{running_qty - pre_q:,.3f} net sh; verified against plan anchor)"
                    else:
                        gross_mo = sum(
                            float(gt["total_qty"])
                            for gt in first_tranche_by_grant.values()
                            if gt["grant_date"] <= vest_iso
                        )
                        est_net = round(gross_mo * net_rate, 3)
                        running_qty = round(running_qty + est_net, 3)
                        note = f"Estimated inter-statement {award_label} vest on {vest_iso} (+{est_net:,.3f} net sh)"
                    pair_adjs.append(
                        {
                            "account_id": plan_acct,
                            "security_id": plan_sec,
                            "effective_date": vest_iso,
                            "adj_type": "POST_STMT_GSU_VEST",
                            "pre_quantity": pre_q,
                            "post_quantity": running_qty,
                            "notes": note,
                        }
                    )
                mo += 1
                if mo > 12:
                    mo = 1
                    yr += 1

    gsu_releases_history = [
        dict(r)
        for r in db.conn.execute(
            "SELECT * FROM gsu_releases_history ORDER BY vest_date ASC, grant_id ASC, order_number ASC"
        ).fetchall()
    ]
    gsu_withdrawals = [
        dict(r)
        for r in db.conn.execute(
            "SELECT * FROM gsu_withdrawals ORDER BY exec_date ASC, order_number ASC"
        ).fetchall()
    ]

    # Earliest statement date across accounts (to badge OBSERVED vs BACKCAST)
    min_stmt_row = db.conn.execute("SELECT MIN(as_of_date) AS min_dt FROM position_snapshots").fetchone()
    earliest_stmt_iso = min_stmt_row["min_dt"] if min_stmt_row and min_stmt_row["min_dt"] else "9999-12-31"

    return {
        "accounts": accounts,
        "securities": securities,
        "snapshots_by_pair": snapshots_by_pair,
        "adjustments_by_pair": adjustments_by_pair,
        "anchors_by_pair": anchors_by_pair,
        "gsu_report_date": latest_gsu_date,
        "gsu_tranches": gsu_tranches,
        "gsu_releases_history": gsu_releases_history,
        "gsu_withdrawals": gsu_withdrawals,
        "earliest_stmt_iso": earliest_stmt_iso,
    }


def evaluate_portfolio_at_date(
    bar_iso: str,
    today_iso: str,
    state: dict[str, Any],
    price_book: PriceBook,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Evaluates all holdings on `bar_iso`:
      - If `bar_iso <= today_iso`: historical/current valuation; `projected_release_gross = 0.0`.
      - If `bar_iso > today_iso`: constant `today_iso` market prices & anchors; GSU tranches with
        `today_iso < vest_date <= bar_iso` are summed into `projected_release_gross`.
    """
    hist_cfg = cfg.get("historical_defaults", {})
    backcast_ok = bool(hist_cfg.get("backcast_before_earliest_statement", True))
    interp_ok = bool(hist_cfg.get("interpolate_between_anchors", True))

    is_future = bar_iso > today_iso
    eval_date_for_holdings = today_iso if is_future else bar_iso

    securities = state["securities"]
    snapshots_by_pair = state["snapshots_by_pair"]
    adjustments_by_pair = state["adjustments_by_pair"]
    anchors_by_pair = state["anchors_by_pair"]

    bucket_totals: dict[str, float] = {}
    account_totals: dict[str, float] = {}
    asset_totals: dict[str, float] = {}
    item_details: list[dict[str, Any]] = []
    vested_total = 0.0

    all_pairs = sorted(set(snapshots_by_pair.keys()) | set(anchors_by_pair.keys()))

    for acct_id, sec_id in all_pairs:
        sec = securities.get(sec_id)
        if not sec:
            continue
        snaps = snapshots_by_pair.get((acct_id, sec_id), [])
        adjs = adjustments_by_pair.get((acct_id, sec_id), [])
        ancs = anchors_by_pair.get((acct_id, sec_id), [])

        if sec["is_scrapeable"]:
            qty, ref_snap = resolve_scrapeable_quantity(
                eval_date_for_holdings,
                snaps,
                adjs,
                backcast_before_earliest=backcast_ok,
            )
            if qty <= 0 or ref_snap is None:
                continue
            val, unit_pr, prov = value_scrapeable_holding(
                sec["quote_symbol"],
                qty,
                ref_snap,
                eval_date_for_holdings,
                price_book,
            )
        else:
            qty = snaps[-1]["quantity"] if snaps else 1.0
            val, prov = value_anchored_holding(
                eval_date_for_holdings,
                today_iso,
                ancs,
                snaps,
                interpolate_between_anchors=interp_ok,
            )
            unit_pr = val / qty if qty else val
            ref_snap = snaps[-1] if snaps else None

        if val == 0 or (val < 0 and sec["asset_class"] != "CASH"):
            continue

        vested_total += val
        b_key = sec["stack_bucket"]
        a_cls = sec["asset_class"]
        bucket_totals[b_key] = round(bucket_totals.get(b_key, 0.0) + val, 2)
        account_totals[acct_id] = round(account_totals.get(acct_id, 0.0) + val, 2)
        asset_totals[a_cls] = round(asset_totals.get(a_cls, 0.0) + val, 2)

        item_details.append(
            {
                "account_id": acct_id,
                "security_id": sec_id,
                "symbol_or_code": sec["symbol_or_code"],
                "canonical_name": sec["canonical_name"],
                "asset_class": a_cls,
                "stack_bucket": b_key,
                "is_scrapeable": bool(sec["is_scrapeable"]),
                "quote_symbol": sec["quote_symbol"],
                "quantity": qty,
                "unit_price": unit_pr,
                "market_value": val,
                "provenance": prov,
                "ref_snapshot": ref_snap,
                "latest_anchor": ancs[-1] if ancs else None,
                "security_meta": json.loads(sec["metadata_json"] or "{}"),
            }
        )

    # Projected Stock Releases: STRICTLY 0 on past/today bars; stacked ONLY on future bars once vest_date <= bar_iso!
    proj_rel_shares = 0.0
    proj_rel_gross = 0.0
    if is_future and state["gsu_tranches"]:
        live_px_by_sym: dict[str, float] = {}
        fallback_rel_px: dict[str, float] = {}
        for r in state.get("gsu_releases_history") or []:
            rsym = (r.get("symbol") or "").strip().upper()
            if rsym and float(r.get("release_price") or 0.0) > 0:
                fallback_rel_px[rsym] = float(r["release_price"])
        for tr in state["gsu_tranches"]:
            if today_iso < tr["vest_date"] <= bar_iso:
                sym = (tr.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                if sym not in live_px_by_sym:
                    px = price_book.price_on_or_before(sym, today_iso)
                    if px is None or px <= 0:
                        px = fallback_rel_px.get(sym, 0.0)
                    live_px_by_sym[sym] = float(px)
                qty = float(tr["total_qty"])
                proj_rel_shares += qty
                proj_rel_gross += qty * live_px_by_sym[sym]
        proj_rel_shares = round(proj_rel_shares, 6)
        proj_rel_gross = round(proj_rel_gross, 2)

    if is_future:
        bar_kind = "FORECAST"
    elif bar_iso == today_iso:
        bar_kind = "TODAY"
    elif bar_iso >= state["earliest_stmt_iso"]:
        bar_kind = "OBSERVED"
    else:
        bar_kind = "BACKCAST"

    return {
        "bar_date": bar_iso,
        "bar_kind": bar_kind,
        "vested_total": round(vested_total, 2),
        "projected_release_gross": proj_rel_gross,
        "projected_release_shares": proj_rel_shares,
        "bucket_totals": bucket_totals,
        "account_totals": account_totals,
        "asset_totals": asset_totals,
        "items": item_details,
    }


def build_and_store_timeline(
    db: PortfolioDB,
    cfg: dict[str, Any],
    start_date: date,
    today_date: date,
    forecast_end_date: date | None = None,
    force_price_sync: bool = False,
    incremental_only: bool = False,
) -> dict[str, Any]:
    """
    Syncs required Yahoo Finance market prices and materializes weekly `timeline_bars` into SQLite.
    - When `incremental_only=True` (Mode A Refresh), only computes missing weeks since the last stored
      non-forecast bar up to `today_date`, plus refreshes all future `FORECAST` bars at today's new prices!
    - When `incremental_only=False` (Mode C Historical / full seed), computes all Friday bars across
      `[start_date .. forecast_end_date]`.
    """
    state = _load_portfolio_state(db, cfg)
    prim_sym = str((cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    grant_syms = {
        (t.get("symbol") or prim_sym).strip().upper()
        for t in state.get("gsu_tranches") or []
        if (t.get("symbol") or prim_sym).strip()
    }
    base_syms = {"SPY"}
    if prim_sym:
        base_syms.add(prim_sym)
    scrapeable_syms = sorted(
        {
            s["quote_symbol"]
            for s in state["securities"].values()
            if s["is_scrapeable"] and s["quote_symbol"]
        }
        | grant_syms
        | base_syms
    )

    hist_end = min(today_date, forecast_end_date) if forecast_end_date else today_date
    try:
        five_yrs_ago_date = today_date.replace(year=today_date.year - 5)
    except ValueError:
        five_yrs_ago_date = today_date.replace(year=today_date.year - 5, day=28)
    # PORTABILITY INVARIANT (SA3-13): Derive price-sync start from statement `start_date` and trailing 5Y window
    # instead of hardcoding household-era literal dates (`2023-01-01` / `2023-03-01`).
    price_sync_start = min(start_date, five_yrs_ago_date)
    price_sync_stats = sync_market_prices(
        db,
        symbols=scrapeable_syms,
        start_date=price_sync_start,
        end_date=today_date,
        force_full=force_price_sync,
    )
    five_yrs_ago_iso = five_yrs_ago_date.isoformat()
    dates_by_sym: dict[str, set[str]] = {}
    if prim_sym:
        dates_by_sym[prim_sym] = {start_date.isoformat(), five_yrs_ago_iso}
    for t in state["gsu_tranches"]:
        sym = (t.get("symbol") or prim_sym).strip().upper()
        if sym:
            dates_by_sym.setdefault(sym, {five_yrs_ago_iso}).add(t["grant_date"])
    for sym, dt_set in sorted(dates_by_sym.items()):
        sync_exact_dates_for_symbol(db, sym, sorted(dt_set))

    price_book = load_price_book(db)
    today_iso = today_date.isoformat()

    # Determine start of bar recomputation
    calc_start = start_date
    if incremental_only:
        row = db.conn.execute(
            "SELECT MAX(bar_date) AS last_hist FROM timeline_bars WHERE bar_kind IN ('OBSERVED', 'BACKCAST')"
        ).fetchone()
        if row and row["last_hist"]:
            # Recompute from the last historical Friday onward (and replace prior TODAY / FORECAST bars)
            calc_start = max(start_date, date.fromisoformat(row["last_hist"]))
            db.conn.execute("DELETE FROM timeline_bars WHERE bar_date >= ?", (calc_start.isoformat(),))

    bar_dates: list[date] = []
    cur_fri = first_friday_on_or_after(calc_start)
    while cur_fri <= hist_end:
        bar_dates.append(cur_fri)
        cur_fri += timedelta(days=7)

    if calc_start <= today_date <= (forecast_end_date or today_date):
        if not bar_dates or bar_dates[-1] != today_date:
            bar_dates.append(today_date)

    if forecast_end_date and forecast_end_date > today_date:
        # Remove stale forecast bars before regenerating at today's latest market prices
        db.conn.execute("DELETE FROM timeline_bars WHERE bar_date > ?", (today_iso,))
        next_fri = first_friday_on_or_after(today_date + timedelta(days=1))
        while next_fri <= forecast_end_date:
            bar_dates.append(next_fri)
            next_fri += timedelta(days=7)

    now_iso = utc_now_iso()
    for b_dt in bar_dates:
        res = evaluate_portfolio_at_date(b_dt.isoformat(), today_iso, state, price_book, cfg)
        db.conn.execute(
            """
            INSERT INTO timeline_bars
                (bar_date, bar_kind, vested_total, projected_release_gross, projected_release_shares,
                 bucket_totals_json, account_totals_json, asset_totals_json, computed_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bar_date) DO UPDATE SET
                bar_kind = excluded.bar_kind,
                vested_total = excluded.vested_total,
                projected_release_gross = excluded.projected_release_gross,
                projected_release_shares = excluded.projected_release_shares,
                bucket_totals_json = excluded.bucket_totals_json,
                account_totals_json = excluded.account_totals_json,
                asset_totals_json = excluded.asset_totals_json,
                computed_at_iso = excluded.computed_at_iso
            """,
            (
                res["bar_date"],
                res["bar_kind"],
                res["vested_total"],
                res["projected_release_gross"],
                res["projected_release_shares"],
                json.dumps(res["bucket_totals"]),
                json.dumps(res["account_totals"]),
                json.dumps(res["asset_totals"]),
                now_iso,
            ),
        )

    db.conn.commit()
    return {
        "bars_computed": len(bar_dates),
        "first_bar": bar_dates[0].isoformat() if bar_dates else None,
        "last_bar": bar_dates[-1].isoformat() if bar_dates else None,
        "price_sync": price_sync_stats,
    }
