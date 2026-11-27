#!/usr/bin/env python3
"""
Test dirty-state / unsaved indicator audit (Phase D RSI.html Sec 3.5):
Asserts that View-Only controls (screen switches, dollar mode, axis scales) do NOT trigger
an unsaved dirty state, while Category (A) Saved Scenario Input controls (active prediction lens,
per-holding forecasts, hypothetical assets, scheduled actions, burn/inflation/SSN sliders,
stress/MC/cohort parameters) DO trigger dirty state and restore clean state when reverted.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "Scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.support import env as _env_support, pipeline as _pipe_support, roots as _roots_support  # noqa: E402

NODE_SCRIPT = """
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const m = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
const script = m[m.length - 1][1];

const mk = () => new Proxy(function(){}, {
  get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p==="length"?0:(p==="value"?"":(p==="classList"?mk():(p==="style"?mk():(p==="dataset"?mk():stub)))))),
  apply: () => stub, set: () => true, has: () => true,
});
const stub = mk();
const _els = new Map();
const _getEl = (id) => {
  if (!_els.has(id)) {
    const state = { value: "", textContent: "", innerHTML: "", checked: false };
    _els.set(id, new Proxy(function(){}, {
      get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p in state ? state[p] : (p==="length"?0:(p==="classList"?mk():(p==="style"?mk():(p==="dataset"?mk():stub)))))),
      set: (t,p,v) => {
        state[p] = v;
        if (p === "innerHTML" && typeof v === "string") {
          for (const m of v.matchAll(/<input\\b[^>]*>/gi)) {
            const idm = m[0].match(/\\bid="([^"]+)"/);
            const valm = m[0].match(/\\bvalue="([^"]*)"/);
            if (idm) {
              if (valm) _getEl(idm[1]).value = valm[1];
              _getEl(idm[1]).checked = /\\bchecked\\b/i.test(m[0]);
            }
          }
        }
        return true;
      },
      apply: () => stub, has: () => true,
    }));
  }
  return _els.get(id);
};
const doc = { getElementById:(id)=>_getEl(id), querySelector:()=>stub, querySelectorAll:()=>[], createElement:()=>stub, createElementNS:()=>stub, addEventListener:()=>{}, body:stub, documentElement:stub };
const win = { addEventListener:()=>{}, location:{hash:"",href:""}, matchMedia:()=>({matches:false,addEventListener:()=>{}}), devicePixelRatio:1, getComputedStyle:()=>stub };
const ctxObj = {
  document:doc, window:win, navigator:{userAgent:"node"}, console,
  localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
  setInterval:()=>0, clearInterval:()=>{}, setTimeout:(f)=>0, clearTimeout:()=>{}, requestAnimationFrame:(f)=>{ f(); return 0; },
  fetch:()=>Promise.resolve({ok:false,json:async()=>({})}),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
};
ctxObj.globalThis = ctxObj; ctxObj.window = ctxObj; ctxObj.self = ctxObj;
ctxObj.addEventListener=()=>{}; ctxObj.removeEventListener=()=>{};
ctxObj.location={hash:"",href:"",search:""}; ctxObj.matchMedia=()=>({matches:false,addEventListener:()=>{}});
ctxObj.getComputedStyle=()=>stub; ctxObj.devicePixelRatio=1; ctxObj.innerWidth=1200; ctxObj.innerHeight=800;
ctxObj.history={replaceState:()=>{}};
ctxObj.URL=URL; ctxObj.Promise=Promise; ctxObj.structuredClone=structuredClone;
ctxObj.AbortController=AbortController; ctxObj.TextEncoder=TextEncoder; ctxObj.TextDecoder=TextDecoder;

const ctx = vm.createContext(ctxObj);
vm.runInContext(script, ctx);

async function runAudit() {
  await vm.runInContext("initSavesSystem()", ctx);
  const getDirty = () => vm.runInContext("isSaveDirty", ctx);

  const results = {};
  results.initialDirty = getDirty();

  // Test 1: View-Only operations (screen navigation, dollar mode, scale mode) MUST NOT trigger dirty state
  results.screenDirty = {};
  for (const s of ["portfolio", "google", "retirement", "income", "models"]) {
    vm.runInContext(`switchScreen("${s}")`, ctx);
    results.screenDirty[s] = getDirty();
  }

  results.dollarDirty = {};
  for (const dm of ["real", "nominal"]) {
    vm.runInContext(`setSimDollarMode("${dm}")`, ctx);
    results.dollarDirty[dm] = getDirty();
  }

  const scales = ["log", "linear", "auto"];
  results.scaleDirty = {};
  for (const s of scales) {
    vm.runInContext(`setSimScaleMode("${s}")`, ctx);
    results.scaleDirty[s] = getDirty();
  }

  // Test 2: Active Model Lens (`simActiveModel`) IS a Saved Model Input in Phase D
  const origLens = vm.runInContext("simActiveModel", ctx);
  const altLens = origLens === "range" ? "single_path" : "range";
  vm.runInContext(`setSimModel("${altLens}")`, ctx);
  results.lensChangedDirty = getDirty();
  vm.runInContext(`setSimModel("${origLens}")`, ctx);
  results.lensRevertedDirty = getDirty();

  // Test 3: Annual Burn & Holding Forecast inputs mark dirty, and reverting restores clean state
  const origBurn = vm.runInContext("simAnnualBurn", ctx);
  vm.runInContext(`onSimBurnInput(${origBurn + 15000})`, ctx);
  results.growthChangedDirty = getDirty();
  vm.runInContext(`onSimBurnInput(${origBurn})`, ctx);
  results.growthRevertedDirty = getDirty();

  // Test 4: Stress scenario
  const origStress = vm.runInContext("simStressScenario", ctx);
  const altStress = origStress === "gfc2008" ? "dotcom2000" : "gfc2008";
  vm.runInContext(`setStressScenario("${altStress}")`, ctx);
  results.stressChangedDirty = getDirty();
  vm.runInContext(`setStressScenario("${origStress}")`, ctx);
  results.stressRevertedDirty = getDirty();

  // Test 5: Monte Carlo volatility
  const origVol = vm.runInContext("mcSingleStockVolPct", ctx);
  vm.runInContext(`onMcVolGoogInput(${origVol + 4})`, ctx);
  results.mcVolChangedDirty = getDirty();
  vm.runInContext(`onMcVolGoogInput(${origVol})`, ctx);
  results.mcVolRevertedDirty = getDirty();

  // Test 6: Cohort center mode
  const origCenter = vm.runInContext("cohortCenterMode", ctx);
  vm.runInContext(`setCohortCenter("${origCenter === "anchor" ? "raw" : "anchor"}")`, ctx);
  results.cohortChangedDirty = getDirty();
  vm.runInContext(`setCohortCenter("${origCenter}")`, ctx);
  results.cohortRevertedDirty = getDirty();

  // Test 7: SSN claim age
  const origAge = vm.runInContext("ssnInv0ClaimAge", ctx);
  vm.runInContext(`onSsnInv0AgeInput(${origAge === 67 ? 65 : 67})`, ctx);
  results.ssnChangedDirty = getDirty();
  vm.runInContext(`onSsnInv0AgeInput(${origAge})`, ctx);
  results.ssnRevertedDirty = getDirty();

  // Test 8: Project Settings Modal open/close & state (including Tab 5 advisor_prompts)
  await vm.runInContext("openProjectSettingsDialog()", ctx);
  results.projDraft = vm.runInContext("_projDraft", ctx);
  vm.runInContext("closeProjectSettingsDialog()", ctx);

  console.log(JSON.stringify(results));
}

runAudit().catch(err => { console.error(err); process.exit(1); });
"""


class DirtyStateAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        troot = Path(cls._tmp.name)
        _roots_support.assemble_root(troot, ["p2_equity_comp_rsu"])
        with _env_support.pointed_at(troot):
            _pipe_support.clear_payload_cache()
            html = _pipe_support.rendered_html("p2_equity_comp_rsu")
            _pipe_support.clear_payload_cache()
        cls.index_path = troot / "index.html"
        cls.index_path.write_text(html, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_view_vs_calculation_dirty_state(self):
        res = subprocess.run(
            ["node", "--input-type=module", "-e", NODE_SCRIPT, str(self.index_path)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        data = json.loads(res.stdout.strip().splitlines()[-1])

        self.assertFalse(data["initialDirty"])

        for scr, dirty in data["screenDirty"].items():
            self.assertFalse(dirty, f"switchScreen('{scr}') unexpectedly triggered dirty state")
        for dm, dirty in data["dollarDirty"].items():
            self.assertFalse(dirty, f"setSimDollarMode('{dm}') unexpectedly triggered dirty state")
        for scale, dirty in data["scaleDirty"].items():
            self.assertFalse(dirty, f"setSimScaleMode('{scale}') unexpectedly triggered dirty state")

        self.assertTrue(data["lensChangedDirty"], "Switching Saved Model Lens must mark model dirty")
        self.assertFalse(data["lensRevertedDirty"], "Reverting Saved Model Lens must restore clean state")

        self.assertTrue(data["growthChangedDirty"], "Changing holding forecast must mark model dirty")
        self.assertFalse(data["growthRevertedDirty"], "Reverting holding forecast must restore clean state")

        self.assertTrue(data["stressChangedDirty"], "Selecting stress scenario must mark model dirty")
        self.assertFalse(data["stressRevertedDirty"], "Reverting stress scenario must restore clean state")

        self.assertTrue(data["mcVolChangedDirty"], "Changing Monte Carlo vol must mark model dirty")
        self.assertFalse(data["mcVolRevertedDirty"], "Reverting Monte Carlo vol must restore clean state")

        self.assertTrue(data["cohortChangedDirty"], "Changing cohort center mode must mark model dirty")
        self.assertFalse(data["cohortRevertedDirty"], "Reverting cohort center mode must restore clean state")

        self.assertTrue(data["ssnChangedDirty"], "Changing SSN claim age must mark model dirty")
        self.assertFalse(data["ssnRevertedDirty"], "Reverting SSN claim age must restore clean state")

        html = self.index_path.read_text(encoding="utf-8")
        self.assertIn("#project-settings-backdrop.open", html)
        self.assertNotIn('<div id="project-settings-backdrop" style="display:none', html)
        proj_draft = data.get("projDraft") or {}
        self.assertGreaterEqual(len(proj_draft.get("investors") or []), 1)
        for inv in proj_draft["investors"]:
            # SA-08: exported ReportPayload minimizes DOB to YYYY-MM in static index.html
            self.assertRegex(inv.get("birth_date", ""), r"^\d{4}-\d{2}(-\d{2})?$")
            self.assertIn(inv["birth_date"], html)

    def test_reload_scenario_revert_and_ui(self):
        """Verifies that Ctrl-R / Cmd-R reverts modifications, reloads scenario, and updates left-nav status."""
        node_script = """
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const m = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
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

const elems = {
  "save-current-name": { textContent: "", title: "", style: {} },
  "save-status-sub": { textContent: "", title: "", style: {}, classList: mkClassList() },
  "save-status-dot": { title: "", style: {}, classList: mkClassList() },
  "nav-disk-btn": { title: "", style: {}, classList: mkClassList() },
  "nav-reload-btn": { title: "", style: {}, classList: mkClassList() },
  "nav-btn-models": { title: "", style: {}, classList: mkClassList() },
};

const stub = new Proxy(function(){}, {
  get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p==="length"?0:(p==="value"?"":(p==="classList"?mkClassList():(p==="style"?{}:(p==="dataset"?{}:stub)))))),
  apply: () => stub, set: () => true, has: () => true,
});
const doc = {
  getElementById: (id) => elems[id] || stub,
  querySelector: () => stub, querySelectorAll: () => [], createElement: () => stub,
  createElementNS: () => stub, addEventListener: () => {}, body: stub, documentElement: stub,
};
const win = { addEventListener: () => {}, location: { hash: "", href: "" }, matchMedia: () => ({ matches: false, addEventListener: () => {} }), devicePixelRatio: 1, getComputedStyle: () => stub };

const ctxObj = {
  document: doc, window: win, navigator: { userAgent: "Macintosh; Intel Mac OS X 10_15_7" }, console,
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

async function run() {
  vm.runInContext('if (typeof _privacyConfig !== "undefined" && _privacyConfig) { if (_privacyConfig.demo_mode) _privacyConfig.demo_mode.enabled = false; } else { _privacyConfig = { demo_mode: { enabled: false } }; } if (typeof PAYLOAD !== "undefined" && PAYLOAD && PAYLOAD.privacy_status) { PAYLOAD.privacy_status.demo_enabled = false; }', ctx);
  await vm.runInContext("initSavesSystem()", ctx);
  const getDirty = () => vm.runInContext("isSaveDirty", ctx);

  const cleanSub = elems["save-status-sub"].textContent;
  const origGoog = vm.runInContext("futureGoogGrowthPct", ctx);

  // Modify growth: should mark dirty
  vm.runInContext(`onGoogGrowthInput(${origGoog + 5})`, ctx);
  const dirtyState = getDirty();
  const dirtySub = elems["save-status-sub"].textContent;
  const reloadBtnDisplay = elems["nav-reload-btn"].style.display;

  // Revert via reloadCurrentScenario()
  await vm.runInContext("reloadCurrentScenario()", ctx);
  const revertedDirty = getDirty();
  const revertedGoog = vm.runInContext("futureGoogGrowthPct", ctx);
  const postRevertSub = elems["save-status-sub"].textContent;
  const postReloadBtnDisplay = elems["nav-reload-btn"].style.display;

  console.log(JSON.stringify({
    cleanSub,
    dirtyState,
    dirtySub,
    reloadBtnDisplay,
    revertedDirty,
    origGoog,
    revertedGoog,
    postRevertSub,
    postReloadBtnDisplay,
  }));
}

run().catch(err => { console.error(err); process.exit(1); });
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(self.index_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        data = json.loads(res.stdout.strip().splitlines()[-1])

        self.assertTrue(data["dirtyState"], "Modifying holding forecast must set isSaveDirty=True")
        self.assertIn("Modified", data["dirtySub"])
        self.assertIn("Save", data["dirtySub"])
        self.assertIn("Reload", data["dirtySub"])
        self.assertIn("⌘S", data["dirtySub"])
        self.assertIn("⌘R", data["dirtySub"])
        self.assertEqual(data["reloadBtnDisplay"], "inline-flex")

        self.assertFalse(data["revertedDirty"], "reloadCurrentScenario() must restore isSaveDirty=False")
        self.assertEqual(data["origGoog"], data["revertedGoog"], "reloadCurrentScenario() must restore original growth rate")
        self.assertNotIn("Modified", data["postRevertSub"])
        self.assertEqual(data["postReloadBtnDisplay"], "none")

        # HTML assertions
        html = self.index_path.read_text(encoding="utf-8")
        self.assertIn('id="nav-reload-btn"', html)
        self.assertIn("reloadCurrentScenario", html)
        self.assertIn('key === "r" || evt.key === "R"', html)

    def test_historical_mode_dollar_toggle_updates_top_kpis(self):
        """Toggling Nominal $ / Today's $ in Historical mode updates all 3 top KPI cards."""
        node_script = """
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const m = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
const script = m[m.length - 1][1];

const mk = () => new Proxy(function(){}, {
  get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p==="length"?0:(p==="value"?"":(p==="classList"?mk():(p==="style"?mk():(p==="dataset"?mk():stub)))))),
  apply: () => stub, set: () => true, has: () => true,
});
const stub = mk();
const _els = new Map();
const _getEl = (id) => {
  if (!_els.has(id)) {
    const state = { value: "", textContent: "", innerHTML: "", checked: false };
    _els.set(id, new Proxy(function(){}, {
      get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p in state ? state[p] : (p==="length"?0:(p==="classList"?mk():(p==="style"?mk():(p==="dataset"?mk():stub)))))),
      set: (t,p,v) => { state[p] = v; return true; },
      apply: () => stub, has: () => true,
    }));
  }
  return _els.get(id);
};
const doc = { getElementById:(id)=>_getEl(id), querySelector:()=>stub, querySelectorAll:()=>[], createElement:()=>stub, createElementNS:()=>stub, addEventListener:()=>{}, body:stub, documentElement:stub };
const win = { addEventListener:()=>{}, location:{hash:"",href:""}, matchMedia:()=>({matches:false,addEventListener:()=>{}}), devicePixelRatio:1, getComputedStyle:()=>stub };
const ctxObj = {
  document:doc, window:win, navigator:{userAgent:"node"}, console,
  localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
  setInterval:()=>0, clearInterval:()=>{}, setTimeout:()=>0, clearTimeout:()=>{}, requestAnimationFrame:(f)=>{ f(); return 0; },
  fetch:()=>Promise.resolve({ok:false,json:async()=>({})}),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  URL, Promise, structuredClone, AbortController, TextEncoder, TextDecoder,
  addEventListener:()=>{}, removeEventListener:()=>{},
  location:{hash:"",href:"",search:""}, matchMedia:()=>({matches:false,addEventListener:()=>{}}),
  getComputedStyle:()=>stub, devicePixelRatio:1, innerWidth:1200, innerHeight:800,
  history:{replaceState:()=>{}},
};
ctxObj.globalThis = ctxObj; ctxObj.window = ctxObj; ctxObj.self = ctxObj;
const ctx = vm.createContext(ctxObj);
vm.runInContext(script, ctx);

async function run() {
  await vm.runInContext("initSavesSystem()", ctx);
  vm.runInContext(`
    switchScreen("retirement");
    onSimBurnInput(10000);
    setSimModel("historical");
    getCohortResult();
  `, ctx);

  const readCards = () => ({
    inhVal: _getEl("sim-kpi-inheritance").innerHTML,
    inhReal: _getEl("sim-kpi-inheritance-real").textContent,
    inhSub: _getEl("sim-kpi-inheritance-sub").textContent,
    solvSub: _getEl("sim-kpi-solvency-sub").textContent,
    taxVal: _getEl("sim-kpi-taxdrag").textContent,
    taxShelter: _getEl("sim-kpi-taxdrag-shelter").textContent,
    taxSub: _getEl("sim-kpi-taxdrag-sub").textContent,
  });

  vm.runInContext('setSimDollarMode("nominal")', ctx);
  const nominal = readCards();

  vm.runInContext('setSimDollarMode("real")', ctx);
  const real = readCards();

  console.log(JSON.stringify({ nominal, real }));
}

run().catch(err => { console.error(err); process.exit(1); });
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(self.index_path)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        out = json.loads(res.stdout.strip().splitlines()[-1])
        nom, real = out["nominal"], out["real"]

        self.assertNotEqual(nom["inhVal"], real["inhVal"])
        self.assertIn("(nominal $)", nom["inhReal"])
        self.assertIn("(today's $)", real["inhReal"])
        self.assertIn("p25 · p50 · p75 (nominal $):", nom["inhSub"])
        self.assertIn("p25 · p50 · p75 (today's $):", real["inhSub"])
        self.assertNotEqual(nom["inhSub"], real["inhSub"])

        self.assertIn("(Nominal $):", nom["solvSub"])
        self.assertIn("(Today's $):", real["solvSub"])
        self.assertNotEqual(nom["solvSub"], real["solvSub"])

        self.assertIn("(nominal $)", nom["taxShelter"])
        self.assertIn("(today's $)", real["taxShelter"])
        self.assertNotEqual(nom["taxShelter"], real["taxShelter"])
        self.assertIn("(Nominal $)", nom["taxSub"])
        self.assertIn("(Today's $)", real["taxSub"])

    def test_mc_simulations_forced_500_on_old_saves_and_narrative(self):
        """Old saves with non-500 mcSimulations load at 500 paths cleanly, slider is gone, narrative shows 500."""
        html = self.index_path.read_text(encoding="utf-8")
        self.assertNotIn('id="sim-mc-sims-slider"', html)
        self.assertNotIn('id="sim-mc-sims-row"', html)

        node_script = """
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const m = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
const script = m[m.length - 1][1];

const mk = () => new Proxy(function(){}, {
  get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p==="length"?0:(p==="value"?"":(p==="classList"?mk():(p==="style"?mk():(p==="dataset"?mk():stub)))))),
  apply: () => stub, set: () => true, has: () => true,
});
const stub = mk();
const _els = new Map();
const _getEl = (id) => {
  if (!_els.has(id)) {
    const state = { value: "", textContent: "", innerHTML: "", checked: false };
    _els.set(id, new Proxy(function(){}, {
      get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p in state ? state[p] : (p==="length"?0:(p==="classList"?mk():(p==="style"?mk():(p==="dataset"?mk():stub)))))),
      set: (t,p,v) => { state[p] = v; return true; },
      apply: () => stub, has: () => true,
    }));
  }
  return _els.get(id);
};
const doc = { getElementById:(id)=>_getEl(id), querySelector:()=>stub, querySelectorAll:()=>[], createElement:()=>stub, createElementNS:()=>stub, addEventListener:()=>{}, body:stub, documentElement:stub };
const win = { addEventListener:()=>{}, location:{hash:"",href:""}, matchMedia:()=>({matches:false,addEventListener:()=>{}}), devicePixelRatio:1, getComputedStyle:()=>stub };
const ctxObj = {
  document:doc, window:win, navigator:{userAgent:"node"}, console,
  localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
  setInterval:()=>0, clearInterval:()=>{}, setTimeout:()=>0, clearTimeout:()=>{}, requestAnimationFrame:(f)=>{ f(); return 0; },
  fetch:()=>Promise.resolve({ok:false,json:async()=>({})}),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  URL, Promise, structuredClone, AbortController, TextEncoder, TextDecoder,
  addEventListener:()=>{}, removeEventListener:()=>{},
  location:{hash:"",href:"",search:""}, matchMedia:()=>({matches:false,addEventListener:()=>{}}),
  getComputedStyle:()=>stub, devicePixelRatio:1, innerWidth:1200, innerHeight:800,
  history:{replaceState:()=>{}},
};
ctxObj.globalThis = ctxObj; ctxObj.window = ctxObj; ctxObj.self = ctxObj;
const ctx = vm.createContext(ctxObj);
vm.runInContext(script, ctx);

async function run() {
  await vm.runInContext("initSavesSystem()", ctx);
  await vm.runInContext(`
    (async () => {
      const legacyEntry = {
        id: "099",
        name: "Legacy 1000 Paths",
        file: "099_legacy.json",
        settings: {
          simActiveModel: "range",
          simAnnualBurn: 80000,
          mcSimulations: 1000,
          mcReturnModel: "normal",
        },
      };
      modelsList.push(legacyEntry);
      savesRegistry[legacyEntry.name] = legacyEntry;
      await loadModelById("099", true);
    })()
  `, ctx);

  const mcSimsAfterLoad = vm.runInContext("mcSimulations", ctx);
  const snapSims = vm.runInContext("getCurrentSettingsSnapshot().mcSimulations", ctx);
  const dirtyAfterLoad = vm.runInContext("isSaveDirty", ctx);
  const narrHtml = _getEl("sim-mc-narrative").innerHTML;
  const subtextHtml = _getEl("sim-model-summary-subtext").innerHTML;

  console.log(JSON.stringify({
    mcSimsAfterLoad,
    snapSims,
    dirtyAfterLoad,
    narrHtml,
    subtextHtml,
  }));
}

run().catch(err => { console.error(err); process.exit(1); });
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(self.index_path)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        out = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(out["mcSimsAfterLoad"], 500)
        self.assertEqual(out["snapSims"], 500)
        self.assertFalse(out["dirtyAfterLoad"])
        self.assertIn("500", out["narrHtml"])
        self.assertIn("randomized market simulations", out["narrHtml"])
        self.assertIn("500 paths", out["subtextHtml"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
