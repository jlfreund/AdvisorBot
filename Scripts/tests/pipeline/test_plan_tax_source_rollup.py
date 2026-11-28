"""Tests for multi-source employer retirement plan ingestion and collapsible strategy rollups."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from db.database import PortfolioDB
from exporters.view_model import build_report_payload
from ingest.pipeline import ingest_statements
from privacy import apply_demo_projection_to_payload, build_translator, sync_privacy_registry
from valuation.engine import build_and_store_timeline
from valuation.forecast_enricher import enrich_holding_forecasts
from valuation.simulator import query_positions, run_simulation


class MultiSourcePlanRollupTest(unittest.TestCase):
    """Multi-source 401(k)/403(b) statements collapse on Screen 1 while staying tax-split in Simulator."""

    def test_canonical_json_tax_sources_collapse_strategy_and_preserve_sim_wrappers(self):
        """Multi-source plan statements collapse into one expandable Strategy row while keeping tax-split Simulator buckets."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdir = root / "Portfolios" / "test_hh"
            stmt_dir = pdir / "Statements"
            stmt_dir.mkdir(parents=True, exist_ok=True)
            (root / "Portfolios" / "_active.json").write_text(
                json.dumps({"active_portfolio_id": "test_hh"}), encoding="utf-8"
            )

            cfg = {
                "version": 1,
                "household_name": "Rivera Household",
                "reference_annual_burn": 100000,
                "retirement_annual_burn": 100000,
                "investors": [
                    {
                        "id": "pat",
                        "name": "Pat Rivera",
                        "birth_date": "1968-07-01",
                        "milestone_age": 85,
                        "is_rmd_owner": True,
                        "ssn": {"fra_age": 67, "default_claim_age": 67, "annual_earnings": {}},
                    }
                ],
                "tax_rates": {
                    "state_code": "CA",
                    "state_rate": 0.133,
                    "married_filing_jointly": True,
                },
                "accounts": {},
            }
            (pdir / "project.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            stmt_payload = {
                "format": "advisorbot.statement/1",
                "institution": "Summit Retirement",
                "account_id": "PLAN990812",
                "account_name": "Pat Rivera — Employer 401(k) Plan",
                "account_type": "401(k) Savings Plan",
                "tax_status": "401K",
                "period_start": "2026-07-01",
                "as_of_date": "2026-09-30",
                "reported_total": 300000.00,
                "tax_sources": [
                    {
                        "source_id": "PRETAX",
                        "tax_status": "401K",
                        "opening_balance": 175000.00,
                        "closing_balance": 180000.00,
                        "cost_basis": 140000.00,
                    },
                    {
                        "source_id": "ROTH",
                        "tax_status": "ROTH_401K",
                        "opening_balance": 115000.00,
                        "closing_balance": 120000.00,
                        "cost_basis": 95000.00,
                    },
                ],
                "positions": [
                    {
                        "security_id": "CIT:SUMMIT_TR2035",
                        "symbol_or_code": "STR2035",
                        "canonical_name": "Summit Target Retirement 2035 Trust",
                        "asset_class": "BALANCED_FUND",
                        "risk_class": "hybrid_buffered",
                        "stack_bucket": "mutual_funds",
                        "is_scrapeable": False,
                        "quantity": 1.0,
                        "stmt_price": 300000.00,
                        "market_value": 300000.00,
                        "reported_return_pct": 9.5,
                        "reported_return_window": "SINCE_INCEPTION",
                    }
                ],
            }
            (stmt_dir / "plan_q3.statement.json").write_text(
                json.dumps(stmt_payload, indent=2), encoding="utf-8"
            )

            db_path = pdir / "portfolio.sqlite"
            today = date(2026, 11, 27)
            with PortfolioDB(db_path) as db:
                ing = ingest_statements(stmt_dir, db, cfg)
                self.assertEqual(ing.files_ingested, 1)
                self.assertEqual(ing.errors, [])
                build_and_store_timeline(
                    db=db,
                    cfg=cfg,
                    start_date=date(2026, 1, 1),
                    today_date=today,
                    forecast_end_date=date(2027, 12, 31),
                    force_price_sync=False,
                    incremental_only=False,
                )
                enrich_holding_forecasts(db=db, cfg=cfg, today_date=today, sync_prices=False)
                payload = build_report_payload(db=db, cfg=cfg, today_date=today, active_portfolio_id="test_hh")

            # 1. Screen 1 Strategy View collapses both slices into ONE expandable parent row
            strat = payload["holdings_by_strategy"]
            self.assertEqual(len(strat), 1)
            parent_row = strat[0]
            self.assertEqual(parent_row["group_id"], "rollup_plan_PLAN990812_str2035")
            self.assertAlmostEqual(parent_row["current_value"], 300000.00, places=2)
            self.assertAlmostEqual(parent_row["principal_val"], 235000.00, places=2)
            self.assertEqual(len(parent_row["children"]), 2)
            self.assertEqual(parent_row["forecast_key"], "rollup_plan_PLAN990812_str2035")

            # 2. Screen 3 Retirement Simulator unpacks into separate Pre-Tax (ORDINARY_FULL) and Roth (TAX_FREE) buckets
            sim_assets = {a["id"]: a for a in payload["retirement_sim_assets"]}
            self.assertIn("str2035_PLAN990812_PRETAX", sim_assets)
            self.assertIn("str2035_PLAN990812_ROTH", sim_assets)
            pre_b = sim_assets["str2035_PLAN990812_PRETAX"]
            roth_b = sim_assets["str2035_PLAN990812_ROTH"]
            self.assertEqual(pre_b["tax_wrapper"], "401K")
            self.assertEqual(pre_b["tax_model"], "ORDINARY_FULL")
            self.assertAlmostEqual(pre_b["current_value"], 180000.00, places=2)
            self.assertEqual(roth_b["tax_wrapper"], "ROTH_401K")
            self.assertEqual(roth_b["tax_model"], "TAX_FREE")
            self.assertAlmostEqual(roth_b["current_value"], 120000.00, places=2)
            self.assertIn("rollup_plan_PLAN990812_str2035", pre_b["parent_forecast_keys"])
            self.assertIn("rollup_plan_PLAN990812_str2035", roth_b["parent_forecast_keys"])

            # 3. Overriding the collapsible parent row's forecast propagates to BOTH simulator buckets
            sim_zero = run_simulation(
                payload,
                {
                    "simAnnualBurn": 0,
                    "simInflationPct": 0.0,
                    "simIncludeSSN": False,
                    "holdingForecasts": {
                        "rollup_plan_PLAN990812_str2035": {"growth_pct": 0.0, "risk_class": "hybrid_buffered"}
                    },
                },
            )
            sim_ten = run_simulation(
                payload,
                {
                    "simAnnualBurn": 0,
                    "simInflationPct": 0.0,
                    "simIncludeSSN": False,
                    "holdingForecasts": {
                        "rollup_plan_PLAN990812_str2035": {"growth_pct": 10.0, "risk_class": "hybrid_buffered"}
                    },
                },
            )
            self.assertGreater(
                sim_ten["bars"][12]["assetVals"]["str2035_PLAN990812_PRETAX"],
                sim_zero["bars"][12]["assetVals"]["str2035_PLAN990812_PRETAX"] + 15000.0,
            )
            self.assertGreater(
                sim_ten["bars"][12]["assetVals"]["str2035_PLAN990812_ROTH"],
                sim_zero["bars"][12]["assetVals"]["str2035_PLAN990812_ROTH"] + 10000.0,
            )

            # 4. Flattened `query_positions` returns both slices cleanly
            flat_pos = query_positions(payload, symbol="STR2035")
            self.assertEqual(len(flat_pos), 2)
            self.assertAlmostEqual(sum(p["current_value"] for p in flat_pos), 300000.00, places=2)

            # 5. Privacy Shield & Demo Mode mask the base plan ID `PLAN990812` with zero leaks
            from tests.support import env as _env_support

            with _env_support.pointed_at(root):
                pcfg = sync_privacy_registry("test_hh")
                pcfg["ai_shield"] = {"mode": "identity_and_values", "scale_factor": 0.5}
                tr = build_translator(pcfg, portfolio_id="test_hh")
                wire = tr.to_alias(payload["holdings_by_strategy"])
                self.assertNotIn("PLAN990812", json.dumps(wire))
                demo_out = apply_demo_projection_to_payload(payload, pcfg)
                self.assertNotIn("PLAN990812", json.dumps(demo_out["holdings_by_strategy"]))


if __name__ == "__main__":
    unittest.main()
