#!/usr/bin/env python3
"""
What actually leaves the machine when the AI assistant is used.

The provider HTTP helper is replaced by a recorder, so these tests assert against the
serialised request body rather than against the translator in isolation. That distinction
matters: a leak can happen after translation (a dict key, a tool name, an error string
baked into a prompt), and only the wire payload shows it.

Covers the Nov 15 audit findings SA-02 (fail closed), SA-03 (wire leak gate and key
scrub), SA-04 (value scaling) and SA-05/SA-10 (paths that bypassed the shield).
"""

from __future__ import annotations

import json

import pytest

from tests.support import fake_llm, roots

pytestmark = [pytest.mark.portfolio("p4_trust_household"), pytest.mark.security]


def _registry_terms(cfg: dict) -> list[str]:
    """Real entity names the shield is supposed to replace, from the fixture's own data."""
    terms: list[str] = [str(cfg["household_name"])]
    for investor in cfg.get("investors") or []:
        terms.append(str(investor["name"]))
    for beneficiary in cfg.get("beneficiaries") or []:
        terms.append(str(beneficiary["name"]))
    for account in (cfg.get("accounts") or {}).values():
        if account.get("institution"):
            terms.append(str(account["institution"]))
    return [t for t in dict.fromkeys(terms) if len(t) > 3]


def _set_shield(mode: str) -> None:
    from privacy import save_privacy_config

    save_privacy_config({"ai_shield": {"mode": mode}})


@pytest.mark.feature("privacy_shield")
def test_fixture_default__is_identity_aliased(root, slug):
    """A fresh portfolio starts with the shield on, not off."""
    from privacy import load_privacy_config

    cfg = load_privacy_config(auto_sync=False)
    assert (cfg.get("ai_shield") or {}).get("mode") == "identity_aliased"
    assert (cfg.get("ai_shield") or {}).get("block_inapp_agy_cli") is True
    assert (cfg.get("mcp_server") or {}).get("enabled") is False


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03")
def test_identity_aliased_chat__sends_no_registry_term_on_the_wire(root, cfg):
    """
    With the shield on, nothing recognisable about the household reaches the provider.

    Exercises: the whole outbound path at once. The assertion is on the serialised request
    (URL, headers and body), so an alias that is applied to prose but not to a dict key or
    a tool name still fails here.
    """
    import llm_tools

    fake_llm.install_config(root, "claude")
    _set_shield("identity_aliased")
    terms = _registry_terms(cfg)

    with fake_llm.recording() as wire:
        result = llm_tools.run_chat_via_agy_cli(
            "Summarise my accounts and their custodians.",
            model="llm:claude:test-model",
            allow_fast_path=False,
        )
    assert wire.requests, f"no provider call was made: {result.get('error')}"
    leaked = wire.leaked_terms(terms)
    assert not leaked, f"registry terms reached the provider: {leaked}"


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03")
def test_wire_payload__carries_no_legacy_vocabulary(root):
    """
    No key or tool name on the wire names the original employer or its stock.

    Exercises: finding SA-03, where `goog_*` payload keys and a tool named after the
    original ticker defeated aliasing even though every value was translated.
    """
    import llm_tools

    fake_llm.install_config(root, "claude")
    _set_shield("identity_aliased")

    with fake_llm.recording() as wire:
        llm_tools.run_chat_via_agy_cli(
            "What is my portfolio worth?", model="llm:claude:test-model", allow_fast_path=False
        )
    assert wire.requests
    leaked = wire.leaked_terms(roots.denylist_terms())
    assert not leaked, f"legacy vocabulary on the wire: {leaked}"


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-02")
def test_shield__fails_closed_when_the_privacy_config_cannot_be_read(root, monkeypatch):
    """
    A broken privacy config blocks the call instead of silently sending raw data.

    Exercises: finding SA-02. The failure mode was a bare `except` that returned mode
    "off", so a corrupt registry downgraded the user to clear text while the badge still
    displayed "Identity Aliased".
    """
    import llm_tools

    fake_llm.install_config(root, "claude")

    def explode(*args, **kwargs):
        raise RuntimeError("corrupt privacy registry")

    monkeypatch.setattr(llm_tools, "load_privacy_config", explode)

    with fake_llm.recording() as wire:
        result = llm_tools.run_chat_via_agy_cli(
            "Anything at all.", model="llm:claude:test-model", allow_fast_path=False
        )
    assert not wire.requests, "a provider call was made despite an unreadable shield config"
    assert result.get("ok") is False
    assert "privacy" in str(result.get("error", "")).lower() or "shield" in str(result.get("error", "")).lower()


@pytest.mark.feature("privacy_shield")
def test_local_only_mode__refuses_a_cloud_endpoint(root):
    """Local-only means local: a remote base URL is rejected before any request."""
    import llm_tools

    fake_llm.install_config(root, "claude")
    _set_shield("local_only")

    with fake_llm.recording() as wire:
        result = llm_tools.run_chat_via_agy_cli(
            "Hello.", model="llm:claude:test-model", allow_fast_path=False
        )
    assert not wire.requests, "a cloud call was made in local-only mode"
    assert result.get("ok") is False
    assert "local" in str(result.get("error", "")).lower()


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-04")
def test_scaled_mode__multiplies_the_dollar_figures_on_the_wire(root):
    """
    In the scaled profile, the money the provider sees is not the household's money.

    Exercises: finding SA-04, where scaling was applied by key name and missed several
    value fields, making the scale factor recoverable. The check here is that the largest
    number on the wire is well below the portfolio's real total.
    """
    import llm_tools
    from tests.support import pipeline as pipeline_support

    fake_llm.install_config(root, "claude")
    _set_shield("identity_and_values")

    payload = pipeline_support.build_payload()
    real_total = sum(float(a.get("current_value") or 0.0) for a in payload["retirement_sim_assets"])
    assert real_total > 0

    with fake_llm.recording() as wire:
        llm_tools.run_chat_via_agy_cli(
            "List every bucket and its value.", model="llm:claude:test-model", allow_fast_path=False
        )
    assert wire.requests

    numbers = [
        float(m)
        for m in __import__("re").findall(r"\d[\d,]*\.?\d*", wire.body_text().replace(",", ""))
        if m.replace(",", "").replace(".", "").isdigit()
    ]
    big = [n for n in numbers if n > real_total * 0.9]
    assert not big, f"unscaled portfolio-sized values on the wire: {sorted(big)[:5]}"


@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-05")
def test_cli_backend__is_blocked_by_default(root):
    """
    The workspace CLI agent cannot be driven from the in-app chat while the shield is on.

    Exercises: that backend reads the portfolio straight off disk, so no membrane can
    translate what it sees. The shield therefore refuses it rather than aliasing in vain.
    """
    import llm_tools

    _set_shield("identity_aliased")
    result = llm_tools.run_chat_via_agy_cli("Use the CLI.", model="default", allow_fast_path=False)
    assert result.get("ok") is False
    assert "cli" in str(result.get("error", "")).lower() or "shield" in str(result.get("error", "")).lower()


@pytest.mark.feature("aliasing")
def test_alias_round_trip__restores_real_names_for_the_user(root, cfg):
    """
    The reply the user reads is de-aliased, so the membrane is invisible in the UI.

    Exercises: the inbound half of the translation. A provider reply that mentions an alias
    token must come back as the real entity name.
    """
    from privacy import build_translator, load_privacy_config

    _set_shield("identity_aliased")
    translator = build_translator(load_privacy_config(auto_sync=True))
    if translator is None:
        pytest.skip("no translator available for this portfolio")

    household = str(cfg["household_name"])
    aliased = translator.to_alias_text(f"The {household} holds two accounts.")
    assert household not in aliased, "the household name was not aliased"
    restored = translator.from_alias_text(aliased)
    assert household in restored, "the alias did not round-trip back to the real name"


@pytest.mark.feature("privacy_shield")
def test_model_list__never_includes_an_api_key(root):
    """The browser's model selector gets status, never credentials."""
    import llm_tools

    fake_llm.install_config(root, "claude", api_key="sk-ant-secret-value-not-real")
    models = llm_tools.list_available_models(probe=False)
    blob = json.dumps(models)
    assert "sk-ant-secret-value-not-real" not in blob
    assert any(m.get("has_key") for m in models), "the fixture provider should report a key"


@pytest.mark.feature("privacy_shield")
def test_shielded_tool_outputs__alias_dynamic_investor_keys_and_ignore_lowercase_meta():
    """Dict keys in reference_rates/milestones alias investor names and lowercase `_meta` does not trip `META`."""
    from privacy import build_translator

    tr = build_translator(
        {
            "ai_shield": {"mode": "identity_aliased", "alias_products": True},
            "entities": [
                {
                    "token": "INVESTOR_1",
                    "category": "investor",
                    "canonical": "Alex",
                    "synonyms": ["Alex"],
                    "enabled": True,
                },
                {
                    "token": "STOCK_2",
                    "category": "equity",
                    "canonical": "META",
                    "synonyms": ["META"],
                    "enabled": True,
                },
            ],
        }
    )
    sample = {
        "advisor_prompts_meta": {"preferred_forecast": {"has_project_override": False}},
        "reference_rates": {"ssn_alex_claim_ref_age": 67},
        "milestones": {"alex_85": {"nominal_usd": 1000000}},
    }
    wire = tr.to_alias(sample)
    assert "advisor_prompts_meta" in wire
    assert "ssn_INVESTOR_1_claim_ref_age" in wire["reference_rates"]
    assert "INVESTOR_1_85" in wire["milestones"]
    assert tr.find_leaked_terms(wire) == []
    restored = tr.from_alias(wire)
    assert restored == sample


@pytest.mark.feature("privacy_shield")
def test_mode_c_scaling__preserves_camelcase_ages_and_horizons():
    """Mode C value scaling never distorts camelCase age, year, or horizon setting keys."""
    from privacy import build_translator

    tr = build_translator(
        {
            "ai_shield": {"mode": "identity_and_values", "scale_factor": 0.25},
            "entities": [],
        }
    )
    payload = {
        "ssnAlexClaimAge": 67,
        "retirementGoalYears": 40,
        "retireAge": 62,
        "birthYear": 1971,
        "simAnnualBurn": 200000,
    }
    wire = tr.to_alias(payload)
    assert wire["ssnAlexClaimAge"] == 67
    assert wire["retirementGoalYears"] == 40
    assert wire["retireAge"] == 62
    assert wire["birthYear"] == 1971
    assert wire["simAnnualBurn"] == 50000


@pytest.mark.feature("privacy_shield")
def test_translator_prose_and_family_birthdays__no_false_code_alias_and_clean_restore():
    """Snake_case identifiers like `ms_month` stay intact while `family_birthdays` restores in all modes."""
    from privacy import DemoTranslator, build_translator

    cfg = {
        "ai_shield": {"mode": "identity_aliased", "dob_granularity": "year_month", "alias_products": True},
        "demo_mode": {"scale_factor": 0.25},
        "entities": [
            {"token": "INVESTOR_1", "category": "investor", "canonical": "Alex", "synonyms": ["Alex"], "demo_value": "Jordan", "enabled": True},
            {"token": "BANK_2", "category": "custodian", "canonical": "Morgan Stanley", "synonyms": ["Morgan Stanley", "MS"], "demo_value": "Northbank", "demo_meta": {"codes": ["NB"]}, "enabled": True},
            {"token": "STOCK_2", "category": "equity", "canonical": "META", "synonyms": ["META"], "demo_value": "APEX", "demo_meta": {"symbol": "APEX"}, "enabled": True},
        ],
    }
    tr = build_translator(cfg)
    prose = "Check ms_month and advisor_prompts_meta vs MS and META holdings."
    wire_txt = tr.to_alias_text(prose)
    assert "ms_month" in wire_txt
    assert "advisor_prompts_meta" in wire_txt
    assert "BANK_2" in wire_txt
    assert "STOCK_2" in wire_txt

    dt = DemoTranslator(cfg)
    demo_txt = dt.map_text(prose)
    assert "ms_month" in demo_txt
    assert "advisor_prompts_meta" in demo_txt

    fb = {"family_birthdays": [{"name": "Alex", "y": 1971, "m": 4, "d": 19}]}
    wire_fb = tr.to_alias(fb)
    assert wire_fb["family_birthdays"][0]["name"] == "INVESTOR_1"
    restored_fb = tr.from_alias(wire_fb)
    assert restored_fb["family_birthdays"][0]["name"] == "Alex"


@pytest.mark.feature("privacy_shield")
def test_sync_privacy_registry__respects_edited_synonym_deletions(root, slug):
    """When `synonyms_edited` is set, re-syncing does not re-add manually removed synonyms."""
    from privacy import load_privacy_config, save_privacy_config, sync_privacy_registry

    initial = load_privacy_config(slug, auto_sync=True)
    target = next(e for e in initial["entities"] if e.get("synonyms"))
    tok = target["token"]
    kept = [target["canonical"]]
    save_privacy_config(
        {"entities": [{"token": tok, "synonyms": kept, "synonyms_edited": True}]},
        portfolio_id=slug,
    )
    resynced = sync_privacy_registry(slug, save=False)
    after = next(e for e in resynced["entities"] if e["token"] == tok)
    assert after.get("synonyms_edited") is True
    assert after["synonyms"] == kept


@pytest.mark.feature("privacy_shield")
def test_http_post_json__retries_transient_503_and_429_then_succeeds(monkeypatch):
    """Transient 503/429 responses retry with backoff, while 400 fails immediately without retrying."""
    import io
    import urllib.error
    import urllib.request
    import llm_tools

    sleeps: list[float] = []
    monkeypatch.setattr(llm_tools.time, "sleep", lambda s: sleeps.append(s))

    attempts = {"n": 0}

    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body
        def read(self) -> bytes:
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def _flaky_urlopen(req, timeout=150):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"busy"))
        if attempts["n"] == 2:
            raise urllib.error.HTTPError(req.full_url, 429, "Rate", {"Retry-After": "1.5"}, io.BytesIO(b"rate"))
        return _FakeResp(b'{"ok": true, "msg": "recovered"}')

    monkeypatch.setattr(urllib.request, "urlopen", _flaky_urlopen)
    out = llm_tools._post_json_with_retry("http://127.0.0.1:9999/v1/test", {}, {"q": 1}, max_retries=3, base_delay=1.0)
    assert out == {"ok": True, "msg": "recovered"}
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert sleeps[1] == 1.5  # Honored Retry-After header

    # Non-retryable 400 fails on first attempt without sleeping
    sleeps.clear()
    attempts["n"] = 0

    def _bad_req(req, timeout=150):
        attempts["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"bad"))

    monkeypatch.setattr(urllib.request, "urlopen", _bad_req)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        llm_tools._post_json_with_retry("http://127.0.0.1:9999/v1/test", {}, {"q": 1}, max_retries=3, base_delay=1.0)
    assert exc_info.value.code == 400
    assert attempts["n"] == 1
    assert sleeps == []

    # Persistent 503 retries max_retries times and raises after exhausting attempts
    sleeps.clear()
    attempts["n"] = 0

    def _always_503(req, timeout=150):
        attempts["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"overloaded"))

    monkeypatch.setattr(urllib.request, "urlopen", _always_503)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        llm_tools._post_json_with_retry("http://127.0.0.1:9999/v1/test", {}, {"q": 1}, max_retries=2, base_delay=0.5)
    assert exc_info.value.code == 503
    assert attempts["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.feature("privacy_shield")
def test_gemini_chat__falls_back_to_healthy_model_on_timeout_or_503(monkeypatch):
    """When the selected Gemini model times out in Round 1, chat automatically falls back to a healthy model."""
    import llm_tools

    called_urls: list[str] = []

    def _fake_post(url, headers, body, **kwargs):
        called_urls.append(url)
        if "gemini-4-hydrogen" in url:
            raise TimeoutError("The read operation timed out")
        return {
            "candidates": [
                {"content": {"parts": [{"text": "Use Historical for fat-tail crash modeling."}]}}
            ]
        }

    monkeypatch.setattr(llm_tools, "_http_post_json", _fake_post)
    out = llm_tools._run_chat_gemini_api(
        "Which return model?",
        {
            "provider": "gemini",
            "model": "gemini-4-hydrogen",
            "api_key": "fake-key",
            "api_base_url": "https://generativelanguage.googleapis.com/v1beta",
        },
        "",
        0,
        translator=None,
    )
    assert out["ok"] is True
    assert any("gemini-4-hydrogen" in u for u in called_urls)
    assert any("gemini-2.5-flash" in u for u in called_urls)
    assert "Use Historical" in out["reply"]
    assert "gemini-2.5-flash" in out["reply"]


@pytest.mark.regression("SA4-03")
@pytest.mark.parametrize("contents", ["{", '{"ai_shield":{"mode":"unsupported"}}'])
def test_damaged_registry_blocks_egress_and_private_http(root, sidecar, contents):
    """A malformed policy on disk blocks a synthetic provider request and returns 503 on private HTTP routes."""
    import llm_tools
    import config
    fake_llm.install_config(root, "claude")
    sidecar.authenticate()
    (config.get_portfolio_dir() / "privacy_registry.json").write_text(contents)
    with fake_llm.recording() as wire:
        result = llm_tools.run_chat_via_agy_cli("Summarize.", model="llm:claude:test-model", allow_fast_path=False)
    assert result["ok"] is False and not wire.requests
    assert sidecar.get("/api/project").status == 503
    assert sidecar.get("/").status == 503


@pytest.mark.regression("SA4-04")
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["off", "local_only", "identity_aliased", "identity_and_values"])
def test_mcp_mode_matrix(root, enabled, mode):
    """MCP requires explicit enablement and rejects Local Only even with the service enabled."""
    import mcp_server
    from privacy import save_privacy_config
    save_privacy_config({"ai_shield": {"mode": mode}, "mcp_server": {"enabled": enabled}})
    error, translator = mcp_server._check_mcp_access()
    assert (error is None) == (enabled and mode != "local_only")
    assert (translator is not None) == (enabled and mode in {"identity_aliased", "identity_and_values"})


@pytest.mark.regression("SA4-05")
def test_switch_during_payload_build_discards_old_household(root, monkeypatch):
    """A generation change during a pinned build prevents the old household from reaching the next cache read."""
    import config
    import llm_tools
    def switch_during_build():
        assert config.get_active_portfolio_id() == "household_a"
        llm_tools.invalidate_payload_cache()
        return {"sentinel": "A"}
    monkeypatch.setattr(llm_tools, "load_live_payload", switch_during_build)
    with config.portfolio_context("household_a"), pytest.raises(RuntimeError, match="changed"):
        llm_tools.get_cached_payload(force_reload=True)
    monkeypatch.setattr(llm_tools, "load_live_payload", lambda: {"sentinel": "B"})
    with config.portfolio_context("household_b"):
        assert llm_tools.get_cached_payload() == {"sentinel": "B"}


@pytest.mark.parametrize("action", ["upvote", "create"])
def test_advisor_review_waits_for_the_user_before_writing(root, monkeypatch, action):
    """A completed review offers an optional action and applying the same proposal twice writes only once."""
    import llm_tools as tools
    import config

    draft = {}
    ctx = tools._advisor_draft.set(draft)
    writes = []
    monkeypatch.setattr(tools, "tool_manage_saves", lambda **args: writes.append(args) or {"ok": True})
    try:
        args = {"action": action, "id": "001", "name": "Alternative", "overwrite_existing": True}
        assert tools.execute_named_tool("holdings__manage_saves", args)["pending_user_choice"]
        assert not tools.execute_named_tool("holdings__manage_saves", {"action": "delete"})["ok"]
        assert not writes
    finally:
        tools._advisor_draft.reset(ctx)
    job_id = "test-review"
    job = {"job_id": job_id, "portfolio_id": config.get_active_portfolio_id(), "status": "completed",
           "_proposal": draft["args"], "proposal_label": draft["label"]}
    monkeypatch.setitem(tools._ADVISOR_JOBS, job_id, job)
    assert tools.apply_advisor_proposal(job_id)["ok"]
    assert tools.apply_advisor_proposal(job_id)["ok"]
    assert len(writes) == 1 and writes[0]["action"] == action
    if action == "create":
        assert writes[0]["set_active"] is False and writes[0]["overwrite_existing"] is False


@pytest.mark.portfolio("p2_equity_comp_rsu")
@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03", "SA-04")
def test_overview_exposes_enriched_ssn_grants_and_evidence_under_mode_c(root, cfg, payload, monkeypatch):
    """Enriched overview sections alias entities and scale only monetary/share quantities in Mode C."""
    from privacy import PrivacyTranslator, load_privacy_config, save_privacy_config
    import llm_tools

    monkeypatch.setattr(llm_tools, "load_live_payload", lambda: payload)
    llm_tools.invalidate_payload_cache()
    save_privacy_config({"ai_shield": {"mode": "identity_and_values", "scale_factor": 0.25}})
    pcfg = load_privacy_config(auto_sync=True)
    tr = PrivacyTranslator(pcfg)

    raw_ov = llm_tools.tool_get_overview()
    assert raw_ov["ok"]
    assert "ssn_model" in raw_ov and raw_ov["ssn_model"].get("investors")
    assert raw_ov["unvested_grants"]["has_unvested"]
    assert raw_ov["unvested_grants"]["companies"][0]["upcoming_vests"]
    assert any(a.get("growth_evidence") for a in raw_ov["simulator_assets"])

    shielded = tr.to_alias(llm_tools._scrub_primary_symbol_keys(raw_ov))
    new_sections_txt = json.dumps({
        "ssn_model": shielded["ssn_model"],
        "unvested_grants": shielded["unvested_grants"],
        "growth_evidence": [a.get("growth_evidence") for a in shielded["simulator_assets"]],
    })
    for term in _registry_terms(cfg):
        assert term not in new_sections_txt, f"real term leaked into enriched overview sections: {term}"
    if cfg.get("primary_equity_symbol"):
        assert str(cfg["primary_equity_symbol"]) not in json.dumps(shielded)

    raw_invs = raw_ov["ssn_model"]["investors"]
    sh_invs = shielded["ssn_model"]["investors"]
    assert len(sh_invs) == len(raw_invs)
    for r_inv, s_inv in zip(raw_invs, sh_invs):
        assert "external_file" not in s_inv
        assert s_inv["fra_age"] == r_inv["fra_age"]
        assert s_inv["default_claim_age"] == r_inv["default_claim_age"]
        assert s_inv["milestone_age"] == r_inv["milestone_age"]
        assert s_inv["work_years"] == r_inv["work_years"]
        assert s_inv["zero_years"] == r_inv["zero_years"]
        assert str(r_inv["name"]) not in str(s_inv["claim_setting_key"])
        assert s_inv["pia_monthly_2026"] == pytest.approx(round(r_inv["pia_monthly_2026"] * 0.25, 2))
        assert s_inv["pia_annual_2026"] == pytest.approx(round(r_inv["pia_annual_2026"] * 0.25, 2))

    raw_ev_idx, raw_ev_asset = next((i, a) for i, a in enumerate(raw_ov["simulator_assets"]) if a.get("growth_evidence"))
    sh_ev_asset = shielded["simulator_assets"][raw_ev_idx]
    assert [e["tier"] for e in sh_ev_asset["growth_evidence"]] == [e["tier"] for e in raw_ev_asset["growth_evidence"]]
    assert [e["projected_return_pct"] for e in sh_ev_asset["growth_evidence"]] == [
        e["projected_return_pct"] for e in raw_ev_asset["growth_evidence"]
    ]

    r_co = raw_ov["unvested_grants"]["companies"][0]
    s_co = shielded["unvested_grants"]["companies"][0]
    assert s_co["net_retention_rate"] == r_co["net_retention_rate"]
    assert s_co["live_price"] == r_co["live_price"]
    assert s_co["future_tranche_count"] == r_co["future_tranche_count"]
    assert s_co["upcoming_vest_dates_count"] == r_co["upcoming_vest_dates_count"]
    assert s_co["future_gross_shares"] == pytest.approx(round(r_co["future_gross_shares"] * 0.25, 6), rel=1e-4)
    assert s_co["future_net_val"] == pytest.approx(round(r_co["future_net_val"] * 0.25, 2), rel=1e-4)
    assert s_co["upcoming_vests"][0]["tranche_count"] == r_co["upcoming_vests"][0]["tranche_count"]
    assert s_co["upcoming_vests"][0]["gross_shares"] == pytest.approx(
        round(r_co["upcoming_vests"][0]["gross_shares"] * 0.25, 6), rel=1e-4
    )
    assert s_co["upcoming_vests"][0]["est_net_val"] == pytest.approx(
        round(r_co["upcoming_vests"][0]["est_net_val"] * 0.25, 2), rel=1e-4
    )


def test_tool_declarations_document_simulation_and_live_ui_controls():
    """Tool declarations expose full simulation, override, liquidation, and view-patch controls."""
    import llm_tools

    by_name = {d["name"]: d for d in llm_tools.GEMINI_TOOL_DECLARATIONS}
    run_props = by_name["holdings__run_simulation"]["parameters"]["properties"]
    assert "compare_with_baseline" in run_props
    ov_desc = run_props["overrides"]["description"]
    for tok in (
        "simStressScenario",
        "simOptimismMultiplier",
        "holdingHalfLives",
        "mcReturnModel",
        "cohortCenterMode",
        "simBearShield",
        "simOrderPreset",
        "simAssetOrder",
        "simScheduledActions",
        "simHypotheticalAssets",
    ):
        assert tok in ov_desc

    ui_props = by_name["holdings__control_live_ui"]["parameters"]["properties"]
    assert "simScaleMode" in ui_props["view_patch"]["description"]
    assert "simStressScenario" in ui_props["settings_patch"]["description"]


@pytest.mark.portfolio("p2_equity_comp_rsu")
@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03", "SA-04", "SA2-10")
def test_overview_normal_mode_cleartext_and_demo_mode_projection(root, cfg, payload, monkeypatch):
    """Normal mode returns clear-text unscaled data while Demo Mode projects all names and scales USD/shares."""
    from privacy import DemoTranslator, load_privacy_config, save_privacy_config
    import llm_tools

    monkeypatch.setattr(llm_tools, "load_live_payload", lambda: payload)
    llm_tools.invalidate_payload_cache()

    # Normal mode: clear text and unscaled values
    normal_ov = llm_tools.tool_get_overview()
    assert normal_ov["ok"]
    inv0 = normal_ov["ssn_model"]["investors"][0]
    assert inv0["name"] in {i["name"] for i in (cfg.get("investors") or [])}
    co0 = normal_ov["unvested_grants"]["companies"][0]
    assert co0["symbol"] == cfg["primary_equity_symbol"]
    uv0 = co0["upcoming_vests"][0]

    # Demo Mode: obfuscated entities and uniformly scaled USD & shares (k=0.25)
    save_privacy_config({"demo_mode": {"enabled": True, "scale_factor": 0.25}})
    demo_cfg = load_privacy_config(auto_sync=True)
    demo_ov = DemoTranslator(demo_cfg).project_obj(normal_ov)

    d_inv0 = demo_ov["ssn_model"]["investors"][0]
    assert d_inv0["name"] != inv0["name"]
    assert inv0["name"] not in d_inv0["claim_setting_key"]
    assert d_inv0["pia_monthly_2026"] == pytest.approx(round(inv0["pia_monthly_2026"] * 0.25, 2))
    assert d_inv0["pia_annual_2026"] == pytest.approx(round(inv0["pia_annual_2026"] * 0.25, 2))
    assert d_inv0["fra_age"] == inv0["fra_age"]

    d_co0 = demo_ov["unvested_grants"]["companies"][0]
    assert d_co0["symbol"] != co0["symbol"]
    assert d_co0["net_retention_rate"] == co0["net_retention_rate"]
    assert d_co0["live_price"] == co0["live_price"]
    assert d_co0["future_tranche_count"] == co0["future_tranche_count"]
    assert d_co0["upcoming_vest_dates_count"] == co0["upcoming_vest_dates_count"]
    assert d_co0["future_gross_shares"] == pytest.approx(round(co0["future_gross_shares"] * 0.25, 3))
    assert d_co0["future_net_val"] == pytest.approx(round(co0["future_net_val"] * 0.25, 2))

    d_uv0 = d_co0["upcoming_vests"][0]
    assert d_uv0["tranche_count"] == uv0["tranche_count"]
    assert d_uv0["gross_shares"] == pytest.approx(round(uv0["gross_shares"] * 0.25, 3))
    assert d_uv0["est_net_shares"] == pytest.approx(round(uv0["est_net_shares"] * 0.25, 3))
    assert d_uv0["gross_val"] == pytest.approx(round(uv0["gross_val"] * 0.25, 2))
    assert d_uv0["est_net_val"] == pytest.approx(round(uv0["est_net_val"] * 0.25, 2))

    # No household/investor/equity name leaks in enriched Demo Mode sections
    demo_sections_txt = json.dumps({
        "ssn_model": demo_ov["ssn_model"],
        "unvested_grants": demo_ov["unvested_grants"],
        "growth_evidence": [a.get("growth_evidence") for a in demo_ov["simulator_assets"]],
    })
    for inv in (cfg.get("investors") or []):
        assert str(inv["name"]) not in demo_sections_txt
    assert str(cfg["household_name"]) not in demo_sections_txt
    assert str(cfg["primary_equity_symbol"]) not in demo_sections_txt


@pytest.mark.portfolio("p4_trust_household")
@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03")
def test_demo_mode_gemini_chat_no_pre_egress_block_unicode_and_dob_ym(root, cfg, payload, monkeypatch):
    """Demo Mode (upgrading Mode B -> Mode C) + Unicode stems & YYYY-MM DOBs pass Gemini pre-egress gate."""
    from privacy import PrivacyTranslator, load_privacy_config, save_privacy_config
    import llm_tools

    monkeypatch.setattr(llm_tools, "load_live_payload", lambda: payload)
    llm_tools.invalidate_payload_cache()
    fake_llm.install_config(root, "gemini")
    gemini_cfg_path = root / "Data" / "Config" / "LLM" / "gemini.json"
    gemini_cfg = json.loads(gemini_cfg_path.read_text(encoding="utf-8"))
    gemini_cfg["max_tool_rounds"] = 5
    gemini_cfg_path.write_text(json.dumps(gemini_cfg, indent=2) + "\n", encoding="utf-8")
    save_privacy_config({
        "ai_shield": {"mode": "identity_aliased", "birthday_shift_years": 3},
        "demo_mode": {"enabled": True, "scale_factor": 0.5},
    })

    # Exercise direct YYYY-MM, YYYY-MM-01, and prose YYYY-MM DOB masking under Mode C
    pcfg = load_privacy_config(auto_sync=True)
    pcfg["ai_shield"]["mode"] = "identity_and_values"
    tr = PrivacyTranslator(pcfg)
    sample_dob = next(iter(tr._known_dobs))
    sample_ym = sample_dob[:7]
    aliased = tr.to_alias({
        "birth_iso": sample_ym,
        "statement_dob": f"{sample_ym}-01",
        "owner_birth_date": sample_dob,
        "summary": f"Born {sample_dob} ({sample_ym}-01 / {sample_ym}) in household.",
    })
    assert tr.find_leaked_terms(aliased) == []

    # Exercise end-to-end Gemini multi-round tool calls on p4_trust_household (Zsófia Varga / Unicode)
    responder = fake_llm.scripted(
        fake_llm.tool_call_response("gemini", "holdings__get_overview", {}),
        fake_llm.tool_call_response("gemini", "holdings__run_simulation", {}),
        fake_llm.tool_call_response("gemini", "holdings__manage_saves", {"action": "list"}),
        fake_llm.text_response("gemini", "Summary complete."),
    )
    with fake_llm.recording(responder) as wire:
        res = llm_tools.run_chat_via_agy_cli(
            "Check Social Security and retirement milestones.",
            model="llm:gemini:test-model",
            allow_fast_path=False,
        )
    assert res.get("ok"), f"Gemini chat blocked or failed under Demo Mode: {res}"
    assert "Blocked by Privacy Shield" not in str(res)
    assert len(wire.requests) == 4
    leaked = wire.leaked_terms(_registry_terms(cfg))
    assert not leaked, f"registry terms leaked on Gemini wire: {leaked}"


@pytest.mark.portfolio("p5_adversarial")
@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03")
def test_unicode_person_stems_gemini_demo_mode_wire_clean(root, cfg, payload, monkeypatch):
    """Adversarial Unicode person stems (e.g., Håkon) in camelCase keys do not trip Gemini pre-egress."""
    from privacy import save_privacy_config
    import llm_tools

    monkeypatch.setattr(llm_tools, "load_live_payload", lambda: payload)
    llm_tools.invalidate_payload_cache()
    fake_llm.install_config(root, "gemini")
    gemini_cfg_path = root / "Data" / "Config" / "LLM" / "gemini.json"
    gemini_cfg = json.loads(gemini_cfg_path.read_text(encoding="utf-8"))
    gemini_cfg["max_tool_rounds"] = 5
    gemini_cfg_path.write_text(json.dumps(gemini_cfg, indent=2) + "\n", encoding="utf-8")
    save_privacy_config({
        "ai_shield": {"mode": "identity_aliased", "birthday_shift_years": 3},
        "demo_mode": {"enabled": True, "scale_factor": 0.5},
    })

    responder = fake_llm.scripted(
        fake_llm.tool_call_response("gemini", "holdings__get_overview", {}),
        fake_llm.tool_call_response("gemini", "holdings__run_simulation", {}),
        fake_llm.text_response("gemini", "Adversarial check complete."),
    )
    with fake_llm.recording(responder) as wire:
        res = llm_tools.run_chat_via_agy_cli(
            "Summarize overview and simulation.",
            model="llm:gemini:test-model",
            allow_fast_path=False,
        )
    assert res.get("ok"), f"Gemini chat failed on p5_adversarial: {res}"
    assert "Blocked by Privacy Shield" not in str(res)
    assert not wire.leaked_terms(_registry_terms(cfg))


@pytest.mark.portfolio("p4_trust_household")
@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03")
def test_system_prompt_boilerplate_and_demo_pool_collision_scrubbed(root):
    """System prompt boilerplate is scrubbed and assign_demo_personas avoids codes/company/name collisions."""
    from privacy import PrivacyTranslator, assign_demo_personas, default_privacy_config, load_privacy_config
    from pathlib import Path
    import llm_tools

    pcfg = load_privacy_config(auto_sync=True)
    pcfg["ai_shield"]["mode"] = "identity_aliased"
    pcfg["entities"].append({
        "token": "INVESTOR_9",
        "category": "investor",
        "canonical": "Advisor",
        "synonyms": ["Advisor"],
        "enabled": True,
        "meta": {"first": "Advisor"},
    })
    tr = PrivacyTranslator(pcfg)
    prompt = llm_tools._build_api_system_prompt("UI ok", shield_active=True, translator=tr)
    assert tr.find_leaked_terms(prompt) == []
    llm_tools._assert_wire_leak_free(tr, {"systemInstruction": {"parts": [{"text": prompt}]}}, "Gemini chat")

    # Verify custodian codes, equity company/plan, and fund name collisions are filtered against pools
    pools_path = Path(llm_tools.__file__).resolve().parent / "privacy" / "demo_pools.json"
    pools = json.loads(pools_path.read_text(encoding="utf-8"))
    blocked_cust_terms = [
        str((c.get("codes") or [c["short"]])[0])
        for c in pools["custodians"][:-1]
    ]
    expected_cust = pools["custodians"][-1]
    blocked_eq_terms = [
        str(e.get("company") or e["symbol"])
        for e in pools["equities"][:-1]
    ]
    expected_eq = pools["equities"][-1]
    blocked_fund_terms = [
        str(f.get("name") or f["symbol"])
        for f in pools["funds"][:-1]
    ]
    expected_fund = pools["funds"][-1]

    dcfg = default_privacy_config()
    dcfg["entities"] = [
        {
            "token": "BANK_1",
            "category": "custodian",
            "canonical": "Unrelated Bank",
            "synonyms": ["Unrelated Bank", *blocked_cust_terms],
            "enabled": True,
        },
        {
            "token": "STOCK_1",
            "category": "equity",
            "canonical": "UNREL",
            "synonyms": ["UNREL", *blocked_eq_terms],
            "enabled": True,
        },
        {
            "token": "FUND_1",
            "category": "fund",
            "canonical": "UNRF",
            "synonyms": ["UNRF", *blocked_fund_terms],
            "enabled": True,
        },
    ]
    updated = assign_demo_personas(dcfg["entities"], demo_seed="collision-test")
    by_tok = {e["token"]: e for e in updated}
    assert by_tok["BANK_1"]["demo_value"] == expected_cust["name"]
    assert by_tok["STOCK_1"]["demo_value"] == expected_eq["symbol"]
    assert by_tok["FUND_1"]["demo_value"] == expected_fund["symbol"]


@pytest.mark.portfolio("p4_trust_household")
@pytest.mark.feature("privacy_shield")
@pytest.mark.regression("SA-03")
def test_gemini_round2_thought_signature_and_model_turn_scrubbed(root, monkeypatch):
    """Base64 thoughtSignature substrings do not false-trigger pre-egress gate; non-base64 leaks block; Round 1 model prose is scrubbed."""
    from privacy import PrivacyTranslator, load_privacy_config
    import llm_tools

    pcfg = load_privacy_config(auto_sync=True)
    pcfg["ai_shield"]["mode"] = "identity_aliased"
    pcfg["entities"].append({
        "token": "STOCK_9",
        "category": "equity",
        "canonical": "V",
        "synonyms": ["V"],
        "enabled": True,
    })
    tr = PrivacyTranslator(pcfg)

    # 1. Valid base64 thoughtSignature containing '+V/' must not trigger pre-egress block
    valid_sig = "AbCdEfGhIjKl+V/MnOpQrStUvWxYz0123456789=="
    assert llm_tools._assert_wire_leak_free(
        tr,
        {"contents": [{"role": "model", "parts": [{"thoughtSignature": valid_sig, "text": "Checking"}]}]},
        "Gemini chat",
    ) is None

    # 2. Non-base64 text inside thoughtSignature must still be blocked fail-closed
    bad_sig = llm_tools._assert_wire_leak_free(
        tr,
        {"contents": [{"role": "model", "parts": [{"thoughtSignature": "Checking ticker V directly"}]}]},
        "Gemini chat",
    )
    assert bad_sig is not None and bad_sig.get("ok") is False

    # 3. End-to-end 2-round Gemini chat where Round 1 returns thoughtSignature ('+V/') + raw term in text/args
    seen_bodies = []
    responses = [
        {
            "candidates": [{
                "content": {
                    "parts": [
                        {"thought": True, "text": "Checking V position"},
                        {"functionCall": {"name": "holdings__get_overview", "args": {"note": "V"}}, "thoughtSignature": valid_sig},
                    ]
                }
            }]
        },
        {
            "candidates": [{
                "content": {"parts": [{"text": "STOCK_9 reviewed."}]}
            }]
        },
    ]

    def _fake_post(_url, _headers, payload, **_kw):
        seen_bodies.append(json.loads(json.dumps(payload)))
        return responses[len(seen_bodies) - 1]

    monkeypatch.setattr(llm_tools, "_http_post_json", _fake_post)
    res = llm_tools._run_chat_gemini_api(
        "Review my portfolio",
        {"provider": "gemini", "model": "gemini-4-hydrogen", "api_key": "fake-key"},
        "",
        0,
        translator=tr,
    )
    assert res.get("ok") is True, res
    assert len(seen_bodies) == 2
    model_parts = seen_bodies[1]["contents"][1]["parts"]
    assert model_parts[0]["text"] == "Checking STOCK_9 position"
    assert model_parts[1]["functionCall"]["args"]["note"] == "STOCK_9"
    assert model_parts[1]["thoughtSignature"] == valid_sig

    # 4. Diagnostics metadata is attached without raw terms for both pre-egress and CLI blocks
    bad_diag = bad_sig.get("diagnostics") or {}
    assert bad_diag.get("gate") == "pre_egress_leak_gate"
    assert "contents[0].parts[0].thoughtSignature" in (bad_diag.get("leak_fields") or [])
    assert any(c.startswith("equity(len=1)") for c in (bad_diag.get("leak_categories") or []))

    cli_block = llm_tools.run_chat_via_agy_cli("hello", model="default", allow_fast_path=False)
    assert cli_block.get("ok") is False
    cli_diag = cli_block.get("diagnostics") or {}
    assert cli_diag.get("gate") == "cli_shield"
    assert cli_diag.get("requested_model") == "default"
    assert cli_diag.get("is_explicit_cli") is True


@pytest.mark.feature("privacy_shield")
def test_max_tool_rounds_triggers_synthesis_and_forwards_history(monkeypatch):
    """Exhausting max_tool_rounds runs a no-tool synthesis turn, and prior chat history is aliased & forwarded."""
    import llm_tools
    from privacy import build_translator

    tr = build_translator({
        "ai_shield": {"mode": "identity_aliased"},
        "entities": [
            {"token": "INVESTOR_1", "category": "investor", "canonical": "Jordan Mercer", "synonyms": ["Jordan Mercer", "Jordan"], "enabled": True},
            {"token": "STOCK_1", "category": "equity", "canonical": "ACME Corp", "synonyms": ["ACME Corp", "ACME"], "enabled": True},
        ],
    })

    seen_bodies: list[dict] = []

    def _fake_post(_url, _headers, payload, **_kw):
        seen_bodies.append(json.loads(json.dumps(payload)))
        if len(seen_bodies) == 1:
            return {
                "candidates": [{
                    "content": {
                        "parts": [{"functionCall": {"name": "holdings__get_overview", "args": {}}}]
                    }
                }]
            }
        return {
            "candidates": [{
                "content": {
                    "parts": [{"text": "INVESTOR_1 holds STOCK_1 and should target $1.2M."}]
                }
            }]
        }

    monkeypatch.setattr(llm_tools, "_http_post_json", _fake_post)
    monkeypatch.setattr(
        llm_tools,
        "_exec_shielded_tool",
        lambda name, args, _tr: (args, {"ok": True, "summary": "ok"}),
    )

    res = llm_tools._run_chat_gemini_api(
        "Follow-up for Jordan on ACME",
        {"provider": "gemini", "model": "gemini-4-hydrogen", "api_key": "fake-key", "max_tool_rounds": 1},
        "",
        0,
        translator=tr,
        history=[
            {"role": "assistant", "content": "Ignored leading assistant msg"},
            {"role": "user", "content": "Initial question from Jordan"},
            {"role": "assistant", "content": "Completed."},
        ],
    )
    assert res["ok"] is True, res
    assert len(seen_bodies) == 2
    # Prior history aliased and strictly alternating before final user prompt
    round0_roles = [c["role"] for c in seen_bodies[0]["contents"]]
    assert round0_roles == ["user", "model", "user"]
    round0_wire = json.dumps(seen_bodies[0])
    assert "Jordan" not in round0_wire and "ACME" not in round0_wire
    assert "INVESTOR_1" in round0_wire and "STOCK_1" in round0_wire
    # Second request is the forced synthesis turn with function calling disabled
    assert (seen_bodies[1].get("toolConfig") or {}).get("functionCallingConfig", {}).get("mode") == "NONE"
    assert "Jordan Mercer holds ACME Corp" in res["reply"]
    assert res["reply"] != "Completed."
