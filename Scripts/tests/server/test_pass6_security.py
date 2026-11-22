#!/usr/bin/env python3
"""
Pass 6 security, Privacy Shield, Demo Mode, server scope, and portability regression tests.

Covers findings SA5-01 through SA5-10 plus DA4-08b, DA4-11, and DA4-13.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile

import pytest

from tests.support import env, pipeline as pipeline_support, registry as _test_registry, roots

_PASS6_FINDINGS = frozenset(
    {
        *(f"SA5-{n:02d}" for n in range(1, 11)),
        "DA4-08b",
        "DA4-11",
        "DA4-13",
    }
)
if not _PASS6_FINDINGS.issubset(_test_registry.KNOWN_FINDINGS):
    _test_registry.KNOWN_FINDINGS = _test_registry.KNOWN_FINDINGS | _PASS6_FINDINGS

pytestmark = [pytest.mark.portfolio("p2_equity_comp_rsu"), pytest.mark.security]


@pytest.mark.feature("demo_mode")
@pytest.mark.regression("SA5-01")
@pytest.mark.regression("SA5-07")
def test_demo_projector__preserves_statutory_bend_points_and_weights_while_scaling_drawdown_usd(root):
    """Statutory SSA bend points and fractional constituent weights stay unscaled while new USD keys scale by k."""
    from privacy.demo_projector import DemoTranslator
    from privacy.registry import load_privacy_config

    pcfg = load_privacy_config("p2_equity_comp_rsu", auto_sync=True)
    pcfg["demo_mode"]["enabled"] = True
    pcfg["demo_mode"]["scale_factor"] = 0.25
    dtr = DemoTranslator(pcfg)

    sample = {
        "ssn_calc": {
            "bend1": 1286,
            "bend2": 7749,
            "pia_monthly_2026": 3600.0,
        },
        "constituent_forecasts": {
            "HB-7101:TICKER:VNTG": {"weight": 0.6, "growth_pct": 11.5},
            "HB-7101:TICKER:VOO": {"weight": 0.4, "growth_pct": 8.5},
        },
        "drawdown_event": {
            "pooledNet": 40000.0,
            "shortfall": 8000.0,
            "requested": 100000.0,
            "funded": 92000.0,
            "tax_paid": 12000.0,
            "prior_dec31": 800000.0,
        },
    }
    projected = dtr.project_obj(sample)
    assert projected["ssn_calc"]["bend1"] == 1286
    assert projected["ssn_calc"]["bend2"] == 7749
    assert projected["ssn_calc"]["pia_monthly_2026"] == pytest.approx(900.0)

    weights = [entry["weight"] for entry in projected["constituent_forecasts"].values()]
    assert sorted(weights) == pytest.approx([0.4, 0.6])
    assert sum(weights) == pytest.approx(1.0)

    ev = projected["drawdown_event"]
    assert ev["pooledNet"] == pytest.approx(10000.0)
    assert ev["shortfall"] == pytest.approx(2000.0)
    assert ev["requested"] == pytest.approx(25000.0)
    assert ev["funded"] == pytest.approx(23000.0)
    assert ev["tax_paid"] == pytest.approx(3000.0)
    assert ev["prior_dec31"] == pytest.approx(200000.0)


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA5-06")
@pytest.mark.regression("SA5-08")
def test_demo_and_shield_translators__alias_fee_halflife_and_principal_override_keys(root):
    """Composite keys in fee/half-life/principal dicts alias cleanly and Mode C leaves non-monetary D7 fields unscaled."""
    from privacy.demo_projector import DemoTranslator
    from privacy.registry import sync_privacy_registry
    from privacy.translator import build_translator

    pipeline_support.build_payload("p2_equity_comp_rsu")
    pcfg = sync_privacy_registry("p2_equity_comp_rsu")
    pcfg["demo_mode"]["enabled"] = True
    pcfg["demo_mode"]["scale_factor"] = 0.25
    pcfg["ai_shield"]["mode"] = "identity_and_values"
    pcfg["ai_shield"]["scale_factor"] = 0.25

    dtr = DemoTranslator(pcfg)
    demo_in = {
        "settings": {
            "holdingFeePct": {"HB-7101:TICKER:VNTG": 0.45, "VNTG": 0.15},
            "holdingHalfLife": {"HB-7101:TICKER:VNTG": 7, "VNTG": 5},
            "holdingHalfLives": {"HB-7101:TICKER:VNTG": 7},
        },
        "principal_overrides": {
            "HB-7101:TICKER:VNTG": {"principal": 200000.0, "inception_date": "2022-06-15"},
        },
    }
    demo_out = dtr.project_obj(demo_in)
    demo_blob = json.dumps(demo_out)
    assert "HB-7101" not in demo_blob
    assert "VNTG" not in demo_blob
    assert sorted(demo_out["settings"]["holdingFeePct"].values()) == pytest.approx([0.15, 0.45])
    assert sorted(demo_out["settings"]["holdingHalfLife"].values()) == [5, 7]
    po_entry = next(iter(demo_out["principal_overrides"].values()))
    assert po_entry["principal"] == pytest.approx(50000.0)
    assert po_entry["inception_date"] == "2022-06-15"

    tr1 = build_translator(pcfg)
    shield_in = {
        "holdingFeePct": {"HB-7101:TICKER:VNTG": 0.45},
        "holdingHalfLife": {"HB-7101:TICKER:VNTG": 8},
        "holdingHalfLives": {"HB-7101:TICKER:VNTG": 8},
        "d7_curve": {
            "half_life": 5,
            "holding_half_life": 6,
            "selected_tier": 2,
            "source_tier": 1,
            "dest_tier": 3,
            "tiers": [14.0, 8.5, 6.5],
            "yearly_rates": [14.0, 12.1, 10.6],
            "forward_rates": [9.0, 8.5],
            "active_tier": 1,
            "fallback_tiers": [2, 3],
            "current_value": 400000.0,
        },
        "prose": "Set half_life=5 and selected_tier=2 with burn=100000",
    }
    wire = tr1.to_alias(shield_in)
    wire_blob = json.dumps(wire)
    assert "HB-7101" not in wire_blob
    assert "VNTG" not in wire_blob
    assert next(iter(wire["holdingFeePct"].values())) == pytest.approx(0.45)
    assert next(iter(wire["holdingHalfLife"].values())) == 8
    assert wire["d7_curve"]["half_life"] == 5
    assert wire["d7_curve"]["holding_half_life"] == 6
    assert wire["d7_curve"]["selected_tier"] == 2
    assert wire["d7_curve"]["source_tier"] == 1
    assert wire["d7_curve"]["dest_tier"] == 3
    assert wire["d7_curve"]["tiers"] == pytest.approx([14.0, 8.5, 6.5])
    assert wire["d7_curve"]["yearly_rates"] == pytest.approx([14.0, 12.1, 10.6])
    assert wire["d7_curve"]["forward_rates"] == pytest.approx([9.0, 8.5])
    assert wire["d7_curve"]["active_tier"] == 1
    assert wire["d7_curve"]["fallback_tiers"] == [2, 3]
    assert wire["d7_curve"]["current_value"] == pytest.approx(100000.0)
    assert "half_life=5" in wire["prose"]
    assert "selected_tier=2" in wire["prose"]
    assert "burn=25000" in wire["prose"]

    tr2 = build_translator(pcfg)
    restored = tr2.from_alias(wire)
    assert restored["holdingFeePct"] == {"HB-7101:TICKER:VNTG": 0.45}
    assert restored["holdingHalfLife"] == {"HB-7101:TICKER:VNTG": 8}
    assert restored["d7_curve"]["current_value"] == pytest.approx(400000.0)
    assert restored["d7_curve"]["yearly_rates"] == pytest.approx([14.0, 12.1, 10.6])


@pytest.mark.feature("demo_mode")
@pytest.mark.regression("SA5-02")
def test_holdings_cli_and_scope_recovery__fail_closed_on_corrupt_privacy_registry_and_project_demo(
    root, payload, sidecar
):
    """CLI demo projection honors saved demo_mode and both CLI and scope recovery fail closed on corrupt registries."""
    import config
    import holdings
    from privacy.registry import PrivacyConfigError, save_privacy_config

    pid = config.get_active_portfolio_id()
    save_privacy_config({"demo_mode": {"enabled": True, "scale_factor": 0.25}}, portfolio_id=pid)

    raw_copy = json.loads(json.dumps(payload))
    projected = holdings._apply_demo_if_active(raw_copy, pid=pid, force_demo=False)
    assert projected["privacy_status"]["demo_enabled"] is True
    assert projected["kpis"]["total_vested"] < payload["kpis"]["total_vested"] * 0.6

    sidecar.authenticate()
    live_html = sidecar.get("/").text
    scope_match = re.search(r'<meta name="advisorbot-portfolio" content="([0-9a-f]+)">', live_html)
    assert scope_match is not None
    scope_token = scope_match.group(1)

    reg_path = config.get_portfolio_dir(pid) / "privacy_registry.json"
    orig_reg = reg_path.read_text(encoding="utf-8")
    active_file = config.get_portfolios_dir() / "_active.json"
    orig_active = active_file.read_text(encoding="utf-8")
    try:
        reg_path.write_text("{corrupt-json", encoding="utf-8")
        with pytest.raises(PrivacyConfigError):
            holdings._apply_demo_if_active(raw_copy, pid=pid, force_demo=False)

        active_file.write_text(
            json.dumps({"active_portfolio_id": None, "last_active_portfolio_id": pid}) + "\n",
            encoding="utf-8",
        )
        config.refresh_active_paths()
        resp = sidecar.get("/api/saves", headers={"X-AdvisorBot-Portfolio": scope_token})
        assert resp.status == 503
    finally:
        reg_path.write_text(orig_reg, encoding="utf-8")
        active_file.write_text(orig_active, encoding="utf-8")
        config.refresh_active_paths()


@pytest.mark.feature("scenarios")
@pytest.mark.regression("SA5-03")
@pytest.mark.regression("DA4-11")
@pytest.mark.regression("DA4-08b")
def test_advisor_jobs__scale_demo_text_isolate_demo_state_and_disambiguate_proposals(root, payload, monkeypatch):
    """Advisor jobs scale inline USD in Demo Mode, isolate demo_enabled jobs, and disambiguate duplicate names."""
    import advisorbot_server as server
    import config
    import llm_tools as tools
    from privacy.demo_projector import DemoTranslator
    from privacy.registry import load_privacy_config, save_privacy_config

    monkeypatch.setattr(tools, "load_live_payload", lambda: payload)
    tools.invalidate_payload_cache()

    pcfg = load_privacy_config(auto_sync=True)
    pcfg["demo_mode"]["enabled"] = True
    pcfg["demo_mode"]["scale_factor"] = 0.25
    dtr = DemoTranslator(pcfg)
    scaled_ev = dtr.project_obj(
        {
            "reply": "Projected milestone wealth is $4,000,000 with $200,000 annual burn.",
            "proposal_label": "Save $200,000 plan",
            "last_step": "Simulating $400,000 withdrawal",
        }
    )
    assert "$4,000,000" not in scaled_ev["reply"]
    assert "$1.00M" in scaled_ev["reply"] or "$1,000,000" in scaled_ev["reply"]
    assert "$50,000" in scaled_ev["proposal_label"]
    assert "$100,000" in scaled_ev["last_step"]

    # DA4-08b: use_ref_rates resets holdingHalfLives and simOptimismMultiplier
    ui_res = tools.tool_control_live_ui(use_ref_rates=True)
    patch = ui_res["queued_command"]["settings_patch"]
    assert patch["holdingHalfLives"] == {}
    assert patch["simOptimismMultiplier"] == 1.0

    # DA4-11: author preservation, duplicate scenario disambiguation, and upvote target validation
    assert server._normalize_author({"type": "ai", "model": "Claude"})["type"] == "ai"
    assert server._normalize_author({"type": "ai_advisor", "model": "Claude"})["type"] == "ai_advisor"

    server.ensure_default_save()
    draft: dict = {"model": "Claude Sonnet 4.5"}
    tok = tools._advisor_draft.set(draft)
    try:
        res_dup = tools.execute_named_tool("holdings__manage_saves", {"action": "create", "name": "Default"})
        assert res_dup["pending_user_choice"] is True
        assert draft["args"]["name"] == "Default (Advisor)"
        assert "Default (Advisor)" in draft["proposal_label"]
        assert draft["args"]["author"] == {"type": "ai", "model": "Claude Sonnet 4.5"}

        bad_up = tools.execute_named_tool("holdings__manage_saves", {"action": "upvote", "id": "999"})
        assert bad_up["ok"] is False

        good_up = tools.execute_named_tool("holdings__manage_saves", {"action": "upvote", "id": "001"})
        assert good_up["pending_user_choice"] is True
        assert draft["args"]["model_name"] == "Claude Sonnet 4.5"
    finally:
        tools._advisor_draft.reset(tok)

    # SA5-03 / DA4-11: (pid, demo_enabled) isolation + applying idempotency + demo apply rejection
    pid = config.get_active_portfolio_id()
    live_job = {
        "job_id": "adv_live_1",
        "portfolio_id": pid,
        "demo_enabled": False,
        "status": "completed",
        "applying": True,
        "reply": "Live balance $4,000,000",
        "_proposal": {"action": "upvote", "id": "001", "model_name": "Advisor"},
    }
    demo_job = {
        "job_id": "adv_demo_1",
        "portfolio_id": pid,
        "demo_enabled": True,
        "status": "completed",
        "reply": "Demo analysis",
        "_proposal": {"action": "upvote", "id": "001", "model_name": "Advisor"},
    }
    monkeypatch.setitem(tools._ADVISOR_JOBS, "adv_live_1", live_job)
    monkeypatch.setitem(tools._ADVISOR_JOBS, "adv_demo_1", demo_job)
    monkeypatch.setitem(tools._LATEST_ADVISOR_JOBS, (pid, False), "adv_live_1")
    monkeypatch.setitem(tools._LATEST_ADVISOR_JOBS, (pid, True), "adv_demo_1")

    save_privacy_config({"demo_mode": {"enabled": False}}, portfolio_id=pid)
    compact_live = tools._compact_latest_advisor_job()
    assert compact_live is not None and compact_live["job_id"] == "adv_live_1"
    assert compact_live["applying"] is True
    assert tools.get_advisor_job_status("adv_demo_1")["job"] is None
    # Idempotent response when applying is already in flight
    assert tools.apply_advisor_proposal("adv_live_1") == {"ok": True, "applying": True}
    # Demo-flagged job cannot be applied even after exiting Demo Mode
    assert tools.apply_advisor_proposal("adv_demo_1")["ok"] is False

    save_privacy_config({"demo_mode": {"enabled": True, "scale_factor": 0.25}}, portfolio_id=pid)
    try:
        assert tools.get_advisor_job_status("adv_live_1")["job"] is None
        assert tools.get_advisor_job_status()["job"]["job_id"] == "adv_demo_1"
    finally:
        save_privacy_config({"demo_mode": {"enabled": False}}, portfolio_id=pid)


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA5-04")
def test_demo_mode__upgrades_mcp_chat_and_badge_shield_even_when_mode_is_off(root, monkeypatch):
    """When Demo Mode is enabled, MCP, in-app chat, and portfolio-scoped badge calls force Mode C scaling."""
    import advisorbot_server as server
    import config
    import llm_tools as tools
    import mcp_server
    from privacy.registry import save_privacy_config

    pid = config.get_active_portfolio_id()
    save_privacy_config(
        {
            "ai_shield": {"mode": "off"},
            "demo_mode": {"enabled": True, "scale_factor": 0.25},
            "mcp_server": {"enabled": True},
        },
        portfolio_id=pid,
    )
    try:
        denied, mcp_tr = mcp_server._check_mcp_access()
        assert denied is None
        assert mcp_tr is not None
        assert mcp_tr.mode == "identity_and_values"
        assert mcp_tr.scale_values is True
        assert mcp_tr.value_scale == pytest.approx(0.25)

        cli_err, cli_tr, _ = tools._resolve_inapp_privacy_shield(True, {}, portfolio_id=pid)
        assert cli_err is not None and cli_err.get("demo_blocked") is True
        assert cli_tr is None

        api_err, api_tr, eff_mode = tools._resolve_inapp_privacy_shield(
            False,
            {"provider": "gemini", "api_key": "fake-key-1234567890"},
            portfolio_id=pid,
        )
        assert api_err is None
        assert eff_mode == "identity_and_values"
        assert api_tr is not None and api_tr.scale_values is True
        assert api_tr.value_scale == pytest.approx(0.25)

        seen_pids: list[str | None] = []
        orig_gen = tools.generate_text_with_model

        def _spy_gen(prompt: str, model_id: str = "gemini", *, validator=None, portfolio_id=None):
            seen_pids.append(portfolio_id)
            return orig_gen(prompt, "parametric", validator=validator, portfolio_id=portfolio_id)

        monkeypatch.setattr(tools, "generate_text_with_model", _spy_gen)
        badge_res = server.handle_generate_badge({"portfolio_id": pid, "model": "gemini"})
        assert badge_res["ok"] is True
        assert seen_pids == [pid]
    finally:
        save_privacy_config({"ai_shield": {"mode": "identity_aliased"}, "demo_mode": {"enabled": False}}, portfolio_id=pid)


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA5-05")
def test_static_routes__block_cross_portfolio_statements_docs_and_snapshots_but_allow_assets(root, sidecar):
    """Cross-portfolio static reads allow only Assets/* icons and reject Statements, Docs, and snapshots."""
    other_slug = "p1_index_saver"
    other_dir = root / "Portfolios" / other_slug
    shutil.copytree(roots.fixture_dir(other_slug), other_dir, dirs_exist_ok=True)

    (other_dir / "Assets").mkdir(parents=True, exist_ok=True)
    (other_dir / "Assets" / "switcher_icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (other_dir / "Statements").mkdir(parents=True, exist_ok=True)
    (other_dir / "Statements" / "other_stmt.pdf").write_bytes(b"%PDF-1.4 secret")
    (other_dir / "Docs").mkdir(parents=True, exist_ok=True)
    (other_dir / "Docs" / "private.md").write_text("# Private", encoding="utf-8")
    (other_dir / "Scenarios" / "snapshots").mkdir(parents=True, exist_ok=True)
    (other_dir / "Scenarios" / "snapshots" / "001_default.png").write_bytes(b"PNG_SECRET")

    sidecar.authenticate()
    prefix = "/" + "/".join(("Portfolios", other_slug))
    assert sidecar.get(f"{prefix}/Assets/switcher_icon.svg").status == 200
    assert sidecar.get(f"{prefix}/Statements/other_stmt.pdf").status == 404
    assert sidecar.get(f"{prefix}/Docs/private.md").status == 404
    assert sidecar.get(f"{prefix}/Scenarios/snapshots/001_default.png").status == 404


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA5-09")
def test_cookie_mutations_and_demo_control_ui__enforce_portfolio_scope_and_read_only_lock(root, sidecar):
    """Cookie-authenticated writes require X-AdvisorBot-Portfolio and Demo Mode blocks mutating control_live_ui calls."""
    import llm_tools as tools
    from privacy.registry import save_privacy_config

    token = sidecar.authenticate()
    bootstrap = sidecar.get(f"/?token={token}", authenticated=False)
    cookie_hdr = bootstrap.headers["Set-Cookie"].split(";", 1)[0]
    scope_match = re.search(r'<meta name="advisorbot-portfolio" content="([0-9a-f]+)">', sidecar.get("/").text)
    assert scope_match is not None
    valid_scope = scope_match.group(1)

    csrf_headers = {
        "Cookie": cookie_hdr,
        "Origin": sidecar.base,
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "AdvisorBot",
    }
    # Missing X-AdvisorBot-Portfolio on portfolio-scoped POST/DELETE must return 409
    unscoped_post = sidecar.request(
        "POST",
        "/api/models/upvote",
        body={"id": "001", "reason": "unscoped"},
        headers=csrf_headers,
        authenticated=False,
    )
    assert unscoped_post.status == 409

    unscoped_del = sidecar.request(
        "DELETE",
        "/api/models",
        body={"id": "999"},
        headers=csrf_headers,
        authenticated=False,
    )
    assert unscoped_del.status == 409

    # Global preferences route remains allowed, and scoped portfolio route succeeds
    pref_ok = sidecar.request(
        "POST",
        "/api/preferences/global",
        body={"friendly_names": {}},
        headers=csrf_headers,
        authenticated=False,
    )
    assert pref_ok.status == 200

    scoped_ok = sidecar.request(
        "POST",
        "/api/ui/poll",
        body={"since": 0},
        headers={**csrf_headers, "X-AdvisorBot-Portfolio": valid_scope},
        authenticated=False,
    )
    assert scoped_ok.status == 200

    # Demo Mode blocks mutating holdings__control_live_ui options while allowing pure screen navigation
    save_privacy_config({"demo_mode": {"enabled": True, "scale_factor": 0.25}})
    try:
        for bad_args in (
            {"settings_patch": {"simAnnualBurn": 50000}},
            {"load_save_id": "001"},
            {"reoptimize_order": True},
            {"use_ref_rates": True},
        ):
            _, wire_res = tools._exec_shielded_tool("holdings__control_live_ui", bad_args, None)
            assert wire_res["ok"] is False and wire_res.get("demo_blocked") is True

        _, nav_res = tools._exec_shielded_tool("holdings__control_live_ui", {"switch_screen": "retirement"}, None)
        assert nav_res["ok"] is True
    finally:
        save_privacy_config({"demo_mode": {"enabled": False}})


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA5-10")
def test_browser_tickets_and_artifact_permissions__fifo_cap_single_mint_and_chmod_0600(root, tmp_path, monkeypatch):
    """Browser tickets cap at 64 FIFO, main() mints a single startup ticket, and badges/snapshots write 0600."""
    import advisorbot_server as server
    import config

    with server._BROWSER_TICKETS_LOCK:
        server._BROWSER_TICKETS.clear()
    minted = [server._new_browser_ticket() for _ in range(70)]
    with server._BROWSER_TICKETS_LOCK:
        assert len(server._BROWSER_TICKETS) == 64
    assert server._consume_browser_ticket(minted[0]) is False
    assert server._consume_browser_ticket(minted[-1]) is True

    # Verify main() mints exactly ONE ticket when both stdout.isatty() and --open are active
    mint_calls: list[str] = []
    opened_tokens: list[str] = []
    orig_mint = server._new_browser_ticket

    def _counting_mint(*args, **kwargs):
        tok = orig_mint(*args, **kwargs)
        mint_calls.append(tok)
        return tok

    class _DummyServer:
        def __init__(self, *args, **kwargs):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(server, "_new_browser_ticket", _counting_mint)
    monkeypatch.setattr(server, "ThreadingHTTPServer", _DummyServer)
    monkeypatch.setattr(server, "_open_authenticated_browser", lambda _h, _p, tok: opened_tokens.append(tok))
    monkeypatch.setattr(server, "ensure_default_save", lambda *a, **kw: None)
    monkeypatch.setattr(server.llm_tools, "get_cached_payload", lambda *a, **kw: {})
    monkeypatch.setattr(sys, "argv", ["advisorbot_server.py", "--open"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    prior_umask = __import__("os").umask(0o077)
    try:
        assert server.main() == 0
    finally:
        __import__("os").umask(prior_umask)
    assert len(mint_calls) == 1
    assert opened_tokens == [mint_calls[0]]

    # Verify 0600 permissions on handle_upload_badge, handle_generate_badge, and _write_jpeg_bytes
    pid = config.get_active_portfolio_id()
    pdir = config.get_portfolio_dir(pid)
    svg_b64 = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64"></svg>').decode("ascii")
    rel_up = server.handle_upload_badge(pdir, pid, f"data:image/svg+xml;base64,{svg_b64}")
    up_path = root / rel_up
    assert (up_path.stat().st_mode & 0o777) == 0o600
    assert ((pdir / "Assets" / "household_badge.svg").stat().st_mode & 0o777) == 0o600

    gen_res = server.handle_generate_badge({"portfolio_id": pid, "model": "parametric"})
    assert gen_res["ok"] is True
    gen_rel = gen_res["badge_image"].split("?", 1)[0]
    assert ((root / gen_rel).stat().st_mode & 0o777) == 0o600

    snap_dest = tmp_path / "snap_test.jpg"
    server._write_jpeg_bytes(snap_dest, b"raw_fallback_bytes")
    assert (snap_dest.stat().st_mode & 0o777) == 0o600

    gitignore_txt = (env.app_checkout_dir() / ".gitignore").read_text(encoding="utf-8")
    assert "*.tmp" in gitignore_txt.splitlines()


@pytest.mark.feature("ingest")
@pytest.mark.regression("DA4-13")
def test_stock_plan_parsers__read_canonical_grant_forecast_config_keys(tmp_path):
    """Stock-plan quarterly PDF, unvested schedule PDF, and activity XLSX parsers honor cfg['grant_forecast']."""
    from ingest.morgan_stanley import parse_ms_quarterly, parse_ms_unvested_gsu
    from ingest.ms_activity_xlsx import parse_ms_activity_xlsx

    cfg_grant_only = {
        "primary_equity_symbol": "VNTG",
        "grant_forecast": {
            "symbol": "VNTG",
            "company_name": "Vantage Systems",
            "award_type": "PSU",
            "target_vested_account_id": "VNTG_PLAN_01",
        },
    }

    fmt_usd = lambda n: "$" + f"{n:,.2f}"
    q_page = "\n".join(
        [
            "Opening Value (as of 01/01/26) Closing Value (as of 03/31/26)",
            "Number of Shares 100.000 120.000",
            f"Share Price {fmt_usd(180)} {fmt_usd(200)}",
            f"Share Value {fmt_usd(18000)} {fmt_usd(24000)}",
            f"Cash Value {fmt_usd(0)} {fmt_usd(500)}",
            f"Total Account Value {fmt_usd(18000)} {fmt_usd(24500)}",
            "02/15/26 Release 20.000",
        ]
    )
    q_stmt = parse_ms_quarterly([q_page], cfg_grant_only)
    assert q_stmt.account_id == "VNTG_PLAN_01"
    assert q_stmt.positions[0].symbol_or_code == "VNTG"
    assert q_stmt.positions[0].metadata["award_type"] == "PSU"

    u_page = "\n".join(
        [
            "Restricted Shares Vesting report run on 31-Mar-2026",
            "RSU GR-101 15-Jun-2026 50.000 50.000",
        ]
    )
    u_stmt = parse_ms_unvested_gsu([u_page], cfg_grant_only)
    assert u_stmt.account_id == "VNTG_PLAN_01"
    assert u_stmt.gsu_tranches[0].symbol == "VNTG"
    assert u_stmt.gsu_tranches[0].company_name == "Vantage Systems"

    xlsx_path = tmp_path / "activity.xlsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>'
            '<sheet name="Releases Report" sheetId="1" r:id="rId1"/>'
            '<sheet name="Withdrawals Report" sheetId="2" r:id="rId2"/>'
            '</sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            '</Relationships>',
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Vest Date</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>Order</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>2026-02-15</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>ORD-1</t></is></c>'
            '<c r="E2" t="inlineStr"><is><t>Complete</t></is></c>'
            '<c r="F2" t="inlineStr"><is><t>200</t></is></c>'
            '<c r="G2" t="inlineStr"><is><t>40</t></is></c>'
            '<c r="I2" t="inlineStr"><is><t>20</t></is></c>'
            '<c r="J2" t="inlineStr"><is><t>Withhold</t></is></c></row>'
            '</sheetData></worksheet>',
        )
        zf.writestr(
            "xl/worksheets/sheet2.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Date</t></is></c></row>'
            '</sheetData></worksheet>',
        )
    xlsx_path.write_bytes(buf.getvalue())
    x_stmt = parse_ms_activity_xlsx(xlsx_path, cfg_grant_only)
    assert x_stmt.account_id == "VNTG_PLAN_01"
    assert len(x_stmt.gsu_releases) == 1
    assert x_stmt.gsu_releases[0].symbol == "VNTG"
    assert x_stmt.gsu_releases[0].company_name == "Vantage Systems"
    assert x_stmt.gsu_releases[0].award_type == "PSU"
