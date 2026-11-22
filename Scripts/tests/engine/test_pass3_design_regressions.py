"""Pass 3 design-audit regression tests (DA4-01/D11, DA4-02, DA4-04, DA4-05, DA4-06, DA4-08/D10, DA4-14, SA5-02b)."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

import settings as settings_mod
from db.database import PortfolioDB
from exporters import view_model as vm
from privacy.registry import PrivacyConfigError
from valuation import simulator as sim


@pytest.mark.feature("rmd")
def test_hypothetical_ira_rollover__inherits_younger_spouse_owner_and_skips_premature_rmd():
    """DA4-01/D11: Rolling a younger spouse's 401k into a hypothetical Traditional IRA preserves their birth year and $0 premature RMD."""
    payload = {
        "today_iso": "2026-11-15",
        "project": {
            "investors": [
                {"id": "alex", "name": "Alex Rivera", "birth_date": "1952-03-01", "is_rmd_owner": True},
                {"id": "jordan", "name": "Jordan Rivera", "birth_date": "1966-06-01", "is_rmd_owner": False},
            ],
            "tax_rates": {"state_rate": 0.0, "ordinary_top": 0.37, "ltcg_top": 0.238, "niit_rate": 0.038},
        },
        "retirement_sim_assets": [
            {
                "id": "plan_401k_jordan",
                "account_id": "HB-7010",
                "rmd_owner_index": 1,
                "owner_id": "jordan",
                "owner_birth_year": 1966,
                "owner_birth_month": 6,
                "owner_birth_day": 1,
                "tax_wrapper": "401K",
                "tax_model": "ORDINARY_FULL",
                "tax_rate": 0.332,
                "default_rank": 35,
                "bucket": "non_goog",
                "current_value": 200000.0,
                "cost_basis": 0.0,
                "default_growth_pct": 0.0,
                "default_risk_class": "broad_equity",
            },
            {
                "id": "cash_sweeps",
                "account_id": "HB-7001",
                "tax_wrapper": "TAXABLE",
                "tax_model": "CASH",
                "tax_rate": 0.0,
                "default_rank": 45,
                "bucket": "non_goog",
                "current_value": 50000.0,
                "cost_basis": 50000.0,
                "default_growth_pct": 0.0,
                "default_risk_class": "cash_fixed",
            },
        ],
    }
    overrides = {
        "simAnnualBurn": 0,
        "simIncludeSSN": False,
        "simHypotheticalAssets": [
            {
                "id": "hyp_rollover_ira",
                "label": "Rollover IRA",
                "institution": "Harbor Brokerage",
                "real_product_ref": "Broad Index ETF",
                "growth_pct": 0.0,
                "risk_class": "broad_equity",
                "tax_wrapper": "trad_ira",
            }
        ],
        "simScheduledActions": [
            {
                "id": "roll_1",
                "type": "transfer",
                "cadence": "one_time",
                "ym": "2026-12",
                "amount_mode": "pct_of_source",
                "amount": 100.0,
                "from_asset_id": "plan_401k_jordan",
                "to_asset_id": "hyp_rollover_ira",
            }
        ],
    }
    eff = sim.build_effective_settings(payload, overrides=overrides)
    mat = sim.materialize_sim_assets(payload["retirement_sim_assets"], eff, payload)
    hyp = next(a for a in mat if a["id"] == "hyp_rollover_ira")
    assert hyp["rmd_owner_index"] == 1
    assert hyp["owner_birth_year"] == 1966
    assert hyp["owner_birth_month"] == 6

    res = sim.run_simulation(payload, eff)
    jan_2027 = next(b for b in res["bars"] if b["iso"].startswith("2027-01"))
    assert jan_2027["assetVals"]["hyp_rollover_ira"] == pytest.approx(200000.0)
    assert jan_2027["cumTax"] == pytest.approx(0.0)


@pytest.mark.feature("rmd")
def test_hypothetical_ira__explicit_owner_pools_with_existing_owner_iras_for_january_rmd():
    """DA4-01/D11: A hypothetical Traditional IRA with explicit owner metadata pools under ('owner', idx, 'IRA') for January RMDs."""
    payload = {
        "today_iso": "2025-12-15",
        "project": {
            "investors": [
                {"id": "alex", "name": "Alex Rivera", "birth_date": "1953-04-01", "is_rmd_owner": True},
                {"id": "jordan", "name": "Jordan Rivera", "birth_date": "1968-08-01", "is_rmd_owner": False},
            ],
            "tax_rates": {"state_rate": 0.0, "ordinary_top": 0.37, "ltcg_top": 0.238, "niit_rate": 0.038},
        },
        "retirement_sim_assets": [
            {
                "id": "ira_liquid",
                "account_id": "HB-7020",
                "rmd_owner_index": 0,
                "owner_id": "alex",
                "owner_birth_year": 1953,
                "owner_birth_month": 4,
                "tax_wrapper": "TRAD_IRA",
                "tax_model": "ORDINARY_FULL",
                "tax_rate": 0.0,
                "default_rank": 35,
                "bucket": "non_goog",
                "current_value": 132500.0,
                "cost_basis": 0.0,
            },
            {
                "id": "ira_source",
                "account_id": "HB-7021",
                "rmd_owner_index": 0,
                "owner_id": "alex",
                "owner_birth_year": 1953,
                "owner_birth_month": 4,
                "tax_wrapper": "TRAD_IRA",
                "tax_model": "ORDINARY_FULL",
                "tax_rate": 0.0,
                "default_rank": 36,
                "bucket": "non_goog",
                "current_value": 132500.0,
                "cost_basis": 0.0,
            },
        ],
    }
    canon = settings_mod.canonicalize_model_settings(
        {
            "simHypotheticalAssets": [
                {
                    "id": "hyp_locked_ira",
                    "label": "Locked MYGA IRA",
                    "growth_pct": 0.0,
                    "risk_class": "cash_fixed",
                    "tax_wrapper": "trad_ira",
                    "unlock_ym": "2030-01",
                    "owner_id": "Alex Rivera",
                }
            ]
        },
        cfg=payload["project"],
    )
    assert canon["simHypotheticalAssets"][0]["owner_id"] == "Alex Rivera"
    assert canon["simHypotheticalAssets"][0]["rmd_owner_index"] is None

    eff = sim.build_effective_settings(payload, canon, overrides={"simAnnualBurn": 0, "simIncludeSSN": False})
    mat = sim.materialize_sim_assets(payload["retirement_sim_assets"], eff, payload)
    rolled, _, _ = sim.apply_hypothetical_rollovers(
        mat,
        [{"from_id": "ira_source", "to_id": "hyp_locked_ira", "sell_all": True}],
        payload=payload,
        settings=eff,
    )
    hyp_ira = next(a for a in rolled if a["id"] == "hyp_locked_ira")
    assert hyp_ira["rmd_owner_index"] == 0
    assert hyp_ira["owner_birth_year"] == 1953

    order = [a["id"] for a in rolled]
    state = sim._clone_sim_asset_state(rolled, "2025-12-15", True)
    step = sim._step_sim_month(
        rolled,
        state,
        "2026-01-15",
        "Jan 2026",
        0.0,
        0.0,
        0.0,
        True,
        order,
        asset_monthly_rates={aid: 0.0 for aid in order},
    )
    # Age 73 ULT divisor is 26.5 -> (132500 + 132500) / 26.5 = 10000 satisfied wholly from unlocked `ira_liquid`
    assert step["monthlyRmdGross"] == pytest.approx(10000.0)
    assert state["ira_liquid"]["val"] == pytest.approx(122500.0)
    assert state["hyp_locked_ira"]["val"] == pytest.approx(132500.0)


@pytest.mark.feature("wrappers")
def test_strategy_rollups__separate_multi_account_sleeves_and_keep_ira_stocks_wrapped():
    """DA4-02: Multi-account taxable equity/muni sleeves emit distinct per-account rollups while >5-stock IRAs stay wrapped."""
    accounts = {
        "HB-7001": {"institution": "Harbor", "account_type": "Brokerage", "tax_status": "TAXABLE"},
        "HB-7002": {"institution": "Harbor", "account_type": "Brokerage", "tax_status": "TAXABLE"},
        "HB-7003": {"institution": "Harbor", "account_type": "Traditional IRA", "tax_status": "TRAD_IRA"},
        "HB-7004": {"institution": "Harbor", "account_type": "Roth IRA", "tax_status": "ROTH_IRA"},
    }

    def _eq_item(aid: str, idx: int) -> dict:
        sym = f"EQ{idx}"
        return {
            "account_id": aid,
            "security_id": f"TICKER:{sym}",
            "symbol": sym,
            "quote_symbol": sym,
            "name": f"Equity {sym}",
            "asset_class": "EQUITY",
            "is_scrapeable": True,
            "quantity": 10.0,
            "stmt_quantity": 10.0,
            "current_price": 100.0,
            "stmt_price": 100.0,
            "stmt_value": 1000.0,
            "current_value": 1000.0,
            "wow_change": 0.0,
            "eval_date": "2026-11-15",
            "stmt_date": "2026-10-31",
            "maturity_date": None,
            "maturity_str": None,
            "source_tooltip": "Quote",
            "principal_val": 800.0,
            "principal_date": "2024-01-15",
            "cagr_pct": 10.0,
            "principal_str": "$800",
            "principal_sub": "Basis",
            "principal_tooltip": "Basis",
            "growth_str": "+10%",
            "growth_sub": "Gain",
            "growth_sign": 1,
            "growth_tooltip": "Growth",
            "pct_portfolio": 1.0,
            "pct_tooltip": "1%",
            "meta": {},
        }

    def _muni_item(aid: str, idx: int) -> dict:
        cusip = f"MUNI{aid[-1]}{idx}"
        return {
            "account_id": aid,
            "security_id": f"CUSIP:{cusip}",
            "symbol": cusip,
            "quote_symbol": None,
            "name": f"Muni Bond {cusip}",
            "asset_class": "MUNI_BOND",
            "is_scrapeable": False,
            "quantity": 5000.0,
            "stmt_quantity": 5000.0,
            "current_price": 100.0,
            "stmt_price": 100.0,
            "stmt_value": 5000.0,
            "current_value": 5000.0,
            "accrued_interest": 25.0,
            "yield_pct": 4.0,
            "wow_change": 0.0,
            "eval_date": "2026-10-31",
            "stmt_date": "2026-10-31",
            "maturity_date": "2032-06-01",
            "maturity_str": "Matures 2032-06-01",
            "source_tooltip": "Statement",
            "principal_val": 5000.0,
            "principal_date": "2024-01-15",
            "cagr_pct": 4.0,
            "principal_str": "$5,000",
            "principal_sub": "Par",
            "principal_tooltip": "Par",
            "growth_str": "4.0% Yld",
            "growth_sub": "Par",
            "growth_sign": 1,
            "growth_tooltip": "Yield",
            "pct_portfolio": 2.0,
            "pct_tooltip": "2%",
            "meta": {},
        }

    items = [
        *[_eq_item("HB-7001", i) for i in range(6)],
        *[_eq_item("HB-7002", i + 10) for i in range(6)],
        *[_eq_item("HB-7003", i + 20) for i in range(6)],
        *[_eq_item("HB-7004", i + 30) for i in range(6)],
        _muni_item("HB-7001", 1),
        _muni_item("HB-7001", 2),
        _muni_item("HB-7002", 1),
        _muni_item("HB-7002", 2),
    ]
    rollups = vm._build_strategy_rollups(items, accounts, 44000.0, "2026-11-15", {}, cfg={})
    sma_groups = [g for g in rollups if str(g.get("security_id") or "").startswith("SMA_")]
    muni_groups = [g for g in rollups if str(g.get("security_id") or "").startswith("MUNIS_")]

    assert [g["account_id"] for g in sma_groups] == ["HB-7001", "HB-7002"]
    assert all(len(g["children"]) == 6 and g["current_value"] == pytest.approx(6000.0) for g in sma_groups)

    assert [g["account_id"] for g in muni_groups] == ["HB-7001", "HB-7002"]
    assert all(len(g["children"]) == 2 and g["current_value"] == pytest.approx(10000.0) for g in muni_groups)

    ira_singles = [g for g in rollups if g.get("account_id") == "HB-7003"]
    roth_singles = [g for g in rollups if g.get("account_id") == "HB-7004"]
    assert len(ira_singles) == 6
    assert len(roth_singles) == 6

    sim_assets = vm._build_retirement_sim_assets(
        {"accounts": accounts},
        rollups,
        {"by_grant": [], "release_bars": []},
        100.0,
        "2026-11-15",
        cfg={"primary_equity_symbol": "ACME", "investors": [{"id": "alex", "name": "Alex", "birth_date": "1970-01-01"}]},
    )
    ira_buckets = [a for a in sim_assets if a.get("account_id") == "HB-7003"]
    roth_buckets = [a for a in sim_assets if a.get("account_id") == "HB-7004"]
    assert len(ira_buckets) == 6 and all(a["tax_model"] == "ORDINARY_FULL" and a["tax_wrapper"] == "TRAD_IRA" for a in ira_buckets)
    assert len(roth_buckets) == 6 and all(a["tax_model"] == "TAX_FREE" and a["tax_wrapper"] == "ROTH_IRA" for a in roth_buckets)


@pytest.mark.feature("simulator")
def test_scheduled_actions_person_age_normalization__supports_prefix_fractional_fallback_without_crash():
    """DA4-04: Scheduled action normalization resolves exact and prefix family names, fractional ages, TBD fallbacks, and unknown names safely."""
    birthdays = {"Alex Rivera": "1970-04", "Sam Chen": "2005-09"}
    raw = [
        {"id": "exact", "type": "withdrawal", "cadence": "one_time", "person": "Alex Rivera", "age": 65, "amount": 5000},
        {"id": "prefix_frac", "type": "withdrawal", "cadence": "one_time", "person": "alex", "age": 59.5, "amount": 5000},
        {"id": "ym_wins", "type": "withdrawal", "cadence": "one_time", "ym": "2028-06", "person": "Alex", "age": 65, "amount": 5000},
        {"id": "tbd_fb", "type": "withdrawal", "cadence": "one_time", "ym": "TBD", "person": "sam", "age": 25, "amount": 5000},
        {"id": "unknown_one_time", "type": "withdrawal", "cadence": "one_time", "person": "Ghost", "age": 60, "amount": 5000},
        {"id": "unknown_recurring", "type": "withdrawal", "cadence": "annual", "person": "Ghost", "age": 60, "amount": 5000},
    ]
    norm = {a["id"]: a for a in sim.normalize_scheduled_actions(raw, "2026-11-15", family_birthdays=birthdays)}
    assert "unknown_one_time" not in norm
    assert norm["exact"]["ym"] == "2035-04"
    assert norm["prefix_frac"]["ym"] == "2029-10"
    assert norm["ym_wins"]["ym"] == "2028-06"
    assert norm["tbd_fb"]["ym"] == "2030-09"
    assert norm["unknown_recurring"]["start_ym"] == "2026-12"


@pytest.mark.feature("simulator")
def test_optimizer_without_payload__preserves_active_project_context():
    """DA4-05: Calling optimize_sim_asset_order with payload=None preserves the active project's filing/tax context."""
    payload = {
        "today_iso": "2026-11-15",
        "project": {
            "primary_equity_symbol": "ACME",
            "investors": [
                {"id": "alex", "name": "Alex Rivera", "birth_date": "1964-06-01", "milestone_age": 85},
                {"id": "jordan", "name": "Jordan Rivera", "birth_date": "1966-06-01", "milestone_age": 85},
            ],
            "tax_rates": {"state_rate": 0.093, "married_filing_jointly": True},
        },
        "ssn_model": {"investors": []},
        "retirement_sim_assets": [
            {
                "id": "taxable_idx",
                "default_rank": 10,
                "bucket": "non_goog",
                "tax_wrapper": "TAXABLE",
                "tax_model": "LTCG_POOL",
                "tax_rate": 0.331,
                "current_value": 500000.0,
                "cost_basis": 300000.0,
                "default_growth_pct": 7.0,
                "default_risk_class": "broad_equity",
            },
            {
                "id": "trad_ira",
                "default_rank": 35,
                "bucket": "non_goog",
                "tax_wrapper": "TRAD_IRA",
                "tax_model": "ORDINARY_FULL",
                "tax_rate": 0.463,
                "owner_birth_year": 1964,
                "current_value": 400000.0,
                "cost_basis": 0.0,
                "default_growth_pct": 6.5,
                "default_risk_class": "broad_equity",
            },
        ],
    }
    eff = sim.build_effective_settings(payload, overrides={"simAnnualBurn": 60000, "simIncludeSSN": False})
    with_payload = sim.optimize_sim_asset_order(
        payload["retirement_sim_assets"], payload["today_iso"], eff, payload["ssn_model"], payload=payload
    )
    without_payload = sim.optimize_sim_asset_order(
        payload["retirement_sim_assets"], payload["today_iso"], eff, payload["ssn_model"], payload=None
    )
    assert without_payload == with_payload


@pytest.mark.feature("tax_models")
def test_pooled_rmd_tax_attribution_and_ordinary_deposit_basis():
    """DA4-06/DA4-14: Mandatory RMD funding living burn never leaks RMD tax onto a 0%-tax cash withdrawal, and IRA deposits add $0 basis."""
    assets = [
        {
            "id": "ira_alex",
            "account_id": "HB-7030",
            "rmd_owner_index": 0,
            "owner_birth_year": 1953,
            "tax_wrapper": "TRAD_IRA",
            "tax_model": "ORDINARY_FULL",
            "tax_rate": 0.25,
            "current_value": 106000.0,
            "cost_basis": 0.0,
        },
        {
            "id": "cash_sweeps",
            "account_id": "HB-7001",
            "tax_wrapper": "TAXABLE",
            "tax_model": "CASH",
            "tax_rate": 0.0,
            "current_value": 50000.0,
            "cost_basis": 50000.0,
        },
    ]
    # Case 1: rmd_net (3000) < target_net_burn (4000) -> 0% of RMD funds the 10000 cash withdrawal -> event taxPaid == 0
    actions = sim.normalize_scheduled_actions(
        [
            {"id": "dep_ira", "type": "deposit", "cadence": "one_time", "ym": "2026-01", "to_asset_id": "ira_alex", "amount": 5000, "dollar_mode": "nominal"},
            {"id": "w_gift", "type": "withdrawal", "cadence": "one_time", "ym": "2026-01", "from_asset_id": "auto", "amount": 10000, "dollar_mode": "nominal"},
        ],
        "2025-12-15",
    )
    state = sim._clone_sim_asset_state(assets, "2025-12-15", True)
    step = sim._step_sim_month(
        assets,
        state,
        "2026-01-15",
        "Jan 2026",
        0.0,
        0.0,
        4000.0,
        True,
        ["cash_sweeps", "ira_alex"],
        asset_monthly_rates={"ira_alex": 0.0, "cash_sweeps": 0.0},
        scheduled_actions=actions,
        include_scheduled_actions=True,
    )
    w_ev = next(e for e in step["schedActionEvents"] if e["id"] == "w_gift")
    assert step["monthlyRmdGross"] == pytest.approx(4000.0)
    assert step["monthlyRmdTax"] == pytest.approx(1000.0)
    assert w_ev["netAmt"] == pytest.approx(10000.0)
    assert w_ev["taxPaid"] == pytest.approx(0.0)
    assert state["ira_alex"]["val"] == pytest.approx(107000.0)
    assert state["ira_alex"]["costBasis"] == pytest.approx(0.0)

    # Case 2: rmd_net (3000) > target_net_burn (1000) -> 2000 of rmd_net funds event -> proportional RMD tax (1000 * 2/3)
    state2 = sim._clone_sim_asset_state(assets, "2025-12-15", True)
    step2 = sim._step_sim_month(
        assets,
        state2,
        "2026-01-15",
        "Jan 2026",
        0.0,
        0.0,
        1000.0,
        True,
        ["cash_sweeps", "ira_alex"],
        asset_monthly_rates={"ira_alex": 0.0, "cash_sweeps": 0.0},
        scheduled_actions=[a for a in actions if a["id"] == "w_gift"],
        include_scheduled_actions=True,
    )
    w_ev2 = step2["schedActionEvents"][0]
    assert w_ev2["netAmt"] == pytest.approx(10000.0)
    assert w_ev2["taxPaid"] == pytest.approx(1000.0 * (2000.0 / 3000.0))


@pytest.mark.feature("grants")
def test_intramonth_rsu_vesting__vests_in_calendar_month_and_keeps_exact_lot_date():
    """DA4-06: An RSU vesting on the 25th vests during the 10th-of-month step while preserving exact vestIso for 1-year holding checks."""
    assets = [
        {
            "id": "future_unvested_acme",
            "symbol": "ACME",
            "bucket": "goog",
            "is_unvested_goog": True,
            "is_goog_priced": True,
            "tax_model": "UNVESTED_GOOG",
            "tax_rate": 0.20,
            "current_value": 10000.0,
            "cost_basis": 10000.0,
            "tranches": [{"vest_date": "2026-12-25", "shares": 50.0, "gross_shares": 100.0, "current_value": 10000.0, "cost_basis": 10000.0}],
        },
        {
            "id": "cash_sweeps",
            "bucket": "non_goog",
            "tax_wrapper": "TAXABLE",
            "tax_model": "CASH",
            "tax_rate": 0.0,
            "current_value": 1000.0,
            "cost_basis": 1000.0,
        },
    ]
    actions = sim.normalize_scheduled_actions(
        [
            {
                "id": "div_vest",
                "type": "transfer",
                "cadence": "on_vest",
                "amount_mode": "pct_of_source",
                "amount": 50.0,
                "from_asset_id": "future_unvested_acme",
                "to_asset_id": "cash_sweeps",
            }
        ],
        "2026-11-10",
    )
    tax_cfg = sim.build_tax_schedule_cfg({"project": {"investors": [{"birth_date": "1975-01-01"}], "tax_rates": {"state_rate": 0.0}}})
    state = sim._clone_sim_asset_state(assets, "2026-11-10", True, tax_cfg=tax_cfg)
    assert state["future_unvested_acme"]["tranches"][0]["vested"] is False

    step = sim._step_sim_month(
        assets,
        state,
        "2026-12-10",
        "Dec 2026",
        0.0,
        0.0,
        0.0,
        True,
        ["future_unvested_acme", "cash_sweeps"],
        asset_monthly_rates={"future_unvested_acme": 0.0, "cash_sweeps": 0.0},
        scheduled_actions=actions,
        include_scheduled_actions=True,
    )
    lot = state["future_unvested_acme"]["tranches"][0]
    assert lot["vested"] is True
    assert lot["vestIso"] == "2026-12-25"
    assert step["schedTransferNom"] == pytest.approx(5000.0)
    assert state["_taxYtd"]["vest_ord"] == pytest.approx(20000.0)
    assert sim._is_short_term_lot(lot, "2027-12-25") is True
    assert sim._is_short_term_lot(lot, "2027-12-26") is False


@pytest.mark.feature("simulator")
def test_zero_percent_tier1_decays_to_equilibrium_and_ref_rates_reset_half_lives():
    """DA4-08/D10: A 0.00% Tier-1 forecast decays toward Tier-2/Tier-3 unless Flat or 0x multiplier, and use_ref_rates resets half-lives."""
    asset = {
        "id": "zero_t1_eq",
        "forecast_key": "zero_t1_eq",
        "default_risk_class": "broad_equity",
        "default_growth_pct": 0.0,
        "ref_growth_pct": 0.0,
        "growth_evidence": [
            {"tier": 1, "projected_return_pct": 0.0},
            {"tier": 2, "projected_return_pct": 7.0},
            {"tier": 3, "projected_return_pct": 8.5},
        ],
    }
    assert sim._single_holding_curve_at(asset, 0.0, 5, 5) > 3.0
    assert sim._single_holding_curve_at(asset, 0.0, 31, 5) == pytest.approx(0.0)
    assert sim._holding_return_curve(asset, 0.0, 5)(5) > 3.0

    adj_active = sim._build_year_rate_adjuster([asset], {"simOptimismMultiplier": 1.0}, "ACME")
    assert adj_active({"zero_t1_eq": 0.0}, 5)["zero_t1_eq"] > 0.0

    adj_zero_mult = sim._build_year_rate_adjuster([asset], {"simOptimismMultiplier": 0.0}, "ACME")
    assert adj_zero_mult({"zero_t1_eq": 0.0}, 5)["zero_t1_eq"] == pytest.approx(0.0)

    payload = {
        "today_iso": "2026-11-15",
        "project": {"primary_equity_symbol": "ACME", "investors": [{"id": "alex", "name": "Alex", "birth_date": "1975-01-01"}]},
        "bucket_summary": {"goog": {"five_yr_cagr_pct": 11.0}, "non_goog": {"annual_appreciation_pct": 7.5}},
        "retirement_sim_assets": [asset],
    }
    eff = sim.build_effective_settings(
        payload,
        {"holdingHalfLives": {"zero_t1_eq": 2}, "simOptimismMultiplier": 1.6},
        use_ref_rates=True,
    )
    assert eff["holdingHalfLives"] == {}
    assert eff["simOptimismMultiplier"] == pytest.approx(1.0)


@pytest.mark.feature("payload")
def test_view_model_cash_collision_secondary_grant_constituent_keys_and_jan1_fra():
    """DA4-08: Verify Jan-1 SSA FRA rule, constituent ticker parent keys, secondary RSU grant isolation, and rollup_cash_all key stability."""
    invs = [
        {"id": "jan1_60", "birth_date": "1960-01-01", "ssn": {}},
        {"id": "jan2_60", "birth_date": "1960-01-02", "ssn": {}},
        {"id": "jan1_55", "birth_date": "1955-01-01", "ssn": {}},
    ]
    profiles = {p["id"]: p for p in vm._build_ssn_model({"investors": invs})["investors"]}
    assert profiles["jan1_60"]["fra_age"] == pytest.approx(round(66.0 + 5 * (2.0 / 12.0), 4))
    assert profiles["jan2_60"]["fra_age"] == pytest.approx(67.0)
    assert profiles["jan1_55"]["fra_age"] == pytest.approx(66.0)

    # Constituent `:TICKER:<sym>` parent_forecast_keys + simulator half-life inheritance
    roll = vm._rollup_sleeve_forecast_metrics(
        [
            {
                "forecast_key": "HB-7001:TICKER:NVDA",
                "current_value": 10000.0,
                "default_growth_pct": 18.0,
                "default_risk_class": "single_stock",
                "growth_evidence": [
                    {"tier": 1, "projected_return_pct": 18.0, "source_detail": "10Y ticker CAGR"},
                    {"tier": 3, "projected_return_pct": 8.5, "source_detail": "Asset class equilibrium"},
                ],
            }
        ],
        "broad_equity",
        date(2026, 11, 15),
    )
    c0 = roll["constituents"][0]
    assert c0["parent_forecast_keys"] == ["NVDA"]
    sleeve_asset = {
        "id": "sleeve_1",
        "forecast_key": "sleeve_1",
        "default_risk_class": "broad_equity",
        "growth_evidence": [{"tier": 1, "projected_return_pct": 18.0}, {"tier": 3, "projected_return_pct": 8.5}],
        "constituent_forecasts": [c0],
    }
    curve_flat_child = sim._holding_return_curve(sleeve_asset, 18.0, 5, years_by_key={"NVDA": 31})
    assert curve_flat_child(10) == pytest.approx(18.0)

    # Secondary unvested grant isolation
    accounts_db = {
        "HB-7001": {"institution": "Harbor", "tax_status": "TAXABLE"},
        "HB-7003": {"institution": "Harbor", "tax_status": "TRAD_IRA"},
    }
    stock_grants = {
        "companies": [{"symbol": "ACME"}, {"symbol": "NVDA"}],
        "by_symbol": {
            "ACME": {
                "symbol": "ACME",
                "live_price": 100.0,
                "account_id": "HB-7001",
                "release_bars": [{"kind": "FUTURE", "iso": "2027-06-15", "totNetShares": 10.0, "totGrossShares": 20.0}],
            },
            "NVDA": {
                "symbol": "NVDA",
                "live_price": 200.0,
                "account_id": "HB-7001",
                "release_bars": [{"kind": "FUTURE", "iso": "2027-06-15", "totNetShares": 5.0, "totGrossShares": 10.0}],
            },
        },
    }
    cash_rollup = {
        "group_id": "rollup_cash_all",
        "current_value": 15000.0,
        "children": [
            {"account_id": "HB-7001", "security_id": "CASH:USD", "current_value": 10000.0},
            {"account_id": "HB-7003", "security_id": "CASH:USD", "current_value": 5000.0},
        ],
    }
    sim_assets = vm._build_retirement_sim_assets(
        {"accounts": accounts_db},
        [cash_rollup],
        {"by_grant": [], "release_bars": []},
        100.0,
        "2026-11-15",
        cfg={"primary_equity_symbol": "ACME", "investors": [{"id": "alex", "name": "Alex", "birth_date": "1975-01-01"}]},
        stock_grants=stock_grants,
    )
    by_id = {a["id"]: a for a in sim_assets}
    assert by_id["future_unvested_acme"]["bucket"] == "goog"
    assert by_id["future_unvested_acme"]["parent_forecast_keys"] == ["ACME"]
    assert by_id["future_unvested_nvda"]["bucket"] == "non_goog"
    assert by_id["future_unvested_nvda"]["parent_forecast_keys"] == []
    assert by_id["future_unvested_nvda"]["default_risk_class"] == "single_stock"

    # End-to-end build_report_payload check: `rollup_cash_all` keeps `forecast_key == "cash_sweeps"` even when IRA cash exists
    import json
    import tempfile
    from pathlib import Path
    from ingest.pipeline import ingest_statements

    with tempfile.TemporaryDirectory() as td, PortfolioDB(":memory:") as mem_db:
        stmt_dir = Path(td)
        for fname, aid, a_name, a_type, tax_st, amt in [
            ("taxable.statement.json", "HB-7001", "Harbor Brokerage", "BROKERAGE", "TAXABLE", 10000.0),
            ("ira.statement.json", "HB-7003", "Harbor Traditional IRA", "IRA", "TRAD_IRA", 5000.0),
        ]:
            (stmt_dir / fname).write_text(
                json.dumps(
                    {
                        "format": "advisorbot.statement/1",
                        "institution": "Harbor",
                        "account_id": aid,
                        "account_name": a_name,
                        "account_type": a_type,
                        "tax_status": tax_st,
                        "as_of_date": "2026-10-31",
                        "reported_total": amt,
                        "positions": [
                            {
                                "security_id": f"CASH:{aid}",
                                "symbol_or_code": "USD",
                                "canonical_name": "Cash Sweep",
                                "asset_class": "CASH",
                                "stack_bucket": "cash",
                                "risk_class": "cash_fixed",
                                "is_scrapeable": False,
                                "quote_symbol": None,
                                "quantity": amt,
                                "stmt_price": 1.0,
                                "market_value": amt,
                                "value_date": "2026-10-31",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        cfg_mem = {"primary_equity_symbol": "ACME", "investors": [{"id": "alex", "name": "Alex", "birth_date": "1975-01-01"}]}
        ingest_statements(stmt_dir, mem_db, cfg_mem, run_id=mem_db.start_run("test"), force_all=True)
        built = vm.build_report_payload(mem_db, cfg_mem, date(2026, 11, 15))
    cash_grp = next(g for g in built["holdings_by_strategy"] if g["group_id"] == "rollup_cash_all")
    assert cash_grp["forecast_key"] == "cash_sweeps"


@pytest.mark.feature("privacy_shield")
def test_privacy_status_meta__reraises_corrupted_privacy_registry_error():
    """SA5-02b: A corrupted privacy_registry.json raises PrivacyConfigError out of _build_privacy_status_meta (fail-closed)."""
    with patch("privacy.load_privacy_config", side_effect=PrivacyConfigError("corrupted privacy_registry.json")):
        with pytest.raises(PrivacyConfigError):
            vm._build_privacy_status_meta("p1_index_saver")
