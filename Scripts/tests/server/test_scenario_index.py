#!/usr/bin/env python3
"""
Phase D Stage 1 tests (`RSI.html` Sec 2):
- Legacy `Project/Saves/` migration to normalized `001_<slug>.json` + `_index.json`.
- Friendly-name decoupling (`rename_model` updates `name` without breaking disk `id`/`file`).
- Markdown `notes_md` & `append_model_upvote` (`author.upvoted_by`).
- Strict `protected_saves` safety guardrails in `llm_tools.tool_manage_saves`.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "Scripts"
sys.path.insert(0, str(SCRIPTS))

import llm_tools  # noqa: E402
import advisorbot_server  # noqa: E402


class ModelsIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.support import env as _env, pipeline as _pipe, roots as _roots

        cls._tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = Path(cls._tmp_dir.name)
        _roots.assemble_root(tmp_root, ["p1_index_saver"])
        with _env.pointed_at(tmp_root):
            _pipe.clear_payload_cache()
            html = _pipe.rendered_html("p1_index_saver")
            _pipe.clear_payload_cache()
        cls.fixture_html_path = tmp_root / "index.html"
        cls.fixture_html_path.write_text(html, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp_dir.cleanup()

    def test_legacy_saves_migration_rename_notes_and_upvote(self):
        with tempfile.TemporaryDirectory() as tmp:
            saves_dir = Path(tmp) / "Saves"
            saves_dir.mkdir(parents=True)

            # Seed legacy pre-Phase-D save files + _meta.json
            (saves_dir / "Default.json").write_text(
                json.dumps(
                    {
                        "name": "Default",
                        "saved_at": "2026-04-01T10:00:00",
                        "settings": {
                            "futureGoogGrowthPct": 12.0,
                            "futureNonGoogGrowthPct": 6.0,
                            "simScheduledWithdrawals": [
                                {"ym": "2028-06", "amount": 100000, "dollar_mode": "today", "comment": "Gift"}
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (saves_dir / "Zero.json").write_text(
                json.dumps({"name": "Zero", "settings": {"futureGoogGrowthPct": 0.0, "futureNonGoogGrowthPct": 0.0}}),
                encoding="utf-8",
            )
            (saves_dir / "_meta.json").write_text(json.dumps({"active_save": "Default"}), encoding="utf-8")

            with patch.object(advisorbot_server, "SAVES_DIR", saves_dir):
                advisorbot_server.ensure_models_migrated(saves_dir)
                idx_path = saves_dir / "_index.json"
                self.assertTrue(idx_path.exists())
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
                self.assertEqual(idx["version"], 1)
                self.assertEqual(len(idx["models"]), 2)

                # Legacy Default.json was migrated to 001_default.json with holdingForecasts & simScheduledActions
                m0 = idx["models"][0]
                self.assertEqual(m0["id"], "001")
                self.assertEqual(m0["name"], "Default")
                self.assertEqual(m0["file"], "001_default.json")
                self.assertFalse((saves_dir / "Default.json").exists())
                doc0 = json.loads((saves_dir / "001_default.json").read_text(encoding="utf-8"))
                self.assertIn("holdingForecasts", doc0["settings"])
                self.assertEqual(len(doc0["settings"]["simScheduledActions"]), 1)
                self.assertEqual(doc0["settings"]["simScheduledActions"][0]["type"], "withdrawal")

                # Create a new non-protected scenario, rename friendly name (disk filename stays 003_...), update notes & upvote
                created = advisorbot_server.write_save_file(
                    name="Advisor Test Plan",
                    settings=doc0["settings"],
                    set_active=True,
                    saves_dir=saves_dir,
                    create_new=True,
                    author={"kind": "ai", "model": "gemini-3.1-pro", "upvoted_by": []},
                    notes_md="## Executive Summary\nInitial rationale.",
                )
                entry = advisorbot_server.get_model_entry(created["saved_id"], saves_dir=saves_dir)
                self.assertIsNotNone(entry)
                self.assertEqual(entry["id"], "003")
                orig_file = entry["file"]
                self.assertTrue((saves_dir / orig_file).exists())

                renamed = advisorbot_server.rename_model("003", "Advisor Plan — Diversified!", saves_dir=saves_dir)
                self.assertEqual(renamed["new_name"], "Advisor Plan — Diversified!")
                ren_entry = advisorbot_server.get_model_entry("003", saves_dir=saves_dir)
                self.assertEqual(ren_entry["name"], "Advisor Plan — Diversified!")
                self.assertEqual(ren_entry["file"], orig_file)
                self.assertTrue((saves_dir / orig_file).exists())

                advisorbot_server.update_model_notes("003", "## Updated Thesis\nLowered concentration.", saves_dir=saves_dir)
                not_doc = json.loads((saves_dir / orig_file).read_text(encoding="utf-8"))
                self.assertIn("Updated Thesis", not_doc["notes_md"])

                upvoted = advisorbot_server.append_model_upvote("003", "claude-opus-4-6", "Confirmed 97% MC solvency", saves_dir=saves_dir)
                self.assertEqual(upvoted["upvoted_id"], "003")
                upv_doc = json.loads((saves_dir / orig_file).read_text(encoding="utf-8"))
                self.assertIn("claude-opus-4-6", upv_doc["notes_md"])

    def test_protected_saves_guardrails_in_llm_tools(self):
        # All saves start out unprotected; toggling protection blocks unconfirmed delete/overwrite
        advisorbot_server.set_model_protected("Default", True)
        try:
            with self.assertRaises(ValueError):
                llm_tools.tool_manage_saves(action="delete", name="Default", apply_to_live_ui=False)

            with self.assertRaises(ValueError):
                llm_tools.tool_manage_saves(
                    action="save",
                    name="Default",
                    overwrite_existing=False,
                    apply_to_live_ui=False,
                )
        finally:
            advisorbot_server.set_model_protected("Default", False)

    def test_scenario_terminology_in_rendered_html(self):
        html_path = self.fixture_html_path
        html = html_path.read_text(encoding="utf-8")
        self.assertIn('id="save-current-name">Scenarios</span>', html)
        self.assertIn('id="nav-disk-btn"', html)
        self.assertIn('id="save-status-dot"', html)
        self.assertNotIn('id="nav-save-bar"', html)
        self.assertIn("Saved Scenarios Library", html)
        self.assertIn("Scenario Note", html)
        self.assertIn("Compare Saved Scenarios", html)
        self.assertNotIn("Simulation Models Library", html)
        self.assertNotIn("Saved Simulation Models", html)

    def test_scenario_nav_button_status_updates_in_js(self):
        html_path = self.fixture_html_path
        import subprocess

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
    list: () => [...set],
  };
};
const elems = {
  "save-current-name": { textContent: "", title: "", style: {} },
  "save-status-sub": { textContent: "", title: "", style: {}, classList: mkClassList() },
  "save-status-dot": { title: "", style: {}, classList: mkClassList() },
  "nav-disk-btn": { title: "", style: {}, classList: mkClassList() },
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
const ctx = {
  document: doc, navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: () => 0,
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  URL, Promise, structuredClone, AbortController, TextEncoder, TextDecoder,
  addEventListener: () => {}, removeEventListener: () => {},
  location: { hash: "", href: "", search: "" }, matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  getComputedStyle: () => stub, devicePixelRatio: 1, innerWidth: 1200, innerHeight: 800,
  history: { replaceState: () => {} },
};
ctx.globalThis = ctx; ctx.window = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(script, ctx);

// Ensure demo mode is explicitly disabled in the test harness context so default save/choose text is verified
vm.runInContext('if (typeof _privacyConfig !== "undefined" && _privacyConfig) { if (_privacyConfig.demo_mode) _privacyConfig.demo_mode.enabled = false; } else { _privacyConfig = { demo_mode: { enabled: false } }; } if (typeof PAYLOAD !== "undefined" && PAYLOAD && PAYLOAD.privacy_status) { PAYLOAD.privacy_status.demo_enabled = false; }', ctx);

// Case 1: No scenario loaded, clean
vm.runInContext('currentSaveName = ""; isSaveDirty = false; updateSaveStatusUI(false);', ctx);
const noneClean = {
  title: elems["save-current-name"].textContent,
  sub: elems["save-status-sub"].textContent,
  dotSaved: elems["save-status-dot"].classList.contains("saved"),
  dotDirty: elems["save-status-dot"].classList.contains("dirty"),
};

// Case 2: Scenario loaded, clean
vm.runInContext('currentSaveName = "Very Long Retirement Scenario Name For Ellipsis"; isSaveDirty = false; updateSaveStatusUI(false);', ctx);
const loadedClean = {
  title: elems["save-current-name"].textContent,
  sub: elems["save-status-sub"].textContent,
  dotSaved: elems["save-status-dot"].classList.contains("saved"),
  dotDirty: elems["save-status-dot"].classList.contains("dirty"),
};

// Case 3: Scenario loaded, modified (dirty)
vm.runInContext('isSaveDirty = true; updateSaveStatusUI(false);', ctx);
const loadedDirty = {
  title: elems["save-current-name"].textContent,
  sub: elems["save-status-sub"].textContent,
  subDirtyCls: elems["save-status-sub"].classList.contains("dirty-text"),
  dotSaved: elems["save-status-dot"].classList.contains("saved"),
  dotDirty: elems["save-status-dot"].classList.contains("dirty"),
};

console.log(JSON.stringify({ noneClean, loadedClean, loadedDirty }));
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(html_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"Node check failed: {res.stderr}")
        out = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(out["noneClean"]["title"], "Scenarios")
        self.assertIn("Choose or save", out["noneClean"]["sub"])
        self.assertTrue(out["noneClean"]["dotSaved"])
        self.assertFalse(out["noneClean"]["dotDirty"])

        self.assertEqual(out["loadedClean"]["title"], "Very Long Retirement Scenario Name For Ellipsis")
        self.assertIn("Saved", out["loadedClean"]["sub"])
        self.assertTrue(out["loadedClean"]["dotSaved"])
        self.assertFalse(out["loadedClean"]["dotDirty"])

        self.assertEqual(out["loadedDirty"]["title"], "Very Long Retirement Scenario Name For Ellipsis")
        self.assertIn("Modified", out["loadedDirty"]["sub"])
        self.assertTrue(out["loadedDirty"]["subDirtyCls"])
        self.assertFalse(out["loadedDirty"]["dotSaved"])
        self.assertTrue(out["loadedDirty"]["dotDirty"])

    def test_scenario_detail_holding_overrides_bar_chart(self):
        html_path = self.fixture_html_path
        import subprocess

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
    list: () => [...set],
  };
};
const detailPanel = { innerHTML: "", id: "models-detail-panel", style: {}, classList: mkClassList() };
const elems = {
  "models-detail-panel": detailPanel,
  "save-current-name": { textContent: "", title: "", style: {} },
  "save-status-sub": { textContent: "", title: "", style: {}, classList: mkClassList() },
  "save-status-dot": { title: "", style: {}, classList: mkClassList() },
  "nav-disk-btn": { title: "", style: {}, classList: mkClassList() },
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
const ctx = {
  document: doc, navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: () => 0,
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  URL, Promise, structuredClone, AbortController, TextEncoder, TextDecoder,
  addEventListener: () => {}, removeEventListener: () => {},
  location: { hash: "", href: "", search: "" }, matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  getComputedStyle: () => stub, devicePixelRatio: 1, innerWidth: 1200, innerHeight: 800,
  history: { replaceState: () => {} },
};
ctx.globalThis = ctx; ctx.window = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(script, ctx);

// Case 1: Test entry with negative and positive growth overrides and friendly names
const entry = {
  id: "002",
  name: "Custom Growth Test",
  file: "002_custom.json",
  settings: {
    simActiveModel: "single_path",
    simAnnualBurn: 400000,
    holdingForecasts: {
      "pos:acct1:FDXF": { growth_pct: -5.0 },
      "pos:acct2:GOOG": { growth_pct: 12.0 },
      "strat:broad_equity": { growth_pct: 7.5 },
    }
  }
};

vm.runInContext('globalPreferences = { friendly_names: { "pos:acct2:GOOG": { friendly_name: "Google Parent Stock" } } };', ctx);
ctx.renderModelDetailPanel(entry);
const renderedHtml = detailPanel.innerHTML;

// Case 2: Test empty overrides state
const emptyEntry = {
  id: "003",
  name: "Empty Forecasts",
  file: "003_empty.json",
  settings: {
    holdingForecasts: {}
  }
};
ctx.renderModelDetailPanel(emptyEntry);
const emptyHtml = detailPanel.innerHTML;

console.log(JSON.stringify({
  renderedHtml,
  emptyHtml,
}));
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(html_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"Node check failed: {res.stderr}")
        out = json.loads(res.stdout.strip().splitlines()[-1])
        rendered = out["renderedHtml"]
        empty = out["emptyHtml"]

        # Check box separation
        self.assertIn("Saved Simulation Parameters", rendered)
        self.assertIn("Per-Holding Annual Growth Overrides", rendered)
        # Ensure they are in distinct mdl-in-box containers
        self.assertIn("3 Overrides", rendered)

        # Check column header
        self.assertIn("Holding", rendered)
        self.assertIn("-5.0% (Min)", rendered)
        self.assertIn("+12.0% (Max)", rendered)
        self.assertIn("Growth Range", rendered)

        # Check holding names: friendly name when available, actual otherwise
        self.assertIn("Google Parent Stock", rendered)
        self.assertIn("FDXF", rendered)

        # Check bars: negative and positive values printed inside bar
        self.assertIn("-5.0%", rendered)
        self.assertIn("+12.0%", rendered)
        self.assertIn("+7.5%", rendered)
        # Check negative bar color/gradient
        self.assertIn("#f43f5e", rendered)
        # Check positive bar color/gradient
        self.assertIn("#10b981", rendered)

        # Check empty state message in dedicated box
        self.assertIn("No per-holding growth overrides configured in this Scenario", empty)
        self.assertIn("0 Overrides", empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
