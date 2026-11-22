"""
Unit tests for the unified Family Members settings feature.
Verifies backend synthesis/persistence and frontend rendering, capsule constraints,
collapsible SSN accordion, and single RMD owner enforcement.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from advisorbot_server import get_project_settings, handle_save_project_settings, load_config


class TestFamilyMembersBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.portfolios_root = Path(self.tmp_dir) / "Portfolios"
        self.p_dir = self.portfolios_root / "test_family_portfolio"
        self.p_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_file = self.p_dir / "project.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_synthesizes_family_from_investors_and_beneficiaries(self) -> None:
        cfg: dict[str, Any] = {
            "household_name": "Acme Family",
            "investors": [
                {
                    "id": "inv_1",
                    "name": "Alice Acme",
                    "birth_date": "1978-04-12",
                    "milestone_age": 85,
                    "is_rmd_owner": True,
                    "ssn": {"default_claim_age": 67, "external_file": ""},
                },
                {
                    "id": "inv_2",
                    "name": "Bob Acme",
                    "birth_date": "1980-09-20",
                    "milestone_age": 85,
                    "is_rmd_owner": False,
                    "ssn": {"default_claim_age": 70, "external_file": ""},
                },
            ],
            "beneficiaries": [
                {
                    "id": "ben_1",
                    "name": "Charlie Acme",
                    "birth_date": "2010-06-15",
                }
            ],
        }
        self.cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        # Monkey-patch get_default_config_path
        import advisorbot_server
        orig_get_cfg = advisorbot_server.get_default_config_path
        orig_get_pdir = advisorbot_server.get_portfolio_dir
        advisorbot_server.get_default_config_path = lambda pid: self.cfg_file
        advisorbot_server.get_portfolio_dir = lambda pid: self.p_dir

        try:
            settings = get_project_settings("test_family_portfolio")
            self.assertTrue(settings.get("ok"))
            p = settings.get("project") or {}
            self.assertIn("family_members", p)
            fams = p["family_members"]
            self.assertEqual(len(fams), 3)

            # First two are contributors
            self.assertTrue(fams[0]["is_ssn_contributor"])
            self.assertTrue(fams[0]["is_rmd_owner"])
            self.assertEqual(fams[0]["name"], "Alice Acme")

            self.assertTrue(fams[1]["is_ssn_contributor"])
            self.assertFalse(fams[1]["is_rmd_owner"])
            self.assertEqual(fams[1]["name"], "Bob Acme")

            # Beneficiary is non-contributor
            self.assertFalse(fams[2]["is_ssn_contributor"])
            self.assertFalse(fams[2]["is_rmd_owner"])
            self.assertEqual(fams[2]["name"], "Charlie Acme")
        finally:
            advisorbot_server.get_default_config_path = orig_get_cfg
            advisorbot_server.get_portfolio_dir = orig_get_pdir

    def test_save_project_config_persists_family_members(self) -> None:
        cfg: dict[str, Any] = {
            "household_name": "Test Household",
            "investors": [],
            "beneficiaries": [],
        }
        self.cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        import advisorbot_server
        orig_get_cfg = advisorbot_server.get_default_config_path
        orig_get_pdir = advisorbot_server.get_portfolio_dir
        advisorbot_server.get_default_config_path = lambda pid: self.cfg_file
        advisorbot_server.get_portfolio_dir = lambda pid: self.p_dir

        try:
            body = {
                "portfolio_id": "test_family_portfolio",
                "rebuild": False,
                "household_name": "Updated Household",
                "family_members": [
                    {
                        "id": "fam_1",
                        "name": "Pat Contributor",
                        "birth_date": "1972-02-14",
                        "milestone_age": 90,
                        "is_rmd_owner": True,
                        "is_ssn_contributor": True,
                        "ssn": {"default_claim_age": 68},
                    },
                    {
                        "id": "fam_2",
                        "name": "Dana Dependent",
                        "birth_date": "2008-11-03",
                        "milestone_age": 85,
                        "is_rmd_owner": False,
                        "is_ssn_contributor": False,
                    },
                ],
                "investors": [
                    {
                        "id": "fam_1",
                        "name": "Pat Contributor",
                        "birth_date": "1972-02-14",
                        "milestone_age": 90,
                        "is_rmd_owner": True,
                        "ssn": {"default_claim_age": 68},
                    }
                ],
                "beneficiaries": [
                    {
                        "id": "fam_2",
                        "name": "Dana Dependent",
                        "birth_date": "2008-11-03",
                    }
                ],
            }
            res = handle_save_project_settings(body)
            self.assertTrue(res.get("ok"))

            # Read back saved file
            saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertIn("family_members", saved)
            self.assertEqual(len(saved["family_members"]), 2)
            self.assertEqual(saved["family_members"][0]["name"], "Pat Contributor")
            self.assertTrue(saved["family_members"][0]["is_rmd_owner"])
            self.assertTrue(saved["family_members"][0]["is_ssn_contributor"])
            self.assertEqual(saved["family_members"][1]["name"], "Dana Dependent")
            self.assertFalse(saved["family_members"][1]["is_ssn_contributor"])
        finally:
            advisorbot_server.get_default_config_path = orig_get_cfg
            advisorbot_server.get_portfolio_dir = orig_get_pdir


class TestFamilyMembersFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.support import env as _env, pipeline as _pipe, roots as _roots

        cls._tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = Path(cls._tmp_dir.name)
        _roots.assemble_root(tmp_root, ["p1_index_saver"])
        with _env.pointed_at(tmp_root):
            _pipe.clear_payload_cache()
            cls.html = _pipe.rendered_html("p1_index_saver")
            _pipe.clear_payload_cache()
        cls.html_dir = tmp_root
        (cls.html_dir / "index.html").write_text(cls.html, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp_dir.cleanup()

    def test_html_contains_family_elements(self) -> None:
        self.assertIn('id="proj-tab-btn-family"', self.html)
        self.assertIn('id="proj-pane-family"', self.html)
        self.assertIn('id="proj-family-list"', self.html)
        self.assertIn('id="proj-add-family-btn"', self.html)
        self.assertIn('id="proj-family-detail-box"', self.html)
        self.assertIn('id="proj-family-detail-content"', self.html)

    def test_node_family_members_logic(self) -> None:
        import subprocess
        node_script = r"""
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('index.html', 'utf8');
const scriptMatch = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/gi);
let combined = "";
if (scriptMatch) {
  for (const s of scriptMatch) {
    const code = s.replace(/^<script\b[^>]*>/i, '').replace(/<\/script>$/i, '');
    combined += code + "\n;\n";
  }
}

const _elems = new Map();
function makeEl(id = "el") {
  if (_elems.has(id)) return _elems.get(id);
  const el = {
    id,
    tagName: "DIV",
    value: "",
    textContent: "",
    innerHTML: "",
    checked: false,
    disabled: false,
    dataset: {},
    style: { display: "" },
    classList: {
      _classes: new Set(),
      add(...c) { c.forEach(x => this._classes.add(x)); },
      remove(...c) { c.forEach(x => this._classes.delete(x)); },
      toggle(c, force) {
        if (force === undefined) {
          if (this._classes.has(c)) { this._classes.delete(c); return false; }
          else { this._classes.add(c); return true; }
        } else if (force) { this._classes.add(c); return true; }
        else { this._classes.delete(c); return false; }
      },
      contains(c) { return this._classes.has(c); }
    },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    appendChild() {},
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; }
  };
  _elems.set(id, el);
  return el;
}

const doc = {
  getElementById: (id) => makeEl(id),
  querySelector: () => makeEl(),
  querySelectorAll: () => [],
  createElement: () => makeEl(),
  addEventListener: () => {},
  body: makeEl(),
  documentElement: makeEl()
};

const win = {
  addEventListener: () => {},
  location: { hash: "", href: "http://127.0.0.1:8765/", search: "", reload: () => {} },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  devicePixelRatio: 1,
  getComputedStyle: () => makeEl()
};

const ctxObj = {
  document: doc, window: win, navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: (f) => { f(); return 0; },
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol
};
ctxObj.globalThis = ctxObj;
ctxObj.window = win;
ctxObj.self = ctxObj;
win.window = win;
win.document = doc;
ctxObj.addEventListener = () => {};
ctxObj.location = win.location;
ctxObj.history = { replaceState: () => {} };
ctxObj.URL = URL;
ctxObj.Promise = Promise;

const ctx = vm.createContext(ctxObj);
vm.runInContext(combined, ctx);

// Test switchProjectSettingsTab
vm.runInContext("switchProjectSettingsTab('family');", ctx);
vm.runInContext("switchProjectSettingsTab('investors');", ctx); // Backward compatibility

// Test Family Members draft init and constraints
vm.runInContext(`
  _projDraft = {
    household_name: 'Test Family',
    family_members: [
      { id: 'm1', name: 'Member 1', birth_date: '1970-01-01', milestone_age: 85, is_rmd_owner: true, is_ssn_contributor: true, ssn: { default_claim_age: 67 } },
      { id: 'm2', name: 'Member 2', birth_date: '1975-01-01', milestone_age: 85, is_rmd_owner: false, is_ssn_contributor: true, ssn: { default_claim_age: 70 } },
      { id: 'm3', name: 'Member 3', birth_date: '2005-01-01', milestone_age: 85, is_rmd_owner: false, is_ssn_contributor: false }
    ]
  };
  _initFamilyMembersFromDraft();
`, ctx);

const res = vm.runInContext(`
  ({
    count: _projDraft.family_members.length,
    investorsCount: _projDraft.investors.length,
    beneficiariesCount: _projDraft.beneficiaries.length,
    rmdCount: _projDraft.family_members.filter(m => m.is_rmd_owner).length,
    ssnCount: _projDraft.family_members.filter(m => m.is_ssn_contributor).length
  })
`, ctx);

if (res.count !== 3) throw new Error("Expected 3 family members, got " + res.count);
if (res.investorsCount !== 2) throw new Error("Expected 2 investors, got " + res.investorsCount);
if (res.beneficiariesCount !== 1) throw new Error("Expected 1 beneficiary, got " + res.beneficiariesCount);
if (res.rmdCount !== 1) throw new Error("Expected exactly 1 RMD owner, got " + res.rmdCount);
if (res.ssnCount !== 2) throw new Error("Expected exactly 2 SSN contributors, got " + res.ssnCount);

// Test adding a 4th member: should be non-contributor by default
vm.runInContext("addProjectFamilyMemberRow();", ctx);
const afterAdd = vm.runInContext(`
  ({
    count: _projDraft.family_members.length,
    lastIsSsn: _projDraft.family_members[_projDraft.family_members.length - 1].is_ssn_contributor
  })
`, ctx);
if (afterAdd.count !== 4) throw new Error("Expected 4 members after add");
if (afterAdd.lastIsSsn !== false) throw new Error("New member should not contribute SSN by default");

// Test rendering of top list and capsules
vm.runInContext("selectProjectFamilyMember(0); renderProjectFamilyMembers();", ctx);
const listHtml = vm.runInContext("document.getElementById('proj-family-list').innerHTML", ctx);
const rmdMatches = (listHtml.match(/RMD Owner \((?:73|75)\+\)/g) || []).length;
const ssnMatches = (listHtml.match(/SSN Contributor #/g) || []).length;
if (rmdMatches !== 1) throw new Error("Expected exactly 1 RMD Owner capsule, found " + rmdMatches);
if (ssnMatches !== 2) throw new Error("Expected exactly 2 SSN Contributor capsules, found " + ssnMatches);

// Test Detail Box for Contributor (Member 0)
const detail0Html = vm.runInContext("document.getElementById('proj-family-detail-content').innerHTML", ctx);
if (!detail0Html.includes("Will contribute SSN during retirement.")) {
  throw new Error("Expected 'Will contribute SSN during retirement.' for contributor");
}
if (!detail0Html.includes('id="proj-cur-fam-claim"') || detail0Html.includes('id="proj-cur-fam-claim" class="save-name-input mono" style="width:100%;opacity:0.4;cursor:not-allowed;" min="62" max="70" value="67" disabled')) {
  throw new Error("Claim age should be interactive for contributor");
}

// Test Detail Box for Non-Contributor (Member 2): Work section IS uniformly rendered, SSN claim age disabled, RMD Owner enabled
vm.runInContext("selectProjectFamilyMember(2);", ctx);
const detail2Html = vm.runInContext("document.getElementById('proj-family-detail-content').innerHTML", ctx);
if (!detail2Html.includes("Will not contribute SSN during retirement.")) {
  throw new Error("Expected 'Will not contribute SSN during retirement.' for non-contributor");
}
if (!detail2Html.includes('id="proj-fam-sec-work"')) {
  throw new Error("Expected uniform Work & Pre-Retirement Plan section on non-SSN family member");
}
if (!detail2Html.includes('id="proj-cur-fam-claim"') || !detail2Html.includes('disabled')) {
  throw new Error("Expected disabled claim age for non-contributor");
}

// Test RMD Owner can be assigned to ANY family member (including non-SSN Member 2) and 0 SSN contributors is allowed
vm.runInContext("selectProjectFamilyMember(2); onToggleFamilyRmdOwner(true);", ctx);
const rmdNonSsnIdx = vm.runInContext("_projDraft.family_members.findIndex(m => m.is_rmd_owner)", ctx);
if (rmdNonSsnIdx !== 2) throw new Error("Expected non-SSN member index 2 to become RMD owner, got " + rmdNonSsnIdx);

vm.runInContext("selectProjectFamilyMember(1); onToggleFamilyRmdOwner(true);", ctx);
const rmdOwnerIdx = vm.runInContext("_projDraft.family_members.findIndex(m => m.is_rmd_owner)", ctx);
if (rmdOwnerIdx !== 1) throw new Error("Expected member index 1 to be RMD owner after toggle, got " + rmdOwnerIdx);
const rmdOwnerCount = vm.runInContext("_projDraft.family_members.filter(m => m.is_rmd_owner).length", ctx);
if (rmdOwnerCount !== 1) throw new Error("Expected exactly 1 RMD owner, got " + rmdOwnerCount);

// Uncheck SSN on both contributors -> 0 SSN contributors allowed without forcing SSN back onto Member 0
vm.runInContext("selectProjectFamilyMember(0); onToggleFamilySsnContribution(false); selectProjectFamilyMember(1); onToggleFamilySsnContribution(false);", ctx);
const zeroSsnCount = vm.runInContext("_projDraft.family_members.filter(m => m.is_ssn_contributor).length", ctx);
if (zeroSsnCount !== 0) throw new Error("Expected 0 SSN contributors to be allowed, got " + zeroSsnCount);
if (vm.runInContext("_projDraft.family_members.filter(m => m.is_rmd_owner).length", ctx) !== 1) {
  throw new Error("Expected RMD owner to remain assigned when 0 SSN contributors exist");
}

// Default Working state: 1st family member defaults to Working (true), subsequent members default to Not Working (false)
const defFirstWorking = vm.runInContext(`
  const freshDraft = _buildDefaultDraftForPortfolio('Fresh Household');
  _projDraft = freshDraft;
  _initFamilyMembersFromDraft();
  const m1Work = Boolean(_projDraft.family_members[0].work_plan && _projDraft.family_members[0].work_plan.is_working);
  addProjectFamilyMemberRow();
  const m2Work = Boolean(_projDraft.family_members[1].work_plan && _projDraft.family_members[1].work_plan.is_working);
  addProjectFamilyMemberRow();
  const m3Work = Boolean(_projDraft.family_members[2].work_plan && _projDraft.family_members[2].work_plan.is_working);
  ({ m1Work, m2Work, m3Work })
`, ctx);
if (defFirstWorking.m1Work !== true) throw new Error("Expected 1st family member to default to Working (true)");
if (defFirstWorking.m2Work !== false || defFirstWorking.m3Work !== false) {
  throw new Error("Expected subsequent family members to default to Not Working (false)");
}

// Dynamic narrative check on Member 0 (Working) vs Member 1 (Not Working)
vm.runInContext("selectProjectFamilyMember(0); renderProjectFamilyMembers();", ctx);
const work0Html = vm.runInContext("document.getElementById('proj-family-detail-content').innerHTML", ctx);
if (!work0Html.includes("Monthly earnings income can optionally be distributed")) {
  throw new Error("Expected Working narrative on Working member");
}
if (!work0Html.includes("Target retirement age")) {
  throw new Error("Expected 'Target retirement age' label on Working member");
}
vm.runInContext("selectProjectFamilyMember(1); renderProjectFamilyMembers();", ctx);
const work1Html = vm.runInContext("document.getElementById('proj-family-detail-content').innerHTML", ctx);
if (!work1Html.includes("does not model any paycheck income or contributions to the portfolio")) {
  throw new Error("Expected Not Working narrative on Not Working member");
}

// Verify tax-adjusted Retirement Savings paycheck breakdown & hide-on-zero-remaining logic
const bdCheck = vm.runInContext(`
  const elig = _getEligibleWorkPlanAssets();
  const pretaxAsset = elig.find(a => a.tax_model === 'ORDINARY_FULL');
  const rothOrTaxAsset = elig.find(a => a.tax_model !== 'ORDINARY_FULL');
  const draftCfg = { reference_annual_burn: 120000, sim_ira_ordinary_tax_rate: 0.40 };
  const wpZeroRem = sanitizeWorkPlanJs({
    is_working: true,
    default_retirement_age: 65,
    annual_take_home: 120000,
    budget_answered: true,
    annual_budget_offset: 120000,
    contributions: [],
  }, 85);
  const bdZero = _computeWorkPlanPaycheckBreakdown(wpZeroRem, draftCfg);
  const wpTaxAdj = sanitizeWorkPlanJs({
    is_working: true,
    default_retirement_age: 65,
    annual_take_home: 200000,
    budget_answered: true,
    annual_budget_offset: 120000,
    contributions: [
      { target_asset_id: pretaxAsset ? pretaxAsset.id : '', employee_annual: 20000, employer_match_annual: 10000 },
      { target_asset_id: rothOrTaxAsset ? rothOrTaxAsset.id : '', employee_annual: 10000, employer_match_annual: 5000 },
    ],
  }, 85);
  const bdAdj = _computeWorkPlanPaycheckBreakdown(wpTaxAdj, draftCfg);
  ({
    hasPretax: Boolean(pretaxAsset),
    zeroShowBar: bdZero.showRetirementSavingsBar,
    adjShowBar: bdAdj.showRetirementSavingsBar,
    empAfterTax: Math.round(bdAdj.empAfterTaxTotal),
    empMatch: Math.round(bdAdj.employerMatchTotal),
    remaining: Math.round(bdAdj.remainingAfterTax),
  })
`, ctx);
if (bdCheck.zeroShowBar !== false) {
  throw new Error("Expected Retirement Savings bar to hide when $0 paycheck remains and no contributions are configured");
}
if (bdCheck.adjShowBar !== true) {
  throw new Error("Expected Retirement Savings bar to show when paycheck remains");
}
if (bdCheck.hasPretax) {
  // $20,000 Pre-Tax at 40% rate = $12,000 after-tax cost + $10,000 Roth/Taxable (1:1) = $22,000 after-tax cost;
  // $15,000 Employer Match is NOT deducted from paycheck -> Remaining = $200k - $120k - $22k = $58,000
  if (bdCheck.empAfterTax !== 22000 || bdCheck.empMatch !== 15000 || bdCheck.remaining !== 58000) {
    throw new Error("Unexpected tax-adjusted paycheck breakdown: " + JSON.stringify(bdCheck));
  }
}

console.log("ALL_JS_FAMILY_TESTS_PASSED");
""";
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(self.html_dir),
        )
        self.assertEqual(proc.returncode, 0, f"Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("ALL_JS_FAMILY_TESTS_PASSED", proc.stdout)


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
