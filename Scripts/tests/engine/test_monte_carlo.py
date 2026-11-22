#!/usr/bin/env python3
"""
Monte Carlo engine tests (CodeReview A3 -- Range + Probability tabs).

The MC engine is stochastic, so it cannot be seed-matched cross-engine against the browser JS.
Instead it is anchored to the *already parity-verified* single-path engine via the zero-volatility
identity: with vol=0 every path is degenerate and the MC median must equal `run_simulation`'s
deterministic year-40 total to the cent. The remaining tests pin determinism, band monotonicity,
success-rate sanity, and that the historical block-bootstrap model runs (or falls back cleanly).
"""
from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "Scripts"
sys.path.insert(0, str(SCRIPTS))
from valuation.simulator import load_live_payload, run_simulation, run_monte_carlo  # noqa: E402

from tests.support import legacy as legacy_support  # noqa: E402

# A scenario the fixture portfolio (`p2_equity_comp_rsu`) survives comfortably, so the parity median
# is a meaningful positive number rather than trivially 0 == 0.
SURVIVE = {"simAnnualBurn": 35000, "futureGoogGrowthPct": 10, "futureNonGoogGrowthPct": 6, "simInflationPct": 3}


class MonteCarloTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = legacy_support.fixture_payload("p2_equity_comp_rsu")  # was load_live_payload()

    def test_bear_shield_first_year_uses_only_available_information(self):
        """Different seeded paths over the first year produce identical results with the shield on and off."""
        for seed in (1, 13):
            params = dict(n_paths=6, horizon_months=12, seed=seed)
            normal = run_monte_carlo(self.payload, {**SURVIVE, "simBearShield": False}, **params)
            shield = run_monte_carlo(self.payload, {**SURVIVE, "simBearShield": True}, **params)
            self.assertEqual(normal["band"], shield["band"])

    def test_zero_vol_matches_single_path(self):
        """vol=0 => degenerate paths => MC median == deterministic single-path engine, to the cent."""
        hf_zero_vol = {
            a["id"]: {"risk_class": "cash_fixed"}
            for a in (self.payload.get("retirement_sim_assets") or [])
        }
        settings_zero = {**SURVIVE, "holdingForecasts": hf_zero_vol}
        det = run_simulation(self.payload, settings=settings_zero)
        mc = run_monte_carlo(
            self.payload, settings=settings_zero,
            mc={
                "singleStockVolPct": 0,
                "broadEquityVolPct": 0,
                "googVolPct": 0,
                "nonGoogVolPct": 0,
                "returnModel": "normal",
                "simulations": 5,
            },
            seed=1,
        )
        y40 = det["milestones"]["year_40"]["nominal_usd"]
        self.assertAlmostEqual(mc["band"][-1]["nominal"]["p50"], y40, delta=1.0)
        # every percentile collapses to the same value when there is no dispersion
        last = mc["band"][-1]["nominal"]
        self.assertAlmostEqual(last["p10"], last["p90"], delta=1.0)
        self.assertEqual(mc["success_to_ms_pct"], 100.0)
        self.assertEqual(mc["success_to_horizon_pct"], 100.0)

    def test_determinism(self):
        """Same seed => byte-identical band + success rates."""
        kw = dict(settings=SURVIVE, mc={"googVolPct": 37.5, "nonGoogVolPct": 13.9, "simulations": 60}, seed=42)
        a = run_monte_carlo(self.payload, **kw)
        b = run_monte_carlo(self.payload, **kw)
        self.assertEqual(a["band"], b["band"])
        self.assertEqual(a["success_to_horizon_pct"], b["success_to_horizon_pct"])
        self.assertEqual(a["ending_histogram"], b["ending_histogram"])

    def test_band_monotonic_and_success_bounds(self):
        mc = run_monte_carlo(
            self.payload, settings=SURVIVE,
            mc={"googVolPct": 37.5, "nonGoogVolPct": 13.9, "googRestCorr": 0.51, "simulations": 200},
            seed=7,
        )
        for pt in mc["band"]:
            for space in ("nominal", "real"):
                p = pt[space]
                self.assertLessEqual(p["p10"], p["p25"] + 1e-6)
                self.assertLessEqual(p["p25"], p["p50"] + 1e-6)
                self.assertLessEqual(p["p50"], p["p75"] + 1e-6)
                self.assertLessEqual(p["p75"], p["p90"] + 1e-6)
        for key in ("success_to_ms_pct", "success_to_horizon_pct"):
            self.assertGreaterEqual(mc[key], 0.0)
            self.assertLessEqual(mc[key], 100.0)
        # surviving longer is never easier than surviving to the horizon
        self.assertGreaterEqual(mc["success_to_ms_pct"], mc["success_to_horizon_pct"] - 1e-6)
        # histogram counts every path
        self.assertEqual(sum(b["count"] for b in mc["ending_histogram"]), mc["n_paths"])

    def test_historical_model_runs(self):
        mc = run_monte_carlo(
            self.payload, settings=SURVIVE,
            mc={"returnModel": "historical", "simulations": 120}, seed=3,
        )
        # either it used real history, or it fell back to normal -- never errors, always valid output
        self.assertIn(mc["model"], ("historical", "normal"))
        self.assertEqual(len(mc["band"]), 41)  # year 0..40 inclusive
        self.assertTrue(0.0 <= mc["success_to_horizon_pct"] <= 100.0)

    def test_empty_ltcg_tranches_fallback_preserves_month0_value(self):
        """A vested LTCG_TRANCHES bucket with empty tranches[] must retain its current_value at Month 0."""
        import copy
        p = copy.deepcopy(self.payload)
        expected_vested = sum(
            float(a.get("current_value") or 0.0)
            for a in p["retirement_sim_assets"]
            if not a.get("is_unvested_goog")
        )
        # Clear tranches on any LTCG_TRANCHES bucket that holds positive value (or add one if absent)
        cleared = False
        for a in p["retirement_sim_assets"]:
            if a.get("tax_model") == "LTCG_TRANCHES" and float(a.get("current_value") or 0.0) > 0:
                a["tranches"] = []
                cleared = True
        if not cleared:
            p["retirement_sim_assets"].append({
                "id": "test_ltcg_empty",
                "default_rank": 5,
                "color": "#0284c7",
                "title": "Synthetic LTCG Empty Lots",
                "short_title": "Syn LTCG",
                "subtitle": "",
                "account_label": "Test",
                "bucket": "goog",
                "is_unvested_goog": False,
                "is_goog_priced": True,
                "shares": 1000.0,
                "current_value": 250000.0,
                "cost_basis": 100000.0,
                "tax_model": "LTCG_TRANCHES",
                "tax_rate": 0.371,
                "tranches": [],
            })
            expected_vested += 250000.0

        det = run_simulation(p, settings=SURVIVE)
        mc = run_monte_carlo(
            p, settings=SURVIVE,
            mc={"googVolPct": 20, "nonGoogVolPct": 10, "returnModel": "normal", "simulations": 20},
            seed=5,
        )
        self.assertAlmostEqual(det["bars"][0]["totalVal"], expected_vested, delta=1.0)
        self.assertAlmostEqual(mc["band"][0]["nominal"]["p10"], expected_vested, delta=1.0)
        self.assertAlmostEqual(mc["band"][0]["nominal"]["p90"], expected_vested, delta=1.0)

    def test_same_ticker_buckets_share_idiosyncratic_shock(self):
        """Splitting one single-stock position into 4 equal buckets must produce identical MC percentiles (zero fake diversification)."""
        import copy
        p_one = copy.deepcopy(self.payload)
        p_one["retirement_sim_assets"] = [
            {
                "id": "ss_single",
                "default_rank": 1,
                "color": "#38bdf8",
                "title": "Single Stock Combined",
                "short_title": "SS Combined",
                "subtitle": "",
                "account_label": "Acct A",
                "bucket": "goog",
                "symbol": "VNTG",
                "parent_forecast_keys": ["VNTG"],
                "default_risk_class": "single_stock",
                "is_unvested_goog": False,
                "current_value": 4_000_000.0,
                "cost_basis": 2_000_000.0,
                "tax_model": "LTCG_POOL",
                "tax_rate": 0.30,
                "tranches": [],
            }
        ]
        p_four = copy.deepcopy(p_one)
        p_four["retirement_sim_assets"] = [
            {
                **copy.deepcopy(p_one["retirement_sim_assets"][0]),
                "id": f"ss_lot_{k}",
                "default_rank": k,
                "current_value": 1_000_000.0,
                "cost_basis": 500_000.0,
            }
            for k in range(1, 5)
        ]
        no_burn = {"simAnnualBurn": 0, "futureGoogGrowthPct": 8.0, "simInflationPct": 3.0, "simIncludeSSN": False, "simIncludeScheduledActions": False}
        mc_kw = dict(settings=no_burn, mc={"singleStockVolPct": 36.0, "singleStockCorr": 0.50, "simulations": 250}, seed=11)
        res_one = run_monte_carlo(p_one, **mc_kw)
        res_four = run_monte_carlo(p_four, **mc_kw)
        for idx in (5, 15, 30, 40):
            for q in ("p10", "p50", "p90"):
                self.assertAlmostEqual(
                    res_one["band"][idx]["nominal"][q],
                    res_four["band"][idx]["nominal"][q],
                    delta=1.0,
                    msg=f"yr {idx} {q} diverged when splitting one ticker across 4 buckets",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
