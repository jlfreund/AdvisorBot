#!/usr/bin/env python3
"""
Pass-3 UI, forecast-fee, scheduled-event, and resilience regression tests (`DA4-03`, `DA4-07`, `DA4-09`–`DA4-12`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.support import env as env_support, pipeline as pipeline_support, roots


NODE_UI_REGRESSION_SCRIPT = r"""
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
const scriptSrc = scripts[scripts.length - 1][1];

const mkStub = () => new Proxy(function(){}, {
  get: (_t, p) => (p === Symbol.toPrimitive ? (() => 0) : (p === "length" ? 0 : stub)),
  apply: () => stub,
  set: () => true,
  has: () => true,
});
const stub = mkStub();

const els = new Map();
function makeClassList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach(x => x && set.add(x)),
    remove: (...c) => c.forEach(x => set.delete(x)),
    contains: c => set.has(c),
    toggle: (c, force) => {
      const next = typeof force === "boolean" ? force : !set.has(c);
      if (next) set.add(c); else set.delete(c);
      return next;
    },
  };
}

let activeElementRef = null;
function getEl(id) {
  if (!els.has(id)) {
    const attrs = new Map();
    const cl = makeClassList();
    const styleObj = { display: "" };
    const state = {
      id,
      tagName: id.endsWith("-input") ? "INPUT" : "DIV",
      value: "",
      textContent: "",
      innerHTML: "",
      checked: false,
      disabled: false,
      hidden: false,
      title: "",
      classList: cl,
      style: styleObj,
      dataset: {},
      getAttribute: k => (attrs.has(k) ? attrs.get(k) : null),
      setAttribute: (k, v) => attrs.set(k, String(v)),
      focus: () => { activeElementRef = proxy; },
      closest: () => null,
      querySelector: sel => {
        if (sel === "[data-advisor-apply]" && state.innerHTML.includes("data-advisor-apply")) {
          return getEl("__advisor_apply_btn__");
        }
        if (sel === "[data-advisor-elapsed]" && state.innerHTML.includes("data-advisor-elapsed")) {
          return getEl("__advisor_elapsed_span__");
        }
        return null;
      },
      querySelectorAll: () => [],
      replaceChildren: () => { state.innerHTML = ""; },
      appendChild: () => {},
      addEventListener: () => {},
    };
    const proxy = new Proxy(function(){}, {
      get: (_t, p) => (p === Symbol.toPrimitive ? (() => 0) : (p in state ? state[p] : stub)),
      set: (_t, p, v) => { state[p] = v; return true; },
      apply: () => stub,
    });
    els.set(id, proxy);
  }
  return els.get(id);
}

const keyListeners = [];
const docObj = {
  get activeElement() { return activeElementRef; },
  getElementById: id => getEl(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: tag => getEl("__dyn_" + tag + "_" + Math.random()),
  addEventListener: (ev, fn) => { if (ev === "keydown") keyListeners.push(fn); },
  body: getEl("__body__"),
};

const winObj = {
  document: docObj,
  location: { protocol: "http:", hash: "", reload: () => { winObj._reloaded = true; } },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, models: [], saves: [] }) }),
  setTimeout: () => 1,
  clearTimeout: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
  requestAnimationFrame: fn => { fn(); return 1; },
  addEventListener: () => {},
  confirm: () => true,
  alert: () => {},
  performance: { now: () => 0 },
  CSS: { escape: s => String(s) },
};
winObj.window = winObj;
winObj.globalThis = winObj;

const ctx = vm.createContext(Object.assign(winObj, {
  Math, Date, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Symbol, Proxy, Promise,
  parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent, console,
}));

vm.runInContext(scriptSrc, ctx);

const results = await vm.runInContext(`(async () => {
  const out = {};

  // 1. DA4-10a: refreshActiveSiteState must NOT throw ReferenceError (no fetchSavesList)
  let refreshErr = null;
  try {
    window.refreshActiveSiteState();
  } catch (e) {
    refreshErr = String(e);
  }
  out.refreshErr = refreshErr;

  // 2. DA4-07: timelineGrowthFactor deducts holdingFeePct (fc.expense_ratio_pct)
  const sampleAsset = BASE_SIM_ASSETS.find(a => a && a.default_risk_class === "broad_equity") || BASE_SIM_ASSETS[0];
  const fk = sampleAsset.forecast_key || sampleAsset.id;
  holdingFeePct[fk] = 0;
  const multNoFee = timelineGrowthFactor(sampleAsset, 5);
  holdingFeePct[fk] = 1.25;
  const multWithFee = timelineGrowthFactor(sampleAsset, 5);
  delete holdingFeePct[fk];
  out.feeReducesGrowth = multWithFee < multNoFee;

  // 3. DA4-09: holdingHalfLives keys sorted in snapshot & _mergeSettingsPatch merges per-key
  holdingHalfLives = { z_key: 12, a_key: 8 };
  const snapKeys = Object.keys(getCurrentSettingsSnapshot().holdingHalfLives);
  out.sortedHalfLifeKeys = snapKeys;
  const mergedPatch = _mergeSettingsPatch(
    { holdingForecasts: { k1: { growth_pct: 7, risk_class: "broad_equity" } }, holdingHalfLives: { k1: 10 } },
    { holdingForecasts: { k2: { growth_pct: 9, risk_class: "broad_equity" } }, holdingHalfLives: { k2: 15 } }
  );
  out.mergedForecastKeys = Object.keys(mergedPatch.holdingForecasts).sort();
  out.mergedHalfLifeKeys = Object.keys(mergedPatch.holdingHalfLives).sort();
  holdingHalfLives = {};

  // 4. DA4-10b & c: control_ui preserves loadedSettingsSnapshot baseline & sets dirty without green "Saved" stickiness; load_save respects isSaveDirty
  loadedSettingsSnapshot = getCurrentSettingsSnapshot();
  isSaveDirty = false;
  await applyLiveUiEvent({ type: "control_ui", settings_patch: { simAnnualBurn: Number(loadedSettingsSnapshot.simAnnualBurn) + 5555 } });
  const dirtyAfterControl = isSaveDirty;
  checkSaveDirtyState();
  const stillDirtyAfterCheck = isSaveDirty;
  const saveSubText = document.getElementById("save-status-sub").textContent || "";
  out.dirtyAfterControl = dirtyAfterControl;
  out.stillDirtyAfterCheck = stillDirtyAfterCheck;
  out.notStuckOnSaved = !saveSubText.includes("Saved \u2713");

  const burnWhileDirty = simAnnualBurn;
  modelsList = [{ id: "999", name: "Other", settings: Object.assign({}, loadedSettingsSnapshot, { simAnnualBurn: 11111 }) }];
  await applyLiveUiEvent({ type: "load_save", id: "999" });
  out.dirtyPreservedAcrossLoadSave = (simAnnualBurn === burnWhileDirty && isSaveDirty === true);

  // 5. DA4-03 / D9: Expired past one-time event shows "Past · Skipped" badge in table
  simScheduledActions = [{ id: "past1", type: "withdrawal", cadence: "one_time", ym: "2022-04", amount_mode: "usd", amount: 5000, dollar_mode: "nominal", from_asset_id: "auto", comment: "Old event" }];
  renderSimWithdrawalsTable();
  const swBodyHtml = document.getElementById("sim-sw-tbody").innerHTML || "";
  out.pastSkippedBadge = swBodyHtml.includes("Past") && swBodyHtml.includes("Skipped") && swBodyHtml.includes("2022");

  // 6. DA4-11: "ai_advisor" author renders AI badge on scenario card & mid-apply keeps button disabled
  modelsList = [
    { id: "001", name: "Human Plan", author: { type: "human" }, settings: loadedSettingsSnapshot, kpi_summary: {} },
    { id: "002", name: "Advisor Plan", author: { type: "ai_advisor", model: "claude-sonnet-4-5" }, settings: loadedSettingsSnapshot, kpi_summary: {} },
  ];
  renderModelsScreen();
  const cardsHtml = document.getElementById("models-cards-container").innerHTML || "";
  out.aiAdvisorBadgeRendered = cardsHtml.includes("AI") && cardsHtml.includes("claude-sonnet-4-5") && !cardsHtml.includes("Human</span>");

  _applyingAdvisorJobIds.add("job_test_1");
  updateAdvisorJobBanner({ job_id: "job_test_1", status: "completed", title: "Review", proposal_label: "Save scenario", elapsed_sec: 2.0 });
  const applyBtn = document.getElementById("__advisor_apply_btn__");
  out.applyDisabledWhileInFlight = Boolean(applyBtn.disabled);
  _applyingAdvisorJobIds.delete("job_test_1");

  return out;
})()`, ctx);

console.log(JSON.stringify(results));
"""


@pytest.mark.render
@pytest.mark.portfolio("p2_equity_comp_rsu")
@pytest.mark.regression("DA4-03")
@pytest.mark.regression("DA4-07")
@pytest.mark.regression("DA4-09")
@pytest.mark.regression("DA4-10")
@pytest.mark.regression("DA4-11")
def test_pass3_ui_regressions_in_rendered_bundle(tmp_path: Path):
    """Pass-3 UI regressions: reconnect retry, fee growth deduction, past-event badge, dirty-state protection, and AI author badge."""
    root = tmp_path / "root"
    roots.assemble_root(root, ["p2_equity_comp_rsu"])
    with env_support.pointed_at(root):
        pipeline_support.clear_payload_cache()
        html_path, _ = pipeline_support.render_fixture("p2_equity_comp_rsu")
        pipeline_support.clear_payload_cache()

    harness = tmp_path / "ui_reg.mjs"
    harness.write_text(NODE_UI_REGRESSION_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(html_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node UI harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert res["refreshErr"] is None
    assert res["feeReducesGrowth"] is True
    assert res["sortedHalfLifeKeys"] == ["a_key", "z_key"]
    assert res["mergedForecastKeys"] == ["k1", "k2"]
    assert res["mergedHalfLifeKeys"] == ["k1", "k2"]
    assert res["dirtyAfterControl"] is True
    assert res["stillDirtyAfterCheck"] is True
    assert res["notStuckOnSaved"] is True
    assert res["dirtyPreservedAcrossLoadSave"] is True
    assert res["pastSkippedBadge"] is True
    assert res["aiAdvisorBadgeRendered"] is True
    assert res["applyDisabledWhileInFlight"] is True
