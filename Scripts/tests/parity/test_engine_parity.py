#!/usr/bin/env python3
"""
Cross-engine parity test (CodeReview R1 + Phase D RSI.html):
Runs deterministic single-path, Early Crash (`stress`), Historical Cohorts, and seeded
non-zero-volatility `Monte-Carlo` (`seed=1`) + `$0`-start `simHypotheticalAssets` +
unified `simScheduledActions` (`transfer`, `deposit`, `withdrawal`, `on_vest`) through
both the Python simulator and the ACTUAL browser JS engine (via Node + DOM stub harness),
asserting milestone equality to the cent.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = ROOT / "Scripts"
sys.path.insert(0, str(SCRIPTS))
from advisorbot_server import list_saves_payload  # noqa: E402
from valuation.simulator import (  # noqa: E402
    build_effective_settings,
    optimize_sim_asset_order,
    run_historical_cohorts,
    run_monte_carlo,
    run_simulation,
)
from tests.support import env as _env_support, pipeline as _pipe_support, roots as _roots_support  # noqa: E402

HARNESS = SCRIPTS / "tests" / "parity_harness.mjs"


def _build_scenarios(payload: dict) -> list[dict]:
    invs = ((payload.get("ssn_model") or {}).get("investors") or [])
    k0 = invs[0].get("claim_setting_key", "ssnInv0ClaimAge") if len(invs) > 0 else "ssnInv0ClaimAge"
    k1 = invs[1].get("claim_setting_key", "ssnInv1ClaimAge") if len(invs) > 1 else "ssnInv1ClaimAge"

    assets = payload.get("retirement_sim_assets") or []
    non_ira = [a["id"] for a in assets if a.get("tax_wrapper") != "TRAD_IRA"]
    ira = [a["id"] for a in assets if a.get("tax_wrapper") == "TRAD_IRA"]
    rmd_order = non_ira + ira

    return [
        {},  # Default save baseline
        {"futureGoogGrowthPct": 0, "futureNonGoogGrowthPct": 0},
        {"futureGoogGrowthPct": 14.5, "futureNonGoogGrowthPct": 5.1, "simAnnualBurn": 80000, "simInflationPct": 3.0},
        {"simAnnualBurn": 120000, "simInflationPct": 6.0, "simIncludeSSN": False},
        {"simIncludeUnvested": False, "simIncludeScheduledActions": False},
        {k0: 62, k1: 70, "ssnInv0ClaimAge": 62, "ssnInv1ClaimAge": 70, "ssnColaPct": 1.5},
        {"simAnnualBurn": 45000, "futureGoogGrowthPct": 8, "futureNonGoogGrowthPct": 5, "simAssetOrder": rmd_order},
    ]


def _invoke_harness(index_path: Path, cfg: dict, mode: str = "run") -> dict | list:
    with tempfile.TemporaryDirectory() as td:
        tmp_in = Path(td) / f"parity_in_{mode}.json"
        tmp_out = Path(td) / f"parity_out_{mode}.json"
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


class ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        troot = Path(cls._tmp.name)
        _roots_support.assemble_root(troot, ["p2_equity_comp_rsu"])
        with _env_support.pointed_at(troot):
            _pipe_support.clear_payload_cache()
            cls.payload = _pipe_support.build_payload("p2_equity_comp_rsu")
            html = _pipe_support.rendered_html("p2_equity_comp_rsu")
            saves_info = list_saves_payload()
            _pipe_support.clear_payload_cache()
        cls.index_path = troot / "index.html"
        cls.index_path.write_text(html, encoding="utf-8")
        active_name = saves_info.get("active_save") or "Default"
        active_save = next((s for s in (saves_info.get("saves") or []) if s["name"] == active_name), None)
        cls.base = active_save["settings"] if active_save else {}
        cls.scenarios = _build_scenarios(cls.payload)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_scenarios_match(self):
        for i, over in enumerate(self.scenarios):
            over_sp = {**over, "simActiveModel": "single_path"}
            eff = build_effective_settings(self.payload, self.base, overrides=over_sp)
            js = _invoke_harness(self.index_path, eff, "run")
            r = run_simulation(self.payload, eff)
            ms, dep = r["milestones"], r["solvency"]["depleted_at"]
            inv1_ms = ms["investor_1_milestone"]
            py = {
                "ms_nominal": inv1_ms["nominal_usd"],
                "ms_real": inv1_ms["real_today_usd"],
                "ms_cumTax": inv1_ms["cum_tax_nominal_usd"],
                "y40_nominal": ms["year_40"]["nominal_usd"],
                "depleted": dep["monthIdx"] if dep else None,
            }
            for k in py:
                if isinstance(py[k], float):
                    self.assertAlmostEqual(
                        py[k], js[k], delta=1.0,
                        msg=f"scenario {i} key {k}: py={py[k]} js={js[k]} (overrides={over})",
                    )
                else:
                    self.assertEqual(py[k], js[k], msg=f"scenario {i} key {k}: py={py[k]} js={js[k]}")

    def test_stress_scenarios_match_js(self):
        """Market Correction 3-phase (Downfall + Broad/Stock Bounce-Back) matches 1:1 between Python and JS across all presets & custom."""
        cases = (
            {"simStressScenario": "gfc2008"},
            {"simStressScenario": "dotcom2000"},
            {"simStressScenario": "covid2020"},
            {"simStressScenario": "rates2022"},
            {
                "simStressScenario": "custom",
                "simCustomShockPct": -45,
                "simCustomDownMonths": 18,
                "simCustomBroadBounceMonths": 24,
                "simCustomStockBounceMonths": 75,
            },
        )
        for spec in cases:
            over = {
                "simActiveModel": "stress",
                **spec,
                "futureGoogGrowthPct": 9.0,
                "futureNonGoogGrowthPct": 6.0,
                "simAnnualBurn": 60000,
            }
            eff = build_effective_settings(self.payload, self.base, overrides=over)
            js = _invoke_harness(self.index_path, eff, "run")
            r = run_simulation(self.payload, eff)
            inv1_ms = r["milestones"]["investor_1_milestone"]
            scen = spec["simStressScenario"]
            self.assertAlmostEqual(inv1_ms["real_today_usd"], js["ms_real"], delta=1.0, msg=f"stress {scen} ms_real")
            self.assertAlmostEqual(inv1_ms["cum_tax_nominal_usd"], js["ms_cumTax"], delta=1.0, msg=f"stress {scen} tax")
            py_opt = optimize_sim_asset_order(
                self.payload["retirement_sim_assets"],
                self.payload["today_iso"],
                eff,
                self.payload["ssn_model"],
                float((self.payload.get("kpis") or {}).get("capital_loss_carryforward") or 0.0),
                primary_sym=str((self.payload.get("project") or {}).get("primary_equity_symbol") or ""),
                payload=self.payload,
            )
            js_opt = _invoke_harness(self.index_path, eff, "optimize")
            self.assertEqual(py_opt, js_opt, msg=f"stress {scen} optimizer order")

    def test_hypothetical_asset_and_scheduled_actions_match_js(self):
        """Phase D Stage 3: $0-start Hypothetical bucket + Transfer + Recurring Deposit/Withdrawal + On-Vest match 1:1."""
        assets = self.payload.get("retirement_sim_assets") or []
        src_eq = next((a["id"] for a in assets if a.get("bucket") == "goog" and not a.get("is_unvested_goog")), assets[0]["id"])
        unvested_id = next((a["id"] for a in assets if a.get("is_unvested_goog")), src_eq)
        cash_id = next((a["id"] for a in assets if "cash" in a["id"]), assets[0]["id"])
        hyps = [
            {
                "id": "hyp_idx_spy",
                "label": "Hypothetical: Broad Index ETF",
                "institution": "Harbor Brokerage",
                "real_product_ref": "VOO / FXAIX",
                "initial_value": 0.0,
                "growth_pct": 9.5,
                "risk_class": "broad_equity",
                "expense_ratio_pct": 0.03,
                "tax_wrapper": "taxable_stock",
                "unlock_ym": None,
            }
        ]
        actions = [
            {
                "id": "act_t1",
                "type": "transfer",
                "cadence": "one_time",
                "ym": "next_month",
                "amount_mode": "usd",
                "amount": 120000,
                "dollar_mode": "nominal",
                "from_asset_id": src_eq,
                "to_asset_id": "hyp_idx_spy",
                "pay_tax_from_proceeds": True,
                "comment": "Diversify $120k primary stock into S&P 500",
            },
            {
                "id": "act_v1",
                "type": "transfer",
                "cadence": "on_vest",
                "amount_mode": "pct_of_source",
                "amount": 50.0,
                "from_asset_id": unvested_id,
                "to_asset_id": "hyp_idx_spy",
                "pay_tax_from_proceeds": True,
                "comment": "Auto-diversify 50% of future RSU vests",
            },
            {
                "id": "act_d1",
                "type": "deposit",
                "cadence": "annual",
                "start_ym": "2027-06",
                "end_ym": "2031-06",
                "amount_mode": "usd",
                "amount": 15000,
                "dollar_mode": "today",
                "to_asset_id": cash_id,
                "comment": "Consulting income",
            },
            {
                "id": "act_w1",
                "type": "withdrawal",
                "cadence": "one_time",
                "ym": "2030-05",
                "amount_mode": "usd",
                "amount": 40000,
                "dollar_mode": "today",
                "from_asset_id": "auto",
                "comment": "Home remodel",
            },
        ]
        over = {
            "simActiveModel": "single_path",
            "simAnnualBurn": 50000,
            "futureGoogGrowthPct": 10.0,
            "futureNonGoogGrowthPct": 6.5,
            "simHypotheticalAssets": hyps,
            "simScheduledActions": actions,
            "simIncludeScheduledActions": True,
            "simAssetOrder": [*[a["id"] for a in assets], "hyp_idx_spy"],
        }
        eff = build_effective_settings(self.payload, self.base, overrides=over)
        js = _invoke_harness(self.index_path, eff, "run")
        r = run_simulation(self.payload, eff)
        self.assertGreater(r["bars"][12]["assetVals"].get("hyp_idx_spy", 0.0), 0.0)
        self.assertGreater(max(b["assetVals"].get("hyp_idx_spy", 0.0) for b in r["bars"][12:]), 50000.0)
        inv1_ms = r["milestones"]["investor_1_milestone"]
        self.assertAlmostEqual(inv1_ms["nominal_usd"], js["ms_nominal"], delta=1.5)
        self.assertAlmostEqual(inv1_ms["real_today_usd"], js["ms_real"], delta=1.5)
        self.assertAlmostEqual(inv1_ms["cum_tax_nominal_usd"], js["ms_cumTax"], delta=1.5)

    def test_browser_release_examples(self):
        """The rendered JS independently matches January RMDs, tax, lot and calendar examples."""
        result = _invoke_harness(self.index_path, {}, "release_examples")
        self.assertAlmostEqual(result["rmd"], 4000)
        for balance in result["balances"]:
            self.assertAlmostEqual(balance, 28150)
        self.assertEqual(result["lot"], [True, True, False])
        self.assertEqual(result["dates"], ["2023-01", "2023-02"])
        self.assertEqual(result["quarters"], [False, True])
        for actual, expected in zip(result["tax"], [0, 2480, 11600]):
            self.assertAlmostEqual(actual, expected)

    def test_optimizer_matches(self):
        """O1 + DA-C04: Python and JS Auto-Optimizer return identical liquidation orders across all 4 simulation lenses."""
        for lens in ("single_path", "stress", "range", "historical"):
            eff = build_effective_settings(self.payload, self.base, overrides={"simActiveModel": lens})
            js_order = _invoke_harness(self.index_path, eff, "optimize")
            py_order = optimize_sim_asset_order(
                self.payload["retirement_sim_assets"],
                self.payload["today_iso"],
                eff,
                self.payload.get("ssn_model") or {},
                payload=self.payload,
            )
            self.assertEqual(py_order, js_order, msg=f"optimizer lens {lens}: py={py_order} js={js_order}")
        for i, over in enumerate(self.scenarios[:2]):
            eff = build_effective_settings(self.payload, self.base, overrides={**over, "simActiveModel": "single_path"})
            js_order = _invoke_harness(self.index_path, eff, "optimize")
            py_order = optimize_sim_asset_order(
                self.payload["retirement_sim_assets"],
                self.payload["today_iso"],
                eff,
                self.payload.get("ssn_model") or {},
                payload=self.payload,
            )
            self.assertEqual(py_order, js_order, msg=f"optimizer scenario {i}: py={py_order} js={js_order}")

    def test_saved_equity_half_life_matches_browser(self):
        """Compare fast, medium, slow, and Flat (31) two-stage decay across single_path, stress, range (Normal & Historical), and historical cohorts."""
        asset = next(a for a in self.payload["retirement_sim_assets"] if a.get("default_risk_class") == "single_stock")
        key = asset.get("forecast_key") or asset["id"]
        balances = []
        for years in (1, 5, 30, 31):
            eff = build_effective_settings(self.payload, self.base, overrides={
                "simAnnualBurn": 0, "holdingForecasts": {key: {"growth_pct": 22.79}},
                "holdingHalfLives": {key: years},
            })
            js = _invoke_harness(self.index_path, eff, "run")
            balance = run_simulation(self.payload, eff)["milestones"]["year_40"]["nominal_usd"]
            self.assertAlmostEqual(js["y40_nominal"], balance, delta=1.0, msg=f"single_path half_life={years}")
            balances.append(balance)
        self.assertGreater(balances[1], balances[0])
        self.assertGreater(balances[2], balances[1])
        self.assertGreater(balances[3], balances[2])

        # Stress lens parity across fast vs Flat half-life
        stress_vals = []
        for years in (1, 31):
            eff_st = build_effective_settings(self.payload, self.base, overrides={
                "simActiveModel": "stress",
                "simStressScenario": "gfc2008",
                "simAnnualBurn": 0,
                "holdingForecasts": {key: {"growth_pct": 22.79}},
                "holdingHalfLives": {key: years},
            })
            js_st = _invoke_harness(self.index_path, eff_st, "run")
            py_st = run_simulation(self.payload, eff_st)["milestones"]["year_40"]["nominal_usd"]
            self.assertAlmostEqual(js_st["y40_nominal"], py_st, delta=1.5, msg=f"stress half_life={years}")
            stress_vals.append(py_st)
        self.assertGreater(stress_vals[1], stress_vals[0])

        # Monte Carlo (Normal & Historical Bootstrap) parity across fast vs Flat half-life
        hist_pairs = list((self.payload.get("montecarlo") or {}).get("hist_pairs") or [])
        if len(hist_pairs) <= 52:
            hist_pairs = [[0.004 * ((-1) ** i), 0.002 * ((-1) ** (i // 2))] for i in range(104)]
        for ret_model in ("normal", "historical"):
            mc_vals = []
            for years in (1, 31):
                eff_mc = build_effective_settings(self.payload, self.base, overrides={
                    "simActiveModel": "range",
                    "simAnnualBurn": 0,
                    "holdingForecasts": {key: {"growth_pct": 22.79}},
                    "holdingHalfLives": {key: years},
                })
                mc_cfg = {
                    "singleStockVolPct": 35.0,
                    "broadEquityVolPct": 14.0,
                    "singleStockCorr": 0.50,
                    "returnModel": ret_model,
                    "simulations": 60,
                    **({"histPairs": hist_pairs} if ret_model == "historical" else {}),
                }
                js_mc = _invoke_harness(self.index_path, {**eff_mc, "__mc": mc_cfg, "__seed": 1}, "montecarlo")
                py_mc = run_monte_carlo(self.payload, eff_mc, mc=mc_cfg, seed=1)
                self.assertEqual(js_mc["model"], ret_model)
                self.assertEqual(py_mc["model"], ret_model)
                self.assertAlmostEqual(js_mc["ms_real"]["p50"], py_mc["ms_real"]["p50"], delta=2.0, msg=f"mc {ret_model} hl={years}")
                self.assertAlmostEqual(
                    js_mc["band"][-1]["nominal"]["p50"],
                    py_mc["band"][-1]["nominal"]["p50"],
                    delta=2.0,
                    msg=f"mc {ret_model} end hl={years}",
                )
                mc_vals.append(py_mc["band"][-1]["nominal"]["p50"])
            self.assertGreater(mc_vals[1], mc_vals[0])

        # Historical Cohorts (`anchor`) parity across fast vs Flat half-life
        coh_vals = []
        for years in (1, 31):
            eff_coh = build_effective_settings(self.payload, self.base, overrides={
                "simActiveModel": "historical",
                "simAnnualBurn": 0,
                "holdingForecasts": {key: {"growth_pct": 22.79}},
                "holdingHalfLives": {key: years},
            })
            js_coh = _invoke_harness(
                self.index_path,
                {**eff_coh, "__opts": {"centerMode": "anchor", "inflationMode": "real_cpi"}},
                "cohorts",
            )
            py_coh = run_historical_cohorts(
                self.payload,
                eff_coh,
                opts={"center_mode": "anchor", "inflation_mode": "real_cpi"},
            )
            self.assertAlmostEqual(js_coh["ms_real"]["p50"], py_coh["ms_real"]["p50"], delta=2.0, msg=f"cohorts hl={years}")
            self.assertAlmostEqual(
                js_coh["band"][-1]["real"]["p50"],
                py_coh["band"][-1]["real"]["p50"],
                delta=2.0,
                msg=f"cohorts end hl={years}",
            )
            coh_vals.append(py_coh["band"][-1]["real"]["p50"])
        self.assertGreater(coh_vals[1], coh_vals[0])

    def test_historical_cohorts_match_js(self):
        """A3 Historical tab: deterministic 4-risk-class cohort replay matches 1:1 between Python and JS."""
        over = {"simAnnualBurn": 50000, "futureGoogGrowthPct": 9, "futureNonGoogGrowthPct": 6, "simInflationPct": 3}
        eff = build_effective_settings(self.payload, self.base, overrides=over)
        for center, infl in (("anchor", "flat"), ("raw", "real_cpi")):
            js = _invoke_harness(self.index_path, {**eff, "__opts": {"centerMode": center, "inflationMode": infl, "spotlight": [1966]}}, "cohorts")
            py = run_historical_cohorts(self.payload, eff, opts={"center_mode": center, "inflation_mode": infl, "spotlight": [1966]})
            self.assertTrue(js["available"] and py["available"])
            self.assertEqual(js["n_cohorts"], py["n_cohorts"])
            self.assertEqual(js["worst_cohort"]["startYear"], py["worst_cohort"]["startYear"])
            self.assertEqual(js["best_cohort"]["startYear"], py["best_cohort"]["startYear"])
            jc = next(c for c in js["named"] if c["startYear"] == 1966)
            pc = next(c for c in py["named"] if c["startYear"] == 1966)
            self.assertAlmostEqual(jc["endReal"], pc["endReal"], delta=1.0, msg=f"{center}/{infl} 1966 endReal")
            self.assertEqual(jc["depletedMonth"], pc["depletedMonth"], msg=f"{center}/{infl} 1966 depletion")
            self.assertEqual(js["success_full_n"], py["success_full_n"])
            self.assertAlmostEqual(js["tax_to_ms"]["cum_tax_real"], py["tax_to_ms"]["cum_tax_real"], delta=2.0)
            self.assertAlmostEqual(js["tax_to_ms"]["eff_tax_pct"], py["tax_to_ms"]["eff_tax_pct"], delta=0.1)
            for i in (10, 20):
                self.assertAlmostEqual(js["band"][i]["real"]["p50"], py["band"][i]["real"]["p50"], delta=1.0)

    def test_monte_carlo_zero_vol_matches_js(self):
        """With volatility=0 across risk classes, MC paths are degenerate and match single-path to the cent."""
        over = {
            "simAnnualBurn": 40000,
            "futureGoogGrowthPct": 10,
            "futureNonGoogGrowthPct": 6,
            "simInflationPct": 3,
            "simActiveModel": "single_path",
        }
        eff = build_effective_settings(self.payload, self.base, overrides=over)
        # Zero out all asset risk classes to cash_fixed with explicit growth_pct for degenerate zero-vol check
        hf_zero_vol = {
            a["id"]: {"growth_pct": 6.0, "risk_class": "cash_fixed", "risk_overridden": True}
            for a in (self.payload.get("retirement_sim_assets") or [])
        }
        eff["holdingForecasts"] = hf_zero_vol
        mc = {
            "singleStockVolPct": 0,
            "broadEquityVolPct": 0,
            "googVolPct": 0,
            "nonGoogVolPct": 0,
            "returnModel": "normal",
            "simulations": 3,
        }
        js = _invoke_harness(self.index_path, {**eff, "__mc": mc, "__seed": 1}, "montecarlo")
        py = run_monte_carlo(self.payload, eff, mc=mc, seed=1)
        det_y40 = run_simulation(self.payload, eff)["milestones"]["year_40"]["nominal_usd"]
        self.assertEqual(len(js["band"]), len(py["band"]))
        self.assertEqual(js["ms_month"], py["ms_month"])
        for i in (0, 10, 20, 40):
            jp = js["band"][i]["nominal"]["p50"]
            pp = py["band"][i]["nominal"]["p50"]
            self.assertAlmostEqual(jp, pp, delta=1.0, msg=f"MC band yr{i}: js={jp} py={pp}")
        self.assertAlmostEqual(js["band"][-1]["nominal"]["p50"], det_y40, delta=1.0)
        self.assertAlmostEqual(py["band"][-1]["nominal"]["p50"], det_y40, delta=1.0)

    def test_monte_carlo_nonzero_vol_matches_js(self):
        """Phase D Stage 4: Shared 32-bit mulberry32 + Box-Muller PRNG (`seed=1`) produces 1:1 non-zero-vol MC parity."""
        over = {
            "simAnnualBurn": 55000,
            "futureGoogGrowthPct": 11.0,
            "futureNonGoogGrowthPct": 6.5,
            "simInflationPct": 3.0,
            "mcSingleStockVolPct": 36.2,
            "mcBroadEquityVolPct": 13.9,
            "mcSingleStockCorr": 0.52,
        }
        eff = build_effective_settings(self.payload, self.base, overrides=over)
        mc = {
            "singleStockVolPct": 36.2,
            "broadEquityVolPct": 13.9,
            "singleStockCorr": 0.52,
            "returnModel": "normal",
            "simulations": 200,
        }
        js = _invoke_harness(self.index_path, {**eff, "__mc": mc, "__seed": 1}, "montecarlo")
        py = run_monte_carlo(self.payload, eff, mc=mc, seed=1)
        self.assertEqual(js["n_paths"], py["n_paths"])
        self.assertAlmostEqual(js["success_to_ms_pct"], py["success_to_ms_pct"], delta=0.01)
        self.assertAlmostEqual(js["ms_real"]["p25"], py["ms_real"]["p25"], delta=2.0)
        self.assertAlmostEqual(js["ms_real"]["p50"], py["ms_real"]["p50"], delta=2.0)
        self.assertAlmostEqual(js["ms_real"]["p75"], py["ms_real"]["p75"], delta=2.0)
        self.assertAlmostEqual(js["tax_to_ms"]["cum_tax_real"], py["tax_to_ms"]["cum_tax_real"], delta=2.0)
        self.assertAlmostEqual(js["tax_to_ms"]["eff_tax_pct"], py["tax_to_ms"]["eff_tax_pct"], delta=0.1)

    def test_scheduled_withdrawal_adds_pre_post_event_checkpoints_in_mc_and_cohorts(self):
        """Non-monthly scheduled actions add pre-(m-1) and post-(m) checkpoints in MC and Historical cohorts."""
        eff = build_effective_settings(
            self.payload,
            self.base,
            overrides={
                "simIncludeScheduledActions": True,
                "simScheduledActions": [
                    {
                        "id": "gift_june",
                        "type": "withdrawal",
                        "cadence": "one_time",
                        "ym": "2027-06",
                        "amount": 1_500_000,
                        "amount_mode": "usd",
                        "dollar_mode": "today",
                        "from_asset_id": "auto",
                        "comment": "June Withdrawal",
                    }
                ],
            },
        )
        mc_cfg = {"returnModel": "normal", "simulations": 25}
        js_mc = _invoke_harness(self.index_path, {**eff, "__mc": mc_cfg, "__seed": 1}, "montecarlo")
        py_mc = run_monte_carlo(self.payload, eff, mc=mc_cfg, seed=1)
        self.assertEqual([b["monthIdx"] for b in js_mc["band"]], [b["monthIdx"] for b in py_mc["band"]])
        mc_by_m = {b["monthIdx"]: b for b in py_mc["band"]}
        self.assertIn(6, mc_by_m)
        self.assertIn(7, mc_by_m)
        self.assertLess(mc_by_m[7]["real"]["p50"], mc_by_m[6]["real"]["p50"] - 1_000_000)

        js_coh = _invoke_harness(
            self.index_path,
            {**eff, "__opts": {"centerMode": "anchor", "inflationMode": "real_cpi"}},
            "cohorts",
        )
        py_coh = run_historical_cohorts(
            self.payload,
            eff,
            opts={"center_mode": "anchor", "inflation_mode": "real_cpi"},
        )
        self.assertEqual([b["monthIdx"] for b in js_coh["band"]], [b["monthIdx"] for b in py_coh["band"]])
        js_by_m = {b["monthIdx"]: b for b in js_coh["band"]}
        py_by_m = {b["monthIdx"]: b for b in py_coh["band"]}
        self.assertIn(6, js_by_m)
        self.assertIn(7, js_by_m)
        self.assertLess(js_by_m[7]["real"]["p50"], js_by_m[6]["real"]["p50"] - 1_000_000)
        self.assertAlmostEqual(js_by_m[6]["real"]["p50"], py_by_m[6]["real"]["p50"], delta=2.0)
        self.assertAlmostEqual(js_by_m[7]["real"]["p50"], py_by_m[7]["real"]["p50"], delta=2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
