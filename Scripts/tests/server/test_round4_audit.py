#!/usr/bin/env python3
"""
Round 4 security, Privacy Shield, Demo Mode, and portability regression tests.

Covers findings SA2-01 through SA2-24 and confirmed architectural decisions DP-01 through
DP-05 from `.dev/SecurityAudit.html` section 4 and section 5.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.support import env, pipeline as pipeline_support, roots

pytestmark = [pytest.mark.portfolio("p2_equity_comp_rsu"), pytest.mark.security]


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA2-01")
@pytest.mark.regression("DP-01")
def test_unauthed_landing_and_reconnect__disclose_no_token_and_mint_no_cookie(sidecar):
    """
    Unauthenticated landing HTML contains no session token and `/api/auth/reconnect` refuses unauthed callers.

    Exercises: SA2-01 and DP-01. Prevents sandboxed `Origin: null` iframes and local
    unprivileged processes from reading `SESSION_TOKEN` or minting a session cookie.
    """
    token = sidecar.authenticate()
    unauthed_html = sidecar.get("/", headers={"Accept": "text/html"}, authenticated=False)
    assert unauthed_html.status == 403
    assert token not in unauthed_html.text
    assert "/?token=" not in unauthed_html.text

    rec = sidecar.post(
        "/api/auth/reconnect",
        body={},
        headers={"Origin": sidecar.base, "Sec-Fetch-Site": "same-origin", "X-Requested-With": "AdvisorBot"},
        authenticated=False,
    )
    assert rec.status == 403
    assert not rec.headers.get("Set-Cookie")


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA2-15")
@pytest.mark.regression("DP-01")
def test_cookie_authenticated_mutations__reject_cross_port_and_plain_text_csrf(sidecar):
    """
    Cookie-authenticated POST and DELETE routes reject cross-port Origins and form-encoded CSRF.

    Exercises: SA2-15. Browsers share `SameSite=Strict` cookies across localhost ports, so
    cookie-authenticated mutations must verify same-port `Origin`, `Sec-Fetch-Site`, and
    `application/json` / `X-Requested-With: AdvisorBot`.
    """
    import advisorbot_server

    token = sidecar.authenticate()
    bootstrap = sidecar.get(f"/?token={token}", authenticated=False)
    cookie_hdr = bootstrap.headers["Set-Cookie"].split(";", 1)[0]

    # Cross-port localhost Origin must fail CSRF check
    bad_port = sidecar.request(
        "POST",
        "/api/preferences/global",
        body={"holding_preferences": {}},
        headers={
            "Cookie": cookie_hdr,
            "Origin": "http://127.0.0.1:9999",
            "Sec-Fetch-Site": "same-site",
            "X-Requested-With": "AdvisorBot",
        },
        authenticated=False,
    )
    assert bad_port.status == 403

    # text/plain simple request without X-Requested-With must fail CSRF check
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{sidecar.base}/api/preferences/global",
        data=b'{"holding_preferences": {}}',
        headers={
            "Host": f"127.0.0.1:{sidecar.port}",
            "Cookie": cookie_hdr,
            "Content-Type": "text/plain",
            "Origin": sidecar.base,
            "Sec-Fetch-Site": "same-origin",
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 403

    # Legitimate same-origin UI request with X-Requested-With succeeds
    good = sidecar.request(
        "POST",
        "/api/preferences/global",
        body={"friendly_names": {}},
        headers={
            "Cookie": cookie_hdr,
            "Origin": sidecar.base,
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "AdvisorBot",
        },
        authenticated=False,
    )
    assert good.status == 200


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA2-02")
def test_snapshots_and_portfolio_assets__require_auth_and_respect_demo_mode(root, sidecar):
    """
    Scenario snapshots and portfolio badges require auth and never serve raw assets in Demo Mode.

    Exercises: SA2-02. Unauthenticated requests get 403; Demo Mode redirects snapshot reads
    to `demo_<id>.png` (or 404 if absent) and custom badges to `default_badge.svg`.
    """
    snap_dir = root / "Portfolios" / "p2_equity_comp_rsu" / "Scenarios" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "001_real.png").write_bytes(b"REAL_PNG_BYTES")
    (snap_dir / "demo_001_real.png").write_bytes(b"DEMO_PNG_BYTES")

    assets_dir = root / "Portfolios" / "p2_equity_comp_rsu" / "Assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "custom_monogram.svg").write_text("<svg><text>REAL_MONOGRAM</text></svg>", encoding="utf-8")

    # Unauthenticated access must return 403
    unauth_snap = sidecar.get("/Scenarios/snapshots/001_real.png", authenticated=False)
    assert unauth_snap.status == 403

    sidecar.authenticate()
    live_snap = sidecar.get("/Scenarios/snapshots/001_real.png")
    assert live_snap.status == 200
    assert live_snap.body == b"REAL_PNG_BYTES"

    # Enable Demo Mode and verify real snapshot & custom badge are never served
    toggle = sidecar.post("/api/privacy", body={"demo_mode": {"enabled": True}})
    assert toggle.status == 200
    try:
        demo_snap = sidecar.get("/Scenarios/snapshots/001_real.png")
        assert demo_snap.status == 200
        assert demo_snap.body == b"DEMO_PNG_BYTES"

        badge_url = "/" + "/".join(("Portfolios", "p2_equity_comp_rsu", "Assets", "custom_monogram.svg"))
        demo_badge = sidecar.get(badge_url)
        assert demo_badge.status == 200
        assert "REAL_MONOGRAM" not in demo_badge.text
    finally:
        sidecar.post("/api/privacy", body={"demo_mode": {"enabled": False}})


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA2-03")
@pytest.mark.regression("SA2-04")
@pytest.mark.regression("SA2-07")
def test_translator__aliases_dynamic_investor_keys_sma_equities_and_stateless_composites(root):
    """
    Dynamic investor dict keys, individual equities, and Turn 2+ composite IDs round-trip cleanly.

    Exercises: SA2-03 (camelCase/digit-joined investor keys + DOB leak check), SA2-04 (SQLite
    `EQUITY` holdings registered as `STOCK_N`), and SA2-07 (cold-start `_comp_alias_to_real`
    pre-seeding + `from_id`/`to_id` composite keys).
    """
    from privacy.registry import sync_privacy_registry
    from privacy.translator import build_translator

    pipeline_support.build_payload("p2_equity_comp_rsu")
    pcfg = sync_privacy_registry("p2_equity_comp_rsu")
    tr1 = build_translator(pcfg)

    sample = {
        "settings": {
            "ssnAmaraClaimAge": 68,
            "ssnEliasClaimAge": 70,
            "simScheduledActions": [
                {"type": "transfer", "from_id": "harbor_vntg_HB_7101", "to_id": "voo_HB_7101", "amount": 25000}
            ],
        },
        "kpis": {
            "success_to_inv_amara85_pct": 96.5,
            "inv_amara85_month": 312,
        },
    }
    wire = tr1.to_alias(sample)
    wire_json = json.dumps(wire)
    assert "Amara" not in wire_json
    assert "Elias" not in wire_json
    assert "inv_amara" not in wire_json
    assert "HB_7101" not in wire_json
    assert tr1.find_leaked_terms(wire) == []

    # SA2-03: deliberate unmasked camelCase or DOB leak must be caught by find_leaked_terms
    assert tr1.find_leaked_terms({"ssnAmaraClaimAge": 67}) != []

    # SA2-07: brand-new translator instance (simulating Turn 2 HTTP request / MCP call)
    # must restore composite IDs and dynamic investor keys losslessly without Turn 1 cache!
    tr2 = build_translator(pcfg)
    restored = tr2.from_alias(wire)
    assert restored["settings"].get("ssnAmaraOkaforClaimAge", restored["settings"].get("ssnAmaraClaimAge")) == 68
    assert restored["settings"].get("ssnEliasLindqvistClaimAge", restored["settings"].get("ssnEliasClaimAge")) == 70
    act = restored["settings"]["simScheduledActions"][0]
    assert act["from_id"] == "harbor_vntg_HB_7101"
    assert act["to_id"] == "voo_HB_7101"
    assert restored["kpis"]["success_to_inv_amara85_pct"] == 96.5
    assert restored["kpis"]["inv_amara85_month"] == 312


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA2-08")
@pytest.mark.regression("DP-03")
def test_mode_c_scaling__preserves_share_price_identity_and_statutory_prose():
    """
    Mode C scales shares while keeping per-share price and statutory prose `$` constants intact.

    Exercises: SA2-08 and DP-03. `(k * shares) * price == k * value` holds, non-monetary
    keys (`mcSingleStockCorr`, `yearsOut`, `pct_of_source` amounts) stay unscaled, and
    `from_alias_text` does not multiply statutory `$250,000` thresholds by `1/k`.
    """
    from privacy.registry import default_privacy_config
    from privacy.translator import build_translator

    cfg = default_privacy_config()
    cfg["ai_shield"]["mode"] = "identity_and_values"
    cfg["ai_shield"]["scale_factor"] = 0.25
    cfg["entities"] = [
        {
            "token": "INVESTOR_1",
            "category": "investor",
            "canonical": "Amara Okafor",
            "synonyms": ["Amara Okafor", "Amara"],
            "enabled": True,
        }
    ]
    tr = build_translator(cfg)

    holding = {
        "shares": 100.0,
        "price": 200.0,
        "current_value": 20000.0,
        "quantity_str": "100 sh",
        "mcSingleStockCorr": 0.52,
        "yearsOut": 30,
        "scheduled": {"amount_mode": "pct_of_source", "amount": 50.0},
    }
    wire = tr.to_alias(holding)
    assert wire["shares"] == 25.0
    assert wire["price"] == 200.0
    assert wire["current_value"] == 5000.0
    assert wire["shares"] * wire["price"] == wire["current_value"]
    assert "25" in wire["quantity_str"]
    assert wire["mcSingleStockCorr"] == 0.52
    assert wire["yearsOut"] == 30
    assert wire["scheduled"]["amount"] == 50.0

    llm_reply = "For INVESTOR_1, the 3.8% NIIT statutory threshold is $250,000 married filing jointly."
    ui_reply = tr.from_alias_text(llm_reply)
    assert "Amara Okafor" in ui_reply
    assert "$250,000" in ui_reply


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA2-06")
def test_mcp_server__enforces_wire_leak_gate_and_preserves_enabled_on_sync(root):
    """
    MCP tool invocation sanitizes exceptions and `sync_privacy_registry` preserves `mcp_server.enabled`.

    Exercises: SA2-06. Enabling MCP in `privacy_registry.json` survives `sync_privacy_registry`,
    and unexpected exceptions scrub local `/Users/<name>/` paths via `_sanitize_error_for_llm`.
    """
    import mcp_server
    from privacy.registry import load_privacy_config, save_privacy_config, sync_privacy_registry

    pcfg = load_privacy_config("p2_equity_comp_rsu")
    assert "mask_in_report" not in pcfg
    pcfg["mcp_server"] = {"enabled": True}
    save_privacy_config(pcfg, "p2_equity_comp_rsu")

    resynced = sync_privacy_registry("p2_equity_comp_rsu")
    assert resynced["mcp_server"]["enabled"] is True
    assert "mask_in_report" not in resynced

    fake_path = "/".join(("", "Users", "secret_owner", "Workspaces", "private", "db.sqlite"))
    with patch("llm_tools._exec_shielded_tool", side_effect=RuntimeError(f"boom at {fake_path}")):
        out_str = mcp_server._invoke_mcp_tool("holdings__get_overview", {})
    assert "secret_owner" not in out_str


@pytest.mark.feature("demo_mode")
@pytest.mark.regression("SA2-10")
@pytest.mark.regression("SA2-11")
@pytest.mark.regression("SA2-12")
@pytest.mark.regression("SA2-13")
@pytest.mark.regression("DP-02")
def test_demo_mode__scales_trajectory_bars_projects_gets_and_locks_mutations(sidecar):
    """
    Demo Mode scales trajectory `totalVal`/`assetVals`, projects GET routes, and blocks writes & chat saves.

    Exercises: SA2-10 (`DemoTranslator` camelCase trajectory keys + `assetVals`), SA2-11
    (7 GET routes projected/redacted), SA2-12 & DP-02 (write locks + read-only Demo AI Chat),
    and SA2-13 (`source_detail` inline scaling + `external_file` basename mapping).
    """
    import llm_tools
    from privacy.demo_projector import DemoTranslator
    from privacy.registry import load_privacy_config

    pcfg = load_privacy_config("p2_equity_comp_rsu")
    pcfg["demo_mode"]["enabled"] = True
    pcfg["demo_mode"]["scale_factor"] = 0.25
    dtr = DemoTranslator(pcfg)

    traj = {
        "bars": [
            {
                "totalVal": 4000000.0,
                "totalRealVal": 3600000.0,
                "googVal": 1200000.0,
                "nonGoogVal": 2800000.0,
                "assetVals": {"stock_vntg_high_basis": 1200000.0, "voo_HB_7101": 2800000.0},
            }
        ],
        "source_detail": "($100,000 -> $400,000, +12.5%)",
        "external_file": "/" + "Users/amara/Docs/amara_ssa.xml",
    }
    proj_traj = dtr.project_value(traj)
    bar0 = proj_traj["bars"][0]
    assert bar0["totalVal"] < 2000000.0
    assert bar0["totalRealVal"] < 2000000.0
    assert sum(bar0["assetVals"].values()) < 2000000.0
    assert "$400,000" not in proj_traj["source_detail"]
    assert "amara" not in proj_traj["external_file"].lower()

    # Live HTTP verification under Demo Mode
    sidecar.authenticate()
    assert sidecar.post("/api/privacy", body={"demo_mode": {"enabled": True, "scale_factor": 0.25}}).status == 200
    try:
        priv = sidecar.get("/api/privacy").json()["privacy"]
        for ent in priv.get("entities", []):
            assert "Okafor" not in str(ent.get("canonical"))
            assert ent.get("synonyms") == []

        p_list = sidecar.get("/api/portfolios").json()
        assert all("Okafor" not in str(p.get("name")) for p in p_list.get("portfolios", []))

        proj_res = sidecar.get("/api/project").json()
        assert "Okafor" not in json.dumps(proj_res)

        ssn_res = sidecar.get("/api/ssn/files").json()
        assert ssn_res.get("files") == []

        prefs_res = sidecar.get("/api/preferences/global").json()
        assert "Okafor" not in json.dumps(prefs_res)

        quote_res = sidecar.get("/api/quote?symbol=VNTG").json()
        assert "VNTG" not in str(quote_res.get("resolved_symbol", ""))
        assert "Vantage" not in str(quote_res.get("name", ""))

        # Mutating endpoints blocked in Demo Mode
        for blocked_path in ("/api/grants/ingest", "/api/portfolio/generate_badge", "/api/models/protect"):
            resp = sidecar.post(blocked_path, body={})
            assert resp.status == 403

        # DP-02: AI Chat tool `manage_saves(create)` blocked in Demo Mode while read-only tools work
        _, blocked_tool = llm_tools._exec_shielded_tool(
            "holdings__manage_saves",
            {"action": "create", "name": "Contaminated Save"},
            None,
        )
        assert blocked_tool.get("ok") is False
        assert blocked_tool.get("demo_blocked") is True
    finally:
        sidecar.post("/api/privacy", body={"demo_mode": {"enabled": False}})


@pytest.mark.feature("render")
@pytest.mark.regression("SA2-14")
@pytest.mark.regression("SA2-22")
def test_frontend_tooltip_sanitizer_and_single_investor_guard(tmp_path):
    """
    `sanitizeTooltipHtml` strips XSS vectors in `[data-tip]`, and 1-investor portfolios set `INV1 = null`.

    Exercises: SA2-14 (stored DOM XSS via `[data-tip]` HTML entity decoding) and SA2-22
    (single-investor households must not clone Investor 0 into a phantom `INV1`).
    """
    roots.assemble_root(tmp_path, ["p1_index_saver"])
    with env.pointed_at(tmp_path):
        pipeline_support.clear_payload_cache()
        html = pipeline_support.rendered_html("p1_index_saver")
        pipeline_support.clear_payload_cache()

    html_file = tmp_path / "index.html"
    html_file.write_text(html, encoding="utf-8")

    node_check = r"""
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(process.argv[1], "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]);
const code = scripts[scripts.length - 1];

const stub = new Proxy(function(){}, {
  get: (t, p) => (p === Symbol.toPrimitive ? (() => 0) : (p === "length" ? 0 : (p === "value" ? "" : (p === "style" ? {} : (p === "dataset" ? {} : (p === "classList" ? { add(){}, remove(){}, toggle(){}, contains(){ return false; } } : stub)))))),
  apply: () => stub,
  set: () => true,
});
const ctx = {
  document: { getElementById: () => stub, querySelector: () => stub, querySelectorAll: () => [], createElement: (tag) => {
    if (String(tag).toLowerCase() === "template") {
      return { innerHTML: "", content: { childNodes: [] } };
    }
    return stub;
  }, addEventListener: () => {}, body: stub, documentElement: stub },
  navigator: { userAgent: "node" }, console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  requestAnimationFrame: () => 0,
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
  URL, Promise, structuredClone, AbortController, TextEncoder, TextDecoder,
  location: { protocol: "http:", hash: "", href: "http://127.0.0.1:8765/", search: "" },
  addEventListener: () => {}, removeEventListener: () => {},
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  getComputedStyle: () => stub, devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800,
  history: { replaceState: () => {} },
};
ctx.globalThis = ctx; ctx.window = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(code, ctx);

const dirty = '<b>Safe</b><img src=x onerror="alert(1)"><script>evil()</script><svg onload="x()"><span style="color:#fff;background:url(javascript:alert(2))" onclick="bad()">ok</span>';
const clean = ctx.window.sanitizeTooltipHtml(dirty);
const invCheck = vm.runInContext('({ hasSecond: HAS_SECOND_INVESTOR, inv1IsNull: INV1 === null, savesBase: SAVES_API_BASE })', ctx);
console.log(JSON.stringify({ clean, invCheck }));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_check, str(html_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    clean = res["clean"].lower()
    assert "<img" not in clean
    assert "onerror" not in clean
    assert "<script" not in clean
    assert "<svg" not in clean
    assert "onclick" not in clean
    assert "javascript:" not in clean
    assert res["invCheck"]["hasSecond"] is False
    assert res["invCheck"]["inv1IsNull"] is True
    assert res["invCheck"]["savesBase"] == ""


@pytest.mark.feature("scenarios")
@pytest.mark.regression("SA2-16")
@pytest.mark.regression("SA2-17")
def test_protected_scenarios_and_markdown_link_schemes__are_enforced_server_side(sidecar):
    """
    Protected scenarios reject unconfirmed DELETE / name-matched overwrite, and markdown strips `javascript:` URIs.

    Exercises: SA2-16 (`DELETE /api/models` and bogus-id + protected-name `POST /api/models`
    must return 409 without `confirm_protected`) and SA2-17 (`_render_markdown_html` link
    scheme allowlist).
    """
    import advisorbot_server

    html_md = advisorbot_server._render_markdown_html(
        "[bad](javascript:alert(1)) [data](data:text/html,<script>x</script>) [good](https://example.com)"
    )
    assert "javascript:" not in html_md.lower()
    assert "data:text/html" not in html_md.lower()
    assert 'href="https://example.com"' in html_md

    sidecar.authenticate()
    created = sidecar.post(
        "/api/models",
        body={"name": "Guarded Plan", "settings": {"simAnnualBurn": 90000}},
    ).json()
    assert created.get("ok") is True
    mid = created.get("id") or (created.get("entry") or {}).get("id") or created.get("active_id")
    assert mid

    assert sidecar.post("/api/models/protect", body={"id": mid, "is_protected": True}).status == 200

    # Unconfirmed DELETE must fail with 409
    del_unconfirmed = sidecar.request("DELETE", f"/api/models?id={mid}")
    assert del_unconfirmed.status == 409
    assert del_unconfirmed.json().get("protected") is True

    # Bogus id + protected scenario's name without confirm_protected must fail with 409
    bogus_overwrite = sidecar.post(
        "/api/models",
        body={"id": "999", "name": "Guarded Plan", "settings": {"simAnnualBurn": 95000}},
    )
    assert bogus_overwrite.status == 409
    assert bogus_overwrite.json().get("protected") is True

    # Confirmed DELETE succeeds
    del_confirmed = sidecar.request("DELETE", f"/api/models?id={mid}&confirm_protected=1")
    assert del_confirmed.status == 200


@pytest.mark.feature("empty_state")
@pytest.mark.regression("SA2-21")
@pytest.mark.regression("DP-04")
def test_incomplete_portfolio__can_be_saved_and_edited_but_cannot_become_active(sidecar):
    """
    Draft portfolios without required simulation fields report `is_complete: False` and block activation.

    Exercises: DP-04 and SA2-21. `default_config()` makes zero silent tax/investor/burn
    assumptions; incomplete portfolios can be created, listed, and edited, but cannot be
    selected as the active simulation portfolio until validated.
    """
    sidecar.authenticate()
    created = sidecar.post("/api/portfolios", body={"name": "Draft Household", "activate": True}).json()
    assert created["ok"] is True
    draft_id = created["portfolio_id"]
    assert created["is_complete"] is False
    assert created["active_portfolio_id"] != draft_id

    listed = sidecar.get("/api/portfolios").json()["portfolios"]
    draft_row = next(p for p in listed if p["id"] == draft_id)
    assert draft_row["is_complete"] is False
    assert len(draft_row["missing_fields"]) >= 3

    # Selecting incomplete portfolio must fail
    sel_fail = sidecar.post("/api/portfolios/select", body={"portfolio_id": draft_id})
    assert sel_fail.status in (400, 422)
    assert sel_fail.json().get("incomplete") is True

    # Saving complete required fields flips is_complete -> True and unlocks activation
    upd = sidecar.post(
        "/api/project",
        body={
            "portfolio_id": draft_id,
            "household_name": "Draft Household",
            "reference_annual_burn": 85000,
            "investors": [
                {"id": "inv_1", "name": "Jordan Draft", "birth_date": "1982-07-15", "milestone_age": 85, "is_rmd_owner": True}
            ],
            "tax_rates": {"state_code": "WA", "state_rate": 0.0, "ltcg_top": 0.238, "ordinary_top": 0.408},
            "rebuild": False,
        },
    ).json()
    assert upd["ok"] is True
    assert upd["validation"]["valid"] is True

    sel_ok = sidecar.post("/api/portfolios/select", body={"portfolio_id": draft_id, "rebuild": False})
    assert sel_ok.status == 200
    assert sel_ok.json()["active_portfolio_id"] == draft_id


@pytest.mark.feature("social_security")
@pytest.mark.portfolio("p3_retiree_annuities")
@pytest.mark.regression("LL-08")
def test_ssn_model__requires_xml_statement_and_ignores_legacy_salary_summary_fields(tmp_path):
    """
    Social Security PIA is `$0` without an uploaded `ssa.gov` XML statement and resolves `external_file` XMLs.

    Exercises: XML-only Social Security policy. Legacy `years_worked` + `average_salary`
    fields must never synthesize flat nominal earnings, whereas `external_file` XML
    statements (such as `p3_retiree_annuities`) load exact year-by-year covered earnings.
    """
    from exporters.view_model import _build_ssn_model

    no_xml_cfg = {
        "investors": [
            {
                "id": "inv_no_xml",
                "name": "Alex NoXml",
                "birth_date": "1974-05-10",
                "milestone_age": 85,
                "is_rmd_owner": True,
                "ssn": {
                    "fra_age": 67,
                    "default_claim_age": 67,
                    "years_worked": 35,
                    "average_salary": 180000,
                    "external_file": "",
                    "annual_earnings": {},
                },
            }
        ],
        "tax_rates": {"state_code": "WA", "state_rate": 0.0, "ltcg_top": 0.238, "ordinary_top": 0.37},
    }
    no_xml_model = _build_ssn_model(no_xml_cfg)
    prof = no_xml_model["investors"][0]
    assert prof["has_statement"] is False
    assert prof["work_years"] == 0
    assert prof["pia_monthly_2026"] == 0.0
    assert prof["pia_annual_2026"] == 0.0
    assert "No ssa.gov XML statement" in prof["work_span_label"]

    # Meanwhile `p3_retiree_annuities` (`emit_xml: true`, empty `annual_earnings` in project.json)
    # loads its real `Statements/ssa/*_ssa_statement.xml` files via `external_file`.
    roots.assemble_root(tmp_path, ["p3_retiree_annuities"])
    with env.pointed_at(tmp_path):
        pipeline_support.clear_payload_cache()
        p3_payload = pipeline_support.build_payload("p3_retiree_annuities")
        pipeline_support.clear_payload_cache()
    p3_invs = p3_payload["ssn_model"]["investors"]
    assert len(p3_invs) == 2
    assert all(inv["has_statement"] is True for inv in p3_invs)
    assert all(inv["pia_monthly_2026"] > 1000.0 for inv in p3_invs)


@pytest.mark.feature("privacy_shield")
def test_demo_chat_response__preserves_configured_shield_mode():
    """Demo Mode chat responses do not report the internal Mode C scaling override as `privacy_shield`."""
    import llm_tools
    from privacy.registry import default_privacy_config

    demo_cfg = default_privacy_config()
    demo_cfg["ai_shield"]["mode"] = "identity_aliased"
    demo_cfg["demo_mode"] = {"enabled": True, "scale_factor": 0.25, "demo_seed": "demo-1234"}

    raw_res = {
        "ok": True,
        "engine": "gemini",
        "privacy_shield": "identity_and_values",
        "reply": "Projected balance is $500,000.",
        "tool_calls": [],
        "ui_events": [],
    }
    projected = llm_tools._project_demo_chat_response(raw_res, demo_cfg, already_value_scaled=True)
    assert projected["demo_mode"] is True
    assert projected["privacy_shield"] == "identity_aliased"


@pytest.mark.feature("privacy_shield")
def test_demo_mode_exit__succeeds_idempotently_from_demo_scoped_tab(sidecar):
    """Exiting Demo Mode from a Demo-scoped browser tab succeeds even if Demo Mode already flipped off."""
    import re

    scope_re = re.compile(r'<meta name="advisorbot-portfolio" content="([0-9a-f]+)">')
    sidecar.authenticate()
    assert sidecar.post("/api/privacy", body={"demo_mode": {"enabled": True}}).status == 200
    try:
        demo_scope = scope_re.search(sidecar.get("/").text)[1]
        # First exit turns Demo Mode off on disk
        first_off = sidecar.post(
            "/api/privacy",
            body={"demo_mode": {"enabled": False}},
            headers={"X-AdvisorBot-Portfolio": demo_scope},
        )
        assert first_off.status == 200
        # Second exit from another/unreloaded Demo tab is idempotent (200), not 409
        second_off = sidecar.post(
            "/api/privacy",
            body={"demo_mode": {"enabled": False}},
            headers={"X-AdvisorBot-Portfolio": demo_scope},
        )
        assert second_off.status == 200
        # Any non-toggle privacy mutation carrying the stale Demo scope is still 409-blocked
        stale_edit = sidecar.post(
            "/api/privacy",
            body={"ai_shield": {"mode": "off"}},
            headers={"X-AdvisorBot-Portfolio": demo_scope},
        )
        assert stale_edit.status == 409
    finally:
        sidecar.post("/api/privacy", body={"demo_mode": {"enabled": False}})



