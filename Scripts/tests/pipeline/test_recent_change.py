#!/usr/bin/env python3
"""
Unit and integration tests for the generalized Adaptive Recent Change column in Current Vested Holdings.
Verifies:
1. Data pipeline: _resolve_item_recent_change generates cadence-appropriate values for each asset class
   with explicit, consistent language detailing:
   - What date the change is measured against (the starting baseline date/value).
   - What date the measurement was taken / valued as of (the valuation date).
   - What statement or quote feed reported the measurement.
2. Rollup groups and account rollups have recent_change_* metadata with explicit baseline dates.
3. DOM & UI: Header buttons 'Adapt' / '1W', toggle behavior, and row rendering.
"""

import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from config import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from db.database import PortfolioDB
from exporters.view_model import (
    _resolve_item_recent_change,
    build_report_payload,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = REPO_ROOT / "index.html"


class AdaptiveRecentChangeTest(unittest.TestCase):
    def test_resolve_item_recent_change_equities(self):
        item = {
            "current_value": 100000.0,
            "wow_change": 2500.0,
            "is_scrapeable": True,
            "asset_class": "EQUITY",
            "symbol": "ACME",
            "stmt_date": "2026-10-31",
        }
        res = _resolve_item_recent_change(item, "2026-11-15", prior_iso="2026-11-06")
        self.assertEqual(res["recent_change_val"], 2500.0)
        self.assertIn("1w", res["recent_change_sub"])
        self.assertIn("+2.6%", res["recent_change_sub"])
        self.assertIn("vs 2026-11-06", res["recent_change_sub"])
        self.assertEqual(res["recent_change_sign"], 1)

        tip = res["recent_change_tip"]
        self.assertIn("1-Week Market Price Change: +$2,500.00 (+2.56%)", tip)
        self.assertIn("• Measured against: Prior week Friday market close on 2026-11-06.", tip)
        self.assertIn("• Valuation date: 2026-11-15", tip)
        # When report date matches valuation date, 'Reported on:' is omitted
        self.assertNotIn("• Reported on:", tip)

    def test_resolve_item_recent_change_annuities(self):
        # Case A: Carrier valuation date differs from brokerage reporting statement date
        item_diff_dates = {
            "current_value": 125000.0,
            "wow_change": 0.0,
            "is_scrapeable": False,
            "asset_class": "INDEX_ANNUITY",
            "canonical_name": "Summit Life Fixed Index 5",
            "symbol": "900100",
            "issue_date": "2022-01-15",
            "initial_premium": 100000.0,
            "cagr_pct": 5.10,
            "stmt_date": "2026-10-29",
            "reported_date": "2026-10-31",
            "reported_source": "Harbor Brokerage monthly statement",
        }
        res_diff = _resolve_item_recent_change(item_diff_dates, "2026-11-15")
        self.assertEqual(res_diff["recent_change_val"], 25000.0)
        self.assertIn("+5.10%/yr • Inception (vs 2022-01-15)", res_diff["recent_change_sub"])
        self.assertEqual(res_diff["recent_change_sign"], 1)

        tip_diff = res_diff["recent_change_tip"]
        self.assertIn("Contractual Gain Since Inception: +$25,000.00", tip_diff)
        self.assertIn("• Measured against: Initial contract premium of $100,000.00 on issue date 2022-01-15.", tip_diff)
        self.assertIn("• Valuation date: 2026-10-29 (Summit Life carrier valuation).", tip_diff)
        self.assertIn("• Reported on: Harbor Brokerage monthly statement dated 2026-10-31.", tip_diff)

        # Case B: Valuation date matches report date -> 'Reported on:' is omitted
        item_same_dates = {
            "current_value": 125000.0,
            "wow_change": 0.0,
            "is_scrapeable": False,
            "asset_class": "INDEX_ANNUITY",
            "canonical_name": "Summit Life Fixed Index 5",
            "symbol": "900100",
            "issue_date": "2022-01-15",
            "initial_premium": 100000.0,
            "stmt_date": "2026-10-29",
            "reported_date": "2026-10-29",
            "reported_source": "Summit Life statement",
        }
        res_same = _resolve_item_recent_change(item_same_dates, "2026-11-15")
        self.assertNotIn("• Reported on:", res_same["recent_change_tip"])

    def test_resolve_item_recent_change_munis(self):
        # Accrued coupon present (both 2026-10-31)
        item_accr = {
            "current_value": 103000.0,
            "quantity": 100000.0,
            "wow_change": 0.0,
            "is_scrapeable": False,
            "asset_class": "MUNI_BOND",
            "institution": "Harbor Brokerage",
            "accrued_interest": 3000.0,
            "yield_pct": 4.00,
            "stmt_date": "2026-10-31",
            "reported_date": "2026-10-31",
        }
        res_accr = _resolve_item_recent_change(item_accr, "2026-11-15")
        self.assertEqual(res_accr["recent_change_val"], 3000.0)
        self.assertIn("4.00% Yld • Accrued (as of 2026-10-31)", res_accr["recent_change_sub"])
        self.assertEqual(res_accr["recent_change_sign"], 1)

        tip_accr = res_accr["recent_change_tip"]
        self.assertIn("Accrued Coupon Interest: +$3,000.00", tip_accr)
        self.assertIn("• Measured against: Cumulative coupon interest earned", tip_accr)
        self.assertIn("through statement date 2026-10-31.", tip_accr)
        self.assertIn("• Valuation date: 2026-10-31 (Harbor Brokerage monthly statement period close).", tip_accr)
        # Omitted because report date matches valuation date
        self.assertNotIn("• Reported on:", tip_accr)

        # Accrued coupon zero -> par diff
        item_par = {
            "current_value": 102500.0,
            "quantity": 100000.0,
            "wow_change": 0.0,
            "is_scrapeable": False,
            "asset_class": "MUNI_BOND",
            "institution": "Harbor Brokerage",
            "accrued_interest": 0.0,
            "yield_pct": 4.00,
            "stmt_date": "2026-10-31",
            "reported_date": "2026-10-31",
        }
        res_par = _resolve_item_recent_change(item_par, "2026-11-15")
        self.assertEqual(res_par["recent_change_val"], 2500.0)
        self.assertIn("• vs Par (as of 2026-10-31)", res_par["recent_change_sub"])
        self.assertEqual(res_par["recent_change_sign"], 1)

        tip_par = res_par["recent_change_tip"]
        self.assertIn("Market Value vs. Par: +$2,500.00", tip_par)
        self.assertIn("• Measured against: Face par principal of $100,000.00.", tip_par)
        self.assertIn("• Valuation date: 2026-10-31 (Harbor Brokerage monthly statement period close).", tip_par)
        self.assertNotIn("• Reported on:", tip_par)

    def test_resolve_item_recent_change_cash(self):
        item = {
            "current_value": 60000.0,
            "wow_change": 0.0,
            "is_scrapeable": False,
            "asset_class": "CASH",
            "institution": "Harbor Brokerage",
            "yield_pct": 4.20,
            "stmt_date": "2026-10-31",
            "reported_date": "2026-10-31",
        }
        res = _resolve_item_recent_change(item, "2026-11-15")
        expected_mo = round(60000.0 * 0.0420 / 12.0, 2)
        self.assertEqual(res["recent_change_val"], expected_mo)
        self.assertIn("~4.2% APY • 1mo (as of 2026-10-31)", res["recent_change_sub"])
        self.assertEqual(res["recent_change_sign"], 1)

        tip = res["recent_change_tip"]
        self.assertIn("Estimated Monthly Sweep Interest:", tip)
        self.assertIn("• Measured against: Liquid cash and FDIC sweep balance", tip)
        self.assertIn("• Valuation date: 2026-10-31 (Harbor Brokerage statement period close).", tip)
        self.assertNotIn("• Reported on:", tip)

    def test_live_payload_groups_contain_recent_change(self):
        from tests.support import legacy as _legacy_support

        payload = _legacy_support.fixture_payload("p3_retiree_annuities")

        groups = payload.get("holdings_by_strategy", []) or payload.get("holdings_by_account", [])
        self.assertGreater(len(groups), 0, "Payload must have holdings rollups")
        for g in groups:
            self.assertIn("recent_change_val", g, f"Group {g.get('title')} missing recent_change_val")
            self.assertIn("recent_change_sub", g, f"Group {g.get('title')} missing recent_change_sub")
            self.assertIn("recent_change_tip", g, f"Group {g.get('title')} missing recent_change_tip")
            self.assertIn("recent_change_sign", g, f"Group {g.get('title')} missing recent_change_sign")

            tip = g["recent_change_tip"]
            self.assertIn("• Measured against:", tip, f"Group {g.get('title')} tooltip missing 'Measured against:'")
            self.assertIn("• Valuation date:", tip, f"Group {g.get('title')} tooltip missing 'Valuation date:'")

            for c in g.get("children", []):
                self.assertIn("recent_change_val", c, f"Child {c.get('title')} missing recent_change_val")
                self.assertIn("recent_change_sub", c, f"Child {c.get('title')} missing recent_change_sub")

    def test_dom_header_and_mode_toggle(self):
        import tempfile as _tempfile
        from tests.support import env as _env, pipeline as _pipe, roots as _roots

        with _tempfile.TemporaryDirectory() as _td:
            _troot = Path(_td)
            _roots.assemble_root(_troot, ["p3_retiree_annuities"])
            with _env.pointed_at(_troot):
                _pipe.clear_payload_cache()
                html = _pipe.rendered_html("p3_retiree_annuities")
                _pipe.clear_payload_cache()
            _idx_path = _troot / "index.html"
            _idx_path.write_text(html, encoding="utf-8")
            self._run_dom_header_assertions(html, _idx_path)

    def _run_dom_header_assertions(self, html: str, INDEX_HTML: Path):

        # Check table header: must NOT be purple (.mdl-in-th) and must NOT have selector buttons
        self.assertIn('id="recent-chg-hdr-lbl"', html)
        self.assertIn("Recent Change", html)
        self.assertIn('<th class="num">\n                      <span class="has-tip" id="recent-chg-hdr-lbl"', html)
        self.assertNotIn('id="chg-mode-adaptive-btn"', html)
        self.assertNotIn('id="chg-mode-1w-btn"', html)

        # Run JS verification via Node VM
        node_script = r"""
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const m = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
const script = m[m.length - 1][1];

const mkClassList = () => {
  const set = new Set();
  return {
    add: (...xs) => xs.forEach(x => set.add(x)),
    remove: (...xs) => xs.forEach(x => set.delete(x)),
    toggle: (x, force) => { if (force === undefined) { set.has(x) ? set.delete(x) : set.add(x); } else if (force) set.add(x); else set.delete(x); },
    contains: (x) => set.has(x),
  };
};

const stub = new Proxy(function(){}, {
  get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p==="length"?0:(p==="value"?"":(p==="classList"?mkClassList():(p==="style"?{}:(p==="dataset"?{}:stub)))))),
  apply: () => stub, set: () => true, has: () => true,
});

const elems = {
  "holding-search": { value: "" },
  "holdings-tbody": { innerHTML: "" },
  "holdings-tfoot": { innerHTML: "" },
};

const doc = {
  getElementById: (id) => elems[id] || stub,
  querySelector: () => stub, querySelectorAll: () => [], createElement: () => stub,
  createElementNS: () => stub, addEventListener: () => {}, body: stub, documentElement: stub,
};
const win = { addEventListener: () => {}, location: { hash: "", href: "" }, matchMedia: () => ({ matches: false, addEventListener: () => {} }), devicePixelRatio: 1, getComputedStyle: () => stub };

const ctxObj = {
  document: doc, window: win, navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: (f) => { try { f(); } catch(_) {} return 0; }, clearTimeout: () => {},
  requestAnimationFrame: (f) => { f(); return 0; },
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
};
ctxObj.globalThis = ctxObj; ctxObj.window = ctxObj; ctxObj.self = ctxObj;
ctxObj.addEventListener = () => {}; ctxObj.removeEventListener = () => {};
ctxObj.location = { hash: "", href: "", search: "" };
ctxObj.matchMedia = () => ({ matches: false, addEventListener: () => {} });
ctxObj.getComputedStyle = () => stub; ctxObj.devicePixelRatio = 1; ctxObj.innerWidth = 1200; ctxObj.innerHeight = 800;
ctxObj.history = { replaceState: () => {} };
ctxObj.URL = URL; ctxObj.Promise = Promise; ctxObj.structuredClone = structuredClone;
ctxObj.AbortController = AbortController; ctxObj.TextEncoder = TextEncoder; ctxObj.TextDecoder = TextDecoder;

const ctx = vm.createContext(ctxObj);
vm.runInContext(script, ctx);

// Test 1: render in default adaptive mode
vm.runInContext("renderHoldingsTable()", ctx);
const adaptiveHtml = elems["holdings-tbody"].innerHTML;

// Test 2: switch to 1w mode programmatically if needed
vm.runInContext("setRecentChangeMode('1w')", ctx);
const w1Html = elems["holdings-tbody"].innerHTML;

// Test 3: switch back to adaptive mode
vm.runInContext("setRecentChangeMode('adaptive')", ctx);
const adaptive2Html = elems["holdings-tbody"].innerHTML;

console.log(JSON.stringify({
  adaptiveHasSinceDate: /Inception \(vs \d{4}-\d{2}-\d{2}\)/.test(adaptiveHtml),
  adaptiveHasAccruedToDate: /(?:Accrued|vs Par|Yld).*as of \d{4}-\d{2}-\d{2}/.test(adaptiveHtml),
  adaptiveHasEarnedMo: /APY • 1mo/.test(adaptiveHtml),
  w1DoesNotHaveSinceDate: !/Inception \(vs \d{4}-\d{2}-\d{2}\)/.test(w1Html),
}));
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(INDEX_HTML)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        data = json.loads(res.stdout.strip().splitlines()[-1])

        self.assertTrue(data["adaptiveHasSinceDate"], "Adaptive table must show 'Inception (vs <date>)' for annuities")
        self.assertTrue(data["adaptiveHasAccruedToDate"], "Adaptive table must show 'Accrued (as of <date>)' for municipal bonds")
        self.assertTrue(data["adaptiveHasEarnedMo"], "Adaptive table must show 'APY • 1mo' for cash sweeps")
        self.assertTrue(data["w1DoesNotHaveSinceDate"], "1W mode table must omit 'Inception (vs <date>)'")


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
