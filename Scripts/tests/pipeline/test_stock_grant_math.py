#!/usr/bin/env python3
"""
Tests for multi-company, multi-FI Stock Grants pipeline, API/CLI ingestion,
dynamic left-nav behavior (N=0 / N=1 / N>=2), schema migration, and custom plugin discovery.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = ROOT / "Scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import default_config  # noqa: E402
from db.database import PortfolioDB  # noqa: E402
from exporters.html_report import build_html_string, render_html_report  # noqa: E402
from exporters.view_model import build_report_payload  # noqa: E402
from ingest.pipeline import ingest_statements  # noqa: E402
from ingest.plugins import build_default_registry  # noqa: E402
from ingest.plugins.grant_json import ingest_grant_payload_dict  # noqa: E402
from valuation.engine import build_and_store_timeline  # noqa: E402
from valuation.simulator import build_effective_settings, run_simulation  # noqa: E402


def _seed_minimal_portfolio(db: PortfolioDB, as_of: str = "2026-10-01") -> None:
    db.upsert_account(
        account_id="ACCT_BROKER",
        institution="Fidelity",
        account_name="Individual Brokerage",
        account_type="TAXABLE",
        tax_status="TAXABLE",
        display_order=1,
    )
    db.upsert_security(
        security_id="CASH:USD",
        symbol_or_code="CASH",
        canonical_name="Cash Sweep",
        asset_class="CASH",
        risk_class="cash_fixed",
        stack_bucket="cash_sweeps",
        is_scrapeable=False,
        quote_symbol=None,
        valuation_model="STATEMENT_ANCHOR",
    )
    db.conn.execute(
        """
        INSERT OR REPLACE INTO position_snapshots
            (account_id, security_id, as_of_date, quantity, stmt_price, stmt_market_value, cost_basis, yield_pct, details_json, source_hash)
        VALUES ('ACCT_BROKER', 'CASH:USD', ?, 500000.0, 1.0, 500000.0, 500000.0, 0.04, '{}', 'seed')
        """,
        (as_of,),
    )
    for sym, dt_s, px in [
        ("GOOG", "2026-11-10", 250.0),
        ("GOOG", "2026-11-09", 248.0),
        ("GOOG", "2025-03-01", 170.0),
        ("SPY", "2026-11-10", 580.0),
    ]:
        db.conn.execute(
            """
            INSERT OR REPLACE INTO market_prices (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
            VALUES (?, ?, ?, ?, '2026-11-10T00:00:00Z')
            """,
            (sym, dt_s, px, px),
        )
    db.conn.commit()


class StockGrantsTests(unittest.TestCase):
    def test_legacy_db_migrates_gsu_tranches_composite_pk(self) -> None:
        """Legacy DB with 3-col PK `(report_date, grant_id, vest_date)` upgrades safely to include `symbol` in PK."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE gsu_tranches (
                    report_date TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    vest_date TEXT NOT NULL,
                    grant_date TEXT NOT NULL,
                    orig_qty REAL NOT NULL,
                    deus REAL NOT NULL,
                    total_qty REAL NOT NULL,
                    source_hash TEXT NOT NULL,
                    PRIMARY KEY (report_date, grant_id, vest_date)
                );
                INSERT INTO gsu_tranches VALUES ('2026-11-09', 'G001', '2027-03-25', '2024-03-01', 10, 0.2, 10.2, 'h1');
                CREATE TABLE gsu_releases_history (
                    order_number TEXT PRIMARY KEY,
                    vest_date TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    release_price REAL NOT NULL,
                    gross_shares REAL NOT NULL,
                    net_shares REAL NOT NULL,
                    tax_method TEXT,
                    source_hash TEXT NOT NULL
                );
                CREATE TABLE gsu_withdrawals (
                    order_number TEXT PRIMARY KEY,
                    exec_date TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    tx_type TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    net_amount REAL,
                    source_hash TEXT NOT NULL
                );
                """
            )
            conn.commit()
            conn.close()

            with PortfolioDB(db_path) as db:
                tinfo = db.conn.execute("PRAGMA table_info(gsu_tranches)").fetchall()
                cols = {r["name"] for r in tinfo}
                pk_cols = {r["name"] for r in tinfo if int(r["pk"] or 0) > 0}
                self.assertEqual(pk_cols, {"report_date", "symbol", "grant_id", "vest_date"})
                self.assertIn("company_name", cols)
                self.assertIn("award_type", cols)
                # Same (report_date, grant_id, vest_date) for a second company succeeds
                db.conn.execute(
                    """
                    INSERT INTO gsu_tranches
                        (report_date, symbol, company_name, award_type, account_id, institution,
                         grant_id, vest_date, grant_date, orig_qty, deus, total_qty, source_hash)
                    VALUES ('2026-11-09', 'AAPL', 'Apple Inc.', 'RSU', '', 'Fidelity',
                            'G001', '2027-03-25', '2025-03-01', 5, 0, 5, 'h2')
                    """
                )
                cnt = db.conn.execute("SELECT COUNT(*) AS c FROM gsu_tranches").fetchone()["c"]
                self.assertEqual(cnt, 2)

    def test_n0_no_unvested_hides_nav_and_omits_future_buckets(self) -> None:
        """D-01/D-06: Historical-only releases with 0 future unvested shares keeps nav hidden (`has_unvested=False`)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "n0.sqlite"
            cfg = default_config()
            today = date(2026, 11, 10)
            with PortfolioDB(db_path) as db:
                _seed_minimal_portfolio(db)
                ingest_grant_payload_dict(
                    db,
                    cfg,
                    {
                        "institution": "Fidelity",
                        "symbol": "MSFT",
                        "company_name": "Microsoft Corporation",
                        "award_type": "RSU",
                        "releases_history": [
                            {
                                "order_number": "MSFT-REL-1",
                                "grant_id": "MSFT-2023",
                                "vest_date": "2024-06-15",
                                "release_price": 420.0,
                                "gross_shares": 50.0,
                                "net_shares": 31.0,
                            }
                        ],
                    },
                )
                build_and_store_timeline(
                    db,
                    cfg,
                    start_date=date(2026, 10, 2),
                    today_date=today,
                    forecast_end_date=today + timedelta(days=30),
                )
                payload = build_report_payload(db, cfg, today)

            sg = payload["stock_grants"]
            self.assertFalse(sg["has_unvested"])
            self.assertEqual(sg["companies"], [])
            self.assertIn("MSFT", sg["by_symbol"])
            self.assertFalse(sg["by_symbol"]["MSFT"]["has_unvested"])
            self.assertFalse(any(a["id"].startswith("future_unvested_") for a in payload["retirement_sim_assets"]))
            self.assertEqual(payload["kpis"]["future_gsu_gross_shares"], 0.0)
            self._verify_html_nav_in_node(payload, expected_visible=False, expected_lbl=None)

    def test_n1_single_company_grants_json_scanner_and_sim(self) -> None:
        """D-02/D-07: Dropping `*.grants.json` into Statements/ auto-configures `<ShortName> Stock` & simulator bucket."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            stmts_dir = tmp_p / "Statements"
            stmts_dir.mkdir(parents=True)
            db_path = tmp_p / "n1.sqlite"
            cfg = default_config()
            today = date(2026, 11, 10)

            example_src = (ROOT / "Examples" / "grants.example.json").read_text(encoding="utf-8")
            (stmts_dir / "fidelity_aapl.grants.json").write_text(example_src, encoding="utf-8")

            with PortfolioDB(db_path) as db:
                _seed_minimal_portfolio(db)
                ing = ingest_statements(stmts_dir, db, cfg=cfg, force_all=True)
                self.assertEqual(ing.files_ingested, 1)
                db.conn.execute(
                    """
                    INSERT OR REPLACE INTO market_prices (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
                    VALUES ('AAPL', '2026-11-10', 225.0, 225.0, '2026-11-10T00:00:00Z')
                    """
                )
                db.conn.commit()
                build_and_store_timeline(
                    db,
                    cfg,
                    start_date=date(2026, 10, 2),
                    today_date=today,
                    forecast_end_date=date(2028, 1, 7),
                )
                payload = build_report_payload(db, cfg, today)

            sg = payload["stock_grants"]
            self.assertTrue(sg["has_unvested"])
            self.assertEqual(sg["default_symbol"], "AAPL")
            self.assertEqual(len(sg["companies"]), 1)
            c0 = sg["companies"][0]
            self.assertEqual(c0["symbol"], "AAPL")
            self.assertEqual(c0["short_name"], "Apple")
            self.assertEqual(c0["award_type"], "RSU")
            self.assertEqual(c0["future_gross_shares"], 240.0)
            self.assertGreater(c0["live_price"], 0.0)
            self.assertEqual(payload["kpis"]["future_gsu_gross_shares"], 240.0)
            self.assertAlmostEqual(payload["kpis"]["future_gsu_gross_val"], c0["future_gross_val"], places=2)

            unv_buckets = [a for a in payload["retirement_sim_assets"] if a["id"] == "future_unvested_aapl"]
            self.assertEqual(len(unv_buckets), 1)
            self.assertTrue(unv_buckets[0]["is_unvested_goog"])
            self.assertFalse(unv_buckets[0]["is_goog_priced"])
            self.assertEqual(unv_buckets[0]["grant_symbol"], "AAPL")
            # Run simulator cleanly with AAPL unvested bucket included
            eff_inc = build_effective_settings(payload, {"futureGoogGrowthPct": 8.0, "simAnnualBurn": 0.0, "simIncludeUnvested": True})
            eff_exc = build_effective_settings(payload, {"futureGoogGrowthPct": 8.0, "simAnnualBurn": 0.0, "simIncludeUnvested": False})
            sim_inc = run_simulation(payload, eff_inc)
            sim_exc = run_simulation(payload, eff_exc)
            self.assertGreater(sim_inc["bars"][-1]["googVal"], 0.0)
            self.assertEqual(sim_exc["bars"][-1]["googVal"], 0.0)

            self._verify_html_nav_in_node(
                payload,
                expected_visible=True,
                expected_lbl="Apple Vesting",
                expected_sub="Releases & RSU Grants",
                expected_chev=False,
            )

    def test_n2_multi_company_and_custom_plugin_and_cli(self) -> None:
        """D-03/D-08/D-10: Two companies show `Stock Grants` picker; `Plugins/*.py` auto-loads; CLI `ingest-grants` works."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            db_path = tmp_p / "n2.sqlite"
            plugins_dir = tmp_p / "Plugins"
            stmts_dir = tmp_p / "Statements"
            plugins_dir.mkdir(parents=True)
            stmts_dir.mkdir(parents=True)

            # Custom user plugin inside simulated Project/Plugins/
            custom_plugin_code = """
import json
from ingest.framework import StatementPlugin
from ingest.models import ParsedGSUTranche, ParsedStatement

class CustomBrokerPlugin(StatementPlugin):
    plugin_id = "custom_broker_csv_marker"
    institution_name = "Custom Broker"
    supported_extensions = (".json",)

    def detect(self, ctx):
        if ctx.file_path.name.endswith(".customgrants.json"):
            return 0.99
        return 0.0

    def parse(self, ctx):
        return ParsedStatement(
            doc_type="CUSTOM_GRANTS",
            institution="Custom Broker",
            account_id="CUST_01",
            account_name=None,
            account_type=None,
            tax_status=None,
            display_order=10,
            period_start=None,
            as_of_date="2026-11-09",
            reported_total=None,
            gsu_tranches=[
                ParsedGSUTranche(
                    grant_id="NV-1",
                    grant_date="2025-05-01",
                    vest_date="2027-05-01",
                    orig_qty=100.0,
                    deus=0.0,
                    total_qty=100.0,
                    symbol="NVDA",
                    company_name="NVIDIA Corporation",
                    award_type="RSU",
                    account_id="CUST_01",
                )
            ],
        )
"""
            (plugins_dir / "custom_broker.py").write_text(custom_plugin_code, encoding="utf-8")
            registry = build_default_registry(plugins_dir=plugins_dir, allow_custom=True)
            self.assertIn("custom_broker_csv_marker", [p.plugin_id for p in registry.plugins])

            cfg = default_config()
            cfg["primary_equity_symbol"] = "GOOG"
            today = date(2026, 11, 10)
            with PortfolioDB(db_path) as db:
                _seed_minimal_portfolio(db)
                ingest_grant_payload_dict(
                    db,
                    cfg,
                    {
                        "institution": "Morgan Stanley",
                        "as_of_date": "2026-11-09",
                        "symbol": "GOOG",
                        "company_name": "Alphabet Inc.",
                        "award_type": "GSU",
                        "unvested_tranches": [
                            {
                                "grant_id": "G-9001",
                                "grant_date": "2025-03-01",
                                "vest_date": "2027-03-25",
                                "orig_qty": 80.0,
                                "deus": 0.0,
                                "total_qty": 80.0,
                            }
                        ],
                    },
                )
                ingest_grant_payload_dict(
                    db,
                    cfg,
                    json.loads((ROOT / "Examples" / "grants.example.json").read_text(encoding="utf-8")),
                )
                db.conn.execute(
                    """
                    INSERT OR REPLACE INTO market_prices (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
                    VALUES ('AAPL', '2026-11-10', 225.0, 225.0, '2026-11-10T00:00:00Z')
                    """
                )
                db.conn.commit()
                build_and_store_timeline(
                    db,
                    cfg,
                    start_date=date(2026, 10, 2),
                    today_date=today,
                    forecast_end_date=date(2028, 1, 7),
                )
                payload = build_report_payload(db, cfg, today)

            sg = payload["stock_grants"]
            self.assertTrue(sg["has_unvested"])
            self.assertEqual([c["symbol"] for c in sg["companies"]], ["GOOG", "AAPL"])
            bucket_ids = {a["id"] for a in payload["retirement_sim_assets"]}
            self.assertIn("future_unvested_goog", bucket_ids)
            self.assertIn("future_unvested_aapl", bucket_ids)
            self.assertEqual(payload["kpis"]["future_gsu_gross_shares"], 320.0)

            self._verify_html_nav_in_node(
                payload,
                expected_visible=True,
                expected_lbl="Stock Vesting",
                expected_chev=True,
                expected_popup_syms=["GOOG", "AAPL"],
            )

            # CLI `ingest-grants` + `--rebuild` smoke check
            out_html = tmp_p / "out.html"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "holdings.py"),
                    "ingest-grants",
                    str(ROOT / "Examples" / "grants.example.json"),
                    "--db",
                    str(db_path),
                    "--output",
                    str(out_html),
                    "--today",
                    "2026-11-10",
                    "--rebuild",
                ],
                env={**os.environ, "ADVISORBOT_ROOT": str(tmp_p)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertTrue(out_html.exists())
            self.assertIn("AAPL", proc.stdout)

    def test_http_handle_ingest_grants_validation_and_success(self) -> None:
        """D-10: `POST /api/grants/ingest` handler validates schema and ingests custom FI grants."""
        import advisorbot_server

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "http_grants.sqlite"
            cfg_path = Path(tmp) / "project.json"
            cfg = default_config()
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            with PortfolioDB(db_path) as db:
                _seed_minimal_portfolio(db)

            orig_db, orig_cfg = advisorbot_server.DEFAULT_DB_PATH, advisorbot_server.DEFAULT_CONFIG_PATH
            try:
                advisorbot_server.DEFAULT_DB_PATH = db_path
                advisorbot_server.DEFAULT_CONFIG_PATH = cfg_path
                bad = advisorbot_server.handle_ingest_grants({"symbol": "AAPL"}, db_path=db_path, cfg=cfg)
                self.assertFalse(bad["ok"])
                example_payload = json.loads((ROOT / "Examples" / "grants.example.json").read_text(encoding="utf-8"))
                good = advisorbot_server.handle_ingest_grants(example_payload, db_path=db_path, cfg=cfg)
                self.assertTrue(good["ok"], msg=str(good))
                self.assertEqual(good["symbols"], ["AAPL"])
                self.assertEqual(good["tranches_loaded"], 2)
                self.assertEqual(good["releases_loaded"], 1)
            finally:
                advisorbot_server.DEFAULT_DB_PATH = orig_db
                advisorbot_server.DEFAULT_CONFIG_PATH = orig_cfg

    def _verify_html_nav_in_node(
        self,
        payload: dict,
        expected_visible: bool,
        expected_lbl: str | None = None,
        expected_sub: str | None = None,
        expected_chev: bool = False,
        expected_popup_syms: list[str] | None = None,
    ) -> None:
        html = build_html_string(payload)
        start = html.index("<script>") + len("<script>")
        end = html.rindex("</script>")
        script_body = html[start:end]

        node_driver = """
const vm = require('vm');
const els = new Map();
function makeEl(id) {
  return {
    id,
    style: {},
    dataset: {},
    classList: {
      _set: new Set(),
      toggle(cls, on) { if (on === undefined) { if (this._set.has(cls)) this._set.delete(cls); else this._set.add(cls); } else if (on) this._set.add(cls); else this._set.delete(cls); },
      add(cls) { this._set.add(cls); },
      remove(cls) { this._set.delete(cls); },
      contains(cls) { return this._set.has(cls); },
    },
    children: [],
    textContent: '',
    innerHTML: '',
    value: '',
    appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    getBoundingClientRect() { return { left: 0, right: 200, top: 100, bottom: 140, width: 200, height: 40 }; },
    contains() { return false; },
    focus() {},
  };
}
const document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  createElement(tag) { return makeEl(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
const window = {
  location: { hash: '' },
  addEventListener() {},
  innerHeight: 900,
  innerWidth: 1400,
};
const history = { replaceState() {} };
const localStorage = { _m: {}, getItem(k) { return this._m[k] || null; }, setItem(k, v) { this._m[k] = String(v); } };
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, JSON, Map, Set, Boolean, isFinite, parseFloat, parseInt, AbortController,
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  document, window, history, localStorage, fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
});
const code = __SCRIPT__;
vm.runInContext(code, ctx);
const btn = document.getElementById('nav-btn-google');
const lbl = document.getElementById('nav-grants-lbl');
const sub = document.getElementById('nav-grants-sub');
const chev = document.getElementById('nav-grants-chevron');
const popup = document.getElementById('nav-grants-popup');
if (ctx.getGrantCompanies().length >= 2) {
  ctx.renderGrantsPopupList();
  ctx.selectGrantCompany(ctx.getGrantCompanies()[1].symbol);
}
const activeSym = vm.runInContext("activeGrantSymbol", ctx);
console.log(JSON.stringify({
  visible: btn.style.display !== 'none',
  lbl: lbl.textContent,
  sub: sub.textContent,
  chevVisible: chev.style.display !== 'none',
  popupHtml: popup.innerHTML,
  activeGrantSymbol: activeSym,
  yrTitle: document.getElementById('gstock-yr-title').textContent,
  kpiAvailLbl: document.getElementById('kpi-grant-avail-lbl').textContent,
  kpiFutureLbl: document.getElementById('kpi-future-lbl').textContent,
  kpiFutureVal: document.getElementById('kpi-future').textContent,
  kpiFuturePretax: document.getElementById('kpi-future-pretax').textContent,
}));
""".replace("__SCRIPT__", json.dumps(script_body))

        with tempfile.TemporaryDirectory() as tmp:
            js_file = Path(tmp) / "verify_nav.js"
            js_file.write_text(node_driver, encoding="utf-8")
            proc = subprocess.run(["node", str(js_file)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, msg=f"Node execution failed:\n{proc.stderr}\n{proc.stdout}")
            res = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertEqual(res["visible"], expected_visible)
        if expected_lbl is not None:
            self.assertEqual(res["lbl"], expected_lbl)
        if expected_sub is not None:
            self.assertEqual(res["sub"], expected_sub)
        self.assertEqual(res["chevVisible"], expected_chev)
        if expected_popup_syms:
            for sym in expected_popup_syms:
                self.assertIn(sym, res["popupHtml"])
            self.assertEqual(res["activeGrantSymbol"], expected_popup_syms[1])
            self.assertIn("Apple", res["yrTitle"])
            self.assertEqual(res["kpiAvailLbl"], f"Available {expected_popup_syms[1]}")
            self.assertIn(f"Future {expected_popup_syms[1]} Releases", res["kpiFutureLbl"])
            self.assertTrue(res["kpiFutureVal"].startswith("+"))
            self.assertIn("Est. after taxes:", res["kpiFuturePretax"])
        
        # Verify DOM placement of KPI cards in the generated HTML
        html_text = build_html_string(payload)
        # Portfolio screen should NOT contain company-specific grant KPI cards
        port_pane = html_text.split('<div id="screen-portfolio"')[1].split('<div id="screen-google"')[0]
        self.assertIn('id="kpi-total"', port_pane)
        self.assertIn('id="kpi-cov"', port_pane)
        self.assertNotIn('id="kpi-goog"', port_pane)
        self.assertNotIn('id="kpi-future"', port_pane)
        # Google / Stock Grants screen SHOULD contain relocated grant KPI cards
        goog_pane = html_text.split('<div id="screen-google"')[1].split('<div id="screen-retirement"')[0]
        self.assertIn('id="kpi-grant-avail-lbl"', goog_pane)
        self.assertIn('id="kpi-goog"', goog_pane)
        self.assertIn('id="kpi-future-lbl"', goog_pane)
        self.assertIn('id="kpi-future"', goog_pane)

    def test_cash_plan_withdrawals_do_not_consume_equity_tranches(self) -> None:
        """USD cash disbursements (plan_name='Cash') must not subtract dollars as equity shares."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cash_wdr.sqlite"
            cfg = default_config()
            cfg["primary_equity_symbol"] = "GOOG"
            today = date(2026, 11, 10)
            with PortfolioDB(db_path) as db:
                _seed_minimal_portfolio(db)
                db.upsert_account(
                    account_id="ACCT_SP",
                    institution="Morgan Stanley",
                    account_name="Stock Plan",
                    account_type="STOCK_PLAN",
                    tax_status="TAXABLE",
                    display_order=2,
                )
                db.upsert_security(
                    security_id="GOOG",
                    symbol_or_code="GOOG",
                    canonical_name="Alphabet Inc Class C",
                    asset_class="EQUITY",
                    risk_class="single_stock",
                    stack_bucket="goog",
                    is_scrapeable=True,
                    quote_symbol="GOOG",
                    valuation_model="LIVE_PRICE",
                )
                db.conn.execute(
                    """
                    INSERT OR REPLACE INTO position_snapshots
                        (account_id, security_id, as_of_date, quantity, stmt_price, stmt_market_value, cost_basis, yield_pct, details_json, source_hash)
                    VALUES ('ACCT_SP', 'GOOG', '2026-10-01', 80.0, 250.0, 20000.0, 14000.0, 0.0, '{}', 'seed_sp')
                    """
                )
                ingest_grant_payload_dict(
                    db,
                    cfg,
                    {
                        "institution": "Morgan Stanley",
                        "account_id": "ACCT_SP",
                        "symbol": "GOOG",
                        "company_name": "Alphabet Inc.",
                        "award_type": "GSU",
                        "releases_history": [
                            {
                                "order_number": "REL-1",
                                "grant_id": "G1",
                                "vest_date": "2024-06-15",
                                "release_price": 175.0,
                                "gross_shares": 150.0,
                                "net_shares": 100.0,
                            }
                        ],
                        "withdrawals": [
                            {
                                "order_number": "WDR-EQ",
                                "exec_date": "2025-07-01",
                                "plan_name": "GSU Class C",
                                "tx_type": "Sale",
                                "price": 200.0,
                                "quantity": -20.0,
                                "net_amount": 4000.0,
                            },
                            {
                                "order_number": "WDR-CASH",
                                "exec_date": "2026-03-16",
                                "plan_name": "Cash",
                                "tx_type": "Sale",
                                "price": 1.0,
                                "quantity": -50000.0,
                                "net_amount": 50000.0,
                            },
                        ],
                    },
                )
                build_and_store_timeline(
                    db,
                    cfg,
                    start_date=date(2026, 10, 2),
                    today_date=today,
                    forecast_end_date=today + timedelta(days=30),
                )
                payload = build_report_payload(db, cfg, today)

            sp_buckets = [
                a for a in payload["retirement_sim_assets"]
                if a["id"] in ("ms_goog_high_basis", "ms_goog_low_basis")
            ]
            total_sp_shares = sum(float(a["shares"] or 0.0) for a in sp_buckets)
            total_tranche_val = sum(sum(float(t["current_value"]) for t in (a.get("tranches") or [])) for a in sp_buckets)
            expected_vested = sum(
                float(a["current_value"] or 0.0)
                for a in payload["retirement_sim_assets"]
                if not a.get("is_unvested_goog")
            )
            self.assertAlmostEqual(total_sp_shares, 80.0, places=3)
            self.assertAlmostEqual(total_tranche_val, 20000.0, places=2)
            det = run_simulation(payload, build_effective_settings(payload, {"simAnnualBurn": 0.0}))
            self.assertAlmostEqual(det["bars"][0]["totalVal"], expected_vested, places=2)


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
