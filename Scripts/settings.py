#!/usr/bin/env python3
"""
Canonical Scenario & Retirement-Simulator settings: defaults, risk classes, per-holding
forecasts, unified scheduled actions, hypothetical assets, and slider bounds.

Reads investor claim-age keys and default bucket ordering from `config.load_config()`
(`Portfolios/<active>/project.json` when present, or `default_config()` fallback) so no PII or
personal account numbers are hardcoded in source.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any

RISK_CLASSES: dict[str, dict[str, Any]] = {
    "cash_fixed": {
        "id": "cash_fixed",
        "label": "Fixed Rate / Cash (0% Volatility)",
        "short_label": "Fixed / Cash (0%)",
        "default_vol_pct": 0.0,
        "default_corr_mkt": 0.00,
        "gfc_shock_pct": 0.0,
        "dotcom_shock_pct": 0.0,
        "covid_shock_pct": 0.0,
        "benchmark_return_pct": 4.0,
        "summary": "Deterministic principal & contractual interest; immune to market drawdowns (cash sweeps, MMFs, T-bills, CDs, fixed MYGAs).",
    },
    "fixed_income": {
        "id": "fixed_income",
        "label": "Bonds / Fixed Income (~4.5% Volatility)",
        "short_label": "Bonds / Fixed Inc (~4.5%)",
        "default_vol_pct": 4.5,
        "default_corr_mkt": 0.20,
        "gfc_shock_pct": 5.0,
        "dotcom_shock_pct": 24.0,
        "covid_shock_pct": 3.0,
        "rates2022_shock_pct": -13.0,
        "benchmark_return_pct": 4.5,
        "summary": "Modest rate/duration swings with low equity correlation and ballast during equity crashes (munis, core bonds, TIPS, 0%-floor FIAs).",
    },
    "hybrid_buffered": {
        "id": "hybrid_buffered",
        "label": "Hybrid / Buffered / Balanced (~8.5% Volatility)",
        "short_label": "Hybrid / Buffered (~8.5%)",
        "default_vol_pct": 8.5,
        "default_corr_mkt": 0.75,
        "gfc_shock_pct": -25.0,
        "dotcom_shock_pct": -20.0,
        "covid_shock_pct": -16.0,
        "rates2022_shock_pct": -16.0,
        "benchmark_return_pct": 6.5,
        "summary": "Dampened equity participation (~50–60% equity sensitivity) with downside buffers or fixed-income cushions (60/40 & target-date funds, RILAs, buffer ETFs).",
    },
    "broad_equity": {
        "id": "broad_equity",
        "label": "Broad Equity / Diversified (~14.5% Volatility)",
        "short_label": "Broad Equity (~14.5%)",
        "default_vol_pct": 14.5,
        "default_corr_mkt": 1.00,
        "gfc_shock_pct": -50.0,
        "dotcom_shock_pct": -45.0,
        "covid_shock_pct": -34.0,
        "rates2022_shock_pct": -24.0,
        "benchmark_return_pct": 8.5,
        "summary": "Tracks diversified equity market cycles and historical S&P 500 drawdowns (index ETFs, equity mutual funds, diversified direct-indexing SMAs, REITs).",
    },
    "real_diversifier": {
        "id": "real_diversifier",
        "label": "Gold / Commodities / Real Assets (~15% Volatility)",
        "short_label": "Gold / Real Assets (~15%)",
        "default_vol_pct": 15.0,
        "default_corr_mkt": 0.05,
        "gfc_shock_pct": 5.0,
        "dotcom_shock_pct": 4.0,
        "covid_shock_pct": 8.0,
        "rates2022_shock_pct": 1.0,
        "benchmark_return_pct": 5.5,
        "summary": "Equity-like standalone volatility with near-zero stock correlation and inflation/crisis hedge behavior (gold, silver, broad commodities, managed futures).",
    },
    "single_stock": {
        "id": "single_stock",
        "label": "Single Stock / Concentrated (~35% Volatility)",
        "short_label": "Single Stock (~35%)",
        "default_vol_pct": 35.0,
        "default_corr_mkt": 0.50,
        "gfc_shock_pct": -60.0,
        "dotcom_shock_pct": -75.0,
        "covid_shock_pct": -34.0,
        "rates2022_shock_pct": -38.0,
        "benchmark_return_pct": 8.5,
        "summary": "High idiosyncratic single-company dispersion on top of market cycles; subject to steep drawdowns and volatility drag (individual stocks, equity awards).",
    },
    "crypto_speculative": {
        "id": "crypto_speculative",
        "label": "Crypto / Leveraged / Speculative (~68% Volatility)",
        "short_label": "Crypto / Speculative (~68%)",
        "default_vol_pct": 68.0,
        "default_corr_mkt": 0.45,
        "gfc_shock_pct": -72.0,
        "dotcom_shock_pct": -80.0,
        "covid_shock_pct": -48.0,
        "rates2022_shock_pct": -64.0,
        "benchmark_return_pct": 10.0,
        "summary": "Extreme tail volatility and 70%+ drawdown risk with severe sequence-of-returns drag during withdrawals (spot BTC/ETH ETFs, 2x/3x leveraged ETFs, venture).",
    },
}

VALID_RISK_CLASSES = tuple(RISK_CLASSES.keys())
VALID_ACTIVE_MODELS = ("single_path", "stress", "range", "historical")
VALID_STRESS_SCENARIOS = ("none", "gfc2008", "dotcom2000", "covid2020", "rates2022", "custom")
STRESS_NO_BOUNCE_MONTHS = 75
MUNI_EQUILIBRIUM_RETURN_PCT = 3.5
DEFAULT_HALF_LIFE_YEARS = 5
FLAT_HALF_LIFE_YEARS = 31


def invalidate_settings_cache() -> None:
    _defaults_for_file.cache_clear()


def _extract_project_defaults(cfg: dict[str, Any]) -> tuple[list[str], list[str], dict[str, int], dict[str, Any]]:
    claim_keys: list[str] = []
    claim_defaults: dict[str, int] = {}
    for idx, inv in enumerate(cfg.get("investors") or []):
        name = str(inv.get("name") or f"Investor{idx + 1}").strip().title().replace(" ", "")
        ck = f"ssn{name}ClaimAge"
        claim_keys.append(ck)
        ssn_cfg = inv.get("ssn") or {}
        claim_defaults[ck] = int(ssn_cfg.get("default_claim_age", 67))
    if not claim_keys:
        claim_keys = ["ssnInvestor1ClaimAge", "ssnInvestor2ClaimAge"]
        claim_defaults = {"ssnInvestor1ClaimAge": 67, "ssnInvestor2ClaimAge": 67}

    sim_buckets = cfg.get("sim_buckets") or {}
    if sim_buckets:
        ordered_buckets = [
            bid
            for bid, meta in sorted(
                sim_buckets.items(),
                key=lambda kv: (int((kv[1] or {}).get("default_rank", 99)), kv[0]),
            )
            if not (meta or {}).get("exclude")
        ]
    else:
        prim_sym = str(cfg.get("primary_equity_symbol") or "").strip().lower()
        if prim_sym:
            ordered_buckets = [
                f"vested_{prim_sym}_high_basis",
                f"future_unvested_{prim_sym}",
                "cash_sweeps",
                f"vested_{prim_sym}_low_basis",
            ]
        else:
            ordered_buckets = ["cash_sweeps"]
    return claim_keys, ordered_buckets, claim_defaults, cfg


@lru_cache(maxsize=8)
def _defaults_for_file(path: Path | None, modified_ns: int | None):
    from config import default_config, load_config
    return _extract_project_defaults(load_config(path) if path is not None else default_config())


def _load_project_defaults(cfg: dict[str, Any] | None = None) -> tuple[list[str], list[str], dict[str, int], dict[str, Any]]:
    if isinstance(cfg, dict):
        return _extract_project_defaults(cfg)
    from config import get_default_config_path
    path = get_default_config_path()
    try:
        modified_ns = path.stat().st_mtime_ns if path is not None else None
    except FileNotFoundError:
        modified_ns = None
    return _defaults_for_file(path, modified_ns)


def infer_risk_class(
    *,
    asset_id: str = "",
    bucket: str = "",
    rate_model: str = "",
    asset_class: str = "",
    tax_model: str = "",
    symbol: str = "",
    primary_symbol: str = "",
) -> str:
    """Auto-assign one of the 7 canonical risk profiles from asset/holding metadata."""
    # PORTABILITY DO: Classify risk profiles using generic asset classes, rate models, and `primary_symbol` (`LL-13`, `LL-15`, `LL-32`).
    # PORTABILITY DON'T: Match against specific insurance carrier names, brokerage tickers, or volatility preset literals.
    rm = str(rate_model or "").strip().lower()
    aid = str(asset_id or "").strip().lower()
    ac = str(asset_class or "").strip().upper()
    tm = str(tax_model or "").strip().upper()
    sym = str(symbol or "").strip().upper()
    prim = str(primary_symbol or "").strip().upper()

    if ac in ("CRYPTO", "LEVERAGED_ETF", "OPTION", "CRYPTO_SPECULATIVE") or "CRYPTO" in ac:
        return "crypto_speculative"
    if ac in ("GOLD", "PRECIOUS_METAL", "COMMODITY", "REAL_DIVERSIFIER") or "PRECIOUS" in ac or "COMMODITY" in ac or "GOLD" in ac:
        return "real_diversifier"
    if ac in ("RILA_ANNUITY", "BALANCED_FUND", "TARGET_DATE_FUND", "BUFFER_ETF", "HIGH_YIELD", "HYBRID_BUFFERED") or "rila" in aid or "RILA" in ac or "INDEX-LINKED" in ac or "STRUCTURED VARIABLE" in ac:
        return "hybrid_buffered"
    if ac in ("INDEXED_ANNUITY", "MUNI_BOND", "MUNI_BONDS", "BOND", "BONDS", "TREASURY", "TIPS", "FIXED_INCOME") or "muni" in aid or tm == "MUNI" or "INDEXED ANNUITY" in ac or "MUNICIPAL BOND" in ac or "FIXED INCOME" in ac:
        return "fixed_income"
    if rm == "fixed" or aid == "cash_sweeps" or ac in ("CASH", "MONEY_MARKET", "CD", "FIXED_ANNUITY", "MYGA") or "FIXED ANNUITY" in ac or "MYGA" in ac or "DEPOSIT SWEEP" in ac or "MONEY MARKET" in ac:
        return "cash_fixed"
    if bucket in ("primary_equity", "goog") or (prim and (sym == prim or f"_{prim.lower()}" in aid or aid.startswith(f"{prim.lower()}_"))) or ac in ("EQUITY", "PRIMARY_STOCK", "RSU", "GSU") or "PUBLIC EQUITY" in ac:
        return "single_stock"
    return "broad_equity"


def default_holding_forecasts_from_config(
    cfg: dict[str, Any] | None = None,
    *,
    single_stock_pct: float = 0.0,
    broad_equity_pct: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Build explicit `holdingForecasts` map from `project.json` bucket & rate definitions (used for legacy/Zero expansion)."""
    if cfg is None:
        _, _, _, cfg = _load_project_defaults()
    prim = str((cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    forecasts: dict[str, dict[str, Any]] = {}
    if prim:
        forecasts[prim] = {
            "growth_pct": round(float(single_stock_pct), 2),
            "risk_class": "single_stock",
        }
    fixed_rates = dict((cfg or {}).get("sim_asset_rates") or {})
    sim_buckets = dict((cfg or {}).get("sim_buckets") or {})
    for bid, meta in sim_buckets.items():
        if not isinstance(meta, dict) or meta.get("exclude"):
            continue
        rm = str(meta.get("rate_model") or (fixed_rates.get(bid) or {}).get("rate_model") or "").lower()
        rc = infer_risk_class(
            asset_id=bid,
            bucket="goog" if ("goog" in bid.lower() or (prim and prim.lower() in bid.lower())) else "non_goog",
            rate_model=rm,
            primary_symbol=prim,
        )
        if rm == "fixed":
            rate_val = meta.get("annual_rate_pct")
            if rate_val is None:
                rate_val = (fixed_rates.get(bid) or {}).get("annual_rate_pct", 4.0)
            gp = round(float(rate_val), 2)
        elif rc == "single_stock":
            gp = round(float(single_stock_pct), 2)
        else:
            gp = round(float(broad_equity_pct), 2)
        forecasts[bid] = {"growth_pct": gp, "risk_class": rc}

    for bid, rmeta in fixed_rates.items():
        if bid not in forecasts and isinstance(rmeta, dict):
            rc = infer_risk_class(asset_id=bid, rate_model=str(rmeta.get("rate_model") or "fixed"), primary_symbol=prim)
            forecasts[bid] = {
                "growth_pct": round(float(rmeta.get("annual_rate_pct", 4.0)), 2),
                "risk_class": rc,
            }
    return forecasts


def parse_rmd_owner_index(raw_val: Any) -> int | None:
    """Normalize optional hypothetical RMD owner index (`int >= 0` when valid integer, else `None`)."""
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int):
        return raw_val if raw_val >= 0 else None
    if isinstance(raw_val, float):
        return int(raw_val) if math.isfinite(raw_val) and raw_val >= 0 and raw_val.is_integer() else None
    s = str(raw_val).strip()
    if not s or not s.isdigit():
        return None
    return int(s)


def normalize_half_lives(raw_half_lives: Any) -> dict[str, int]:
    """Retain finite per-holding settings in whole years within the slider range (1..30 yrs, 31 = Flat)."""
    return {
        str(key): max(1, min(FLAT_HALF_LIFE_YEARS, math.floor(value + 0.5)))
        for key, value in (raw_half_lives.items() if isinstance(raw_half_lives, dict) else ())
        if type(value) in (int, float) and math.isfinite(value)
    }


def resolve_holding_forecast(
    holding_forecasts: dict[str, Any] | None,
    key: str,
    parent_keys: list[str] | tuple[str, ...] = (),
    fallback_growth_pct: float = 0.0,
    fallback_risk_class: str = "broad_equity",
) -> dict[str, Any]:
    """
    Resolve `(growth_pct, risk_class, is_override, risk_overridden)` for a bucket or holding:
    1. Exact `key` in `holding_forecasts`
    2. First matching key in `parent_keys` (e.g. strategy key or primary ticker symbol)
    3. `fallback_growth_pct` & `fallback_risk_class` (ingested baseline)
    """
    hf = holding_forecasts if isinstance(holding_forecasts, dict) else {}
    rc_fb = fallback_risk_class if fallback_risk_class in RISK_CLASSES else "broad_equity"
    candidates = [key, *parent_keys]
    for cand in candidates:
        if not cand or cand not in hf:
            continue
        entry = hf[cand]
        if isinstance(entry, dict) and entry.get("use_reference") and cand != key:
            continue
        if isinstance(entry, (int, float)):
            return {
                "growth_pct": max(-20.0, min(25.0, float(entry))),
                "risk_class": rc_fb,
                "inherited_from": None if cand == key else cand,
                "is_override": True,
                "risk_overridden": False,
            }
        if isinstance(entry, dict):
            gp_raw = fallback_growth_pct if entry.get("use_reference") else entry.get("growth_pct", fallback_growth_pct)
            try:
                gp = max(-20.0, min(25.0, float(gp_raw)))
            except (TypeError, ValueError):
                gp = float(fallback_growth_pct)
            raw_rc = str(entry.get("risk_class") or "").strip().lower()
            rc = raw_rc if raw_rc in RISK_CLASSES else rc_fb
            explicit_risk_flag = bool(entry.get("risk_overridden")) or (bool(raw_rc) and rc != rc_fb)
            vol_raw = entry.get("vol_pct")
            vol_pct = None
            if vol_raw is not None:
                try:
                    vol_pct = max(0.0, min(150.0, float(vol_raw)))
                except (TypeError, ValueError):
                    vol_pct = None
            return {
                "growth_pct": gp,
                "risk_class": rc,
                "vol_pct": vol_pct,
                "inherited_from": None if cand == key else cand,
                "is_override": True,
                "risk_overridden": explicit_risk_flag,
            }
    return {
        "growth_pct": float(fallback_growth_pct),
        "risk_class": rc_fb,
        "inherited_from": None,
        "is_override": False,
        "risk_overridden": False,
    }


def build_settings_schema() -> dict[str, dict[str, float]]:
    claim_keys, _, _, _ = _load_project_defaults()
    schema: dict[str, dict[str, float]] = {
        "holdingGrowthPct": {"min": -20.0, "max": 25.0, "step": 0.1},
        "simOptimismMultiplier": {"min": -2.0, "max": 2.0, "step": 0.05},
        "simAnnualBurn": {"min": 0.0, "max": 1_000_000.0, "step": 1000.0},
        "simInflationPct": {"min": 0.0, "max": 6.0, "step": 0.1},
        "ssnColaPct": {"min": 0.0, "max": 6.0, "step": 0.1},
        "mcSingleStockVolPct": {"min": 0.0, "max": 80.0, "step": 0.5},
        "mcBroadEquityVolPct": {"min": 0.0, "max": 50.0, "step": 0.5},
        "mcSingleStockCorr": {"min": -0.5, "max": 1.0, "step": 0.01},
    }
    for ck in claim_keys:
        schema[ck] = {"min": 62.0, "max": 70.0, "step": 1.0}
    for neutral_ck in ("ssnInvestor1ClaimAge", "ssnInvestor2ClaimAge"):
        schema.setdefault(neutral_ck, {"min": 62.0, "max": 70.0, "step": 1.0})
    for ret_ck in ("simInvestor1RetirementAge", "simInvestor2RetirementAge"):
        schema[ret_ck] = {"min": 18.0, "max": 100.0, "step": 1.0}
    return schema


SETTINGS_SCHEMA: dict[str, dict[str, float]] = build_settings_schema()


def default_settings_snapshot(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Canonical default Scenario settings matching initial webpage state (`DP-1: 1A`):
    `holdingForecasts` starts empty `{}` so every untouched holding automatically resolves
    to its ingested `projected_return_pct` and `risk_class` baseline.
    """
    _, ordered_buckets, claim_defaults, _ = _load_project_defaults(cfg)
    ref_burn = int((cfg or {}).get("reference_annual_burn") or (cfg or {}).get("retirement_annual_burn") or 50000)
    try:
        from valuation.simulator import compute_trailing_inflation_cagr
        inf_ref = compute_trailing_inflation_cagr()
    except Exception:
        inf_ref = 3.1
    try:
        from valuation.simulator import compute_ssn_cola_rate
        cola_ref = compute_ssn_cola_rate()["cola_pct"]
    except Exception:
        cola_ref = 2.8

    snap: dict[str, Any] = {
        "holdingForecasts": {},
        "holdingHalfLives": {},
        "taxMode": "net",
        "pastReleaseValMode": "release",
        "simAnnualBurn": ref_burn,
        "simInflationPct": inf_ref,
        "simOptimismMultiplier": 1.0,
        "simIncludeUnvested": True,
        "simIncludeSSN": True,
        "simIncludeScheduledActions": True,
        "simBearShield": True,
        "simOrderPreset": "tax_optimal",
        "simAssetOrder": list(ordered_buckets),
        "simScheduledActions": [],
        "simHypotheticalAssets": [],
        "simActiveModel": "single_path",
        "simScaleMode": "auto",
        "simStressScenario": "none",
        "simCustomShockPct": -30.0,
        "simCustomDownMonths": 12,
        "simCustomBroadBounceMonths": 24,
        "simCustomStockBounceMonths": 36,
        "mcSingleStockVolPct": 35.0,
        "mcBroadEquityVolPct": 13.9,
        "mcReturnModel": "normal",
        "mcSingleStockCorr": 0.50,
        "mcSimulations": 1000,
        "mcSuccessThresholdUsd": 0,
        "cohortCenterMode": "anchor",
        "cohortInflationMode": "real_cpi",
        "cohortStartFrom": 1947,
        "ssnColaPct": cola_ref,
        "simMarriedFilingJointly": None,
        "simMinTaxBracketFloor": None,
        "simInvestor1RetirementAge": None,
        "simInvestor2RetirementAge": None,
        "simMemberRetirementAges": {},
    }
    for ck, def_age in claim_defaults.items():
        snap[ck] = def_age
    return snap


def canonicalize_model_settings(
    raw_settings: dict[str, Any] | None,
    *,
    cfg: dict[str, Any] | None = None,
    sim_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Normalize a raw settings dict into the single canonical Phase D/E schema:
    - Preserves only explicit `holdingForecasts` overrides so untouched / newly ingested holdings
      automatically inherit their enriched `default_growth_pct` & `default_risk_class` (`DP-1: 1A`).
    - Upgrades any legacy `futureGoogGrowthPct` / `futureNonGoogGrowthPct` into `holdingForecasts`.
    - Upgrades any legacy `simScheduledWithdrawals` / `simIncludeScheduledWithdrawals` into
      `simScheduledActions` / `simIncludeScheduledActions`.
    - Upgrades `mcGoogVolPct` / `mcNonGoogVolPct` / `mcGoogRestCorr` into portfolio-agnostic keys.
    """
    _, _, _, eff_cfg = _load_project_defaults(cfg)
    prim = str((eff_cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    if not prim and isinstance((eff_cfg or {}).get("symbols"), list) and eff_cfg["symbols"]:
        prim = str(eff_cfg["symbols"][0] or "").strip().upper()

    base = default_settings_snapshot(eff_cfg)
    src = dict(raw_settings or {})

    raw_g = src.get("primaryGrowthPct") if src.get("primaryGrowthPct") is not None else src.get("futureGoogGrowthPct")
    raw_ng = src.get("diversifiedGrowthPct") if src.get("diversifiedGrowthPct") is not None else src.get("futureNonGoogGrowthPct")
    has_legacy_g = raw_g is not None
    has_legacy_ng = raw_ng is not None
    legacy_g = float(raw_g) if has_legacy_g else 0.0
    legacy_ng = float(raw_ng) if has_legacy_ng else 0.0

    # Reference metadata per bucket/key for fallback risk_class lookup without forcing overrides
    ref_meta: dict[str, dict[str, Any]] = default_holding_forecasts_from_config(
        eff_cfg,
        single_stock_pct=legacy_g,
        broad_equity_pct=legacy_ng,
    )
    if sim_assets:
        for a in sim_assets:
            aid = str(a.get("id") or "").strip()
            if not aid or aid.startswith("hyp_"):
                continue
            rm = str(a.get("rate_model") or "").lower()
            rc = str(a.get("default_risk_class") or infer_risk_class(
                asset_id=aid,
                bucket=str(a.get("bucket") or ""),
                rate_model=rm,
                tax_model=str(a.get("tax_model") or ""),
                primary_symbol=prim,
            ))
            def_gp = float(a.get("default_growth_pct") if a.get("default_growth_pct") is not None else (
                a.get("annual_rate_pct", 4.0) if rm == "fixed" else 0.0
            ))
            ref_meta[aid] = {"growth_pct": round(def_gp, 2), "risk_class": rc, "rate_model": rm, "bucket": a.get("bucket")}
            fk = str(a.get("forecast_key") or "").strip()
            if fk and fk != aid:
                ref_meta[fk] = {"growth_pct": round(def_gp, 2), "risk_class": rc, "rate_model": rm, "bucket": a.get("bucket")}

    seeded_hf: dict[str, dict[str, Any]] = {}
    if isinstance(src.get("holdingForecasts"), dict):
        for k, v in src["holdingForecasts"].items():
            ks = str(k)
            prev = ref_meta.get(ks) or {}
            def_rc = str(prev.get("risk_class") or infer_risk_class(asset_id=ks, primary_symbol=prim))
            if isinstance(v, (int, float)):
                seeded_hf[ks] = {"growth_pct": round(float(v), 2), "risk_class": def_rc}
            elif isinstance(v, dict):
                rc = str(v.get("risk_class") or def_rc).lower()
                if rc not in RISK_CLASSES:
                    rc = def_rc if def_rc in RISK_CLASSES else "broad_equity"
                gp = float(v.get("growth_pct", prev.get("growth_pct", 0.0)))
                entry_out: dict[str, Any] = {"growth_pct": round(gp, 2), "risk_class": rc}
                if v.get("use_reference"):
                    entry_out["use_reference"] = True
                if v.get("risk_overridden"):
                    entry_out["risk_overridden"] = True
                if v.get("vol_pct") is not None:
                    try:
                        entry_out["vol_pct"] = round(float(v["vol_pct"]), 2)
                    except (TypeError, ValueError):
                        pass
                seeded_hf[ks] = entry_out

    if has_legacy_g and not isinstance(src.get("holdingForecasts"), dict):
        if prim:
            seeded_hf[prim] = {"growth_pct": round(legacy_g, 2), "risk_class": "single_stock"}
        for k, meta in ref_meta.items():
            if meta.get("risk_class") == "single_stock" or meta.get("bucket") in ("primary_equity", "goog"):
                seeded_hf[k] = {"growth_pct": round(legacy_g, 2), "risk_class": "single_stock"}
    if has_legacy_ng and not isinstance(src.get("holdingForecasts"), dict):
        for k, meta in ref_meta.items():
            rc_k = str(meta.get("risk_class") or "broad_equity")
            rm_k = str(meta.get("rate_model") or "")
            if rc_k != "single_stock" and meta.get("bucket") not in ("primary_equity", "goog"):
                if rm_k == "fixed" or rc_k in ("cash_fixed", "fixed_income") or "muni" in k.lower():
                    if k not in seeded_hf:
                        seeded_hf[k] = {"growth_pct": round(float(meta.get("growth_pct", 4.0)), 2), "risk_class": rc_k}
                else:
                    seeded_hf[k] = {"growth_pct": round(legacy_ng, 2), "risk_class": rc_k}

    base["holdingForecasts"] = seeded_hf
    base["holdingHalfLives"] = normalize_half_lives(src.get("holdingHalfLives"))

    # Scheduled Actions migration
    if "simIncludeScheduledActions" in src:
        base["simIncludeScheduledActions"] = bool(src["simIncludeScheduledActions"])
    elif "simIncludeScheduledWithdrawals" in src:
        base["simIncludeScheduledActions"] = bool(src["simIncludeScheduledWithdrawals"])

    raw_actions = src.get("simScheduledActions")
    if not isinstance(raw_actions, list) and isinstance(src.get("simScheduledWithdrawals"), list):
        migrated_actions: list[dict[str, Any]] = []
        for idx, w in enumerate(src["simScheduledWithdrawals"]):
            if not isinstance(w, dict):
                continue
            amt = float(w.get("amount") or 0.0)
            if amt <= 0:
                continue
            act: dict[str, Any] = {
                "id": str(w.get("id") or f"act_mig_{idx + 1}"),
                "type": "withdrawal",
                "cadence": "one_time",
                "ym": str(w.get("ym") or w.get("date") or "").strip(),
                "amount": round(amt, 2),
                "amount_mode": "usd",
                "dollar_mode": "nominal" if str(w.get("dollar_mode") or "today").lower() == "nominal" else "today",
                "from_asset_id": str(w.get("from_asset_id") or w.get("source") or "auto"),
                "comment": str(w.get("comment") or w.get("purpose") or "").strip(),
            }
            if w.get("person"):
                act["person"] = str(w["person"])
            if w.get("age") is not None:
                act["age"] = float(w["age"])
            migrated_actions.append(act)
        base["simScheduledActions"] = migrated_actions
    elif isinstance(raw_actions, list):
        base["simScheduledActions"] = list(raw_actions)

    if isinstance(src.get("simHypotheticalAssets"), list):
        base["simHypotheticalAssets"] = [
            {
                **h,
                "owner_id": str(h.get("owner_id") or "").strip(),
                "rmd_owner_index": parse_rmd_owner_index(h.get("rmd_owner_index")),
            }
            if isinstance(h, dict)
            else h
            for h in src["simHypotheticalAssets"]
        ]

    # Volatility / correlation key migration
    if "mcSingleStockVolPct" in src and src["mcSingleStockVolPct"] is not None:
        base["mcSingleStockVolPct"] = float(src["mcSingleStockVolPct"])
    elif "mcGoogVolPct" in src and src["mcGoogVolPct"] is not None:
        base["mcSingleStockVolPct"] = float(src["mcGoogVolPct"])

    if "mcBroadEquityVolPct" in src and src["mcBroadEquityVolPct"] is not None:
        base["mcBroadEquityVolPct"] = float(src["mcBroadEquityVolPct"])
    elif "mcNonGoogVolPct" in src and src["mcNonGoogVolPct"] is not None:
        base["mcBroadEquityVolPct"] = float(src["mcNonGoogVolPct"])

    if "mcSingleStockCorr" in src and src["mcSingleStockCorr"] is not None:
        base["mcSingleStockCorr"] = float(src["mcSingleStockCorr"])
    elif "mcGoogRestCorr" in src and src["mcGoogRestCorr"] is not None:
        base["mcSingleStockCorr"] = float(src["mcGoogRestCorr"])

    # Copy remaining canonical scalar/list keys
    passthrough_keys = (
        "taxMode",
        "pastReleaseValMode",
        "simAnnualBurn",
        "simInflationPct",
        "simOptimismMultiplier",
        "simIncludeUnvested",
        "simIncludeSSN",
        "simIncludeScheduledActions",
        "simBearShield",
        "simOrderPreset",
        "simAssetOrder",
        "simActiveModel",
        "simScaleMode",
        "simStressScenario",
        "simCustomShockPct",
        "simCustomDownMonths",
        "simCustomBroadBounceMonths",
        "simCustomStockBounceMonths",
        "mcReturnModel",
        "mcSimulations",
        "mcSuccessThresholdUsd",
        "cohortCenterMode",
        "cohortInflationMode",
        "cohortStartFrom",
        "ssnColaPct",
        "simMarriedFilingJointly",
        "simMinTaxBracketFloor",
    )
    for k in passthrough_keys:
        if k in src and src[k] is not None:
            base[k] = src[k]

    if str(base.get("simActiveModel") or "") not in VALID_ACTIVE_MODELS:
        base["simActiveModel"] = "single_path"

    # Ensure any hypothetical asset IDs are present in simAssetOrder
    order_list = [str(x) for x in (base.get("simAssetOrder") or []) if x]
    for hyp in base.get("simHypotheticalAssets") or []:
        if isinstance(hyp, dict):
            hid = str(hyp.get("id") or "").strip()
            if hid and hid not in order_list:
                order_list.append(hid)
    base["simAssetOrder"] = order_list

    # Preserve investor SSN claim age keys and map neutral ssnInvestor{1,2}ClaimAge aliases onto active claim_keys
    claim_keys, _, _, _ = _load_project_defaults(eff_cfg)
    for k, v in src.items():
        if k.startswith("ssn") and k.endswith("ClaimAge") and v is not None:
            try:
                base[k] = int(round(float(v)))
            except (TypeError, ValueError):
                pass
    for idx, neutral_ck in enumerate(("ssnInvestor1ClaimAge", "ssnInvestor2ClaimAge")):
        if neutral_ck in src and src[neutral_ck] is not None and idx < len(claim_keys):
            target_ck = claim_keys[idx]
            if target_ck not in src or src[target_ck] is None:
                try:
                    base[target_ck] = int(round(float(src[neutral_ck])))
                except (TypeError, ValueError):
                    pass

    # Positional per-Scenario retirement age overrides (strictly positional — never name-embedded PII keys)
    from config import resolve_family_members

    cfg_fams = resolve_family_members(eff_cfg) if isinstance(eff_cfg, dict) else []
    cfg_invs = (eff_cfg or {}).get("investors") if isinstance(eff_cfg, dict) else None
    for idx, ret_key in enumerate(("simInvestor1RetirementAge", "simInvestor2RetirementAge")):
        raw_ret = src.get(ret_key)
        if raw_ret is None or isinstance(raw_ret, bool) or raw_ret == "":
            base[ret_key] = None
            continue
        try:
            ret_val = int(round(float(raw_ret)))
        except (TypeError, ValueError):
            base[ret_key] = None
            continue
        ms_cap = 100
        if isinstance(cfg_invs, list) and idx < len(cfg_invs) and isinstance(cfg_invs[idx], dict):
            try:
                ms_cap = max(18, min(100, int(round(float(cfg_invs[idx].get("milestone_age", 85))))))
            except (TypeError, ValueError):
                ms_cap = 85
        elif idx < len(cfg_fams):
            try:
                ms_cap = max(18, min(100, int(round(float(cfg_fams[idx].get("milestone_age", 85))))))
            except (TypeError, ValueError):
                ms_cap = 85
        base[ret_key] = max(18, min(ms_cap, ret_val))

    raw_mem_ages = src.get("simMemberRetirementAges")
    clean_mem_ages: dict[str, int] = {}
    if isinstance(raw_mem_ages, dict):
        for raw_mk, raw_mv in raw_mem_ages.items():
            if raw_mv is None or isinstance(raw_mv, bool) or raw_mv == "":
                continue
            mk = str(raw_mk or "").strip()
            if mk.isdigit():
                f_idx = int(mk)
                mk = f"member_{f_idx}_retirement_age"
            elif mk.startswith("member_") and mk.endswith("_retirement_age"):
                mid_tok = mk[len("member_") : -len("_retirement_age")]
                if not mid_tok.isdigit():
                    continue
                f_idx = int(mid_tok)
            else:
                continue
            if f_idx < 0 or f_idx > 25:
                continue
            try:
                mv_val = int(round(float(raw_mv)))
            except (TypeError, ValueError):
                continue
            ms_cap = 100
            if f_idx < len(cfg_fams):
                try:
                    ms_cap = max(18, min(100, int(round(float(cfg_fams[f_idx].get("milestone_age", 85))))))
                except (TypeError, ValueError):
                    ms_cap = 85
            clean_mem_ages[mk] = max(18, min(ms_cap, mv_val))
    base["simMemberRetirementAges"] = clean_mem_ages

    return base
