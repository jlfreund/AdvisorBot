#!/usr/bin/env python3
"""Unit tests for the simulator's tax engine (CodeReview R5)."""
from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from valuation.simulator import _sell_capital_gain_lot, _step_sim_month, _clone_sim_asset_state


class SellLotTests(unittest.TestCase):
    def test_underwater_lot_harvests_exact_loss_no_tax(self):
        state = {"_lossCarryforward": 0.0}
        lot = {"val": 800.0, "costBasis": 1000.0}  # $200 underwater
        res = _sell_capital_gain_lot(lot, rem_net_needed=800.0, tax_rate=0.25, state_map=state)
        self.assertAlmostEqual(res["taxPaid"], 0.0, places=4)
        self.assertAlmostEqual(res["netProceeds"], 800.0, places=4)
        self.assertAlmostEqual(res["lossHarvested"], 200.0, places=2)
        self.assertAlmostEqual(state["_lossCarryforward"], 200.0, places=2)

    def test_gross_up_formula_net_equals_gross_times_factor(self):
        state = {"_lossCarryforward": 0.0}
        lot = {"val": 1000.0, "costBasis": 0.0}  # 100% gain
        res = _sell_capital_gain_lot(lot, rem_net_needed=100.0, tax_rate=0.25, state_map=state)
        # eff rate = 0.25*1.0; net = gross*(1-0.25) -> gross = 100/0.75
        self.assertAlmostEqual(res["netProceeds"], 100.0, places=2)
        self.assertAlmostEqual(res["taxPaid"], res["grossSold"] - res["netProceeds"], places=4)
        self.assertAlmostEqual(res["grossSold"] * 0.25, res["taxPaid"], places=2)

    def test_carryforward_shelters_gain_dollar_for_dollar(self):
        state = {"_lossCarryforward": 1000.0, "_totalLossUsed": 0.0, "_totalTaxSavedByLoss": 0.0}
        lot = {"val": 1000.0, "costBasis": 0.0}  # 100% gain, gainRatio=1
        res = _sell_capital_gain_lot(lot, rem_net_needed=500.0, tax_rate=0.25, state_map=state)
        # First $500 gross fully sheltered (gainRatio 1 -> $500 gain sheltered), 0 tax on it
        self.assertAlmostEqual(res["taxPaid"], 0.0, places=2)
        self.assertAlmostEqual(res["netProceeds"], 500.0, places=2)
        self.assertAlmostEqual(state["_lossCarryforward"], 500.0, places=2)


class AnnuityAndLockTests(unittest.TestCase):
    def _assets(self):
        return [{"id": "ann", "bucket": "non_goog", "tax_model": "ANNUITY_LIFO", "tax_rate": 0.40,
                 "maturity_iso": "2028-01-01", "current_value": 1000.0, "cost_basis": 600.0, "tranches": []}]

    def test_locked_annuity_skipped_before_maturity(self):
        a = self._assets(); sm = _clone_sim_asset_state(a, "2026-11-11", True)
        res = _step_sim_month(a, sm, "2027-06-01", "", 0.0, 0.0, 5000.0, True, ["ann"], record_stats=False)
        self.assertAlmostEqual(res["monthlyGrossSold"], 0.0, places=2)  # locked, nothing sold
        self.assertAlmostEqual(sm["ann"]["val"], 1000.0, places=2)

    def test_annuity_lifo_earnings_before_principal(self):
        a = self._assets(); sm = _clone_sim_asset_state(a, "2026-11-11", True)
        # after maturity, need net that exhausts the $400 earnings (taxed 40.0%) then dips into principal
        res = _step_sim_month(a, sm, "2028-06-01", "", 0.0, 0.0, 1000.0, True, ["ann"], record_stats=False)
        # earnings 400 -> net earn = 400*(1-0.40)=240.0 taxed; then principal tapped for remainder
        self.assertGreater(res["monthlyTax"], 0.0)
        self.assertLess(sm["ann"]["val"], 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
