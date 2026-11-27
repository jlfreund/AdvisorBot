#!/usr/bin/env python3
"""
Tests for multi-Portfolio / Project management:
  1. Legacy `Project/` -> `Portfolios/<slug>/` + `Data/Config/LLM/` + `Data/.session_token` migration.
  2. Portfolio CRUD (`create_portfolio`, `list_portfolios`, `set_active_portfolio`, `delete_portfolio`)
     and HTTP endpoints (`/api/portfolios`, `/api/portfolios/select`, `/api/portfolios/delete` requiring `confirm: "yes"`).
  3. Statement refresh isolation between two portfolios (`Alpha Portfolio` vs `Beta Portfolio`).
  4. No-portfolio empty state rendering and exact JS/UI contracts (`Portfolio Settings` button, `Portfolio Settings` title,
     `Portfolio` tab, `Close` / `Select Portfolio` visibility across tabs, typed `yes` delete confirmation).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT / "Scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
import holdings  # noqa: E402
import llm_tools  # noqa: E402
import advisorbot_server  # noqa: E402
import settings  # noqa: E402
from db.database import PortfolioDB  # noqa: E402
from exporters import html_report  # noqa: E402
from exporters.html_report import render_html_report  # noqa: E402
from exporters.view_model import build_report_payload  # noqa: E402


class _IsolatedPortfolioEnv:
    """Temporarily points PORTFOLIOS_DIR, HOLDINGS_DATA_DIR, LLM_DIR, and OUTPUT_HTML_PATH at a temp directory."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "Data"
        self.portfolios_dir = self.root / "Portfolios"
        self.llm_dir = self.data_dir / "Config" / "LLM"
        self.out_html = self.root / "index.html"
        self._saved_env: dict[str, str | None] = {}
        self._patches: list = []

    def __enter__(self) -> "_IsolatedPortfolioEnv":
        for key, val in {
            "HOLDINGS_DATA_DIR": str(self.data_dir),
            "PORTFOLIOS_DIR": str(self.portfolios_dir),
            "LLM_DIR": str(self.llm_dir),
            "PROJECT_DIR": "",
            "PORTFOLIO_DIR": "",
        }.items():
            self._saved_env[key] = os.environ.get(key)
            if val:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.portfolios_dir.mkdir(parents=True, exist_ok=True)
        self.llm_dir.mkdir(parents=True, exist_ok=True)

        self._patches = [
            patch.object(advisorbot_server, "DEFAULT_HTML_REPORT_PATH", self.out_html),
            patch.object(holdings, "DEFAULT_HTML_REPORT_PATH", self.out_html),
        ]
        for p in self._patches:
            p.start()
        config.refresh_active_paths()
        settings.invalidate_settings_cache()
        llm_tools.invalidate_payload_cache()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for p in reversed(self._patches):
            p.stop()
        for key, prev in self._saved_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        config.refresh_active_paths()
        settings.invalidate_settings_cache()
        llm_tools.invalidate_payload_cache()
        self._tmp.cleanup()


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str = "",
) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-Session-Token": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        err.close()
        return err.code, json.loads(raw)


def _complete_test_portfolio(pid: str, *, activate: bool = False) -> dict:
    """Populates required DP-04 simulation fields on a test draft portfolio and optionally activates it."""
    cfg = config.load_config(portfolio_id=pid)
    if not cfg.get("household_name"):
        cfg["household_name"] = pid.replace("_", " ").title()
    if not cfg.get("reference_annual_burn"):
        cfg["reference_annual_burn"] = 80000
        cfg["retirement_annual_burn"] = 80000
    if not cfg.get("investors"):
        cfg["investors"] = [
            {
                "id": "inv_1",
                "name": "Pat Investor",
                "birth_date": "1978-06-15",
                "milestone_age": 85,
                "is_rmd_owner": True,
                "ssn": {"fra_age": 67, "default_claim_age": 67, "external_file": "", "annual_earnings": {}},
            }
        ]
    if not cfg.get("tax_rates"):
        cfg["tax_rates"] = {
            "state_code": "WA",
            "state_rate": 0.0,
            "ltcg_top": 0.238,
            "ordinary_top": 0.408,
            "ssn_effective": 0.3145,
        }
    config.save_config(cfg, portfolio_id=pid)
    if activate:
        config.set_active_portfolio(pid)
    return cfg


class PortfoliosTest(unittest.TestCase):
    def test_legacy_project_migration_to_portfolios_and_data_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            legacy_dir = tmp_root / "Project"
            (legacy_dir / "Statements").mkdir(parents=True)
            (legacy_dir / "Saves").mkdir(parents=True)
            (legacy_dir / "Assets").mkdir(parents=True)
            (legacy_dir / "LLM").mkdir(parents=True)

            (legacy_dir / "Assets" / "acme_badge.png").write_bytes(b"PNGDATA")
            (legacy_dir / "Statements" / "broker.txt").write_text("sample", encoding="utf-8")
            (legacy_dir / "Saves" / "001_default.json").write_text(json.dumps({"name": "Default", "settings": {}}), encoding="utf-8")
            (legacy_dir / "LLM" / "gemini.json").write_text(json.dumps({"provider": "gemini", "api_key": "test"}), encoding="utf-8")
            (legacy_dir / ".session_token").write_text("tok_legacy_12345", encoding="utf-8")
            with PortfolioDB(legacy_dir / "portfolio.sqlite"):
                pass

            legacy_cfg = config.default_config()
            legacy_cfg["household_name"] = "Acme Family"
            legacy_cfg["badge_image"] = "Project/Assets/acme_badge.png"
            legacy_cfg["reference_annual_burn"] = 75000
            legacy_cfg["retirement_annual_burn"] = 75000
            legacy_cfg["tax_rates"] = {"state_code": "WA", "state_rate": 0.0, "ltcg_top": 0.238, "ordinary_top": 0.408}
            legacy_cfg["investors"] = [
                {
                    "id": "inv_1",
                    "name": "Alex Acme",
                    "birth_date": "1976-03-10",
                    "milestone_age": 85,
                    "is_rmd_owner": True,
                    "ssn": {"external_file": "Project/Assets/ssa.xml"},
                }
            ]
            (legacy_dir / "project.json").write_text(json.dumps(legacy_cfg, indent=2), encoding="utf-8")

            portfolios_dir = tmp_root / "Portfolios"
            llm_dir = tmp_root / "Data" / "Config" / "LLM"
            data_dir = tmp_root / "Data"

            slug = config.migrate_legacy_project_dir(
                legacy_dir=legacy_dir,
                portfolios_dir=portfolios_dir,
                llm_dir=llm_dir,
                data_dir=data_dir,
            )

            self.assertEqual(slug, "acme_family")
            self.assertFalse(legacy_dir.exists())
            self.assertTrue((portfolios_dir / "acme_family" / "project.json").is_file())
            self.assertTrue((portfolios_dir / "acme_family" / "portfolio.sqlite").is_file())
            self.assertTrue((portfolios_dir / "acme_family" / "Statements" / "broker.txt").is_file())
            self.assertTrue((portfolios_dir / "acme_family" / "Saves" / "001_default.json").is_file())
            self.assertTrue((llm_dir / "gemini.json").is_file())
            self.assertEqual((data_dir / ".session_token").read_text(encoding="utf-8"), "tok_legacy_12345")

            active_doc = json.loads((portfolios_dir / "_active.json").read_text(encoding="utf-8"))
            self.assertEqual(active_doc["active_portfolio_id"], "acme_family")

            migrated_cfg = json.loads((portfolios_dir / "acme_family" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(migrated_cfg["badge_image"], "Portfolios/acme_family/Assets/acme_badge.png")
            self.assertEqual(migrated_cfg["investors"][0]["ssn"]["external_file"], "Portfolios/acme_family/Assets/ssa.xml")

    def test_portfolio_crud_and_http_routes_with_delete_confirmation(self):
        with _IsolatedPortfolioEnv() as env:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), advisorbot_server.SavesRequestHandler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            tok = advisorbot_server.SESSION_TOKEN

            try:
                # Initially empty
                st, body = _http_json(f"{base}/api/portfolios", token=tok)
                self.assertEqual(st, 200)
                self.assertIsNone(body["active_portfolio_id"])
                self.assertEqual(body["portfolios"], [])

                # Create Alpha Portfolio: per DP-04, starts out incomplete (saved & listed, NOT activated)
                st, c1 = _http_json(
                    f"{base}/api/portfolios",
                    method="POST",
                    payload={"name": "Alpha Portfolio", "activate": True, "rebuild": False},
                    token=tok,
                )
                self.assertEqual(st, 200)
                self.assertTrue(c1["ok"])
                self.assertEqual(c1["portfolio_id"], "alpha_portfolio")
                self.assertFalse(c1.get("is_complete", True))
                self.assertIsNone(c1["active_portfolio_id"])

                # Complete Alpha Portfolio required fields and activate it
                _complete_test_portfolio("alpha_portfolio", activate=True)
                self.assertEqual(config.get_active_portfolio_id(), "alpha_portfolio")

                # Create Beta Portfolio without switching active
                st, c2 = _http_json(
                    f"{base}/api/portfolios",
                    method="POST",
                    payload={"name": "Beta Portfolio", "activate": False, "rebuild": False},
                    token=tok,
                )
                self.assertEqual(st, 200)
                self.assertEqual(c2["portfolio_id"], "beta_portfolio")
                self.assertEqual(c2["active_portfolio_id"], "alpha_portfolio")

                # Attempting to select incomplete Beta Portfolio MUST be rejected (DP-04)
                st_inc, sel_inc = _http_json(
                    f"{base}/api/portfolios/select",
                    method="POST",
                    payload={"portfolio_id": "beta_portfolio"},
                    token=tok,
                )
                self.assertIn(st_inc, (400, 422))
                self.assertFalse(sel_inc["ok"])
                self.assertEqual(config.get_active_portfolio_id(), "alpha_portfolio")

                # Auto-save edit to Beta Portfolio (non-active) completing required fields with rebuild=False
                beta_cfg = config.load_config(portfolio_id="beta_portfolio")
                beta_cfg["household_name"] = "Beta Family Edited"
                beta_cfg["reference_annual_burn"] = 90000
                beta_cfg["investors"] = [
                    {"id": "inv_b", "name": "Blair Beta", "birth_date": "1980-04-12", "milestone_age": 85, "is_rmd_owner": True}
                ]
                beta_cfg["tax_rates"] = {"state_code": "TX", "state_rate": 0.0, "ltcg_top": 0.238, "ordinary_top": 0.408}
                beta_cfg["portfolio_id"] = "beta_portfolio"
                beta_cfg["rebuild"] = False
                st, upd = _http_json(f"{base}/api/project", method="POST", payload=beta_cfg, token=tok)
                self.assertEqual(st, 200)
                self.assertTrue(upd["ok"])
                self.assertFalse(upd.get("rebuilt", False))
                self.assertEqual(config.load_config(portfolio_id="beta_portfolio")["household_name"], "Beta Family Edited")
                self.assertEqual(config.get_active_portfolio_id(), "alpha_portfolio")

                # Switch active portfolio to Beta -> triggers render_active_site to env.out_html
                st, sel = _http_json(
                    f"{base}/api/portfolios/select",
                    method="POST",
                    payload={"portfolio_id": "beta_portfolio"},
                    token=tok,
                )
                self.assertEqual(st, 200)
                self.assertTrue(sel["ok"])
                self.assertEqual(sel["active_portfolio_id"], "beta_portfolio")
                self.assertTrue(sel.get("rebuilt"))
                self.assertTrue(env.out_html.is_file())
                self.assertIn("Beta Family Edited", env.out_html.read_text(encoding="utf-8"))

                # Deleting without typing "yes" MUST be rejected
                st, bad_del = _http_json(
                    f"{base}/api/portfolios/delete",
                    method="POST",
                    payload={"portfolio_id": "beta_portfolio", "confirm": "no"},
                    token=tok,
                )
                self.assertEqual(st, 400)
                self.assertFalse(bad_del["ok"])
                self.assertTrue((env.portfolios_dir / "beta_portfolio").is_dir())

                # Deleting active portfolio with confirm="yes" permanently removes folder & clears active cleanly
                st, good_del = _http_json(
                    f"{base}/api/portfolios/delete",
                    method="POST",
                    payload={"portfolio_id": "beta_portfolio", "confirm": "yes"},
                    token=tok,
                )
                self.assertEqual(st, 200)
                self.assertTrue(good_del["ok"])
                self.assertFalse((env.portfolios_dir / "beta_portfolio").exists())
                self.assertIsNone(config.get_active_portfolio_id())
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_statement_refresh_isolation_between_portfolios(self):
        with _IsolatedPortfolioEnv() as env:
            p1 = config.create_portfolio("Alpha Portfolio", activate=False)["portfolio_id"]
            p2 = config.create_portfolio("Beta Portfolio", activate=False)["portfolio_id"]
            _complete_test_portfolio(p1, activate=True)
            _complete_test_portfolio(p2, activate=False)

            (env.portfolios_dir / p1 / "Statements" / "alpha.grants.json").write_text(
                json.dumps(
                    {
                        "institution": "BrokerAlpha",
                        "account_id": "ALPHA_SP",
                        "account_name": "Alpha Stock Plan",
                        "as_of_date": "2026-10-01",
                        "symbol": "ACME",
                        "company_name": "Acme Corp",
                        "unvested_tranches": [
                            {
                                "grant_id": "G-A1",
                                "grant_date": "2025-03-15",
                                "vest_date": "2027-03-15",
                                "orig_qty": 100.0,
                                "total_qty": 100.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (env.portfolios_dir / p2 / "Statements" / "beta.grants.json").write_text(
                json.dumps(
                    {
                        "institution": "BrokerBeta",
                        "account_id": "BETA_SP",
                        "account_name": "Beta Stock Plan",
                        "as_of_date": "2026-10-01",
                        "symbol": "BETA",
                        "company_name": "Beta Inc",
                        "unvested_tranches": [
                            {
                                "grant_id": "G-B1",
                                "grant_date": "2025-04-20",
                                "vest_date": "2027-04-20",
                                "orig_qty": 250.0,
                                "total_qty": 250.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            parser = holdings.build_parser()
            with patch(
                "valuation.market_data._fetch_yahoo_chart",
                side_effect=lambda sym, *a, **kw: (sym, [("2026-10-16", 55.0, 55.0)], 55.0),
            ):
                # Refresh Alpha
                config.set_active_portfolio(p1)
                rc1 = holdings.cmd_refresh(parser.parse_args(["refresh", "--output", str(env.out_html), "--today", "2026-10-15"]))
                self.assertEqual(rc1, 0)

                # Refresh Beta
                config.set_active_portfolio(p2)
                rc2 = holdings.cmd_refresh(parser.parse_args(["refresh", "--output", str(env.out_html), "--today", "2026-10-15"]))
                self.assertEqual(rc2, 0)

            c1 = sqlite3.connect(env.portfolios_dir / p1 / "portfolio.sqlite")
            try:
                syms1 = {r[0] for r in c1.execute("SELECT DISTINCT symbol FROM gsu_tranches").fetchall()}
            finally:
                c1.close()
            c2 = sqlite3.connect(env.portfolios_dir / p2 / "portfolio.sqlite")
            try:
                syms2 = {r[0] for r in c2.execute("SELECT DISTINCT symbol FROM gsu_tranches").fetchall()}
            finally:
                c2.close()

            self.assertIn("ACME", syms1)
            self.assertNotIn("BETA", syms1)
            self.assertIn("BETA", syms2)
            self.assertNotIn("ACME", syms2)

    def test_empty_state_render_and_ui_modal_js_contracts(self):
        with _IsolatedPortfolioEnv() as env:
            # 0 portfolios active -> build empty report & render HTML
            config.set_active_portfolio(None)
            with PortfolioDB(":memory:") as db:
                payload = build_report_payload(db, config.default_config(), date(2026, 10, 15))
            self.assertFalse(payload["has_portfolio"])
            self.assertIsNone(payload["active_portfolio_id"])

            render_html_report(payload, env.out_html)
            html_empty = env.out_html.read_text(encoding="utf-8")

            # Also render an active portfolio HTML to verify active nav & modal title behavior
            p1 = config.create_portfolio("Alpha Portfolio", activate=False)["portfolio_id"]
            _complete_test_portfolio(p1, activate=True)
            with PortfolioDB(env.portfolios_dir / p1 / "portfolio.sqlite") as db:
                payload_active = build_report_payload(db, config.load_config(portfolio_id=p1), date(2026, 10, 15))
            self.assertTrue(payload_active["has_portfolio"])
            self.assertEqual(payload_active["active_portfolio_id"], p1)
            out_active = env.root / "active.html"
            render_html_report(payload_active, out_active)

            # Execute generated UI script inside Node VM to verify exact DOM/button/modal contracts
            node_script = """
import fs from "node:fs";
import vm from "node:vm";

function runCheck(htmlPath) {
  const html = fs.readFileSync(htmlPath, "utf8");
  const scripts = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
  const code = scripts[scripts.length - 1][1];

  const elems = new Map();
  function makeEl(id) {
    if (elems.has(id)) return elems.get(id);
    const classes = new Set();
    const el = {
      id,
      textContent: "",
      innerHTML: "",
      value: "",
      disabled: false,
      style: {},
      dataset: {},
      classList: {
        add: (...cs) => cs.forEach(c => classes.add(c)),
        remove: (...cs) => cs.forEach(c => classes.delete(c)),
        toggle: (c, force) => { if (force === undefined ? !classes.has(c) : force) classes.add(c); else classes.delete(c); },
        contains: (c) => classes.has(c),
      },
      addEventListener: () => {},
      removeEventListener: () => {},
      setAttribute: () => {},
      removeAttribute: () => {},
      getAttribute: () => null,
      appendChild: () => {},
      closest: () => null,
      focus: () => {},
      scrollIntoView: () => {},
      querySelector: () => makeEl(id + "__q"),
      querySelectorAll: () => [],
    };
    elems.set(id, el);
    return el;
  }

  const doc = {
    getElementById: (id) => makeEl(id),
    querySelector: (sel = "") => makeEl("__doc_q"),
    querySelectorAll: (sel = "") => (typeof sel === "string" && sel.includes("badge-style-chip"))
      ? ["gilded","crest","fintech","prism","swiss","nordic","artdeco","flora","quantum","monogram"].map(k => makeEl("chip-badge-" + k))
      : [],
    createElement: () => makeEl("__created"),
    createElementNS: () => makeEl("__created_ns"),
    addEventListener: () => {},
    body: makeEl("__body"),
    documentElement: makeEl("__root"),
    title: "",
  };
  const win = {
    addEventListener: () => {},
    location: { hash: "", href: "http://127.0.0.1:8765/", search: "", reload: () => {} },
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    devicePixelRatio: 1,
    getComputedStyle: () => makeEl("__style"),
  };
  const ctxObj = {
    document: doc, window: win, navigator: { userAgent: "node" }, console,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    requestAnimationFrame: (f) => { f(); return 0; },
    fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
    Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  };
  ctxObj.globalThis = ctxObj;
  ctxObj.window = ctxObj;
  ctxObj.self = ctxObj;
  ctxObj.addEventListener = () => {};
  ctxObj.location = win.location;
  ctxObj.history = { replaceState: () => {} };
  ctxObj.URL = URL; ctxObj.Promise = Promise; ctxObj.structuredClone = structuredClone;
  ctxObj.AbortController = AbortController; ctxObj.TextEncoder = TextEncoder; ctxObj.TextDecoder = TextDecoder;

  const ctx = vm.createContext(ctxObj);
  vm.runInContext(code, ctx);
  vm.runInContext("initHeaderAndKPIs(); updateProjectSettingsHeaderUI();", ctx);

  const navLabel = makeEl("nav-settings-lbl").textContent;
  const navSub = makeEl("nav-settings-sub").textContent;
  const dlgTitle = makeEl("proj-dlg-title-text").textContent;

  vm.runInContext("switchProjectSettingsTab('info');", ctx);
  const saveDispOnInfo = makeEl("proj-save-btn").style.display;
  const saveDisabledOnInfo = Boolean(makeEl("proj-save-btn").disabled);
  vm.runInContext("switchProjectSettingsTab('investors');", ctx);
  const saveDispOnInvestors = makeEl("proj-save-btn").style.display;

  // Verify typed "yes" delete confirmation guard
  vm.runInContext("openPortfolioDeleteConfirm('alpha_portfolio');", ctx);
  const delDisabledInitially = makeEl("portfolio-delete-confirm-btn").disabled;
  makeEl("portfolio-delete-confirm-input").value = "nope";
  vm.runInContext("onPortfolioDeleteInput();", ctx);
  const delDisabledOnNope = makeEl("portfolio-delete-confirm-btn").disabled;
  makeEl("portfolio-delete-confirm-input").value = "yes";
  vm.runInContext("onPortfolioDeleteInput();", ctx);
  const delEnabledOnYes = !makeEl("portfolio-delete-confirm-btn").disabled;

  // Verify Select Portfolio button enables when 2+ portfolios exist in list
  vm.runInContext("switchProjectSettingsTab('info'); _portfoliosBundle.portfolios = [{id:'p1',name:'P1'},{id:'p2',name:'P2'}]; _selectedPortfolioId = 'p2'; renderPortfoliosTab();", ctx);
  const saveEnabledWithMultiple = !makeEl("proj-save-btn").disabled;

  // Verify Badge slider and prompt
  vm.runInContext("openProjectSettingsDialog();", ctx);
  const badgeSlider = makeEl("proj-badge-slider");
  const badgeSliderMin = badgeSlider.min;
  const badgeSliderMax = badgeSlider.max;
  const badgePromptSlot1 = makeEl("proj-badge-prompt").value || "";
  vm.runInContext("applyBadgeStylePreset('gilded');", ctx);
  const badgePromptAfterPreset = makeEl("proj-badge-prompt").value || "";

  // Test Badge Rotation: slot 1 is Default, slots 2-10 are moving window of last 9
  vm.runInContext(`
    _projDraft.badge_history = [
      "Assets/default_badge.svg",
      "b1.svg", "b2.svg", "b3.svg", "b4.svg", "b5.svg", "b6.svg", "b7.svg", "b8.svg", "b9.svg"
    ];
    const rotBefore = getBadgeRotation().slice();
    // Push an 11th total (10th custom) badge
    _projDraft.badge_history.push("b10.svg");
    const rotAfter = getBadgeRotation();
    syncBadgeSliderUI();
    onBadgeSliderChange("10");
    const activeBadgeAfterSlider = _projDraft.badge_image;
    const sliderMaxAfter = document.getElementById("proj-badge-slider").max;

    _projDraft.badge_metadata = {
      "b10.svg": { prompt: "Custom quantum prompt", preset: "quantum", edited_by_hand: false },
      "b9.svg": { prompt: "Hand typed prompt", preset: "", edited_by_hand: true }
    };
    // Re-trigger slot 10 with metadata
    onBadgeSliderChange("10");
    const slot10Prompt = document.getElementById("proj-badge-prompt").value || "";
    const slot10QuantumActive = document.getElementById("chip-badge-quantum").classList.contains("active");

    // User edits prompt by hand -> deselects presets
    document.getElementById("proj-badge-prompt").value = "Modified prompt";
    onBadgePromptInput(document.getElementById("proj-badge-prompt"));
    const afterEditQuantumActive = document.getElementById("chip-badge-quantum").classList.contains("active");

    // Scroll to b9.svg (slot 9, hand edited) -> prompt empty, all presets deselected
    onBadgeSliderChange("9");
    const slot9Prompt = document.getElementById("proj-badge-prompt").value || "";
    const slot9AnyActive = Array.from(document.querySelectorAll(".badge-style-chip")).some(c => c.classList.contains("active"));

    // Scroll to slot 1 (default badge from disk) -> prompt empty, all presets deselected
    onBadgeSliderChange("1");
    const slot1PromptAfterScroll = document.getElementById("proj-badge-prompt").value || "";
    const slot1AnyActive = Array.from(document.querySelectorAll(".badge-style-chip")).some(c => c.classList.contains("active"));

    // Verify Annual Burn Rate slider & text input override
    const defaultBurnDraft = _buildDefaultDraftForPortfolio("Burn Test").reference_annual_burn;
    onProjectBurnSliderInput("75000");
    const draftAfterSlider = _projDraft.reference_annual_burn;
    const inputValAfterSlider = document.getElementById("proj-burn-input").value;
    document.getElementById("proj-burn-input").value = "2000000";
    onProjectBurnTextInput("2000000");
    const draftAfterType = _projDraft.reference_annual_burn;
    const sliderAfterType = document.getElementById("proj-burn-slider").value;
    onProjectBurnTextBlur(document.getElementById("proj-burn-input"));
    const inputValAfterBlur = document.getElementById("proj-burn-input").value;
  `, ctx);
  const rotBeforeLen = vm.runInContext("rotBefore.length", ctx);
  const rotAfterLen = vm.runInContext("rotAfter.length", ctx);
  const rotAfterFirst = vm.runInContext("rotAfter[0]", ctx);
  const rotAfterSecond = vm.runInContext("rotAfter[1]", ctx);
  const rotAfterLast = vm.runInContext("rotAfter[rotAfter.length - 1]", ctx);
  const sliderMaxAfter = vm.runInContext("sliderMaxAfter", ctx);
  const activeBadgeAfterSlider = vm.runInContext("activeBadgeAfterSlider", ctx);

  const slot10Prompt = vm.runInContext("slot10Prompt", ctx);
  const slot10QuantumActive = vm.runInContext("slot10QuantumActive", ctx);
  const afterEditQuantumActive = vm.runInContext("afterEditQuantumActive", ctx);
  const slot9Prompt = vm.runInContext("slot9Prompt", ctx);
  const slot9AnyActive = vm.runInContext("slot9AnyActive", ctx);
  const slot1PromptAfterScroll = vm.runInContext("slot1PromptAfterScroll", ctx);
  const slot1AnyActive = vm.runInContext("slot1AnyActive", ctx);

  const defaultBurnDraft = vm.runInContext("defaultBurnDraft", ctx);
  const draftAfterSlider = vm.runInContext("draftAfterSlider", ctx);
  const inputValAfterSlider = vm.runInContext("inputValAfterSlider", ctx);
  const draftAfterType = vm.runInContext("draftAfterType", ctx);
  const sliderAfterType = vm.runInContext("sliderAfterType", ctx);
  const inputValAfterBlur = vm.runInContext("inputValAfterBlur", ctx);

  return {
    navLabel,
    navSub,
    dlgTitle,
    saveDispOnInfo,
    saveDisabledOnInfo,
    saveEnabledWithMultiple,
    saveDispOnInvestors,
    delDisabledInitially,
    delDisabledOnNope,
    delEnabledOnYes,
    badgeSliderMin,
    badgeSliderMax,
    badgePromptSlot1,
    badgePromptAfterPreset,
    slot10Prompt,
    slot10QuantumActive,
    afterEditQuantumActive,
    slot9Prompt,
    slot9AnyActive,
    slot1PromptAfterScroll,
    slot1AnyActive,
    rotBeforeLen,
    rotAfterLen,
    rotAfterFirst,
    rotAfterSecond,
    rotAfterLast,
    sliderMaxAfter,
    activeBadgeAfterSlider,
    defaultBurnDraft,
    draftAfterSlider,
    inputValAfterSlider,
    draftAfterType,
    sliderAfterType,
    inputValAfterBlur,
  };
}

const emptyRes = runCheck(process.argv[1]);
const activeRes = runCheck(process.argv[2]);
console.log(JSON.stringify({ emptyRes, activeRes }));
"""
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", node_script, str(env.out_html), str(out_active)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node check failed:\n{proc.stderr}")
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])

            empty_res = parsed["emptyRes"]
            self.assertEqual(empty_res["navLabel"], "Portfolio Settings")
            self.assertEqual(empty_res["navSub"], "Select or create portfolio")
            self.assertEqual(empty_res["dlgTitle"], "Portfolio Settings")
            self.assertNotEqual(empty_res["saveDispOnInfo"], "none")
            self.assertTrue(empty_res["saveDisabledOnInfo"])
            self.assertTrue(empty_res["saveEnabledWithMultiple"])
            self.assertEqual(empty_res["saveDispOnInvestors"], "none")
            self.assertEqual(empty_res["badgeSliderMin"], "1")
            self.assertEqual(empty_res["badgePromptSlot1"], "")
            self.assertIn("gilded", empty_res["badgePromptAfterPreset"].lower())

            default_titles = [title for _, title in config.DEFAULT_SAVED_PROMPT_SPECS]
            active_res = parsed["activeRes"]
            self.assertEqual(active_res["navLabel"], "Alpha Portfolio")
            self.assertEqual(active_res["navSub"], "Portfolio Settings")
            self.assertEqual(active_res["dlgTitle"], "Alpha Portfolio")
            self.assertTrue(active_res["saveDisabledOnInfo"])
            self.assertTrue(active_res["saveEnabledWithMultiple"])
            self.assertTrue(active_res["delDisabledInitially"])
            self.assertTrue(active_res["delDisabledOnNope"])
            self.assertTrue(active_res["delEnabledOnYes"])
            self.assertEqual(active_res["badgeSliderMin"], "1")
            self.assertEqual(active_res["badgePromptSlot1"], "")
            self.assertIn("Alpha Portfolio", active_res["badgePromptAfterPreset"])
            self.assertIn("gilded", active_res["badgePromptAfterPreset"].lower())

            # Verify Annual Burn Rate slider & text override in portfolio settings (DP-04: draft starts None)
            self.assertIsNone(active_res["defaultBurnDraft"])
            self.assertEqual(active_res["draftAfterSlider"], 75000)
            self.assertEqual(active_res["inputValAfterSlider"], "$75,000")
            self.assertEqual(active_res["draftAfterType"], 2000000)
            self.assertEqual(active_res["sliderAfterType"], "1000000")
            self.assertEqual(active_res["inputValAfterBlur"], "$2,000,000")

            # Verify badge rotation behavior: slot 1 Default, 2-10 moving window of 9
            self.assertEqual(active_res["rotBeforeLen"], 10)
            self.assertEqual(active_res["rotAfterLen"], 10)
            self.assertEqual(active_res["rotAfterFirst"], "Assets/default_badge.svg")
            self.assertEqual(active_res["rotAfterSecond"], "b2.svg")  # b1 was dropped in moving window
            self.assertEqual(active_res["rotAfterLast"], "b10.svg")
            self.assertEqual(active_res["sliderMaxAfter"], "10")
            self.assertEqual(active_res["activeBadgeAfterSlider"], "b10.svg")

            # Verify badge metadata sync with slider & prompt
            self.assertEqual(active_res["slot10Prompt"], "Custom quantum prompt")
            self.assertTrue(active_res["slot10QuantumActive"])
            self.assertFalse(active_res["afterEditQuantumActive"])
            self.assertEqual(active_res["slot9Prompt"], "Hand typed prompt")
            self.assertFalse(active_res["slot9AnyActive"])
            self.assertEqual(active_res["slot1PromptAfterScroll"], "")
            self.assertFalse(active_res["slot1AnyActive"])

            # Static HTML labels check: slider present, Reset to Default absent
            self.assertIn('id="proj-badge-slider"', html_empty)
            self.assertNotIn("Reset to Default", html_empty)
            self.assertIn(">Portfolio<", html_empty)
            self.assertIn(">Select Portfolio<", html_empty)
            self.assertIn(">Close<", html_empty)
            self.assertLess(html_empty.index('id="proj-save-btn"'), html_empty.index('id="proj-close-btn"'))

    def test_reopening_editor_with_no_active_portfolio_keeps_saved_draft(self) -> None:
        """Verifies the editor loads the edited portfolio's own config, not the empty no-active-portfolio response."""
        with _IsolatedPortfolioEnv() as env:
            config.set_active_portfolio(None)
            with PortfolioDB(":memory:") as db:
                payload = build_report_payload(db, config.default_config(), date(2026, 10, 15))
            render_html_report(payload, env.out_html)

            node_script = """
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const scripts = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
const code = scripts[scripts.length - 1][1];

const elems = new Map();
function makeEl(id) {
  if (elems.has(id)) return elems.get(id);
  const classes = new Set();
  const el = {
    id, textContent: "", innerHTML: "", value: "", disabled: false,
    style: {}, dataset: {}, options: [], min: "", max: "",
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      toggle: (c, force) => { if (force === undefined ? !classes.has(c) : force) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    addEventListener: () => {}, removeEventListener: () => {},
    setAttribute: () => {}, removeAttribute: () => {}, getAttribute: () => null,
    appendChild: () => {}, closest: () => null, focus: () => {}, scrollIntoView: () => {},
    querySelector: () => makeEl(id + "__q"), querySelectorAll: () => [],
  };
  elems.set(id, el);
  return el;
}

const doc = {
  getElementById: (id) => makeEl(id),
  querySelector: () => makeEl("__doc_q"),
  querySelectorAll: () => [],
  createElement: () => makeEl("__created"),
  createElementNS: () => makeEl("__created_ns"),
  addEventListener: () => {},
  body: makeEl("__body"), documentElement: makeEl("__root"), title: "",
};

// The portfolio the user is editing: saved on disk, but NOT the active one.
const SAVED_PROJECT = {
  version: 1,
  household_name: "Saved Household",
  tax_rates: { state_code: "CA", ltcg_top: 0.371, ordinary_top: 0.541 },
  reference_annual_burn: 120000,
};
// What `/api/project` returns with no `portfolio_id` while nothing is active: the empty default.
const EMPTY_PROJECT = { version: 1, household_name: "", tax_rates: {}, reference_annual_burn: null };
const BUNDLE = {
  active_portfolio_id: null,
  has_active: false,
  portfolios: [{ id: "p_saved", name: "Saved", is_complete: false, badge_image: "" }],
};

const ctxObj = {
  document: doc, navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: (f) => { f(); return 0; }, clearTimeout: () => {},
  requestAnimationFrame: (f) => { f(); return 0; },
  fetch: (url) => {
    const u = String(url);
    let payload = { ok: false };
    if (u.indexOf("portfolio_id=p_saved") > -1) {
      payload = { ok: true, portfolio_id: "p_saved", project: SAVED_PROJECT, portfolios_bundle: BUNDLE };
    } else if (u.indexOf("/api/project") > -1) {
      payload = { ok: true, portfolio_id: null, has_portfolio: false, project: EMPTY_PROJECT, portfolios_bundle: BUNDLE };
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => payload });
  },
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite,
  Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
};
ctxObj.globalThis = ctxObj; ctxObj.window = ctxObj; ctxObj.self = ctxObj;
ctxObj.addEventListener = () => {};
ctxObj.location = { hash: "", href: "http://127.0.0.1:8765/", search: "", protocol: "http:", reload: () => {} };
ctxObj.history = { replaceState: () => {} };
ctxObj.matchMedia = () => ({ matches: false, addEventListener: () => {} });
ctxObj.getComputedStyle = () => makeEl("__style");
ctxObj.devicePixelRatio = 1;
ctxObj.URL = URL; ctxObj.Promise = Promise; ctxObj.structuredClone = structuredClone;
ctxObj.AbortController = AbortController; ctxObj.TextEncoder = TextEncoder; ctxObj.TextDecoder = TextDecoder;

const ctx = vm.createContext(ctxObj);
vm.runInContext(code, ctx);
await vm.runInContext("openProjectSettingsDialog('info')", ctx);

const out = vm.runInContext(`JSON.stringify({
  editingId: String(_editingPortfolioId),
  draftName: String(_projDraft && _projDraft.household_name),
  draftState: String(_projDraft && (_projDraft.tax_rates || {}).state_code),
})`, ctx);
console.log(out);
"""
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", node_script, str(env.out_html)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node editor-draft check failed:\n{proc.stderr}")
            res = json.loads(proc.stdout.strip().splitlines()[-1])

            # The editor targets the selected-but-inactive portfolio...
            self.assertEqual(res["editingId"], "p_saved")
            # ...so the draft must be that portfolio's saved config. The bug adopted the empty
            # no-active-portfolio response instead, and the next auto-save wrote those blanks
            # over the real project.json.
            self.assertEqual(res["draftName"], "Saved Household")
            self.assertEqual(res["draftState"], "CA")

    def test_autosave_with_no_portfolio_reports_instead_of_discarding(self) -> None:
        """Verifies Portfolio Settings edits with no portfolio surface a message instead of silently no-op saving."""
        with _IsolatedPortfolioEnv() as env:
            config.set_active_portfolio(None)
            with PortfolioDB(":memory:") as db:
                payload = build_report_payload(db, config.default_config(), date(2026, 10, 15))
            self.assertFalse(payload["has_portfolio"])
            render_html_report(payload, env.out_html)

            node_script = """
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const scripts = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
const code = scripts[scripts.length - 1][1];

const elems = new Map();
function makeEl(id) {
  if (elems.has(id)) return elems.get(id);
  const classes = new Set();
  const el = {
    id, textContent: "", innerHTML: "", value: "", disabled: false,
    style: {}, dataset: {},
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      toggle: (c, force) => { if (force === undefined ? !classes.has(c) : force) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    addEventListener: () => {}, removeEventListener: () => {},
    setAttribute: () => {}, removeAttribute: () => {}, getAttribute: () => null,
    appendChild: () => {}, closest: () => null, focus: () => {}, scrollIntoView: () => {},
    querySelector: () => makeEl(id + "__q"), querySelectorAll: () => [],
  };
  elems.set(id, el);
  return el;
}

const doc = {
  getElementById: (id) => makeEl(id),
  querySelector: () => makeEl("__doc_q"),
  querySelectorAll: () => [],
  createElement: () => makeEl("__created"),
  createElementNS: () => makeEl("__created_ns"),
  addEventListener: () => {},
  body: makeEl("__body"), documentElement: makeEl("__root"), title: "",
};
const ctxObj = {
  document: doc, navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: (f) => { f(); return 0; },
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite,
  Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
};
ctxObj.globalThis = ctxObj; ctxObj.window = ctxObj; ctxObj.self = ctxObj;
ctxObj.addEventListener = () => {};
ctxObj.location = { hash: "", href: "http://127.0.0.1:8765/", search: "", reload: () => {} };
ctxObj.history = { replaceState: () => {} };
ctxObj.matchMedia = () => ({ matches: false, addEventListener: () => {} });
ctxObj.getComputedStyle = () => makeEl("__style");
ctxObj.devicePixelRatio = 1;
ctxObj.URL = URL; ctxObj.Promise = Promise; ctxObj.structuredClone = structuredClone;
ctxObj.AbortController = AbortController; ctxObj.TextEncoder = TextEncoder; ctxObj.TextDecoder = TextDecoder;

const ctx = vm.createContext(ctxObj);
vm.runInContext(code, ctx);

// A draft exists (the editor seeds one) but there is no portfolio to write it into.
vm.runInContext("_projDraft = _buildDefaultDraftForPortfolio('My Portfolio'); _editingPortfolioId = null;", ctx);
makeEl("proj-settings-msg").textContent = "";
vm.runInContext("_scheduleProjectAutoSave(0);", ctx);
const msgNoPortfolio = makeEl("proj-settings-msg").textContent;

// Editing a family member field must not be silently dropped either.
makeEl("proj-settings-msg").textContent = "";
vm.runInContext("_initFamilyMembersFromDraft(); _syncCurrentFamilyMemberFromDOM(true);", ctx);
const msgOnFamilyEdit = makeEl("proj-settings-msg").textContent;

// Positive control: with a portfolio to save into, the no-portfolio warning must NOT fire.
makeEl("proj-settings-msg").textContent = "";
vm.runInContext("_editingPortfolioId = 'some_portfolio'; _scheduleProjectAutoSave(0);", ctx);
const msgWithPortfolio = makeEl("proj-settings-msg").textContent;

console.log(JSON.stringify({ msgNoPortfolio, msgOnFamilyEdit, msgWithPortfolio }));
"""
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", node_script, str(env.out_html)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node autosave check failed:\n{proc.stderr}")
            res = json.loads(proc.stdout.strip().splitlines()[-1])

            # The bug: both of these were "" — the save returned at its first line with no signal.
            self.assertIn("No portfolio selected", res["msgNoPortfolio"])
            self.assertIn("No portfolio selected", res["msgOnFamilyEdit"])
            self.assertNotIn("No portfolio selected", res["msgWithPortfolio"])

    def test_badge_generation_and_rotation_server(self) -> None:
        """Tests that handle_generate_badge saves generated SVG and updates 10-badge rotation window."""
        with _IsolatedPortfolioEnv() as env:
            p_res = advisorbot_server.create_portfolio("Omega Household", activate=True)
            self.assertTrue(p_res.get("ok"))
            pid = p_res["portfolio_id"]

            # Generate first badge
            b1 = advisorbot_server.handle_generate_badge({
                "portfolio_id": pid,
                "portfolio_name": "Omega Household",
                "prompt": "Gilded gold badge",
                "model": "parametric",
                "preset": "gilded",
                "edited_by_hand": False,
            })
            self.assertTrue(b1.get("ok"))
            self.assertEqual(len(b1["badge_history"]), 2)
            self.assertEqual(b1["badge_history"][0], "Assets/default_badge.svg")
            self.assertIn("badge_gen_", b1["badge_history"][1])
            self.assertIn("badge_metadata", b1)
            b1_meta = b1["badge_metadata"].get(b1["badge_history"][1])
            self.assertIsNotNone(b1_meta)
            self.assertEqual(b1_meta["preset"], "gilded")
            self.assertEqual(b1_meta["prompt"], "Gilded gold badge")
            self.assertFalse(b1_meta["edited_by_hand"])

            # Generate 9 more badges to test the 10-badge moving window
            cfg_path = config.get_default_config_path(pid)
            cfg = config.load_config(cfg_path, portfolio_id=pid)
            # Pre-seed 9 custom badges
            cfg["badge_history"] = ["Assets/default_badge.svg"] + [f"gen_{i}.svg" for i in range(1, 10)]
            config.save_config(cfg, cfg_path, portfolio_id=pid)

            # Generate one more (10th generated badge)
            b_next = advisorbot_server.handle_generate_badge({
                "portfolio_id": pid,
                "portfolio_name": "Omega Household",
                "model": "parametric",
            })
            self.assertTrue(b_next.get("ok"))
            hist = b_next["badge_history"]
            self.assertEqual(len(hist), 10)
            self.assertEqual(b_next["badge_history"][0], "Assets/default_badge.svg")  # Always Default
            self.assertEqual(b_next["badge_history"][1], "gen_2.svg")  # gen_1 was dropped
            self.assertIn("badge_gen_", b_next["badge_history"][-1])  # latest generated badge at the end

            # Generate with iterate_current: True
            b_iter = advisorbot_server.handle_generate_badge({
                "portfolio_id": pid,
                "portfolio_name": "Omega Household",
                "prompt": "Change accents to electric cyan",
                "model": "parametric",
                "iterate_current": True,
            })
            self.assertTrue(b_iter.get("ok"))
            self.assertEqual(len(b_iter["badge_history"]), 10)

            # Assert HTML contracts for slider labels, preset chips, and iterate checkbox
            with PortfolioDB(env.portfolios_dir / pid / "portfolio.sqlite") as db:
                payload = build_report_payload(db, config.load_config(portfolio_id=pid), date(2026, 10, 15))
            html = html_report.build_html_string(payload)
            self.assertIn("proj-badge-slider-left-lbl", html)
            self.assertIn("proj-badge-slider-right-lbl", html)
            self.assertIn("1 / 10 used", html)
            self.assertIn("proj-badge-delete-btn", html)
            self.assertIn("chip-badge-gilded", html)
            self.assertIn("chip-badge-crest", html)
            self.assertIn("chip-badge-fintech", html)
            self.assertIn("chip-badge-prism", html)
            self.assertIn("chip-badge-swiss", html)
            self.assertIn("chip-badge-nordic", html)
            self.assertIn("chip-badge-artdeco", html)
            self.assertIn("chip-badge-flora", html)
            self.assertIn("chip-badge-quantum", html)
            self.assertIn("chip-badge-monogram", html)
            self.assertIn("proj-badge-iterate-check", html)

    def test_saved_prompts_lifecycle_and_migration(self) -> None:
        """Verifies default pre-population, CRUD persistence (including empty []), and legacy migration."""
        with _IsolatedPortfolioEnv() as env:
            # 1. Create a new portfolio -> automatically seeded with the default prompts
            p_res = advisorbot_server.create_portfolio("Beta Family", activate=True)
            self.assertTrue(p_res.get("ok"))
            pid = p_res["portfolio_id"]

            cfg = config.load_config(config.get_default_config_path(pid), portfolio_id=pid)
            prompts = cfg.get("saved_prompts") or []
            self.assertEqual(
                [p["title"] for p in prompts],
                [title for _, title in config.DEFAULT_SAVED_PROMPT_SPECS],
            )
            self.assertEqual(
                [p["id"] for p in prompts],
                ["preferred_forecast", "portfolio_improvements", "monthly_spending", "growth_sensitivity", "gift_affordability"],
            )
            self.assertTrue(all(str(p.get("prompt", "")).strip() for p in prompts))

            # 2. Add a custom prompt via handle_save_project_settings
            updated = prompts + [{"id": "custom_prompt_1", "title": "Cash Drag", "prompt": "Analyze cash yield."}]
            save_res = advisorbot_server.handle_save_project_settings({
                "portfolio_id": pid,
                "household_name": "Beta Family",
                "saved_prompts": updated,
                "rebuild": False,
            })
            self.assertTrue(save_res.get("ok"))
            self.assertEqual(save_res["project"]["saved_prompts"], updated)

            # 3. Delete ALL prompts -> saved_prompts stays explicitly []
            empty_save = advisorbot_server.handle_save_project_settings({
                "portfolio_id": pid,
                "household_name": "Beta Family",
                "saved_prompts": [],
                "rebuild": False,
            })
            self.assertTrue(empty_save.get("ok"))
            self.assertEqual(empty_save["project"]["saved_prompts"], [])
            reloaded = config.load_config(config.get_default_config_path(pid), portfolio_id=pid)
            self.assertEqual(reloaded["saved_prompts"], [])

            # 4. Legacy project.json without saved_prompts migrates advisor_prompts overrides
            legacy_path = config.get_default_config_path(pid)
            raw_legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            raw_legacy.pop("saved_prompts", None)
            raw_legacy["advisor_prompts"] = {
                "preferred_forecast": "Custom forecast rule",
                "portfolio_improvements": "",
            }
            legacy_path.write_text(json.dumps(raw_legacy), encoding="utf-8")
            migrated = config.load_config(legacy_path, portfolio_id=pid)
            mig_prompts = migrated["saved_prompts"]
            self.assertEqual(len(mig_prompts), 5)
            self.assertEqual(mig_prompts[0]["title"], "Preferred Simulation Settings")
            self.assertEqual(mig_prompts[0]["prompt"], "Custom forecast rule")
            self.assertEqual(mig_prompts[1]["title"], "Recommended Portfolio Improvements")
            self.assertTrue(bool(mig_prompts[1]["prompt"].strip()))

            # Older portfolios with an edited list gain only the new questions.
            raw_legacy.pop("saved_prompts_version", None)
            custom = {"id": "my_question", "title": "My question", "prompt": "Custom text"}
            edited = dict(prompts[2], prompt="My spending question")
            raw_legacy["saved_prompts"] = [custom, edited]
            legacy_path.write_text(json.dumps(raw_legacy), encoding="utf-8")
            upgraded = config.load_config(legacy_path, portfolio_id=pid)
            self.assertEqual(upgraded["saved_prompts"], [custom, edited, *prompts[3:]])
            upgraded["saved_prompts"].pop()
            config.save_config(upgraded, legacy_path, portfolio_id=pid)
            self.assertEqual(config.load_config(legacy_path, portfolio_id=pid)["saved_prompts"], [custom, edited, prompts[3]])
            self.assertEqual(config.normalize_saved_prompts({"saved_prompts": []}), [])

    def test_prompts_ui_and_agent_dropdown_node_vm(self) -> None:
        """Verifies Prompts settings UI and Agent dropdown behavior in rendered HTML via Node VM."""
        with _IsolatedPortfolioEnv() as env:
            # Render empty state (0 portfolios)
            config.set_active_portfolio(None)
            with PortfolioDB(":memory:") as db:
                payload_empty = build_report_payload(db, config.default_config(), date(2026, 10, 15))
            empty_html_path = env.root / "empty_prompts.html"
            render_html_report(payload_empty, empty_html_path)

            # Create active portfolio, complete required investor config so it activates, and render
            p_res = advisorbot_server.create_portfolio("Gamma Portfolio", activate=True)
            self.assertTrue(p_res.get("ok"))
            pid = p_res["portfolio_id"]
            advisorbot_server.handle_save_project_settings({
                "portfolio_id": pid,
                "household_name": "Gamma Portfolio",
                "reference_annual_burn": 100000,
                "tax_rates": {
                    "state_code": "WA",
                    "ltcg_top": 0.238,
                    "ordinary_top": 0.37,
                    "ira_ordinary": 0.37,
                },
                "investors": [
                    {
                        "id": "alex",
                        "name": "Alex",
                        "birth_date": "1975-05-10",
                        "milestone_age": 85,
                        "is_rmd_owner": True,
                    }
                ],
            })
            config.set_active_portfolio(pid)
            with PortfolioDB(env.portfolios_dir / pid / "portfolio.sqlite") as db:
                payload_active = build_report_payload(db, config.load_config(portfolio_id=pid), date(2026, 10, 15))
            active_html_path = env.root / "active_prompts.html"
            render_html_report(payload_active, active_html_path)
            html_active = active_html_path.read_text(encoding="utf-8")

            node_script = r"""
import fs from 'node:fs';
import vm from 'node:vm';

function runPromptsCheck(htmlPath) {
  const html = fs.readFileSync(htmlPath, 'utf8');
  const scriptMatches = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
  const inline = scriptMatches.filter(m => !/\bsrc=/.test(m[1])).map(m => m[2]);
  const code = inline[inline.length - 1];
  const els = new Map();
  function makeEl(id = '') {
    if (id && els.has(id)) return els.get(id);
    const el = {
      id, tagName: 'DIV', type: 'text', value: '', textContent: '', _innerHTML: '',
      min: '1', max: '10', step: '1', checked: false, disabled: false,
      style: {}, dataset: {},
      classList: {
        _set: new Set(),
        add(c) { this._set.add(c); },
        remove(c) { this._set.delete(c); },
        toggle(c, f) { if (f === undefined) { if (this._set.has(c)) this._set.delete(c); else this._set.add(c); } else if (f) this._set.add(c); else this._set.delete(c); },
        contains(c) { return this._set.has(c); }
      },
      options: [],
      get innerHTML() { return this._innerHTML; },
      set innerHTML(v) {
        this._innerHTML = String(v);
        if (id === 'ai-prompts-select') {
          this.options = [];
          const re = /<option\s+value="([^"]*)"[^>]*>([\s\S]*?)<\/option>/gi;
          let m;
          while ((m = re.exec(this._innerHTML)) !== null) {
            this.options.push({ value: m[1], textContent: m[2] });
          }
        }
      },
      appendChild() {}, addEventListener() {}, removeEventListener() {},
      setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
      scrollIntoView() {}, focus() {}, select() {}, click() {}, remove() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
      getContext() { return null; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 300 }; },
      closest() { return null; },
    };
    if (id) els.set(id, el);
    return el;
  }
  const fetchCalls = [];
  const doc = {
    getElementById: (id) => makeEl(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => makeEl(),
    body: makeEl('__body'),
    addEventListener: () => {},
  };
  const win = {
    location: { protocol: 'http:', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/', hash: '' },
    addEventListener: () => {},
    matchMedia: () => ({ matches: false }),
    innerWidth: 1280, innerHeight: 900,
    confirm: () => true,
  };
  const ctxObj = {
    document: doc, window: win, navigator: { userAgent: 'node' }, console,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    requestAnimationFrame: (f) => { f(); return 0; },
    fetch: (url, opts) => {
      fetchCalls.push({ url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null });
      return Promise.resolve({
        ok: true,
        json: async () => ({ ok: true, job_id: 'job_test', reply: 'ok', status: 'running' }),
      });
    },
    Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  };
  ctxObj.globalThis = ctxObj;
  ctxObj.window = ctxObj;
  ctxObj.self = ctxObj;
  ctxObj.addEventListener = () => {};
  ctxObj.location = win.location;
  ctxObj.history = { replaceState: () => {} };
  ctxObj.URL = URL; ctxObj.Promise = Promise; ctxObj.structuredClone = structuredClone;
  ctxObj.AbortController = AbortController; ctxObj.TextEncoder = TextEncoder; ctxObj.TextDecoder = TextDecoder;

  const ctx = vm.createContext(ctxObj);
  vm.runInContext(code, ctx);
  vm.runInContext('initHeaderAndKPIs(); updateProjectSettingsHeaderUI(); renderAiPromptsDropdown();', ctx);

  const initialOpts = makeEl('ai-prompts-select').options.map(o => ({ value: o.value, text: o.textContent }));

  // Test deep link when Create Saved Prompt is selected
  let openedPromptsTabFromDropdown = false;
  if (initialOpts.some(o => o.value === '__create_saved_prompt__')) {
    const sel = makeEl('ai-prompts-select');
    sel.value = '__create_saved_prompt__';
    vm.runInContext('onAiPromptDropdownChange(document.getElementById("ai-prompts-select"));', ctx);
    openedPromptsTabFromDropdown = makeEl('project-settings-backdrop').classList.contains('open') && makeEl('proj-pane-prompts').classList.contains('active');
  }

  let planningPrompts = [];
  let advisorCall = null;
  let customChatCall = null;
  let listAfterAdd = null;
  let detailAfterSelect = null;
  let emptyStateAfterDeleteAll = null;

  if (initialOpts.some(o => o.value === '0')) {
    const sel = makeEl('ai-prompts-select');
    sel.value = '0';
    vm.runInContext('onAiPromptDropdownChange(document.getElementById("ai-prompts-select"));', ctx);
    advisorCall = fetchCalls.find(c => c.url.includes('/api/advisor/run')) || null;

    planningPrompts = vm.runInContext(`(() => {
      const sent = [];
      const original = sendQuickAiPrompt;
      try {
        sendQuickAiPrompt = text => sent.push(text);
        [2, 3, 4].forEach(index => onAiPromptDropdownChange({value: String(index)}));
      } finally {
        sendQuickAiPrompt = original;
      }
      return sent;
    })()`, ctx);

    // Open Prompts tab, verify list + add + edit + immediate submit of custom prompt + delete all
    vm.runInContext('openProjectSettingsDialog("prompts");', ctx);
    detailAfterSelect = {
      title: makeEl('proj-prompt-title-input').value,
      detailLen: (makeEl('proj-prompt-detail-input').value || '').length,
      detailBoxDisplay: makeEl('proj-prompt-detail-box').style.display,
    };

    vm.runInContext('addProjectPromptRow();', ctx);
    makeEl('proj-prompt-title-input').value = 'Tax Sweep';
    makeEl('proj-prompt-detail-input').value = 'Analyze tax loss harvesting options now.';
    vm.runInContext('onProjectPromptFieldInput(true); _activeSavedPrompts = JSON.parse(JSON.stringify(_projDraft.saved_prompts)); renderAiPromptsDropdown();', ctx);
    listAfterAdd = makeEl('ai-prompts-select').options.map(o => o.textContent);

    sel.value = String(listAfterAdd.length - 2);
    vm.runInContext('onAiPromptDropdownChange(document.getElementById("ai-prompts-select"));', ctx);
    customChatCall = fetchCalls.find(c => c.url.includes('/api/chat')) || null;

    // Delete all saved prompts
    vm.runInContext('for (let n = _projDraft.saved_prompts.length; n > 0; n--) removeProjectPromptRow(0); _activeSavedPrompts = []; renderAiPromptsDropdown();', ctx);
    emptyStateAfterDeleteAll = {
      draftLen: vm.runInContext('_projDraft.saved_prompts.length', ctx),
      detailDisplay: makeEl('proj-prompt-detail-box').style.display,
      dropdownOpts: makeEl('ai-prompts-select').options.map(o => o.textContent),
    };
  }

  vm.runInContext(`
    availableAiModels = [
      { id: 'llm:claude:claude-sonnet-5-5', raw_id: 'claude', provider: 'claude', model: 'claude-sonnet-5-5', display_name: 'Claude Sonnet 5.5', type: 'api', has_key: true, status: 'working', latency_ms: 641, error: null },
      { id: 'llm:gemini:gemini-2.5-flash', raw_id: 'gemini', provider: 'gemini', model: 'gemini-2.5-flash', display_name: 'Gemini 2.5 Flash', type: 'api', has_key: true, status: 'error', error: 'Quota exceeded' },
      { id: 'llm:gemini:gemini-3.8-flash', raw_id: 'gemini', provider: 'gemini', model: 'gemini-3.8-flash', display_name: 'Gemini 3.8 Flash', type: 'api', has_key: true, status: 'error', error: 'Quota exceeded' },
      { id: 'llm:openai:gpt-4.1', raw_id: 'openai', provider: 'openai', model: 'gpt-4.1', display_name: 'OpenAI GPT-4.1', type: 'api', has_key: true, status: 'error', error: 'You have no credits remaining' },
      { id: 'llm:missing:m1', raw_id: 'missing', provider: 'missing', model: 'm1', display_name: 'Missing Key Model', type: 'api', has_key: false, status: 'missing_key', error: 'API key not set' },
    ];
    renderAiModelUI();
  `, ctx);
  const aiModelSelectHtml = makeEl('ai-model-select').innerHTML;
  const badgeSelectHtml = makeEl('proj-badge-model-select').innerHTML;

  return {
    initialOpts,
    planningPrompts,
    openedPromptsTabFromDropdown,
    advisorCall,
    customChatCall,
    listAfterAdd,
    detailAfterSelect,
    emptyStateAfterDeleteAll,
    aiModelSelectHtml,
    badgeSelectHtml,
  };
}

console.log(JSON.stringify({
  emptyRes: runPromptsCheck(process.argv[1]),
  activeRes: runPromptsCheck(process.argv[2]),
}));
"""
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", node_script, str(empty_html_path), str(active_html_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node prompt check failed:\n{proc.stderr}")
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])

            empty_res = parsed["emptyRes"]
            self.assertEqual(
                [o["text"] for o in empty_res["initialOpts"]],
                ["Run a Saved Prompt...", "Create Saved Prompt"],
            )
            self.assertTrue(empty_res["openedPromptsTabFromDropdown"])

            default_titles = [title for _, title in config.DEFAULT_SAVED_PROMPT_SPECS]
            active_res = parsed["activeRes"]
            self.assertEqual(
                [o["text"] for o in active_res["initialOpts"]],
                ["Run a Saved Prompt...", *default_titles],
            )
            self.assertEqual(active_res["planningPrompts"], [entry["prompt"] for entry in config.default_saved_prompts()[2:]])
            self.assertIsNotNone(active_res["advisorCall"])
            self.assertEqual(active_res["advisorCall"]["body"]["prompt_type"], "preferred_forecast")
            self.assertEqual(active_res["detailAfterSelect"]["title"], "Preferred Simulation Settings")
            self.assertGreater(active_res["detailAfterSelect"]["detailLen"], 20)
            self.assertEqual(active_res["detailAfterSelect"]["detailBoxDisplay"], "block")
            self.assertEqual(
                active_res["listAfterAdd"],
                ["Run a Saved Prompt...", *default_titles, "Tax Sweep"],
            )
            self.assertIsNotNone(active_res["customChatCall"])
            self.assertEqual(active_res["customChatCall"]["body"]["prompt"], "Analyze tax loss harvesting options now.")
            self.assertEqual(active_res["emptyStateAfterDeleteAll"]["draftLen"], 0)
            self.assertEqual(active_res["emptyStateAfterDeleteAll"]["detailDisplay"], "none")
            self.assertEqual(
                active_res["emptyStateAfterDeleteAll"]["dropdownOpts"],
                ["Run a Saved Prompt...", "Create Saved Prompt"],
            )

            # Static HTML labels check
            self.assertTrue(">Site Graphic" in html_active or ">Portfolio Badge<" in html_active)
            self.assertIn('id="nav-ai-lbl">AI Chat<', html_active)
            self.assertNotIn('id="nav-ai-lbl">AI Financial Advisor<', html_active)
            self.assertIn('id="ai-model-config-label"', html_active)
            self.assertIn("3rd-Party LLM Auto-Discovery", html_active)
            self.assertIn("api_base_url", html_active)
            self.assertNotIn('id="ai-models-status-row"', html_active)
            self.assertNotIn('id="ai-models-pills-container"', html_active)
            self.assertIn(">Prompts<", html_active)
            self.assertIn(">+Create Prompt<", html_active)
            self.assertIn(">Short Title<", html_active)
            self.assertIn(">Prompt Detail<", html_active)
            self.assertNotIn("Advisor Preset", html_active)
            self.assertNotIn("Reset to Generic Default", html_active)
            self.assertNotIn("ai-quick-chips", html_active)

            # Model status indicator parity between AI Chat (#ai-model-select) & Badge dropdown
            for sel_html in (active_res["aiModelSelectHtml"], active_res["badgeSelectHtml"]):
                self.assertIn("🟢 Claude Sonnet 5.5", sel_html)
                self.assertIn("(641ms)", sel_html)
                self.assertIn("🔴 Gemini 2.5 Flash", sel_html)
                self.assertIn("(quota/429)", sel_html)
                self.assertIn("🔴 Gemini 3.8 Flash", sel_html)
                self.assertIn("🔴 OpenAI GPT-4.1", sel_html)
                self.assertIn("(no credits)", sel_html)
                self.assertIn("⚪ Missing Key Model", sel_html)
                self.assertIn("(needs key)", sel_html)
                self.assertNotIn("Antigravity CLI", sel_html)
                self.assertNotIn("Gemini 4 Argon (Medium)", sel_html)
                self.assertIn("Gemini (Direct API)", sel_html)

    def test_financial_statements_dry_run_and_status(self) -> None:
        """Verifies statement parsers do dry runs emitting pass/fail signals to drive status capsules (Processed, Available, Unknown)."""
        import hashlib
        from unittest.mock import patch
        from config import save_config
        from db.database import PortfolioDB
        from advisorbot_server import handle_list_statements

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            p_dir = tmp_root / "Portfolios" / "test_p"
            stmts_dir = p_dir / "Statements"
            stmts_dir.mkdir(parents=True, exist_ok=True)
            db_path = p_dir / "portfolio.sqlite"
            cfg_path = p_dir / "project.json"

            save_config({"household_name": "Test P", "investors": []}, cfg_path, portfolio_id="test_p")
            with PortfolioDB(db_path) as db:
                db.ensure_schema()
                proc_file = stmts_dir / "Processed.grants.json"
                proc_file.write_text("dummy")
                h = hashlib.sha256(proc_file.read_bytes()).hexdigest()
                db.conn.execute(
                    """
                    INSERT INTO ingested_files (sha256_hash, file_path, rel_path, file_size, file_birthtime, file_mtime, file_ctime, doc_type, account_id, period_start, statement_as_of_date, reported_total, ingested_at_iso)
                    VALUES (?, ?, 'Processed.grants.json', 100, 1.0, 1.0, 1.0, 'GENERIC_GRANTS', 'ACC1', '2026-01-01', '2026-10-01', 1000.0, '2026-11-13T00:00:00Z')
                    """,
                    (h, str(proc_file.resolve()))
                )
                db.conn.commit()

            # Create Available file (valid grants schema that passes dry run)
            avail_file = stmts_dir / "Available.grants.json"
            avail_file.write_text(json.dumps({
                "company": "Beta Corp",
                "symbol": "BETA",
                "as_of_date": "2026-11-05",
                "unvested_tranches": [{"grant_id": "B-1", "vest_date": "2027-02-01", "shares": 75, "orig_qty": 75}]
            }))

            # Create Unknown file (corrupt or unrecognizable)
            unknown_file = stmts_dir / "Unknown.txt"
            unknown_file.write_text("Unrecognizable statement content")

            with patch("config.get_portfolio_dir", return_value=p_dir), \
                 patch("config.get_statements_dir", return_value=stmts_dir), \
                 patch("config.get_default_config_path", return_value=cfg_path), \
                 patch("config.get_default_db_path", return_value=db_path):

                res = handle_list_statements("test_p")
                self.assertTrue(res["ok"])
                self.assertEqual(res["counts"]["processed"], 1)
                self.assertEqual(res["counts"]["available"], 1)
                self.assertEqual(res["counts"]["unknown"], 1)
                self.assertTrue(res["has_available"])

                file_map = {f["filename"]: f for f in res["files"]}
                self.assertEqual(file_map["Processed.grants.json"]["status"], "Processed")
                self.assertEqual(file_map["Processed.grants.json"]["status_color"], "gray")

                self.assertEqual(file_map["Available.grants.json"]["status"], "Available")
                self.assertEqual(file_map["Available.grants.json"]["status_color"], "green")
                self.assertEqual(file_map["Available.grants.json"]["date"], "2026-11-05")

                self.assertEqual(file_map["Unknown.txt"]["status"], "Unknown")
                self.assertEqual(file_map["Unknown.txt"]["status_color"], "red")
                self.assertIsNotNone(file_map["Unknown.txt"]["error"])

                # Demo Mode: counts["total"] remains exact integer count (not scaled by k), and processed PDFs skip extraction
                pdf_file = stmts_dir / "ProcessedStmt.pdf"
                pdf_file.write_bytes(b"%PDF-1.4\n%fake\n")
                pdf_hash = hashlib.sha256(pdf_file.read_bytes()).hexdigest()
                with PortfolioDB(db_path) as db:
                    db.conn.execute(
                        """
                        INSERT INTO ingested_files (sha256_hash, file_path, rel_path, file_size, file_birthtime, file_mtime, file_ctime, doc_type, account_id, period_start, statement_as_of_date, reported_total, ingested_at_iso)
                        VALUES (?, ?, 'ProcessedStmt.pdf', 100, 1.0, 1.0, 1.0, 'BROKERAGE_PDF', 'ACC1', '2026-10-01', '2026-11-01', 1000.0, '2026-11-13T00:00:00Z')
                        """,
                        (pdf_hash, str(pdf_file.resolve())),
                    )
                    db.conn.commit()
                from privacy import default_privacy_config

                demo_cfg = default_privacy_config()
                demo_cfg["demo_mode"]["enabled"] = True
                demo_cfg["demo_mode"]["scale_factor"] = 0.25
                extracted_pdfs: list[Path] = []
                with patch("advisorbot_server._active_demo_privacy_cfg", return_value=demo_cfg), \
                     patch("config.get_active_portfolio_id", return_value="test_p"), \
                     patch("ingest.pdf_reader.extract_pdf_pages_batch", side_effect=lambda paths: (extracted_pdfs.extend(paths) or {})):
                    demo_res = handle_list_statements("demo_portfolio")
                self.assertTrue(demo_res["ok"])
                self.assertEqual(demo_res["portfolio_id"], "demo_portfolio")
                self.assertEqual(demo_res["counts"], {"processed": 2, "available": 1, "unknown": 1, "total": 4})
                self.assertEqual(extracted_pdfs, [])

    def test_scenarios_card_keyboard_nav_and_active_cyan_ring(self) -> None:
        """Verifies removal of 'Open in Simulator' button, active card gold ring + outer teal focus ring, and arrow+Enter navigation."""
        with _IsolatedPortfolioEnv() as env:
            p_res = advisorbot_server.create_portfolio("Scenario Portfolio", activate=False)
            self.assertTrue(p_res.get("ok"))
            pid = p_res["portfolio_id"]
            _complete_test_portfolio(pid, activate=True)

            # Save 2 extra scenarios in Saves/
            s_dir = config.get_saves_dir(pid)
            s_dir.mkdir(parents=True, exist_ok=True)
            base_cfg = {
                "version": 1,
                "created_at": "2026-11-13 14:00",
                "settings": {"futureGoogGrowthPct": 8.0, "simAnnualBurn": 180000},
            }
            (s_dir / "Bull_Case.json").write_text(json.dumps(dict(base_cfg, name="Bull Case")), encoding="utf-8")
            (s_dir / "Bear_Case.json").write_text(json.dumps(dict(base_cfg, name="Bear Case")), encoding="utf-8")

            with PortfolioDB(env.portfolios_dir / pid / "portfolio.sqlite") as db:
                payload = build_report_payload(db, config.load_config(portfolio_id=pid), date(2026, 10, 15))
            html = html_report.build_html_string(payload)
            out_path = env.root / "scenarios.html"
            render_html_report(payload, out_path)

            # 1. Assert "Open in Simulator" button is absent
            self.assertNotIn("Open in Simulator", html)

            # 2. Assert (A) Inner yellow-gold ring for currently loaded card and (B) Outer teal navigation focus ring wrapping
            self.assertIn("border-color: #fbbf24 !important", html)
            self.assertIn("box-shadow: 0 0 0 2px #fbbf24", html)
            self.assertIn("outline: 2px solid #38bdf8 !important", html)
            self.assertIn("outline-offset: 4px !important", html)
            self.assertIn(".model-card:only-child:focus", html)
            self.assertIn("outline: none !important", html)

            # 3. Node VM contract test for keyboard navigation (arrow keys + Enter)
            node_script = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[1], 'utf8');
const scriptMatches = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
const inline = scriptMatches.filter(m => !/\bsrc=/.test(m[1])).map(m => m[2]);
const code = inline[inline.length - 1];

const els = new Map();
function makeEl(id = '') {
  if (id && els.has(id)) return els.get(id);
  const classes = new Set();
  const el = {
    id, tagName: 'DIV', textContent: '', value: '',
    style: {}, dataset: {}, _innerHTML: '',
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) {
      this._innerHTML = String(v);
      if (id === 'models-cards-container') {
        for (const k of Array.from(els.keys())) {
          if (k.startsWith('model-card-')) els.delete(k);
        }
      }
      const re = /<div\s+class="([^"]*)"\s+id="(model-card-[^"]*)"\s+data-model-id="([^"]*)"/g;
      let m;
      while ((m = re.exec(this._innerHTML)) !== null) {
        const cardEl = makeEl(m[2]);
        cardEl.dataset.modelId = m[3];
        cardEl.classList.clear();
        m[1].split(/\s+/).forEach(c => { if (c) cardEl.classList.add(c); });
      }
    },
    classList: {
      add(...cs) { cs.forEach(c => classes.add(c)); },
      remove(...cs) { cs.forEach(c => classes.delete(c)); },
      clear() { classes.clear(); },
      toggle(c, force) { if (force === undefined ? !classes.has(c) : force) classes.add(c); else classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    addEventListener() {}, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k] || null; },
    focus() { els.set('__focused', this); },
    scrollIntoView() {},
    closest(sel) { return sel === '.model-card' ? this : null; },
    querySelector() { return null; },
    querySelectorAll(sel) {
      if (sel && sel.includes('.model-card')) {
        return Array.from(els.values()).filter(e => e.id.startsWith('model-card-'));
      }
      return [];
    },
  };
  if (id) els.set(id, el);
  return el;
}

const doc = {
  getElementById: (id) => makeEl(id),
  querySelector: () => null,
  querySelectorAll: (sel) => {
    if (sel.includes('.model-card')) {
      return Array.from(els.values()).filter(e => e.id.startsWith('model-card-'));
    }
    return [];
  },
  createElement: () => makeEl(),
  body: makeEl('__body'),
  addEventListener() {},
  get activeElement() { return els.get('__focused') || null; },
};

const win = {
  location: { protocol: 'http:', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/', hash: '' },
  history: { replaceState() {}, pushState() {} },
  addEventListener: (type, fn) => { if (type === 'keydown') win.__keydownListener = fn; },
  matchMedia: () => ({ matches: false }),
  innerWidth: 1280, innerHeight: 900,
};

const ctxObj = {
  document: doc, window: win, navigator: { userAgent: 'node' }, console,
  history: win.history,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: (f) => { f(); return 0; },
  fetch: () => Promise.resolve({ ok: true, json: async () => ({ ok: true }) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  AbortController, TextEncoder, TextDecoder, URL,
};
ctxObj.globalThis = ctxObj; ctxObj.self = ctxObj;
const ctx = vm.createContext(ctxObj);
vm.runInContext(code, ctx);

// Switch to scenarios screen and render cards
vm.runInContext('switchScreen("models"); renderModelsScreen();', ctx);

const initialCards = doc.querySelectorAll('.model-card');
const initialActive = initialCards.find(c => c.classList.contains('active-loaded') || c.classList.contains('active-model'));
const initialFocused = doc.activeElement;

// Step 1: When card 0 receives ArrowRight, onModelCardKeyDown must call stopPropagation()
// and move focus to card 1 (index 1), NEVER skipping to card 2 (index 2).
let stoppedProp = false;
let defaultPrevented = false;
ctx.testEvt = {
  key: "ArrowRight",
  preventDefault: () => { defaultPrevented = true; },
  stopPropagation: () => { stoppedProp = true; },
  target: initialCards[0],
};
vm.runInContext(`onModelCardKeyDown(testEvt, "${initialCards[0].dataset.modelId}");`, ctx);
const focusedAfterFirstRight = doc.activeElement;
const stoppedPropAfterCardKey = stoppedProp;

// Step 2: Press Enter on card 1 to load it
if (focusedAfterFirstRight) {
  await vm.runInContext(`loadModelById("${focusedAfterFirstRight.dataset.modelId}", false);`, ctx);
}
const activeAfterEnter = doc.querySelectorAll('.model-card').find(c => c.classList.contains('active-loaded') || c.classList.contains('active-model'));

// Step 3: ArrowLeft from card 1 moves strictly back to card 0
vm.runInContext('navigateModelCards("left");', ctx);
const focusedAfterLeft = doc.activeElement;

// Step 4: Verify single-scenario behavior: auto-focus is suppressed when cards.length < 2
els.delete('__focused');
vm.runInContext(`
  const firstEntry = Object.values(savesRegistry)[0];
  modelsList = [firstEntry];
  savesRegistry = { [firstEntry.name || '001']: firstEntry };
  renderModelsScreen();
`, ctx);
const singleScenarioCards = doc.querySelectorAll('.model-card');
const focusedSingleScenario = doc.activeElement;
vm.runInContext('navigateModelCards("right");', ctx);
const focusedAfterRightOnSingle = doc.activeElement;

console.log(JSON.stringify({
  cardCount: initialCards.length,
  initialActiveId: initialActive ? initialActive.dataset.modelId : null,
  initialFocusedId: initialFocused ? initialFocused.dataset.modelId : null,
  card0Id: initialCards[0]?.dataset.modelId || null,
  card1Id: initialCards[1]?.dataset.modelId || null,
  card2Id: initialCards[2]?.dataset.modelId || null,
  focusedAfterFirstRightId: focusedAfterFirstRight ? focusedAfterFirstRight.dataset.modelId : null,
  focusedAfterLeftId: focusedAfterLeft ? focusedAfterLeft.dataset.modelId : null,
  stoppedPropAfterCardKey,
  activeAfterEnterId: activeAfterEnter ? activeAfterEnter.dataset.modelId : null,
  singleCardCount: singleScenarioCards.length,
  focusedSingleScenarioId: focusedSingleScenario ? focusedSingleScenario.dataset.modelId : null,
  focusedAfterRightOnSingleId: focusedAfterRightOnSingle ? focusedAfterRightOnSingle.dataset.modelId : null,
}));
"""
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", node_script, str(out_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node scenarios check failed:\n{proc.stderr}")
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertGreaterEqual(parsed["cardCount"], 2)
            self.assertEqual(parsed["initialActiveId"], parsed["card0Id"])
            # Auto-focused upon opening when >= 2 cards, displaying both rings
            self.assertEqual(parsed["initialFocusedId"], parsed["card0Id"])
            # 1st right arrow moves strictly to card 1 (never skipping to card 2)
            self.assertEqual(parsed["focusedAfterFirstRightId"], parsed["card1Id"])
            self.assertTrue(parsed["stoppedPropAfterCardKey"])
            # Enter loads card 1 as active
            self.assertEqual(parsed["activeAfterEnterId"], parsed["card1Id"])
            # Left arrow moves back to card 0
            self.assertEqual(parsed["focusedAfterLeftId"], parsed["card0Id"])
            # When only 1 scenario card exists, auto-focus is suppressed (only 1 ring displayed)
            self.assertEqual(parsed["singleCardCount"], 1)
            self.assertIsNone(parsed["focusedSingleScenarioId"])
            self.assertIsNone(parsed["focusedAfterRightOnSingleId"])

    def test_ai_chat_nav_button_selected_state(self) -> None:
        """Verifies that the leftnav AI Chat button draws active/selected when open, and unselected when closed."""
        with _IsolatedPortfolioEnv() as env:
            p_res = advisorbot_server.create_portfolio("AI Button Portfolio", activate=True)
            self.assertTrue(p_res.get("ok"))
            pid = p_res["portfolio_id"]

            with PortfolioDB(env.portfolios_dir / pid / "portfolio.sqlite") as db:
                payload = build_report_payload(db, config.load_config(portfolio_id=pid), date(2026, 10, 15))
            html = html_report.build_html_string(payload)
            out_path = env.root / "ai_button.html"
            render_html_report(payload, out_path)

            # 1. Assert CSS rules for selected state exist
            self.assertIn("#nav-ai-bar.active", html)
            self.assertIn("#nav-ai-bar.selected", html)
            self.assertIn("#nav-ai-bar.active #nav-ai-btn", html)
            self.assertIn("rgba(45, 212, 191", html)

            # 2. Node VM contract test for open / close toggling
            node_script = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[1], 'utf8');
const scriptMatches = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
const inline = scriptMatches.filter(m => !/\bsrc=/.test(m[1])).map(m => m[2]);
const code = inline[inline.length - 1];

const els = new Map();
function makeEl(id = '') {
  if (id && els.has(id)) return els.get(id);
  const classes = new Set();
  if (id === 'ai-chat-pane') {
    classes.add('right-chat-pane');
    classes.add('collapsed');
  }
  const el = {
    id, tagName: 'DIV', textContent: '', value: '',
    style: {}, dataset: {}, _innerHTML: '',
    classList: {
      add(...cs) { cs.forEach(c => classes.add(c)); },
      remove(...cs) { cs.forEach(c => classes.delete(c)); },
      clear() { classes.clear(); },
      toggle(c, force) { if (force === undefined ? !classes.has(c) : force) classes.add(c); else classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    addEventListener() {}, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k] || null; },
    focus() {}, scrollIntoView() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  };
  if (id) els.set(id, el);
  return el;
}

const doc = {
  getElementById: (id) => makeEl(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl(),
  body: makeEl('__body'),
  addEventListener() {},
};

const win = {
  location: { protocol: 'http:', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/', hash: '' },
  history: { replaceState() {}, pushState() {} },
  addEventListener: () => {},
  matchMedia: () => ({ matches: false }),
  innerWidth: 1280, innerHeight: 900,
};

let storedValue = null;
const ctxObj = {
  document: doc, window: win, navigator: { userAgent: 'node' }, console,
  history: win.history,
  localStorage: { getItem: (k) => storedValue, setItem: (k, v) => { storedValue = String(v); }, removeItem: () => { storedValue = null; } },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: (f) => { f(); return 0; },
  fetch: () => Promise.resolve({ ok: true, json: async () => ({ ok: true }) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  AbortController, TextEncoder, TextDecoder, URL,
};
ctxObj.globalThis = ctxObj; ctxObj.self = ctxObj;
const ctx = vm.createContext(ctxObj);
vm.runInContext(code, ctx);

const aiNavBtn = doc.getElementById('nav-ai-bar');
const aiPane = doc.getElementById('ai-chat-pane');

// State 0: Initial state (collapsed, unselected)
const initSelected = aiNavBtn.classList.contains('selected') || aiNavBtn.classList.contains('active');
const initCollapsed = aiPane.classList.contains('collapsed');

// State 1: Open chat via toggleAiChatPane(true)
vm.runInContext('toggleAiChatPane(true);', ctx);
const openSelected = aiNavBtn.classList.contains('selected') && aiNavBtn.classList.contains('active');
const openCollapsed = aiPane.classList.contains('collapsed');
const openAria = aiNavBtn['aria-expanded'] || aiNavBtn.getAttribute('aria-expanded');

// State 2: Close chat via toggleAiChatPane(false) (like clicking "X" close button)
vm.runInContext('toggleAiChatPane(false);', ctx);
const closeSelected = aiNavBtn.classList.contains('selected') || aiNavBtn.classList.contains('active');
const closeCollapsed = aiPane.classList.contains('collapsed');
const closeAria = aiNavBtn['aria-expanded'] || aiNavBtn.getAttribute('aria-expanded');

// State 3: Toggle chat via toggleAiChatPane() (like clicking AI Chat button again)
vm.runInContext('toggleAiChatPane();', ctx);
const reOpenSelected = aiNavBtn.classList.contains('selected') && aiNavBtn.classList.contains('active');
const reOpenCollapsed = aiPane.classList.contains('collapsed');

// State 4: Toggle chat again via toggleAiChatPane() to close
vm.runInContext('toggleAiChatPane();', ctx);
const reCloseSelected = aiNavBtn.classList.contains('selected') || aiNavBtn.classList.contains('active');
const reCloseCollapsed = aiPane.classList.contains('collapsed');

console.log(JSON.stringify({
  initSelected, initCollapsed,
  openSelected, openCollapsed, openAria,
  closeSelected, closeCollapsed, closeAria,
  reOpenSelected, reOpenCollapsed,
  reCloseSelected, reCloseCollapsed,
}));
"""
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", node_script, str(out_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node AI Chat button check failed:\n{proc.stderr}")
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(parsed["initSelected"])
            self.assertTrue(parsed["initCollapsed"])
            self.assertTrue(parsed["openSelected"])
            self.assertFalse(parsed["openCollapsed"])
            self.assertEqual(parsed["openAria"], "true")
            self.assertFalse(parsed["closeSelected"])
            self.assertTrue(parsed["closeCollapsed"])
            self.assertEqual(parsed["closeAria"], "false")
            self.assertTrue(parsed["reOpenSelected"])
            self.assertFalse(parsed["reOpenCollapsed"])
            self.assertFalse(parsed["reCloseSelected"])
            self.assertTrue(parsed["reCloseCollapsed"])

    def test_fresh_install_cwd_filepath_construction(self) -> None:
        """Verifies that for a fresh install in any cwd, filepaths never refer to upstream folders."""
        orig_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir).resolve()
            try:
                os.chdir(tmp_path)
                # Ensure no root override env vars are set
                old_advisorbot_root = os.environ.pop("ADVISORBOT_ROOT", None)
                old_holdings_root = os.environ.pop("HOLDINGS_ROOT", None)
                old_portfolios_dir = os.environ.pop("PORTFOLIOS_DIR", None)
                old_data_dir = os.environ.pop("HOLDINGS_DATA_DIR", None)
                old_llm_dir = os.environ.pop("LLM_DIR", None)

                resolved = config._resolve_root()
                self.assertEqual(resolved, tmp_path)
                self.assertNotIn("..", str(resolved))

                data_dir = config.get_data_dir()
                self.assertEqual(data_dir, tmp_path / "Data")
                self.assertTrue(str(data_dir).startswith(str(tmp_path)))

                portfolios_dir = config.get_portfolios_dir()
                self.assertEqual(portfolios_dir, tmp_path / "Portfolios")
                self.assertTrue(str(portfolios_dir).startswith(str(tmp_path)))

                llm_dir = config.get_llm_dir()
                self.assertEqual(llm_dir, tmp_path / "Data" / "Config" / "LLM")
                self.assertTrue(str(llm_dir).startswith(str(tmp_path)))

                session_tok = config.get_session_token_file()
                self.assertEqual(session_tok, tmp_path / "Data" / ".session_token")
                self.assertTrue(str(session_tok).startswith(str(tmp_path)))

                # Create a portfolio and verify all created paths remain strictly inside cwd
                res = advisorbot_server.create_portfolio("Fresh User Household", activate=True)
                self.assertTrue(res.get("ok"))
                pid = res["portfolio_id"]

                pdir = config.get_portfolio_dir(pid)
                self.assertIsNotNone(pdir)
                self.assertTrue(str(pdir).startswith(str(tmp_path)))
                self.assertTrue((pdir / "project.json").is_file())
                self.assertFalse(any(".." in p for p in pdir.parts))

            finally:
                os.chdir(orig_cwd)
                if old_advisorbot_root is not None:
                    os.environ["ADVISORBOT_ROOT"] = old_advisorbot_root
                if old_holdings_root is not None:
                    os.environ["HOLDINGS_ROOT"] = old_holdings_root
                if old_portfolios_dir is not None:
                    os.environ["PORTFOLIOS_DIR"] = old_portfolios_dir
                if old_data_dir is not None:
                    os.environ["HOLDINGS_DATA_DIR"] = old_data_dir
                if old_llm_dir is not None:
                    os.environ["LLM_DIR"] = old_llm_dir

    def test_annual_burn_portfolio_setting(self):
        """Verifies Annual Burn Rate setting in portfolio settings, defaulting to $50K and overriding."""
        import tempfile
        from valuation import simulator

        with tempfile.TemporaryDirectory() as tmp_dir:
            orig_pdir = os.environ.get("PORTFOLIOS_DIR")
            try:
                os.environ["PORTFOLIOS_DIR"] = tmp_dir
                config.refresh_active_paths()

                # 1. Per DP-04, default_config() makes zero silent assumptions (reference_annual_burn is None)
                cfg_def = config.default_config()
                self.assertIsNone(cfg_def.get("reference_annual_burn"))

                # 2. create_portfolio() starts as an incomplete draft without a silent burn default
                res = config.create_portfolio("Burn Test Household", activate=True)
                self.assertTrue(res.get("ok"))
                self.assertFalse(res.get("is_complete", True))
                pid = res["portfolio_id"]
                proj = advisorbot_server.get_project_settings(pid)["project"]
                self.assertIsNone(proj.get("reference_annual_burn"))

                # 3. Completing required fields with $120,000 sets burn_ref_usd to 120000
                advisorbot_server.handle_save_project_settings({
                    "portfolio_id": pid,
                    "reference_annual_burn": 120000,
                    "rebuild": False,
                })
                proj_init = advisorbot_server.get_project_settings(pid)["project"]
                refs = simulator.get_reference_rates({"project": proj_init})
                self.assertEqual(refs["burn_ref_usd"], 120000)

                # 4. handle_save_project_settings() overrides to $2,000,000
                save_res = advisorbot_server.handle_save_project_settings({
                    "portfolio_id": pid,
                    "reference_annual_burn": 2000000,
                    "rebuild": False,
                })
                self.assertTrue(save_res.get("ok"))
                self.assertEqual(save_res["project"]["reference_annual_burn"], 2000000)

                # 5. verify saved config & simulator reference rates reflect $2,000,000
                reloaded = config.load_config(config.get_default_config_path(pid), portfolio_id=pid)
                self.assertEqual(reloaded["reference_annual_burn"], 2000000)
                refs2 = simulator.get_reference_rates({"project": reloaded})
                self.assertEqual(refs2["burn_ref_usd"], 2000000)

            finally:
                if orig_pdir is not None:
                    os.environ["PORTFOLIOS_DIR"] = orig_pdir
                else:
                    os.environ.pop("PORTFOLIOS_DIR", None)
                config.refresh_active_paths()


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
