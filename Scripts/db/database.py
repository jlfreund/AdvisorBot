#!/usr/bin/env python3
"""Database management layer (`PortfolioDB`) wrapping SQLite operations."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PortfolioDB:
    """Manages connection, migrations, and structured queries on `portfolio.sqlite`."""

    def __init__(self, db_path: Path | str):
        is_mem = str(db_path) == ":memory:"
        self.db_path = ":memory:" if is_mem else Path(db_path)
        if not is_mem:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        try:
            self.conn.row_factory = sqlite3.Row
            if not is_mem:
                self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self._schema_verified = False
            self.ensure_schema()
            if not is_mem:
                try:
                    os.chmod(self.db_path, 0o600)
                except OSError:
                    pass
        except Exception:
            self.close()
            raise

    def _add_missing_columns(self, table: str, specs: list[tuple[str, str]]) -> None:
        cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_def in specs:
            if col_name not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")

    def ensure_schema(self, force: bool = False) -> None:
        if self._schema_verified and not force:
            return
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(ddl)
        self._add_missing_columns("accounts", [("parent_account_id", "TEXT")])
        self._add_missing_columns("holding_forecasts_baseline", [("growth_evidence_json", "TEXT")])
        self._add_missing_columns("holding_preferences", [
            ("fee_pct", "REAL"),
            ("comment", "TEXT"),
        ])

        # Phase E Migration: Ensure `securities` has the strict 7-class `risk_class` column & CHECK constraint
        sec_sql_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='securities'"
        ).fetchone()
        sec_sql = str(sec_sql_row[0] or "") if sec_sql_row else ""
        sec_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(securities)").fetchall()}
        if "risk_class" not in sec_cols or "hybrid_buffered" not in sec_sql or "crypto_speculative" not in sec_sql:
            has_rc = "risk_class" in sec_cols
            rc_expr = (
                """
                CASE
                    WHEN UPPER(asset_class) IN ('CASH','MONEY_MARKET','CD','FIXED_ANNUITY','MYGA') THEN 'cash_fixed'
                    WHEN UPPER(asset_class) IN ('MUNI_BOND','BOND','TREASURY','TIPS','INDEXED_ANNUITY') THEN 'fixed_income'
                    WHEN UPPER(asset_class) IN ('RILA_ANNUITY','BALANCED_FUND','TARGET_DATE_FUND','BUFFER_ETF','HIGH_YIELD') THEN 'hybrid_buffered'
                    WHEN UPPER(asset_class) IN ('COMMODITY','GOLD','PRECIOUS_METAL') THEN 'real_diversifier'
                    WHEN UPPER(asset_class) IN ('CRYPTO','LEVERAGED_ETF','OPTION') THEN 'crypto_speculative'
                    WHEN UPPER(asset_class) IN ('MUTUAL_FUND','ETF','CIT','REIT') THEN 'broad_equity'
                    ELSE 'single_stock'
                END
                """
                if not has_rc
                else """
                CASE
                    WHEN risk_class IN ('cash_fixed','fixed_income','hybrid_buffered','broad_equity','real_diversifier','single_stock','crypto_speculative') THEN risk_class
                    WHEN UPPER(asset_class) IN ('CASH','MONEY_MARKET','CD','FIXED_ANNUITY','MYGA') THEN 'cash_fixed'
                    WHEN UPPER(asset_class) IN ('MUNI_BOND','BOND','TREASURY','TIPS','INDEXED_ANNUITY') THEN 'fixed_income'
                    WHEN UPPER(asset_class) IN ('RILA_ANNUITY','BALANCED_FUND','TARGET_DATE_FUND','BUFFER_ETF','HIGH_YIELD') THEN 'hybrid_buffered'
                    WHEN UPPER(asset_class) IN ('MUTUAL_FUND','ETF','CIT','REIT') THEN 'broad_equity'
                    ELSE 'single_stock'
                END
                """
            )
            self.conn.executescript(
                f"""
                PRAGMA foreign_keys = OFF;
                BEGIN;
                CREATE TABLE securities_new (
                    security_id TEXT PRIMARY KEY,
                    symbol_or_code TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    risk_class TEXT NOT NULL CHECK (risk_class IN ('cash_fixed','fixed_income','hybrid_buffered','broad_equity','real_diversifier','single_stock','crypto_speculative')),
                    stack_bucket TEXT NOT NULL,
                    is_scrapeable INTEGER NOT NULL,
                    quote_symbol TEXT,
                    valuation_model TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{{}}'
                );
                INSERT INTO securities_new (
                    security_id, symbol_or_code, canonical_name, asset_class, risk_class,
                    stack_bucket, is_scrapeable, quote_symbol, valuation_model, metadata_json
                )
                SELECT
                    security_id, symbol_or_code, canonical_name, asset_class, {rc_expr},
                    stack_bucket, is_scrapeable, quote_symbol, valuation_model, COALESCE(metadata_json, '{{}}')
                FROM securities;
                DROP TABLE securities;
                ALTER TABLE securities_new RENAME TO securities;
                COMMIT;
                PRAGMA foreign_keys = ON;
                """
            )

        # Migrate gsu_tranches to include multi-company metadata & composite PK (report_date, symbol, grant_id, vest_date)
        tranche_info = self.conn.execute("PRAGMA table_info(gsu_tranches)").fetchall()
        tranche_cols = {r[1] for r in tranche_info}
        tranche_pk_cols = {r[1] for r in tranche_info if int(r[5] or 0) > 0}
        if "symbol" not in tranche_cols or "symbol" not in tranche_pk_cols:
            self.conn.executescript(
                """
                CREATE TABLE gsu_tranches_new (
                    report_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    company_name TEXT DEFAULT '',
                    award_type TEXT NOT NULL DEFAULT 'RSU',
                    account_id TEXT DEFAULT '',
                    institution TEXT DEFAULT '',
                    grant_id TEXT NOT NULL,
                    vest_date TEXT NOT NULL,
                    grant_date TEXT NOT NULL,
                    orig_qty REAL NOT NULL,
                    deus REAL NOT NULL,
                    total_qty REAL NOT NULL,
                    source_hash TEXT NOT NULL,
                    PRIMARY KEY (report_date, symbol, grant_id, vest_date)
                );
                INSERT INTO gsu_tranches_new (
                    report_date, symbol, company_name, award_type, account_id, institution,
                    grant_id, vest_date, grant_date, orig_qty, deus, total_qty, source_hash
                )
                SELECT
                    report_date, '', '', 'RSU', '', '',
                    grant_id, vest_date, grant_date, orig_qty, deus, total_qty, source_hash
                FROM gsu_tranches;
                DROP TABLE gsu_tranches;
                ALTER TABLE gsu_tranches_new RENAME TO gsu_tranches;
                CREATE INDEX IF NOT EXISTS idx_gsu_vest_date ON gsu_tranches(report_date, vest_date);
                """
            )
        else:
            self._add_missing_columns("gsu_tranches", [
                ("company_name", "TEXT DEFAULT ''"),
                ("award_type", "TEXT NOT NULL DEFAULT 'RSU'"),
                ("account_id", "TEXT DEFAULT ''"),
                ("institution", "TEXT DEFAULT ''"),
            ])

        self._add_missing_columns("gsu_releases_history", [
            ("symbol", "TEXT NOT NULL DEFAULT ''"),
            ("company_name", "TEXT DEFAULT ''"),
            ("award_type", "TEXT NOT NULL DEFAULT 'RSU'"),
            ("account_id", "TEXT DEFAULT ''"),
            ("institution", "TEXT DEFAULT ''"),
        ])
        self._add_missing_columns("gsu_withdrawals", [
            ("symbol", "TEXT NOT NULL DEFAULT ''"),
            ("account_id", "TEXT DEFAULT ''"),
            ("institution", "TEXT DEFAULT ''"),
        ])

        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gsu_tranches_sym_vest ON gsu_tranches(report_date, symbol, vest_date)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gsu_rel_hist_sym_date ON gsu_releases_history(symbol, vest_date, grant_id)"
        )
        self.conn.commit()
        self._schema_verified = True

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PortfolioDB":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.close()

    # --- Run watermark management ---

    def get_last_completed_run_epoch(self) -> float:
        row = self.conn.execute(
            "SELECT MAX(started_at) AS last_ts FROM update_runs WHERE completed_iso IS NOT NULL"
        ).fetchone()
        return float(row["last_ts"]) if row and row["last_ts"] is not None else 0.0

    def start_run(self, mode: str, notes: str = "") -> int:
        now_ts = time.time()
        cur = self.conn.execute(
            "INSERT INTO update_runs (mode, started_at, started_iso, notes) VALUES (?, ?, ?, ?)",
            (mode, now_ts, utc_now_iso(), notes),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        files_scanned: int = 0,
        files_ingested: int = 0,
        positions_changed: int = 0,
        anchors_updated: int = 0,
    ) -> None:
        self.conn.execute(
            """
            UPDATE update_runs
               SET completed_iso = ?,
                   files_scanned = ?,
                   files_ingested = ?,
                   positions_changed = ?,
                   anchors_updated = ?
             WHERE run_id = ?
            """,
            (utc_now_iso(), files_scanned, files_ingested, positions_changed, anchors_updated, run_id),
        )
        self.conn.commit()

    def log_event(
        self,
        run_id: int | None,
        event_type: str,
        summary: str,
        account_id: str | None = None,
        security_id: str | None = None,
        as_of_date: str | None = None,
        old_value: float | None = None,
        new_value: float | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ingest_events
                (run_id, occurred_at_iso, event_type, account_id, security_id, as_of_date, old_value, new_value, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, utc_now_iso(), event_type, account_id, security_id, as_of_date, old_value, new_value, summary),
        )

    # --- Accounts & Securities ---

    def upsert_account(
        self,
        account_id: str,
        institution: str,
        account_name: str,
        account_type: str,
        tax_status: str,
        display_order: int = 100,
        parent_account_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO accounts (account_id, institution, account_name, account_type, tax_status, display_order, parent_account_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                institution = excluded.institution,
                account_name = excluded.account_name,
                account_type = excluded.account_type,
                tax_status = excluded.tax_status,
                display_order = excluded.display_order,
                parent_account_id = COALESCE(excluded.parent_account_id, accounts.parent_account_id)
            """,
            (account_id, institution, account_name, account_type, tax_status, display_order, parent_account_id),
        )

    def upsert_security(
        self,
        security_id: str,
        symbol_or_code: str,
        canonical_name: str,
        asset_class: str,
        stack_bucket: str,
        is_scrapeable: bool,
        quote_symbol: str | None,
        valuation_model: str,
        risk_class: str = "",
        metadata_json: str = "{}",
    ) -> None:
        from ingest.classification import validate_risk_class

        validated_rc = validate_risk_class(asset_class, risk_class, security_id=security_id)
        merged_meta_json = metadata_json
        existing_sec = self.conn.execute(
            "SELECT metadata_json FROM securities WHERE security_id = ?",
            (security_id,),
        ).fetchone()
        if existing_sec and existing_sec["metadata_json"]:
            try:
                old_m = json.loads(existing_sec["metadata_json"])
                new_m = json.loads(metadata_json or "{}")
                if isinstance(old_m, dict) and isinstance(new_m, dict):
                    merged = {**old_m, **{k: v for k, v in new_m.items() if v is not None}}
                    merged_meta_json = json.dumps(merged)
            except (TypeError, ValueError):
                pass
        self.conn.execute(
            """
            INSERT INTO securities
                (security_id, symbol_or_code, canonical_name, asset_class, risk_class, stack_bucket,
                 is_scrapeable, quote_symbol, valuation_model, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(security_id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                asset_class = excluded.asset_class,
                risk_class = excluded.risk_class,
                stack_bucket = excluded.stack_bucket,
                is_scrapeable = excluded.is_scrapeable,
                quote_symbol = COALESCE(excluded.quote_symbol, securities.quote_symbol),
                valuation_model = excluded.valuation_model,
                metadata_json = excluded.metadata_json
            """,
            (
                security_id,
                symbol_or_code,
                canonical_name,
                asset_class,
                validated_rc,
                stack_bucket,
                1 if is_scrapeable else 0,
                quote_symbol,
                valuation_model,
                merged_meta_json,
            ),
        )

    def upsert_holding_forecast_baseline(
        self,
        *,
        account_id: str,
        security_id: str,
        risk_class: str,
        projected_return_pct: float,
        return_source: str,
        confidence: str,
        source_detail: str = "",
        growth_evidence: list[dict[str, Any]] | None = None,
        realized_vol_pct: float | None = None,
        beta_spy: float | None = None,
        corr_spy: float | None = None,
        lookback_years: float | None = None,
        computed_at_iso: str | None = None,
    ) -> None:
        from ingest.classification import VALID_RISK_CLASSES

        rc = str(risk_class or "").strip().lower()
        if rc not in VALID_RISK_CLASSES:
            raise ValueError(f"Invalid risk_class '{risk_class}' for holding_forecasts_baseline")
        conf = str(confidence or "MEDIUM").strip().upper()
        if conf not in ("HIGH", "MEDIUM", "LOW"):
            conf = "MEDIUM"
        ts = computed_at_iso or utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO holding_forecasts_baseline (
                account_id, security_id, risk_class, projected_return_pct,
                realized_vol_pct, beta_spy, corr_spy, lookback_years,
                return_source, confidence, source_detail, growth_evidence_json, computed_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, security_id) DO UPDATE SET
                risk_class = excluded.risk_class,
                projected_return_pct = excluded.projected_return_pct,
                realized_vol_pct = excluded.realized_vol_pct,
                beta_spy = excluded.beta_spy,
                corr_spy = excluded.corr_spy,
                lookback_years = excluded.lookback_years,
                return_source = excluded.return_source,
                confidence = excluded.confidence,
                source_detail = excluded.source_detail,
                growth_evidence_json = excluded.growth_evidence_json,
                computed_at_iso = excluded.computed_at_iso
            """,
            (
                str(account_id or ""),
                security_id,
                rc,
                round(float(projected_return_pct), 4),
                round(float(realized_vol_pct), 4) if realized_vol_pct is not None else None,
                round(float(beta_spy), 4) if beta_spy is not None else None,
                round(float(corr_spy), 4) if corr_spy is not None else None,
                round(float(lookback_years), 3) if lookback_years is not None else None,
                str(return_source),
                conf,
                str(source_detail or ""),
                json.dumps(growth_evidence) if growth_evidence else None,
                ts,
            ),
        )

    def get_holding_forecasts_baseline(self) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM holding_forecasts_baseline").fetchall()
        return {(str(r["account_id"] or ""), str(r["security_id"])): dict(r) for r in rows}

    def get_ingested_hashes(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM ingested_files").fetchall()
        return {r["sha256_hash"]: dict(r) for r in rows}

    def get_ingested_by_path(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ingested_files ORDER BY ingested_at_iso ASC"
        ).fetchall()
        return {r["file_path"]: dict(r) for r in rows}

    def get_holding_preferences(self) -> dict[str, dict[str, Any]]:
        self.ensure_schema()
        rows = self.conn.execute("SELECT * FROM holding_preferences").fetchall()
        return {r["holding_key"]: dict(r) for r in rows}

    def set_holding_preference(
        self,
        holding_key: str,
        statement_name: str,
        friendly_name: str,
        account_id: str | None = None,
        security_id: str | None = None,
        institution: str | None = None,
        investment_type: str | None = None,
        maturity_date: str | None = None,
        fee_pct: float | None = None,
        comment: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now_iso = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO holding_preferences (
                holding_key, account_id, security_id, statement_name, friendly_name,
                institution, investment_type, maturity_date, fee_pct, comment, notes, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(holding_key) DO UPDATE SET
                account_id = COALESCE(excluded.account_id, holding_preferences.account_id),
                security_id = COALESCE(excluded.security_id, holding_preferences.security_id),
                statement_name = excluded.statement_name,
                friendly_name = excluded.friendly_name,
                institution = COALESCE(excluded.institution, holding_preferences.institution),
                investment_type = COALESCE(excluded.investment_type, holding_preferences.investment_type),
                maturity_date = COALESCE(excluded.maturity_date, holding_preferences.maturity_date),
                fee_pct = excluded.fee_pct,
                comment = excluded.comment,
                notes = COALESCE(excluded.notes, holding_preferences.notes),
                updated_at_iso = excluded.updated_at_iso
            """,
            (
                holding_key,
                account_id,
                security_id,
                statement_name,
                friendly_name,
                institution,
                investment_type,
                maturity_date,
                fee_pct,
                comment,
                notes,
                now_iso,
            ),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM holding_preferences WHERE holding_key = ?", (holding_key,)).fetchone()
        return dict(row) if row else {}
