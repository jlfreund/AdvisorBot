#!/usr/bin/env python3
"""
Phase E (`IngestPipeline.html`) unit & integration test suite:
Covers:
  - DP-0 (0A): 7-class canonical `RiskClass` taxonomy (`cash_fixed`, `fixed_income`, `hybrid_buffered`,
               `broad_equity`, `real_diversifier`, `single_stock`, `crypto_speculative`), SQLite `CHECK`
               constraint, and deterministic `classify_public_security()` mapping.
  - DP-1 (1A): Sparse `holdingForecasts: {}` default inheritance of ingested baseline return & risk class.
  - DP-2 (2A): 10Y total-return CAGR computation + `[-20.0%, +25.0%]` clamping.
  - DP-3 (3B): Realized 10Y `realized_vol_pct` & `beta_spy` used in Monte Carlo until `risk_overridden=True`.
  - DP-4 (4A): Statement contractual APY / YTM primary over price swing for cash, bonds, and MYGAs.
  - DP-5 (5A): Bi-directional dollar-weighted sleeve constituent roll-up vs explicit parent override.
  - DP-6 (6A): Strict `InvalidHoldingClassificationError` gate + atomic transaction rollback on failure,
               and `LOW`-confidence benchmark fallback + diagnostic warning for `<180d` spin-offs.
  - DP-7 (7A): Custom plugin tie-break priority (`is_custom=True`), strict checksum gate (`±$0.05`),
               and `inspect-statement` / `test-plugin` CLI subcommands.
  - 7-class Python <-> JS parity across `single_path`, `stress`, `montecarlo`, and `historical`.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "Scripts"
sys.path.insert(0, str(SCRIPTS))

from db.database import PortfolioDB  # noqa: E402
from ingest.classification import (  # noqa: E402
    VALID_RISK_CLASSES,
    classify_public_security,
    validate_risk_class,
)
from ingest.errors import (  # noqa: E402
    InvalidHoldingClassificationError,
    StatementChecksumError,
)
from ingest.framework import IngestContext, PluginRegistry, StatementPlugin  # noqa: E402
from ingest.models import ParsedPosition, ParsedStatement  # noqa: E402
from ingest.pipeline import apply_parsed_statement  # noqa: E402
from ingest.plugins import load_custom_plugins_from_dir  # noqa: E402
from ingest.scanner import CandidateFile  # noqa: E402
from settings import RISK_CLASSES, canonicalize_model_settings, default_settings_snapshot  # noqa: E402
from valuation.forecast_enricher import enrich_holding_forecasts  # noqa: E402
from valuation.simulator import (  # noqa: E402
    build_effective_settings,
    load_live_payload,
    resolve_asset_forecast,
    run_historical_cohorts,
    run_monte_carlo,
    run_simulation,
    stress_year1_shocks_by_risk_class,
)

HARNESS = SCRIPTS / "tests" / "parity_harness.mjs"


def _invoke_js_harness(index_path: Path, cfg: dict, mode: str = "run") -> dict | list:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_in = Path(tmpdir) / f"parity_in_{mode}.json"
        tmp_out = Path(tmpdir) / f"parity_out_{mode}.json"
        tmp_in.write_text(json.dumps(cfg), encoding="utf-8")
        out = subprocess.run(
            ["node", str(HARNESS), str(index_path), str(tmp_in), mode, str(tmp_out)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if out.returncode != 0 or not tmp_out.exists():
            raise AssertionError(
                f"node harness ({mode}) failed rc={out.returncode}:\nstdout={out.stdout}\nstderr={out.stderr}"
            )
        return json.loads(tmp_out.read_text(encoding="utf-8"))


class PhaseEIngestForecastsTest(unittest.TestCase):
    def test_7_risk_classes_taxonomy_and_validation_gate(self):
        expected = (
            "cash_fixed",
            "fixed_income",
            "hybrid_buffered",
            "broad_equity",
            "real_diversifier",
            "single_stock",
            "crypto_speculative",
        )
        self.assertEqual(tuple(VALID_RISK_CLASSES), expected)
        self.assertEqual(tuple(RISK_CLASSES.keys()), expected)

        # Deterministic public security classification across all 7 classes
        self.assertEqual(classify_public_security("GOOG", "Alphabet Inc", "EQUITY")[1], "single_stock")
        self.assertEqual(classify_public_security("VOO", "Vanguard S&P 500 ETF", "ETF")[1], "broad_equity")
        self.assertEqual(classify_public_security("SGOV", "iShares 0-3 Month Treasury", "ETF")[1], "cash_fixed")
        self.assertEqual(classify_public_security("BND", "Vanguard Total Bond Market", "ETF")[1], "fixed_income")
        self.assertEqual(classify_public_security("BUFR", "FT Vest Laddered Buffer ETF", "ETF")[1], "hybrid_buffered")
        self.assertEqual(classify_public_security("GLD", "SPDR Gold Shares", "ETF")[1], "real_diversifier")
        self.assertEqual(classify_public_security("IBIT", "iShares Bitcoin Trust", "ETF")[1], "crypto_speculative")
        self.assertEqual(classify_public_security("TQQQ", "ProShares UltraPro QQQ", "ETF")[1], "crypto_speculative")

        # Missing, unknown, and conflicting risk_class MUST raise InvalidHoldingClassificationError
        with self.assertRaises(InvalidHoldingClassificationError) as ctx_missing:
            ParsedPosition(
                security_id="TICKER:AAPL",
                symbol_or_code="AAPL",
                canonical_name="Apple Inc",
                asset_class="EQUITY",
                stack_bucket="stocks",
                is_scrapeable=True,
                quote_symbol="AAPL",
                quantity=10.0,
                stmt_price=200.0,
                market_value=2000.0,
                value_date="2026-10-31",
                risk_class="",
            )
        self.assertEqual(ctx_missing.exception.diagnostic.code, "INVALID_RISK_CLASS")

        with self.assertRaises(InvalidHoldingClassificationError):
            validate_risk_class("ETF", "mystery_bucket", security_id="TICKER:XYZ")

        with self.assertRaises(InvalidHoldingClassificationError):
            validate_risk_class("CASH", "single_stock", security_id="CASH:ACCT-1")

        with self.assertRaises(InvalidHoldingClassificationError):
            validate_risk_class("MUNI_BOND", "crypto_speculative", security_id="CUSIP:123456789")

        # Database gate + SQLite CHECK constraint
        with PortfolioDB(":memory:") as db:
            with self.assertRaises(InvalidHoldingClassificationError):
                db.upsert_security(
                    security_id="CASH:ACCT-1",
                    symbol_or_code="USD",
                    canonical_name="Sweep Cash",
                    asset_class="CASH",
                    risk_class="broad_equity",
                    stack_bucket="cash",
                    is_scrapeable=False,
                    quote_symbol=None,
                    valuation_model="STATEMENT_ANCHOR",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.conn.execute(
                    """
                    INSERT INTO securities (
                        security_id, symbol_or_code, canonical_name, asset_class,
                        risk_class, stack_bucket, is_scrapeable, quote_symbol, valuation_model
                    ) VALUES ('TICKER:BAD', 'BAD', 'Bad', 'EQUITY', 'not_a_valid_class', 'stocks', 1, 'BAD', 'LIVE_TICKER')
                    """
                )

    def test_atomic_rollback_on_mid_statement_classification_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stmts_dir = Path(tmpdir) / "Statements"
            stmts_dir.mkdir(parents=True)
            bad_pdf = stmts_dir / "Broken_Statement.txt"
            bad_pdf.write_text("SYNTHETIC_BROKEN_FI_STATEMENT\n", encoding="utf-8")

            # Test atomic rollback when an error occurs mid-apply after writing position 1
            with PortfolioDB(":memory:") as db:
                db.upsert_security(
                    security_id="CASH:SEED",
                    symbol_or_code="USD",
                    canonical_name="Seed Cash",
                    asset_class="CASH",
                    risk_class="cash_fixed",
                    stack_bucket="cash",
                    is_scrapeable=False,
                    quote_symbol=None,
                    valuation_model="STATEMENT_ANCHOR",
                )
                db.conn.commit()

                good_pos = ParsedPosition(
                    security_id="TICKER:MSFT",
                    symbol_or_code="MSFT",
                    canonical_name="Microsoft Corp",
                    asset_class="EQUITY",
                    risk_class="single_stock",
                    stack_bucket="stocks",
                    is_scrapeable=True,
                    quote_symbol="MSFT",
                    quantity=50.0,
                    stmt_price=400.0,
                    market_value=20000.0,
                    value_date="2026-10-31",
                )
                tampered_pos = ParsedPosition(
                    security_id="CASH:BAD",
                    symbol_or_code="USD",
                    canonical_name="Tampered Cash",
                    asset_class="CASH",
                    risk_class="cash_fixed",
                    stack_bucket="cash",
                    is_scrapeable=False,
                    quote_symbol=None,
                    quantity=1.0,
                    stmt_price=1000.0,
                    market_value=1000.0,
                    value_date="2026-10-31",
                )
                # Simulate a buggy caller mutating risk_class post-construction before DB insert
                tampered_pos.risk_class = "single_stock"

                stmt = ParsedStatement(
                    doc_type="TEST_SNAPSHOT",
                    institution="Synthetic Bank",
                    account_id="ACCT-9999",
                    account_name="Test Account",
                    account_type="TAXABLE",
                    tax_status="TAXABLE",
                    display_order=1,
                    period_start="2026-10-01",
                    as_of_date="2026-10-31",
                    reported_total=21000.0,
                    positions=[good_pos, tampered_pos],
                )
                cand = CandidateFile(
                    path=bad_pdf,
                    rel_path=bad_pdf.name,
                    size=bad_pdf.stat().st_size,
                    birthtime=0.0,
                    mtime=0.0,
                    ctime=0.0,
                    sha256="deadbeef" * 8,
                    reason="TEST",
                )

                try:
                    apply_parsed_statement(db, stmt, cand, run_id=None)
                    self.fail("Expected InvalidHoldingClassificationError")
                except InvalidHoldingClassificationError:
                    db.conn.rollback()

                sec_ids = [
                    r["security_id"]
                    for r in db.conn.execute("SELECT security_id FROM securities").fetchall()
                ]
                self.assertEqual(sec_ids, ["CASH:SEED"], "Partial security insert must roll back cleanly")
                snap_cnt = db.conn.execute("SELECT COUNT(*) AS n FROM position_snapshots").fetchone()["n"]
                self.assertEqual(snap_cnt, 0, "Partial position snapshot must roll back cleanly")

    def test_3_tier_forecast_enricher_waterfall(self):
        with PortfolioDB(":memory:") as db:
            db.upsert_account(
                account_id="ACCT-01",
                institution="Test Custodian",
                account_name="Test Brokerage",
                account_type="BROKERAGE",
                tax_status="TAXABLE",
                display_order=1,
            )
            cand_dummy = CandidateFile(
                path=Path("/tmp/test_stmt.pdf"),
                rel_path="test_stmt.pdf",
                size=100,
                birthtime=0.0,
                mtime=0.0,
                ctime=0.0,
                sha256="11" * 32,
                reason="TEST",
            )
            positions = [
                # 1. Public 10Y stock (+12% steady CAGR)
                ParsedPosition(
                    security_id="TICKER:STEADY",
                    symbol_or_code="STEADY",
                    canonical_name="Steady Compounder Inc",
                    asset_class="EQUITY",
                    risk_class="single_stock",
                    stack_bucket="stocks",
                    is_scrapeable=True,
                    quote_symbol="STEADY",
                    quantity=100.0,
                    stmt_price=310.58,
                    reported_return_pct=0.0,
                    reported_return_window="1Y",
                    market_value=31058.0,
                    value_date="2026-10-31",
                ),
                # 2. Extreme bull outlier (+80% CAGR -> clamped to +25.0%)
                ParsedPosition(
                    security_id="TICKER:ROCKET",
                    symbol_or_code="ROCKET",
                    canonical_name="Rocket Crypto Trust",
                    asset_class="ETF",
                    risk_class="crypto_speculative",
                    stack_bucket="stocks",
                    is_scrapeable=True,
                    quote_symbol="ROCKET",
                    quantity=50.0,
                    stmt_price=500.0,
                    market_value=25000.0,
                    value_date="2026-10-31",
                ),
                # 3. Day-1 corporate spin-off (<180d history, no cost-basis age -> LOW benchmark + warning)
                ParsedPosition(
                    security_id="TICKER:NEWSPIN",
                    symbol_or_code="NEWSPIN",
                    canonical_name="New SpinOff Corp",
                    asset_class="EQUITY",
                    risk_class="single_stock",
                    stack_bucket="stocks",
                    is_scrapeable=True,
                    quote_symbol="NEWSPIN",
                    quantity=25.0,
                    stmt_price=40.0,
                    market_value=1000.0,
                    value_date="2026-10-31",
                ),
                # 4. Individual Muni bond with YTM=4.65% despite slight capital loss (DP-4: YTM wins!)
                ParsedPosition(
                    security_id="CUSIP:999999AA1",
                    symbol_or_code="999999AA1",
                    canonical_name="State Tax-Exempt Bond 5%",
                    asset_class="MUNI_BOND",
                    risk_class="fixed_income",
                    stack_bucket="munis",
                    is_scrapeable=False,
                    quote_symbol=None,
                    quantity=50000.0,
                    stmt_price=98.0,
                    market_value=49000.0,
                    cost_basis=50000.0,
                    acquisition_date="2023-10-31",
                    yield_pct=4.65,
                    value_date="2026-10-31",
                ),
                # 5. Buffered RILA annuity ($100k -> $133.1k over 3.0 years = +10.0% CAGR)
                ParsedPosition(
                    security_id="CONTRACT:RILA01",
                    symbol_or_code="RILA01",
                    canonical_name="Structured Buffer RILA",
                    asset_class="RILA_ANNUITY",
                    risk_class="hybrid_buffered",
                    stack_bucket="annuities",
                    is_scrapeable=False,
                    quote_symbol=None,
                    quantity=1.0,
                    stmt_price=133100.0,
                    market_value=133100.0,
                    cost_basis=100000.0,
                    initial_premium=100000.0,
                    issue_date="2023-10-31",
                    acquisition_date="2023-10-31",
                    value_date="2026-10-31",
                ),
                # 6. Contractual Cash Sweep (4.25% APY)
                ParsedPosition(
                    security_id="CASH:ACCT-01",
                    symbol_or_code="USD",
                    canonical_name="Bank Deposit Sweep",
                    asset_class="CASH",
                    risk_class="cash_fixed",
                    stack_bucket="cash",
                    is_scrapeable=False,
                    quote_symbol=None,
                    quantity=10000.0,
                    stmt_price=1.0,
                    market_value=10000.0,
                    credited_rate_pct=4.25,
                    value_date="2026-10-31",
                ),
            ]
            stmt = ParsedStatement(
                doc_type="TEST_SNAPSHOT",
                institution="Test Custodian",
                account_id="ACCT-01",
                account_name="Test Brokerage",
                account_type="BROKERAGE",
                tax_status="TAXABLE",
                display_order=1,
                period_start="2026-10-01",
                as_of_date="2026-10-31",
                reported_total=sum(p.market_value for p in positions),
                positions=positions,
            )
            apply_parsed_statement(db, stmt, cand_dummy, run_id=None)

            # Seed 10 years of weekly adj_close prices for SPY, STEADY (~12% CAGR), ROCKET (~80% CAGR),
            # and only 45 days for NEWSPIN.
            end_dt = date(2026, 10, 30)
            n_weeks = 520
            price_rows = []
            for w in range(n_weeks + 1):
                dt_iso = (end_dt - timedelta(days=(n_weeks - w) * 7)).isoformat()
                t_yrs = w / 52.0
                wig = 1.0 + 0.015 * math.sin(w * 0.7)
                spy_px = 100.0 * ((1.10) ** t_yrs) * wig
                steady_px = 100.0 * ((1.12) ** t_yrs) * (1.0 + 0.022 * math.sin(w * 0.7 + 0.1))
                rocket_px = 10.0 * ((1.80) ** t_yrs) * (1.0 + 0.05 * math.cos(w * 0.5))
                price_rows.append(("SPY", dt_iso, spy_px, spy_px, "2026-10-31T00:00:00Z"))
                price_rows.append(("STEADY", dt_iso, steady_px, steady_px, "2026-10-31T00:00:00Z"))
                price_rows.append(("ROCKET", dt_iso, rocket_px, rocket_px, "2026-10-31T00:00:00Z"))
            for w in range(6):
                dt_iso = (end_dt - timedelta(days=(6 - w) * 7)).isoformat()
                price_rows.append(("NEWSPIN", dt_iso, 40.0 + w * 0.5, 40.0 + w * 0.5, "2026-10-31T00:00:00Z"))
            db.conn.executemany(
                """
                INSERT OR REPLACE INTO market_prices
                    (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
                VALUES (?, ?, ?, ?, ?)
                """,
                price_rows,
            )
            db.conn.commit()

            cfg = {"principal_overrides": {"ACCT-01": {"principal": 1_000_000}}}
            summary = enrich_holding_forecasts(db, cfg=cfg, today_date=date(2026, 10, 31), sync_prices=False)
            self.assertEqual(summary["holdings_enriched"], 6)
            self.assertTrue(
                any("NEWSPIN" in w for w in summary["low_confidence_warnings"]),
                f"Expected LOW confidence warning for NEWSPIN, got {summary['low_confidence_warnings']}",
            )

            baselines = db.get_holding_forecasts_baseline()
            steady = baselines[("ACCT-01", "TICKER:STEADY")]
            self.assertEqual(steady["return_source"], "PUBLIC_10Y_TOTAL_RETURN")
            self.assertEqual(steady["confidence"], "HIGH")
            self.assertAlmostEqual(steady["projected_return_pct"], 12.0, delta=0.6)
            self.assertIsNotNone(steady["realized_vol_pct"])
            self.assertIsNotNone(steady["beta_spy"])

            evidence = json.loads(steady["growth_evidence_json"])
            self.assertEqual([entry["tier"] for entry in evidence], [1, 1, 2, 3])
            self.assertEqual(evidence[0]["projected_return_pct"], steady["projected_return_pct"])
            self.assertIn(str(end_dt - timedelta(weeks=n_weeks)), evidence[0]["source_detail"])
            self.assertIn("2026-10-30", evidence[0]["source_detail"])
            self.assertEqual(evidence[1]["projected_return_pct"], 0.0)
            self.assertIn("2026-10-31", evidence[1]["source_detail"])
            self.assertAlmostEqual(evidence[2]["projected_return_pct"], 10.0, delta=0.5)
            self.assertIn("SPY", evidence[2]["source_detail"])
            self.assertEqual(evidence[3]["projected_return_pct"], 8.5)

            rocket = baselines[("ACCT-01", "TICKER:ROCKET")]
            self.assertEqual(rocket["projected_return_pct"], 25.0, "DP-2: must clamp extreme bull CAGR to +25.0%")

            spinoff = baselines[("ACCT-01", "TICKER:NEWSPIN")]
            self.assertEqual(spinoff["return_source"], "PEER_MARKET_BENCHMARK")
            self.assertEqual(spinoff["confidence"], "MEDIUM")
            self.assertAlmostEqual(spinoff["projected_return_pct"], 10.0, delta=0.5)

            muni = baselines[("ACCT-01", "CUSIP:999999AA1")]
            self.assertEqual(muni["return_source"], "STATEMENT_YTM")
            self.assertEqual(muni["confidence"], "HIGH")
            self.assertEqual(muni["projected_return_pct"], 4.65)

            bond_evidence = json.loads(muni["growth_evidence_json"])
            bond_t1 = [e for e in bond_evidence if e["tier"] == 1]
            self.assertTrue(any("cost basis" in e["source_detail"] for e in bond_t1))
            bond_t3 = next(e for e in bond_evidence if e["tier"] == 3)
            self.assertEqual(bond_t3["projected_return_pct"], 3.5)

            rila = baselines[("ACCT-01", "CONTRACT:RILA01")]
            self.assertEqual(rila["return_source"], "STATEMENT_INCEPTION_CAGR")
            self.assertEqual(rila["confidence"], "HIGH")
            self.assertAlmostEqual(rila["projected_return_pct"], 10.0, delta=0.15)
            rila_evidence = json.loads(rila["growth_evidence_json"])
            self.assertEqual([e["tier"] for e in rila_evidence], [1, 2, 3])
            self.assertIn("60/40", rila_evidence[1]["source_detail"])

            cash = baselines[("ACCT-01", "CASH:ACCT-01")]
            self.assertEqual(cash["return_source"], "CONTRACT_CREDITED_RATE")
            self.assertEqual(cash["projected_return_pct"], 4.25)
            self.assertEqual(cash["realized_vol_pct"], 0.0)

            # A newer statement may omit the historical return printed in the previous one.
            # Its date and explicit zero must survive inside Tier 1 even if the security metadata changes.
            db.conn.execute("""
                INSERT INTO position_snapshots(account_id, as_of_date, security_id, quantity, stmt_market_value,
                                               status, source_hash, details_json)
                SELECT account_id, '2026-11-01', security_id, quantity, stmt_market_value, status, source_hash, '{}'
                  FROM position_snapshots WHERE security_id='TICKER:STEADY'
            """)
            db.conn.execute("UPDATE securities SET metadata_json='{}' WHERE security_id='TICKER:STEADY'")
            enrich_holding_forecasts(db, cfg=cfg, today_date=date(2026, 11, 1), sync_prices=False)
            recent = db.get_holding_forecasts_baseline()[("ACCT-01", "TICKER:STEADY")]
            recent_ev = json.loads(recent["growth_evidence_json"])
            old_statement = [e for e in recent_ev if e["tier"] == 1][1]
            self.assertEqual(old_statement["projected_return_pct"], 0.0)
            self.assertIn("2026-10-31", old_statement["source_detail"])

            db.conn.execute("DELETE FROM market_prices WHERE quote_symbol = ? AND price_date < ?",
                            ("STEADY", str(end_dt - timedelta(weeks=52))))
            enrich_holding_forecasts(db, cfg=cfg, today_date=date(2026, 10, 31), sync_prices=False)
            short = db.get_holding_forecasts_baseline()[("ACCT-01", "TICKER:STEADY")]
            first, last = [price_rows[3 * w + 1][3] for w in (n_weeks - 52, n_weeks)]
            observed = ((last / first) ** (365.2425 / 364) - 1) * 100
            self.assertEqual(short["return_source"], "PUBLIC_SHORT_TOTAL_RETURN")
            self.assertAlmostEqual(short["projected_return_pct"], observed, delta=0.01)

    def test_sleeve_constituent_bidirectional_rollup_and_realized_vol_override(self):
        from exporters.view_model import _rollup_sleeve_forecast_metrics

        def child(value, tier, rate):
            return {"current_value": value, "growth_evidence": [
                {"tier": tier, "projected_return_pct": rate, "source_detail": "Statement dated 2026-10-31"},
                {"tier": 3, "projected_return_pct": 4.5, "source_detail": "Class assumption"},
            ]}

        children = [child(75, 1, 4.6), child(25, 1, 4.4)]
        def group():
            return _rollup_sleeve_forecast_metrics(children, "fixed_income", date(2026, 11, 18))

        evidence = group()["growth_evidence"]
        self.assertEqual([(e["tier"], e["projected_return_pct"]) for e in evidence], [(1, 4.55), (3, 4.5)])
        self.assertIn("2026-10-31", evidence[0]["source_detail"])
        children[1] = child(25, 2, 4.4)
        mixed = group()
        self.assertEqual(mixed["default_growth_pct"], 4.6)
        self.assertEqual([e["tier"] for e in mixed["growth_evidence"]], [1, 2, 3])
        self.assertIn("75.0%", mixed["growth_evidence"][0]["source_detail"])
        children.pop(0)
        self.assertEqual(group()["growth_evidence"][0]["tier"], 2)
        children.clear()
        self.assertEqual(group()["growth_evidence"][0]["tier"], 3)

        sleeve_asset = {
            "id": "uma_sleeve",
            "forecast_key": "uma_sleeve",
            "bucket": "non_goog",
            "rate_model": "slider",
            "default_growth_pct": 14.0,
            "ref_growth_pct": 14.0,
            "default_risk_class": "broad_equity",
            "ingested_risk_class": "broad_equity",
            "realized_vol_pct": 19.5,
            "beta_spy": 1.10,
            "corr_spy": 0.96,
            "constituent_forecasts": [
                {
                    "forecast_key": "ACCT:TICKER:AAA",
                    "weight": 0.60,
                    "default_growth_pct": 10.0,
                    "default_risk_class": "single_stock",
                },
                {
                    "forecast_key": "ACCT:TICKER:BBB",
                    "weight": 0.40,
                    "default_growth_pct": 20.0,
                    "default_risk_class": "single_stock",
                },
            ],
        }

        # 1. Untouched sparse `holdingForecasts: {}` -> inherits 14.0%
        f_base = resolve_asset_forecast(sleeve_asset, {"holdingForecasts": {}})
        self.assertAlmostEqual(f_base["growth_pct"], 14.0, places=2)
        self.assertFalse(f_base["risk_overridden"])

        # Reset must survive canonicalization and a later change to the reference,
        # even when a child remains customized or a legacy slider alias is present.
        from settings import canonicalize_model_settings
        settings = canonicalize_model_settings({
            "futureNonGoogGrowthPct": 2,
            "holdingForecasts": {"uma_sleeve": {"use_reference": True, "growth_pct": 10},
                                 "ACCT:TICKER:BBB": {"growth_pct": 5}},
        }, cfg={}, sim_assets=[sleeve_asset])
        self.assertEqual(resolve_asset_forecast(sleeve_asset, settings)["growth_pct"], 14)
        self.assertEqual(settings["holdingForecasts"]["ACCT:TICKER:BBB"]["growth_pct"], 5)
        reset = canonicalize_model_settings({"holdingForecasts": {}, "futureNonGoogGrowthPct": 2}, cfg={})
        self.assertEqual(reset["holdingForecasts"], {})
        self.assertEqual(resolve_asset_forecast(sleeve_asset, reset)["growth_pct"], 14)
        dated = _rollup_sleeve_forecast_metrics([child(50, 1, 4.6)], "fixed_income", date(2026, 11, 18),
            group={"principal_date": "2020-01-01", "asof_date": "2026-10-31", "cagr_pct": 0.0, "cagr_method": "Account inception"})
        self.assertEqual([e["tier"] for e in dated["growth_evidence"]], [1, 1, 3])
        self.assertEqual(dated["growth_evidence"][1]["projected_return_pct"], 0)
        self.assertIn("2020-01-01", dated["growth_evidence"][1]["source_detail"])

        # 2. Override child BBB to 5.0% -> parent rolls up dynamically to 0.6*10 + 0.4*5 = 8.0%
        f_child = resolve_asset_forecast(
            sleeve_asset,
            {"holdingForecasts": {"ACCT:TICKER:BBB": {"growth_pct": 5.0, "risk_class": "single_stock"}}},
        )
        self.assertAlmostEqual(f_child["growth_pct"], 8.0, places=2)

        # 3a. Mixed parent + child override: un-overridden AAA inherits parent 6.5%, BBB keeps 5.0% -> 0.6*6.5 + 0.4*5.0 = 5.9%
        f_mixed = resolve_asset_forecast(
            sleeve_asset,
            {
                "holdingForecasts": {
                    "ACCT:TICKER:BBB": {"growth_pct": 5.0, "risk_class": "single_stock"},
                    "uma_sleeve": {
                        "growth_pct": 6.5,
                        "risk_class": "hybrid_buffered",
                        "risk_overridden": True,
                    },
                }
            },
        )
        self.assertAlmostEqual(f_mixed["growth_pct"], 5.9, places=2)
        self.assertEqual(f_mixed["risk_class"], "hybrid_buffered")
        self.assertTrue(f_mixed["risk_overridden"])

        # 3b. Parent sleeve override alone (after clearing child overrides) -> exact 6.5%
        f_parent = resolve_asset_forecast(
            sleeve_asset,
            {
                "holdingForecasts": {
                    "uma_sleeve": {
                        "growth_pct": 6.5,
                        "risk_class": "hybrid_buffered",
                        "risk_overridden": True,
                    },
                }
            },
        )
        self.assertAlmostEqual(f_parent["growth_pct"], 6.5, places=2)
        self.assertEqual(f_parent["risk_class"], "hybrid_buffered")
        self.assertTrue(f_parent["risk_overridden"])

        # 4. Market Correction peak-to-trough shocks cover all variable classes while preserving contractual cash_fixed
        shocks_gfc = stress_year1_shocks_by_risk_class({"simStressScenario": "gfc2008", "simCustomShockPct": -30.0})
        self.assertNotIn("cash_fixed", shocks_gfc)
        self.assertAlmostEqual(shocks_gfc["fixed_income"], 0.05)
        self.assertAlmostEqual(shocks_gfc["hybrid_buffered"], -0.25)
        self.assertAlmostEqual(shocks_gfc["broad_equity"], -0.50)
        self.assertAlmostEqual(shocks_gfc["real_diversifier"], 0.05)
        self.assertAlmostEqual(shocks_gfc["single_stock"], -0.60)
        self.assertAlmostEqual(shocks_gfc["crypto_speculative"], -0.72)

        shocks_2022 = stress_year1_shocks_by_risk_class({"simStressScenario": "rates2022"})
        self.assertAlmostEqual(shocks_2022["fixed_income"], -0.13)
        self.assertAlmostEqual(shocks_2022["broad_equity"], -0.24)
        self.assertAlmostEqual(shocks_2022["single_stock"], -0.38)

    def test_7_risk_classes_python_js_parity(self):
        from tests.support import env as _env_support, pipeline as _pipe_support, roots as _roots_support

        with tempfile.TemporaryDirectory() as _fx_td:
            _fx_root = Path(_fx_td)
            _roots_support.assemble_root(_fx_root, ["p2_equity_comp_rsu"])
            with _env_support.pointed_at(_fx_root):
                _pipe_support.clear_payload_cache()
                payload = _pipe_support.build_payload("p2_equity_comp_rsu")
                html = _pipe_support.rendered_html("p2_equity_comp_rsu")
                _pipe_support.clear_payload_cache()
            index_path = _fx_root / "index.html"
            index_path.write_text(html, encoding="utf-8")
            self._run_7_risk_classes_parity_checks(payload, index_path)

    def _run_7_risk_classes_parity_checks(self, payload: dict, index_path: Path):
        def_snap = default_settings_snapshot()
        self.assertEqual(def_snap["holdingForecasts"], {})

        # Synthesize hypothetical assets spanning the newly added risk classes + constituent child override
        hyps = [
            {
                "id": "hyp_gold",
                "label": "Hypothetical Gold ETF",
                "institution": "Custodian",
                "real_product_ref": "GLD",
                "initial_value": 250000.0,
                "growth_pct": 5.5,
                "risk_class": "real_diversifier",
                "expense_ratio_pct": 0.40,
                "tax_wrapper": "taxable_stock",
                "unlock_ym": None,
            },
            {
                "id": "hyp_btc",
                "label": "Hypothetical Bitcoin ETF",
                "institution": "Custodian",
                "real_product_ref": "IBIT",
                "initial_value": 150000.0,
                "growth_pct": 12.0,
                "risk_class": "crypto_speculative",
                "expense_ratio_pct": 0.25,
                "tax_wrapper": "taxable_stock",
                "unlock_ym": None,
            },
            {
                "id": "hyp_buf",
                "label": "Hypothetical Buffer ETF",
                "institution": "Custodian",
                "real_product_ref": "BUFR",
                "initial_value": 300000.0,
                "growth_pct": 7.0,
                "risk_class": "hybrid_buffered",
                "expense_ratio_pct": 0.50,
                "tax_wrapper": "taxable_stock",
                "unlock_ym": None,
            },
        ]

        # Also test overriding one constituent inside a multi-constituent sleeve AND setting risk_overridden on one bucket
        assets = payload.get("retirement_sim_assets") or []
        sleeve = next((a for a in assets if len(a.get("constituent_forecasts") or []) > 2), None)
        hf_overrides = {}
        if sleeve:
            first_child_key = sleeve["constituent_forecasts"][0]["forecast_key"]
            hf_overrides[first_child_key] = {"growth_pct": -5.0, "risk_class": "single_stock"}
        if assets:
            hf_overrides[assets[0]["id"]] = {
                "growth_pct": 9.5,
                "risk_class": "hybrid_buffered",
                "risk_overridden": True,
            }

        base_settings = canonicalize_model_settings(
            {
                "holdingForecasts": hf_overrides,
                "simAnnualBurn": 65000,
                "simInflationPct": 3.0,
                "simHypotheticalAssets": hyps,
                "simAssetOrder": [*[a["id"] for a in assets], "hyp_gold", "hyp_btc", "hyp_buf"],
            }
        )

        # (A) Single-path parity with sparse ingested defaults + child constituent roll-up + 7 risk classes
        eff_sp = build_effective_settings(payload, base_settings, overrides={"simActiveModel": "single_path"})
        js_sp = _invoke_js_harness(index_path, eff_sp, "run")
        py_sp = run_simulation(payload, eff_sp)
        self.assertAlmostEqual(
            py_sp["milestones"]["investor_1_milestone"]["real_today_usd"],
            js_sp["ms_real"],
            delta=1.5,
        )

        # (B) Early Crash (`stress` gfc2008) parity across all 7 risk classes
        eff_st = build_effective_settings(
            payload,
            base_settings,
            overrides={"simActiveModel": "stress", "simStressScenario": "gfc2008"},
        )
        js_st = _invoke_js_harness(index_path, eff_st, "run")
        py_st = run_simulation(payload, eff_st)
        self.assertAlmostEqual(
            py_st["milestones"]["investor_1_milestone"]["real_today_usd"],
            js_st["ms_real"],
            delta=1.5,
        )

        # (C) Seeded Monte Carlo (`seed=7`) parity with realized_vol_pct + risk_overridden + 7 risk classes
        mc_opts = {
            "singleStockVolPct": 36.2,
            "broadEquityVolPct": 14.5,
            "singleStockCorr": 0.52,
            "returnModel": "normal",
            "simulations": 120,
        }
        js_mc = _invoke_js_harness(index_path, {**eff_sp, "__mc": mc_opts, "__seed": 7}, "montecarlo")
        py_mc = run_monte_carlo(payload, eff_sp, mc=mc_opts, seed=7)
        self.assertAlmostEqual(js_mc["ms_real"]["p50"], py_mc["ms_real"]["p50"], delta=2.0)
        self.assertAlmostEqual(js_mc["ms_real"]["p25"], py_mc["ms_real"]["p25"], delta=2.0)
        self.assertAlmostEqual(js_mc["success_to_ms_pct"], py_mc["success_to_ms_pct"], delta=0.01)

        # (D) Historical Cohorts parity across all 7 risk classes
        js_coh = _invoke_js_harness(
            index_path,
            {**eff_sp, "__opts": {"centerMode": "anchor", "inflationMode": "real_cpi", "spotlight": [1966]}},
            "cohorts",
        )
        py_coh = run_historical_cohorts(
            payload,
            eff_sp,
            opts={"center_mode": "anchor", "inflation_mode": "real_cpi", "spotlight": [1966]},
        )
        self.assertAlmostEqual(js_coh["band"][20]["real"]["p50"], py_coh["band"][20]["real"]["p50"], delta=2.0)

    def test_plugin_checksum_custom_discovery_and_cli(self):
        class DummyPlugin(StatementPlugin):
            plugin_id = "builtin_dummy"
            institution_name = "Dummy FI"
            supported_extensions = (".txt",)

            def detect(self, ctx: IngestContext) -> float:
                return 0.90 if "DUMMY_HEADER" in ctx.full_text else 0.0

            def parse(self, ctx: IngestContext) -> ParsedStatement:
                return ParsedStatement(
                    doc_type="DUMMY",
                    institution="Dummy FI",
                    account_id="ACC-1",
                    account_name="Dummy",
                    account_type="TAXABLE",
                    tax_status="TAXABLE",
                    display_order=1,
                    period_start="2026-10-01",
                    as_of_date="2026-10-31",
                    reported_total=1000.00,
                    positions=[
                        ParsedPosition(
                            security_id="CASH:ACC-1",
                            symbol_or_code="USD",
                            canonical_name="Cash",
                            asset_class="CASH",
                            risk_class="cash_fixed",
                            stack_bucket="cash",
                            is_scrapeable=False,
                            quote_symbol=None,
                            quantity=1000.0,
                            stmt_price=1.0,
                            market_value=1000.00,
                            credited_rate_pct=4.0,
                            value_date="2026-10-31",
                        )
                    ],
                )

        plug = DummyPlugin()
        ctx = IngestContext(file_path=Path("/tmp/d.txt"), rel_path="d.txt", sha256="00" * 32, pages=["DUMMY_HEADER"])
        stmt_ok = plug.parse(ctx)
        ok, comp, diff = plug.verify_checksum(stmt_ok, ctx, strict=True)
        self.assertTrue(ok)
        self.assertAlmostEqual(comp, 1000.0)
        self.assertAlmostEqual(diff, 0.0)

        stmt_bad = plug.parse(ctx)
        stmt_bad.reported_total = 1050.00
        with self.assertRaises(StatementChecksumError):
            plug.verify_checksum(stmt_bad, ctx, strict=True)

        # DP-7 (7A): Custom plugin wins tie-break on identical detection score
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            sample_stmt = pdir / "sample_custom.txt"
            sample_stmt.write_text("DUMMY_HEADER\nBalance: $2,500.00\n", encoding="utf-8")

            custom_py = pdir / "custom_dummy_plugin.py"
            custom_py.write_text(
                """
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedPosition, ParsedStatement

class CustomDummyPlugin(StatementPlugin):
    plugin_id = "custom_dummy"
    institution_name = "Custom FI"
    supported_extensions = (".txt",)

    def detect(self, ctx: IngestContext) -> float:
        return 0.90 if "DUMMY_HEADER" in ctx.full_text else 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        return ParsedStatement(
            doc_type="CUSTOM_DUMMY",
            institution="Custom FI",
            account_id="CUST-01",
            account_name="Custom Account",
            account_type="TAXABLE",
            tax_status="TAXABLE",
            display_order=1,
            period_start="2026-10-01",
            as_of_date="2026-10-31",
            reported_total=2500.00,
            positions=[
                ParsedPosition(
                    security_id="CASH:CUST-01",
                    symbol_or_code="USD",
                    canonical_name="Custom Sweep",
                    asset_class="CASH",
                    risk_class="cash_fixed",
                    stack_bucket="cash",
                    is_scrapeable=False,
                    quote_symbol=None,
                    quantity=2500.0,
                    stmt_price=1.0,
                    market_value=2500.00,
                    credited_rate_pct=4.5,
                    value_date="2026-10-31",
                )
            ],
        )
""",
                encoding="utf-8",
            )

            reg = PluginRegistry()
            reg.register(DummyPlugin(), is_custom=False)
            load_custom_plugins_from_dir(reg, plugins_dir=pdir)

            winner, best_sc = reg.detect_plugin(
                IngestContext(file_path=sample_stmt, rel_path=sample_stmt.name, sha256="aa" * 32)
            )
            self.assertIsNotNone(winner)
            self.assertEqual(best_sc, 0.90)
            self.assertEqual(winner.plugin_id, "custom_dummy", "Custom plugin must win score ties over built-in")

            # Verify CLI `inspect-statement --json` and `test-plugin --offline --json`
            cli_env = {**os.environ, "ADVISORBOT_ALLOW_CUSTOM_PLUGINS": "1"}
            insp = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "holdings.py"),
                    "inspect-statement",
                    str(sample_stmt),
                    "--plugins-dir",
                    str(pdir),
                    "--json",
                ],
                env=cli_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(insp.returncode, 0, msg=insp.stderr)
            insp_json = json.loads(insp.stdout)
            self.assertEqual(insp_json["matched_plugin"], "custom_dummy")

            tp = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "holdings.py"),
                    "test-plugin",
                    str(sample_stmt),
                    "--plugins-dir",
                    str(pdir),
                    "--offline",
                    "--json",
                ],
                env=cli_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(tp.returncode, 0, msg=tp.stderr)
            tp_json = json.loads(tp.stdout)
            self.assertTrue(tp_json["ok"])
            self.assertEqual(tp_json["plugin_id"], "custom_dummy")
            self.assertTrue(tp_json["checksum_ok"])
            self.assertEqual(tp_json["holdings"][0]["projected_return_pct"], 4.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
