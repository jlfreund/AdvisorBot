#!/usr/bin/env python3
"""
Pass-2 Design Audit (`DA2-01`..`DA2-19`) and Pass-5 Security Audit (`SA3-01`..`SA3-14`)
regression tests locking down coupled security, privacy, ingest, and simulator invariants.
"""

from __future__ import annotations

import json
import pytest

import config as cfg_mod
from ingest.errors import StatementParseError
from ingest.framework import IngestContext
from ingest.plugins.canonical_json import CanonicalStatementJsonPlugin
from privacy.demo_projector import apply_demo_projection_to_payload
from privacy.registry import default_privacy_config, load_privacy_config, save_privacy_config
from privacy.translator import build_translator
from tests.support import pipeline as pipeline_support
from valuation import simulator

pytestmark = pytest.mark.portfolio("p3_retiree_annuities")


@pytest.mark.security
@pytest.mark.regression("SA3-01")
def test_demo_mode__blocks_privacy_subroutes_and_protects_entity_registry(sidecar):
    """Demo Mode blocks `/api/privacy/*` subroutes, blocks entity clobbering, and only permits exit."""
    sidecar.authenticate()

    orig_priv = load_privacy_config("p3_retiree_annuities")
    orig_canonicals = {e["token"]: e["canonical"] for e in orig_priv.get("entities", []) if e.get("canonical")}
    assert orig_canonicals, "fixture should discover canonical entities"

    en_resp = sidecar.post(
        "/api/privacy",
        body={"demo_mode": {"enabled": True, "scale_factor": 0.5, "demo_seed": "custom-seed-99"}},
    )
    assert en_resp.status == 200, en_resp.text[:300]
    try:
        en_body = en_resp.json()
        assert en_body.get("ok") is True
        for ent in en_body["privacy"].get("entities", []):
            assert ent["canonical"] == "[Hidden in Demo Mode]"

        # 1. `/api/privacy/sync` and `/api/privacy/preview` MUST be rejected with 403 in Demo Mode
        sync_resp = sidecar.post("/api/privacy/sync", body={})
        assert sync_resp.status == 403, sync_resp.text[:200]
        prev_resp = sidecar.post("/api/privacy/preview", body={"sample_text": "Hello"})
        assert prev_resp.status == 403, prev_resp.text[:200]

        # 2. Attempting to save redacted `[Hidden in Demo Mode]` entities or change privacy settings in Demo Mode MUST 403
        mut_resp = sidecar.post(
            "/api/privacy",
            body={
                "demo_mode": {"enabled": True, "scale_factor": 0.25},
                "entities": en_body["privacy"]["entities"],
            },
        )
        assert mut_resp.status == 403, mut_resp.text[:200]

        # 3. Direct defense-in-depth check on `save_privacy_config`: sentinel canonicals never overwrite disk
        saved_after = save_privacy_config(
            {"demo_mode": {"demo_seed": "custom-seed-99"}, "entities": en_body["privacy"]["entities"]},
            "p3_retiree_annuities",
        )
        assert saved_after["privacy"]["demo_mode"]["demo_seed"] == "custom-seed-99"
        disk_priv = load_privacy_config("p3_retiree_annuities")
        disk_canonicals = {e["token"]: e["canonical"] for e in disk_priv.get("entities", [])}
        for tok, canon in orig_canonicals.items():
            assert disk_canonicals.get(tok) == canon
    finally:
        off_resp = sidecar.post("/api/privacy", body={"demo_mode": {"enabled": False}})
        assert off_resp.status == 200, off_resp.text[:200]


@pytest.mark.security
@pytest.mark.regression("SA3-02")
def test_demo_mode__blocks_static_statements_on_http(sidecar):
    """Static `/Statements/*` routes return 403 while Demo Mode is active."""
    sidecar.authenticate()
    stmt_dir = cfg_mod.get_statements_dir()
    stmt_files = sorted(p for p in stmt_dir.rglob("*") if p.is_file())
    assert stmt_files, "fixture should have statement files"
    rel_stmt = stmt_files[0].relative_to(stmt_dir).as_posix()

    sidecar.post("/api/privacy", body={"demo_mode": {"enabled": True}})
    try:
        resp = sidecar.get(f"/Statements/{rel_stmt}")
        assert resp.status == 403
    finally:
        sidecar.post("/api/privacy", body={"demo_mode": {"enabled": False}})


@pytest.mark.security
@pytest.mark.regression("SA3-05")
def test_exported_payload__trims_all_birth_days_to_first_of_month(payload):
    """Exported `retirement_sim_assets[*].owner_birth_day` and investor DOBs never expose exact calendar days."""
    assert payload.get("retirement_sim_assets")
    for a in payload.get("retirement_sim_assets", []):
        if "owner_birth_day" in a and a["owner_birth_day"] is not None:
            assert int(a["owner_birth_day"]) == 1, a
    for inv in payload.get("investors", []):
        assert int(inv.get("birth_day", 1)) == 1


@pytest.mark.security
@pytest.mark.regression("SA3-06")
def test_privacy_translator__roundtrips_cash_and_acct_composite_ids_and_non_monetary_counts(root):
    """`PrivacyTranslator` preserves `cash_<slug>` and `acct_<id>` round-trips and leaves cohort/tranche counts unscaled."""
    cfg = default_privacy_config()
    cfg["ai_shield"] = {"mode": "identity_and_values", "scale_factor": 0.5, "birthday_shift_years": 2}
    cfg["entities"] = [
        {
            "token": "ACCT_1",
            "category": "account",
            "canonical": "HB-7001",
            "synonyms": ["HB-7001", "HB_7001"],
            "enabled": True,
        }
    ]
    tr = build_translator(cfg)
    wire = tr.to_alias(
        {
            "from_asset_id": "cash_HB_7001",
            "to_asset_id": "acct_HB-7001",
            "note": "Move from single_HB-7001_VTI into cash_HB_7001",
            "success_to_ms_n": 58,
            "vested_tranches": 12,
            "bend1": 1286,
            "current_value": 100000,
        }
    )
    assert "HB-7001" not in json.dumps(wire)
    assert "HB_7001" not in json.dumps(wire)
    assert wire["success_to_ms_n"] == 58
    assert wire["vested_tranches"] == 12
    assert wire["bend1"] == 1286
    assert wire["current_value"] == 50000

    restored = tr.from_alias(wire)
    assert restored["from_asset_id"] == "cash_HB_7001"
    assert restored["to_asset_id"] == "acct_HB-7001"


@pytest.mark.security
@pytest.mark.regression("SA3-08")
def test_demo_projector__scales_surrender_value_and_preserves_pct_of_source():
    """`DemoTranslator` scales `surrender_value` and `*_usd` keys by `k` while keeping `pct_of_source` amounts unscaled."""
    cfg = default_privacy_config()
    cfg["demo_mode"] = {"enabled": True, "scale_factor": 0.25, "round_to": 1000}
    projected = apply_demo_projection_to_payload(
        {
            "holdings_by_account": [
                {
                    "id": "acct_1",
                    "holdings": [{"id": "ann_1", "value": 200000, "surrender_value": 180000}],
                }
            ],
            "simScheduledActions": [
                {"id": "act_1", "type": "transfer", "amount_mode": "pct_of_source", "amount": 50},
                {"id": "act_2", "type": "withdrawal", "amount_mode": "dollar", "amount": 40000},
            ],
        },
        cfg,
    )
    h0 = projected["holdings_by_account"][0]["holdings"][0]
    assert h0["surrender_value"] == 45000
    acts = projected["simScheduledActions"]
    assert acts[0]["amount"] == 50
    assert acts[1]["amount"] == 10000


@pytest.mark.regression("DA2-01")
def test_cash_sweep_buckets__preserve_cash_fixed_risk_class_and_sweep_yield(payload):
    """Retirement cash-sweep buckets (`cash_*`) retain `cash_fixed` risk class rather than equity growth."""
    cash_buckets = [a for a in payload.get("retirement_sim_assets", []) if str(a.get("id", "")).startswith("cash_")]
    assert cash_buckets, "fixture should include at least one cash sweep bucket"
    for cb in cash_buckets:
        assert cb.get("default_risk_class") == "cash_fixed", cb
        assert float(cb.get("default_growth_pct") or 0) <= 6, cb


@pytest.mark.regression("DA2-04")
def test_hypothetical_rollovers__honor_partial_amount_and_wrapper_tax_rules(payload):
    """Day-0 `hypothetical_rollovers` honor partial `amount`, 0% IRA-to-IRA rollovers, and ordinary Roth conversions."""
    p_copy = pipeline_support.payload_copy(payload)
    sim_assets = p_copy["retirement_sim_assets"]
    ira_buckets = [a for a in sim_assets if a.get("tax_model") == "ORDINARY_FULL" and a.get("current_value", 0) > 50000]
    tax_bucket = next((a for a in sim_assets if a.get("tax_model") != "ORDINARY_FULL"), None)
    assert ira_buckets and tax_bucket is not None
    ira_bucket = ira_buckets[0]

    new_assets, _loss_cf, summary = simulator.apply_hypothetical_rollovers(
        sim_assets,
        [
            {
                "from_asset_id": ira_bucket["id"],
                "to_asset_id": tax_bucket["id"],
                "sell_all": False,
                "amount": 50000,
                "pay_tax_from_proceeds": True,
            }
        ],
        payload=p_copy,
    )
    r0 = summary[0]
    assert r0["gross_sold_today"] == 50000
    assert r0["immediate_tax_paid"] > 0
    assert r0["net_rolled_into_dest"] < 50000
    updated_ira = next(a for a in new_assets if a["id"] == ira_bucket["id"])
    assert updated_ira["current_value"] > 0

    if len(ira_buckets) >= 2:
        p_same = pipeline_support.payload_copy(payload)
        _, _, same_sum = simulator.apply_hypothetical_rollovers(
            p_same["retirement_sim_assets"],
            [
                {
                    "from_asset_id": ira_buckets[0]["id"],
                    "to_asset_id": ira_buckets[1]["id"],
                    "sell_all": False,
                    "amount": 50000,
                    "pay_tax_from_proceeds": True,
                }
            ],
            payload=p_same,
        )
        assert same_sum[0]["immediate_tax_paid"] == 0


@pytest.mark.regression("DA2-13")
def test_canonical_json_plugin__rejects_mismatched_reported_total(tmp_path):
    """`CanonicalStatementJsonPlugin` raises `StatementParseError` when `reported_total` disagrees with positions."""
    bad_stmt = tmp_path / "mismatch.statement.json"
    bad_stmt.write_text(
        json.dumps(
            {
                "format": "advisorbot.statement/1",
                "institution": "Synthetic Bank",
                "account_id": "HB-7001",
                "as_of_date": "2026-06-30",
                "reported_total": 250000,
                "positions": [
                    {
                        "security_id": "TICKER:VTI",
                        "symbol_or_code": "VTI",
                        "canonical_name": "Total Stock Market ETF",
                        "asset_class": "EQUITY_ETF",
                        "quantity": 100,
                        "market_value": 100000,
                        "risk_class": "broad_equity",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plugin = CanonicalStatementJsonPlugin()
    ctx = IngestContext(file_path=bad_stmt, rel_path="mismatch.statement.json")
    assert plugin.detect(ctx) > 0.9
    with pytest.raises(StatementParseError, match="reported_total checksum mismatch"):
        plugin.parse(ctx)
