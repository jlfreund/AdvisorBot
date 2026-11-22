#!/usr/bin/env python3
"""Regression and 1:1 Python-JS parity tests for Pre-Retirement Work, Contributions & Staggered Retirement."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = ROOT / "Scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from config import auto_retire_elapsed_work_plan, sanitize_work_plan, validate_portfolio_config  # noqa: E402
from privacy.demo_projector import DemoTranslator  # noqa: E402
from privacy.translator import PrivacyTranslator  # noqa: E402
from settings import canonicalize_model_settings  # noqa: E402
from tests.support import env as _env_support, pipeline as _pipe_support, roots as _roots_support  # noqa: E402
from valuation.simulator import (  # noqa: E402
    build_effective_settings,
    optimize_sim_asset_order,
    run_historical_cohorts,
    run_monte_carlo,
    run_simulation,
)

HARNESS = SCRIPTS / "tests" / "parity_harness.mjs"


def _make_base_valid_cfg() -> dict:
    return {
        "version": 1,
        "household_name": "Synthetic Work Plan Household",
        "reference_annual_burn": 120000,
        "investors": [
            {
                "id": "alex",
                "name": "Alex Rivera",
                "birth_date": "1970-06",
                "milestone_age": 85,
                "is_rmd_owner": True,
                "ssn": {"fra_age": 67, "default_claim_age": 67},
            },
            {
                "id": "blair",
                "name": "Blair Rivera",
                "birth_date": "1972-06",
                "milestone_age": 88,
                "is_rmd_owner": False,
                "ssn": {"fra_age": 67, "default_claim_age": 67},
            },
        ],
        "beneficiaries": [
            {"id": "casey", "name": "Casey Rivera", "birth_date": "2004-03"},
        ],
        "tax_rates": {
            "state_code": "WA",
            "state_rate": 0.0,
            "ltcg_top": 0.238,
            "ordinary_top": 0.408,
        },
    }


def _make_synthetic_payload(*, today_iso: str = "2026-06-01") -> dict:
    return {
        "today_iso": today_iso,
        "reference_annual_burn": 120000,
        "project": {
            "household_name": "Synthetic Work Plan Household",
            "primary_equity_symbol": "ACME",
            "symbols": ["ACME"],
            "investors": [
                {
                    "id": "alex",
                    "name": "Alex",
                    "birth_date": "1970-06",
                    "milestone_age": 85,
                    "is_rmd_owner": True,
                },
                {
                    "id": "blair",
                    "name": "Blair",
                    "birth_date": "1972-06",
                    "milestone_age": 88,
                    "is_rmd_owner": False,
                },
            ],
            "beneficiaries": [],
            "tax_rates": {
                "state_code": "FED",
                "state_rate": 0.0,
                "fed_ltcg_top": 0.20,
                "niit_rate": 0.038,
                "fed_ordinary_top": 0.37,
                "ltcg_top": 0.238,
                "ordinary_top": 0.408,
            },
        },
        "ssn_model": {
            "default_cola_pct": 0.0,
            "effective_ssn_tax_rate": 0.0,
            "investors": [
                {
                    "id": "alex",
                    "name": "Alex",
                    "birth_year": 1970,
                    "birth_month": 6,
                    "birth_day": 1,
                    "milestone_age": 85,
                    "rmd_owner": True,
                    "claim_setting_key": "ssnAlexClaimAge",
                    "default_claim_age": 67,
                    "pia_monthly_2026": 0,
                },
                {
                    "id": "blair",
                    "name": "Blair",
                    "birth_year": 1972,
                    "birth_month": 6,
                    "birth_day": 1,
                    "milestone_age": 88,
                    "rmd_owner": False,
                    "claim_setting_key": "ssnBlairClaimAge",
                    "default_claim_age": 67,
                    "pia_monthly_2026": 0,
                },
            ],
        },
        "retirement_sim_assets": [
            {
                "id": "acct_HB_7001",
                "forecast_key": "acct_HB_7001",
                "default_rank": 1,
                "title": "Taxable Equity Index",
                "short_title": "Taxable Index",
                "bucket": "broad_equity",
                "current_value": 500000,
                "cost_basis": 400000,
                "tax_wrapper": "TAXABLE",
                "tax_model": "LTCG_POOL",
                "tax_rate": 0.20,
                "default_growth_pct": 0.0,
                "ref_growth_pct": 0.0,
                "default_risk_class": "broad_equity",
            },
            {
                "id": "acct_HB_7002",
                "forecast_key": "acct_HB_7002",
                "default_rank": 2,
                "title": "Pre-Tax 401k",
                "short_title": "Pre-Tax 401k",
                "bucket": "retirement",
                "current_value": 200000,
                "cost_basis": 0,
                "tax_wrapper": "401K",
                "tax_model": "ORDINARY_FULL",
                "tax_rate": 0.30,
                "owner_birth_year": 1970,
                "rmd_owner_index": 0,
                "default_growth_pct": 0.0,
                "ref_growth_pct": 0.0,
                "default_risk_class": "broad_equity",
            },
            {
                "id": "acct_HB_7003",
                "forecast_key": "acct_HB_7003",
                "default_rank": 3,
                "title": "Roth 401k",
                "short_title": "Roth 401k",
                "bucket": "retirement",
                "current_value": 100000,
                "cost_basis": 80000,
                "tax_wrapper": "ROTH_401K",
                "tax_model": "TAX_FREE",
                "tax_rate": 0.0,
                "owner_birth_year": 1970,
                "default_growth_pct": 0.0,
                "ref_growth_pct": 0.0,
                "default_risk_class": "broad_equity",
            },
            {
                "id": "ann_HB_7004",
                "forecast_key": "ann_HB_7004",
                "default_rank": 4,
                "title": "Locked Fixed MYGA",
                "short_title": "Locked MYGA",
                "bucket": "fixed_annuities",
                "current_value": 100000,
                "cost_basis": 100000,
                "tax_wrapper": "NON_QUAL_ANNUITY",
                "tax_model": "ANNUITY_LIFO",
                "rate_model": "fixed",
                "annual_rate_pct": 0.0,
                "maturity_iso": "2035-06-01",
                "default_growth_pct": 0.0,
                "ref_growth_pct": 0.0,
                "default_risk_class": "cash_fixed",
            },
        ],
    }


def _invoke_js_harness(index_path: Path, cfg: dict, mode: str = "run"):
    with tempfile.TemporaryDirectory() as td:
        in_fp = Path(td) / "in.json"
        out_fp = Path(td) / "out.json"
        in_fp.write_text(json.dumps(cfg), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(HARNESS), str(index_path), str(in_fp), mode, str(out_fp)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0 or not out_fp.exists():
            raise AssertionError(f"JS harness ({mode}) failed rc={proc.returncode}: {proc.stderr}")
        return json.loads(out_fp.read_text(encoding="utf-8"))


class PreRetirementWorkTest(unittest.TestCase):
    """Domain, tax-wrapper, privacy, and JS parity tests for pre-retirement work plans."""

    def test_work_plan_validation_and_zero_pii_sanitization(self):
        """Sanitizer strips free-text employer PII and validator enforces Retirement Age <= Milestone Age."""
        cleaned = sanitize_work_plan(
            {
                "is_working": True,
                "default_retirement_age": 92,
                "covers_full_burn": False,
                "annual_budget_offset": 75000,
                "employer_name": "Acme Megacorp Inc",
                "job_title": "VP Engineering",
                "contributions": [
                    {
                        "target_asset_id": "acct_HB_7003",
                        "employee_annual": 24500,
                        "employer_match_annual": 12000,
                        "match_target_asset_id": "acct_HB_7002",
                        "age_50_catchup": True,
                        "employer_ein": "EIN_REDACTED_TEST",
                    },
                    {
                        "target_asset_id": "DROP TABLE; unsafe spaces!",
                        "employee_annual": 0,
                        "employer_match_annual": 0,
                    },
                ],
            },
            milestone_age=85,
            clamp_to_milestone=True,
        )
        self.assertNotIn("employer_name", cleaned)
        self.assertNotIn("job_title", cleaned)
        self.assertEqual(cleaned["default_retirement_age"], 85)
        self.assertEqual(len(cleaned["contributions"]), 1)
        self.assertNotIn("employer_ein", cleaned["contributions"][0])

        cfg_bad = _make_base_valid_cfg()
        cfg_bad["investors"][0]["work_plan"] = {"is_working": True, "default_retirement_age": 90}
        res_bad = validate_portfolio_config(cfg_bad)
        self.assertFalse(res_bad["valid"])
        self.assertTrue(any("cannot exceed milestone age" in e for e in res_bad["validation_errors"]))

        # Household budget contribution cannot exceed annual take-home income when annual_take_home > 0
        cfg_over_budget = _make_base_valid_cfg()
        cfg_over_budget["investors"][0]["work_plan"] = {
            "is_working": True,
            "default_retirement_age": 65,
            "annual_take_home": 150000,
            "annual_budget_offset": 180000,
        }
        res_over_budget = validate_portfolio_config(cfg_over_budget)
        self.assertFalse(res_over_budget["valid"])
        self.assertTrue(any("cannot exceed annual take-home income" in e for e in res_over_budget["validation_errors"]))

        # Auto-retire flips is_working to False once calendar month reaches target retirement month
        wp_elapsed = sanitize_work_plan({"is_working": True, "default_retirement_age": 65, "annual_take_home": 200000}, milestone_age=85)
        auto_retire_elapsed_work_plan(wp_elapsed, "1960-06", today_ym="2025-06")
        self.assertFalse(wp_elapsed["is_working"])
        wp_future = sanitize_work_plan({"is_working": True, "default_retirement_age": 65, "annual_take_home": 200000}, milestone_age=85)
        auto_retire_elapsed_work_plan(wp_future, "1960-06", today_ym="2025-05")
        self.assertTrue(wp_future["is_working"])

        # Non-SSN family members (e.g., an adult child contributing to the household budget) CAN configure work_plan,
        # and portfolios with 0 SSN contributors remain valid so long as >=1 family member has name + birth_date.
        cfg_ben = _make_base_valid_cfg()
        cfg_ben["beneficiaries"][0]["work_plan"] = {
            "is_working": True,
            "default_retirement_age": 26,
            "covers_full_burn": False,
            "annual_budget_offset": 18000,
            "contributions": [],
        }
        res_ben = validate_portfolio_config(cfg_ben)
        self.assertTrue(res_ben["valid"], res_ben)

        cfg_zero_ssn = _make_base_valid_cfg()
        cfg_zero_ssn["family_members"] = [
            {
                "id": "alex",
                "name": "Alex Rivera",
                "birth_date": "1970-06",
                "milestone_age": 85,
                "is_rmd_owner": True,
                "is_ssn_contributor": False,
                "work_plan": {"is_working": True, "default_retirement_age": 62, "covers_full_burn": True},
            }
        ]
        cfg_zero_ssn["investors"] = []
        res_zero_ssn = validate_portfolio_config(cfg_zero_ssn)
        self.assertTrue(res_zero_ssn["valid"], res_zero_ssn)

        snap = canonicalize_model_settings(
            {"simInvestor1RetirementAge": 95, "simMemberRetirementAges": {"member_2_retirement_age": 99}},
            cfg=_make_base_valid_cfg(),
        )
        self.assertEqual(snap["simInvestor1RetirementAge"], 85)
        self.assertEqual(snap["simMemberRetirementAges"]["member_2_retirement_age"], 85)

    def test_wrapper_cost_basis_and_catchup_invariants(self):
        """Employee Roth adds basis, employer Roth/Pre-Tax adds $0 basis, and Age 50/60-63 catch-up triggers by year."""
        # Investor born 1966-06 -> turns 60 in 2026 (SECURE 2.0 $11,250 super catch-up in 2026..2029, then $8,000 in 2030 at age 64!)
        payload = _make_synthetic_payload(today_iso="2026-01-01")
        payload["project"]["investors"][0]["birth_date"] = "1966-01"
        payload["ssn_model"]["investors"][0]["birth_year"] = 1966
        payload["ssn_model"]["investors"][0]["birth_month"] = 1
        payload["project"]["investors"][0]["work_plan"] = {
            "is_working": True,
            "default_retirement_age": 65,
            "covers_full_burn": True,
            "annual_budget_offset": 0,
            "contributions": [
                # Row 1: Employee Roth ($24,500 + catch-up) + Employer Match routed to Pre-Tax 401k ($12,000)
                {
                    "target_asset_id": "acct_HB_7003",
                    "employee_annual": 24500,
                    "employer_match_annual": 12000,
                    "match_target_asset_id": "acct_HB_7002",
                    "age_50_catchup": True,
                },
                # Row 2: Employer Match directly into Roth ($6,000 -> adds to val, +$0 to costBasis)
                {
                    "target_asset_id": "acct_HB_7003",
                    "employee_annual": 0,
                    "employer_match_annual": 6000,
                    "match_target_asset_id": "",
                    "age_50_catchup": False,
                },
                # Row 3: Locked MYGA & missing stale ID must be skipped cleanly
                {
                    "target_asset_id": "ann_HB_7004",
                    "employee_annual": 10000,
                    "employer_match_annual": 0,
                },
                {
                    "target_asset_id": "acct_STALE_REMOVED",
                    "employee_annual": 10000,
                    "employer_match_annual": 0,
                },
            ],
        }
        res = run_simulation(
            payload,
            {
                "simAnnualBurn": 120000,
                "simInflationPct": 0.0,
                "simIncludeSSN": False,
                "holdingForecasts": {
                    "acct_HB_7001": {"growth_pct": 0.0, "risk_class": "broad_equity"},
                    "acct_HB_7002": {"growth_pct": 0.0, "risk_class": "broad_equity"},
                    "acct_HB_7003": {"growth_pct": 0.0, "risk_class": "broad_equity"},
                    "ann_HB_7004": {"growth_pct": 0.0, "risk_class": "cash_fixed"},
                },
            },
        )
        # Attained age in 2026..2029 (47 months: Feb 2026..Dec 2029) is 60..63 -> +$11,250/yr super catch-up
        # Attained age in 2030 (12 months: Jan 2030..Dec 2030) is 64 -> +$8,000/yr standard catch-up
        # Retires 2031-01 (65th birthday month), so total working months = 47 + 12 = 59 months!
        self.assertEqual(res["drawdown_start_month"], 60)
        exp_emp_roth = 47 * ((24500 + 11250) / 12.0) + 12 * ((24500 + 8000) / 12.0)
        exp_match_pretax = 59 * (12000 / 12.0)
        exp_match_roth = 59 * (6000 / 12.0)

        bar_59 = res["bars"][59]
        self.assertAlmostEqual(bar_59["assetVals"]["acct_HB_7002"], 200000 + exp_match_pretax, delta=1)
        self.assertAlmostEqual(bar_59["assetVals"]["acct_HB_7003"], 100000 + exp_emp_roth + exp_match_roth, delta=1)
        # Locked MYGA received $0 deposits and Taxable Index had $0 sales while paycheck covered 100% burn
        self.assertAlmostEqual(bar_59["assetVals"]["ann_HB_7004"], 100000, delta=1)
        self.assertAlmostEqual(bar_59["assetVals"]["acct_HB_7001"], 500000, delta=1)
        self.assertAlmostEqual(bar_59["cumGross"], 0.0, delta=1)

    def test_staggered_retirement_bridge_burn_and_gift_independence(self):
        """Staggered retirements draw partial bridge burn during overlap and never suppress Scheduled Gifts."""
        payload = _make_synthetic_payload(today_iso="2026-06-01")
        # Alex (born 1970-06, age 56) retires at 58 (2028-06 = month 24), covers 100% burn while working.
        payload["project"]["investors"][0]["work_plan"] = {
            "is_working": True,
            "default_retirement_age": 58,
            "covers_full_burn": True,
            "annual_budget_offset": 0,
            "contributions": [],
        }
        # Blair (born 1972-06, age 54) retires at 58 (2030-06 = month 48), offsets $60k/yr of $120k/yr burn.
        payload["project"]["investors"][1]["work_plan"] = {
            "is_working": True,
            "default_retirement_age": 58,
            "covers_full_burn": False,
            "annual_budget_offset": 60000,
            "contributions": [],
        }
        res = run_simulation(
            payload,
            {
                "simAnnualBurn": 120000,
                "simInflationPct": 0.0,
                "simIncludeSSN": False,
                "simIncludeScheduledActions": True,
                "simScheduledActions": [
                    {
                        "id": "gift_while_working",
                        "type": "withdrawal",
                        "cadence": "one_time",
                        "ym": "2027-06",
                        "amount": 30000,
                        "amount_mode": "usd",
                        "dollar_mode": "today",
                        "from_asset_id": "auto",
                        "comment": "Family wedding gift",
                    }
                ],
                "holdingForecasts": {
                    "acct_HB_7001": {"growth_pct": 0.0, "risk_class": "broad_equity"},
                    "acct_HB_7002": {"growth_pct": 0.0, "risk_class": "broad_equity"},
                    "acct_HB_7003": {"growth_pct": 0.0, "risk_class": "broad_equity"},
                    "ann_HB_7004": {"growth_pct": 0.0, "risk_class": "cash_fixed"},
                },
            },
        )
        # Recurring drawdown begins at Month 24 (2028-06) when Alex retires and Blair only covers $60k/yr ($5k/mo shortfall)
        self.assertEqual(res["drawdown_start_month"], 24)
        # Month 12 (2027-06): Paycheck covers 100% of recurring burn, YET $30,000 Scheduled Gift still executes!
        self.assertAlmostEqual(res["bars"][12]["schedWithdrawNom"], 30000, delta=1)
        # Months 1..23 recurring net spend = $0 (only the $30k gift in month 12)
        self.assertAlmostEqual(res["bars"][23]["cumNet"], 30000, delta=1)
        # Months 24..47 (24 bridge months): $5,000/mo net bridge spend = +$120,000
        self.assertAlmostEqual(res["bars"][47]["cumNet"], 30000 + 24 * 5000, delta=1)
        # Month 48 (2030-06): Blair also retires -> full $10,000/mo net spend resumes!
        self.assertAlmostEqual(res["bars"][48]["cumNet"] - res["bars"][47]["cumNet"], 10000, delta=1)

    def test_early_crash_stress_anchors_to_drawdown_start_month(self):
        """Early Crash (`stress`) defers Downfall shock until `drawdown_start_month` when paycheck covers burn."""
        payload = _make_synthetic_payload(today_iso="2026-06-01")
        payload["project"]["investors"][0]["work_plan"] = {
            "is_working": True,
            "default_retirement_age": 58,  # Month 24
            "covers_full_burn": True,
            "annual_budget_offset": 0,
            "contributions": [],
        }
        res_stress = run_simulation(
            payload,
            {
                "simActiveModel": "stress",
                "simStressScenario": "gfc2008",
                "simAnnualBurn": 120000,
                "simInflationPct": 0.0,
                "simIncludeSSN": False,
                "holdingForecasts": {
                    "acct_HB_7001": {"growth_pct": 6.0, "risk_class": "broad_equity"},
                    "acct_HB_7002": {"growth_pct": 6.0, "risk_class": "broad_equity"},
                    "acct_HB_7003": {"growth_pct": 6.0, "risk_class": "broad_equity"},
                    "ann_HB_7004": {"growth_pct": 0.0, "risk_class": "cash_fixed"},
                },
            },
        )
        self.assertEqual(res_stress["drawdown_start_month"], 24)
        # During months 1..23 (pre-retirement), portfolio grows positively rather than crashing in Year 1
        self.assertGreater(res_stress["bars"][23]["totalVal"], res_stress["bars"][0]["totalVal"])
        # Starting at Month 24 (`drawdown_start_month`), GFC crash hits over the next 18 months
        self.assertLess(res_stress["bars"][35]["totalVal"], res_stress["bars"][23]["totalVal"])

    def test_privacy_shield_and_demo_mode_work_plan_invariants(self):
        """Mode C and Demo Mode alias composite IDs and scale work USD fields without scaling retirement ages."""
        reg = {
            "ai_shield": {
                "mode": "identity_and_values",
                "scale_values": True,
                "value_scale": 0.25,
                "dob_granularity": "shift_years",
                "birthday_shift_years": 2,
            },
            "demo_mode": {"enabled": True, "scale_factor": 0.5, "round_to": 1000, "demo_seed": "demo-7777"},
            "entities": [
                {
                    "category": "investor",
                    "canonical": "Alex Rivera",
                    "token": "PERSON_1",
                    "demo_value": "Jordan Taylor",
                    "synonyms": ["Alex Rivera", "Alex"],
                    "enabled": True,
                    "meta": {"birth_date": "1970-06-15"},
                },
                {
                    "category": "account",
                    "canonical": "HB-7003",
                    "token": "ACCT_1",
                    "demo_value": "DM-2200",
                    "synonyms": ["HB-7003", "HB_7003"],
                    "enabled": True,
                },
                {
                    "category": "account",
                    "canonical": "HB-7002",
                    "token": "ACCT_2",
                    "demo_value": "DM-3300",
                    "synonyms": ["HB-7002", "HB_7002"],
                    "enabled": True,
                },
            ],
        }
        tr = PrivacyTranslator(reg)
        sample = {
            "name": "Alex Rivera",
            "birth_year": 1970,
            "retirement_year": 2035,
            "drawdown_start_month": 60,
            "simInvestor1RetirementAge": 65,
            "work_plan": {
                "is_working": True,
                "default_retirement_age": 65,
                "annual_take_home": 160000,
                "budget_answered": True,
                "covers_full_burn": False,
                "annual_budget_offset": 80000,
                "contributions": [
                    {
                        "target_asset_id": "acct_HB-7003",
                        "employee_annual": 24000,
                        "employer_match_annual": 12000,
                        "match_target_asset_id": "uma_HB_7002",
                        "age_50_catchup": True,
                    }
                ],
            },
        }
        wire = tr.to_alias(sample)
        self.assertEqual(wire["work_plan"]["default_retirement_age"], 65)
        self.assertEqual(wire["simInvestor1RetirementAge"], 65)
        self.assertEqual(wire["drawdown_start_month"], 60)
        self.assertEqual(wire["retirement_year"] - wire["birth_year"], 65)
        self.assertEqual(wire["work_plan"]["annual_take_home"], 40000)
        self.assertTrue(wire["work_plan"]["budget_answered"])
        self.assertEqual(wire["work_plan"]["annual_budget_offset"], 20000)
        self.assertEqual(wire["work_plan"]["contributions"][0]["employee_annual"], 6000)
        self.assertEqual(wire["work_plan"]["contributions"][0]["employer_match_annual"], 3000)
        self.assertNotIn("HB-7003", wire["work_plan"]["contributions"][0]["target_asset_id"])
        self.assertEqual(tr.find_leaked_terms(wire), [])

        restored = tr.from_alias(wire)
        self.assertEqual(restored["work_plan"]["annual_take_home"], 160000)
        self.assertEqual(restored["work_plan"]["annual_budget_offset"], 80000)
        self.assertEqual(restored["work_plan"]["contributions"][0]["target_asset_id"], "acct_HB-7003")
        self.assertEqual(restored["work_plan"]["contributions"][0]["match_target_asset_id"], "uma_HB_7002")
        self.assertEqual(restored["retirement_year"], 2035)

        demo = DemoTranslator(reg).project_obj(sample)
        self.assertEqual(demo["work_plan"]["default_retirement_age"], 65)
        self.assertEqual(demo["work_plan"]["annual_take_home"], 80000)
        self.assertEqual(demo["work_plan"]["annual_budget_offset"], 40000)
        self.assertEqual(demo["work_plan"]["contributions"][0]["employee_annual"], 12000)
        self.assertNotIn("HB-7003", demo["work_plan"]["contributions"][0]["target_asset_id"])

    def test_python_js_parity_with_work_plan_and_scenario_overrides(self):
        """Python and Browser JS engines match 1:1 across Single-Path, Stress, MC, Cohorts, and Auto-Optimize."""
        with tempfile.TemporaryDirectory() as td:
            troot = Path(td)
            _roots_support.assemble_root(troot, ["p2_equity_comp_rsu"])
            proj_fp = next(troot.rglob("p2_equity_comp_rsu/project.json"))
            proj_data = json.loads(proj_fp.read_text(encoding="utf-8"))

            with _env_support.pointed_at(troot):
                _pipe_support.clear_payload_cache()
                base_payload = _pipe_support.build_payload("p2_equity_comp_rsu")
                elig_assets = [
                    a for a in (base_payload.get("retirement_sim_assets") or [])
                    if not a.get("is_unvested_goog") and a.get("tax_model") in ("ORDINARY_FULL", "TAX_FREE", "LTCG_POOL")
                ]
                pretax_id = next((a["id"] for a in elig_assets if a.get("tax_model") == "ORDINARY_FULL"), elig_assets[0]["id"])
                roth_id = next((a["id"] for a in elig_assets if a.get("tax_model") == "TAX_FREE"), pretax_id)

                proj_data["investors"][0]["work_plan"] = {
                    "is_working": True,
                    "default_retirement_age": 66,
                    "covers_full_burn": True,
                    "annual_budget_offset": 0,
                    "contributions": [
                        {
                            "target_asset_id": roth_id,
                            "employee_annual": 24500,
                            "employer_match_annual": 10000,
                            "match_target_asset_id": pretax_id if pretax_id != roth_id else "",
                            "age_50_catchup": True,
                        }
                    ],
                }
                if len(proj_data["investors"]) > 1:
                    proj_data["investors"][1]["work_plan"] = {
                        "is_working": True,
                        "default_retirement_age": 68,
                        "covers_full_burn": False,
                        "annual_budget_offset": 48000,
                        "contributions": [
                            {
                                "target_asset_id": pretax_id,
                                "employee_annual": 18000,
                                "employer_match_annual": 6000,
                                "match_target_asset_id": "",
                                "age_50_catchup": True,
                            }
                        ],
                    }
                # Also configure a 3rd non-SSN adult child contributing $18,000/yr to the household budget until age 26
                if proj_data.get("beneficiaries"):
                    proj_data["beneficiaries"][0]["birth_date"] = "2004-05-03"
                    proj_data["beneficiaries"][0]["work_plan"] = {
                        "is_working": True,
                        "default_retirement_age": 26,
                        "covers_full_burn": False,
                        "annual_budget_offset": 18000,
                        "contributions": [],
                    }
                proj_fp.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")

                _pipe_support.clear_payload_cache()
                payload = _pipe_support.build_payload("p2_equity_comp_rsu")
                html = _pipe_support.rendered_html("p2_equity_comp_rsu")
                _pipe_support.clear_payload_cache()

            index_path = troot / "index.html"
            index_path.write_text(html, encoding="utf-8")

            for ov in (
                {"simActiveModel": "single_path"},
                {"simActiveModel": "single_path", "simInvestor1RetirementAge": 63, "simInvestor2RetirementAge": 65},
                {"simActiveModel": "single_path", "simMemberRetirementAges": {"member_2_retirement_age": 24}},
                {"simActiveModel": "stress", "simStressScenario": "gfc2008"},
            ):
                eff = build_effective_settings(payload, {}, overrides=ov)
                js_res = _invoke_js_harness(index_path, eff, "run")
                py_res = run_simulation(payload, eff)
                inv1_ms = py_res["milestones"]["investor_1_milestone"]
                self.assertAlmostEqual(inv1_ms["nominal_usd"], js_res["ms_nominal"], delta=1.0)
                self.assertAlmostEqual(inv1_ms["real_today_usd"], js_res["ms_real"], delta=1.0)
                self.assertAlmostEqual(py_res["milestones"]["year_40"]["nominal_usd"], js_res["y40_nominal"], delta=1.0)

            eff_mc = build_effective_settings(
                payload,
                {},
                overrides={"simActiveModel": "range", "mcSimulations": 60, "simInvestor1RetirementAge": 64},
            )
            mc_in = {**eff_mc, "__mc": {"simulations": 60}, "__seed": 1}
            js_mc = _invoke_js_harness(index_path, mc_in, "montecarlo")
            py_mc = run_monte_carlo(payload, eff_mc, {"simulations": 60}, n_paths=60, seed=1)
            self.assertAlmostEqual(py_mc["ms_real"]["p50"], js_mc["ms_real"]["p50"], delta=2.0)

            eff_coh = build_effective_settings(payload, {}, overrides={"simActiveModel": "historical"})
            js_coh = _invoke_js_harness(index_path, eff_coh, "cohorts")
            py_coh = run_historical_cohorts(payload, eff_coh)
            self.assertAlmostEqual(py_coh["ms_real"]["p50"], js_coh["ms_real"]["p50"], delta=2.0)

            eff_opt = build_effective_settings(payload, {}, overrides={"simActiveModel": "single_path"})
            js_order = _invoke_js_harness(index_path, eff_opt, "optimize")
            py_order = optimize_sim_asset_order(
                payload["retirement_sim_assets"],
                payload["today_iso"],
                eff_opt,
                payload["ssn_model"],
                payload=payload,
            )
            self.assertEqual(py_order, js_order)


if __name__ == "__main__":
    unittest.main()
