#!/usr/bin/env python3
"""Statement ingestion coordinator: chronological ordering, position diffing, and DB anchor updates."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db.database import PortfolioDB, utc_now_iso
from ingest.account_masking import resolve_account_map_path, resolve_target_account_id
from ingest.errors import IngestError
from ingest.framework import IngestContext
from ingest.models import ParsedPosition, ParsedStatement
from ingest.ms_activity_xlsx import match_releases_for_date
from ingest.pdf_reader import extract_pdf_pages_batch
from ingest.plugins import build_default_registry
from ingest.scanner import CandidateFile, scan_statement_files


@dataclass
class IngestSummary:
    files_scanned: int
    files_ingested: int
    positions_changed: int
    anchors_updated: int
    messages: list[str]
    errors: list[dict[str, str]] = field(default_factory=list)


def ingest_statements(
    statements_dir: Path,
    db: PortfolioDB,
    cfg: dict[str, Any],
    run_id: int | None = None,
    force_all: bool = False,
) -> IngestSummary:
    """
    Scans `statements_dir` for new/updated PDFs, parses them in chronological order,
    diffs positions against prior snapshots, and copies non-scrapeable values into `valuation_anchors`.
    """
    total_scanned, candidates = scan_statement_files(statements_dir, db, force_all=force_all)
    if not candidates:
        return IngestSummary(
            files_scanned=total_scanned,
            files_ingested=0,
            positions_changed=0,
            anchors_updated=0,
            messages=["No new or modified statement PDFs detected since last run."],
        )

    pdf_cands = [c for c in candidates if c.path.suffix.lower() == ".pdf"]
    pages_by_path = extract_pdf_pages_batch([c.path for c in pdf_cands]) if pdf_cands else {}

    parse_cfg = dict(cfg or {})
    if str(getattr(db, "db_path", "")) != ":memory:" and not parse_cfg.get("_account_map_path"):
        parse_cfg["_account_map_path"] = str(Path(db.db_path).resolve().parent / "account_map.json")

    registry = build_default_registry(cfg=parse_cfg)
    parsed_batch: list[tuple[CandidateFile, ParsedStatement]] = []
    messages: list[str] = []
    errors: list[dict[str, str]] = []

    for cand in candidates:
        ctx = IngestContext(
            file_path=cand.path,
            rel_path=cand.rel_path,
            sha256=cand.sha256,
            pages=pages_by_path.get(cand.path) if cand.path.suffix.lower() == ".pdf" else None,
            cfg=parse_cfg,
        )
        plugin, score = registry.detect_plugin(ctx)
        if plugin is None or score <= 0.0:
            msg = f"Skipped unrecognized statement file: {cand.rel_path}"
            messages.append(msg)
            db.log_event(run_id, "FILE_SKIPPED", msg)
            continue
        try:
            stmt = plugin.parse(ctx)
            parsed_batch.append((cand, stmt))
        except IngestError as exc:
            msg = f"Plugin {plugin.plugin_id} failed on {cand.rel_path}: {exc}"
            messages.append(msg)
            errors.append({"rel_path": cand.rel_path, "plugin_id": plugin.plugin_id, "error": str(exc)})
            db.log_event(run_id, "FILE_PARSE_ERROR", msg)
            continue

    # Sort chronologically by `as_of_date` so historical files populate prior state before newer files
    parsed_batch.sort(key=lambda pair: (pair[1].as_of_date, pair[0].rel_path))

    pos_changes = 0
    anchor_updates = 0

    try:
        for cand, stmt in parsed_batch:
            chg, anc = apply_parsed_statement(db, stmt, cand, run_id=run_id)
            pos_changes += chg
            anchor_updates += anc
            msg = (
                f"Ingested {cand.rel_path} [{stmt.doc_type} as of {stmt.as_of_date}] "
                f"(+{chg} position updates, +{anc} non-scrapeable anchor updates)"
            )
            messages.append(msg)

        _reconcile_gsu_release_grants_and_ledger(db, cfg=cfg)
        for plugin in registry.plugins:
            plugin.post_ingest_reconcile(db, cfg)
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return IngestSummary(
        files_scanned=total_scanned,
        files_ingested=len(parsed_batch),
        positions_changed=pos_changes,
        anchors_updated=anchor_updates,
        messages=messages,
        errors=errors,
    )


def _ensure_grant_security(db: PortfolioDB, symbol: str, company_name: str = "") -> str:
    sym = (symbol or "").strip().upper()
    if not sym:
        return ""
    sec_id = f"TICKER:{sym}"
    row = db.conn.execute("SELECT 1 FROM securities WHERE security_id = ?", (sec_id,)).fetchone()
    if not row:
        db.upsert_security(
            security_id=sec_id,
            symbol_or_code=sym,
            canonical_name=company_name or sym,
            asset_class="EQUITY",
            risk_class="single_stock",
            stack_bucket="stocks",
            is_scrapeable=True,
            quote_symbol=sym,
            valuation_model="LIVE_TICKER",
        )
    return sec_id


def apply_parsed_statement(
    db: PortfolioDB,
    stmt: ParsedStatement,
    cand: CandidateFile | None = None,
    source_hash: str | None = None,
    run_id: int | None = None,
) -> tuple[int, int]:
    """
    Applies a single `ParsedStatement` into `PortfolioDB` (usable both from file scan and unit tests).
    Returns `(positions_changed_count, anchors_updated_count)`.
    """
    fhash = source_hash or (cand.sha256 if cand else f"synthetic:{stmt.doc_type}:{stmt.account_id}:{stmt.as_of_date}")
    pos_changes = 0
    anchor_updates = 0

    if stmt.account_id and stmt.account_name and stmt.account_type and stmt.tax_status:
        db.upsert_account(
            account_id=stmt.account_id,
            institution=stmt.institution,
            account_name=stmt.account_name,
            account_type=stmt.account_type,
            tax_status=stmt.tax_status,
            display_order=stmt.display_order,
        )

    # Capability 1: Unvested Stock Grant Tranches
    if stmt.gsu_tranches:
        by_sym: dict[str, list[Any]] = {}
        for t in stmt.gsu_tranches:
            sym = (getattr(t, "symbol", "") or "").strip().upper()
            if sym:
                by_sym.setdefault(sym, []).append(t)

        for sym, sym_tranches in by_sym.items():
            db.conn.execute(
                "DELETE FROM gsu_tranches WHERE report_date = ? AND symbol = ?",
                (stmt.as_of_date, sym),
            )
            comp_name = ""
            for t in sym_tranches:
                comp_name = (getattr(t, "company_name", "") or comp_name or "").strip()
                award_type = (getattr(t, "award_type", "") or "RSU").strip()
                acct_id = (getattr(t, "account_id", "") or stmt.account_id or "").strip()
                db.conn.execute(
                    """
                    INSERT INTO gsu_tranches
                        (report_date, symbol, company_name, award_type, account_id, institution,
                         grant_id, vest_date, grant_date, orig_qty, deus, total_qty, source_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stmt.as_of_date,
                        sym,
                        comp_name,
                        award_type,
                        acct_id,
                        stmt.institution or "",
                        t.grant_id,
                        t.vest_date,
                        t.grant_date,
                        t.orig_qty,
                        t.deus,
                        t.total_qty,
                        fhash,
                    ),
                )
            _ensure_grant_security(db, sym, comp_name)

        tot_gsu = sum(t.total_qty for t in stmt.gsu_tranches)
        syms_desc = ", ".join(sorted(by_sym.keys()))
        db.log_event(
            run_id,
            "GSU_SCHEDULE_INGESTED",
            f"Loaded {len(stmt.gsu_tranches)} {syms_desc} vesting tranches ({tot_gsu:,.3f} unvested shares) as of {stmt.as_of_date}",
            account_id=stmt.account_id,
            as_of_date=stmt.as_of_date,
            new_value=tot_gsu,
        )
        pos_changes += len(stmt.gsu_tranches)

    # Capability 2: Historical Grant Releases & Share Withdrawals
    if stmt.gsu_releases or stmt.gsu_withdrawals:
        for rel in stmt.gsu_releases:
            sym = (getattr(rel, "symbol", "") or "").strip().upper()
            comp_name = (getattr(rel, "company_name", "") or "").strip()
            award_type = (getattr(rel, "award_type", "") or "RSU").strip()
            acct_id = (getattr(rel, "account_id", "") or stmt.account_id or "").strip()
            _ensure_grant_security(db, sym, comp_name)
            db.conn.execute(
                """
                INSERT INTO gsu_releases_history
                    (order_number, symbol, company_name, award_type, account_id, institution,
                     vest_date, grant_id, release_price, gross_shares, net_shares, tax_method, source_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_number) DO UPDATE SET
                    symbol = excluded.symbol,
                    company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE gsu_releases_history.company_name END,
                    award_type = CASE WHEN excluded.award_type != '' THEN excluded.award_type ELSE gsu_releases_history.award_type END,
                    account_id = CASE WHEN excluded.account_id != '' THEN excluded.account_id ELSE gsu_releases_history.account_id END,
                    institution = CASE WHEN excluded.institution != '' THEN excluded.institution ELSE gsu_releases_history.institution END,
                    vest_date = excluded.vest_date,
                    grant_id = CASE WHEN excluded.grant_id != 'UNRESOLVED' AND excluded.grant_id != '' THEN excluded.grant_id ELSE gsu_releases_history.grant_id END,
                    release_price = excluded.release_price,
                    gross_shares = excluded.gross_shares,
                    net_shares = excluded.net_shares,
                    tax_method = excluded.tax_method,
                    source_hash = excluded.source_hash
                """,
                (
                    rel.order_number,
                    sym,
                    comp_name,
                    award_type,
                    acct_id,
                    stmt.institution or "",
                    rel.vest_date,
                    rel.grant_id,
                    rel.release_price,
                    rel.gross_shares,
                    rel.net_shares,
                    rel.tax_method,
                    fhash,
                ),
            )
        for wdr in stmt.gsu_withdrawals:
            sym = (getattr(wdr, "symbol", "") or "").strip().upper()
            acct_id = (getattr(wdr, "account_id", "") or stmt.account_id or "").strip()
            _ensure_grant_security(db, sym)
            db.conn.execute(
                """
                INSERT INTO gsu_withdrawals
                    (order_number, symbol, account_id, institution, exec_date, plan_name, tx_type, price, quantity, net_amount, source_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_number) DO UPDATE SET
                    symbol = excluded.symbol,
                    account_id = CASE WHEN excluded.account_id != '' THEN excluded.account_id ELSE gsu_withdrawals.account_id END,
                    institution = CASE WHEN excluded.institution != '' THEN excluded.institution ELSE gsu_withdrawals.institution END,
                    exec_date = excluded.exec_date,
                    plan_name = excluded.plan_name,
                    tx_type = excluded.tx_type,
                    price = excluded.price,
                    quantity = excluded.quantity,
                    net_amount = excluded.net_amount,
                    source_hash = excluded.source_hash
                """,
                (
                    wdr.order_number,
                    sym,
                    acct_id,
                    stmt.institution or "",
                    wdr.exec_date,
                    wdr.plan_name,
                    wdr.tx_type,
                    wdr.price,
                    wdr.quantity,
                    wdr.net_amount,
                    fhash,
                ),
            )
        tot_net = sum(r.net_shares for r in stmt.gsu_releases)
        db.log_event(
            run_id,
            "GSU_ACTIVITY_INGESTED",
            f"Loaded {len(stmt.gsu_releases)} historical grant releases ({tot_net:,.3f} net sh) and {len(stmt.gsu_withdrawals)} withdrawals",
            account_id=stmt.account_id,
            as_of_date=stmt.as_of_date,
            new_value=tot_net,
        )
        pos_changes += len(stmt.gsu_releases) + len(stmt.gsu_withdrawals)

    # Capability 3: Holdings Positions Snapshot
    if stmt.account_id and stmt.positions:
        acct_id = stmt.account_id
        # Fetch immediately preceding statement date for this account to compute position diffs
        prev_date_row = db.conn.execute(
            """
            SELECT MAX(as_of_date) AS prev_dt
              FROM position_snapshots
             WHERE account_id = ? AND as_of_date < ?
            """,
            (acct_id, stmt.as_of_date),
        ).fetchone()
        prev_dt = prev_date_row["prev_dt"] if prev_date_row else None

        prev_positions: dict[str, dict[str, Any]] = {}
        if prev_dt:
            rows = db.conn.execute(
                """
                SELECT security_id, quantity, stmt_market_value
                  FROM position_snapshots
                 WHERE account_id = ? AND as_of_date = ? AND status = 'OPEN'
                """,
                (acct_id, prev_dt),
            ).fetchall()
            prev_positions = {r["security_id"]: dict(r) for r in rows}

        seen_sec_ids: set[str] = set()

        for pos in stmt.positions:
            seen_sec_ids.add(pos.security_id)
            pos_meta = dict(pos.metadata or {})
            for extra_key in (
                "acquisition_date",
                "maturity_date",
                "expense_ratio_pct",
                "credited_rate_pct",
                "reported_return_pct",
                "reported_return_window",
                "issue_date",
            ):
                val = getattr(pos, extra_key, None)
                if val is not None and pos_meta.get(extra_key) is None:
                    pos_meta[extra_key] = val

            db.upsert_security(
                security_id=pos.security_id,
                symbol_or_code=pos.symbol_or_code,
                canonical_name=pos.canonical_name,
                asset_class=pos.asset_class,
                risk_class=pos.risk_class,
                stack_bucket=pos.stack_bucket,
                is_scrapeable=pos.is_scrapeable,
                quote_symbol=pos.quote_symbol,
                valuation_model="LIVE_TICKER" if pos.is_scrapeable else "STATEMENT_ANCHOR",
                metadata_json=json.dumps(pos_meta),
            )

            # Record position snapshot
            db.conn.execute(
                """
                INSERT INTO position_snapshots
                    (account_id, as_of_date, security_id, quantity, stmt_price, stmt_market_value,
                     accrued_interest, cost_basis, est_annual_income, yield_pct, status, source_hash, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                ON CONFLICT(account_id, as_of_date, security_id) DO UPDATE SET
                    quantity = excluded.quantity,
                    stmt_price = excluded.stmt_price,
                    stmt_market_value = excluded.stmt_market_value,
                    accrued_interest = excluded.accrued_interest,
                    cost_basis = excluded.cost_basis,
                    est_annual_income = excluded.est_annual_income,
                    yield_pct = excluded.yield_pct,
                    status = 'OPEN',
                    source_hash = excluded.source_hash,
                    details_json = excluded.details_json
                """,
                (
                    acct_id,
                    stmt.as_of_date,
                    pos.security_id,
                    pos.quantity,
                    pos.stmt_price,
                    pos.market_value,
                    pos.accrued_interest,
                    pos.cost_basis,
                    pos.est_annual_income,
                    pos.yield_pct,
                    fhash,
                    json.dumps(pos_meta),
                ),
            )

            # Diff against prior statement snapshot
            prev_p = prev_positions.get(pos.security_id)
            if prev_p is None:
                pos_changes += 1
                if prev_dt is not None:
                    db.log_event(
                        run_id,
                        "NEW_POSITION",
                        f"New position in {acct_id}: {pos.canonical_name} ({pos.security_id}) qty={pos.quantity:g}, val=${pos.market_value:,.2f}",
                        account_id=acct_id,
                        security_id=pos.security_id,
                        as_of_date=stmt.as_of_date,
                        new_value=pos.market_value,
                    )
            elif abs(pos.quantity - float(prev_p["quantity"])) > 1e-6:
                pos_changes += 1
                db.log_event(
                    run_id,
                    "QTY_CHANGED",
                    f"Quantity changed in {acct_id} for {pos.canonical_name}: {prev_p['quantity']:g} -> {pos.quantity:g}",
                    account_id=acct_id,
                    security_id=pos.security_id,
                    as_of_date=stmt.as_of_date,
                    old_value=float(prev_p["quantity"]),
                    new_value=pos.quantity,
                )

            # Non-scrapeable holding: copy reported value into `valuation_anchors`!
            if not pos.is_scrapeable:
                updated = _upsert_non_scrapeable_anchor(db, acct_id, pos, fhash, run_id)
                if updated:
                    anchor_updates += 1

        # Detect closed/removed positions that existed in `prev_dt` for this account
        if prev_dt is not None:
            for old_sec_id, old_rec in prev_positions.items():
                if old_sec_id not in seen_sec_ids:
                    pos_changes += 1
                    db.conn.execute(
                        """
                        INSERT INTO position_snapshots
                            (account_id, as_of_date, security_id, quantity, stmt_price, stmt_market_value,
                             status, source_hash)
                        VALUES (?, ?, ?, 0.0, 0.0, 0.0, 'CLOSED', ?)
                        ON CONFLICT(account_id, as_of_date, security_id) DO UPDATE SET
                            quantity = 0.0, stmt_market_value = 0.0, status = 'CLOSED', source_hash = excluded.source_hash
                        """,
                        (acct_id, stmt.as_of_date, old_sec_id, fhash),
                    )
                    db.log_event(
                        run_id,
                        "POSITION_CLOSED",
                        f"Closed/redeemed position in {acct_id}: {old_sec_id} (prior val ${old_rec['stmt_market_value']:,.2f})",
                        account_id=acct_id,
                        security_id=old_sec_id,
                        as_of_date=stmt.as_of_date,
                        old_value=float(old_rec["stmt_market_value"]),
                        new_value=0.0,
                    )

        # Save adjustments (e.g. fund reverse split, stock-plan intra-quarter release steps)
        for adj in stmt.adjustments:
            db.conn.execute(
                """
                INSERT INTO position_adjustments
                    (account_id, security_id, effective_date, adj_type, pre_quantity, post_quantity, source_hash, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, security_id, effective_date, adj_type) DO UPDATE SET
                    pre_quantity = excluded.pre_quantity,
                    post_quantity = excluded.post_quantity,
                    source_hash = excluded.source_hash,
                    notes = excluded.notes
                """,
                (
                    acct_id,
                    adj.security_id,
                    adj.effective_date,
                    adj.adj_type,
                    adj.pre_quantity,
                    adj.post_quantity,
                    fhash,
                    adj.notes,
                ),
            )

    if cand is not None:
        db.conn.execute(
            """
            INSERT INTO ingested_files
                (sha256_hash, file_path, rel_path, file_size, file_birthtime, file_mtime, file_ctime,
                 doc_type, account_id, period_start, statement_as_of_date, reported_total, ingested_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256_hash) DO UPDATE SET
                file_path = excluded.file_path,
                rel_path = excluded.rel_path,
                file_mtime = excluded.file_mtime,
                ingested_at_iso = excluded.ingested_at_iso
            """,
            (
                cand.sha256,
                str(cand.path),
                cand.rel_path,
                cand.size,
                cand.birthtime,
                cand.mtime,
                cand.ctime,
                stmt.doc_type,
                stmt.account_id,
                stmt.period_start,
                stmt.as_of_date,
                stmt.reported_total,
                utc_now_iso(),
            ),
        )
        db.log_event(
            run_id,
            "FILE_INGESTED",
            f"Ingested {cand.rel_path} ({stmt.doc_type}, as of {stmt.as_of_date})",
            account_id=stmt.account_id,
            as_of_date=stmt.as_of_date,
            new_value=stmt.reported_total,
        )

    return pos_changes, anchor_updates


def _upsert_non_scrapeable_anchor(
    db: PortfolioDB,
    account_id: str,
    pos: ParsedPosition,
    source_hash: str,
    run_id: int | None,
) -> bool:
    """
    Copies a reported non-scrapeable holding value (e.g. a fixed or indexed annuity contract)
    into `valuation_anchors` and emits an `ANCHOR_UPDATED` event when new or changed.
    """
    prior = db.conn.execute(
        """
        SELECT value_date, market_value
          FROM valuation_anchors
         WHERE security_id = ? AND account_id = ?
         ORDER BY value_date DESC
         LIMIT 1
        """,
        (pos.security_id, account_id),
    ).fetchone()

    existing_same_date = db.conn.execute(
        """
        SELECT market_value
          FROM valuation_anchors
         WHERE security_id = ? AND account_id = ? AND value_date = ?
        """,
        (pos.security_id, account_id, pos.value_date),
    ).fetchone()

    is_new_or_changed = (
        existing_same_date is None
        or abs(float(existing_same_date["market_value"]) - pos.market_value) > 0.005
    )

    db.conn.execute(
        """
        INSERT INTO valuation_anchors
            (security_id, account_id, value_date, market_value, surrender_value, accrued_interest,
             issue_date, initial_premium, source_hash, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(security_id, account_id, value_date) DO UPDATE SET
            market_value = excluded.market_value,
            surrender_value = COALESCE(excluded.surrender_value, valuation_anchors.surrender_value),
            accrued_interest = excluded.accrued_interest,
            issue_date = COALESCE(excluded.issue_date, valuation_anchors.issue_date),
            initial_premium = COALESCE(excluded.initial_premium, valuation_anchors.initial_premium),
            source_hash = excluded.source_hash,
            metadata_json = excluded.metadata_json
        """,
        (
            pos.security_id,
            account_id,
            pos.value_date,
            pos.market_value,
            pos.surrender_value,
            pos.accrued_interest,
            pos.issue_date,
            pos.initial_premium,
            source_hash,
            json.dumps(pos.metadata),
        ),
    )

    if is_new_or_changed:
        old_val = float(prior["market_value"]) if prior else None
        old_dt = prior["value_date"] if prior else None
        if old_val is not None and old_dt != pos.value_date:
            summary = (
                f"Updated non-scrapeable anchor for {pos.canonical_name} ({pos.security_id}): "
                f"${old_val:,.2f} [{old_dt}] -> ${pos.market_value:,.2f} [{pos.value_date}]"
            )
        else:
            summary = (
                f"Recorded non-scrapeable anchor for {pos.canonical_name} ({pos.security_id}): "
                f"${pos.market_value:,.2f} as of {pos.value_date}"
            )
        db.log_event(
            run_id,
            "ANCHOR_UPDATED",
            summary,
            account_id=account_id,
            security_id=pos.security_id,
            as_of_date=pos.value_date,
            old_value=old_val,
            new_value=pos.market_value,
        )
        return True
    return False


def _reconcile_gsu_release_grants_and_ledger(
    db: PortfolioDB,
    cfg: dict[str, Any] | None = None,
) -> None:
    """
    Per-symbol reconciliation: matches unresolved historical release rows to exact Grant IDs
    from `gsu_tranches`, and rebuilds the chronological share ledger (`GSU_RELEASE_STEP`) when
    a target vested account exists in `accounts`.
    """
    rel_rows = [
        dict(r)
        for r in db.conn.execute(
            "SELECT * FROM gsu_releases_history ORDER BY vest_date ASC, order_number ASC"
        ).fetchall()
    ]
    if not rel_rows:
        return

    gsu_cfg = (cfg or {}).get("grant_forecast") or (cfg or {}).get("gsu_forecast") or {}
    cfg_target_acct_id = gsu_cfg.get("target_vested_account_id") or ""
    cfg_target_sec_id = gsu_cfg.get("target_vested_security_id") or ""
    primary_sym = str((gsu_cfg.get("symbol") or (cfg or {}).get("primary_equity_symbol") or "")).strip().upper()

    rel_by_sym: dict[str, list[dict[str, Any]]] = {}
    for r in rel_rows:
        sym = (r.get("symbol") or primary_sym).strip().upper()
        if sym:
            rel_by_sym.setdefault(sym, []).append(r)

    known_accounts = {
        r["account_id"] for r in db.conn.execute("SELECT account_id FROM accounts").fetchall()
    }
    db_p = Path(db.db_path) if str(getattr(db, "db_path", "")) != ":memory:" else None
    resolved_target = resolve_target_account_id(cfg_target_acct_id, known_accounts, cfg=cfg, db_path=db_p)
    if resolved_target:
        cfg_target_acct_id = resolved_target

    for sym, sym_rels in sorted(rel_by_sym.items()):
        latest_gsu = db.conn.execute(
            "SELECT MAX(report_date) AS rd FROM gsu_tranches WHERE symbol = ?",
            (sym,),
        ).fetchone()
        active_grants: list[dict[str, Any]] = []
        if latest_gsu and latest_gsu["rd"]:
            active_grants = [
                dict(r)
                for r in db.conn.execute(
                    """
                    SELECT grant_id, grant_date, MAX(vest_date) AS last_vest,
                           SUM(orig_qty) * 1.0 / COUNT(*) AS base_orig
                      FROM gsu_tranches
                     WHERE report_date = ? AND symbol = ?
                     GROUP BY grant_id
                    """,
                    (latest_gsu["rd"], sym),
                ).fetchall()
            ]

        # Match unresolved release rows per vest_date against active grants for the same symbol
        by_date: dict[str, list[dict[str, Any]]] = {}
        for r in sym_rels:
            gid_cur = (r.get("grant_id") or "").strip()
            if not gid_cur or gid_cur in ("UNRESOLVED", "PRIOR-GRANT", "PRIOR_GRANT"):
                by_date.setdefault(r["vest_date"], []).append(r)

        for vdate, drows in sorted(by_date.items()):
            needs_match = [
                d for d in drows
                if not d.get("grant_id") or d.get("grant_id") in ("UNRESOLVED", "PRIOR-GRANT", "PRIOR_GRANT")
            ]
            if not needs_match:
                continue
            mapping = match_releases_for_date(vdate, needs_match, active_grants)
            for onum, gid in mapping.items():
                db.conn.execute(
                    "UPDATE gsu_releases_history SET grant_id = ? WHERE order_number = ?",
                    (gid, onum),
                )

        target_sec_id = (
            cfg_target_sec_id
            if (cfg_target_sec_id and sym == primary_sym)
            else f"TICKER:{sym}"
        )
        target_acct_id = ""
        for cand_r in sym_rels:
            if cand_r.get("account_id"):
                target_acct_id = str(cand_r["account_id"]).strip()
                break
        if not target_acct_id and sym == primary_sym:
            target_acct_id = cfg_target_acct_id
        if not target_acct_id and sym == primary_sym:
            sp_acct = db.conn.execute(
                "SELECT account_id FROM accounts WHERE UPPER(account_type) LIKE '%STOCK%PLAN%' OR UPPER(account_name) LIKE '%STOCK%PLAN%' LIMIT 1"
            ).fetchone()
            target_acct_id = sp_acct["account_id"] if sp_acct else ""

        if not target_acct_id or target_acct_id not in known_accounts:
            # Skip writing ledger adjustments for accounts that don't exist in `accounts` (FK constraint)
            continue

        sec_exists = db.conn.execute(
            "SELECT 1 FROM securities WHERE security_id = ?",
            (target_sec_id,),
        ).fetchone()
        if not sec_exists:
            continue

        wdr_rows = [
            dict(r)
            for r in db.conn.execute(
                """
                SELECT * FROM gsu_withdrawals
                 WHERE symbol = ?
                   AND lower(plan_name) != 'cash'
                 ORDER BY exec_date ASC, order_number ASC
                """,
                (sym,),
            ).fetchall()
        ]

        deltas_by_date: dict[str, float] = {}
        for r in sym_rels:
            dt = r["vest_date"]
            deltas_by_date[dt] = round(deltas_by_date.get(dt, 0.0) + float(r["net_shares"]), 6)
        for w in wdr_rows:
            dt = w["exec_date"]
            deltas_by_date[dt] = round(deltas_by_date.get(dt, 0.0) + float(w["quantity"]), 6)

        db.conn.execute(
            "DELETE FROM position_adjustments WHERE account_id = ? AND security_id = ? AND adj_type = 'GSU_RELEASE_STEP'",
            (target_acct_id, target_sec_id),
        )
        running = 0.0
        for dt in sorted(deltas_by_date.keys()):
            dq = deltas_by_date[dt]
            if abs(dq) < 1e-9:
                continue
            pre_q = running
            running = round(running + dq, 6)
            db.conn.execute(
                """
                INSERT INTO position_adjustments
                    (account_id, security_id, effective_date, adj_type, pre_quantity, post_quantity, source_hash, notes)
                VALUES (?, ?, ?, 'GSU_RELEASE_STEP', ?, ?, 'activity_ledger', ?)
                """,
                (target_acct_id, target_sec_id, dt, pre_q, running, f"Activity ledger net share delta {dq:+,.3f} on {dt}"),
            )

        # If an in-kind transfer (tx_type == 'Transfer') moved shares into a brokerage account, record pre-transfer 0 shares
        for w in wdr_rows:
            if w["tx_type"] == "Transfer" and w["quantity"] < 0:
                xfer_sh = abs(float(w["quantity"]))
                matching_snaps = db.conn.execute(
                    """
                    SELECT account_id, quantity
                      FROM position_snapshots
                     WHERE security_id = ? AND account_id != ?
                     ORDER BY as_of_date DESC
                    """,
                    (target_sec_id, target_acct_id),
                ).fetchall()
                for snap in matching_snaps:
                    if abs(float(snap["quantity"]) - xfer_sh) < 1e-3:
                        dest_acct_id = snap["account_id"]
                        db.conn.execute(
                            """
                            INSERT INTO position_adjustments
                                (account_id, security_id, effective_date, adj_type, pre_quantity, post_quantity, source_hash, notes)
                            VALUES (?, ?, ?, 'PRE_SPLIT_SHARES', 0.0, ?, 'activity_ledger', ?)
                            ON CONFLICT(account_id, security_id, effective_date, adj_type) DO UPDATE SET
                                pre_quantity = 0.0, post_quantity = excluded.post_quantity, notes = excluded.notes
                            """,
                            (
                                dest_acct_id,
                                target_sec_id,
                                w["exec_date"],
                                xfer_sh,
                                f"In-kind grant share transfer from {target_acct_id} on {w['exec_date']}",
                            ),
                        )
                        break
