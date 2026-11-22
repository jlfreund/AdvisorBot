#!/usr/bin/env python3
"""Goal-seek solver tests (CodeReview V1/V3/V4)."""
from __future__ import annotations
import json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Scripts"))
from advisorbot_server import list_saves_payload  # noqa: E402
from valuation.simulator import load_live_payload, build_effective_settings, solve_simulation  # noqa: E402

from tests.support import legacy as legacy_support  # noqa: E402


class SolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = legacy_support.fixture_payload("p2_equity_comp_rsu")  # was load_live_payload()
        saves_info = list_saves_payload()
        active_name = saves_info.get("active_save") or "Default"
        active_save = next((s for s in (saves_info.get("saves") or []) if s["name"] == active_name), None)
        base = active_save["settings"] if active_save else {}
        cls.eff = build_effective_settings(cls.p, base)
        cls.eff["simActiveModel"] = "single_path"
        cls.eff["simAnnualBurn"] = 50000

    def test_unreachable_target_reports_not_converged(self):
        r = solve_simulation(self.p, solve_for="simAnnualBurn", target_metric="investor_1_real",
                             target_value=500_000_000, settings=self.eff)
        self.assertFalse(r["converged"])
        self.assertTrue(r["note"])
        self.assertEqual(len(r["achievable_range_usd"]), 2)

    def test_reachable_target_converges_small_residual(self):
        probe = solve_simulation(self.p, solve_for="futureGoogGrowthPct", target_metric="investor_1_real",
                                 target_value=500_000_000, settings=self.eff)
        lo, hi = probe["achievable_range_usd"]
        target_mid = 0.5 * (float(lo) + float(hi))
        r = solve_simulation(self.p, solve_for="futureGoogGrowthPct", target_metric="investor_1_real",
                             target_value=target_mid, settings=self.eff)
        self.assertTrue(r["converged"])
        self.assertLess(abs(r["residual_pct"]), 1.0)
        self.assertGreaterEqual(r["achievable_range_usd"][0], 0)

    def test_fewer_iterations_than_fixed_45(self):
        probe = solve_simulation(self.p, solve_for="simAnnualBurn", target_metric="investor_1_real",
                                 target_value=500_000_000, settings=self.eff)
        lo, hi = probe["achievable_range_usd"]
        target_mid = 0.5 * (float(lo) + float(hi))
        r = solve_simulation(self.p, solve_for="simAnnualBurn", target_metric="investor_1_real",
                             target_value=target_mid, settings=self.eff)
        self.assertLess(r["iterations"], 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
