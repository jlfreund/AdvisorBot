#!/usr/bin/env python3
"""Top-level CLI orchestrator for the Portfolio Tracker & Equity Grant Forecast pipeline.

Supported workflows:
  (A) Refresh    : python3 holdings.py refresh
                   Catch up from last run to today — scans Statements/ for new/updated PDFs,
                   updates non-scrapeable anchors, fetches missing market quotes, updates weekly
                   bars through today (and refreshes future equity grant forecast bars at today's prices),
                   and renders index.html.
  (C) Historical : python3 holdings.py historical --from 2024-01-01 --to present [--include-future 2029-12-28]
                   python3 holdings.py historical --period 2026
                   python3 holdings.py historical --period 2026-10
                   Evaluates and populates weekly historical datapoints over a month, year, or range.
  (D) Future     : python3 holdings.py future --to 2029-12-28 [--tax-mode net|gross]
                   Forecasts weekly holdings forward based on the unvested stock grant release schedule
                   at constant current market prices.
  Offline Render : python3 holdings.py render
  DB Status      : python3 holdings.py status
"""

from __future__ import annotations

import argparse
import calendar
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from config import (  # noqa: E402
    DEFAULT_HTML_REPORT_PATH,
    get_active_portfolio_id,
    get_default_config_path,
    get_default_db_path,
    get_statements_dir,
    load_config,
    validate_portfolio_config,
)
from db.database import PortfolioDB  # noqa: E402
from exporters.html_report import render_html_report  # noqa: E402
from exporters.view_model import build_report_payload  # noqa: E402
from ingest.pipeline import ingest_statements  # noqa: E402
from privacy.registry import PrivacyConfigError  # noqa: E402
from valuation.engine import build_and_store_timeline  # noqa: E402
from valuation.forecast_enricher import enrich_holding_forecasts  # noqa: E402


def _apply_demo_if_active(payload: dict[str, Any], pid: str | None = None, force_demo: bool = False) -> dict[str, Any]:
    try:
        from privacy import apply_demo_projection_to_payload, load_privacy_config

        active_pid = pid or get_active_portfolio_id()
        if active_pid:
            pcfg = load_privacy_config(active_pid, auto_sync=False)
            if (
                force_demo
                or bool(os.environ.get("DEMO_EXPORT"))
                or bool((pcfg.get("demo_mode") or {}).get("enabled"))
            ):
                return apply_demo_projection_to_payload(payload, pcfg)
            return payload
        if force_demo or bool(os.environ.get("DEMO_EXPORT")):
            pcfg = load_privacy_config(None, auto_sync=False)
            return apply_demo_projection_to_payload(payload, pcfg)
    except PrivacyConfigError:
        raise
    except Exception:
        pass
    return payload


def parse_date_arg(val: str, today: date) -> date:
    v = val.strip().lower()
    if v in ("present", "today", "now"):
        return today
    return date.fromisoformat(val.strip())


def parse_period_spec(spec: str, today: date) -> tuple[date, date]:
    """
    Parses flexible `--period` expressions:
      - '2026'                  -> (2026-01-01, min(today, 2026-12-31))
      - '2026-10'               -> (2026-10-01, 2026-10-31)
      - '2024-01-01..present'   -> (2024-01-01, today)
      - '2024..2026'            -> (2024-01-01, min(today, 2026-12-31))
    """
    s = spec.strip()
    if ".." in s:
        left, right = s.split("..", 1)
        d_start = date(int(left), 1, 1) if len(left.strip()) == 4 else parse_date_arg(left, today)
        d_end = (
            min(today, date(int(right), 12, 31))
            if len(right.strip()) == 4 and right.strip().isdigit()
            else parse_date_arg(right, today)
        )
        return d_start, d_end

    if len(s) == 4 and s.isdigit():
        yr = int(s)
        return date(yr, 1, 1), min(today, date(yr, 12, 31))

    if len(s) == 7 and s[4] == "-":
        yr, mo = int(s[:4]), int(s[5:7])
        last_day = calendar.monthrange(yr, mo)[1]
        return date(yr, mo, 1), min(today, date(yr, mo, last_day))

    d = parse_date_arg(s, today)
    return d, today


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="Path to SQLite database (defaults to active portfolio)")
    p.add_argument("--config", type=Path, default=None, help="Path to project.json (defaults to active portfolio)")
    p.add_argument("--statements", type=Path, default=None, help="Path to Statements/ folder (defaults to active portfolio)")
    p.add_argument("--output", type=Path, default=DEFAULT_HTML_REPORT_PATH, help="Path to output index.html")
    p.add_argument("--today", type=str, default=None, help="Override today YYYY-MM-DD (for deterministic testing)")
    p.add_argument("--demo", action="store_true", help="Project report into Demo Mode (Case A scaled & aliased)")


def _resolve_runtime_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None, Path | None]:
    pid = get_active_portfolio_id()
    db_path = args.db if getattr(args, "db", None) is not None else (get_default_db_path(pid) if pid else None)
    cfg_path = args.config if getattr(args, "config", None) is not None else (get_default_config_path(pid) if pid else None)
    stmt_path = args.statements if getattr(args, "statements", None) is not None else (get_statements_dir(pid) if pid else None)
    return db_path, cfg_path, stmt_path


def _run_forecast_enrichment(
    db: PortfolioDB,
    cfg: dict,
    today: date,
    *,
    sync_prices: bool = True,
    force_prices: bool = False,
    price_sync_stats: dict | None = None,
) -> dict:
    errs = (price_sync_stats or {}).get("errors") or []
    failed_syms = {str(e).split(":", 1)[0].strip() for e in errs if ":" in str(e)}
    fc_stats = enrich_holding_forecasts(
        db=db,
        cfg=cfg,
        today_date=today,
        sync_prices=sync_prices,
        force_price_sync=force_prices,
        skip_symbols=failed_syms,
    )
    if fc_stats["symbols_synced_10y"] > 0:
        print(
            f"  [forecast] Backfilled 10Y Yahoo total-return history for {fc_stats['symbols_synced_10y']} symbol(s)"
        )
    if fc_stats["holdings_enriched"] > 0:
        print(
            f"  [forecast] Enriched {fc_stats['holdings_enriched']} holding forecast baseline(s) "
            f"{fc_stats['by_source']}"
        )
    for w in fc_stats.get("low_confidence_warnings") or []:
        print(f"  WARNING: [forecast] {w}")
    return fc_stats


def _is_demo_export_requested(args: argparse.Namespace) -> bool:
    if bool(getattr(args, "demo", False) or os.environ.get("DEMO_EXPORT")):
        return True
    active_pid = getattr(args, "portfolio", None) or get_active_portfolio_id()
    if active_pid:
        try:
            from privacy import load_privacy_config

            pcfg = load_privacy_config(active_pid, auto_sync=False)
            return bool((pcfg.get("demo_mode") or {}).get("enabled"))
        except PrivacyConfigError:
            raise
        except Exception:
            return False
    return False


def _check_portfolio_completeness(cfg: dict, cfg_path: Path | None, command_name: str) -> bool:
    if cfg_path is None:
        return True
    val = validate_portfolio_config(cfg)
    if not val["valid"]:
        errs = "; ".join(val["validation_errors"])
        print(
            f"ERROR: Cannot run '{command_name}' on incomplete portfolio ({cfg_path}): {errs}",
            file=sys.stderr,
        )
        return False
    return True


def cmd_refresh(args: argparse.Namespace) -> int:
    db_path, cfg_path, stmt_path = _resolve_runtime_paths(args)
    today = date.fromisoformat(args.today) if args.today else date.today()
    demo_req = _is_demo_export_requested(args)
    if db_path is None or stmt_path is None:
        cfg = load_config(cfg_path)
        if cfg_path is not None and not _check_portfolio_completeness(cfg, cfg_path, "refresh"):
            return 1
        with PortfolioDB(":memory:") as db:
            payload = build_report_payload(
                db=db,
                cfg=cfg,
                today_date=today,
                initial_preset=args.preset,
                tax_mode=args.tax_mode,
                bypass_export_masking=demo_req,
            )
        payload = _apply_demo_if_active(payload, getattr(args, "portfolio", None), getattr(args, "demo", False))
        out_path = render_html_report(payload, args.output)
        print(f"  [report] No active portfolio selected; wrote empty dashboard {out_path} ({out_path.stat().st_size:,} bytes)")
        return 0

    cfg = load_config(cfg_path)
    if not _check_portfolio_completeness(cfg, cfg_path or db_path, "refresh"):
        return 1
    hist_cfg = cfg.get("historical_defaults", {})
    default_start = date.fromisoformat(hist_cfg.get("default_start_date", "2024-01-01"))
    forecast_end = (
        date.fromisoformat(args.forecast_to)
        if args.forecast_to
        else date.fromisoformat(hist_cfg.get("default_forecast_end_date", "2029-12-28"))
    )

    with PortfolioDB(db_path) as db:
        pre_prices = {
            (r["quote_symbol"], r["price_date"]): round(float(r["close_price"]), 4)
            for r in db.conn.execute("SELECT quote_symbol, price_date, close_price FROM market_prices").fetchall()
        }
        pre_bars = {
            r["bar_date"]: (round(float(r["vested_total"]), 2), round(float(r["projected_release_gross"]), 2))
            for r in db.conn.execute("SELECT bar_date, vested_total, projected_release_gross FROM timeline_bars").fetchall()
        }
        existing_cnt = len(pre_bars)

        run_id = db.start_run("REFRESH", notes=f"Refresh through {today.isoformat()}")
        ing = ingest_statements(stmt_path, db, cfg, run_id=run_id, force_all=args.force_ingest)
        print(
            f"  [ingest] Scanned {ing.files_scanned} statement file(s) in {stmt_path} "
            f"({ing.files_ingested} newly ingested)"
        )
        if ing.files_ingested == 0:
            for m in ing.messages:
                print(f"  [ingest] {m}")

        ev_rows = db.conn.execute(
            "SELECT event_type, summary FROM ingest_events WHERE run_id = ? ORDER BY event_id ASC",
            (run_id,),
        ).fetchall()
        for ev in ev_rows:
            et = ev["event_type"]
            sm = ev["summary"]
            if et in ("FILE_INGESTED", "NEW_POSITION", "GSU_SCHEDULE_INGESTED", "GSU_ACTIVITY_INGESTED"):
                print(f"  [ADD] [ingest] {sm}")
            elif et in ("QTY_CHANGED", "ANCHOR_UPDATED", "POSITION_CLOSED", "POSITION_RENAMED", "POSITION_TRANSFERRED"):
                print(f"  [CHANGE] [ingest] {sm}")
            elif et in ("FILE_SKIPPED", "FILE_PARSE_ERROR"):
                print(f"  WARNING: [ingest] {sm}")
            elif et == "RECORD_DELETED":
                print(f"  [DELETE] [ingest] {sm}")
            else:
                print(f"  [ingest] {sm}")

        # If timeline already exists and no positions/anchors changed, do fast incremental catch-up
        do_incremental = (
            existing_cnt > 0
            and ing.positions_changed == 0
            and ing.anchors_updated == 0
            and not args.force_prices
        )

        tl_stats = build_and_store_timeline(
            db=db,
            cfg=cfg,
            start_date=default_start,
            today_date=today,
            forecast_end_date=forecast_end,
            force_price_sync=args.force_prices,
            incremental_only=do_incremental,
        )
        ps = tl_stats["price_sync"]
        post_prices = {
            (r["quote_symbol"], r["price_date"]): round(float(r["close_price"]), 4)
            for r in db.conn.execute("SELECT quote_symbol, price_date, close_price FROM market_prices").fetchall()
        }
        added_prices = len(set(post_prices) - set(pre_prices))
        changed_prices = sum(1 for k in (set(post_prices) & set(pre_prices)) if post_prices[k] != pre_prices[k])
        deleted_prices = len(set(pre_prices) - set(post_prices))

        print(
            f"  [market] Queried {ps['symbols_queried']} symbol(s) via Yahoo Finance "
            f"({len(post_prices):,} cached price record(s) in market_prices)"
        )
        if added_prices > 0:
            print(f"  [ADD] [market] Added {added_prices:,} new price record(s) to market_prices table")
        if changed_prices > 0:
            print(f"  [market] Refreshed {changed_prices:,} existing price record(s) in market_prices table")
        if deleted_prices > 0:
            print(f"  [DELETE] [market] Removed {deleted_prices:,} price record(s) from market_prices table")
        for err in ps.get("errors") or []:
            print(f"  WARNING: [market] {err}")

        post_bars = {
            r["bar_date"]: (round(float(r["vested_total"]), 2), round(float(r["projected_release_gross"]), 2))
            for r in db.conn.execute("SELECT bar_date, vested_total, projected_release_gross FROM timeline_bars").fetchall()
        }
        added_bars = len(set(post_bars) - set(pre_bars))
        changed_bars = sum(1 for k in (set(post_bars) & set(pre_bars)) if post_bars[k] != pre_bars[k])
        deleted_bars = len(set(pre_bars) - set(post_bars))

        if added_bars > 0:
            print(
                f"  [ADD] [engine] Added {added_bars} new weekly timeline bar(s) to timeline_bars "
                f"({tl_stats['first_bar']} -> {tl_stats['last_bar']})"
            )
        if changed_bars > 0:
            print(
                f"  [engine] Refreshed {changed_bars} existing weekly timeline bar(s) in timeline_bars "
                f"({tl_stats['first_bar']} -> {tl_stats['last_bar']})"
            )
        if deleted_bars > 0:
            print(f"  [DELETE] [engine] Removed {deleted_bars} weekly timeline bar(s) from timeline_bars")
        if added_bars == 0 and changed_bars == 0 and deleted_bars == 0:
            print(
                f"  [engine] Verified {len(post_bars)} weekly timeline bar(s) in timeline_bars "
                f"({tl_stats['first_bar']} -> {tl_stats['last_bar']}, no value changes)"
            )

        _run_forecast_enrichment(
            db, cfg, today, sync_prices=True, force_prices=bool(args.force_prices), price_sync_stats=ps
        )

        payload = build_report_payload(
            db=db,
            cfg=cfg,
            today_date=today,
            initial_preset=args.preset,
            tax_mode=args.tax_mode,
            bypass_export_masking=demo_req,
        )
        payload = _apply_demo_if_active(payload, getattr(args, "portfolio", None), getattr(args, "demo", False))
        out_path = render_html_report(payload, args.output)
        db.finish_run(
            run_id,
            files_scanned=ing.files_scanned,
            files_ingested=ing.files_ingested,
            positions_changed=ing.positions_changed,
            anchors_updated=ing.anchors_updated,
        )
        print(
            f"  [report] Wrote {out_path} ({out_path.stat().st_size:,} bytes) | "
            f"Vested Total: ${payload['kpis']['total_vested']:,.2f} | "
            f"Timeline Bars: {len(payload['timeline'])}"
        )
    return 0


def cmd_historical(args: argparse.Namespace) -> int:
    db_path, cfg_path, stmt_path = _resolve_runtime_paths(args)
    if db_path is None or stmt_path is None:
        print("No active portfolio selected. Select or create a portfolio first.", file=sys.stderr)
        return 1
    cfg = load_config(cfg_path)
    today = date.fromisoformat(args.today) if args.today else date.today()
    hist_cfg = cfg.get("historical_defaults", {})

    if args.period:
        start_dt, end_dt = parse_period_spec(args.period, today)
    else:
        start_dt = (
            parse_date_arg(args.from_date, today)
            if args.from_date
            else date.fromisoformat(hist_cfg.get("default_start_date", "2024-01-01"))
        )
        end_dt = parse_date_arg(args.to_date, today) if args.to_date else today

    forecast_end = date.fromisoformat(args.include_future) if args.include_future else end_dt

    with PortfolioDB(db_path) as db:
        run_id = db.start_run(
            "HISTORICAL",
            notes=f"Historical {start_dt.isoformat()}..{end_dt.isoformat()} (forecast_end={forecast_end.isoformat()})",
        )
        ing = ingest_statements(stmt_path, db, cfg, run_id=run_id, force_all=args.force_ingest)
        for m in ing.messages:
            print(f"  [ingest] {m}")

        tl_stats = build_and_store_timeline(
            db=db,
            cfg=cfg,
            start_date=start_dt,
            today_date=today,
            forecast_end_date=forecast_end,
            force_price_sync=args.force_prices,
            incremental_only=False,
        )
        ps = tl_stats["price_sync"]
        print(
            f"  [market] Queried {ps['symbols_queried']} symbol(s), upserted {ps['bars_upserted']} price row(s)"
            + (f" ({len(ps['errors'])} warning(s))" if ps["errors"] else "")
        )
        print(
            f"  [engine] Built {tl_stats['bars_computed']} weekly bar(s) "
            f"({tl_stats['first_bar']} -> {tl_stats['last_bar']})"
        )

        preset = args.preset or ("all" if forecast_end > today else "historical")
        win_start = None if args.include_future else start_dt
        win_end = forecast_end

        _run_forecast_enrichment(
            db, cfg, today, sync_prices=True, force_prices=bool(args.force_prices), price_sync_stats=ps
        )

        payload = build_report_payload(
            db=db,
            cfg=cfg,
            today_date=today,
            window_start=win_start,
            window_end=win_end,
            initial_preset=preset,
            tax_mode=args.tax_mode,
            bypass_export_masking=_is_demo_export_requested(args),
        )
        payload = _apply_demo_if_active(payload, getattr(args, "portfolio", None), getattr(args, "demo", False))
        out_path = render_html_report(payload, args.output)
        db.finish_run(
            run_id,
            files_scanned=ing.files_scanned,
            files_ingested=ing.files_ingested,
            positions_changed=ing.positions_changed,
            anchors_updated=ing.anchors_updated,
        )
        print(
            f"  [report] Wrote {out_path} ({out_path.stat().st_size:,} bytes) | "
            f"Vested Total: ${payload['kpis']['total_vested']:,.2f} | "
            f"Timeline Bars: {len(payload['timeline'])}"
        )
    return 0


def cmd_future(args: argparse.Namespace) -> int:
    db_path, cfg_path, stmt_path = _resolve_runtime_paths(args)
    if db_path is None or stmt_path is None:
        print("No active portfolio selected. Select or create a portfolio first.", file=sys.stderr)
        return 1
    cfg = load_config(cfg_path)
    today = date.fromisoformat(args.today) if args.today else date.today()
    hist_cfg = cfg.get("historical_defaults", {})
    forecast_end = (
        date.fromisoformat(args.to_date)
        if args.to_date
        else date.fromisoformat(hist_cfg.get("default_forecast_end_date", "2029-12-28"))
    )

    with PortfolioDB(db_path) as db:
        run_id = db.start_run("FUTURE", notes=f"Future forecast through {forecast_end.isoformat()}")
        ing = ingest_statements(stmt_path, db, cfg, run_id=run_id, force_all=False)
        for m in ing.messages:
            print(f"  [ingest] {m}")

        min_bar_row = db.conn.execute("SELECT MIN(bar_date) AS m FROM timeline_bars").fetchone()
        if args.from_date:
            start_dt = parse_date_arg(args.from_date, today)
            incremental = False
        elif min_bar_row and min_bar_row["m"]:
            start_dt = date.fromisoformat(min_bar_row["m"])
            incremental = True
        else:
            start_dt = date.fromisoformat(hist_cfg.get("default_start_date", "2024-01-01"))
            incremental = False

        tl_stats = build_and_store_timeline(
            db=db,
            cfg=cfg,
            start_date=start_dt,
            today_date=today,
            forecast_end_date=forecast_end,
            force_price_sync=False,
            incremental_only=incremental,
        )
        print(
            f"  [engine] Forecast updated ({tl_stats['bars_computed']} bar(s) computed through {forecast_end.isoformat()})"
        )

        _run_forecast_enrichment(
            db, cfg, today, sync_prices=True, force_prices=False, price_sync_stats=tl_stats.get("price_sync")
        )

        payload = build_report_payload(
            db=db,
            cfg=cfg,
            today_date=today,
            initial_preset=args.preset or "future",
            tax_mode=args.tax_mode,
            bypass_export_masking=_is_demo_export_requested(args),
        )
        payload = _apply_demo_if_active(payload, getattr(args, "portfolio", None), getattr(args, "demo", False))
        out_path = render_html_report(payload, args.output)
        db.finish_run(
            run_id,
            files_scanned=ing.files_scanned,
            files_ingested=ing.files_ingested,
            positions_changed=ing.positions_changed,
            anchors_updated=ing.anchors_updated,
        )
        print(
            f"  [report] Wrote {out_path} ({out_path.stat().st_size:,} bytes) | "
            f"Future Gross Releases: ${payload['kpis']['future_gsu_gross_val']:,.2f} | "
            f"Future Net Releases: ${payload['kpis']['future_gsu_net_val']:,.2f}"
        )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    db_path, cfg_path, _ = _resolve_runtime_paths(args)
    cfg = load_config(cfg_path)
    if (cfg_path is not None or db_path is not None) and not _check_portfolio_completeness(cfg, cfg_path or db_path, "render"):
        return 1
    demo_req = _is_demo_export_requested(args)
    today = date.fromisoformat(args.today) if args.today else date.today()
    db_ctx = PortfolioDB(db_path) if db_path is not None else PortfolioDB(":memory:")
    with db_ctx as db:
        _run_forecast_enrichment(db, cfg, today, sync_prices=False)
        payload = build_report_payload(
            db=db,
            cfg=cfg,
            today_date=today,
            initial_preset=args.preset,
            tax_mode=args.tax_mode,
            bypass_export_masking=demo_req,
        )
        payload = _apply_demo_if_active(payload, getattr(args, "portfolio", None), getattr(args, "demo", False))
        out_path = render_html_report(payload, args.output)
        print(f"Rendered {out_path} ({out_path.stat().st_size:,} bytes, {len(payload['timeline'])} bars).")
    return 0


def cmd_ingest_grants(args: argparse.Namespace) -> int:
    import json
    from ingest.plugins.grant_json import ingest_grant_payload_dict

    db_path, cfg_path, _ = _resolve_runtime_paths(args)
    if db_path is None:
        print("No active portfolio selected. Select or create a portfolio first.", file=sys.stderr)
        return 1
    cfg = load_config(cfg_path)
    raw = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    with PortfolioDB(db_path) as db:
        res = ingest_grant_payload_dict(
            db=db,
            cfg=cfg,
            payload=raw,
            reconcile=not args.no_reconcile,
            source_label=str(args.json_file),
        )
        print(
            f"Ingested grants for {', '.join(res['symbols']) or '(none)'} "
            f"({res['tranches_loaded']} tranches, {res['releases_loaded']} releases, "
            f"{res['withdrawals_loaded']} withdrawals, as_of={res['as_of_date']})."
        )
        if args.rebuild:
            today = date.fromisoformat(args.today) if args.today else date.today()
            _run_forecast_enrichment(db, cfg, today, sync_prices=False)
            payload = build_report_payload(db, cfg, today, bypass_export_masking=_is_demo_export_requested(args))
            payload = _apply_demo_if_active(payload, getattr(args, "portfolio", None), getattr(args, "demo", False))
            out_path = render_html_report(payload, args.output)
            print(f"Rebuilt {out_path} ({out_path.stat().st_size:,} bytes).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db_path, _, _ = _resolve_runtime_paths(args)
    if db_path is None or not db_path.exists():
        print("No active portfolio database found.")
        return 1
    with PortfolioDB(db_path) as db:
        acct_cnt = db.conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        file_cnt = db.conn.execute("SELECT COUNT(*) AS c FROM ingested_files").fetchone()["c"]
        live_sec = db.conn.execute("SELECT COUNT(*) AS c FROM securities WHERE is_scrapeable = 1").fetchone()["c"]
        anc_sec = db.conn.execute("SELECT COUNT(*) AS c FROM securities WHERE is_scrapeable = 0").fetchone()["c"]
        gsu_row = db.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(total_qty), 0.0) AS sh FROM gsu_tranches"
        ).fetchone()
        hist_rel_row = db.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(net_shares), 0.0) AS sh FROM gsu_releases_history"
        ).fetchone()
        bar_row = db.conn.execute(
            "SELECT COUNT(*) AS c, MIN(bar_date) AS mn, MAX(bar_date) AS mx FROM timeline_bars"
        ).fetchone()

        print(f"Database         : {db_path}")
        print(f"Accounts         : {acct_cnt}")
        print(f"Ingested Files   : {file_cnt}")
        print(f"Securities       : {live_sec} live-scrapeable tickers | {anc_sec} statement-anchored holdings")
        print(f"Grant Hist. Vests: {hist_rel_row['c']} releases ({hist_rel_row['sh']:,.3f} net shares)")
        print(f"Grant Unvested   : {gsu_row['c']} rows ({gsu_row['sh']:,.3f} unvested shares)")
        print(f"Timeline Bars    : {bar_row['c']} bars ({bar_row['mn']} -> {bar_row['mx']})")

        kinds = db.conn.execute(
            "SELECT bar_kind, COUNT(*) AS c, MIN(bar_date) AS mn, MAX(bar_date) AS mx "
            "FROM timeline_bars GROUP BY bar_kind ORDER BY mn ASC"
        ).fetchall()
        for k in kinds:
            print(f"  - {k['bar_kind']:10s} : {k['c']:3d} bar(s) [{k['mn']} .. {k['mx']}]")
    return 0


def cmd_llm_overview(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    res = llm_tools.tool_get_overview()
    print(json.dumps(res, indent=2))
    return 0


def cmd_llm_positions(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    has_mat = True if args.has_maturity else (False if args.no_maturity else None)
    res = llm_tools.tool_query_positions(
        has_maturity=has_mat,
        account_id=args.account,
        symbol=args.search or args.category,
    )
    print(json.dumps(res, indent=2))
    return 0


def cmd_llm_sim(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    overrides = json.loads(args.overrides) if args.overrides else {}
    if args.gifts:
        overrides["withdrawals"] = json.loads(args.gifts)
    rollovers = json.loads(args.rollovers) if args.rollovers else None
    res = llm_tools.tool_run_simulation(
        base_save=args.base_save,
        use_ref_rates=args.use_reference_rates,
        overrides=overrides or None,
        rollovers=rollovers,
        reoptimize_order=args.reoptimize_order,
        compare_with_baseline=not args.no_baseline,
        apply_non_goog_rate_mode=args.apply_non_goog_rate_mode,
        uma_rate_mode=args.uma_rate_mode,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_llm_non_goog_growth(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    rollovers = json.loads(args.rollovers) if args.rollovers else None
    asset_overrides = json.loads(args.asset_rate_overrides) if args.asset_rate_overrides else None
    res = llm_tools.tool_compute_non_primary_growth(
        rollovers=rollovers,
        uma_rate_mode=args.uma_rate_mode,
        muni_rate_mode=args.muni_rate_mode,
        include_cash=args.include_cash,
        asset_rate_overrides=asset_overrides,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_llm_solve(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    overrides = json.loads(args.overrides) if args.overrides else {}
    if args.gifts:
        overrides["withdrawals"] = json.loads(args.gifts)
    dm = "nominal" if str(args.dollar_mode or "real").lower() == "nominal" else "real"
    if args.milestone:
        ms = str(args.milestone).strip().lower()
        target_metric: str | None = ms if ms.endswith(("_real", "_nominal")) else f"{ms}_{dm}"
    elif dm == "nominal":
        cfg = load_config(get_default_config_path())
        invs = cfg.get("investors") or []
        p_inv = invs[0] if invs and isinstance(invs[0], dict) else {}
        inv_id = str(p_inv.get("id") or "investor_1").strip().lower()
        ms_age = int(p_inv.get("milestone_age") or 85)
        target_metric = f"{inv_id}_{ms_age}_nominal"
    else:
        target_metric = None
    res = llm_tools.tool_solve_simulation(
        solve_for=args.solve_for,
        target_metric=target_metric,
        target_value=args.target_amount,
        base_save=args.base_save,
        use_ref_rates=args.use_reference_rates,
        overrides=overrides or None,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_llm_saves(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    overrides = json.loads(args.overrides) if args.overrides else {}
    if args.gifts:
        overrides["scheduled_actions"] = json.loads(args.gifts)
    act = "load" if args.action == "set_active" else args.action
    res = llm_tools.tool_manage_saves(
        action=act,
        name=args.name,
        id=args.id,
        new_name=args.new_name,
        base_save=args.base_save,
        use_ref_rates=args.use_reference_rates,
        overrides=overrides or None,
        notes_md=args.notes_md,
        model_name=args.model_name,
        reason=args.reason,
        reoptimize_order=args.optimize_order,
        set_active=args.set_active,
        overwrite_existing=args.allow_overwrite,
        apply_to_live_ui=args.push_ui,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_llm_ui(args: argparse.Namespace) -> int:
    import json
    import llm_tools

    if args.load_save:
        res = llm_tools.tool_manage_saves(action="load", name=args.load_save, apply_to_live_ui=True)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    tune = json.loads(args.tune) if args.tune else None
    refresh_act = "primary_quote" if (getattr(args, "refresh_primary_quote", False) or args.refresh_goog) else ("reload_page" if args.reload_page else None)
    res = llm_tools.tool_control_live_ui(
        settings_patch=tune,
        switch_screen=args.tab,
        refresh_action=refresh_act,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def _extract_statement_pages_for_cli(file_path: Path) -> list[str]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        from ingest.pdf_reader import extract_pdf_pages_batch

        return extract_pdf_pages_batch([file_path]).get(file_path, [])
    if suffix in (".xlsx", ".xls"):
        try:
            import openpyxl

            wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
            try:
                sheets_out: list[str] = []
                for name in wb.sheetnames:
                    ws = wb[name]
                    lines = [f"[Sheet: {name}]"]
                    for idx, row in enumerate(ws.iter_rows(values_only=True)):
                        if idx >= 25:
                            break
                        cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                        if cells:
                            lines.append(" | ".join(cells))
                    sheets_out.append("\n".join(lines))
                return sheets_out
            finally:
                wb.close()
        except Exception as exc:
            return [f"[XLSX preview unavailable: {exc}]"]
    try:
        return [file_path.read_text(encoding="utf-8", errors="replace")]
    except Exception as exc:
        return [f"[Binary file preview unavailable: {exc}]"]


def _redact_cli_statement_text(text: str) -> str:
    """Redacts >=7-digit runs and structured account/contract numbers from CLI output."""
    import re

    out = re.sub(r"\b\d{4}[-_]\d{4}\b", "[REDACTED-ACCT]", str(text or ""))
    out = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED-SSN]", out)
    out = re.sub(r"\b[A-Z]{2,4}\d[A-Z0-9]{5,}\b", "[REDACTED-ID]", out)
    out = re.sub(r"\b[A-Z]\d{2}[A-Z]\d{6,}\b", "[REDACTED-CONTRACT]", out)
    out = re.sub(r"(?<!\$)\b\d{7,}\b", "[REDACTED-NUM]", out)
    return out


def cmd_inspect_statement(args: argparse.Namespace) -> int:
    import json
    import re
    from ingest.framework import IngestContext
    from ingest.plugins import build_default_registry
    from ingest.scanner import compute_sha256

    file_path = args.statement_file.expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        print(f"ERROR: Statement file not found: {file_path}", file=sys.stderr)
        return 1

    pages = _extract_statement_pages_for_cli(file_path)
    sha256 = compute_sha256(file_path)
    cfg = load_config(getattr(args, "config", None))
    ctx = IngestContext(
        file_path=file_path,
        rel_path=file_path.name,
        sha256=sha256,
        pages=pages if file_path.suffix.lower() == ".pdf" else None,
        cfg=cfg,
    )
    registry = build_default_registry(
        plugins_dir=args.plugins_dir,
        cfg=cfg,
        allow_custom=True if args.plugins_dir is not None else None,
    )
    scores = registry.detect_all(ctx)
    best_plugin, best_score = registry.detect_plugin(ctx)

    full_text = "\n".join(pages)
    stop_words = {
        "TOTAL", "VALUE", "SHARES", "PRICE", "ACCOUNT", "SUMMARY", "MARKET", "COST", "BASIS",
        "GAIN", "LOSS", "CASH", "SWEEP", "FIXED", "INCOME", "BOND", "BONDS", "EQUITY", "STOCK",
        "PAGE", "DATE", "PERIOD", "BEGIN", "ENDING", "YIELD", "COUPON", "TRUST", "ASSET", "FUND",
    }
    cand_tickers = sorted(
        {
            m.group(0)
            for m in re.finditer(r"\b[A-Z]{2,5}\b", full_text)
            if m.group(0) not in stop_words
        }
    )[:40]
    cand_cusips = sorted(
        {
            m.group(0)
            for m in re.finditer(r"\b[0-9A-Z]{9}\b", full_text)
            if sum(ch.isdigit() for ch in m.group(0)) >= 3 and any(ch.isalpha() for ch in m.group(0))
        }
    )[:25]
    cand_totals = []
    for m in re.finditer(r"\$[0-9]{1,3}(?:,[0-9]{3})*\.\d{2}", full_text):
        tok = m.group(0)
        if tok not in cand_totals:
            cand_totals.append(tok)
        if len(cand_totals) >= 20:
            break

    header_lines: list[str] = []
    for pg_i, pg in enumerate(pages, start=1):
        for ln in pg.splitlines():
            clean = " ".join(ln.split())
            low = clean.lower()
            if len(clean) >= 12 and sum(
                kw in low for kw in ("quantity", "shares", "price", "market value", "cost basis", "symbol", "cusip", "yield", "description")
            ) >= 2:
                entry = f"P{pg_i}: {clean[:120]}"
                if entry not in header_lines:
                    header_lines.append(entry)
            if len(header_lines) >= 15:
                break

    redact = not bool(getattr(args, "unredacted", False))
    disp_headers = [_redact_cli_statement_text(h) for h in header_lines] if redact else header_lines
    disp_cusips = [_redact_cli_statement_text(c) for c in cand_cusips] if redact else cand_cusips
    result = {
        "file": str(file_path) if not redact else file_path.name,
        "redacted": redact,
        "extension": file_path.suffix.lower(),
        "size_bytes": file_path.stat().st_size,
        "sha256": sha256,
        "page_count": len(pages),
        "matched_plugin": best_plugin.plugin_id if (best_plugin and best_score > 0.0) else None,
        "matched_score": round(best_score, 3),
        "plugin_scores": [
            {
                "plugin_id": p.plugin_id,
                "institution": p.institution_name,
                "is_custom": bool(getattr(p, "_is_custom_plugin", False)),
                "score": round(sc, 3),
            }
            for p, sc in scores
        ],
        "candidate_headers": disp_headers,
        "candidate_dollar_amounts": cand_totals,
        "candidate_cusips": disp_cusips,
        "candidate_tickers_sample": cand_tickers,
        "pages_preview": [
            {
                "page": i + 1,
                "text": (_redact_cli_statement_text(pg) if redact else pg)[: args.max_chars_per_page],
            }
            for i, pg in enumerate(pages[: args.max_pages])
        ],
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"=== Statement Inspection: {file_path.name} ===")
    print(f"Path:          {result['file']}" + (" (redacted; pass --unredacted for raw text)" if redact else " (UNREDACTED)"))
    print(f"Pages/Blocks:  {len(pages)}  |  Size: {result['size_bytes']:,} bytes  |  SHA256: {sha256[:16]}...")
    if best_plugin and best_score > 0.0:
        cust_tag = " [custom]" if getattr(best_plugin, "_is_custom_plugin", False) else " [built-in]"
        print(f"Best Match:    {best_plugin.plugin_id}{cust_tag} (score={best_score:.2f}, {best_plugin.institution_name})")
    else:
        print("Best Match:    NONE (score=0.00 — write a custom plugin in Portfolios/<active>/Plugins/)")
    if disp_headers:
        print("\nCandidate Table Headers:")
        for hl in disp_headers[:8]:
            print(f"  • {hl}")
    if cand_totals:
        print(f"\nCandidate Dollar Amounts: {', '.join(cand_totals[:10])}")
    if disp_cusips:
        print(f"Candidate CUSIPs:         {', '.join(disp_cusips[:10])}")
    print("\n--- Page Previews ---")
    for pv in result["pages_preview"]:
        print(f"\n[Page {pv['page']}]")
        print(pv["text"])
    return 0


def cmd_test_plugin(args: argparse.Namespace) -> int:
    import json
    from ingest.classification import validate_risk_class
    from ingest.errors import IngestError
    from ingest.framework import IngestContext
    from ingest.pipeline import apply_parsed_statement
    from ingest.plugins import build_default_registry
    from ingest.scanner import CandidateFile, compute_sha256
    from valuation.forecast_enricher import enrich_holding_forecasts

    file_path = args.statement_file.expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        print(f"ERROR: Statement file not found: {file_path}", file=sys.stderr)
        return 1

    pages = _extract_statement_pages_for_cli(file_path) if file_path.suffix.lower() == ".pdf" else None
    sha256 = compute_sha256(file_path)
    st = file_path.stat()
    cfg = load_config(getattr(args, "config", None))
    ctx = IngestContext(
        file_path=file_path,
        rel_path=file_path.name,
        sha256=sha256,
        pages=pages,
        cfg=cfg,
    )
    registry = build_default_registry(
        plugins_dir=args.plugins_dir,
        cfg=cfg,
        allow_custom=True if args.plugins_dir is not None else None,
    )
    plugin, score = registry.detect_plugin(ctx)
    if plugin is None or score <= 0.0:
        msg = f"FAIL [DETECT]: No registered plugin matched '{file_path.name}' (best score={score:.2f})."
        if args.json:
            print(json.dumps({"ok": False, "stage": "detect", "error": msg}, indent=2))
        else:
            print(msg, file=sys.stderr)
        return 2

    try:
        stmt = plugin.parse(ctx)
    except IngestError as exc:
        if args.json:
            print(json.dumps({"ok": False, "stage": "parse", "code": exc.diagnostic.code, "error": str(exc)}, indent=2))
        else:
            print(f"FAIL [{exc.diagnostic.code}]: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "stage": "parse", "error": str(exc)}, indent=2))
        else:
            print(f"FAIL [PARSE_EXCEPTION]: {exc}", file=sys.stderr)
        return 3

    try:
        for pos in stmt.positions:
            validate_risk_class(
                pos.asset_class,
                pos.risk_class,
                security_id=pos.security_id,
                plugin_id=plugin.plugin_id,
                file_path=file_path,
            )
    except IngestError as exc:
        if args.json:
            print(json.dumps({"ok": False, "stage": "risk_gate", "code": exc.diagnostic.code, "error": str(exc)}, indent=2))
        else:
            print(f"FAIL [{exc.diagnostic.code}]: {exc}", file=sys.stderr)
        return 3

    try:
        chk_ok, computed_total, discrepancy = plugin.verify_checksum(
            stmt,
            ctx,
            strict=not args.no_strict_checksum,
        )
    except IngestError as exc:
        if args.json:
            print(json.dumps({"ok": False, "stage": "checksum", "code": exc.diagnostic.code, "error": str(exc)}, indent=2))
        else:
            print(f"FAIL [{exc.diagnostic.code}]: {exc}", file=sys.stderr)
        return 4

    cand = CandidateFile(
        path=file_path,
        rel_path=file_path.name,
        size=st.st_size,
        birthtime=getattr(st, "st_birthtime", st.st_mtime),
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        sha256=sha256,
        reason="TEST_PLUGIN_DRY_RUN",
    )

    with PortfolioDB(":memory:") as mem_db:
        pos_chg, anc_chg = apply_parsed_statement(mem_db, stmt, cand, run_id=None, cfg=cfg)
        plugin.post_ingest_reconcile(mem_db, cfg)
        enrich_summary = enrich_holding_forecasts(
            mem_db,
            cfg=cfg,
            sync_prices=not args.offline,
        )
        rows = mem_db.conn.execute(
            """
            SELECT s.security_id, s.symbol_or_code AS symbol, s.canonical_name AS name, s.asset_class, s.risk_class,
                   COALESCE(SUM(ps.stmt_market_value), 0.0) AS market_value,
                   b.projected_return_pct, b.realized_vol_pct, b.beta_spy,
                   b.confidence, b.return_source, b.source_detail
            FROM securities s
            LEFT JOIN position_snapshots ps ON ps.security_id = s.security_id
            LEFT JOIN holding_forecasts_baseline b ON b.security_id = s.security_id AND b.account_id = ''
            WHERE s.security_id != 'TICKER:SPY' OR ps.security_id IS NOT NULL
            GROUP BY s.security_id
            ORDER BY market_value DESC, s.security_id ASC
            """
        ).fetchall()
        holdings_out = [dict(r) for r in rows]

    high_cnt = sum(1 for h in holdings_out if h.get("confidence") == "HIGH")
    med_cnt = sum(1 for h in holdings_out if h.get("confidence") == "MEDIUM")
    low_cnt = sum(1 for h in holdings_out if h.get("confidence") == "LOW")
    warnings_list = list(enrich_summary.get("low_confidence_warnings") or [])

    out_payload = {
        "ok": True,
        "plugin_id": plugin.plugin_id,
        "is_custom_plugin": bool(getattr(plugin, "_is_custom_plugin", False)),
        "detection_score": round(score, 3),
        "doc_type": stmt.doc_type,
        "institution": stmt.institution,
        "account_id": stmt.account_id,
        "as_of_date": stmt.as_of_date,
        "reported_total": stmt.reported_total,
        "computed_total": computed_total,
        "discrepancy": discrepancy,
        "checksum_ok": chk_ok,
        "positions_count": len(stmt.positions),
        "gsu_tranches_count": len(stmt.gsu_tranches),
        "position_updates": pos_chg,
        "anchor_updates": anc_chg,
        "enrichment": {
            "total": len(holdings_out),
            "high_confidence": high_cnt,
            "medium_confidence": med_cnt,
            "low_confidence": low_cnt,
            "by_source": enrich_summary.get("by_source") or {},
            "warnings": warnings_list,
        },
        "holdings": holdings_out,
    }

    if args.json:
        print(json.dumps(out_payload, indent=2))
        return 0

    cust_lbl = "custom" if out_payload["is_custom_plugin"] else "built-in"
    print(f"PASS  Plugin: {plugin.plugin_id} ({cust_lbl}, score={score:.2f})  |  As-Of: {stmt.as_of_date}")
    if stmt.reported_total is not None:
        print(
            f"      Checksum: reported=${stmt.reported_total:,.2f} vs computed=${computed_total:,.2f} "
            f"(diff=${discrepancy:+,.2f}) -> {'OK' if chk_ok else 'WARN'}"
        )
    else:
        print(f"      Checksum: reported=None, computed=${computed_total:,.2f} ({len(stmt.gsu_tranches)} grant tranches)")
    print(
        f"      Enrichment: {len(holdings_out)} securities "
        f"(HIGH={high_cnt}, MEDIUM={med_cnt}, LOW={low_cnt})"
    )
    if holdings_out:
        print("\n  " + f"{'SECURITY_ID':<22} {'ASSET_CLASS':<15} {'RISK_CLASS':<19} {'MKT_VALUE':>14} {'PROJ_RET':>9} {'CONF':<6} SOURCE")
        print("  " + "-" * 110)
        for h in holdings_out[:35]:
            sid = str(h["security_id"] or "")[:21]
            ac = str(h["asset_class"] or "")[:14]
            rc = str(h["risk_class"] or "")[:18]
            mv = float(h["market_value"] or 0.0)
            pr = float(h["projected_return_pct"] or 0.0)
            cf = str(h["confidence"] or "—")[:6]
            src = str(h["return_source"] or "—")
            print(f"  {sid:<22} {ac:<15} {rc:<19} ${mv:>13,.2f} {pr:>+8.2f}% {cf:<6} {src}")
        if len(holdings_out) > 35:
            print(f"  ... ({len(holdings_out) - 35} additional holdings omitted; pass --json for full list)")
    for w in warnings_list:
        print(f"  WARNING: {w}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portfolio Tracker Holdings & Forecast Pipeline")
    sub = parser.add_subparsers(dest="command")

    # (A) Refresh
    p_ref = sub.add_parser("refresh", help="(A) Catch up from last run to today & regenerate report")
    add_common_args(p_ref)
    p_ref.add_argument("--forecast-to", type=str, default=None, help="End date for future grant forecast bars (default 2029-12-28)")
    p_ref.add_argument("--preset", type=str, default="all", choices=["all", "past_month", "recent", "ytd", "past_year", "historical", "future"])
    p_ref.add_argument("--tax-mode", type=str, default="net", choices=["net", "gross"])
    p_ref.add_argument("--force-ingest", action="store_true", help="Re-parse all PDFs regardless of watermark")
    p_ref.add_argument("--force-prices", action="store_true", help="Re-fetch full Yahoo price history")
    p_ref.set_defaults(func=cmd_refresh)

    # (C) Historical
    p_hist = sub.add_parser("historical", help="(C) Populate/evaluate historical weekly bars for a period or range")
    add_common_args(p_hist)
    p_hist.add_argument("--period", type=str, default=None, help="e.g. '2026', '2026-10', or '2024-01-01..present'")
    p_hist.add_argument("--from", dest="from_date", type=str, default=None, help="Start date YYYY-MM-DD")
    p_hist.add_argument("--to", dest="to_date", type=str, default="present", help="End date YYYY-MM-DD or 'present'")
    p_hist.add_argument("--include-future", type=str, default=None, help="Also populate future forecast bars up to YYYY-MM-DD")
    p_hist.add_argument("--preset", type=str, default=None, choices=["all", "past_month", "recent", "ytd", "past_year", "historical", "future"])
    p_hist.add_argument("--tax-mode", type=str, default="net", choices=["net", "gross"])
    p_hist.add_argument("--force-ingest", action="store_true")
    p_hist.add_argument("--force-prices", action="store_true")
    p_hist.set_defaults(func=cmd_historical)

    # (D) Future
    p_fut = sub.add_parser("future", help="(D) Forecast future weekly bars from stock grant release schedule at today's prices")
    add_common_args(p_fut)
    p_fut.add_argument("--from", dest="from_date", type=str, default=None, help="Optional historical start YYYY-MM-DD")
    p_fut.add_argument("--to", dest="to_date", type=str, default=None, help="Forecast horizon YYYY-MM-DD (default 2029-12-28)")
    p_fut.add_argument("--preset", type=str, default="future", choices=["all", "past_month", "recent", "ytd", "past_year", "historical", "future"])
    p_fut.add_argument("--tax-mode", type=str, default="net", choices=["net", "gross"])
    p_fut.set_defaults(func=cmd_future)

    # Offline Render
    p_ren = sub.add_parser("render", help="Render index.html directly from SQLite without network calls")
    add_common_args(p_ren)
    p_ren.add_argument("--preset", type=str, default="all", choices=["all", "past_month", "recent", "ytd", "past_year", "historical", "future"])
    p_ren.add_argument("--tax-mode", type=str, default="net", choices=["net", "gross"])
    p_ren.set_defaults(func=cmd_render)

    # Status
    p_st = sub.add_parser("status", help="Show SQLite database summary")
    add_common_args(p_st)
    p_st.set_defaults(func=cmd_status)

    # Custom Stock Grants JSON Ingestion
    p_ig = sub.add_parser("ingest-grants", help="Ingest a canonical stock grants JSON payload into SQLite")
    add_common_args(p_ig)
    p_ig.add_argument("json_file", type=Path, help="Path to canonical *.grants.json file")
    p_ig.add_argument("--no-reconcile", action="store_true", help="Skip historical release/ledger reconciliation")
    p_ig.add_argument("--rebuild", action="store_true", help="Rebuild index.html immediately after ingesting")
    p_ig.set_defaults(func=cmd_ingest_grants)

    # LLM / AI Agent Subcommands
    p_lo = sub.add_parser("llm-overview", help="JSON overview of portfolio KPIs, reference rates, and saves")
    p_lo.set_defaults(func=cmd_llm_overview)

    p_lp = sub.add_parser("llm-positions", help="Query positions & annuities with optional maturity/account filters")
    p_lp.add_argument("--account", type=str, default=None, help="Filter by account ID or name substring")
    p_lp.add_argument("--category", type=str, default=None, help="Filter by category")
    p_lp.add_argument("--has-maturity", action="store_true", help="Only return holdings with a maturity/surrender date")
    p_lp.add_argument("--no-maturity", action="store_true", help="Only return holdings without a maturity date")
    p_lp.add_argument("--search", type=str, default=None, help="Case-insensitive substring search")
    p_lp.add_argument("--sort-by", type=str, default="maturity", choices=["maturity", "value", "gain", "yield"])
    p_lp.set_defaults(func=cmd_llm_positions)

    p_lsim = sub.add_parser("llm-sim", help="Run 40-year retirement simulation with optional overrides, gifts, or rollovers")
    p_lsim.add_argument("--base-save", type=str, default=None, help="Base save name (e.g. 'Default')")
    p_lsim.add_argument("--use-reference-rates", action="store_true", help="Seed rates from Reference Values bar")
    p_lsim.add_argument("--overrides", type=str, default=None, help="JSON dict of setting overrides")
    p_lsim.add_argument("--gifts", type=str, default=None, help="JSON list of scheduled gifts/withdrawals")
    p_lsim.add_argument("--replace-withdrawals", action="store_true", help="Replace existing withdrawals with --gifts")
    p_lsim.add_argument("--rollovers", type=str, default=None, help="JSON list of hypothetical Day-0 trades/rollovers")
    p_lsim.add_argument("--reoptimize-order", action="store_true", help="Re-run tax-optimal withdrawal order optimizer")
    p_lsim.add_argument("--apply-non-primary-rate-mode", "--apply-non-goog-rate-mode", dest="apply_non_goog_rate_mode", type=str, default="none", choices=["none", "shift", "solved"], help="Whether to apply rollover non-primary weighted growth shift/solved rate")
    p_lsim.add_argument("--uma-rate-mode", type=str, default="ytd_statement", choices=["ytd_statement", "matched_window", "full_history", "excluded"], help="Empirical rate mode for Direct-Indexing UMA")
    p_lsim.add_argument("--no-baseline", action="store_true", help="Skip baseline comparison")
    p_lsim.add_argument("--annual-table", action="store_true", help="Include 40-year annual projection table")
    p_lsim.set_defaults(func=cmd_llm_sim)

    p_lng = sub.add_parser("llm-non-primary-growth", aliases=["llm-non-goog-growth"], help="Compute weighted-average non-primary growth rate before and after hypothetical rollovers")
    p_lng.add_argument("--rollovers", type=str, default=None, help="JSON list of hypothetical Day-0 trades/rollovers")
    p_lng.add_argument("--uma-rate-mode", type=str, default="ytd_statement", choices=["ytd_statement", "matched_window", "full_history", "excluded"])
    p_lng.add_argument("--muni-rate-mode", type=str, default="price_cagr", choices=["price_cagr", "coupon_yield"])
    p_lng.add_argument("--include-cash", action="store_true", help="Include cash_sweeps (0.0%%/yr) in denominator")
    p_lng.add_argument("--asset-rate-overrides", type=str, default=None, help="JSON dict of {asset_id: annual_rate_pct}")
    p_lng.set_defaults(func=cmd_llm_non_goog_growth)

    p_lsol = sub.add_parser("llm-solve", help="Goal-seek a parameter to reach a target portfolio value at a milestone")
    p_lsol.add_argument("--solve-for", type=str, default="primary_growth", help="Parameter to solve for ('primary_growth', 'broad_equity', 'all_holdings', forecast_key, 'simAnnualBurn', 'simInflationPct')")
    p_lsol.add_argument("--target-amount", type=float, default=2000000.0, help="Target portfolio value ($)")
    p_lsol.add_argument("--dollar-mode", type=str, default="real", choices=["real", "nominal"])
    p_lsol.add_argument("--milestone", type=str, default=None, help="Target milestone (defaults to primary investor's configured milestone, or '<investor_id>_<age>', 'year_40', 'YYYY-MM')")
    p_lsol.add_argument("--base-save", type=str, default=None, help="Base save name or ID")
    p_lsol.add_argument("--use-reference-rates", action="store_true")
    p_lsol.add_argument("--overrides", type=str, default=None, help="JSON dict of locked setting overrides")
    p_lsol.add_argument("--gifts", type=str, default=None, help="JSON list of scheduled actions/gifts")
    p_lsol.add_argument("--replace-withdrawals", action="store_true")
    p_lsol.add_argument("--rollovers", type=str, default=None, help="JSON list of hypothetical rollovers")
    p_lsol.add_argument("--reoptimize-order", action="store_true")
    p_lsol.set_defaults(func=cmd_llm_solve)

    p_lsav = sub.add_parser("llm-saves", help="List, get, create, activate, rename, upvote, or delete Scenarios")
    p_lsav.add_argument("action", choices=["list", "get", "create", "save", "set_active", "load", "rename", "upvote", "notes", "delete"])
    p_lsav.add_argument("--id", type=str, default=None, help="3-digit Scenario ID (e.g. '001')")
    p_lsav.add_argument("--name", type=str, default=None, help="Scenario friendly name")
    p_lsav.add_argument("--new-name", type=str, default=None, help="New friendly name for action='rename'")
    p_lsav.add_argument("--base-save", type=str, default=None, help="Base Scenario ID or name to clone from")
    p_lsav.add_argument("--use-reference-rates", action="store_true", help="Seed rates from Reference Values")
    p_lsav.add_argument("--overrides", type=str, default=None, help="JSON dict of setting overrides")
    p_lsav.add_argument("--gifts", type=str, default=None, help="JSON list of scheduled actions/gifts")
    p_lsav.add_argument("--notes-md", type=str, default=None, help="Markdown thesis/notes for Scenario")
    p_lsav.add_argument("--model-name", type=str, default=None, help="LLM identifier for action='upvote'")
    p_lsav.add_argument("--reason", type=str, default=None, help="Concurrence rationale for action='upvote'")
    p_lsav.add_argument("--replace-withdrawals", action="store_true")
    p_lsav.add_argument("--no-optimize-order", dest="optimize_order", action="store_false", default=True)
    p_lsav.add_argument("--set-active", action="store_true", help="Mark created Scenario as active")
    p_lsav.add_argument("--allow-overwrite", action="store_true", help="Allow overwriting an existing Scenario")
    p_lsav.add_argument("--no-push-ui", dest="push_ui", action="store_false", default=True)
    p_lsav.set_defaults(func=cmd_llm_saves)

    p_lui = sub.add_parser("llm-ui", help="Push realtime setting changes or commands to the open index.html browser session")
    p_lui.add_argument("--tune", type=str, default=None, help="JSON dict of settings to tune live in browser")
    p_lui.add_argument("--load-save", type=str, default=None, help="Save name to load in browser")
    p_lui.add_argument("--tab", type=str, default=None, help="Screen to switch to (models, portfolio, grants, retirement, income)")
    p_lui.add_argument("--refresh-primary-quote", "--refresh-goog", dest="refresh_goog", action="store_true", help="Trigger live primary equity quote refresh")
    p_lui.add_argument("--reload-page", action="store_true", help="Trigger browser page reload")
    p_lui.set_defaults(func=cmd_llm_ui)

    # Agent Statement Onboarding SDK Subcommands (Phase E)
    p_ins = sub.add_parser("inspect-statement", help="Extract page text, headers, candidate symbols/CUSIPs, and plugin detection scores")
    add_common_args(p_ins)
    p_ins.add_argument("statement_file", type=Path, help="Path to PDF/XLSX/JSON/XML statement file")
    p_ins.add_argument("--plugins-dir", type=Path, default=None, help="Optional custom plugins directory override")
    p_ins.add_argument("--max-pages", type=int, default=6, help="Max pages to preview (default 6)")
    p_ins.add_argument("--max-chars-per-page", type=int, default=2000, help="Max preview chars per page (default 2000)")
    p_ins.add_argument("--unredacted", action="store_true", help="Do not redact >=7-digit runs / account IDs (default redacted)")
    p_ins.add_argument("--json", action="store_true", help="Emit structured JSON output")
    p_ins.set_defaults(func=cmd_inspect_statement)

    p_tp = sub.add_parser("test-plugin", help="Dry-run statement detection, parsing, 7-class risk gate, checksum, and forecast enrichment in :memory:")
    add_common_args(p_tp)
    p_tp.add_argument("statement_file", type=Path, help="Path to statement file to test")
    p_tp.add_argument("--plugins-dir", type=Path, default=None, help="Optional custom plugins directory override")
    p_tp.add_argument("--offline", action="store_true", help="Skip live Yahoo Finance 10Y price sync during dry-run")
    p_tp.add_argument("--no-strict-checksum", action="store_true", help="Warn instead of failing on checksum mismatch > $0.05")
    p_tp.add_argument("--json", action="store_true", help="Emit structured JSON output")
    p_tp.set_defaults(func=cmd_test_plugin)

    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        raw_args = ["refresh"]
    parser = build_parser()
    args = parser.parse_args(raw_args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
