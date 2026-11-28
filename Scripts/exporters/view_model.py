#!/usr/bin/env python3
"""Canonical report view-model builder for the local 4-screen HTML dashboard."""

from __future__ import annotations

import colorsys
import copy
import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import (
    DEFAULT_SAVED_PROMPT_SPECS,
    US_STATE_TAX_PRESETS,
    get_active_portfolio_id,
    get_saves_dir,
    list_portfolios,
    normalize_badge_history,
    normalize_badge_image_path,
    normalize_saved_prompts,
    resolve_advisor_prompts,
    resolve_family_members,
    sanitize_work_plan,
)
from db.database import PortfolioDB
from ingest.account_masking import resolve_target_account_id
from advisorbot_server import list_saves_payload, load_global_preferences
from settings import MUNI_EQUILIBRIUM_RETURN_PCT, RISK_CLASSES, infer_risk_class
from valuation.engine import _load_portfolio_state, evaluate_portfolio_at_date, load_price_book
from valuation.forecast_enricher import (
    EVIDENCE_VERSION, MIN_HISTORY_DAYS_FOR_CAGR, clamp_forecast_return, enrich_holding_forecasts,
    estimate_benchmark_vol_pct, format_equilibrium_detail,
)
from valuation.market_data import sync_exact_dates_for_symbol

SAFE_DATA_PALETTE = [
    "#38bdf8",
    "#10b981",
    "#14b8a6",
    "#0284c7",
    "#06b6d4",
    "#2dd4bf",
    "#22c55e",
    "#fb7185",
    "#0ea5e9",
    "#f43f5e",
    "#84cc16",
    "#94a3b8",
]


def _rollup_growth_evidence(positions: list[dict[str, Any]], sleeve_risk_class: str, as_of: date) -> list[dict[str, Any]]:
    if len(positions) == 1 and positions[0].get("growth_evidence"):
        return sorted((entry for entry in positions[0]["growth_evidence"] if entry.get("tier") in (1, 2, 3)),
                      key=lambda entry: entry["tier"]) or [_class_growth_evidence(sleeve_risk_class, as_of)]
    total = sum(ch["current_value"] for ch in positions)
    evidence = []
    for tier in (1, 2, 3):
        available = []
        for ch in positions:
            entry = next((e for e in ch.get("growth_evidence", []) if e.get("tier") == tier), None)
            if entry is not None:
                available.append((ch["current_value"], entry))
        if available:
            covered = sum(value for value, _ in available)
            rate = sum(value * entry["projected_return_pct"] for value, entry in available) / covered
            details = {entry["source_detail"] for _, entry in available}
            method = details.pop() if len(details) == 1 else "Expand individual holdings to see each position's source and dates."
            evidence.append({"tier": tier, "projected_return_pct": round(rate, 2),
                             "source_detail": f"Weighted by dollar balance across {len(available)} of {len(positions)} holdings "
                                              f"({covered / total:.1%} of group value on {as_of}). {method}"})
    return evidence or [_class_growth_evidence(sleeve_risk_class, as_of)]


def _class_growth_evidence(risk_class: str, as_of: date, *, is_muni: bool = False) -> dict[str, Any]:
    if is_muni:
        return {"tier": 3, "projected_return_pct": MUNI_EQUILIBRIUM_RETURN_PCT,
                "source_detail": format_equilibrium_detail(risk_class, str(as_of), is_muni=True)}
    rate = RISK_CLASSES.get(risk_class, RISK_CLASSES["broad_equity"])["benchmark_return_pct"]
    return {"tier": 3, "projected_return_pct": rate,
            "source_detail": format_equilibrium_detail(risk_class, str(as_of))}


def _add_dated_return(evidence: list[dict[str, Any]], row: dict[str, Any], rc: str) -> list[dict[str, Any]]:
    """Include an independently dated holding or group return inside Tier 1 (preserving Tier 2 for peer benchmarks)."""
    raw = row.get("cagr_pct")
    start, end = row.get("principal_date"), row.get("cagr_end_date") or row.get("asof_date")
    if raw is None or not start or not end:
        return evidence
    try:
        age_days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (ValueError, TypeError):
        return evidence
    if age_days < MIN_HISTORY_DAYS_FOR_CAGR:
        return evidence
    rate, capped = clamp_forecast_return(float(raw), rc)
    detail = row.get("cagr_method") or "Average annual growth from recorded starting principal to current value"
    detail += f" from {start} to {end}"
    if capped:
        detail += f" (capped from {raw:+.2f}% to {rate:+.2f}% to stay within realistic long-term bounds)"
    # Keep whole-account dated principal returns as Tier-1 `Also available` comparisons alongside
    # constituent Tier-1 rates without polluting Tier-2 Peer Market Benchmarks.
    if not row.get("cagr_method") and any(e["tier"] == 1 for e in evidence):
        return evidence
    return sorted([*evidence, {"tier": 1, "projected_return_pct": rate, "source_detail": detail}],
                  key=lambda e: e["tier"])


def _rollup_sleeve_forecast_metrics(
    children: list[dict[str, Any]],
    sleeve_risk_class: str,
    as_of: date,
    spy_vol_pct: float = 14.5,
    group: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select the most specific available return tier and aggregate whole-sleeve risk metrics."""
    valid_children = [c for c in (children or []) if not c.get("is_sub_segment") and (c.get("current_value") or 0) > 0]
    tot_val = sum(c["current_value"] for c in valid_children)
    evidence = _rollup_growth_evidence(valid_children, sleeve_risk_class, as_of)
    evidence = _add_dated_return(evidence, group or {}, sleeve_risk_class)
    selected = evidence[0]

    high_weight = 0.0
    low_weight = 0.0
    constituents: list[dict[str, Any]] = []

    # 1-factor covariance decomposition across children with realized 10Y vol & beta_spy
    vol_weight_sum = 0.0
    beta_weighted_sum = 0.0
    idio_var_sum = 0.0
    sigma_m = max(0.01, float(spy_vol_pct) / 100.0)

    for c in valid_children:
        cv = max(0.0, float(c.get("current_value") or 0.0))
        w = cv / tot_val
        gp = float(c.get("default_growth_pct") or 0.0)
        rc = str(c.get("default_risk_class") or sleeve_risk_class)
        conf = str(c.get("confidence") or "MEDIUM").upper()
        if conf == "HIGH":
            high_weight += w
        elif conf == "LOW":
            low_weight += w
        fk_c = str(c.get("forecast_key") or c.get("holding_key") or "")
        if fk_c:
            c_entry: dict[str, Any] = {
                "forecast_key": fk_c,
                "weight": round(w, 8),
                "default_growth_pct": round(gp, 2),
                "default_risk_class": rc,
                "rate_model": c.get("rate_model"),
                "return_source": c.get("return_source"),
                "maturity_date": c.get("maturity_date") or c.get("maturity_iso"),
                "growth_evidence": [
                    {"tier": e["tier"], "projected_return_pct": e["projected_return_pct"]}
                    for e in c.get("growth_evidence") or []
                ],
            }
            sym_c = fk_c.split(":TICKER:", 1)[1].strip().upper() if ":TICKER:" in fk_c else ""
            if not sym_c and str(c.get("security_id") or "").upper().startswith("TICKER:"):
                cand_sym = str(c["security_id"]).split(":", 1)[1].strip().upper()
                if cand_sym and cand_sym != fk_c:
                    sym_c = cand_sym
            if sym_c:
                c_entry["parent_forecast_keys"] = [sym_c]
            constituents.append(c_entry)
        rvol = c.get("realized_vol_pct")
        b_spy = c.get("beta_spy")
        if rvol is not None and b_spy is not None and sleeve_risk_class != "cash_fixed":
            vol_weight_sum += w
            sig_i = float(rvol) / 100.0
            beta_i = float(b_spy)
            beta_weighted_sum += w * beta_i
            idio_var_i = max(0.0, (sig_i * sig_i) - (beta_i * sigma_m) ** 2)
            idio_var_sum += (w * w) * idio_var_i

    realized_vol_out: float | None = None
    beta_out: float | None = None
    corr_out: float | None = None
    if sleeve_risk_class == "cash_fixed":
        realized_vol_out, beta_out, corr_out = 0.0, 0.0, 0.0
    elif vol_weight_sum >= 0.70:
        # Normalize if a tiny fraction of children lacked 20w history
        scale = 1.0 / vol_weight_sum
        eff_beta = beta_weighted_sum * scale
        sys_sd = abs(eff_beta) * sigma_m
        idio_var = idio_var_sum * (scale * scale)
        tot_sd = math.sqrt(sys_sd * sys_sd + idio_var)
        realized_vol_out = round(max(0.5, min(120.0, tot_sd * 100.0)), 2)
        beta_out = round(eff_beta, 3)
        if tot_sd > 1e-6:
            corr_raw = (1.0 if eff_beta >= 0 else -1.0) * (sys_sd / tot_sd)
            corr_out = round(max(-0.95, min(0.999, corr_raw)), 3)
        else:
            corr_out = 0.0

    sleeve_conf = "HIGH" if high_weight >= 0.75 else ("LOW" if low_weight >= 0.50 else "MEDIUM")
    return {
        "default_growth_pct": selected["projected_return_pct"],
        "growth_evidence": evidence,
        "realized_vol_pct": realized_vol_out,
        "beta_spy": beta_out,
        "corr_spy": corr_out,
        "confidence": sleeve_conf,
        "return_source": "SLEEVE_WEIGHTED_ROLLUP",
        "source_detail": selected["source_detail"],
        "constituents": constituents,
    }


def _sanitize_non_exclusive_color(raw_hex: str | None, fallback_idx: int = 0) -> str:
    """
    Enforces Strict Global Color Exclusivity (Phase D Sec 3.5):
    Category (A) Indigo/Violet (`#8B5CF6`) and Category (B) Gold/Amber (`#FBBF24`)
    are reserved strictly for Saved Model Inputs and Primary KPI cards. Any data
    series color in the yellow/amber/orange (20°-68°) or indigo/purple/magenta
    (225°-330°) hue bands is mapped onto the safe Ocean/Teal/Emerald/Rose palette.
    """
    s = str(raw_hex or "").strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", s)
    if not m:
        return SAFE_DATA_PALETTE[fallback_idx % len(SAFE_DATA_PALETTE)]
    hx = m.group(1)
    r, g, b = int(hx[0:2], 16) / 255.0, int(hx[2:4], 16) / 255.0, int(hx[4:6], 16) / 255.0
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    deg = h * 360.0
    if sat > 0.18 and ((20.0 <= deg <= 68.0) or (225.0 <= deg <= 330.0)):
        idx = sum(ord(ch) for ch in hx.lower()) + fallback_idx
        return SAFE_DATA_PALETTE[idx % len(SAFE_DATA_PALETTE)]
    return f"#{hx.lower()}"


# PORTABILITY DO: Keep `ASSET_CLASS_META` labels generic across all portfolios (`LL-04`).
# PORTABILITY DON'T: Embed specific mutual fund tickers, sleeve counts, or state codes in category labels.
ASSET_CLASS_META = [
    {"id": "EQUITY", "label": "Public Equities", "color": "#38bdf8"},
    {"id": "INDEXED_ANNUITY", "label": "Indexed Annuities", "color": "#06b6d4"},
    {"id": "FIXED_ANNUITY", "label": "Fixed Annuities", "color": "#14b8a6"},
    {"id": "MUNI_BOND", "label": "Municipal Bonds", "color": "#0ea5e9"},
    {"id": "RILA_ANNUITY", "label": "Structured Buffer Annuities (RILA)", "color": "#f43f5e"},
    {"id": "MUTUAL_FUND", "label": "Mutual Funds", "color": "#10b981"},
    {"id": "CASH", "label": "Cash & Deposit Sweeps", "color": "#94a3b8"},
]

INVESTMENT_TYPE_LABELS: dict[str, str] = {
    "RILA_ANNUITY": "Registered Index-Linked Annuity (RILA)",
    "INDEXED_ANNUITY": "Fixed Indexed Annuity (FIA)",
    "FIXED_ANNUITY": "Fixed Annuity (MYGA)",
    "MYGA": "Fixed Annuity (MYGA)",
    "EQUITY": "Public Equity / Common Stock",
    "MUTUAL_FUND": "Mutual Fund",
    "ETF": "Exchange-Traded Fund (ETF)",
    "MUNI_BOND": "Tax-Exempt Municipal Bond",
    "BOND": "Fixed Income / Bond",
    "TREASURY": "US Treasury Security",
    "TIPS": "Treasury Inflation-Protected Security (TIPS)",
    "CASH": "Cash & Deposit Sweep",
    "MONEY_MARKET": "Money Market Fund",
    "CD": "Certificate of Deposit (CD)",
    "RSU": "Restricted Stock Unit (RSU)",
    "GSU": "Restricted Stock Unit (GSU)",
    "BUFFER_ETF": "Buffered / Defined-Outcome ETF",
    "BALANCED_FUND": "Balanced / Allocation Fund",
    "TARGET_DATE_FUND": "Target Date Fund",
    "HIGH_YIELD": "High-Yield Bond / Credit",
    "COMMODITY": "Commodity",
    "GOLD": "Precious Metals / Gold",
    "PRECIOUS_METAL": "Precious Metals",
    "REIT": "Real Estate Investment Trust (REIT)",
    "CIT": "Collective Investment Trust (CIT)",
    "CRYPTO": "Cryptocurrency / Digital Asset",
    "LEVERAGED_ETF": "Leveraged ETF",
    "OPTION": "Option Contract",
}


def format_investment_type(asset_class: str | None) -> str:
    """Format internal asset_class enums into human-readable, professional investment types."""
    if not asset_class:
        return "—"
    ac = str(asset_class).strip()
    if ac in INVESTMENT_TYPE_LABELS:
        return INVESTMENT_TYPE_LABELS[ac]
    upper = ac.upper()
    if upper in INVESTMENT_TYPE_LABELS:
        return INVESTMENT_TYPE_LABELS[upper]
    if " " in ac or any(c.islower() for c in ac):
        return ac
    return ac.replace("_", " ").title()

DEFAULT_ACCOUNT_PALETTE = [
    "#38bdf8",
    "#06b6d4",
    "#10b981",
    "#f43f5e",
    "#0ea5e9",
    "#14b8a6",
    "#0284c7",
    "#fb7185",
]

GRANT_PALETTE = [
    "#38bdf8",
    "#2dd4bf",
    "#06b6d4",
    "#f43f5e",
    "#0ea5e9",
    "#10b981",
    "#fb7185",
    "#84cc16",
    "#14b8a6",
]
PRIOR_GRANT_COLOR = "#0284c7"


def solve_annualized_return(
    dated_principals: list[tuple[str, float]],
    end_iso: str,
    current_value: float,
) -> tuple[float | None, float]:
    """
    Computes the annualized compound growth / average annual interest rate `r` solving:
        sum(P_i * (1 + r) ** ((end_date - d_i).days / 365.25)) = current_value
    For a single holding (N=1), this reduces identically to CAGR = (V / P)**(365.25 / days) - 1.
    Returns `(annualized_rate_pct, weighted_years)`.
    """
    if not dated_principals or current_value <= 0:
        return None, 0.0

    d_end = date.fromisoformat(end_iso)
    valid_lots: list[tuple[float, float]] = []
    tot_p = 0.0
    for d_str, p_val in dated_principals:
        if not d_str or p_val is None or p_val <= 0:
            continue
        d0 = date.fromisoformat(d_str)
        yrs = (d_end - d0).days / 365.25
        if yrs >= 0.02:
            valid_lots.append((yrs, float(p_val)))
            tot_p += float(p_val)

    if not valid_lots or tot_p <= 0:
        return None, 0.0

    w_years = sum(y * p for y, p in valid_lots) / tot_p

    if len(valid_lots) == 1:
        yrs, p0 = valid_lots[0]
        rate = ((current_value / p0) ** (1.0 / yrs) - 1.0) * 100.0
        return round(rate, 2), round(yrs, 2)

    # Bisection over [-0.99, 10.0] (-99%/yr to +1000%/yr) for multi-lot money-weighted return
    lo, hi = -0.99, 10.0
    for _ in range(70):
        mid = 0.5 * (lo + hi)
        fv = sum(p * ((1.0 + mid) ** y) for y, p in valid_lots)
        if fv < current_value:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi) * 100.0, 2), round(w_years, 2)


def _format_valuation_source_tooltip(it: dict[str, Any]) -> str:
    if it["is_scrapeable"]:
        stmt_qty = it.get("stmt_quantity")
        iq_note = ""
        if stmt_qty is not None and abs(it["quantity"] - stmt_qty) > 1e-4:
            diff_q = it["quantity"] - stmt_qty
            iq_note = (
                f" Includes +{diff_q:,.3f} net shares vested after {it['stmt_date']} quarterly statement "
                f"({stmt_qty:,.3f} sh on {it['stmt_date']} -> {it['quantity']:,.3f} sh available)."
            )
        stmt_part = (
            f"Statement reference: ${it['stmt_price']:,.2f}/sh on {it['stmt_date']} (${it['stmt_value']:,.2f})."
            if it.get("stmt_price")
            else f"Statement date: {it['stmt_date']}."
        )
        return (
            f"Live Public Market Quote (Yahoo Finance API — {it['quote_symbol']}): "
            f"Available share count ({it['quantity']:,.3f} sh) multiplied by live split-adjusted "
            f"quote ${it['current_price']:,.2f}/sh as of {it['eval_date']}. {stmt_part}{iq_note}"
        )

    cls = it["asset_class"]
    inst_lbl = str(it.get("institution") or "custodian").strip()
    if cls in ("FIXED_ANNUITY", "INDEXED_ANNUITY", "RILA_ANNUITY"):
        return (
            f"Statement-Anchored Insurance Contract: "
            f"Private carrier contract with no public market ticker. Value (${it['current_value']:,.2f} as of "
            f"{it['stmt_date']}) anchored to latest {inst_lbl} statement; automatically updates whenever a newer "
            f"statement is added."
        )
    if cls == "MUNI_BOND":
        return (
            f"Statement Municipal Bond (CUSIP {it['symbol']}): "
            f"Institutional matrix-priced municipal bond (${it['current_value']:,.2f} market value as of {it['stmt_date']}, "
            f"price {it['stmt_price']:.3f} per $100 par). Updated automatically when new {inst_lbl} statements arrive; "
            f"click CUSIP link for public MSRB EMMA trade disclosures."
        )
    return (
        f"Statement Cash / Deposit Sweep ({it['account_id']}): "
        f"USD cash and FDIC sweep balance (${it['current_value']:,.2f}) reported on statement as of {it['stmt_date']}."
    )


def _resolve_item_principal_and_growth(
    it: dict[str, Any],
    overrides: dict[str, Any],
    stock_plan_acct_id: str = "",
) -> dict[str, Any]:
    """
    Determines initial principal investment, inception date, and annualized growth (`Total Growth`)
    for a single position using statement data (or optional `principal_overrides` in Portfolios/<active>/project.json).
    """
    ovr_key = f"{it['account_id']}:{it['security_id']}"
    ovr = overrides.get(ovr_key) or {}

    cur_val = it["current_value"]
    cls = it["asset_class"]
    snap_meta = it["snap_meta"]
    inst_lbl = str(it.get("institution") or "Custodian").strip()

    principal_val: float | None = ovr.get("principal")
    principal_date: str | None = ovr.get("inception_date")
    principal_kind = ""
    principal_sub = ""
    principal_tip = ""

    if principal_val is not None:
        principal_val = float(principal_val)
        principal_kind = "Initial Principal"
        principal_sub = f"Acquired {principal_date}" if principal_date else "Initial investment"
        adj_c = it.get("cost_basis")
        principal_tip = (
            f"Initial Principal Investment: ${principal_val:,.2f}"
            + (f" on {principal_date}." if principal_date else ".")
            + (
                f" (Statement adjusted tax-lot cost including reinvested distributions: ${adj_c:,.2f}.)"
                if adj_c and abs(adj_c - principal_val) > 1.0
                else ""
            )
        )
    if principal_val is None:
        if it.get("initial_premium"):
            principal_val = float(it["initial_premium"])
            principal_date = principal_date or it.get("issue_date")
            principal_kind = "Premium"
            principal_sub = f"Issued {principal_date}" if principal_date else "Initial premium"
            principal_tip = (
                f"Initial Contract Premium reported on statement: ${principal_val:,.2f}"
                + (f" on issue date {principal_date}." if principal_date else ".")
            )
        elif snap_meta.get("orig_cost"):
            principal_val = float(snap_meta["orig_cost"])
            adj_c = it.get("cost_basis")
            principal_date = principal_date or snap_meta.get("inception_date")
            principal_kind = "Orig Cost"
            principal_sub = (
                f"Issued {principal_date}"
                if principal_date
                else (f"Orig cost (Adj: ${adj_c:,.0f})" if adj_c else "Orig tax-lot cost")
            )
            principal_tip = (
                f"Original Principal Invested (ORIG COST on statement): ${principal_val:,.2f}. "
                + (f"Adjusted cost including reinvested distributions: ${adj_c:,.2f}. " if adj_c else "")
                + (
                    f"Acquired {principal_date}."
                    if principal_date
                    else "Statement consolidates lots as 'Acquired Net Tax Lots' without a single initial purchase date."
                )
            )
        elif it.get("cost_basis"):
            principal_val = float(it["cost_basis"])
            principal_date = principal_date or snap_meta.get("inception_date") or it.get("issue_date")
            principal_kind = "Cost Basis"
            principal_sub = f"Acquired {principal_date}" if principal_date else "Net tax lots (date N/A)"
            principal_tip = (
                f"Tax-Lot Principal / Cost Basis reported on statement: ${principal_val:,.2f}"
                + (
                    f" (acquired {principal_date})."
                    if principal_date
                    else " ('Acquired Net Tax Lots' — statement omits original purchase date)."
                )
            )
        elif cls == "MUNI_BOND":
            principal_val = float(it["quantity"])
            principal_kind = "Par Face"
            principal_sub = "Face par value"
            principal_tip = (
                f"Municipal Bond Face Par Principal: ${principal_val:,.2f}. "
                f"{inst_lbl} statements report par value, coupon ({it['meta'].get('coupon', '')}), "
                f"and current market yield ({it.get('yield_pct') or 0:.2f}%), without original secondary-market trade cost/date."
            )
        elif cls == "CASH":
            principal_val = cur_val
            principal_kind = "Cash"
            principal_sub = "Liquid USD"
            principal_tip = f"Liquid cash / FDIC sweep balance (${cur_val:,.2f}); 1:1 USD principal."
        elif stock_plan_acct_id and it["account_id"] == stock_plan_acct_id and it["asset_class"] == "EQUITY":
            award_lbl = str(it.get("award_type") or "Stock").strip() or "Stock"
            principal_str = f"{award_lbl} Grants ($0)"
            principal_sub = "Stock plan awards"
            principal_tip = (
                f"Vested employee {award_lbl} equity awards ($0 out-of-pocket cash purchase outlay; "
                "ordinary income tax withheld via shares at each release)."
            )
            return {
                "principal_val": None,
                "principal_date": None,
                "principal_str": principal_str,
                "principal_sub": principal_sub,
                "principal_tooltip": principal_tip,
                "growth_str": f"{award_lbl} Equity",
                "growth_sub": f"{it['quantity']:,.3f} vested sh",
                "growth_sign": 0,
                "growth_tooltip": (
                    f"Employee {award_lbl} awards vest at $0 cash outlay (ordinary income taxed at release via share withholding). "
                    "Market reference returns are available in the Scenario Forecast details."
                ),
            }

    if principal_val is None:
        return {
            "principal_val": None,
            "principal_date": None,
            "principal_str": "—",
            "principal_sub": "Cost N/A in statement",
            "principal_tooltip": (
                "Statement snapshot omits per-lot purchase cost basis and original account funding date."
            ),
            "growth_str": "—",
            "growth_sub": "Inception N/A",
            "growth_sign": 0,
            "growth_tooltip": (
                "Initial principal date & cost basis are not printed on the statement snapshot. "
                "Add an inception or tax-lot cost statement to compute annualized personal CAGR."
            ),
        }

    p_str = f"${principal_val:,.2f}"
    gain_usd = round(cur_val - principal_val, 2)
    gain_pct = (gain_usd / principal_val * 100.0) if principal_val > 0 else 0.0

    # Use statement valuation date for non-scrapeable anchored contracts (so annualized rate matches exact carrier as-of date)
    eval_end_iso = it["eval_date"] if it["is_scrapeable"] else (it["stmt_date"] or it["eval_date"])

    if cls == "CASH":
        return {
            "principal_val": principal_val,
            "principal_date": None,
            "principal_str": p_str,
            "principal_sub": principal_sub,
            "principal_tooltip": principal_tip,
            "growth_str": "Sweep Yield",
            "growth_sub": "Liquid cash",
            "growth_sign": 0,
            "growth_tooltip": "Cash & FDIC bank deposit sweep balance; interest accrues monthly into cash balance.",
        }

    if principal_date:
        cagr, yrs = solve_annualized_return([(principal_date, principal_val)], eval_end_iso, cur_val)
        if cagr is not None:
            sign = 1 if cagr > 0 else (-1 if cagr < 0 else 0)
            g_str = f"{'+' if cagr >= 0 else ''}{cagr:.2f}% / yr"
            g_sub = f"{'+' if gain_usd >= 0 else '-'}${abs(gain_usd):,.0f} ({'+' if gain_pct >= 0 else ''}{gain_pct:.1f}%)"
            g_tip = (
                f"Annualized Average Growth (CAGR): {'+' if cagr >= 0 else ''}{cagr:.2f}% / yr over {yrs:.2f} yrs "
                f"({principal_date} → {eval_end_iso}). "
                f"Initial Principal: ${principal_val:,.2f} → Current Value: ${cur_val:,.2f} "
                f"(Total Gain: {'+' if gain_usd >= 0 else '-'}${abs(gain_usd):,.2f} / {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%)."
            )
            return {
                "principal_val": principal_val,
                "principal_date": principal_date,
                "cagr_pct": round(cagr, 4),
                "principal_str": p_str,
                "principal_sub": principal_sub,
                "principal_tooltip": principal_tip,
                "growth_str": g_str,
                "growth_sub": g_sub,
                "growth_sign": sign,
                "growth_tooltip": g_tip,
            }

    # Known principal/par, but no inception date printed on statement
    if cls == "MUNI_BOND":
        yld = it.get("yield_pct")
        g_str = f"{yld:.2f}% Yld" if yld else f"{'+' if gain_pct >= 0 else ''}{gain_pct:.1f}% vs Par"
        g_sub = f"{'+' if gain_usd >= 0 else '-'}${abs(gain_usd):,.0f} vs Par"
        g_tip = (
            f"Current Market Value (${cur_val:,.2f}) vs. Par Face Principal (${principal_val:,.2f}): "
            f"{'+' if gain_usd >= 0 else '-'}${abs(gain_usd):,.2f} ({'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%). "
            f"Estimated annual tax-exempt coupon yield at market: {yld or 0:.2f}%/yr. "
            f"(Original secondary-market purchase date is not printed on {inst_lbl} statements.)"
        )
        return {
            "principal_val": principal_val,
            "principal_date": None,
            "principal_str": p_str,
            "principal_sub": principal_sub,
            "principal_tooltip": principal_tip,
            "growth_str": g_str,
            "growth_sub": g_sub,
            "growth_sign": 1 if gain_usd >= 0 else -1,
            "growth_tooltip": g_tip,
        }

    sign = 1 if gain_usd > 0 else (-1 if gain_usd < 0 else 0)
    g_str = f"{'+' if gain_pct >= 0 else ''}{gain_pct:.1f}% total"
    g_sub = f"{'+' if gain_usd >= 0 else '-'}${abs(gain_usd):,.0f} (date N/A)"
    g_tip = (
        f"Cumulative Growth vs. {principal_kind} (${principal_val:,.2f}): "
        f"{'+' if gain_usd >= 0 else '-'}${abs(gain_usd):,.2f} ({'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%). "
        f"Annualized %/yr requires an initial purchase date (statement reports consolidated 'Net Tax Lots')."
    )
    return {
        "principal_val": principal_val,
        "principal_date": None,
        "principal_str": p_str,
        "principal_sub": principal_sub,
        "principal_tooltip": principal_tip,
        "growth_str": g_str,
        "growth_sub": g_sub,
        "growth_sign": sign,
        "growth_tooltip": g_tip,
    }


def _resolve_effective_tax_rates(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Derives statutory + configured tax rates strictly from `cfg['tax_rates']` without state-specific fallbacks."""
    raw_cfg = cfg or {}
    tr = dict(raw_cfg.get("tax_rates") or {}) if isinstance(raw_cfg.get("tax_rates"), dict) else {}
    state_code = str(tr.get("state_code") or raw_cfg.get("household_state") or "").strip().upper()
    fed_ltcg = float(tr["fed_ltcg_top"]) if tr.get("fed_ltcg_top") is not None else 0.20
    niit = float(tr["niit_rate"]) if tr.get("niit_rate") is not None else 0.038
    fed_ord = float(tr["fed_ordinary_top"]) if tr.get("fed_ordinary_top") is not None else 0.37
    preset_sr = float((US_STATE_TAX_PRESETS.get(state_code) or {}).get("state_rate", 0.0)) if state_code else 0.0
    if tr.get("state_rate") is not None:
        state_rate = float(tr["state_rate"])
    elif tr.get("ltcg_top") is not None:
        state_rate = max(0.0, round(float(tr["ltcg_top"]) - fed_ltcg - niit, 4))
    else:
        state_rate = preset_sr
    ltcg_top = float(tr["ltcg_top"]) if tr.get("ltcg_top") is not None else round(fed_ltcg + niit + state_rate, 4)
    ord_top = float(tr["ordinary_top"]) if tr.get("ordinary_top") is not None else round(fed_ord + niit + state_rate, 4)
    cash_tax = (
        float(raw_cfg["sim_cash_interest_tax_rate"])
        if raw_cfg.get("sim_cash_interest_tax_rate") is not None
        else ord_top
    )
    ira_tax = (
        float(raw_cfg["sim_ira_ordinary_tax_rate"])
        if raw_cfg.get("sim_ira_ordinary_tax_rate") is not None
        else round(fed_ord + state_rate, 4)
    )
    out_tr = dict(tr)
    if state_code and "state_code" not in out_tr:
        out_tr["state_code"] = state_code
    out_tr.setdefault("state_rate", state_rate)
    out_tr.setdefault("ltcg_top", ltcg_top)
    out_tr.setdefault("ordinary_top", ord_top)
    out_tr.setdefault("fed_ltcg_top", fed_ltcg)
    out_tr.setdefault("niit_rate", niit)
    out_tr.setdefault("fed_ordinary_top", fed_ord)
    return {
        "tax_rates": out_tr,
        "state_code": state_code,
        "state_rate": state_rate,
        "fed_ltcg_top": fed_ltcg,
        "niit_rate": niit,
        "fed_ordinary_top": fed_ord,
        "ltcg_top": ltcg_top,
        "ordinary_top": ord_top,
        "cash_interest_tax_rate": cash_tax,
        "ira_ordinary_tax_rate": ira_tax,
    }


def _friendly_account_label(
    aid: str,
    accounts: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """
    Returns a human-readable, friendly account identifier formatted as:
      '<Institution> • <Friendly Account Name> (<Account ID>)'
    e.g., 'Charles Schwab • Joint Brokerage (SCHW-1001)' or 'Fidelity • Stock Plan (FID-2001)'.
    """
    if not aid:
        return ""
    accts_db = accounts or {}
    cfg_accts = (cfg or {}).get("accounts") or {}
    acc = accts_db.get(aid) or cfg_accts.get(aid) or {}
    c_entry = cfg_accts.get(aid) or {}
    if c_entry.get("friendly_account_name"):
        return str(c_entry["friendly_account_name"])

    # If the user has configured a custom friendly name in project.json, honor it immediately.
    c_entry = cfg_accts.get(aid) or {}
    if c_entry.get("friendly_account_name"):
        return str(c_entry["friendly_account_name"])

    inst = acc.get("institution") or c_entry.get("institution") or "Brokerage"
    inst_short = inst.replace(" Advisors", "").replace(" Bank", "").strip()

    aname = str(c_entry.get("account_name") or acc.get("account_name") or "")
    if aname:
        return f"{inst_short} • {aname} ({aid})" if inst_short and inst_short not in aname else f"{aname} ({aid})"
    atype = str(acc.get("account_type") or "Account")
    return f"{inst_short} • {atype} ({aid})" if inst_short else f"{atype} ({aid})"


def _format_delta_tooltip(
    metric_title: str,
    metric_delta_str: str,
    measured_against: str,
    valuation_date: str,
    reported_on: str = "",
    reported_date: str = "",
    reported_source: str = "",
    extra_note: str = "",
) -> str:
    parts = [
        f"{metric_title}: {metric_delta_str}",
        f"• Measured against: {measured_against}",
        f"• Valuation date: {valuation_date}",
    ]
    # Check if the date when the measurement was reported is DIFFERENT from the date the measurement represents
    val_m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", valuation_date)
    val_iso = val_m.group(0) if val_m else ""

    rep_iso = reported_date
    if not rep_iso and reported_on:
        rep_m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", reported_on)
        if rep_m:
            rep_iso = rep_m.group(0)

    # Consistent language for when the measurement was reported is ONLY included
    # if that date is different from the date the measurement represents.
    if rep_iso and val_iso and rep_iso != val_iso:
        if reported_on:
            line = reported_on.strip().rstrip(".")
            if line.lower().startswith("reported on:"):
                line = line[12:].strip()
            parts.append(f"• Reported on: {line}.")
        else:
            src = reported_source or "Statement"
            parts.append(f"• Reported on: {src} dated {rep_iso}.")

    if extra_note:
        parts.append(f"• Note: {extra_note}")
    return "\n".join(parts)


def _derive_carrier_statement_name(it: dict[str, Any]) -> str:
    meta_carrier = str((it.get("meta") or {}).get("carrier") or "").strip()
    if meta_carrier:
        return meta_carrier
    raw = str(it.get("meta", {}).get("raw_title") or it.get("canonical_name") or it.get("name") or "").strip()
    if raw:
        m_prefix = re.split(r"\b(?:MYG|PAC\s+INDEX|POWER\s+INDEX|STRUCTURED|FIXED|INDEXED|RILA|FIA|ANNUITY)\b", raw, maxsplit=1, flags=re.IGNORECASE)
        cand = m_prefix[0].strip(" -—•")
        if cand and len(cand) >= 3:
            return cand.title() if cand.isupper() else cand
    inst = str(it.get("institution") or "")
    if inst:
        return f"{inst} statement"
    return "Insurance carrier statement"


def _resolve_item_recent_change(
    it: dict[str, Any],
    today_iso: str,
    prior_iso: str = "",
) -> dict[str, Any]:
    """
    Computes a context-appropriate recent performance indicator with consistent,
    unambiguous language stating:
    1. What date the change is measured against (the starting baseline date/value).
    2. What date the measurement was taken / valued as of.
    3. What statement or quote feed reported the measurement.
    """
    cur_val = it["current_value"]
    cls = it.get("asset_class") or ""
    is_scrapeable = it.get("is_scrapeable", False)
    w_chg = it.get("wow_change", 0.0)
    stmt_dt = it.get("stmt_date") or ""
    issue_dt = it.get("issue_date") or it.get("principal_date") or ""
    if not prior_iso:
        try:
            t_date = date.fromisoformat(today_iso)
            prior_iso = (t_date - timedelta(days=7)).isoformat()
        except Exception:
            prior_iso = ""

    # 1. Scrapeable equities / public securities (stocks, ETFs, mutual funds)
    if is_scrapeable:
        prev_val = cur_val - w_chg
        pct = (w_chg / prev_val * 100.0) if prev_val > 0 else 0.0
        sign = 1 if w_chg > 0 else (-1 if w_chg < 0 else 0)
        pct_sign = "+" if pct >= 0 else ""
        val_sign = "+" if w_chg >= 0 else "-"
        base_date_str = f" (vs {prior_iso})" if prior_iso else ""
        sub = f"{pct_sign}{pct:.1f}% • 1w{base_date_str}"
        tip = _format_delta_tooltip(
            "1-Week Market Price Change",
            f"{val_sign}${abs(w_chg):,.2f} ({pct_sign}{pct:.2f}%)",
            f"Prior week Friday market close on {prior_iso or 'prior week'}.",
            f"{today_iso} (market quote as of latest exchange close).",
            reported_date=today_iso,
        )
        return {
            "recent_change_val": w_chg,
            "recent_change_sub": sub,
            "recent_change_tip": tip,
            "recent_change_sign": sign,
        }

    # 2. Annuities (Contractual gain since inception)
    if "ANNUITY" in cls or it.get("initial_premium") or ("RILA" in it.get("symbol", "")):
        p_val = float(it.get("initial_premium") or it.get("principal_val") or 0.0)
        carrier = _derive_carrier_statement_name(it)
        rep_dt = it.get("reported_date") or ""
        rep_src = it.get("reported_source") or (f"{carrier} statement" if carrier else "Brokerage statement")
        if p_val > 0:
            gain = round(cur_val - p_val, 2)
            gpct = (gain / p_val * 100.0)
            cagr = it.get("cagr_pct")
            sign = 1 if gain > 0 else (-1 if gain < 0 else 0)
            val_sign = "+" if gain >= 0 else "-"
            cagr_part = f", {cagr:+.2f}%/yr CAGR" if cagr is not None else ""

            base_date_str = f" (vs {issue_dt})" if issue_dt else ""
            if cagr is not None:
                cagr_sign = "+" if cagr >= 0 else ""
                sub = f"{cagr_sign}{cagr:.2f}%/yr • Inception{base_date_str}"
            else:
                gpct_sign = "+" if gpct >= 0 else ""
                sub = f"{gpct_sign}{gpct:.1f}% • Inception{base_date_str}"

            base_desc = (
                f"Initial contract premium of ${p_val:,.2f} on issue date {issue_dt}."
                if issue_dt
                else f"Initial contract premium of ${p_val:,.2f} at contract inception."
            )
            tip = _format_delta_tooltip(
                "Contractual Gain Since Inception",
                f"{val_sign}${abs(gain):,.2f} ({gpct:+.2f}% total{cagr_part})",
                base_desc,
                f"{stmt_dt or today_iso} ({carrier} carrier valuation).",
                reported_date=rep_dt,
                reported_source=rep_src,
            )
            return {
                "recent_change_val": gain,
                "recent_change_sub": sub,
                "recent_change_tip": tip,
                "recent_change_sign": sign,
            }
        else:
            date_str = f" (as of {stmt_dt})" if stmt_dt else ""
            return {
                "recent_change_val": 0.0,
                "recent_change_sub": f"Statement{date_str}",
                "recent_change_tip": _format_delta_tooltip(
                    "Statement Valuation",
                    f"${cur_val:,.2f}",
                    "Contract baseline.",
                    f"{stmt_dt or today_iso}.",
                    reported_date=rep_dt,
                    reported_source=rep_src,
                ),
                "recent_change_sign": 0,
            }

    # 3. Municipal Bonds
    if cls == "MUNI_BOND":
        accr = round(float(it.get("accrued_interest") or 0.0), 2)
        yld = float(it.get("yield_pct") or 0.0)
        par = float(it.get("quantity") or 0.0)
        inst = it.get("institution") or "Custodian"
        rep_dt = it.get("reported_date") or stmt_dt
        rep_src = it.get("reported_source") or f"{inst} monthly statement"
        date_str = f" (as of {stmt_dt})" if stmt_dt else ""
        if accr > 0:
            sub = f"{yld:.2f}% Yld • Accrued{date_str}"
            tip = _format_delta_tooltip(
                "Accrued Coupon Interest",
                f"+${accr:,.2f} ({yld:.2f}%/yr current market yield)",
                f"Cumulative coupon interest earned since prior semi-annual payment cycle through statement date {stmt_dt or today_iso}.",
                f"{stmt_dt or today_iso} ({inst} monthly statement period close).",
                reported_date=rep_dt,
                reported_source=rep_src,
            )
            return {
                "recent_change_val": accr,
                "recent_change_sub": sub,
                "recent_change_tip": tip,
                "recent_change_sign": 1,
            }
        else:
            diff = round(cur_val - par, 2)
            sign = 1 if diff >= 0 else -1
            diff_sign = "+" if diff >= 0 else "-"
            diff_pct = (diff / par * 100.0) if par > 0 else 0.0
            sub = f"{diff_pct:+.1f}% • vs Par{date_str}"
            tip = _format_delta_tooltip(
                "Market Value vs. Par",
                f"{diff_sign}${abs(diff):,.2f} ({diff_pct:+.2f}% premium/discount, {yld:.2f}%/yr yield)",
                f"Face par principal of ${par:,.2f}.",
                f"{stmt_dt or today_iso} ({inst} monthly statement period close).",
                reported_date=rep_dt,
                reported_source=rep_src,
            )
            return {
                "recent_change_val": diff,
                "recent_change_sub": sub,
                "recent_change_tip": tip,
                "recent_change_sign": sign,
            }

    # 4. Cash / Sweeps
    if cls == "CASH":
        cash_apy_pct = float(it.get("yield_pct") if it.get("yield_pct") is not None else (it.get("cash_apy_pct") or 0.0))
        mo_int = round(cur_val * (cash_apy_pct / 100.0) / 12.0, 2)
        inst = it.get("institution") or "Brokerage"
        rep_dt = it.get("reported_date") or stmt_dt
        rep_src = it.get("reported_source") or f"{inst} statement"
        date_str = f" (as of {stmt_dt})" if stmt_dt else ""
        apy_lbl = f"~{cash_apy_pct:.1f}% APY" if cash_apy_pct > 0 else "0.0% APY"
        sub = f"{apy_lbl} • 1mo{date_str}"
        tip = _format_delta_tooltip(
            "Estimated Monthly Sweep Interest",
            f"+${mo_int:,.2f}/mo (~{cash_apy_pct:.2f}% APY)",
            f"Liquid cash and FDIC sweep balance of ${cur_val:,.2f} over trailing 30 days.",
            f"{stmt_dt or today_iso} ({inst} statement period close).",
            reported_date=rep_dt,
            reported_source=rep_src,
        )
        return {
            "recent_change_val": mo_int,
            "recent_change_sub": sub,
            "recent_change_tip": tip,
            "recent_change_sign": 1 if mo_int > 0 else 0,
        }

    # 5. Fallback
    if w_chg != 0.0:
        prev_val = cur_val - w_chg
        pct = (w_chg / prev_val * 100.0) if prev_val > 0 else 0.0
        sign = 1 if w_chg > 0 else (-1 if w_chg < 0 else 0)
        base_date_str = f" (vs {prior_iso})" if prior_iso else ""
        return {
            "recent_change_val": w_chg,
            "recent_change_sub": f"{'+' if pct >= 0 else ''}{pct:.1f}% • 1w{base_date_str}",
            "recent_change_tip": _format_delta_tooltip(
                "1-Week Price Delta",
                f"{'+' if w_chg >= 0 else '-'}${abs(w_chg):,.2f} ({pct:+.2f}%)",
                f"Valuation as of {prior_iso or 'prior week'}.",
                f"{today_iso}.",
            ),
            "recent_change_sign": sign,
        }

    date_str = f" (as of {stmt_dt})" if stmt_dt else ""
    return {
        "recent_change_val": 0.0,
        "recent_change_sub": f"Statement{date_str}",
        "recent_change_tip": _format_delta_tooltip(
            "Statement Valuation",
            f"${cur_val:,.2f}",
            "Position baseline on statement.",
            f"{stmt_dt or today_iso}.",
        ),
        "recent_change_sign": 0,
    }


_SHORT_COMPANY_ALIASES: dict[str, str] = {
    "alphabet": "Google",
    "alphabet inc": "Google",
    "alphabet inc.": "Google",
    "goog": "Google",
    "googl": "Google",
    "apple inc": "Apple",
    "apple inc.": "Apple",
    "meta platforms": "Meta",
    "meta platforms, inc.": "Meta",
    "microsoft corporation": "Microsoft",
    "nvidia corporation": "NVIDIA",
    "amazon.com, inc.": "Amazon",
}


def _derive_short_company_name(symbol: str, company_name: str = "", cfg: dict[str, Any] | None = None) -> str:
    sym = (symbol or "").strip().upper()
    custom_cfg = ((cfg or {}).get("grant_companies") or {}).get(sym) or {}
    if custom_cfg.get("short_name"):
        return str(custom_cfg["short_name"]).strip()
    raw = (company_name or "").strip()
    if raw.lower() in _SHORT_COMPANY_ALIASES:
        return _SHORT_COMPANY_ALIASES[raw.lower()]
    if sym.lower() in _SHORT_COMPANY_ALIASES and (not raw or raw.upper() == sym):
        return _SHORT_COMPANY_ALIASES[sym.lower()]
    if raw and raw.upper() != sym:
        cleaned = re.sub(
            r",?\s*\b(incorporated|inc\.?|corporation|corp\.?|company|co\.?|ltd\.?|llc|plc|holdings?|group)\b\.?",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
        if cleaned:
            return cleaned
    return sym or "Equity"


def _compute_gsu_net_retention_rate(
    state: dict[str, Any],
    today_date: date,
    cfg: dict[str, Any],
    symbol: str | None = None,
) -> float:
    """
    Derives the net-of-tax share retention rate from the actual release history
    (CodeReview A5) over a trailing window, scoped optionally to `symbol`.
    Falls back to `gsu_forecast.default_net_retention_rate` when history is thin.
    """
    gsu_cfg = cfg.get("grant_forecast") or cfg.get("gsu_forecast") or {}
    cfg_default = float(gsu_cfg.get("default_net_retention_rate", 0.5))
    window_months = int(gsu_cfg.get("retention_trailing_months", 24))
    try:
        cutoff = today_date.replace(year=today_date.year - (window_months // 12),
                                    month=today_date.month) if window_months >= 12 else today_date
        cutoff_iso = cutoff.isoformat()
    except ValueError:
        cutoff_iso = today_date.replace(day=28).isoformat()
    today_iso = today_date.isoformat()
    sym_up = symbol.strip().upper() if symbol else None
    recent = [
        r for r in state.get("gsu_releases_history", [])
        if cutoff_iso <= r["vest_date"] <= today_iso
        and (sym_up is None or (r.get("symbol") or "").strip().upper() == sym_up)
    ]
    gross = sum(float(r["gross_shares"]) for r in recent)
    net = sum(float(r["net_shares"]) for r in recent)
    min_samples = int(gsu_cfg.get("min_retention_samples", 1))
    if gross > 0 and net > 0 and len(recent) >= min_samples:
        return round(net / gross, 4)
    return cfg_default


def build_report_payload(
    db: PortfolioDB,
    cfg: dict[str, Any],
    today_date: date,
    window_start: date | None = None,
    window_end: date | None = None,
    initial_preset: str = "all",
    tax_mode: str = "net",
    bypass_export_masking: bool = False,
    active_portfolio_id: str | None = None,
) -> dict[str, Any]:
    """
    Constructs a pure-data `ReportPayload` dictionary from `PortfolioDB` for `html_report.py`.
    """
    state = _load_portfolio_state(db, cfg)
    primary_sym_cfg = str(cfg.get("primary_equity_symbol") or "").strip().upper()
    try:
        five_yrs_ago_iso = today_date.replace(year=today_date.year - 5).isoformat()
    except ValueError:
        five_yrs_ago_iso = today_date.replace(year=today_date.year - 5, day=28).isoformat()
    price_book = load_price_book(db)
    if primary_sym_cfg and price_book.earliest_price(primary_sym_cfg) is not None and price_book.price_with_date_on_or_before(primary_sym_cfg, five_yrs_ago_iso) is None:
        sync_exact_dates_for_symbol(db, primary_sym_cfg, [five_yrs_ago_iso])
        price_book = load_price_book(db)
    today_iso = today_date.isoformat()
    overrides = dict(cfg.get("principal_overrides") or {})
    gsu_cfg = cfg.get("grant_forecast") or cfg.get("gsu_forecast") or {}
    cfg_accts = cfg.get("accounts") or {}
    db_p = Path(db.db_path) if str(getattr(db, "db_path", "")) != ":memory:" else None
    resolved_sp = resolve_target_account_id(
        gsu_cfg.get("target_vested_account_id") or "",
        state["accounts"].keys(),
        cfg=cfg,
        db_path=db_p,
    )
    stock_plan_acct_id = resolved_sp or next(
        (
            aid for aid, a in state["accounts"].items()
            if (cfg_accts.get(aid) or {}).get("is_stock_plan")
            or "STOCK_PLAN" in str(a.get("account_type") or "").upper()
            or "STOCK PLAN" in str(a.get("account_type") or "").upper()
            or "STOCK PLAN" in str(a.get("account_name") or "").upper()
        ),
        gsu_cfg.get("target_vested_account_id") or "",
    )

    cur_eval = evaluate_portfolio_at_date(today_iso, today_iso, state, price_book, cfg)

    ref_today_row = db.conn.execute(
        "SELECT bar_date FROM timeline_bars WHERE bar_kind = 'TODAY' ORDER BY bar_date DESC LIMIT 1"
    ).fetchone()
    ref_today_dt = date.fromisoformat(ref_today_row["bar_date"]) if ref_today_row else today_date
    prior_iso = (ref_today_dt - timedelta(days=7)).isoformat()
    prev_eval = evaluate_portfolio_at_date(prior_iso, today_iso, state, price_book, cfg)
    prev_map = {
        (it["account_id"], it["security_id"]): it["market_value"]
        for it in prev_eval["items"]
    }

    total_now = cur_eval["vested_total"]
    total_prev = prev_eval["vested_total"]
    wow_diff = round(total_now - total_prev, 2)
    wow_pct = round((wow_diff / total_prev * 100.0), 2) if total_prev > 0 else 0.0

    try:
        file_rows = db.conn.execute(
            "SELECT sha256_hash, statement_as_of_date, rel_path, doc_type, account_id FROM ingested_files"
        ).fetchall()
        file_map = {r["sha256_hash"]: dict(r) for r in file_rows}
    except Exception:
        file_map = {}

    enriched_items: list[dict[str, Any]] = []
    live_scraped_sum = 0.0
    goog_family_sum = 0.0
    goog_family_shares = 0.0
    ms_goog_sum = 0.0
    ms_goog_shares = 0.0
    primary_sym_family = {primary_sym_cfg} | ({"GOOGL"} if primary_sym_cfg == "GOOG" else set()) if primary_sym_cfg else set()
    cash_cfg_apy = float(((cfg.get("sim_asset_rates") or {}).get("cash_sweeps") or {}).get("annual_rate_pct", 0.0))

    for it in sorted(cur_eval["items"], key=lambda x: x["market_value"], reverse=True):
        key = (it["account_id"], it["security_id"])
        p_val = prev_map.get(key, it["market_value"])
        w_chg = round(it["market_value"] - p_val, 2)
        if it["is_scrapeable"]:
            live_scraped_sum += it["market_value"]
        if primary_sym_family and it["symbol_or_code"] in primary_sym_family:
            goog_family_sum += it["market_value"]
            goog_family_shares += it["quantity"]
            if stock_plan_acct_id and it["account_id"] == stock_plan_acct_id:
                ms_goog_sum += it["market_value"]
                ms_goog_shares += it["quantity"]

        snap = it["ref_snapshot"] or {}
        snap_meta = json.loads(snap.get("details_json") or "{}")
        anc = it["latest_anchor"] or {}
        stmt_dt = anc.get("value_date") or snap.get("as_of_date") or ""
        stmt_mv = float(snap.get("stmt_market_value") or it["market_value"])
        stmt_pr = snap.get("stmt_price")

        src_hash = anc.get("source_hash") or snap.get("source_hash") or ""
        finfo = file_map.get(src_hash, {})
        rep_dt = finfo.get("statement_as_of_date") or ""
        acct_meta = state["accounts"].get(it["account_id"], {})
        inst = acct_meta.get("institution") or "Brokerage"
        rep_source = f"{inst} brokerage statement"

        # Maturity metadata (only per-account/per-security where applicable)
        sec_meta = it["security_meta"] or {}
        anc_meta = json.loads(anc.get("metadata_json") or "{}") if anc else {}
        merged_meta = {**sec_meta, **anc_meta, **snap_meta}

        maturity_date = merged_meta.get("maturity_date")
        term_label = merged_meta.get("term_label")
        maturity_str: str | None = None
        if maturity_date:
            maturity_str = f"Matures: {maturity_date}" + (f" ({term_label})" if term_label else "")

        base_item = {
            "account_id": it["account_id"],
            "security_id": it["security_id"],
            "institution": inst,
            "symbol": it["symbol_or_code"],
            "quote_symbol": it["quote_symbol"],
            "name": it["canonical_name"],
            "asset_class": it["asset_class"],
            "stack_bucket": it["stack_bucket"],
            "is_scrapeable": it["is_scrapeable"],
            "provenance": it["provenance"],
            "quantity": it["quantity"],
            "stmt_quantity": float(snap["quantity"]) if snap.get("quantity") is not None else None,
            "eval_date": today_iso,
            "stmt_date": stmt_dt,
            "reported_date": rep_dt,
            "reported_source": rep_source,
            "stmt_price": float(stmt_pr) if stmt_pr is not None else None,
            "stmt_value": round(stmt_mv, 2),
            "cost_basis": float(snap["cost_basis"]) if snap.get("cost_basis") is not None else None,
            "yield_pct": float(snap["yield_pct"]) if snap.get("yield_pct") is not None else None,
            "cash_apy_pct": cash_cfg_apy,
            "current_price": round(float(it["unit_price"]), 4),
            "current_value": round(float(it["market_value"]), 2),
            "wow_change": w_chg,
            "pct_portfolio": round(it["market_value"] / total_now * 100.0, 2) if total_now > 0 else 0.0,
            "surrender_value": anc.get("surrender_value") if anc.get("surrender_value") is not None else snap.get("surrender_value"),
            "accrued_interest": float(snap.get("accrued_interest") or 0.0),
            "issue_date": anc.get("issue_date") or snap_meta.get("inception_date"),
            "initial_premium": anc.get("initial_premium"),
            "maturity_date": maturity_date,
            "maturity_str": maturity_str,
            "meta": merged_meta,
            "snap_meta": snap_meta,
        }
        base_item["source_tooltip"] = _format_valuation_source_tooltip(base_item)
        base_item["pct_tooltip"] = (
            f"{base_item['pct_portfolio']:.2f}% of Current Vested Portfolio "
            f"(${base_item['current_value']:,.2f} ÷ ${total_now:,.2f}). "
            f"Excludes unvested future equity grant releases."
        )
        base_item.update(_resolve_item_principal_and_growth(base_item, overrides, stock_plan_acct_id=stock_plan_acct_id))
        base_item.update(_resolve_item_recent_change(base_item, today_iso, prior_iso=prior_iso))
        enriched_items.append(base_item)

    active_portfolio_id = active_portfolio_id if active_portfolio_id is not None else get_active_portfolio_id()
    active_saves_dir = get_saves_dir()
    db_prefs = db.get_holding_preferences()
    global_prefs = load_global_preferences(active_saves_dir) if active_saves_dir is not None else {
        "schema_version": 1,
        "friendly_names": {},
        "global_asset_risk_classes": {},
    }
    holding_prefs = {**db_prefs}
    for k, v in global_prefs.get("friendly_names", {}).items():
        if k not in holding_prefs:
            holding_prefs[k] = v
        elif not holding_prefs[k].get("friendly_name") and v.get("friendly_name"):
            holding_prefs[k]["friendly_name"] = v["friendly_name"]

    holdings_rollups = _build_strategy_rollups(enriched_items, state["accounts"], total_now, today_iso, overrides, holding_prefs=holding_prefs, cfg=cfg, prior_iso=prior_iso)
    account_rollups = _build_account_rollups(enriched_items, state["accounts"], total_now, today_iso, overrides, holding_prefs=holding_prefs, cfg=cfg, prior_iso=prior_iso)

    # Fetch timeline bars across requested window
    q_clauses = []
    q_params: list[Any] = []
    if window_start:
        q_clauses.append("bar_date >= ?")
        q_params.append(window_start.isoformat())
    if window_end:
        q_clauses.append("bar_date <= ?")
        q_params.append(window_end.isoformat())
    where_sql = ("WHERE " + " AND ".join(q_clauses)) if q_clauses else ""

    bar_rows = db.conn.execute(
        f"SELECT * FROM timeline_bars {where_sql} ORDER BY bar_date ASC",
        q_params,
    ).fetchall()

    timeline: list[dict[str, Any]] = []
    for r in bar_rows:
        dt = date.fromisoformat(r["bar_date"])
        is_today = r["bar_kind"] == "TODAY" or r["bar_date"] == today_iso
        timeline.append(
            {
                "iso": r["bar_date"],
                "shortLbl": "Today*" if is_today else dt.strftime("%b %d '%y"),
                "monthLbl": dt.strftime("%b '%y"),
                "yearLbl": str(dt.year),
                "kind": r["bar_kind"],
                "isToday": is_today,
                "vestedTotal": round(float(r["vested_total"]), 2),
                "projRelGross": round(float(r["projected_release_gross"]), 2),
                "projRelShares": round(float(r["projected_release_shares"]), 3),
                "buckets": json.loads(r["bucket_totals_json"]),
                "accounts": json.loads(r["account_totals_json"]),
                "assets": json.loads(r["asset_totals_json"]),
            }
        )

    goog_live_pair = price_book.price_with_date_on_or_before(primary_sym_cfg, today_iso) if primary_sym_cfg else None
    goog_live = goog_live_pair[1] if goog_live_pair else 0.0
    goog_prev_close = 0.0
    if goog_live_pair and primary_sym_cfg:
        prev_lookup_iso = (date.fromisoformat(goog_live_pair[0]) - timedelta(days=1)).isoformat()
        goog_prev_close = price_book.price_on_or_before(primary_sym_cfg, prev_lookup_iso) or 0.0
    net_rate = _compute_gsu_net_retention_rate(state, today_date, cfg)
    future_tranches = [t for t in state["gsu_tranches"] if t["vest_date"] > today_iso]
    hist_releases_to_date = [r for r in state.get("gsu_releases_history", []) if r["vest_date"] <= today_iso]
    tot_vested_to_date_net_sh = round(sum(float(r["net_shares"]) for r in hist_releases_to_date), 3)
    tot_vested_to_date_gross_sh = round(sum(float(r["gross_shares"]) for r in hist_releases_to_date), 3)
    tot_vested_to_date_net_val = round(tot_vested_to_date_net_sh * goog_live, 2)
    tot_vested_to_date_gross_val = round(tot_vested_to_date_gross_sh * goog_live, 2)

    bucket_summary = _build_bucket_appreciation_summary(
        enriched_items=enriched_items,
        holdings_rollups=holdings_rollups,
        price_book=price_book,
        today_date=today_date,
        total_now=total_now,
        goog_live=goog_live,
        stock_plan_acct_id=stock_plan_acct_id,
        primary_sym=primary_sym_cfg,
        accounts=state["accounts"],
    )

    stock_grants = _build_stock_grants_analytics(
        state=state,
        price_book=price_book,
        cfg=cfg,
        today_date=today_date,
        today_iso=today_iso,
        goog_live=goog_live,
        cur_items=cur_eval["items"],
        total_now=total_now,
    )
    default_grant_sym = stock_grants.get("default_symbol")
    google_stock = (
        stock_grants["by_symbol"].get(primary_sym_cfg)
        or (stock_grants["by_symbol"].get(default_grant_sym) if default_grant_sym else None)
        or _build_google_stock_analytics(
            state=state,
            price_book=price_book,
            cfg=cfg,
            today_iso=today_iso,
            goog_live=goog_live,
            net_rate=net_rate,
        )
    )

    retirement_sim_assets = _build_retirement_sim_assets(
        state=state,
        holdings_rollups=holdings_rollups,
        google_stock=google_stock,
        goog_live=goog_live,
        today_iso=today_iso,
        cfg=cfg,
        stock_grants=stock_grants,
    )

    recent_events = [
        dict(r)
        for r in db.conn.execute(
            "SELECT occurred_at_iso, event_type, account_id, security_id, as_of_date, summary "
            "FROM ingest_events ORDER BY event_id DESC LIMIT 35"
        ).fetchall()
    ]

    acct_cfg_map = cfg.get("accounts") or {}
    account_schemas = [
        {
            "id": aid,
            "label": f"{acc['account_name']} ({aid})",
            "color": _sanitize_non_exclusive_color(
                (acct_cfg_map.get(aid) or {}).get("color") or DEFAULT_ACCOUNT_PALETTE[idx % len(DEFAULT_ACCOUNT_PALETTE)],
                idx,
            ),
        }
        for idx, (aid, acc) in enumerate(state["accounts"].items())
    ]

    sanitized_stack_buckets = [
        {
            **b,
            "color": _sanitize_non_exclusive_color(b.get("color"), idx),
        }
        for idx, b in enumerate(cfg.get("stack_buckets") or [])
    ]

    # Attach canonical `forecast_key`, `parent_forecast_keys`, `default_growth_pct`, `ref_growth_pct`,
    # `default_risk_class`, and 10Y realized vol/beta provenance across Screen 1 and Screen 3.
    baselines_map = db.get_holding_forecasts_baseline()
    def current_evidence(row: dict[str, Any]) -> bool:
        try:
            entries = json.loads(row.get("growth_evidence_json") or "[]")
            tiers = [e["tier"] for e in entries]
            return (3 in tiers and entries[0].get("version") == EVIDENCE_VERSION
                    and tiers == sorted(tiers) and all(t in (1, 2, 3) for t in tiers)
                    and all(isinstance(e["projected_return_pct"], (int, float))
                            and math.isfinite(e["projected_return_pct"]) for e in entries)
                    and entries[0]["projected_return_pct"] == row.get("projected_return_pct"))
        except (TypeError, KeyError, ValueError):
            return False

    needs_growth_evidence = any(
        not current_evidence(baselines_map.get((item["account_id"], item["security_id"])) or {})
        or not current_evidence(baselines_map.get(("", item["security_id"])) or {})
        for item in enriched_items
    )
    if needs_growth_evidence and enriched_items:
        enrich_holding_forecasts(db=db, cfg=cfg, today_date=today_date, sync_prices=False)
        baselines_map = db.get_holding_forecasts_baseline()

    # Estimate SPY 10Y annualized volatility from `market_prices` for 1-factor sleeve variance roll-up
    spy_vol_pct = estimate_benchmark_vol_pct(db, today_date)

    primary_sym_up = primary_sym_cfg
    def _lookup_baseline(aid: str | None, sid: str | None, fallback_rc: str = "broad_equity") -> dict[str, Any]:
        a_str = str(aid or "")
        s_str = str(sid or "")
        bl = baselines_map.get((a_str, s_str))
        if bl and a_str and current_evidence(bl):
            return bl
        shared = baselines_map.get(("", s_str))
        if shared and current_evidence(shared):
            return {**shared, "shared_reference": True}
        evidence = [_class_growth_evidence(fallback_rc, today_date)]
        return {
            "risk_class": fallback_rc if fallback_rc in RISK_CLASSES else "broad_equity",
            "projected_return_pct": evidence[0]["projected_return_pct"],
            "growth_evidence_json": json.dumps(evidence),
            "realized_vol_pct": 0.0 if fallback_rc == "cash_fixed" else None,
            "beta_spy": 0.0 if fallback_rc == "cash_fixed" else None,
            "corr_spy": 0.0 if fallback_rc == "cash_fixed" else None,
            "lookback_years": None,
            "return_source": "FALLBACK_DEFAULT",
            "confidence": "MEDIUM",
            "source_detail": evidence[0]["source_detail"],
        }

    def _apply_baseline_fields(target: dict[str, Any], bl: dict[str, Any]) -> None:
        rc_b = str(bl.get("risk_class") or "broad_equity")
        if rc_b not in RISK_CLASSES:
            rc_b = "broad_equity"
        stored_evidence = json.loads(bl.get("growth_evidence_json") or "[]")
        evidence = _add_dated_return(stored_evidence, target, rc_b)
        if not evidence:
            evidence = [_class_growth_evidence(rc_b, today_date)]
        gp_b = evidence[0]["projected_return_pct"]
        target["ingested_risk_class"] = rc_b
        target["default_risk_class"] = rc_b
        target["default_growth_pct"] = gp_b
        target["ref_growth_pct"] = gp_b
        target["realized_vol_pct"] = round(float(bl["realized_vol_pct"]), 2) if bl.get("realized_vol_pct") is not None else None
        target["beta_spy"] = round(float(bl["beta_spy"]), 3) if bl.get("beta_spy") is not None else None
        target["corr_spy"] = round(float(bl["corr_spy"]), 3) if bl.get("corr_spy") is not None else None
        target["lookback_years"] = round(float(bl["lookback_years"]), 2) if bl.get("lookback_years") is not None else None
        target["return_source"] = ("STATEMENT_INCEPTION_CAGR" if evidence[0]["tier"] == 1 and evidence[0] not in stored_evidence
                                   else str(bl.get("return_source") or ""))
        target["confidence"] = str(bl.get("confidence") or "MEDIUM")
        target["source_detail"] = evidence[0]["source_detail"]
        target["shared_reference"] = bool(bl.get("shared_reference"))
        target["growth_evidence"] = evidence


    rollup_to_sim_key: dict[str, str] = {}
    sim_by_id: dict[str, dict[str, Any]] = {}
    for a in retirement_sim_assets:
        aid = a["id"]
        sim_by_id[aid] = a
        rgid = a.get("rollup_group_id")
        if rgid and not str(rgid).startswith("rollup_plan_") and (rgid not in rollup_to_sim_key or aid == "cash_sweeps"):
            rollup_to_sim_key[rgid] = aid

    # Map standalone `(account_id, security_id)` pairs to their canonical Strategy/Sim `forecast_key`
    # so Strategy View, Account View, and Simulator Liquidation Priority share identical keys!
    acct_sec_to_canonical_fk: dict[tuple[str, str], tuple[str, list[str]]] = {}
    sleeve_parent_by_acct_sec: dict[tuple[str, str], str] = {}

    for g in holdings_rollups:
        gid = g["group_id"]
        mapped_sim_key = rollup_to_sim_key.get(gid)
        raw_sym = str(g.get("symbol") or "").lstrip("#").upper()
        is_multi_sleeve = gid.startswith(("rollup_uma_", "rollup_munis_")) or gid == "rollup_cash_all"

        if mapped_sim_key:
            fk = mapped_sim_key
            sim_a = sim_by_id[mapped_sim_key]
            parent_fks = list(sim_a.get("parent_forecast_keys") or [])
        elif primary_sym_up and raw_sym == primary_sym_up:
            fk = primary_sym_up
            parent_fks = []
        else:
            fk = gid
            parent_fks = [primary_sym_up] if (primary_sym_up and raw_sym == primary_sym_up and fk != primary_sym_up) else []

        g["forecast_key"] = fk
        g["parent_forecast_keys"] = parent_fks

        if is_multi_sleeve:
            sleeve_rc = (
                "cash_fixed"
                if gid == "rollup_cash_all"
                else ("fixed_income" if gid.startswith("rollup_munis_") else "broad_equity")
            )
            for ch in g.get("children") or []:
                ch_aid = str(ch.get("account_id") or "")
                ch_sid = str(ch.get("security_id") or "")
                ch_fk = str(ch.get("holding_key") or f"{ch_aid}:{ch_sid}")
                ch["forecast_key"] = ch_fk
                ch["parent_forecast_keys"] = [fk, *([f"acct_{ch_aid}"] if ch_aid else []), *parent_fks]
                ch_fb_rc = infer_risk_class(
                    asset_id=ch_fk,
                    asset_class=str(ch.get("asset_class") or ch.get("investment_type") or ""),
                    symbol=str(ch.get("symbol") or ""),
                    primary_symbol=primary_sym_up,
                )
                ch_bl = _lookup_baseline(ch_aid, ch_sid, fallback_rc=ch_fb_rc)
                _apply_baseline_fields(ch, ch_bl)
                if ch_aid and ch_sid:
                    sleeve_parent_by_acct_sec[(ch_aid, ch_sid)] = fk

            roll_metrics = _rollup_sleeve_forecast_metrics(g.get("children") or [], sleeve_rc, today_date, spy_vol_pct=spy_vol_pct, group=g)
            g["ingested_risk_class"] = sleeve_rc
            g["default_risk_class"] = sleeve_rc
            g["default_growth_pct"] = roll_metrics["default_growth_pct"]
            g["ref_growth_pct"] = roll_metrics["default_growth_pct"]
            g["realized_vol_pct"] = roll_metrics["realized_vol_pct"]
            g["beta_spy"] = roll_metrics["beta_spy"]
            g["corr_spy"] = roll_metrics["corr_spy"]
            g["lookback_years"] = 10.0 if roll_metrics["realized_vol_pct"] is not None and sleeve_rc != "cash_fixed" else None
            g["return_source"] = roll_metrics["return_source"]
            g["confidence"] = roll_metrics["confidence"]
            g["source_detail"] = roll_metrics["source_detail"]
            g["growth_evidence"] = roll_metrics["growth_evidence"]
            g["constituent_forecasts"] = roll_metrics["constituents"]
        elif gid.startswith("rollup_plan_"):
            plan_children = list(g.get("children") or [])
            first_ch_aid = str(plan_children[0].get("account_id") or "") if plan_children else ""
            st_sid = str(g.get("security_id") or "")
            st_fb_rc = infer_risk_class(
                asset_id=fk,
                asset_class=str(g.get("asset_class") or g.get("investment_type") or ""),
                symbol=raw_sym,
                primary_symbol=primary_sym_up,
            )
            st_bl = _lookup_baseline(first_ch_aid, st_sid, fallback_rc=st_fb_rc)
            _apply_baseline_fields(g, st_bl)
            for ch in plan_children:
                ch_aid = str(ch.get("account_id") or "")
                ch_sid = str(ch.get("security_id") or st_sid)
                ch_sym = str(ch.get("symbol") or raw_sym).lstrip("#").lower()
                ch_sim_fk = f"{ch_sym}_{ch_aid.replace('-', '_')}" if (ch_sym and ch_aid) else str(ch.get("holding_key") or f"{ch_aid}:{ch_sid}")
                ch["forecast_key"] = ch_sim_fk
                ch["parent_forecast_keys"] = [fk, *([f"acct_{ch_aid}"] if ch_aid else []), *parent_fks]
                ch_bl = _lookup_baseline(ch_aid, ch_sid, fallback_rc=st_fb_rc)
                _apply_baseline_fields(ch, ch_bl)
                if ch_aid and ch_sid:
                    acct_sec_to_canonical_fk[(ch_aid, ch_sid)] = (ch_sim_fk, [fk])
        else:
            st_aid = str(g.get("account_id") or "")
            st_sid = str(g.get("security_id") or "")
            st_fb_rc = infer_risk_class(
                asset_id=fk,
                asset_class=str(g.get("asset_class") or g.get("investment_type") or ""),
                symbol=raw_sym,
                primary_symbol=primary_sym_up,
            )
            st_bl = _lookup_baseline(st_aid, st_sid, fallback_rc=st_fb_rc)
            _apply_baseline_fields(g, st_bl)
            if st_aid and st_sid:
                acct_sec_to_canonical_fk[(st_aid, st_sid)] = (fk, list(parent_fks))
            # Sub-segments on RILA contracts inherit the parent contract's forecast
            for ch in g.get("children") or []:
                ch["is_sub_segment"] = True
                ch["forecast_key"] = fk
                ch["parent_forecast_keys"] = list(parent_fks)
                _apply_baseline_fields(ch, st_bl)
                for field in ("growth_evidence", "source_detail", "default_growth_pct", "ref_growth_pct"):
                    ch[field] = g[field]

    # Sync enriched baseline fields & constituent_forecasts onto `retirement_sim_assets`
    holding_prefs_cfg = (cfg or {}).get("friendly_names") or (cfg or {}).get("holding_preferences") or {}
    rollup_by_gid = {g["group_id"]: g for g in holdings_rollups}
    for a in retirement_sim_assets:
        rgid = a.get("rollup_group_id")
        if rgid and rgid in rollup_by_gid:
            rg = rollup_by_gid[rgid]
            for fld in (
                "ingested_risk_class",
                "default_risk_class",
                "default_growth_pct",
                "ref_growth_pct",
                "realized_vol_pct",
                "beta_spy",
                "corr_spy",
                "lookback_years",
                "return_source",
                "confidence",
                "source_detail",
                "growth_evidence",
                "shared_reference",
                "fee_pct",
            ):
                if fld in rg and rg[fld] is not None:
                    a[fld] = rg[fld]
            if not a.get("maturity_iso") and rg.get("maturity_date") and str(rgid).startswith("single_"):
                a["maturity_iso"] = str(rg["maturity_date"])
                inv_cls = str(rg.get("asset_class") or rg.get("investment_type") or "").upper()
                if any(tok in inv_cls for tok in ("CD", "CERTIFICATE", "MYGA", "TERM")):
                    a["maturity_locked"] = True
            rg_sym = str(rg.get("symbol") or "").strip().upper()
            if str(rgid).startswith("single_") and rg_sym and not rg_sym.startswith("#") and " " not in rg_sym:
                a["symbol"] = rg_sym
            if rg.get("constituent_forecasts"):
                a["constituent_forecasts"] = copy.deepcopy(rg["constituent_forecasts"]) if "copy" in globals() else [dict(c) for c in rg["constituent_forecasts"]]
            if a.get("rate_model") == "fixed" and rg.get("default_growth_pct") is not None:
                a["annual_rate_pct"] = float(rg["default_growth_pct"])
        elif a.get("is_goog_priced") or a.get("is_unvested_goog") or str(a.get("id") or "").startswith("future_unvested_"):
            # DESIGN INVARIANT (DA2-01 — Never Use a Catch-All `else:` for Employer Stock Enrichment):
            # Only explicit employer stock-plan vested & unvested grant buckets (`is_goog_priced` / `is_unvested_goog`)
            # inherit `TICKER:<grant_sym>` / `single_stock` baseline enrichment. Non-equity buckets without a rollup
            # group (such as per-retirement-account cash sweeps `cash_<acct>`) MUST keep `cash_fixed` & sweep yield!
            grant_sym = str(a.get("grant_symbol") or primary_sym_up or "").strip().upper()
            if grant_sym and not a.get("symbol"):
                a["symbol"] = grant_sym
            g_bl = _lookup_baseline("", f"TICKER:{grant_sym}" if grant_sym else "", fallback_rc="single_stock")
            _apply_baseline_fields(a, g_bl)
        else:
            fb_rc = str(a.get("default_risk_class") or "cash_fixed")
            c_bl = _lookup_baseline("", "CASH_SWEEPS_ALL" if fb_rc == "cash_fixed" else "", fallback_rc=fb_rc)
            _apply_baseline_fields(a, c_bl)
        if rgid not in rollup_by_gid and a.get("rate_model") == "fixed" and a.get("annual_rate_pct") is not None:
            fixed_rate = round(float(a["annual_rate_pct"]), 2)
            a["default_growth_pct"] = a["ref_growth_pct"] = fixed_rate
            a["growth_evidence"] = [
                {"tier": 1, "projected_return_pct": fixed_rate, "source_detail": f"Configured fixed bucket rate as of {today_date}"},
                *[entry for entry in a["growth_evidence"] if entry.get("tier")],
            ]
        # Direct holding_prefs fee_pct override keyed by sim bucket id
        hp_entry = holding_prefs_cfg.get(a["id"])
        if isinstance(hp_entry, dict) and hp_entry.get("fee_pct") is not None:
            try:
                a["fee_pct"] = round(max(0.0, float(hp_entry["fee_pct"])), 4)
            except (TypeError, ValueError):
                pass

    # DA-C05 Tier-1: Precompute exact calendar-year `adj_close` total returns from SQLite `market_prices`
    # for all completed calendar years (`< today_date.year`) so Historical Cohorts use actual ticker/sleeve
    # returns whenever trading history exists before falling back to Tier 2 / Tier 3!
    # MODE C PRIVACY SHIELD INVARIANT: Keep dict keys strictly `{"year": y, "ret_pct": r}` — `"year"` is in
    # `_RAW_NON_MONETARY_KEY_EXACT` and `"ret_pct"` ends in `"_pct"`, ensuring Mode C never scales return %.
    _max_hist_yr = today_date.year - 1
    _yr_end_px: dict[str, dict[int, float]] = {}
    try:
        for _sym_r, _yr_s, _px_r in db.conn.execute(
            """
            SELECT m.quote_symbol, SUBSTR(m.price_date, 1, 4) AS yr, COALESCE(m.adj_close, m.close_price)
            FROM market_prices m
            JOIN (
                SELECT quote_symbol, SUBSTR(price_date, 1, 4) AS yr, MAX(price_date) AS max_dt
                FROM market_prices
                WHERE price_date <= ?
                GROUP BY quote_symbol, SUBSTR(price_date, 1, 4)
            ) latest
              ON m.quote_symbol = latest.quote_symbol AND m.price_date = latest.max_dt
            """,
            (f"{_max_hist_yr:04d}-12-31",),
        ).fetchall():
            if _sym_r and _yr_s and _px_r is not None and float(_px_r) > 0:
                _yr_end_px.setdefault(str(_sym_r).upper(), {})[int(_yr_s)] = float(_px_r)
    except Exception:
        _yr_end_px = {}

    _sym_hist_rets: dict[str, dict[int, float]] = {}
    for _sym_u, _ymap in _yr_end_px.items():
        _s_out: dict[int, float] = {}
        for _y in sorted(_ymap):
            if (_y - 1) in _ymap and _ymap[_y - 1] > 0:
                _s_out[_y] = round((_ymap[_y] / _ymap[_y - 1] - 1.0) * 100.0, 3)
        if _s_out:
            _sym_hist_rets[_sym_u] = _s_out

    for a in retirement_sim_assets:
        rgid_a = a.get("rollup_group_id")
        rg_a = rollup_by_gid.get(rgid_a) if rgid_a else None
        if a["id"].startswith("uma_") and rg_a and rg_a.get("children"):
            ch_syms = [
                (str(ch.get("symbol") or "").strip().upper(), max(0.0, float(ch.get("current_value") or 0.0)))
                for ch in rg_a["children"]
                if ch.get("symbol") and float(ch.get("current_value") or 0.0) > 0
            ]
            tot_sv = sum(v for _, v in ch_syms)
            cand_yrs = sorted({y for s, _ in ch_syms for y in (_sym_hist_rets.get(s) or {})})
            uma_hist: list[dict[str, Any]] = []
            for _y in cand_yrs:
                cov_v = sum(v for s, v in ch_syms if _y in (_sym_hist_rets.get(s) or {}))
                if tot_sv > 0 and cov_v >= 0.50 * tot_sv:
                    w_r = sum(v * _sym_hist_rets[s][_y] for s, v in ch_syms if _y in (_sym_hist_rets.get(s) or {})) / cov_v
                    uma_hist.append({"year": _y, "ret_pct": round(w_r, 3)})
            if uma_hist:
                a["hist_annual_returns"] = uma_hist
        else:
            sym_a = str(a.get("symbol") or (primary_sym_up if a.get("bucket") == "goog" else "") or "").lstrip("#").strip().upper()
            if sym_a and sym_a in _sym_hist_rets:
                a["hist_annual_returns"] = [
                    {"year": _y, "ret_pct": _r}
                    for _y, _r in sorted(_sym_hist_rets[sym_a].items())
                ]

    for ag in account_rollups:
        aid = str(ag.get("account_id") or "")
        fk = f"acct_{aid}"
        ag["forecast_key"] = fk
        ag["parent_forecast_keys"] = []
        for ch in ag.get("children") or []:
            ch_sid = str(ch.get("security_id") or "")
            ch_sym = str(ch.get("symbol") or "").lstrip("#").upper()
            ch_fb_rc = infer_risk_class(
                asset_id=str(ch.get("holding_key") or fk),
                asset_class=str(ch.get("asset_class") or ch.get("investment_type") or ""),
                symbol=ch_sym,
                primary_symbol=primary_sym_up,
            )
            ch_bl = _lookup_baseline(aid, ch_sid, fallback_rc=ch_fb_rc)
            _apply_baseline_fields(ch, ch_bl)
            if (aid, ch_sid) in acct_sec_to_canonical_fk:
                canon_fk, canon_parents = acct_sec_to_canonical_fk[(aid, ch_sid)]
                ch["forecast_key"] = canon_fk
                ch["parent_forecast_keys"] = [*canon_parents, *([fk] if fk not in canon_parents else [])]
            else:
                ch["forecast_key"] = str(ch.get("holding_key") or f"{aid}:{ch_sid}")
                sleeve_fk = sleeve_parent_by_acct_sec.get((aid, ch_sid))
                p_list = [p for p in (sleeve_fk, fk) if p]
                ch["parent_forecast_keys"] = p_list

        rc_weights: dict[str, float] = {}
        for ch in ag.get("children") or []:
            ch_rc = str(ch.get("default_risk_class") or "broad_equity")
            rc_weights[ch_rc] = rc_weights.get(ch_rc, 0.0) + max(0.0, float(ch.get("current_value") or 0.0))
        dom_rc = max(rc_weights.items(), key=lambda kv: kv[1])[0] if rc_weights else "broad_equity"
        acct_roll = _rollup_sleeve_forecast_metrics(ag.get("children") or [], dom_rc, today_date, spy_vol_pct=spy_vol_pct, group=ag)
        ag["ingested_risk_class"] = dom_rc
        ag["default_risk_class"] = dom_rc
        ag["default_growth_pct"] = acct_roll["default_growth_pct"]
        ag["ref_growth_pct"] = acct_roll["default_growth_pct"]
        ag["realized_vol_pct"] = acct_roll["realized_vol_pct"]
        ag["beta_spy"] = acct_roll["beta_spy"]
        ag["corr_spy"] = acct_roll["corr_spy"]
        ag["return_source"] = acct_roll["return_source"]
        ag["confidence"] = acct_roll["confidence"]
        ag["source_detail"] = acct_roll["source_detail"]
        ag["growth_evidence"] = acct_roll["growth_evidence"]
        ag["constituent_forecasts"] = acct_roll["constituents"]

    _sr = (cfg or {}).get("sim_asset_rates", {}) or {}
    _muni_rate = next(
        (float(a["default_growth_pct"]) for a in retirement_sim_assets if a["id"].startswith("munis_") and a.get("default_growth_pct") is not None),
        next(
            (float(v["annual_rate_pct"]) for k, v in _sr.items() if k.startswith("munis_") and v.get("annual_rate_pct") is not None),
            0.0,
        ),
    )
    _fixed_ann_rate = next(
        (float(a["default_growth_pct"]) for a in retirement_sim_assets if a["id"].startswith("ann_") and a.get("rate_model") == "fixed" and a.get("default_growth_pct") is not None),
        next(
            (float(v["annual_rate_pct"]) for k, v in _sr.items() if k.startswith("ann_") and v.get("annual_rate_pct") is not None),
            0.0,
        ),
    )
    _cash_rate = next(
        (float(a["default_growth_pct"]) for a in retirement_sim_assets if a["id"] == "cash_sweeps" and a.get("default_growth_pct") is not None),
        float((_sr.get("cash_sweeps") or {}).get("annual_rate_pct", 0.0)),
    )
    _goog_def_rate = float(
        _lookup_baseline("", f"TICKER:{primary_sym_up}" if primary_sym_up else "", fallback_rc="single_stock")["projected_return_pct"]
    )

    # Map timeline stack_buckets to their constituent simulator bucket forecast_keys for per-holding forward curves
    stack_bucket_forecast_keys: dict[str, list[dict[str, Any]]] = {
        "goog_combined": [{"key": primary_sym_up or "primary_equity", "weight": 1.0, "fallback_rate": round(_goog_def_rate, 2)}],
        "cash_sweeps": [{"key": "cash_sweeps", "weight": 1.0, "fallback_rate": _cash_rate}],
        "muni_bonds": [
            {"key": a["id"], "weight": max(1.0, float(a.get("current_value") or 1.0)), "fallback_rate": float(a.get("default_growth_pct") or _muni_rate)}
            for a in retirement_sim_assets if a["id"].startswith("munis_")
        ] or [{"key": "munis", "weight": 1.0, "fallback_rate": _muni_rate}],
        "uma_equities": [
            {"key": a["id"], "weight": max(1.0, float(a.get("current_value") or 1.0)), "fallback_rate": float(a.get("default_growth_pct") or 0.0)}
            for a in retirement_sim_assets if a["id"].startswith("uma_")
        ] or [{"key": "uma_equities", "weight": 1.0, "fallback_rate": 0.0}],
        "fixed_annuities": [
            {"key": a["id"], "weight": max(1.0, float(a.get("current_value") or 1.0)), "fallback_rate": float(a.get("default_growth_pct") or _fixed_ann_rate)}
            for a in retirement_sim_assets if a["id"].startswith("ann_") and a.get("rate_model") == "fixed"
        ] or [{"key": "fixed_annuities", "weight": 1.0, "fallback_rate": _fixed_ann_rate}],
        "indexed_annuities": [
            {"key": a["id"], "weight": max(1.0, float(a.get("current_value") or 1.0)), "fallback_rate": float(a.get("default_growth_pct") or 0.0)}
            for a in retirement_sim_assets
            if a["id"].startswith("ann_")
            and a.get("rate_model") != "fixed"
            and a.get("default_risk_class") != "hybrid_buffered"
            and "RILA" not in str(a.get("asset_class") or "").upper()
            and "rila" not in str(a.get("title") or "").lower()
        ],
        "rila_annuities": [
            {"key": a["id"], "weight": max(1.0, float(a.get("current_value") or 1.0)), "fallback_rate": float(a.get("default_growth_pct") or 0.0)}
            for a in retirement_sim_assets
            if a["id"].startswith("ann_")
            and (
                a.get("default_risk_class") == "hybrid_buffered"
                or "RILA" in str(a.get("asset_class") or "").upper()
                or "rila" in str(a.get("title") or "").lower()
            )
        ],
        "mutual_funds": [
            {"key": a["id"], "weight": max(1.0, float(a.get("current_value") or 1.0)), "fallback_rate": float(a.get("default_growth_pct") or 0.0)}
            for a in retirement_sim_assets
            if a.get("bucket") not in ("primary_equity", "goog")
            and not a["id"].startswith(("future_unvested", "cash_", "munis_", "uma_", "ann_"))
        ],
    }

    forecast_bucket_rates = {
        "goog_combined": {"model": "goog", "forecast_keys": stack_bucket_forecast_keys["goog_combined"]},
        "uma_equities": {"model": "nongoog", "forecast_keys": stack_bucket_forecast_keys["uma_equities"]},
        "mutual_funds": {"model": "nongoog", "forecast_keys": stack_bucket_forecast_keys["mutual_funds"]},
        "indexed_annuities": {"model": "nongoog", "forecast_keys": stack_bucket_forecast_keys["indexed_annuities"]},
        "rila_annuities": {"model": "nongoog", "forecast_keys": stack_bucket_forecast_keys["rila_annuities"]},
        "cash_sweeps": {"model": "fixed", "rate": _cash_rate, "forecast_keys": stack_bucket_forecast_keys["cash_sweeps"]},
        "muni_bonds": {"model": "fixed", "rate": _muni_rate, "forecast_keys": stack_bucket_forecast_keys["muni_bonds"]},
        "fixed_annuities": {"model": "fixed", "rate": _fixed_ann_rate, "forecast_keys": stack_bucket_forecast_keys["fixed_annuities"]},
    }

    # CodeReview T3: surface how stale the statement-anchored (non-scrapeable) values are.
    _anchor_row = db.conn.execute("SELECT MAX(value_date) AS d FROM valuation_anchors").fetchone()
    _anchor_as_of_iso = _anchor_row["d"] if _anchor_row and _anchor_row["d"] else None
    _anchor_stale_days = (today_date - date.fromisoformat(_anchor_as_of_iso)).days if _anchor_as_of_iso else None

    investors_cfg = cfg.get("investors") or []
    beneficiaries_cfg = cfg.get("beneficiaries") or []
    resolved_family_cfg = resolve_family_members(cfg, clamp_work_plan=True)

    def _ym_dob(raw_dob: Any) -> str:
        # SECURITY DO: Minimize birthdates in exported HTML/JSON to `YYYY-MM` and strip full `annual_earnings` / local `external_file` paths (`SA-11`/`SA-19`).
        # SECURITY DON'T: Never embed full `YYYY-MM-DD` DOBs or raw `account_id`/`contract_id` strings in exported view-models (`SA-21`).
        s = str(raw_dob or "").strip()
        m = re.match(r"^(\d{4}-\d{2})-\d{2}$", s)
        return m.group(1) if m else s

    def _sanitize_person_for_export(
        p: dict[str, Any],
        *,
        is_investor: bool = False,
        idx: int = 0,
        fam_idx: int | None = None,
    ) -> dict[str, Any]:
        q = copy.deepcopy(p)
        if q.get("birth_date"):
            q["birth_date"] = _ym_dob(q["birth_date"])
        if q.get("birthdate"):
            q["birthdate"] = _ym_dob(q["birthdate"])
        ssn = q.get("ssn")
        if isinstance(ssn, dict):
            if "annual_earnings" in ssn:
                ssn["annual_earnings"] = {}
            if ssn.get("external_file"):
                ssn["external_file"] = Path(str(ssn["external_file"])).name
            if ssn.get("statement_dob"):
                ssn["statement_dob"] = _ym_dob(ssn["statement_dob"])
        ms_age = int(q.get("milestone_age") or 85)
        q["work_plan"] = sanitize_work_plan(q.get("work_plan"), milestone_age=ms_age, clamp_to_milestone=True)
        if fam_idx is not None:
            q["member_retirement_key"] = f"member_{fam_idx}_retirement_age"
        if is_investor and idx < 2:
            q["retirement_setting_key"] = f"simInvestor{idx + 1}RetirementAge"
        return q

    export_investors = [
        _sanitize_person_for_export(p, is_investor=True, idx=i)
        for i, p in enumerate(p_item for p_item in investors_cfg if isinstance(p_item, dict))
    ]
    export_beneficiaries = [
        _sanitize_person_for_export(p, is_investor=False, idx=i)
        for i, p in enumerate(beneficiaries_cfg)
        if isinstance(p, dict)
    ]
    export_family_members = [
        _sanitize_person_for_export(
            p,
            is_investor=bool(p.get("is_ssn_contributor")),
            idx=i,
            fam_idx=i,
        )
        for i, p in enumerate(resolved_family_cfg)
    ]
    family_birthdays = {
        str(p["name"]): _ym_dob(p["birth_date"])
        for p in (resolved_family_cfg if resolved_family_cfg else [*investors_cfg, *beneficiaries_cfg])
        if p.get("name") and p.get("birth_date")
    }

    badge_history = normalize_badge_history(
        cfg.get("badge_history"),
        active_portfolio_id,
        cfg.get("badge_image"),
    )
    badge_metadata = cfg.get("badge_metadata", {}) if isinstance(cfg.get("badge_metadata"), dict) else {}

    badge_img = normalize_badge_image_path(cfg.get("badge_image", "Assets/default_badge.svg"), active_portfolio_id)
    hh_name = cfg.get("household_name", "My Portfolio") if active_portfolio_id else "Portfolios"
    saved_prompts_list = normalize_saved_prompts(cfg) if active_portfolio_id else []

    from valuation.simulator import compute_trailing_inflation_info
    _ref_b_raw = (cfg or {}).get("reference_annual_burn")
    _ret_b_raw = (cfg or {}).get("retirement_annual_burn")
    ref_burn = float(_ref_b_raw if _ref_b_raw is not None else (_ret_b_raw if _ret_b_raw is not None else 0.0))
    as_of_yr = int(today_iso[:4]) if today_iso and len(today_iso) >= 4 else 2026
    ref_inflation, ref_inflation_range = compute_trailing_inflation_info(as_of_year=as_of_yr)
    _eff_tax = _resolve_effective_tax_rates(cfg)
    hh_name = (str(cfg.get("household_name") or "").strip() or "My Portfolio") if active_portfolio_id else "Portfolios"

    raw_payload = {
        "active_portfolio_id": active_portfolio_id,
        "has_portfolio": active_portfolio_id is not None,
        "portfolios_bundle": list_portfolios(),
        "household_name": hh_name,
        "badge_image": badge_img,
        "badge_history": badge_history,
        "badge_metadata": badge_metadata,
        "project": {
            "household_name": str(cfg.get("household_name") or "").strip() or hh_name,
            "badge_image": badge_img,
            "badge_history": badge_history,
            "badge_metadata": badge_metadata,
            "reference_annual_burn": int(round(ref_burn)) if ref_burn > 0 else None,
            "retirement_annual_burn": int(round(ref_burn)) if ref_burn > 0 else None,
            "primary_equity_symbol": primary_sym_up,
            "symbols": cfg.get("symbols") or ([primary_sym_up] if primary_sym_up else []),
            "investors": export_investors,
            "beneficiaries": export_beneficiaries,
            "family_members": export_family_members,
            "family_birthdays": family_birthdays,
            "tax_rates": _eff_tax["tax_rates"],
            "sim_cash_interest_tax_rate": _eff_tax["cash_interest_tax_rate"],
            "sim_ira_ordinary_tax_rate": _eff_tax["ira_ordinary_tax_rate"],
            "sim_buckets": cfg.get("sim_buckets", {}),
            "principal_overrides": cfg.get("principal_overrides", {}),
            "saved_prompts": saved_prompts_list,
        },
        "generated_at": datetime.now().strftime("%b %d, %Y %I:%M %p"),
        "today_iso": today_iso,
        "prior_bar_iso": prior_iso,
        "primary_symbol": primary_sym_up,
        "initial_preset": initial_preset,
        "default_tax_mode": tax_mode,
        "net_retention_rate": net_rate,
        "forecast_bucket_rates": forecast_bucket_rates,
        "goog_live_price": round(goog_live, 2),
        "goog_prev_close": round(goog_prev_close, 2) if goog_prev_close > 0 else round(goog_live, 2),
        "kpis": {
            "total_vested": round(total_now, 2),
            "wow_change": wow_diff,
            "wow_pct": wow_pct,
            "live_scraped_val": round(live_scraped_sum, 2),
            "live_scraped_pct": round(live_scraped_sum / total_now * 100.0, 1) if total_now > 0 else 0.0,
            "anchored_val": round(total_now - live_scraped_sum, 2),
            "anchored_pct": round((total_now - live_scraped_sum) / total_now * 100.0, 1) if total_now > 0 else 0.0,
            "anchored_as_of": _anchor_as_of_iso,  # CodeReview T3: latest statement-anchor date
            "anchored_stale_days": _anchor_stale_days,
            "goog_available_val": round(ms_goog_sum, 2),
            "goog_available_shares": round(ms_goog_shares, 3),
            "goog_available_pct": round(ms_goog_sum / total_now * 100.0, 1) if total_now > 0 else 0.0,
            "goog_vested_to_date_net_shares": tot_vested_to_date_net_sh,
            "goog_vested_to_date_gross_shares": tot_vested_to_date_gross_sh,
            "goog_vested_to_date_net_val": tot_vested_to_date_net_val,
            "goog_vested_to_date_gross_val": tot_vested_to_date_gross_val,
            "goog_vested_val": round(goog_family_sum, 2),
            "goog_vested_shares": round(goog_family_shares, 3),
            "goog_vested_pct": round(goog_family_sum / total_now * 100.0, 1) if total_now > 0 else 0.0,
            "future_gsu_gross_shares": round(sum(float(c.get("future_gross_shares") or 0.0) for c in stock_grants["companies"]), 3),
            "future_gsu_gross_val": round(sum(float(c.get("future_gross_val") or 0.0) for c in stock_grants["companies"]), 2),
            "future_gsu_net_val": round(sum(float(c.get("future_net_val") or 0.0) for c in stock_grants["companies"]), 2),
            "future_grant_companies": stock_grants["companies"],
        },
        "bucket_summary": bucket_summary,
        "schemas": {
            "bucket": sanitized_stack_buckets,
            "account": account_schemas,
            "asset": ASSET_CLASS_META,
        },
        "timeline": timeline,
        "reference_annual_burn": ref_burn,
        "retirement_annual_burn": ref_burn,
        "retirement_goal_years": 30,
        "retirement_goal_val": ref_burn * 30.0,
        "reference_inflation_pct": ref_inflation,
        "reference_inflation_range": ref_inflation_range,
        "holdings_by_strategy": holdings_rollups,
        "holdings_by_account": account_rollups,
        "gsu_schedule": {
            "report_date": google_stock.get("report_date") or state["gsu_report_date"],
            "by_year": google_stock["by_year"],
            "by_grant": google_stock["by_grant"],
        },
        "google_stock": google_stock,
        "stock_grants": stock_grants,
        "retirement_sim_assets": retirement_sim_assets,
        "risk_classes": RISK_CLASSES,
        "us_state_tax_presets": US_STATE_TAX_PRESETS,
        "saved_prompts": saved_prompts_list,
        "default_saved_prompt_specs": DEFAULT_SAVED_PROMPT_SPECS,
        "advisor_prompts": resolve_advisor_prompts(cfg),
        "ssn_model": _build_ssn_model(cfg, portfolio_id=active_portfolio_id),
        "montecarlo": _build_montecarlo_data(primary_symbol=primary_sym_up, db=db),
        "cohorts": _build_cohort_data(),
        "recent_events": recent_events,
        "saves_bundle": (
            {**list_saves_payload(active_saves_dir), "saves_dir": ""}
            if active_saves_dir is not None
            else {
                "ok": True,
                "saves_dir": "",
                "active_id": None,
                "active_save": None,
                "default_id": None,
                "saves": [],
                "scenarios": [],
            }
        ),
        "global_preferences": global_prefs,
        "privacy_status": _build_privacy_status_meta(active_portfolio_id),
    }
    # DESIGN INTENT (DP-05 / SA2-13 / SA2-20):
    # - When not in Demo Mode, Live View ALWAYS displays real account names, account numbers, and
    #   annuity contract numbers (`ACCT-01` / `ANN-01` Live View masking does not exist).
    # - Account and contract numbers are aliased dynamically by `DemoTranslator` when Demo Mode is
    #   active, and translated to `ACCT_N` / `CONTRACT_N` by `PrivacyTranslator` when AI Chat / MCP
    #   Privacy Shield is configured to obfuscate names (Mode B / Mode C).
    def _enforce_export_dob_trimming(node: Any) -> Any:
        """
        SECURITY INVARIANT (SA-08 / SA-19 / SA3-05 — Central Unconditional Export DOB Gate):
        NEVER allow exact calendar birth days (`*_birth_day != 1` or `YYYY-MM-DD`
        `birth_date`/`birthdate`/`birth_iso`) anywhere in exported `ReportPayload`.
        """
        if isinstance(node, list):
            return [_enforce_export_dob_trimming(x) for x in node]
        if isinstance(node, dict):
            out_d: dict[str, Any] = {}
            is_fb_entry = {"name", "y", "m", "d"}.issubset(node.keys())
            for k, v in node.items():
                if isinstance(k, str) and (k == "birth_day" or k.endswith("_birth_day") or (is_fb_entry and k == "d")):
                    out_d[k] = 1 if (isinstance(v, (int, float)) and not isinstance(v, bool)) else v
                elif isinstance(k, str) and k in ("birth_date", "birthdate", "birth_iso") and isinstance(v, str):
                    out_d[k] = _ym_dob(v)
                else:
                    out_d[k] = _enforce_export_dob_trimming(v)
            return out_d
        return node

    return _enforce_export_dob_trimming(raw_payload)


def _build_privacy_status_meta(portfolio_id: str | None) -> dict[str, Any]:
    """Non-sensitive privacy status flags for browser badge & AI Shield UI (omits secret scale factor k per D4)."""
    from privacy.registry import PrivacyConfigError

    try:
        from privacy import load_privacy_config

        pcfg = load_privacy_config(portfolio_id, auto_sync=False)
        return {
            "demo_enabled": bool((pcfg.get("demo_mode") or {}).get("enabled", False)),
            "shield_mode": str((pcfg.get("ai_shield") or {}).get("mode") or "identity_aliased"),
            "block_inapp_agy_cli": bool((pcfg.get("ai_shield") or {}).get("block_inapp_agy_cli", True)),
        }
    except PrivacyConfigError:
        raise
    except Exception:
        return {"demo_enabled": False, "shield_mode": "identity_aliased", "block_inapp_agy_cli": True}


def _build_montecarlo_data(primary_symbol: str = "", db: PortfolioDB | None = None) -> dict[str, Any]:
    """
    CodeReview A3 / DA-B01: aligned weekly [primary_symbol, benchmark] return pairs for the Historical
    Monte Carlo bootstrap, plus realized annualized volatilities and primary<->benchmark correlation.
    """
    from valuation.simulator import _mc_load_weekly_pairs, _mc_realized_stats

    pairs = _mc_load_weekly_pairs(primary_symbol=primary_symbol, db=db)
    out: dict[str, Any] = {"hist_pairs": [[round(g, 6), round(b, 6)] for g, b in pairs]}
    out.update(_mc_realized_stats(primary_symbol=primary_symbol, db=db))
    return out


def _build_cohort_data() -> dict[str, Any]:
    """
    CodeReview A3 (Historical Cohort tab): the real long-history annual series (S&P 500 price return,
    GOOG-proxy return, and CPI inflation, 1929-present) produced by `build_cohort_data.py`. Returns an
    empty structure when the data file is absent (the Historical tab then shows an unavailable note).
    """
    from config import DATA_DIR

    path = DATA_DIR / "historical_cohorts.json"  # DATA_DIR is already a pathlib.Path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"years": [], "first_year": None, "last_year": None}


def _build_ssn_model(cfg: dict[str, Any] | None = None, portfolio_id: str | None = None) -> dict[str, Any]:
    """
    Computes Social Security (OASDI) retirement benefits for all configured investors in
    `Portfolios/<active>/project.json` using official SSA National Average Wage Index (AWI),
    OASDI Taxable Maximums (1965–2026), 35-year Average Indexed Monthly Earnings (AIME),
    and eligibility-year bend points, with subsequent COLAs through 2026.
    Requires an uploaded official `ssa.gov` XML statement (`annual_earnings` / `external_file`);
    investors without an uploaded XML statement receive `$0` PIA (`has_statement: False`).
    """
    if cfg is None:
        from config import load_config

        cfg = load_config()

    # Official SSA OASDI Taxable Maximum & National Average Wage Index (1965–2026) — DA-B06
    ssa_table = [
        (1965, 4800, 4658.72),
        (1966, 6600, 4938.36),
        (1967, 6600, 5213.44),
        (1968, 7800, 5571.76),
        (1969, 7800, 5893.76),
        (1970, 7800, 6186.24),
        (1971, 7800, 6497.08),
        (1972, 9000, 7133.80),
        (1973, 10800, 7580.16),
        (1974, 13200, 8030.76),
        (1975, 14100, 8630.92),
        (1976, 15300, 9226.48),
        (1977, 16500, 9779.44),
        (1978, 17700, 10556.03),
        (1979, 22900, 11479.46),
        (1980, 25900, 12513.46),
        (1981, 29700, 13773.10),
        (1982, 32400, 14531.34),
        (1983, 35700, 15239.24),
        (1984, 37800, 16135.07),
        (1985, 39600, 16822.51),
        (1986, 42000, 17321.82),
        (1987, 43800, 18426.51),
        (1988, 45000, 19334.04),
        (1989, 48000, 20099.55),
        (1990, 51300, 21027.98),
        (1991, 53400, 21811.60),
        (1992, 55500, 22935.42),
        (1993, 57600, 23132.67),
        (1994, 60600, 23753.53),
        (1995, 61200, 24705.66),
        (1996, 62700, 25913.90),
        (1997, 65400, 27426.00),
        (1998, 68400, 28861.44),
        (1999, 72600, 30469.84),
        (2000, 76200, 32154.82),
        (2001, 80400, 32921.92),
        (2002, 84900, 33252.09),
        (2003, 87000, 34064.95),
        (2004, 87900, 35648.55),
        (2005, 90000, 36952.94),
        (2006, 94200, 38651.41),
        (2007, 97500, 40405.48),
        (2008, 102000, 41334.97),
        (2009, 106800, 40711.61),
        (2010, 106800, 41673.83),
        (2011, 106800, 42979.61),
        (2012, 110100, 44321.67),
        (2013, 113700, 44888.16),
        (2014, 117000, 46481.52),
        (2015, 118500, 48098.63),
        (2016, 118500, 48642.15),
        (2017, 127200, 50321.89),
        (2018, 128400, 52145.80),
        (2019, 132900, 54099.99),
        (2020, 137700, 55628.60),
        (2021, 142800, 60575.07),
        (2022, 147000, 63795.13),
        (2023, 160200, 66621.80),
        (2024, 168600, 69846.57),
        (2025, 176100, 72950.00),
        (2026, 184500, 76430.00),
    ]
    awi_by_year = {yr: awi for yr, _cap, awi in ssa_table}
    base_awi_2024 = 69846.57  # Latest finalized pre-2026 eligibility indexing AWI

    # SSA annual COLAs effective in 1979 through 2025, inclusive.
    # https://www.ssa.gov/oact/cola/colaseries.html
    cola_tenths = (
        99, 143, 112, 74, 35, 35, 31, 13, 42, 40, 47, 54, 37, 30, 26, 28,
        26, 29, 21, 13, 25, 35, 26, 14, 21, 27, 41, 33, 23, 58, 0, 0, 36,
        17, 15, 17, 0, 3, 20, 28, 16, 13, 59, 87, 32, 25, 28,
    )

    def _compute_pia(records: list[dict[str, Any]], eligibility_year: int, base_awi: float) -> dict[str, Any]:
        indexed_vals = sorted([float(r["indexed_earnings"]) for r in records if r["indexed_earnings"] > 0], reverse=True)
        top35 = (indexed_vals + [0.0] * 35)[:35]
        work_yrs = len(indexed_vals)
        zero_yrs = max(0, 35 - work_yrs)
        sum_top35 = round(sum(top35), 2)
        aime = math.floor(sum_top35 / 420.0)

        # 1979 bend points indexed to the worker's age-60 average wage.
        # https://www.ssa.gov/oact/cola/piaformula.html
        bend1 = math.floor(180 * base_awi / 9779.44 + 0.5)
        bend2 = math.floor(1085 * base_awi / 9779.44 + 0.5)
        t1 = round(0.90 * min(aime, bend1), 2)
        t2 = round(0.32 * max(0.0, min(aime, bend2) - bend1), 2)
        t3 = round(0.15 * max(0.0, aime - bend2), 2)
        pia_raw = t1 + t2 + t3
        pia_dimes = round(pia_raw * 100) // 10
        for year in range(max(1979, eligibility_year), 2026):
            pia_dimes = pia_dimes * (1000 + cola_tenths[year - 1979]) // 1000
        pia_mo = pia_dimes / 10

        return {
            "work_years": work_yrs,
            "zero_years": zero_yrs,
            "sum_indexed_35": sum_top35,
            "aime": aime,
            "bend1": bend1,
            "bend2": bend2,
            "benefit_basis": (
                "Approximation using the 1979 formula" if eligibility_year < 1979 else
                "Projection using 2026 wage and formula basis" if eligibility_year > 2026 else
                f"{eligibility_year} eligibility formula, adjusted to 2026 payments"
            ),
            "tier1_90pct": t1,
            "tier2_32pct": t2,
            "tier3_15pct": t3,
            "pia_monthly_2026": round(pia_mo, 2),
            "pia_annual_2026": round(pia_mo * 12.0, 2),
        }

    investors_cfg = list(cfg.get("investors") or [])
    is_zero_ssn_fallback = False
    if not investors_cfg:
        fam_list = resolve_family_members(cfg, clamp_work_plan=True)
        if fam_list:
            primary_mem = next((m for m in fam_list if m.get("is_rmd_owner")), fam_list[0])
            investors_cfg = [{**primary_mem, "ssn": {"fra_age": 67, "default_claim_age": 67, "external_file": "", "annual_earnings": {}}}]
            is_zero_ssn_fallback = True
    investors_profiles: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    for idx, inv in enumerate(investors_cfg):
        inv_id = str(inv.get("id") or inv.get("name", f"investor_{idx + 1}")).strip().lower().replace(" ", "_")
        name = str(inv.get("name") or f"Investor {idx + 1}")
        birth_iso = str(inv.get("birth_date") or "1975-01-01")
        try:
            b_yr, b_mo, b_dy = [int(x) for x in birth_iso.split("-")]
        except Exception:
            b_yr, b_mo, b_dy = 1975, 1, 1

        ssn_cfg = inv.get("ssn") or {}
        fra_b_yr = (b_yr - 1) if (b_mo == 1 and b_dy == 1) else b_yr
        if ssn_cfg.get("fra_age") is not None:
            fra_age = float(ssn_cfg["fra_age"])
        elif fra_b_yr <= 1937:
            fra_age = 65.0
        elif fra_b_yr <= 1942:
            fra_age = round(65.0 + (fra_b_yr - 1937) * (2.0 / 12.0), 4)
        elif fra_b_yr <= 1954:
            fra_age = 66.0
        elif fra_b_yr <= 1959:
            fra_age = round(66.0 + (fra_b_yr - 1954) * (2.0 / 12.0), 4)
        else:
            fra_age = 67.0
        default_claim_age = int(ssn_cfg.get("default_claim_age", 67))
        external_file = str(ssn_cfg.get("external_file") or "").strip()

        raw_earnings = ssn_cfg.get("annual_earnings") if isinstance(ssn_cfg.get("annual_earnings"), dict) else {}
        parsed_xml_span = ""
        if not raw_earnings and external_file:
            from ingest.ssa_xml import parse_ssa_xml_path

            parsed_xml = parse_ssa_xml_path(external_file, portfolio_id=portfolio_id)
            if parsed_xml.get("ok") and isinstance(parsed_xml.get("annual_earnings"), dict):
                raw_earnings = parsed_xml["annual_earnings"]
                parsed_xml_span = str(parsed_xml.get("work_span_label") or "")

        # DESIGN INTENT (XML-Only Social Security):
        # - Populate `earnings_by_yr` strictly from an uploaded official `ssa.gov` XML statement
        #   (`annual_earnings` cached on upload, or `parse_ssa_xml_path(external_file)` on disk).
        # - NEVER synthesize flat nominal earnings from `years_worked` + `average_salary` or substitute
        #   a default monthly PIA when no XML statement is loaded (`has_statement == False` -> `$0` PIA).
        earnings_by_yr: dict[int, float] = {}
        if raw_earnings:
            for k, v in raw_earnings.items():
                try:
                    val_f = float(v)
                    if val_f > 0:
                        earnings_by_yr[int(k)] = val_f
                except (ValueError, TypeError):
                    continue

        # Statutory SSA age-60 indexing year (`birth_year + 60`), capped at latest published AWI year (`DA-B06`)
        eligibility_year = b_yr + 62 - int(b_mo == 1 and b_dy == 1)
        cohort_idx_yr = min(2024, max(1977, eligibility_year - 2))
        cohort_base_awi = awi_by_year.get(cohort_idx_yr, base_awi_2024)
        records: list[dict[str, Any]] = []
        for yr, cap, awi in ssa_table:
            idx_mult = cohort_base_awi / awi if yr < cohort_idx_yr else 1.0
            gross = float(earnings_by_yr.get(yr, 0.0))
            if gross > 0:
                taxed = min(gross, float(cap))
                indexed = round(taxed * idx_mult, 2)
            else:
                taxed = 0.0
                indexed = 0.0
            records.append(
                {
                    "year": yr,
                    "oasdi_cap": cap,
                    "index_factor": round(idx_mult, 4),
                    "est_gross": gross,
                    "capped_earnings": taxed,
                    "indexed_earnings": indexed,
                }
            )

        calc = _compute_pia(records, eligibility_year, cohort_base_awi)
        nonzero_yrs = [r["year"] for r in records if r["est_gross"] > 0]
        has_statement = bool(nonzero_yrs)
        if has_statement:
            if ssn_cfg.get("work_span_label"):
                work_span_label = str(ssn_cfg["work_span_label"])
            elif parsed_xml_span:
                work_span_label = parsed_xml_span
            else:
                work_span_label = f"{min(nonzero_yrs)} – {max(nonzero_yrs)} ({calc['work_years']} yrs worked, {calc['zero_years']} zero yrs)"
            if ssn_cfg.get("earnings_note"):
                earnings_note = str(ssn_cfg["earnings_note"])
            elif external_file:
                earnings_note = f"Official ssa.gov XML statement ({Path(external_file).name})"
            else:
                earnings_note = f"Official ssa.gov statement earnings ({calc['work_years']} yrs worked)"
        else:
            work_span_label = "No ssa.gov XML statement uploaded"
            earnings_note = "Upload an official ssa.gov XML statement in Portfolio Settings → Family Members"

        claim_setting_key = f"ssn{name.strip().title().replace(' ', '')}ClaimAge"
        ms_age_val = int(inv.get("milestone_age", 85))
        prof = {
            "id": inv_id,
            "name": name,
            "birth_iso": f"{b_yr:04d}-{b_mo:02d}",
            "birth_year": b_yr,
            "birth_month": b_mo,
            "birth_day": 1,
            "milestone_age": ms_age_val,
            "rmd_owner": bool(inv.get("is_rmd_owner", inv.get("rmd_owner", idx == 0))),
            "is_ssn_contributor": not is_zero_ssn_fallback,
            "claim_setting_key": claim_setting_key,
            "retirement_setting_key": f"simInvestor{idx + 1}RetirementAge" if idx < 2 else "",
            "work_plan": sanitize_work_plan(inv.get("work_plan"), milestone_age=ms_age_val, clamp_to_milestone=True),
            "fra_age": fra_age,
            "default_claim_age": default_claim_age,
            "has_statement": has_statement,
            "external_file": Path(external_file).name if external_file else "",
            "work_span_label": work_span_label,
            "earnings_note": earnings_note,
            **calc,
            "history": [],
        }
        investors_profiles.append(prof)
        by_id[inv_id] = prof

    today_iso = cfg.get("today_iso") if isinstance(cfg, dict) else None
    current_year = int(str(today_iso)[:4]) if today_iso else None
    from valuation.simulator import compute_ssn_cola_rate
    cola_meta = compute_ssn_cola_rate(current_year=current_year, payload=cfg)

    # PORTABILITY DO: Derive `effective_ssn_tax_rate` and `tax_rule_label` from `cfg["tax_rates"]` (`LL-10`, `LL-11`).
    # PORTABILITY DON'T: Hardcode state codes or household-specific effective tax rates when `tax_rates` omits them.
    tr = (cfg.get("tax_rates") or {}) if isinstance(cfg, dict) else {}
    fed_taxable_portion = float(tr.get("ssn_fed_taxable_portion", 0.85))
    fed_top_rate = float(tr.get("fed_ordinary_top", 0.37))
    state_code = str(tr.get("state_code") or "").strip().upper()
    state_ssn_rate = float(tr.get("state_ssn_rate", 0.0))
    eff_ssn_rate = (
        float(tr["ssn_effective"])
        if tr.get("ssn_effective") is not None
        else round(fed_taxable_portion * fed_top_rate + state_ssn_rate, 6)
    )
    state_ssn_note = f"{state_ssn_rate * 100:g}% {state_code}" if state_code else f"{state_ssn_rate * 100:g}% State"
    tax_rule_label = (
        f"{fed_taxable_portion * 100:g}% × {fed_top_rate * 100:g}% Fed = "
        f"{eff_ssn_rate * 100:.2f}% ({state_ssn_note})"
    )

    out: dict[str, Any] = {
        "bend_points_2026": {"bend1": 1286, "bend2": 7749},
        "default_cola_pct": cola_meta["cola_pct"],
        "cola_10y_avg_pct": cola_meta["ten_yr_avg_pct"],
        "cola_span_label": cola_meta.get("span_label", "2016–2026"),
        "cola_source": cola_meta["source"],
        "fed_taxable_portion": fed_taxable_portion,
        "fed_top_rate": fed_top_rate,
        "state_rate": _resolve_effective_tax_rates(cfg)["state_rate"],
        "state_code": state_code,
        "state_ssn_rate": state_ssn_rate,
        "effective_ssn_tax_rate": eff_ssn_rate,
        "tax_rule_label": tax_rule_label,
        "investors": investors_profiles,
        **by_id,
    }
    return out


def _derive_annuity_carrier_slug(statement_name: str, symbol: str) -> str:
    tokens = [
        re.sub(r"[^a-z0-9]+", "", w)
        for w in (statement_name or symbol or "contract").lower().split()
    ]
    tokens = [t for t in tokens if t]
    return tokens[0] if tokens else "contract"


def _short_inst_tag(inst_name: str) -> str:
    up = (inst_name or "").upper()
    if "MORGAN STANLEY" in up:
        return "MS"
    if "WELLS FARGO" in up:
        return "WFA"
    cleaned = re.sub(r"\b(Advisors|Services|Investments|Brokerage)\b", "", inst_name or "", flags=re.IGNORECASE).strip()
    words = [w for w in cleaned.split() if w]
    if not words:
        return "Custodian"
    return words[0]


def _build_retirement_sim_assets(
    state: dict[str, Any],
    holdings_rollups: list[dict[str, Any]],
    google_stock: dict[str, Any],
    goog_live: float,
    today_iso: str,
    cfg: dict[str, Any] | None = None,
    stock_grants: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Two-layer Retirement Simulator bucketizer:
      Layer 1 (Automatic FI-Parenting & Account/Asset Categorization):
        Automatically constructs tax-aware drawdown buckets from `state` and `holdings_rollups`
        based on Financial Institution parenting, account wrapper (`TAXABLE`, `TRAD_IRA`, `ROTH_IRA`,
        Stock Plan, Direct-Indexing UMA, Municipal Bond SMA), and asset class.
      Layer 2 (Power-User JSON Customization in `Portfolios/<active>/project.json -> sim_buckets`):
        Applies fine-tuned overrides (`default_rank`, `color`, `title`, `short_title`, `subtitle`,
        `cost_basis`, `basis_sub`, `maturity_iso`, `liquidity_label`, `liquidity_sub`,
        `tax_rule_title`, `tax_rule_detail`, `rate_model`, `annual_rate_pct`) so power users can
        customize any bucket directly in JSON.
    """
    cfg = cfg or {}
    gsu_cfg = cfg.get("grant_forecast") or cfg.get("gsu_forecast") or {}
    accounts_db = state.get("accounts", {})
    acct_cfg_map = cfg.get("accounts") or {}
    sim_buckets_cfg = cfg.get("sim_buckets") or {}

    # PORTABILITY DO: Keep `primary_sym` empty (`""`) when `primary_equity_symbol` is unset, and resolve
    # stock-plan accounts, transfer dates, basis, and tax labels from `project.json` or statement data (`LL-01`, `LL-03`, `LL-09`, `LL-10`, `LL-12`, `LL-15`).
    # PORTABILITY DON'T: Fall back to a hardcoded ticker, custodian name, transfer date, or dollar basis literal.
    primary_sym = str(cfg.get("primary_equity_symbol") or "").strip().upper()
    resolved_sp = resolve_target_account_id(
        gsu_cfg.get("target_vested_account_id") or "",
        accounts_db.keys(),
        cfg=cfg,
    )
    stock_plan_acct_id = resolved_sp or next(
        (
            aid
            for aid, a in accounts_db.items()
            if (acct_cfg_map.get(aid) or {}).get("is_stock_plan")
            or "STOCK_PLAN" in (a.get("account_type") or "").upper()
            or "STOCK PLAN" in (a.get("account_name") or "").upper()
            or "STOCK PLAN" in (a.get("account_type") or "").upper()
        ),
        gsu_cfg.get("target_vested_account_id") or "",
    )
    xfer_vest_date = str(gsu_cfg.get("in_kind_transfer_vest_date") or "").strip()
    split_threshold = (
        float(gsu_cfg["high_basis_split_threshold"])
        if gsu_cfg.get("high_basis_split_threshold") is not None
        else (round(goog_live * 0.85, 2) if goog_live > 0 else 0.0)
    )

    _eff_tax = _resolve_effective_tax_rates(cfg)
    LTCG_TOP_RATE = _eff_tax["ltcg_top"]
    ORD_TOP_RATE = _eff_tax["ordinary_top"]
    _state_code = _eff_tax["state_code"]
    _fed_ltcg = _eff_tax["fed_ltcg_top"]
    _niit = _eff_tax["niit_rate"]
    _fed_ord = _eff_tax["fed_ordinary_top"]
    _state_rate = _eff_tax["state_rate"]
    _state_lbl = _state_code if _state_code else "State"
    _ltcg_decomp = f"{_fed_ltcg * 100:g}% Fed + {_niit * 100:g}% NIIT" + (
        f" + {_state_rate * 100:g}% {_state_lbl}" if _state_rate > 0 else ""
    )
    _ord_tag = (
        f"Fed+NIIT+{_state_code}"
        if (_state_code and _state_rate > 0)
        else ("Fed+NIIT+State" if _state_rate > 0 else "Fed+NIIT")
    )
    _muni_juris = f"0% Fed/{_state_code}" if _state_code else "0% Fed/State"
    _ira_decomp = f"{_fed_ord * 100:g}% Fed" + (
        f" + {_state_rate * 100:g}% {_state_lbl}" if _state_rate > 0 else ""
    )

    rollup_map = {g["group_id"]: g for g in holdings_rollups}
    ms_rollup_gid = f"single_{stock_plan_acct_id}_{primary_sym}" if (stock_plan_acct_id and primary_sym) else ""
    ms_rollup = rollup_map.get(ms_rollup_gid, {})
    ms_total_val = float(ms_rollup.get("current_value") or 0.0)

    def _sim_title_short(gid: str, default_title: str, default_short: str) -> tuple[str, str]:
        r = rollup_map.get(gid, {})
        fn = r.get("friendly_name")
        if fn:
            return fn, fn
        return default_title, default_short

    assets: list[dict[str, Any]] = []

    # --- 1. Stock Plan Vested Lots (High-Basis vs Low-Basis) & Unvested Schedule ---
    hist_releases = sorted(
        [
            r for r in state.get("gsu_releases_history", [])
            if r["vest_date"] <= today_iso
            and (not primary_sym or str(r.get("symbol") or primary_sym).strip().upper() == primary_sym)
        ],
        key=lambda r: (r["vest_date"], r.get("grant_id") or ""),
    )
    # Exclude Cash-sweep / USD disbursement rows (plan_name='Cash', unit is dollars not shares)
    withdrawals = [
        w
        for w in state.get("gsu_withdrawals", [])
        if str(w.get("plan_name") or "").strip().lower() != "cash"
        and (not primary_sym or str(w.get("symbol") or primary_sym).strip().upper() == primary_sym)
    ]
    wfa_transferred_sh = sum(
        abs(float(w["quantity"]))
        for w in withdrawals
        if w.get("tx_type") == "Transfer"
    )
    ms_sold_sh = sum(
        abs(float(w["quantity"]))
        for w in withdrawals
        if w.get("tx_type") == "Sale"
    )

    if stock_plan_acct_id and primary_sym and (hist_releases or ms_total_val > 0):
        rem_ms_tranches: list[dict[str, Any]] = []
        transfer_rem = wfa_transferred_sh
        sale_rem = ms_sold_sh
        for r in hist_releases:
            sh = float(r["net_shares"])
            px = float(r["release_price"])
            vdate = str(r["vest_date"])
            _raw_gid = str(r.get("grant_id") or "").strip()
            gid = "PRIOR-GRANT" if (not _raw_gid or _raw_gid.upper().startswith("PRIOR-")) else _raw_gid
            if transfer_rem > 0 and xfer_vest_date and vdate == xfer_vest_date:
                used_t = min(sh, transfer_rem)
                sh -= used_t
                transfer_rem -= used_t
            if sh <= 1e-6:
                continue
            if sale_rem > 0:
                used_s = min(sh, sale_rem)
                sh -= used_s
                sale_rem -= used_s
            if sh > 1e-6:
                rem_ms_tranches.append(
                    {
                        "vest_date": vdate,
                        "grant_id": gid,
                        "shares": round(sh, 6),
                        "basis_per_share": round(px, 4),
                        "cost_basis": round(sh * px, 2),
                        "current_value": round(sh * goog_live, 2),
                    }
                )

        high_tranches = sorted(
            [t for t in rem_ms_tranches if t["basis_per_share"] >= split_threshold],
            key=lambda t: (-t["basis_per_share"], t["vest_date"]),
        )
        low_tranches = sorted(
            [t for t in rem_ms_tranches if t["basis_per_share"] < split_threshold],
            key=lambda t: (-t["basis_per_share"], t["vest_date"]),
        )

        high_sh = round(sum(t["shares"] for t in high_tranches), 3)
        high_basis = round(sum(t["cost_basis"] for t in high_tranches), 2)
        high_val = round(high_sh * goog_live, 2)
        high_avg_px = round(high_basis / high_sh, 2) if high_sh > 0 else 0.0
        high_min_px = min((t["basis_per_share"] for t in high_tranches), default=0.0)
        high_max_px = max((t["basis_per_share"] for t in high_tranches), default=0.0)

        # DA-E03 Conservation of Vested Shares: When total statement stock-plan value (`ms_total_val`)
        # exceeds the sum of reconstructed release tranches (e.g. shares vested prior to ingested history),
        # synthesize a `PRIOR-LOTS` catch-all tranche so `_clone_sim_asset_state` (which sums `tranches`)
        # never silently drops pre-history shares from Month-0 net worth.
        low_tr_val_sum = round(sum(t["current_value"] for t in low_tranches), 2)
        target_low_val = round(ms_total_val - high_val, 2) if ms_total_val > high_val else low_tr_val_sum
        if target_low_val - low_tr_val_sum > 0.01 and goog_live > 0:
            res_val = round(target_low_val - low_tr_val_sum, 2)
            res_sh = round(res_val / goog_live, 6)
            ms_prin_tot = float(ms_rollup.get("principal_val") or 0.0)
            low_tr_basis_sum = round(sum(t["cost_basis"] for t in low_tranches), 2)
            if ms_prin_tot > high_basis + low_tr_basis_sum:
                res_basis = round(ms_prin_tot - high_basis - low_tr_basis_sum, 2)
            else:
                fallback_px = min((t["basis_per_share"] for t in low_tranches), default=goog_live * 0.5)
                res_basis = round(res_sh * fallback_px, 2)
            res_px = round(res_basis / res_sh, 4) if res_sh > 0 else 0.0
            low_tranches.append(
                {
                    "vest_date": "",
                    "grant_id": "PRIOR-LOTS",
                    "shares": res_sh,
                    "basis_per_share": res_px,
                    "cost_basis": res_basis,
                    "current_value": res_val,
                }
            )
            low_tranches.sort(key=lambda t: (-t["basis_per_share"], t["vest_date"]))

        low_sh = round(sum(t["shares"] for t in low_tranches), 3)
        low_basis = round(sum(t["cost_basis"] for t in low_tranches), 2)
        low_val = round(ms_total_val - high_val, 2) if ms_total_val > high_val else round(low_sh * goog_live, 2)
        low_avg_px = round(low_basis / low_sh, 2) if low_sh > 0 else 0.0
        low_min_px = min((t["basis_per_share"] for t in low_tranches), default=0.0)
        low_max_px = max((t["basis_per_share"] for t in low_tranches), default=0.0)

        ms_inst = (accounts_db.get(stock_plan_acct_id) or {}).get("institution") or "Stock Plan"
        ms_tag = _short_inst_tag(ms_inst)
        ms_acct_lbl = _friendly_account_label(stock_plan_acct_id, accounts_db, cfg) if stock_plan_acct_id else ms_inst
        use_legacy_ms_id = primary_sym == "GOOG" or "ms_goog_high_basis" in sim_buckets_cfg
        inst_slug = re.sub(r"[^a-z0-9]+", "_", ms_tag.lower()).strip("_") or "sp"
        high_bid = "ms_goog_high_basis" if use_legacy_ms_id else f"{inst_slug}_{primary_sym.lower()}_high_basis"
        low_bid = "ms_goog_low_basis" if use_legacy_ms_id else f"{inst_slug}_{primary_sym.lower()}_low_basis"
        award_lbl_sp = str(google_stock.get("award_type") or "RSU")

        assets.append(
            {
                "id": high_bid,
                "account_id": stock_plan_acct_id,
                "symbol": primary_sym,
                "default_rank": 1,
                "color": "#38bdf8",
                "title": f"{primary_sym} — {ms_inst} High-Basis Vests",
                "short_title": f"{ms_tag} {primary_sym} (High-Basis)",
                "subtitle": f"{ms_acct_lbl} • {high_sh:,.3f} sh across {len(high_tranches)} vest lots (${high_min_px:,.2f}–${high_max_px:,.2f}/sh)",
                "account_label": ms_acct_lbl,
                "bucket": "goog",
                "is_unvested_goog": False,
                "is_goog_priced": True,
                "shares": high_sh,
                "current_value": high_val,
                "cost_basis": high_basis,
                "basis_sub": f"${high_avg_px:,.2f}/sh avg vest FMV (${high_min_px:,.0f}–${high_max_px:,.0f})",
                "maturity_iso": None,
                "liquidity_label": "Liquid Now",
                "liquidity_sub": f"Vested {award_lbl_sp} shares",
                "tax_model": "LTCG_TRANCHES",
                "tax_rate": LTCG_TOP_RATE,
                "tax_rule_title": f"Progressive LTCG (0%–20% Fed, up to {LTCG_TOP_RATE * 100:.1f}%) • HIFO Lots",
                "tax_rule_detail": (
                    f"Ordinary income tax already paid at vest. Sells highest-basis lots first to harvest losses or minimize "
                    f"gain; gains held >=1 yr are taxed progressively across 0%/15%/20% Federal LTCG + state/NIIT tiers "
                    f"(ceiling {LTCG_TOP_RATE * 100:.1f}%), while <1 yr lots incur ordinary short-term rates."
                ),
                "tranches": high_tranches,
            }
        )

        assets.append(
            {
                "id": low_bid,
                "account_id": stock_plan_acct_id,
                "symbol": primary_sym,
                "default_rank": 40,
                "color": "#0284c7",
                "title": f"{primary_sym} — {ms_inst} Core Vests",
                "short_title": f"{ms_tag} {primary_sym} (Core)",
                "subtitle": f"{ms_acct_lbl} • {low_sh:,.3f} sh across {len(low_tranches)} vest lots (${low_min_px:,.2f}–${low_max_px:,.2f}/sh)",
                "account_label": ms_acct_lbl,
                "bucket": "goog",
                "is_unvested_goog": False,
                "is_goog_priced": True,
                "shares": low_sh,
                "current_value": low_val,
                "cost_basis": low_basis,
                "basis_sub": f"${low_avg_px:,.2f}/sh avg vest FMV (${low_min_px:,.0f}–${low_max_px:,.0f})",
                "maturity_iso": None,
                "liquidity_label": "Liquid Now",
                "liquidity_sub": f"Vested {award_lbl_sp} shares",
                "tax_model": "LTCG_TRANCHES",
                "tax_rate": LTCG_TOP_RATE,
                "tax_rule_title": f"Progressive LTCG (0%–20% Fed, up to {LTCG_TOP_RATE * 100:.1f}%) • HIFO Lots",
                "tax_rule_detail": (
                    f"Sells highest-basis lots first; gains above each lot's vest FMV are taxed progressively across "
                    f"0%/15%/20% Federal LTCG + state/NIIT brackets (up to {LTCG_TOP_RATE * 100:.1f}% top marginal ceiling)."
                ),
                "tranches": low_tranches,
            }
        )

    # --- 2. Future Unvested Stock Plan Releases (per unvested grant company) ---
    unvested_entries: list[dict[str, Any]] = []
    if stock_grants and stock_grants.get("companies"):
        for c in stock_grants["companies"]:
            sym = str(c["symbol"]).upper()
            analytics = stock_grants["by_symbol"].get(sym)
            if analytics:
                unvested_entries.append(analytics)
    elif google_stock and primary_sym and [b for b in google_stock.get("release_bars", []) if b.get("kind") != "OBSERVED"]:
        unvested_entries.append({
            **google_stock,
            "symbol": primary_sym,
            "short_name": _derive_short_company_name(primary_sym, "", cfg),
            "award_type": str(google_stock.get("award_type") or "RSU"),
            "live_price": goog_live,
            "account_id": stock_plan_acct_id,
            "institution": (accounts_db.get(stock_plan_acct_id) or {}).get("institution") or "Stock Plan",
        })

    for ug_idx, ug in enumerate(unvested_entries):
        ug_sym = str(ug.get("symbol") or primary_sym).upper()
        ug_px = float(ug.get("live_price") or (goog_live if ug_sym == primary_sym else 0.0))
        fut_bars = [b for b in ug.get("release_bars", []) if b.get("kind") != "OBSERVED"]
        if not fut_bars:
            continue
        fut_tranches: list[dict[str, Any]] = []
        for fb in fut_bars:
            n_sh = round(float(fb.get("totNetShares") or 0.0), 6)
            g_sh = round(float(fb.get("totGrossShares") or 0.0), 6)
            if n_sh > 0:
                fut_tranches.append(
                    {
                        "vest_date": str(fb["iso"]),
                        "grant_id": "UNVESTED",
                        "gross_shares": g_sh,
                        "shares": n_sh,
                        "basis_per_share": round(ug_px, 4),
                        "cost_basis": round(n_sh * ug_px, 2),
                        "current_value": round(n_sh * ug_px, 2),
                    }
                )
        if not fut_tranches:
            continue
        fut_net_sh = round(sum(t["shares"] for t in fut_tranches), 3)
        fut_gross_sh = round(sum(t["gross_shares"] for t in fut_tranches), 3)
        fut_val = round(fut_net_sh * ug_px, 2)
        fut_first_iso = fut_tranches[0]["vest_date"]
        fut_last_iso = fut_tranches[-1]["vest_date"]
        ug_acct = str(ug.get("account_id") or (stock_plan_acct_id if ug_sym == primary_sym else "") or "").strip()
        ug_inst = str(ug.get("institution") or (accounts_db.get(ug_acct) or {}).get("institution") or "Stock Plan").strip()
        ug_acct_lbl = _friendly_account_label(ug_acct, accounts_db, cfg) if ug_acct else ug_inst
        bucket_id = "future_unvested_goog" if ug_sym == "GOOG" else f"future_unvested_{ug_sym.lower()}"
        award_lbl = str(ug.get("award_type") or "RSU").strip()

        assets.append(
            {
                "id": bucket_id,
                "account_id": ug_acct or None,
                "symbol": ug_sym,
                "default_rank": 2 + ug_idx,
                "color": "#7dd3fc" if ug_idx == 0 else "#a5b4fc",
                "title": f"Future Unvested {ug_sym} Releases",
                "short_title": f"Unvested {ug_sym}",
                "subtitle": f"{ug_acct_lbl} • {fut_net_sh:,.3f} est. net sh ({fut_gross_sh:,.3f} gross sh across {len(fut_tranches)} future {award_lbl} vests)",
                "account_label": ug_acct_lbl,
                "bucket": "goog" if (ug_sym == primary_sym if primary_sym else ug_idx == 0) else "non_goog",
                "grant_symbol": ug_sym,
                "forecast_key": ug_sym,
                "default_risk_class": "single_stock",
                "is_unvested_goog": True,
                "is_goog_priced": (ug_sym == primary_sym),
                "shares": fut_net_sh,
                "current_value": fut_val,
                "cost_basis": fut_val,
                "basis_sub": "100% FMV at each vest date",
                "maturity_iso": fut_first_iso,
                "liquidity_label": "Scheduled Vests",
                "liquidity_sub": f"{fut_first_iso} → {fut_last_iso}",
                "tax_model": "UNVESTED_GOOG",
                "tax_rate": LTCG_TOP_RATE,
                "tax_rule_title": "Withheld at Vest • LTCG Post-Vest",
                "tax_rule_detail": "Unlocks as each tranche vests with 100% cost basis on vest date (ordinary income tax already withheld via shares). Selling immediately at vest incurs 0% LTCG.",
                "tranches": fut_tranches,
            }
        )

    # --- 3. Cash & Bank Deposit Sweeps (`rollup_cash_all`) ---
    # DA-E01: Keep retirement-account (`TRAD_IRA`, `ROTH_IRA`, etc.) cash sweeps inside their
    # respective tax wrappers so IRA cash is taxed as ordinary income (and counted in RMDs) and
    # Roth cash compounds 100% tax-free rather than co-mingling into taxable `cash_sweeps`.
    _RET_TAX_FREE_WRAPPERS = {"ROTH_IRA", "ROTH_401K", "HSA"}
    _RET_PRETAX_WRAPPERS = {"TRAD_IRA", "401K", "TRAD_401K", "SEP_IRA", "SIMPLE_IRA", "403B"}
    if "rollup_cash_all" in rollup_map:
        c_roll = rollup_map["rollup_cash_all"]
        c_children = list(c_roll.get("children") or [])
        taxable_cash_children: list[dict[str, Any]] = []
        wrapped_cash_by_acct: dict[str, float] = {}
        for ch in c_children:
            ch_aid = str(ch.get("account_id") or "")
            ch_wrap = str((accounts_db.get(ch_aid) or {}).get("tax_status") or "TAXABLE").upper()
            if ch_aid and ch_wrap in (_RET_TAX_FREE_WRAPPERS | _RET_PRETAX_WRAPPERS):
                wrapped_cash_by_acct[ch_aid] = round(
                    wrapped_cash_by_acct.get(ch_aid, 0.0) + float(ch.get("current_value") or 0.0), 2
                )
            else:
                taxable_cash_children.append(ch)

        if wrapped_cash_by_acct:
            taxable_cash_val = round(sum(float(ch.get("current_value") or 0.0) for ch in taxable_cash_children), 2)
        else:
            taxable_cash_val = round(float(c_roll.get("current_value") or 0.0), 2)

        if taxable_cash_val > 0 or not wrapped_cash_by_acct:
            n_cash_accts = len(taxable_cash_children) or len(c_children) or len(accounts_db)
            c_title, c_short = _sim_title_short(
                "rollup_cash_all",
                "Cash, Bank Deposit Sweeps & Liquid Deposits",
                "Cash & Deposit Sweeps",
            )
            assets.append(
                {
                    "id": "cash_sweeps",
                    "account_id": None,
                    "default_rank": 45,
                    "color": "#94a3b8",
                    "title": c_title,
                    "short_title": c_short,
                    "subtitle": f"{n_cash_accts} Accounts • Liquid USD & FDIC Bank Sweeps",
                    "account_label": f"{n_cash_accts} Accounts",
                    "bucket": "non_goog",
                    "is_unvested_goog": False,
                    "is_goog_priced": False,
                    "shares": None,
                    "current_value": taxable_cash_val,
                    "cost_basis": taxable_cash_val,
                    "basis_sub": "1:1 USD Principal (0% gain)",
                    "maturity_iso": None,
                    "liquidity_label": "Liquid Now",
                    "liquidity_sub": "Immediate cash availability",
                    "tax_model": "CASH",
                    "tax_rate": LTCG_TOP_RATE,
                    "tax_rule_title": "0% Tax on Principal",
                    "tax_rule_detail": "Principal withdrawals are 100% tax-free; only future accumulated sweep interest is taxed.",
                    "tranches": [],
                    "_rollup_gid": "rollup_cash_all",
                }
            )

        for ch_aid, w_cash_val in wrapped_cash_by_acct.items():
            if w_cash_val <= 0:
                continue
            w_slug = ch_aid.replace("-", "_")
            w_lbl = _friendly_account_label(ch_aid, accounts_db, cfg)
            w_status = str((accounts_db.get(ch_aid) or {}).get("tax_status") or "TAXABLE").upper()
            # CROSS-FILE INVARIANTS (DA2-01 & SA3-06):
            # 1. Bind `"_rollup_gid": "rollup_cash_all"` so `build_report_payload` assigns `cash_fixed` sweep
            #    enrichment (~3.5% APY, 0% equity beta) rather than employer `single_stock`!
            # 2. Keep `cash_{w_slug}` synchronized with `translator.py:_preseed_portfolio_composite_ids()`.
            assets.append(
                {
                    "id": f"cash_{w_slug}",
                    "account_id": ch_aid,
                    "default_rank": 65 if w_status in _RET_TAX_FREE_WRAPPERS else 46,
                    "color": "#94a3b8",
                    "title": f"Cash & Deposit Sweep ({w_lbl})",
                    "short_title": f"Cash ({w_lbl[:18]})",
                    "subtitle": f"{w_lbl} • Retirement Sweep Cash",
                    "account_label": w_lbl,
                    "bucket": "non_goog",
                    "is_unvested_goog": False,
                    "is_goog_priced": False,
                    "shares": None,
                    "current_value": w_cash_val,
                    "cost_basis": w_cash_val,
                    "basis_sub": "Retirement Account Cash Sweep",
                    "maturity_iso": None,
                    "liquidity_label": "Liquid Now",
                    "liquidity_sub": "Immediate sweep availability",
                    "tax_model": "CASH",
                    "tax_rate": 0.0,
                    "tax_rule_title": "Retirement Cash Sweep",
                    "tax_rule_detail": "Cash sweep held inside retirement wrapper.",
                    "tranches": [],
                    "_rollup_gid": "rollup_cash_all",
                }
            )

    # --- 4. Municipal Bond Rollups (`rollup_munis_*`), Direct-Indexing UMAs (`rollup_uma_*`),
    #        Standalone Mutual Funds/Equities (`single_*`), and Annuities (`single_*`) ---
    # DA-E06: Tax-optimal unconfigured default rank hierarchy:
    #   1..3 High-basis / unvested grants -> 5 Direct-Indexing SMA -> 10..19 Taxable diversified funds ->
    #   20..29 Non-qualified annuities -> 32 Tax-exempt Munis -> 35..39 Traditional IRA ->
    #   40..41 Low-basis concentrated §1014 stock -> 45 Cash Reserve -> 60+ Roth IRA / HSA.
    fund_rank_counter = 10
    ann_rank_counter = 20
    for r in holdings_rollups:
        gid = r["group_id"]
        if gid in ("rollup_cash_all", ms_rollup_gid):
            continue
        aid = str(r.get("account_id") or "")
        acct_slug = aid.replace("-", "_")
        inst_lbl = _friendly_account_label(aid, accounts_db, cfg) if aid else (r.get("account_label") or aid)
        cur_v = round(float(r.get("current_value") or 0.0), 2)
        p_val = float(r.get("principal_val") or cur_v)

        if gid.startswith("rollup_munis_"):
            bid = f"munis_{acct_slug}"
            n_bonds = len(r.get("children") or [])
            d_title = r.get("statement_name") or f"Municipal Bond SMA ({n_bonds} Bonds)"
            d_short = f"Muni Bonds ({n_bonds})"
            t_final, s_final = _sim_title_short(gid, d_title, d_short)
            assets.append(
                {
                    "id": bid,
                    "account_id": aid or None,
                    "default_rank": 32,
                    "color": "#0ea5e9",
                    "title": t_final,
                    "short_title": s_final,
                    "subtitle": f"{inst_lbl} • Tax-Exempt Municipal Yield",
                    "account_label": inst_lbl,
                    "bucket": "non_goog",
                    "is_unvested_goog": False,
                    "is_goog_priced": False,
                    "shares": None,
                    "current_value": cur_v,
                    "cost_basis": p_val,
                    "basis_sub": r.get("principal_sub") or "Initial Principal",
                    "maturity_iso": None,
                    "liquidity_label": "Liquid Now",
                    "liquidity_sub": r.get("maturity_str") or f"{n_bonds} Municipal Bonds",
                    "tax_model": "MUNI",
                    "tax_rate": LTCG_TOP_RATE,
                    "tax_rule_title": f"{_muni_juris} Tax on Yield • LTCG on Prem.",
                    "tax_rule_detail": "Tax-exempt municipal coupon growth is 100% Federal & state tax-exempt (increases tax-free basis); placed after taxable funds to preserve tax-exempt compounding.",
                    "tranches": [],
                    "_rollup_gid": gid,
                }
            )
        elif gid.startswith("rollup_uma_"):
            bid = f"uma_{acct_slug}"
            uma_children = list(r.get("children") or [])
            n_stocks = len(uma_children)
            acct_c = acct_cfg_map.get(aid) or {}
            uma_b = float(acct_c.get("uma_rollup_basis") or r.get("principal_val") or cur_v)
            uma_b_sub = acct_c.get("uma_rollup_basis_sub") or r.get("principal_sub") or "Direct-Indexing Basis"
            d_title = r.get("statement_name") or f"US Large Cap Basket ({n_stocks} Individual Stocks)"
            d_short = f"{n_stocks}-Stock UMA"
            t_final, s_final = _sim_title_short(gid, d_title, d_short)
            # DA-E07: Preserve constituent lot dispersion for HIFO & tax-loss harvesting when statements report per-stock cost basis
            uma_lots: list[dict[str, Any]] = []
            for ch in uma_children:
                ch_cv = float(ch.get("current_value") or 0.0)
                ch_pv = ch.get("principal_val")
                if ch_cv > 0.01 and ch_pv is not None:
                    uma_lots.append({
                        "vest_date": str(ch.get("principal_date") or ""),
                        "grant_id": str(ch.get("symbol") or "LOT"),
                        "shares": round(float(ch.get("quantity") or 0.0), 6),
                        "cost_basis": round(float(ch_pv), 2),
                        "current_value": round(ch_cv, 2),
                    })
            use_uma_tranches = len(uma_lots) >= 2
            if use_uma_tranches:
                uma_b = round(sum(t["cost_basis"] for t in uma_lots), 2)
            assets.append(
                {
                    "id": bid,
                    "account_id": aid or None,
                    "default_rank": 5,
                    "color": "#10b981",
                    "title": t_final,
                    "short_title": s_final,
                    "subtitle": f"{inst_lbl} • Direct-Indexing Tax-Loss Harvesting SMA",
                    "account_label": inst_lbl,
                    "bucket": "non_goog",
                    "is_unvested_goog": False,
                    "is_goog_priced": False,
                    "shares": None,
                    "current_value": cur_v,
                    "cost_basis": uma_b,
                    "basis_sub": uma_b_sub,
                    "maturity_iso": None,
                    "liquidity_label": "Liquid Now",
                    "liquidity_sub": f"{n_stocks} exchange-traded equities",
                    "tax_model": "LTCG_TRANCHES" if use_uma_tranches else "LTCG_POOL",
                    "tax_rate": LTCG_TOP_RATE,
                    "tax_rule_title": f"Progressive LTCG (0%–20% Fed, up to {LTCG_TOP_RATE * 100:.1f}%) • Direct-Indexing SMA",
                    "tax_rule_detail": (
                        f"Taxed progressively across 0%/15%/20% Federal LTCG + state/NIIT brackets "
                        f"(up to {LTCG_TOP_RATE * 100:.1f}% ceiling: {_ltcg_decomp}) strictly on the realized gain above basis."
                    ),
                    "tranches": uma_lots if use_uma_tranches else [],
                    "_rollup_gid": gid,
                }
            )
        elif gid.startswith("rollup_plan_"):
            raw_sym = str(r.get("symbol") or "").lstrip("#")
            inv_type = str(r.get("asset_class") or r.get("investment_type") or "")
            r_rc = str(r.get("default_risk_class") or r.get("ingested_risk_class") or "").lower()
            inv_up = inv_type.upper()
            is_taxable_bond = (
                (r_rc == "fixed_income" or any(tok in inv_up for tok in ("FIXED_INCOME", "BOND", "TREASURY", "CD", "CERTIFICATE")))
                and "MUNI" not in inv_up
            )
            for ch in r.get("children") or []:
                ch_aid = str(ch.get("account_id") or "")
                if not ch_aid:
                    continue
                ch_slug = ch_aid.replace("-", "_")
                ch_lbl = _friendly_account_label(ch_aid, accounts_db, cfg)
                ch_cur_v = round(float(ch.get("current_value") or 0.0), 2)
                ch_p_val = float(ch.get("principal_val") if ch.get("principal_val") is not None else ch_cur_v)
                ch_sym = str(ch.get("symbol") or raw_sym).lstrip("#")
                bid = f"{ch_sym.lower()}_{ch_slug}"
                ch_gid = str(ch.get("holding_key") or f"single_{ch_aid}_{ch_sym}")
                base_stmt_name = str(r.get("statement_name") or ch.get("statement_name") or ch_sym)
                ch_wrap = str((accounts_db.get(ch_aid) or {}).get("tax_status") or "TAXABLE").upper()
                wrap_tag = _tax_sleeve_short_label(ch_wrap)
                d_title = f"{base_stmt_name} — {wrap_tag}"
                d_short = f"{ch_sym} ({wrap_tag})"
                t_final, s_final = _sim_title_short(ch_gid, d_title, d_short)
                gain_ratio = max(0.0, (ch_cur_v - ch_p_val) / ch_cur_v) if ch_cur_v > 0 else 0.0
                rank_val = 10 + min(9, int(round(gain_ratio * 8))) + (fund_rank_counter - 10)
                assets.append(
                    {
                        "id": bid,
                        "account_id": ch_aid,
                        "symbol": ch_sym.upper() if ch_sym else None,
                        "parent_forecast_keys": [gid],
                        "default_rank": rank_val,
                        "color": "#2dd4bf",
                        "title": t_final,
                        "short_title": s_final,
                        "subtitle": f"{ch_lbl} • {ch.get('quantity_str') or '1 contract'}",
                        "account_label": ch_lbl,
                        "bucket": "non_goog",
                        "is_unvested_goog": False,
                        "is_goog_priced": False,
                        "shares": None,
                        "current_value": ch_cur_v,
                        "cost_basis": ch_p_val,
                        "basis_sub": ch.get("principal_sub") or "Initial Principal",
                        "maturity_iso": None,
                        "liquidity_label": "Liquid Now",
                        "liquidity_sub": "Daily mutual fund NAV" if inv_type == "MUTUAL_FUND" else ("Fixed-income position" if is_taxable_bond else "Retirement plan investment"),
                        "tax_model": "TAXABLE_YIELD" if is_taxable_bond else "LTCG_POOL",
                        "tax_rate": ORD_TOP_RATE if is_taxable_bond else LTCG_TOP_RATE,
                        "tax_rule_title": "",
                        "tax_rule_detail": "",
                        "tranches": [],
                        "_rollup_gid": gid,
                    }
                )
                fund_rank_counter += 1
        elif gid.startswith("single_"):
            inv_type = str(r.get("asset_class") or r.get("investment_type") or "")
            raw_sym = str(r.get("symbol") or "").lstrip("#")
            if inv_type.endswith("ANNUITY") or "ANNUITY" in inv_type.upper():
                existing_ann_bid = next(
                    (k for k in sim_buckets_cfg if k.startswith("ann_") and k.endswith(f"_{raw_sym}")),
                    None,
                )
                c_slug = _derive_annuity_carrier_slug(r.get("statement_name") or "", raw_sym)
                bid = existing_ann_bid or f"ann_{c_slug}_{raw_sym}"
                mat_iso = r.get("maturity_date")
                surr_raw = r.get("surrender_val")
                surr_val = round(float(surr_raw), 2) if surr_raw is not None else None
                d_title = r.get("statement_name") or f"Annuity {raw_sym}"
                d_short = d_title[:28]
                t_final, s_final = _sim_title_short(gid, d_title, d_short)
                assets.append(
                    {
                        "id": bid,
                        "account_id": aid or None,
                        "default_rank": ann_rank_counter,
                        "color": "#06b6d4",
                        "title": t_final,
                        "short_title": s_final,
                        "subtitle": f"{inst_lbl}",
                        "account_label": inst_lbl,
                        "bucket": "non_goog",
                        "is_unvested_goog": False,
                        "is_goog_priced": False,
                        "shares": None,
                        "current_value": cur_v,
                        "cost_basis": p_val,
                        "basis_sub": f"Initial Contract Premium (${p_val / 1000.0:,.0f}K)",
                        "maturity_iso": mat_iso,
                        "surrender_value": surr_val,
                        "free_withdrawal_pct": 10.0 if mat_iso else 0.0,
                        "liquidity_label": f"Locked until {mat_iso} (10%/yr free)" if mat_iso else "Liquid Now",
                        "liquidity_sub": "Annuity Surrender Period" if mat_iso else "Annuity Contract",
                        "tax_model": "ANNUITY_LIFO",
                        "tax_rate": ORD_TOP_RATE,
                        "tax_rule_title": f"IRS §72 LIFO: Progressive Ord. Inc. (up to {ORD_TOP_RATE * 100:.1f}%) on Gain, 0% Prin.",
                        "tax_rule_detail": (
                            (f"Surrender period until {mat_iso} (standard 10%/yr penalty-free corridor). " if mat_iso else "")
                            + f"Under IRS §72 LIFO rules, withdrawals pull accumulated earnings first at progressive ordinary + NIIT rates "
                            f"(up to {ORD_TOP_RATE * 100:.1f}% {_ord_tag}; +10% IRS penalty before age 59½), then ${p_val:,.0f} principal comes out at 0% tax."
                        ),
                        "tranches": [],
                        "_rollup_gid": gid,
                    }
                )
                ann_rank_counter += 1
            elif primary_sym and raw_sym.upper() == primary_sym:
                brok_inst = str(r.get("institution") or (accounts_db.get(aid) or {}).get("institution") or "Brokerage")
                brok_tag = _short_inst_tag(brok_inst)
                brok_slug = re.sub(r"[^a-z0-9]+", "_", brok_tag.lower()).strip("_") or "brok"
                legacy_wfa_bid = f"wfa_goog_{acct_slug}"
                bid = (
                    legacy_wfa_bid
                    if (primary_sym == "GOOG" and (legacy_wfa_bid in sim_buckets_cfg or brok_tag == "WFA"))
                    else f"{brok_slug}_{primary_sym.lower()}_{acct_slug}"
                )
                sh_str = str(r.get("quantity_str") or "").replace("sh", "").replace(",", "").strip()
                sh_cnt = float(sh_str) if sh_str else round(cur_v / goog_live, 3) if goog_live > 0 else 0.0
                d_title = r.get("statement_name") or f"{primary_sym} ({inst_lbl})"
                d_short = f"{brok_tag} {primary_sym}"
                t_final, s_final = _sim_title_short(gid, d_title, d_short)
                assets.append(
                    {
                        "id": bid,
                        "account_id": aid or None,
                        "symbol": primary_sym,
                        "default_rank": 41,
                        "color": "#0369a1",
                        "title": t_final,
                        "short_title": s_final,
                        "subtitle": f"{inst_lbl} • {sh_cnt:,.3f} sh",
                        "account_label": inst_lbl,
                        "bucket": "goog",
                        "is_unvested_goog": False,
                        "is_goog_priced": True,
                        "shares": sh_cnt,
                        "current_value": cur_v,
                        "cost_basis": p_val,
                        "basis_sub": r.get("principal_sub") or "Carryover basis",
                        "maturity_iso": None,
                        "liquidity_label": "Liquid Now",
                        "liquidity_sub": f"{sh_cnt:,.0f} shares",
                        "tax_model": "LTCG_POOL",
                        "tax_rate": LTCG_TOP_RATE,
                        "tax_rule_title": f"Progressive LTCG (0%–20% Fed, up to {LTCG_TOP_RATE * 100:.1f}%) on Gain",
                        "tax_rule_detail": (
                            f"Realized gain above ${p_val:,.0f} basis is taxed progressively across 0%/15%/20% Federal LTCG "
                            f"+ state/NIIT brackets (up to {LTCG_TOP_RATE * 100:.1f}% ceiling)."
                        ),
                        "tranches": [],
                        "_rollup_gid": gid,
                    }
                )
            else:
                bid = f"{raw_sym.lower()}_{acct_slug}"
                sh_str = str(r.get("quantity_str") or "")
                d_title = f"{r.get('statement_name') or raw_sym} ({raw_sym})" if raw_sym not in str(r.get("statement_name") or "") else str(r.get("statement_name"))
                d_short = f"{raw_sym}"
                t_final, s_final = _sim_title_short(gid, d_title, d_short)
                gain_ratio = max(0.0, (cur_v - p_val) / cur_v) if cur_v > 0 else 0.0
                rank_val = 10 + min(9, int(round(gain_ratio * 8))) + (fund_rank_counter - 10)
                # DA2-10a: Assign `TAXABLE_YIELD` to taxable fixed-income bond funds/CDs (annual ordinary coupon drag),
                # while keeping equities/equity-funds on `LTCG_POOL` (wrapper loop overrides IRA/Roth accounts below).
                r_rc = str(r.get("default_risk_class") or r.get("ingested_risk_class") or "").lower()
                inv_up = inv_type.upper()
                is_taxable_bond = (
                    (r_rc == "fixed_income" or any(tok in inv_up for tok in ("FIXED_INCOME", "BOND", "TREASURY", "CD", "CERTIFICATE")))
                    and "MUNI" not in inv_up
                )
                is_us_treas = any(tok in (d_title.upper() + " " + inv_up) for tok in ("TREASURY", "UST ", "T-BILL", "TBILL"))
                if is_us_treas:
                    f_tax_model = "TREASURY"
                    f_tax_rate = round(max(0.0, ORD_TOP_RATE - float(((cfg or {}).get("tax_rates") or {}).get("state_rate") or 0.0)), 4)
                    f_tax_title = f"US Treasury (Progressive Fed Ord. on Yield, 0% State)"
                    f_tax_detail = "Annual yield taxed progressively at Federal ordinary + NIIT rates (0% state tax) with stepped-up basis."
                elif is_taxable_bond:
                    f_tax_model = "TAXABLE_YIELD"
                    f_tax_rate = ORD_TOP_RATE
                    f_tax_title = f"Taxable Fixed Income (Progressive Ord. on Yield, up to {ORD_TOP_RATE * 100:.1f}%)"
                    f_tax_detail = f"Annual coupon yield is taxed progressively at ordinary income + NIIT rates (up to {ORD_TOP_RATE * 100:.1f}%), stepping up cost basis on reinvested net yield."
                else:
                    f_tax_model = "LTCG_POOL"
                    f_tax_rate = LTCG_TOP_RATE
                    f_tax_title = f"Progressive LTCG (0%–20% Fed, up to {LTCG_TOP_RATE * 100:.1f}%) on Gain"
                    f_tax_detail = (
                        f"Return of ${p_val:,.0f} principal is 0% tax; embedded gain portion is taxed progressively across "
                        f"0%/15%/20% Federal LTCG + state/NIIT brackets (up to {LTCG_TOP_RATE * 100:.1f}% ceiling)."
                    )
                assets.append(
                    {
                        "id": bid,
                        "account_id": aid or None,
                        "symbol": raw_sym.upper() if raw_sym else None,
                        "default_rank": rank_val,
                        "color": "#2dd4bf",
                        "title": t_final,
                        "short_title": s_final,
                        "subtitle": f"{inst_lbl} • {sh_str}",
                        "account_label": inst_lbl,
                        "bucket": "non_goog",
                        "is_unvested_goog": False,
                        "is_goog_priced": False,
                        "shares": None,
                        "current_value": cur_v,
                        "cost_basis": p_val,
                        "basis_sub": r.get("principal_sub") or "Initial Principal",
                        "maturity_iso": None,
                        "liquidity_label": "Liquid Now",
                        "liquidity_sub": "Daily mutual fund NAV" if inv_type == "MUTUAL_FUND" else ("Fixed-income position" if is_taxable_bond else "Exchange-traded equity"),
                        "tax_model": f_tax_model,
                        "tax_rate": f_tax_rate,
                        "tax_rule_title": f_tax_title,
                        "tax_rule_detail": f_tax_detail,
                        "tranches": [],
                        "_rollup_gid": gid,
                    }
                )
                fund_rank_counter += 1

    # --- Account Tax Wrapper Resolution (DA-E02 & DA-E08) ---
    # DESIGN INVARIANTS (DO NOT REORDER OR REMOVE `account_id`):
    #   1. `DA-E02`: Every bucket stores explicit `"account_id"` at creation time above so `_acct_id(a)`
    #      resolves directly against `accounts_db` even when user friendly names omit raw account numbers
    #      (preventing Roth / Traditional IRAs from being silently demoted to `TAXABLE` `LTCG_POOL`).
    #   2. `DA-E08`: Wrapper defaults MUST execute BEFORE Layer 2 `sim_buckets` user JSON overrides below
    #      so custom `tax_rule_title` / `tax_rule_detail` labels in `project.json` are preserved.
    IRA_ORD_RATE = _eff_tax["ira_ordinary_tax_rate"]

    def _acct_id(a: dict[str, Any]) -> str | None:
        explicit_aid = a.get("account_id")
        if explicit_aid:
            return str(explicit_aid)
        lbl = a.get("account_label") or a.get("subtitle") or ""
        for known_aid in accounts_db.keys():
            if known_aid in lbl:
                return known_aid
        m = re.search(r"(\d{4}[-_]\d{4}|[A-Z]{2,4}[-_]?\d+[A-Z0-9_-]*)", lbl)
        return m.group(1) if m else None

    invs_raw = cfg.get("investors") or []
    inv_dob_list: list[tuple[str, str, int, int, int, bool]] = []
    for i_idx, inv_item in enumerate(invs_raw):
        if not isinstance(inv_item, dict):
            continue
        i_name = str(inv_item.get("name") or "").strip()
        i_id = str(inv_item.get("id") or i_name or f"investor_{i_idx + 1}").strip().lower()
        b_parts = str(inv_item.get("birth_date") or "1975-01-01").split("-")
        # SECURITY INVARIANT (SA-08 / SA3-05): Trim exported owner birth day to `1` (`YYYY-MM` DOB granularity);
        # monthly simulation steps resolve ages at month-start (`YYYY-MM-01`), so exact `DD` is never needed.
        try:
            by_i = int(b_parts[0])
            bm_i = int(b_parts[1]) if len(b_parts) > 1 else 1
            bd_i = 1
        except (TypeError, ValueError):
            by_i, bm_i, bd_i = 1975, 1, 1
        is_rmd_i = bool(inv_item.get("is_rmd_owner", inv_item.get("rmd_owner", i_idx == 0)))
        inv_dob_list.append((i_id, i_name.lower(), by_i, bm_i, bd_i, is_rmd_i))
    def_owner = next((x for x in inv_dob_list if x[5]), inv_dob_list[0] if inv_dob_list else ("", "", 1975, 1, 1, True))

    def _resolve_owner(aid_val: str | None, asset_obj: dict[str, Any]):
        acct_meta = (acct_cfg_map.get(aid_val) or {}) if aid_val else {}
        db_meta = (accounts_db.get(aid_val) or {}) if aid_val else {}
        explicit_owner = str(acct_meta.get("owner") or acct_meta.get("investor_id") or db_meta.get("owner") or "").strip().lower()
        hay = " ".join([
            explicit_owner,
            str(acct_meta.get("friendly_account_name") or ""),
            str(db_meta.get("account_name") or ""),
            str(asset_obj.get("title") or ""),
        ]).lower()
        for i_id, i_name, by_i, bm_i, bd_i, _rmd in inv_dob_list:
            if explicit_owner and (explicit_owner == i_id or explicit_owner == i_name):
                return i_id, by_i, bm_i, bd_i
        if not explicit_owner:
            candidates = []
            for i_id, i_name, by_i, bm_i, bd_i, _rmd in inv_dob_list:
                first_tok = i_name.split()[0] if i_name else ""
                if len(first_tok) >= 3 and re.search(rf"\b{re.escape(first_tok)}\b", hay):
                    candidates.append((i_id, by_i, bm_i, bd_i))
            if len(candidates) == 1:
                return candidates[0]
        return None, def_owner[2], def_owner[3], def_owner[4]

    for a in assets:
        aid = _acct_id(a)
        a["account_id"] = aid
        if inv_dob_list:
            owner_id, oby, obm, obd = _resolve_owner(aid, a)
            if owner_id is not None:
                a["rmd_owner_index"] = next(i for i, owner in enumerate(inv_dob_list) if owner[0] == owner_id)
            a["owner_birth_year"] = oby
            a["owner_birth_month"] = obm
            a["owner_birth_day"] = obd
        wrapper = str((accounts_db.get(aid) or {}).get("tax_status") or "TAXABLE") if aid else "TAXABLE"
        a["tax_wrapper"] = wrapper
        if wrapper in _RET_TAX_FREE_WRAPPERS:
            a["tax_model"] = "TAX_FREE"
            a["tax_rate"] = 0.0
            if a["id"] not in sim_buckets_cfg:
                a["default_rank"] = max(60, int(a.get("default_rank", 60)))
            a["tax_rule_title"] = f"{wrapper.replace('_', ' ')} — 0% Tax on Qualified Distributions"
            a["tax_rule_detail"] = (
                f"Held in a tax-free {wrapper.replace('_', ' ')} ({aid}). Qualified withdrawals are 100% Federal & state tax-free; "
                "growth compounds tax-free. Capital losses here are not deductible."
            )
        elif wrapper in _RET_PRETAX_WRAPPERS:
            a["tax_model"] = "ORDINARY_FULL"
            a["tax_rate"] = IRA_ORD_RATE
            if a["id"] not in sim_buckets_cfg and not a["id"].startswith("cash_"):
                a["default_rank"] = max(35, int(a.get("default_rank", 35)))
            a["tax_rule_title"] = f"Tax-Deferred ({wrapper.replace('_', ' ')}) — Progressive Ord. Inc. (up to {IRA_ORD_RATE * 100:.1f}%)"
            a["tax_rule_detail"] = (
                f"Held in a pre-tax retirement account ({aid}). Distributions are taxed progressively across 10%–37% Federal "
                f"+ state ordinary income brackets (up to {IRA_ORD_RATE * 100:.1f}% ceiling: {_ira_decomp}; NIIT does not apply "
                "to qualified-plan/IRA distributions; +10% IRS penalty before age 59½). Subject to SECURE 2.0 RMDs."
                + ("" if not a.get("maturity_iso") else f" Contract surrender period until {a['maturity_iso']} (10%/yr free corridor).")
            )

    # --- Layer 2: Power-User `sim_buckets` JSON Customization ---
    filtered_assets: list[dict[str, Any]] = []
    for a in assets:
        b_ovr = sim_buckets_cfg.get(a["id"]) or {}
        if b_ovr.get("exclude"):
            continue
        rollup_gid = a.pop("_rollup_gid", None)
        if rollup_gid:
            a["rollup_group_id"] = rollup_gid
        has_live_friendly_name = bool(rollup_gid and (rollup_map.get(rollup_gid) or {}).get("friendly_name"))

        if "default_rank" in b_ovr:
            a["default_rank"] = int(b_ovr["default_rank"])
        if "color" in b_ovr:
            a["color"] = str(b_ovr["color"])
        a["color"] = _sanitize_non_exclusive_color(a.get("color"), int(a.get("default_rank", 0)))
        if not has_live_friendly_name:
            if b_ovr.get("title") or b_ovr.get("default_title"):
                a["title"] = str(b_ovr.get("title") or b_ovr.get("default_title"))
            if b_ovr.get("short_title") or b_ovr.get("default_short_title"):
                a["short_title"] = str(b_ovr.get("short_title") or b_ovr.get("default_short_title"))
        for k in (
            "subtitle",
            "cost_basis",
            "basis_sub",
            "maturity_iso",
            "surrender_value",
            "free_withdrawal_pct",
            "liquidity_label",
            "liquidity_sub",
            "tax_rule_title",
            "tax_rule_detail",
        ):
            if k in b_ovr and b_ovr[k] is not None:
                a[k] = float(b_ovr[k]) if k in ("cost_basis", "surrender_value", "free_withdrawal_pct") else b_ovr[k]
        filtered_assets.append(a)

    assets = sorted(filtered_assets, key=lambda x: (int(x.get("default_rank", 99)), x["id"]))

    # Attach per-asset growth model (`sim_asset_rates` or `sim_buckets[id].rate_model`)
    rate_cfg = cfg.get("sim_asset_rates") or {}
    cash_int_tax = _eff_tax["cash_interest_tax_rate"]
    for a in assets:
        b_ovr = sim_buckets_cfg.get(a["id"]) or {}
        ov = rate_cfg.get(a["id"]) or b_ovr
        rm = str(ov.get("rate_model", "slider")).lower()
        a["rate_model"] = "fixed" if rm == "fixed" else "slider"
        a["annual_rate_pct"] = float(ov["annual_rate_pct"]) if ov.get("annual_rate_pct") is not None else None
        if a["tax_model"] == "CASH":
            a["tax_rate"] = cash_int_tax
        is_cash_id = a["id"].startswith("cash_") or a["tax_model"] == "CASH"
        rc = "cash_fixed" if is_cash_id else infer_risk_class(
            asset_id=a["id"],
            bucket=str(a.get("bucket") or ""),
            rate_model=a["rate_model"],
            asset_class="RSU" if a.get("is_unvested_goog") else str(a.get("asset_class") or ""),
            tax_model=str(a.get("tax_model") or ""),
            symbol=str(a.get("symbol") or ""),
            primary_symbol=primary_sym,
        )
        a["forecast_key"] = a["id"]
        existing_parents = list(a.get("parent_forecast_keys") or [])
        if primary_sym and (a.get("symbol") == primary_sym or a.get("is_goog_priced")) and primary_sym not in existing_parents:
            existing_parents.append(primary_sym)
        a["parent_forecast_keys"] = existing_parents
        a["default_risk_class"] = rc
        a["default_growth_pct"] = round(float(a["annual_rate_pct"]), 2) if (a["rate_model"] == "fixed" and a["annual_rate_pct"] is not None) else 0.0

    return assets


def _build_bucket_appreciation_summary(
    enriched_items: list[dict[str, Any]],
    holdings_rollups: list[dict[str, Any]],
    price_book: Any,
    today_date: date,
    total_now: float,
    goog_live: float,
    stock_plan_acct_id: str = "",
    primary_sym: str = "",
    accounts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Computes the annual appreciation summary for the two top-level vested buckets:
      1. Primary equity holdings: pure share price CAGR from start of 2023 -> `today_date`.
      2. Diversified (non-primary) holdings: current-value-weighted average annual growth
         based on each holding's actual starting principal and actual starting date.
    """
    today_iso = today_date.isoformat()
    sym_lookup = (primary_sym or "").strip().upper()
    goog_items = [it for it in enriched_items if it["stack_bucket"] == "goog_combined"]

    goog_val = round(sum(it["current_value"] for it in goog_items), 2)
    goog_shares = round(sum(it["quantity"] for it in goog_items), 3)
    goog_pct = round(goog_val / total_now * 100.0, 2) if total_now > 0 else 0.0

    non_goog_val = round(total_now - goog_val, 2)
    non_goog_pct = round(non_goog_val / total_now * 100.0, 2) if total_now > 0 else 0.0
    wfb_accounts_val = round(
        sum(it["current_value"] for it in enriched_items if it["account_id"] != stock_plan_acct_id),
        2,
    )

    # 1. Pure primary stock price CAGR from start of 2023 -> today, plus 5-year reference CAGR
    goog_start = (
        (
            price_book.price_with_date_on_or_before(sym_lookup, "2023-01-01")
            or price_book.price_with_date_on_or_before(sym_lookup, "2023-01-10")
            or price_book.earliest_price(sym_lookup)
        )
        if sym_lookup
        else None
    )
    goog_cagr_pct = 0.0
    goog_start_dt = "2023-01-01"
    goog_start_px = goog_live
    goog_yrs = 0.0
    if goog_start and goog_start[1] > 0 and goog_live > 0:
        goog_start_dt, goog_start_px = goog_start[0], float(goog_start[1])
        goog_yrs = (today_date - date.fromisoformat(goog_start_dt)).days / 365.25
        if goog_yrs >= 0.05:
            goog_cagr_pct = round(((goog_live / goog_start_px) ** (1.0 / goog_yrs) - 1.0) * 100.0, 2)

    try:
        five_yrs_ago_dt = today_date.replace(year=today_date.year - 5)
    except ValueError:
        five_yrs_ago_dt = today_date.replace(year=today_date.year - 5, day=28)
    five_yrs_ago_iso = five_yrs_ago_dt.isoformat()
    goog_5yr = (
        (
            price_book.price_with_date_on_or_before(sym_lookup, five_yrs_ago_iso)
            or price_book.price_with_date_on_or_before(sym_lookup, (five_yrs_ago_dt + timedelta(days=7)).isoformat())
            or goog_start
        )
        if sym_lookup
        else None
    )
    goog_5yr_cagr_pct = goog_cagr_pct
    goog_5yr_start_dt = goog_start_dt
    goog_5yr_start_px = goog_start_px
    goog_5yr_yrs = goog_yrs
    if goog_5yr and goog_5yr[1] > 0 and goog_live > 0:
        goog_5yr_start_dt, goog_5yr_start_px = goog_5yr[0], float(goog_5yr[1])
        goog_5yr_yrs = (today_date - date.fromisoformat(goog_5yr_start_dt)).days / 365.25
        if goog_5yr_yrs >= 0.05:
            goog_5yr_cagr_pct = round(((goog_live / goog_5yr_start_px) ** (1.0 / goog_5yr_yrs) - 1.0) * 100.0, 2)

    sym_disp_lbl = sym_lookup or "Primary Equity"
    goog_tip = (
        f"Pure {sym_disp_lbl} Stock Price Appreciation ({goog_start_dt[:4]} – Present): "
        f"{'+' if goog_cagr_pct >= 0 else ''}{goog_cagr_pct:.2f}% / yr over {goog_yrs:.2f} yrs "
        f"(${goog_start_px:,.2f}/sh on {goog_start_dt} → ${goog_live:,.2f}/sh on {today_iso}). "
        f"Past 5-year CAGR ({goog_5yr_start_dt} → {today_iso}): "
        f"{'+' if goog_5yr_cagr_pct >= 0 else ''}{goog_5yr_cagr_pct:.2f}% / yr "
        f"(${goog_5yr_start_px:,.2f}/sh → ${goog_live:,.2f}/sh). "
        f"Measures pure underlying share price CAGR, unaffected by equity vest inflows, sales, or transfers."
    )

    # 2. Diversified Holdings: weighted average annual growth based strictly on actual starting principal & starting date
    dated_rows: list[tuple[str, float, float, str, float]] = []  # (title, cur_val, cagr_pct, p_date, p_val)
    missing_rows: list[tuple[str, float]] = []
    weighted_rate_num = 0.0
    weighted_val_den = 0.0
    total_prin_den = 0.0

    primary_syms_set = {sym_lookup, "GOOGL"} if sym_lookup == "GOOG" else ({sym_lookup} if sym_lookup else set())
    for g in holdings_rollups:
        if primary_syms_set and str(g.get("symbol") or "").upper() in primary_syms_set:
            continue
        g_val = float(g["current_value"])
        g_cagr = g.get("cagr_pct")
        g_p_val = g.get("principal_val")
        g_p_dt = g.get("principal_date")
        if g_cagr is not None and g_p_val is not None and g_p_dt:
            dated_rows.append((g["title"], g_val, float(g_cagr), str(g_p_dt), float(g_p_val)))
            weighted_rate_num += g_val * float(g_cagr)
            weighted_val_den += g_val
            total_prin_den += float(g_p_val)
        elif g["group_id"] != "rollup_cash_all":
            missing_rows.append((g["title"], g_val))

    non_goog_cagr_pct = round(weighted_rate_num / weighted_val_den, 2) if weighted_val_den > 0 else 0.0

    cat_breakdown_parts = []
    for title, c_val, c_rate, p_dt, p_val in sorted(dated_rows, key=lambda item: item[1], reverse=True):
        c_wt = (c_val / weighted_val_den * 100.0) if weighted_val_den > 0 else 0.0
        cat_breakdown_parts.append(
            f"{title}: {'+' if c_rate >= 0 else ''}{c_rate:.2f}%/yr "
            f"(${p_val:,.0f} on {p_dt} → ${c_val:,.0f}, {c_wt:.1f}% wt)"
        )

    missing_note = ""
    if missing_rows:
        m_names = ", ".join(f"{t} (${v:,.0f})" for t, v in missing_rows)
        missing_note = f" Excludes holdings without a configured starting principal/date ({m_names}) and liquid cash sweeps."

    non_primary_lbl = f"Non-{sym_lookup}" if sym_lookup else "Diversified"
    non_goog_tip = (
        f"Value-Weighted Average Annual Growth of {non_primary_lbl} Holdings (from Actual Starting Principal & Inception Date): "
        f"{'+' if non_goog_cagr_pct >= 0 else ''}{non_goog_cagr_pct:.2f}% / yr across ${weighted_val_den:,.2f} "
        f"(initial principal sum: ${total_prin_den:,.2f}).{missing_note} Breakdown: "
        + " • ".join(cat_breakdown_parts)
    )

    # PORTABILITY DO: Build custodian tags dynamically from `accounts[account_id]["institution"]` (`LL-01`, `LL-21`).
    # PORTABILITY DON'T: Hardcode specific brokerage pairs in the primary equity appreciation label.
    inst_tags: list[str] = []
    if accounts:
        for it in goog_items:
            inst = (accounts.get(it["account_id"]) or {}).get("institution") or ""
            tag = "WFB" if "WELLS FARGO" in inst.upper() else _short_inst_tag(inst)
            if tag and tag not in inst_tags:
                inst_tags.append(tag)
    cust_suffix = f" ({' + '.join(inst_tags)})" if inst_tags else ""

    return {
        "goog": {
            "label": f"Total {sym_disp_lbl} Holdings{cust_suffix}",
            "value": goog_val,
            "shares": goog_shares,
            "pct_portfolio": goog_pct,
            "annual_appreciation_pct": goog_cagr_pct,
            "start_date": goog_start_dt,
            "start_price": round(goog_start_px, 2),
            "five_yr_cagr_pct": goog_5yr_cagr_pct,
            "five_yr_start_date": goog_5yr_start_dt,
            "five_yr_start_price": round(goog_5yr_start_px, 2),
            "five_yr_years": round(goog_5yr_yrs, 2),
            "end_price": round(goog_live, 2),
            "tooltip": goog_tip,
        },
        "non_goog": {
            "label": f"Total {non_primary_lbl} Holdings (All Other Assets)",
            "value": non_goog_val,
            "wfb_accounts_value": wfb_accounts_val,
            "pct_portfolio": non_goog_pct,
            "annual_appreciation_pct": non_goog_cagr_pct,
            "tooltip": non_goog_tip,
        },
    }


def _build_single_symbol_grant_analytics(
    state: dict[str, Any],
    price_book: Any,
    cfg: dict[str, Any],
    today_iso: str,
    symbol: str,
    live_price: float,
    net_rate: float,
    prev_close: float = 0.0,
) -> dict[str, Any]:
    """
    Builds the complete grant model, release timeline, calendar-year summary, and per-grant
    schedule metadata for a single equity ticker (`symbol`).
    """
    # PORTABILITY DO: Resolve `award_type`, `institution`, and `net_retention_rate` from ingested grant rows
    # or `project.json -> grant_forecast` (`LL-01`, `LL-03`, `LL-06`, `LL-22`).
    # PORTABILITY DON'T: Default to a specific employer's award acronym, stock-plan custodian, or withholding fraction.
    sym_up = (symbol or "").strip().upper()
    hist_releases = [
        r for r in state.get("gsu_releases_history", [])
        if r["vest_date"] <= today_iso and (r.get("symbol") or sym_up).strip().upper() == sym_up
    ]
    all_sym_tranches = [
        t for t in state.get("gsu_tranches", [])
        if (t.get("symbol") or sym_up).strip().upper() == sym_up
    ]
    future_tranches = [t for t in all_sym_tranches if t["vest_date"] > today_iso]

    company_name = next(
        (str(t.get("company_name") or "").strip() for t in all_sym_tranches if (t.get("company_name") or "").strip()),
        "",
    ) or next(
        (str(r.get("company_name") or "").strip() for r in hist_releases if (r.get("company_name") or "").strip()),
        "",
    )
    short_name = _derive_short_company_name(sym_up, company_name, cfg)
    award_type = next(
        (str(t.get("award_type") or "").strip() for t in all_sym_tranches if (t.get("award_type") or "").strip()),
        "",
    ) or next(
        (str(r.get("award_type") or "").strip() for r in hist_releases if (r.get("award_type") or "").strip()),
        str(((cfg or {}).get("grant_forecast") or (cfg or {}).get("gsu_forecast") or {}).get("award_type") or "RSU").strip(),
    )
    account_id = next(
        (str(t.get("account_id") or "").strip() for t in all_sym_tranches if (t.get("account_id") or "").strip()),
        "",
    ) or next(
        (str(r.get("account_id") or "").strip() for r in hist_releases if (r.get("account_id") or "").strip()),
        "",
    )
    institution = next(
        (str(t.get("institution") or "").strip() for t in all_sym_tranches if (t.get("institution") or "").strip()),
        "",
    ) or next(
        (str(r.get("institution") or "").strip() for r in hist_releases if (r.get("institution") or "").strip()),
        str((state.get("accounts", {}).get(account_id) or {}).get("institution") or "Stock Plan").strip(),
    )
    report_date = max(
        (str(t.get("report_date") or "") for t in all_sym_tranches),
        default=state.get("gsu_report_date"),
    )

    # 1. Summarize active grants from `gsu_tranches`
    active_by_grant: dict[str, dict[str, Any]] = {}
    for tr in future_tranches:
        gid = tr["grant_id"]
        ge = active_by_grant.setdefault(
            gid,
            {
                "grant_id": gid,
                "grant_date": tr["grant_date"],
                "first_vest": tr["vest_date"],
                "last_vest": tr["vest_date"],
                "tranches": 0,
                "orig_qty": 0.0,
                "deus": 0.0,
                "total_qty": 0.0,
            },
        )
        ge["tranches"] += 1
        ge["last_vest"] = tr["vest_date"]
        ge["orig_qty"] = round(ge["orig_qty"] + float(tr["orig_qty"]), 3)
        ge["deus"] = round(ge["deus"] + float(tr["deus"]), 6)
        ge["total_qty"] = round(ge["total_qty"] + float(tr["total_qty"]), 6)

    # Summarize historical releases per grant_id
    hist_by_grant: dict[str, dict[str, Any]] = {}
    for r in hist_releases:
        _rgid = str(r.get("grant_id") or "").strip()
        gid = "PRIOR-GRANT" if (not _rgid or _rgid.upper().startswith("PRIOR-")) else _rgid
        hg = hist_by_grant.setdefault(
            gid,
            {
                "vested_tranches": 0,
                "first_hist_vest": r["vest_date"],
                "last_hist_vest": r["vest_date"],
                "vested_gross_shares": 0.0,
                "vested_net_shares": 0.0,
                "vested_rel_gross_val": 0.0,
                "vested_rel_net_val": 0.0,
            },
        )
        hg["vested_tranches"] += 1
        hg["last_hist_vest"] = r["vest_date"]
        g_sh = float(r["gross_shares"])
        n_sh = float(r["net_shares"])
        r_px = float(r["release_price"])
        hg["vested_gross_shares"] = round(hg["vested_gross_shares"] + g_sh, 6)
        hg["vested_net_shares"] = round(hg["vested_net_shares"] + n_sh, 6)
        hg["vested_rel_gross_val"] = round(hg["vested_rel_gross_val"] + g_sh * r_px, 2)
        hg["vested_rel_net_val"] = round(hg["vested_rel_net_val"] + n_sh * r_px, 2)

    # Sort active grants ascending by `last_vest` so earliest grant to finish vesting is first
    sorted_active_grants = sorted(
        active_by_grant.values(),
        key=lambda g: (g["last_vest"], g["grant_date"], g["grant_id"]),
    )

    grant_notes = ((cfg or {}).get("grant_forecast") or (cfg or {}).get("gsu_forecast") or {}).get("grant_notes") or {}
    grant_series_meta: list[dict[str, Any]] = []
    if "PRIOR-GRANT" in hist_by_grant:
        hp = hist_by_grant["PRIOR-GRANT"]
        last_v_lbl = date.fromisoformat(hp["last_hist_vest"]).strftime("%b '%y") if hp.get("last_hist_vest") else ""
        first_v_yr = int(str(hp.get("first_hist_vest") or "2024")[:4])
        prior_lbl = str(grant_notes.get("PRIOR-GRANT") or (f"Prior Grant (Ended {last_v_lbl})" if last_v_lbl else "Prior Grant"))
        prior_gdate = f"Pre-{first_v_yr}" if first_v_yr else "Prior"
        grant_series_meta.append(
            {
                "id": "PRIOR-GRANT",
                "label": prior_lbl,
                "color": PRIOR_GRANT_COLOR,
                "status": "RETIRED",
                "grant_date": prior_gdate,
                "grant_price": None,
                "first_vest_ever": hp["first_hist_vest"],
                "last_vest": hp["last_hist_vest"],
                "vested_tranches": hp["vested_tranches"],
                "rem_tranches": 0,
                "vested_gross_shares": round(hp["vested_gross_shares"], 3),
                "vested_net_shares": round(hp["vested_net_shares"], 3),
                "rem_gross_shares": 0.0,
                "rem_est_net_shares": 0.0,
            }
        )

    enriched_grants: list[dict[str, Any]] = []
    for idx, ge in enumerate(sorted_active_grants):
        gid = ge["grant_id"]
        col = GRANT_PALETTE[idx % len(GRANT_PALETTE)]
        g_px = price_book.price_on_or_before(sym_up, ge["grant_date"])
        hg = hist_by_grant.get(gid, {})
        v_tr = int(hg.get("vested_tranches", 0))
        v_gross = round(float(hg.get("vested_gross_shares", 0.0)), 3)
        v_net = round(float(hg.get("vested_net_shares", 0.0)), 3)
        first_ever = hg.get("first_hist_vest") or ge["first_vest"]
        tot_tr = v_tr + ge["tranches"]
        tot_award_gross = round(v_gross + ge["total_qty"], 3)
        end_short = date.fromisoformat(ge["last_vest"]).strftime("%b '%y")

        cadence_note = f"{tot_tr} scheduled releases ({v_tr} vested + {ge['tranches']} remaining)"
        if gid in grant_notes:
            cadence_note += f"; {grant_notes[gid]}"

        rec = {
            **ge,
            "color": col,
            "label": f"{gid} (Ends {end_short})",
            "grant_price": round(float(g_px), 2) if g_px is not None else None,
            "first_vest_ever": first_ever,
            "vested_tranches": v_tr,
            "total_tranches": tot_tr,
            "vested_gross_shares": v_gross,
            "vested_net_shares": v_net,
            "total_award_shares": tot_award_gross,
            "est_net_qty": round(ge["total_qty"] * net_rate, 3),
            "gross_val": round(ge["total_qty"] * live_price, 2),
            "net_val": round(ge["total_qty"] * net_rate * live_price, 2),
            "cadence_note": cadence_note,
        }
        enriched_grants.append(rec)
        grant_series_meta.append(
            {
                "id": gid,
                "label": f"{gid} (Ends {end_short})",
                "color": col,
                "status": "ACTIVE",
                "grant_date": ge["grant_date"],
                "grant_price": rec["grant_price"],
                "first_vest_ever": first_ever,
                "last_vest": ge["last_vest"],
                "vested_tranches": v_tr,
                "rem_tranches": ge["tranches"],
                "vested_gross_shares": v_gross,
                "vested_net_shares": v_net,
                "rem_gross_shares": round(ge["total_qty"], 3),
                "rem_est_net_shares": rec["est_net_qty"],
            }
        )

    # 2. Build monthly `release_bars` (Observed <= today_iso + Future Estimated > today_iso)
    bars_by_date: dict[str, dict[str, Any]] = {}

    for r in hist_releases:
        vdt = r["vest_date"]
        dt = date.fromisoformat(vdt)
        _rgid = str(r.get("grant_id") or "").strip()
        gid = "PRIOR-GRANT" if (not _rgid or _rgid.upper().startswith("PRIOR-")) else _rgid
        g_sh = float(r["gross_shares"])
        n_sh = float(r["net_shares"])
        r_px = float(r["release_price"])
        rel_net_v = round(n_sh * r_px, 2)
        rel_gross_v = round(g_sh * r_px, 2)
        tdy_net_v = round(n_sh * live_price, 2)
        tdy_gross_v = round(g_sh * live_price, 2)

        b = bars_by_date.setdefault(
            vdt,
            {
                "iso": vdt,
                "shortLbl": dt.strftime("%b %d '%y"),
                "monthLbl": dt.strftime("%b '%y"),
                "yearLbl": str(dt.year),
                "kind": "OBSERVED",
                "releasePrice": round(r_px, 2),
                "trancheCount": 0,
                "totGrossShares": 0.0,
                "totNetShares": 0.0,
                "totRelNetVal": 0.0,
                "totRelGrossVal": 0.0,
                "totTodayNetVal": 0.0,
                "totTodayGrossVal": 0.0,
                "byGrant": {},
            },
        )
        b["trancheCount"] += 1
        b["totGrossShares"] = round(b["totGrossShares"] + g_sh, 6)
        b["totNetShares"] = round(b["totNetShares"] + n_sh, 6)
        b["totRelNetVal"] = round(b["totRelNetVal"] + rel_net_v, 2)
        b["totRelGrossVal"] = round(b["totRelGrossVal"] + rel_gross_v, 2)
        b["totTodayNetVal"] = round(b["totTodayNetVal"] + tdy_net_v, 2)
        b["totTodayGrossVal"] = round(b["totTodayGrossVal"] + tdy_gross_v, 2)

        gb = b["byGrant"].setdefault(
            gid,
            {"gross_sh": 0.0, "net_sh": 0.0, "val_rel_net": 0.0, "val_today_net": 0.0, "val_rel_gross": 0.0, "val_today_gross": 0.0},
        )
        gb["gross_sh"] = round(gb["gross_sh"] + g_sh, 6)
        gb["net_sh"] = round(gb["net_sh"] + n_sh, 6)
        gb["val_rel_net"] = round(gb["val_rel_net"] + rel_net_v, 2)
        gb["val_today_net"] = round(gb["val_today_net"] + tdy_net_v, 2)
        gb["val_rel_gross"] = round(gb["val_rel_gross"] + rel_gross_v, 2)
        gb["val_today_gross"] = round(gb["val_today_gross"] + tdy_gross_v, 2)

    for tr in future_tranches:
        vdt = tr["vest_date"]
        dt = date.fromisoformat(vdt)
        gid = tr["grant_id"]
        g_sh = float(tr["total_qty"])
        n_sh = round(g_sh * net_rate, 6)
        base_net_v = round(n_sh * live_price, 2)
        base_gross_v = round(g_sh * live_price, 2)

        b = bars_by_date.setdefault(
            vdt,
            {
                "iso": vdt,
                "shortLbl": dt.strftime("%b %d '%y"),
                "monthLbl": dt.strftime("%b '%y"),
                "yearLbl": str(dt.year),
                "kind": "ESTIMATED",
                "releasePrice": round(live_price, 2),
                "trancheCount": 0,
                "totGrossShares": 0.0,
                "totNetShares": 0.0,
                "totRelNetVal": 0.0,
                "totRelGrossVal": 0.0,
                "totTodayNetVal": 0.0,
                "totTodayGrossVal": 0.0,
                "byGrant": {},
            },
        )
        b["trancheCount"] += 1
        b["totGrossShares"] = round(b["totGrossShares"] + g_sh, 6)
        b["totNetShares"] = round(b["totNetShares"] + n_sh, 6)
        b["totRelNetVal"] = round(b["totRelNetVal"] + base_net_v, 2)
        b["totRelGrossVal"] = round(b["totRelGrossVal"] + base_gross_v, 2)
        b["totTodayNetVal"] = round(b["totTodayNetVal"] + base_net_v, 2)
        b["totTodayGrossVal"] = round(b["totTodayGrossVal"] + base_gross_v, 2)

        gb = b["byGrant"].setdefault(
            gid,
            {"gross_sh": 0.0, "net_sh": 0.0, "val_rel_net": 0.0, "val_today_net": 0.0, "val_rel_gross": 0.0, "val_today_gross": 0.0},
        )
        gb["gross_sh"] = round(gb["gross_sh"] + g_sh, 6)
        gb["net_sh"] = round(gb["net_sh"] + n_sh, 6)
        gb["val_rel_net"] = round(gb["val_rel_net"] + base_net_v, 2)
        gb["val_today_net"] = round(gb["val_today_net"] + base_net_v, 2)
        gb["val_rel_gross"] = round(gb["val_rel_gross"] + base_gross_v, 2)
        gb["val_today_gross"] = round(gb["val_today_gross"] + base_gross_v, 2)

    release_bars = [bars_by_date[k] for k in sorted(bars_by_date.keys())]

    # 3. Build Calendar Year summary rows directly from observed + scheduled bars
    bars_by_year: dict[str, list[dict[str, Any]]] = {}
    for b in release_bars:
        bars_by_year.setdefault(b["yearLbl"], []).append(b)

    gsu_year_rows: list[dict[str, Any]] = []
    for yr in sorted(bars_by_year.keys()):
        ybars = bars_by_year[yr]
        obs_bars = [b for b in ybars if b["kind"] == "OBSERVED"]
        est_bars = [b for b in ybars if b["kind"] == "ESTIMATED"]

        obs_tr = sum(b["trancheCount"] for b in obs_bars)
        est_tr = sum(b["trancheCount"] for b in est_bars)
        tot_tr = obs_tr + est_tr

        obs_gross_sh = sum(b["totGrossShares"] for b in obs_bars)
        est_gross_sh = sum(b["totGrossShares"] for b in est_bars)
        tot_gross_sh = round(obs_gross_sh + est_gross_sh, 3)

        obs_net_sh = sum(b["totNetShares"] for b in obs_bars)
        est_net_sh = sum(b["totNetShares"] for b in est_bars)
        tot_net_sh = round(obs_net_sh + est_net_sh, 3)

        rel_gross_val = round(sum(b["totRelGrossVal"] for b in ybars), 2)
        rel_net_val = round(sum(b["totRelNetVal"] for b in ybars), 2)
        tdy_gross_val = round(sum(b["totTodayGrossVal"] for b in ybars), 2)
        tdy_net_val = round(sum(b["totTodayNetVal"] for b in ybars), 2)

        if obs_bars and not est_bars:
            status = "OBSERVED_HIST"
            badge = "Observed"
            subnote = f"{len(obs_bars)} release dates ({obs_bars[0]['iso'][5:]} → {obs_bars[-1]['iso'][5:]})"
            tip = (
                f"Complete {institution} release history for {yr}: {obs_tr} grant releases across {len(obs_bars)} dates "
                f"({tot_gross_sh:,.3f} gross sh → {tot_net_sh:,.3f} actual net sh after tax withholding). "
                f"Value @ Release FMV: ${rel_net_val:,.0f} net (${rel_gross_val:,.0f} gross) • "
                f"Value @ Today (${live_price:,.2f}/sh): ${tdy_net_val:,.0f} net (${tdy_gross_val:,.0f} gross)."
            )
        elif obs_bars and est_bars:
            status = "CURRENT_SPLIT"
            badge = f"{est_gross_sh:,.0f} sh rem."
            subnote = f"{obs_tr} vested YTD + {est_tr} scheduled ({est_gross_sh:,.1f} sh)"
            tip = (
                f"{yr} Full-Year Releases: {obs_tr} completed releases YTD ({obs_gross_sh:,.3f} gross / {obs_net_sh:,.3f} actual net sh) "
                f"+ {est_tr} scheduled releases remaining ({est_gross_sh:,.3f} gross / {est_net_sh:,.3f} est. net sh)."
            )
        else:
            status = "SCHEDULED"
            badge = "Scheduled"
            subnote = f"{len(est_bars)} vest dates"
            tip = (
                f"Scheduled unvested releases in {yr}: {est_tr} grant tranches across {len(est_bars)} dates "
                f"({tot_gross_sh:,.3f} gross sh / {tot_net_sh:,.3f} est. net sh at {net_rate * 100:.1f}% retention)."
            )

        gsu_year_rows.append(
            {
                "year": yr,
                "status": status,
                "badge": badge,
                "subnote": subnote,
                "tooltip": tip,
                "tranches": tot_tr,
                "obs_tranches": obs_tr,
                "est_tranches": est_tr,
                "shares": tot_gross_sh,
                "unvested_shares": round(est_gross_sh, 3),
                "net_shares": tot_net_sh,
                "gross_val": rel_gross_val,
                "net_val": rel_net_val,
                "today_gross_val": tdy_gross_val,
                "today_net_val": tdy_net_val,
            }
        )

    retired_summary = None
    if "PRIOR-GRANT" in hist_by_grant:
        hp = hist_by_grant["PRIOR-GRANT"]
        retired_summary = {
            "grant_id": "PRIOR-GRANT",
            "first_vest": hp["first_hist_vest"],
            "last_vest": hp["last_hist_vest"],
            "tranches": hp["vested_tranches"],
            "gross_shares": round(hp["vested_gross_shares"], 3),
            "net_shares": round(hp["vested_net_shares"], 3),
            "rel_gross_val": round(hp["vested_rel_gross_val"], 2),
            "rel_net_val": round(hp["vested_rel_net_val"], 2),
        }

    fut_gross_sh = round(sum(float(t["total_qty"]) for t in future_tranches), 3)
    fut_net_sh = round(sum(float(t["total_qty"]) * net_rate for t in future_tranches), 3)
    vested_gross_sh = round(sum(float(r["gross_shares"]) for r in hist_releases), 3)
    vested_net_sh = round(sum(float(r["net_shares"]) for r in hist_releases), 3)

    return {
        "symbol": sym_up,
        "company_name": company_name or sym_up,
        "short_name": short_name,
        "award_type": award_type,
        "institution": institution,
        "account_id": account_id,
        "report_date": report_date,
        "live_price": round(live_price, 2),
        "prev_close": round(prev_close, 2) if prev_close > 0 else round(live_price, 2),
        "net_retention_rate": net_rate,
        "has_unvested": len(future_tranches) > 0 and fut_gross_sh > 0,
        "future_tranche_count": len(future_tranches),
        "future_gross_shares": fut_gross_sh,
        "future_est_net_shares": fut_net_sh,
        "future_gross_val": round(fut_gross_sh * live_price, 2),
        "future_net_val": round(fut_net_sh * live_price, 2),
        "vested_tranche_count": len(hist_releases),
        "vested_gross_shares": vested_gross_sh,
        "vested_net_shares": vested_net_sh,
        "vested_rel_gross_val_today": round(vested_gross_sh * live_price, 2),
        "vested_rel_val_today": round(vested_net_sh * live_price, 2),
        "grant_series": grant_series_meta,
        "release_bars": release_bars,
        "by_year": gsu_year_rows,
        "by_grant": enriched_grants,
        "retired_grant_summary": retired_summary,
    }


def _build_stock_grants_analytics(
    state: dict[str, Any],
    price_book: Any,
    cfg: dict[str, Any],
    today_date: date,
    today_iso: str,
    goog_live: float = 0.0,
    cur_items: list[dict[str, Any]] | None = None,
    total_now: float = 0.0,
) -> dict[str, Any]:
    """
    Aggregates per-symbol stock grant analytics across all companies present in SQLite,
    and constructs the navigation summary (`has_unvested`, `default_symbol`, `companies`)
    strictly filtered to companies with future unvested shares > 0 (`D-01`).
    """
    # PORTABILITY DO: Keep `primary_sym` empty (`""`) when `primary_equity_symbol` is unset, and resolve
    # stock-plan accounts, transfer dates, basis, and tax labels from `project.json` or statement data (`LL-01`, `LL-03`, `LL-09`, `LL-10`, `LL-12`, `LL-15`).
    # PORTABILITY DON'T: Fall back to a hardcoded ticker, custodian name, transfer date, or dollar basis literal.
    primary_sym = str(cfg.get("primary_equity_symbol") or "").strip().upper()
    all_syms = sorted(
        s
        for s in {
            *((t.get("symbol") or primary_sym).strip().upper() for t in state.get("gsu_tranches", [])),
            *((r.get("symbol") or primary_sym).strip().upper() for r in state.get("gsu_releases_history", [])),
        }
        if s
    )

    by_symbol: dict[str, dict[str, Any]] = {}
    for sym in all_syms:
        px_pair = price_book.price_with_date_on_or_before(sym, today_iso)
        sym_live = float(px_pair[1]) if px_pair else (goog_live if sym == primary_sym else 0.0)
        sym_prev = 0.0
        if px_pair:
            prev_iso = (date.fromisoformat(px_pair[0]) - timedelta(days=1)).isoformat()
            sym_prev = float(price_book.price_on_or_before(sym, prev_iso) or 0.0)
        if sym_live <= 0:
            sym_hist = [
                float(r["release_price"])
                for r in state.get("gsu_releases_history", [])
                if (r.get("symbol") or primary_sym).strip().upper() == sym and float(r.get("release_price") or 0.0) > 0
            ]
            if sym_hist:
                sym_live = sym_hist[-1]
        sym_net_rate = _compute_gsu_net_retention_rate(state, today_date, cfg, symbol=sym)
        sym_analytics = _build_single_symbol_grant_analytics(
            state=state,
            price_book=price_book,
            cfg=cfg,
            today_iso=today_iso,
            symbol=sym,
            live_price=sym_live,
            net_rate=sym_net_rate,
            prev_close=sym_prev,
        )

        # Available shares in portfolio holdings for this symbol
        avail_sh = 0.0
        avail_val = 0.0
        avail_acct = sym_analytics.get("account_id") or ""
        avail_inst = sym_analytics.get("institution") or ""
        if cur_items:
            matching = [
                it for it in cur_items
                if (it.get("symbol_or_code") or "").strip().upper() == sym
                or (sym in ("GOOG", "GOOGL") and (it.get("symbol_or_code") or "").strip().upper() in ("GOOG", "GOOGL"))
            ]
            aid_matches = [it for it in matching if avail_acct and it.get("account_id") == avail_acct]
            chosen = aid_matches if aid_matches else matching
            if chosen:
                avail_sh = sum(float(it.get("quantity") or 0.0) for it in chosen)
                avail_val = sum(float(it.get("market_value") or 0.0) for it in chosen)
                if chosen[0].get("account_id"):
                    avail_acct = chosen[0]["account_id"]
                    inst_from_acc = (state.get("accounts", {}).get(avail_acct) or {}).get("institution")
                    if inst_from_acc:
                        avail_inst = inst_from_acc

        avail_pct = round(avail_val / total_now * 100.0, 1) if total_now > 0 else 0.0
        sym_analytics["available_shares"] = round(avail_sh, 3)
        sym_analytics["available_val"] = round(avail_val, 2)
        sym_analytics["available_pct"] = avail_pct
        sym_analytics["available_account_id"] = avail_acct
        sym_analytics["available_institution"] = avail_inst
        by_symbol[sym] = sym_analytics

    unvested_list = [
        by_symbol[s] for s in all_syms if by_symbol[s].get("has_unvested")
    ]
    unvested_list.sort(
        key=lambda c: (
            0 if c["symbol"] == primary_sym else 1,
            -float(c.get("future_gross_val") or 0.0),
            c["symbol"],
        )
    )
    companies = [
        {
            "symbol": c["symbol"],
            "company_name": c["company_name"],
            "short_name": c["short_name"],
            "award_type": c["award_type"],
            "institution": c.get("available_institution") or c["institution"],
            "account_id": c.get("available_account_id") or c["account_id"],
            "live_price": c["live_price"],
            "prev_close": c["prev_close"],
            "net_retention_rate": c["net_retention_rate"],
            "future_tranche_count": c["future_tranche_count"],
            "future_gross_shares": c["future_gross_shares"],
            "future_est_net_shares": c["future_est_net_shares"],
            "future_gross_val": c["future_gross_val"],
            "future_net_val": c["future_net_val"],
            "vested_gross_shares": c["vested_gross_shares"],
            "vested_net_shares": c["vested_net_shares"],
            "vested_to_date_net_val": c.get("vested_rel_val_today", round(c["vested_net_shares"] * c["live_price"], 2)),
            "vested_to_date_gross_val": c.get("vested_rel_gross_val_today", round(c["vested_gross_shares"] * c["live_price"], 2)),
            "available_shares": c.get("available_shares", 0.0),
            "available_val": c.get("available_val", 0.0),
            "available_pct": c.get("available_pct", 0.0),
        }
        for c in unvested_list
    ]
    return {
        "has_unvested": len(companies) > 0,
        "default_symbol": companies[0]["symbol"] if companies else None,
        "companies": companies,
        "by_symbol": by_symbol,
    }


def _build_google_stock_analytics(
    state: dict[str, Any],
    price_book: Any,
    cfg: dict[str, Any],
    today_iso: str,
    goog_live: float,
    net_rate: float,
) -> dict[str, Any]:
    """Backward-compatible wrapper returning single-symbol analytics for `primary_equity_symbol`."""
    # PORTABILITY DO: Keep `primary_sym` empty (`""`) when `primary_equity_symbol` is unset, and resolve
    # stock-plan accounts, transfer dates, basis, and tax labels from `project.json` or statement data (`LL-01`, `LL-03`, `LL-09`, `LL-10`, `LL-12`, `LL-15`).
    # PORTABILITY DON'T: Fall back to a hardcoded ticker, custodian name, transfer date, or dollar basis literal.
    primary_sym = str(cfg.get("primary_equity_symbol") or "").strip().upper()
    return _build_single_symbol_grant_analytics(
        state=state,
        price_book=price_book,
        cfg=cfg,
        today_iso=today_iso,
        symbol=primary_sym,
        live_price=goog_live,
        net_rate=net_rate,
    )


def _child_from_item(
    x: dict[str, Any],
    inst_short: str,
    holding_prefs: dict[str, dict[str, Any]] | None = None,
    accounts: dict[str, dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    link = None
    if x["is_scrapeable"] and x["quote_symbol"]:
        link = f"https://finance.yahoo.com/quote/{x['quote_symbol']}"
    elif x["asset_class"] == "MUNI_BOND":
        link = f"https://emma.msrb.org/Security/Details/{x['symbol']}"

    sub_parts = []
    if x["asset_class"] == "MUNI_BOND" and x["meta"].get("call_info"):
        sub_parts.append(f"Call {x['meta']['call_info']}")
    if x.get("is_scrapeable"):
        sub_parts.append(f"Live quote: ${x['current_price']:,.2f}/sh")

    c_key = f"{x['account_id']}:{x['security_id']}"
    pref = (holding_prefs or {}).get(c_key) or (holding_prefs or {}).get(x["security_id"]) or (holding_prefs or {}).get(x["symbol"]) or {}
    c_fn = pref.get("friendly_name") or ""
    stmt_name = x["name"]
    disp_title = c_fn if c_fn else stmt_name
    c_fee = pref.get("fee_pct")
    c_comment = pref.get("comment") or pref.get("notes") or ""

    return {
        "title": disp_title,
        "statement_name": stmt_name,
        "friendly_name": c_fn,
        "fee_pct": c_fee,
        "comment": c_comment,
        "holding_key": c_key,
        "account_id": x["account_id"],
        "security_id": x["security_id"],
        "institution": inst_short,
        "asset_class": x.get("asset_class"),
        "investment_type": format_investment_type(pref.get("investment_type") or x.get("asset_class")),
        "maturity_date": x.get("maturity_date"),
        "call_info": x.get("meta", {}).get("call_info"),
        "asof_date": x.get("stmt_date"),
        "subtitle": " • ".join(sub_parts),
        "maturity_str": x["maturity_str"],
        "symbol": x["symbol"],
        "link": link,
        "account_label": _friendly_account_label(x["account_id"], accounts, cfg) if x.get("account_id") else (f"{inst_short} • {x['account_id']}" if inst_short else x["account_id"]),
        "source_label": "Live Ticker" if x["is_scrapeable"] else f"Statement ({x['stmt_date']})",
        "source_tooltip": x["source_tooltip"],
        "is_scrapeable": x["is_scrapeable"],
        "quantity_str": (
            f"{x['quantity']:,.0f} sh"
            if x["is_scrapeable"] and abs(x["quantity"] - round(x["quantity"])) < 1e-6
            else (
                f"{x['quantity']:,.3f} sh"
                if x["is_scrapeable"]
                else (f"${x['quantity']:,.0f} par" if x["asset_class"] == "MUNI_BOND" else "1 contract")
            )
        ),
        "stmt_price_str": (
            f"{x['stmt_price']:.3f}"
            if x["asset_class"] == "MUNI_BOND" and x["stmt_price"]
            else (f"${x['stmt_price']:,.2f}" if x["stmt_price"] else "—")
        ),
        "principal_val": x.get("principal_val"),
        "principal_date": x.get("principal_date"),
        "quantity": x.get("quantity"),
        "cagr_pct": x.get("cagr_pct"),
        "cagr_end_date": x["eval_date"] if x.get("is_scrapeable") else x.get("stmt_date"),
        "principal_str": x["principal_str"],
        "principal_sub": x["principal_sub"],
        "principal_tooltip": x["principal_tooltip"],
        "current_value": x["current_value"],
        "wow_change": x["wow_change"],
        "recent_change_val": x.get("recent_change_val"),
        "recent_change_sub": x.get("recent_change_sub", ""),
        "recent_change_tip": x.get("recent_change_tip", ""),
        "recent_change_sign": x.get("recent_change_sign", 0),
        "growth_str": x["growth_str"],
        "growth_sub": x["growth_sub"],
        "growth_sign": x["growth_sign"],
        "growth_tooltip": x["growth_tooltip"],
        "pct_portfolio": x["pct_portfolio"],
        "pct_tooltip": x["pct_tooltip"],
    }


_PLAN_SUBACCOUNT_SUFFIX_RE = re.compile(
    r"^(.+?)[-_](?:PRETAX|PRE[-_]?TAX|ROTH|AFTERTAX|AFTER[-_]?TAX|TRAD|MATCH)$",
    re.IGNORECASE,
)


def _tax_sleeve_short_label(tax_status: str) -> str:
    ts = str(tax_status or "").strip().upper()
    mapping = {
        "401K": "Pre-Tax 401(k)",
        "TRAD_401K": "Pre-Tax 401(k)",
        "ROTH_401K": "Roth 401(k)",
        "403B": "Pre-Tax 403(b)",
        "TRAD_IRA": "Traditional IRA",
        "ROTH_IRA": "Roth IRA",
        "HSA": "HSA",
        "TAXABLE": "Taxable",
    }
    return mapping.get(ts, ts.replace("_", " ").title() or "Sub-Account")


def _resolve_parent_plan_id(
    aid: str | None,
    accounts: dict[str, dict[str, Any]],
    cfg: dict[str, Any] | None = None,
) -> str | None:
    """
    Identifies the shared parent plan ID when an employer retirement plan is split across
    multiple tax-wrapper sub-accounts (e.g. `<PLAN>-PRETAX` + `<PLAN>-ROTH`), either via
    `accounts.parent_account_id` or matching `<BASE>-PRETAX`/`<BASE>-ROTH` sibling accounts.
    """
    if not aid:
        return None
    acc = accounts.get(aid) or {}
    cfg_accts = (cfg or {}).get("accounts") or {}
    c_acc = cfg_accts.get(aid) or {}
    explicit = str(acc.get("parent_account_id") or c_acc.get("parent_account_id") or "").strip()
    if explicit:
        return explicit
    m = _PLAN_SUBACCOUNT_SUFFIX_RE.match(str(aid).strip())
    if not m:
        return None
    base_id = m.group(1).strip()
    if not base_id:
        return None
    inst = str(acc.get("institution") or c_acc.get("institution") or "").strip().lower()
    siblings = [
        other_id
        for other_id, other_acc in accounts.items()
        if other_id != aid
        and (
            str(other_acc.get("parent_account_id") or "").strip() == base_id
            or (
                _PLAN_SUBACCOUNT_SUFFIX_RE.match(str(other_id))
                and _PLAN_SUBACCOUNT_SUFFIX_RE.match(str(other_id)).group(1).strip() == base_id
                and (not inst or str(other_acc.get("institution") or "").strip().lower() == inst)
            )
        )
    ]
    return base_id if siblings else None


def _derive_parent_plan_account_label(
    plan_id: str,
    plan_items: list[dict[str, Any]],
    accounts: dict[str, dict[str, Any]],
    cfg: dict[str, Any] | None,
) -> str:
    first_aid = str(plan_items[0].get("account_id") or "") if plan_items else ""
    first_lbl = _friendly_account_label(first_aid, accounts, cfg) if first_aid else ""
    cleaned = re.sub(
        r"\s*(?:Pre-Tax(?:\s*&\s*Match)?|Roth(?:\s*401\(k\))?(?:\s*&\s*(?:In-Plan\s+)?Conversions)?|After-Tax)\s*",
        " ",
        first_lbl,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(rf"\({re.escape(first_aid)}\)", f"({plan_id})", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" •-")
    if cleaned:
        return cleaned
    inst = (accounts.get(first_aid) or {}).get("institution") or "Retirement Plan"
    return f"{inst} • Plan ({plan_id})"


def _build_strategy_rollups(
    items: list[dict[str, Any]],
    accounts: dict[str, dict[str, Any]],
    total_now: float,
    today_iso: str,
    overrides: dict[str, Any],
    holding_prefs: dict[str, dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
    prior_iso: str = "",
) -> list[dict[str, Any]]:
    """
    Groups multi-constituent baskets (UMA multi-stock baskets, Municipal Bond SMAs, Cash Sweeps)
    into clean collapsible parent rows based on FI parenting & account categorization while keeping
    major standalone positions & annuities directly visible.
    """
    # PORTABILITY DO: Derive sleeve titles, constituent counts, municipal bond descriptions, and annuity
    # surrender-free dates from ingested statement metadata and `project.json -> accounts` (`LL-02`, `LL-05`, `LL-06`, `LL-13`, `LL-17`, `LL-19`).
    # PORTABILITY DON'T: Hardcode advisory program names, state codes, or carrier-specific maturity dates.
    # Identify multi-stock managed equity accounts dynamically (>5 equity holdings in an account or managed sleeve account_type)
    eq_by_acct: dict[str, list[dict[str, Any]]] = {}
    for x in items:
        if x["asset_class"] == "EQUITY":
            eq_by_acct.setdefault(x["account_id"], []).append(x)
    uma_acct_ids = {
        aid
        for aid, eqs in eq_by_acct.items()
        if "UMA" in ((accounts.get(aid) or {}).get("account_type") or "").upper()
        or (
            len(eqs) > 5
            and str((accounts.get(aid) or {}).get("tax_status") or "TAXABLE").upper() == "TAXABLE"
        )
    }
    uma_items = [x for x in items if x["account_id"] in uma_acct_ids and x["asset_class"] == "EQUITY"]
    muni_items = [x for x in items if x["asset_class"] == "MUNI_BOND"]
    cash_items = [x for x in items if x["asset_class"] == "CASH"]
    standalone_raw = [
        x for x in items
        if x not in uma_items and x not in muni_items and x not in cash_items
    ]

    # Group multi-source employer retirement plan holdings that share `(parent_plan_id, security_id)`
    # (e.g. a 401(k)/403(b) fund held across `<PLAN>-PRETAX` and `<PLAN>-ROTH` sleeves) into a single
    # collapsible Screen 1 parent row while preserving separate tax-wrapped Simulator buckets underneath.
    plan_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for x in standalone_raw:
        if x["asset_class"].endswith("ANNUITY"):
            continue
        p_plan = _resolve_parent_plan_id(x.get("account_id"), accounts, cfg)
        if p_plan and x.get("security_id"):
            plan_candidates.setdefault((p_plan, str(x["security_id"])), []).append(x)

    plan_grouped_pairs = {k: v for k, v in plan_candidates.items() if len(v) >= 2}
    plan_grouped_item_ids = {id(it) for grp_items in plan_grouped_pairs.values() for it in grp_items}
    standalone = [x for x in standalone_raw if id(x) not in plan_grouped_item_ids]

    groups: list[dict[str, Any]] = []

    for (plan_id, sec_id), plan_items in plan_grouped_pairs.items():
        first_it = plan_items[0]
        first_acc = accounts.get(first_it["account_id"], {})
        inst = first_acc.get("institution") or "Retirement Plan"
        inst_short = inst.replace(" Advisors", "")
        raw_sym = str(first_it.get("symbol") or "").strip()
        plan_slug = re.sub(r"[^A-Za-z0-9]+", "_", plan_id).strip("_") or "plan"
        sym_slug = re.sub(r"[^A-Za-z0-9]+", "_", raw_sym.lower()).strip("_") or "fund"
        plan_gid = f"rollup_plan_{plan_slug}_{sym_slug}"

        plan_val = round(sum(float(x["current_value"]) for x in plan_items), 2)
        plan_wow = round(sum(float(x["wow_change"]) for x in plan_items), 2)
        plan_pct = round(plan_val / total_now * 100.0, 2) if total_now else 0.0
        plan_stmt_val = round(sum(float(x["stmt_value"]) for x in plan_items), 2)
        plan_qty = sum(float(x["quantity"]) for x in plan_items)
        asof_dt = first_it["eval_date"] if first_it["is_scrapeable"] else first_it["stmt_date"]

        if all(x.get("principal_val") is not None for x in plan_items):
            p_tot = round(sum(float(x["principal_val"]) for x in plan_items), 2)
            gain_u = round(plan_val - p_tot, 2)
            gain_p = (gain_u / p_tot * 100.0) if p_tot > 0 else 0.0
            p_dates = [str(x["principal_date"]) for x in plan_items if x.get("principal_date")]
            p_dt = min(p_dates) if len(p_dates) == len(plan_items) else None
            p_str = f"${p_tot:,.2f}"
            p_sub = f"Acquired {p_dt}" if p_dt else f"{len(plan_items)} tax sources (net contributions)"
            p_tip = (
                f"Combined contributed principal / cost basis across {len(plan_items)} plan tax sources "
                f"({', '.join(_tax_sleeve_short_label((accounts.get(x['account_id']) or {}).get('tax_status') or '') for x in plan_items)}): "
                f"${p_tot:,.2f}."
            )
            if p_dt:
                cagr_v, yrs_v = solve_annualized_return([(p_dt, p_tot)], asof_dt or today_iso, plan_val)
            else:
                cagr_v, yrs_v = None, 0.0
            if cagr_v is not None:
                g_str = f"{'+' if cagr_v >= 0 else ''}{cagr_v:.2f}% / yr"
                g_sub = f"{'+' if gain_u >= 0 else '-'}${abs(gain_u):,.0f} ({'+' if gain_p >= 0 else ''}{gain_p:.1f}%)"
                g_sign = 1 if cagr_v >= 0 else -1
                g_tip = f"Annualized growth over {yrs_v:.2f} yrs since {p_dt}: ${p_tot:,.2f} → ${plan_val:,.2f}."
            else:
                cagr_v = None
                g_str = f"{'+' if gain_p >= 0 else ''}{gain_p:.1f}% total"
                g_sub = f"{'+' if gain_u >= 0 else '-'}${abs(gain_u):,.0f} (across {len(plan_items)} sources)"
                g_sign = 1 if gain_u > 0 else (-1 if gain_u < 0 else 0)
                g_tip = (
                    f"Combined investment gain across Pre-Tax & Roth plan sources vs. ${p_tot:,.2f} net contributions: "
                    f"{'+' if gain_u >= 0 else '-'}${abs(gain_u):,.2f} ({'+' if gain_p >= 0 else ''}{gain_p:.2f}%)."
                )
        else:
            p_tot, p_dt, cagr_v = None, None, None
            p_str, p_sub = "—", "Cost N/A in statement"
            p_tip = "Statement snapshot omits cumulative contribution basis."
            g_str, g_sub, g_sign = "—", "Inception N/A", 0
            g_tip = p_tip

        if first_it["is_scrapeable"] or plan_wow != 0.0:
            prev_p = plan_val - plan_wow
            rc_pct = (plan_wow / prev_p * 100.0) if prev_p > 0 else 0.0
            rc_val = plan_wow
            rc_sign = 1 if plan_wow > 0 else (-1 if plan_wow < 0 else 0)
            base_date_str = f" (vs {prior_iso})" if prior_iso else ""
            rc_sub = f"{'+' if rc_pct >= 0 else ''}{rc_pct:.1f}% • 1w{base_date_str}"
            rc_tip = _format_delta_tooltip(
                "1-Week Change",
                f"{'+' if plan_wow >= 0 else '-'}${abs(plan_wow):,.2f} ({rc_pct:+.2f}%)",
                f"Valuation as of {prior_iso or 'prior week'} across {len(plan_items)} plan tax sources.",
                f"{today_iso}.",
            )
        else:
            rc_val = 0.0
            rc_sign = 0
            date_str = f" (as of {asof_dt})" if asof_dt else ""
            rc_sub = f"Statement{date_str}"
            rc_tip = _format_delta_tooltip(
                "Statement Valuation",
                f"${plan_val:,.2f}",
                f"Combined valuation across {len(plan_items)} plan tax sources.",
                f"{asof_dt or today_iso}.",
            )

        plan_children: list[dict[str, Any]] = []
        for x in plan_items:
            ch = _child_from_item(x, inst_short, holding_prefs, accounts=accounts, cfg=cfg)
            ch_wrap = str((accounts.get(x["account_id"]) or {}).get("tax_status") or "")
            sleeve_lbl = _tax_sleeve_short_label(ch_wrap)
            if not ch.get("friendly_name"):
                ch["title"] = f"{sleeve_lbl} — {ch['statement_name']}"
            plan_children.append(ch)
        plan_children.sort(key=lambda c: (c["pct_portfolio"], c["current_value"]), reverse=True)

        pref = (holding_prefs or {}).get(plan_gid) or (holding_prefs or {}).get(sec_id) or {}
        fn = pref.get("friendly_name") or ""
        stmt_name = first_it["name"]
        fee_pct = pref.get("fee_pct")
        comment = pref.get("comment") or pref.get("notes") or ""
        link = f"https://finance.yahoo.com/quote/{first_it['quote_symbol']}" if first_it["is_scrapeable"] and first_it["quote_symbol"] else None
        sleeve_summary = " & ".join(
            dict.fromkeys(_tax_sleeve_short_label((accounts.get(x["account_id"]) or {}).get("tax_status") or "") for x in plan_items)
        )

        groups.append(
            {
                "group_id": plan_gid,
                "holding_key": plan_gid,
                "account_id": plan_id,
                "security_id": sec_id,
                "institution": inst,
                "asset_class": first_it.get("asset_class"),
                "investment_type": format_investment_type(pref.get("investment_type") or first_it.get("asset_class")),
                "maturity_date": first_it.get("maturity_date"),
                "surrender_val": None,
                "recent_vest_note": None,
                "asof_date": asof_dt,
                "statement_name": stmt_name,
                "friendly_name": fn,
                "fee_pct": fee_pct,
                "comment": comment,
                "title": fn if fn else stmt_name,
                "subtitle": f"As of {asof_dt} • Click to expand {sleeve_summary} sleeves",
                "maturity_str": first_it["maturity_str"],
                "symbol": raw_sym,
                "link": link,
                "account_label": _derive_parent_plan_account_label(plan_id, plan_items, accounts, cfg),
                "source_label": "Live Ticker" if first_it["is_scrapeable"] else f"Statement ({asof_dt})",
                "source_tooltip": (
                    f"Combined holding across {len(plan_items)} tax sources ({sleeve_summary}) in {inst} plan {plan_id}: "
                    f"${plan_val:,.2f} as of {asof_dt}."
                ),
                "is_scrapeable": first_it["is_scrapeable"],
                "quantity_str": f"{plan_qty:,.3f} sh" if first_it["is_scrapeable"] else f"{len(plan_items)} sources",
                "stmt_price_str": (
                    f"${first_it['stmt_price']:,.2f}"
                    if first_it["stmt_price"] and first_it["is_scrapeable"]
                    else f"${plan_stmt_val:,.0f}"
                ),
                "principal_val": p_tot,
                "principal_date": p_dt,
                "cagr_pct": cagr_v,
                "principal_str": p_str,
                "principal_sub": p_sub,
                "principal_tooltip": p_tip,
                "current_value": plan_val,
                "wow_change": plan_wow,
                "recent_change_val": rc_val,
                "recent_change_sub": rc_sub,
                "recent_change_tip": rc_tip,
                "recent_change_sign": rc_sign,
                "growth_str": g_str,
                "growth_sub": g_sub,
                "growth_sign": g_sign,
                "growth_tooltip": g_tip,
                "pct_portfolio": plan_pct,
                "pct_tooltip": (
                    f"{plan_pct:.2f}% of Current Vested Portfolio (${plan_val:,.2f} ÷ ${total_now:,.2f}). "
                    f"Excludes unvested future equity grant releases."
                ),
                "tax_rule_title": f"Multi-Source Plan ({sleeve_summary}) — Modeled Separately by Tax Wrapper",
                "tax_rule_detail": (
                    f"Combined {inst} plan ({plan_id}) holding spanning {sleeve_summary} sleeves. "
                    "In the Retirement Simulator, each tax source is modeled in its own tax-accurate bucket "
                    "(pre-tax ordinary income with RMDs vs. 0%-tax qualified Roth distributions) while inheriting "
                    "any growth/volatility forecast set on this parent holding."
                ),
                "children": plan_children,
            }
        )

    for st in standalone:
        acc = accounts.get(st["account_id"], {})
        inst_short = (acc.get("institution") or "Brokerage").replace(" Advisors", "")
        sub_segments = (st["meta"] or {}).get("sub_segments") or []
        children = []
        for seg in sub_segments:
            seg_val = round(float(seg["value"]), 2)
            seg_pct = round(seg_val / total_now * 100.0, 2) if total_now else 0.0
            unit_p = f"${seg_val / seg['units']:,.2f}/unit" if seg.get("units") else "—"
            s_name = re.sub(r"NASDAQ\s*100", "NASDAQ 100 ", str(seg["name"]))
            s_name = re.sub(r"S&P\s*500", "S&P 500 ", s_name)
            s_name = re.sub(r"Dual\s*Dir(?:ection)?", "Dual Direction ", s_name)
            s_name = re.sub(r"(\d+)\s*Yr", r"\1-Yr ", s_name)
            s_name = re.sub(r"\s*(-\d+%)\s*$", r" (\1 Buffer)", s_name)
            s_name = re.sub(r"\s+", " ", s_name).strip()
            children.append(
                {
                    "title": s_name,
                    "subtitle": f"Unit value: {unit_p}",
                    "maturity_str": (
                        f"Matures: {seg['maturity_date']}"
                        if seg.get("maturity_date")
                        else seg.get("term_note")
                    ),
                    "symbol": "",
                    "link": None,
                    "account_label": _friendly_account_label(st["account_id"], accounts, cfg) if st.get("account_id") else f"{inst_short} • {st['account_id']}",
                    "source_label": f"Statement Sub-Account ({st['stmt_date']})",
                    "source_tooltip": (
                        f"Structured RILA Sub-Segment ({seg['name']}): {seg['units']:,.3f} units valued at "
                        f"${seg_val:,.2f} on {st['stmt_date']} statement."
                    ),
                    "is_scrapeable": False,
                    "asof_date": st.get("stmt_date"),
                    "quantity_str": f"{seg['units']:,.3f} units",
                    "stmt_price_str": unit_p,
                    "principal_str": "In Contract",
                    "principal_sub": f"{seg.get('pct', 0):.1f}% of RILA",
                    "principal_tooltip": (
                        f"Allocated sub-segment of RILA contract #{st['symbol']} "
                        f"(contract initial premium: ${st.get('initial_premium') or 0:,.2f} on {st.get('issue_date')})."
                    ),
                    "current_value": seg_val,
                    "wow_change": 0.0,
                    "recent_change_val": None,
                    "recent_change_sub": "In Contract",
                    "recent_change_tip": st.get("recent_change_tip", ""),
                    "recent_change_sign": 0,
                    "growth_str": "In Contract",
                    "growth_sub": f"See #{st['symbol']}",
                    "growth_sign": 0,
                    "growth_tooltip": st["growth_tooltip"],
                    "pct_portfolio": seg_pct,
                    "pct_tooltip": (
                        f"{seg_pct:.2f}% of Current Vested Portfolio (${seg_val:,.2f} ÷ ${total_now:,.2f})."
                    ),
                }
            )
        children.sort(key=lambda c: (c["pct_portfolio"], c["current_value"]), reverse=True)

        sub_parts = [f"As of {st['eval_date'] if st['is_scrapeable'] else st['stmt_date']}"]
        if st["is_scrapeable"]:
            sub_parts.append(f"Live quote: ${st['current_price']:,.2f}/sh")
        recent_vest_note = None
        if st.get("stmt_quantity") is not None and abs(st["quantity"] - st["stmt_quantity"]) > 1e-4:
            recent_vest_note = f"Incl. +{st['quantity'] - st['stmt_quantity']:,.3f} sh vest since statement"
            sub_parts.append(recent_vest_note)
        if st.get("surrender_value"):
            sub_parts.append(f"Surrender val: ${st['surrender_value']:,.2f}")

        sym_disp = f"#{st['symbol']}" if st["asset_class"].endswith("ANNUITY") else st["symbol"]
        link = f"https://finance.yahoo.com/quote/{st['quote_symbol']}" if st["is_scrapeable"] and st["quote_symbol"] else None

        disp_title = st["name"]
        sim_b_map = (cfg or {}).get("sim_buckets") or {}
        sim_b_key = f"{str(st['symbol']).lower()}_{str(st['account_id']).replace('-', '_')}"
        sim_b_ovr = sim_b_map.get(sim_b_key) or {}
        sim_b_title = sim_b_ovr.get("default_title") or sim_b_ovr.get("title")
        if sim_b_title and (not disp_title or disp_title.upper() == str(st["symbol"]).upper()):
            disp_title = re.sub(rf"\s*\({re.escape(str(st['symbol']))}\)\s*$", "", str(sim_b_title)).strip()

        gid = f"single_{st['account_id']}_{st['symbol']}"
        pref = (
            (holding_prefs or {}).get(gid)
            or (holding_prefs or {}).get(f"{st['account_id']}:{st['security_id']}")
            or (holding_prefs or {}).get(st["security_id"])
            or {}
        )
        fn = pref.get("friendly_name") or ""
        stmt_name = disp_title
        disp_title_final = fn if fn else stmt_name
        fee_pct = pref.get("fee_pct")
        comment = pref.get("comment") or pref.get("notes") or ""

        groups.append(
            {
                "group_id": gid,
                "holding_key": gid,
                "account_id": st["account_id"],
                "security_id": st["security_id"],
                "institution": acc.get("institution") or inst_short,
                "asset_class": st.get("asset_class"),
                "investment_type": format_investment_type(pref.get("investment_type") or st.get("asset_class")),
                "maturity_date": st.get("maturity_date"),
                "surrender_val": st.get("surrender_value"),
                "recent_vest_note": recent_vest_note,
                "asof_date": st['eval_date'] if st['is_scrapeable'] else st['stmt_date'],
                "statement_name": stmt_name,
                "friendly_name": fn,
                "fee_pct": fee_pct,
                "comment": comment,
                "title": disp_title_final,
                "subtitle": " • ".join(sub_parts),
                "maturity_str": st["maturity_str"],
                "symbol": sym_disp,
                "link": link,
                "account_label": _friendly_account_label(st["account_id"], accounts, cfg) if st.get("account_id") else f"{inst_short} • {st['account_id']}",
                "source_label": "Live Ticker" if st["is_scrapeable"] else f"Statement ({st['stmt_date']})",
                "source_tooltip": st["source_tooltip"],
                "is_scrapeable": st["is_scrapeable"],
                "quantity_str": f"{st['quantity']:,.3f} sh" if st["is_scrapeable"] else "1 contract",
                "stmt_price_str": (
                    f"${st['stmt_price']:,.2f}"
                    if st["stmt_price"] and st["is_scrapeable"]
                    else f"${st['stmt_value']:,.0f}"
                ),
                "principal_val": st.get("principal_val"),
                "principal_date": st.get("principal_date"),
                "cagr_pct": st.get("cagr_pct"),
                "principal_str": st["principal_str"],
                "principal_sub": st["principal_sub"],
                "principal_tooltip": st["principal_tooltip"],
                "current_value": st["current_value"],
                "wow_change": st["wow_change"],
                "recent_change_val": st.get("recent_change_val"),
                "recent_change_sub": st.get("recent_change_sub", ""),
                "recent_change_tip": st.get("recent_change_tip", ""),
                "recent_change_sign": st.get("recent_change_sign", 0),
                "growth_str": st["growth_str"],
                "growth_sub": st["growth_sub"],
                "growth_sign": st["growth_sign"],
                "growth_tooltip": st["growth_tooltip"],
                "pct_portfolio": st["pct_portfolio"],
                "pct_tooltip": st["pct_tooltip"],
                "children": children,
            }
        )

    uma_by_acct: dict[str, list[dict[str, Any]]] = {}
    for x in uma_items:
        uma_by_acct.setdefault(x["account_id"], []).append(x)

    for uma_aid, acct_uma_items in uma_by_acct.items():
        uma_slug = uma_aid.replace("-", "_")
        uma_acc = accounts.get(uma_aid, {})
        uma_inst = uma_acc.get("institution") or "Brokerage"
        uma_inst_short = uma_inst.replace(" Advisors", "")
        uma_val = round(sum(x["current_value"] for x in acct_uma_items), 2)
        uma_wow = round(sum(x["wow_change"] for x in acct_uma_items), 2)
        uma_pct = round(uma_val / total_now * 100.0, 2) if total_now else 0.0
        uma_ovr = overrides.get(uma_aid) or {}
        uma_p_val: float | None = None
        uma_p_dt: str | None = None
        uma_cagr_pct: float | None = None
        if uma_ovr.get("principal") and uma_ovr.get("inception_date"):
            p_v = float(uma_ovr["principal"])
            p_d = str(uma_ovr["inception_date"])
            cagr, yrs = solve_annualized_return([(p_d, p_v)], today_iso, uma_val)
            uma_p_val = p_v
            uma_p_dt = p_d
            uma_cagr_pct = round(cagr, 4) if cagr is not None else None
            gain = uma_val - p_v
            gpct = (gain / p_v) * 100.0
            uma_p_str, uma_p_sub = f"${p_v:,.2f}", f"Inception {p_d}"
            uma_p_tip = f"Configured initial principal: ${p_v:,.2f} on {p_d}."
            uma_g_str = f"{'+' if (cagr or 0) >= 0 else ''}{cagr or 0:.2f}% / yr"
            uma_g_sub = f"{'+' if gain >= 0 else '-'}${abs(gain):,.0f} ({'+' if gpct >= 0 else ''}{gpct:.1f}%)"
            uma_g_sign = 1 if (cagr or 0) >= 0 else -1
            uma_g_tip = f"Annualized return over {yrs:.2f} yrs since {p_d}."
        else:
            uma_p_str, uma_p_sub = "—", "Cost N/A in statement"
            uma_p_tip = (
                "Monthly statement snapshot does not report original tax-lot cost basis or account opening date. "
                "Configure an initial principal override in Portfolio Settings to compute exact annualized CAGR."
            )
            uma_g_str, uma_g_sub, uma_g_sign = "—", "Inception N/A", 0
            uma_g_tip = uma_p_tip

        prev_uma = uma_val - uma_wow
        uma_pct_chg = (uma_wow / prev_uma * 100.0) if prev_uma > 0 else 0.0
        uma_sign = 1 if uma_wow > 0 else (-1 if uma_wow < 0 else 0)
        base_date_str = f" (vs {prior_iso})" if prior_iso else ""
        uma_sub = f"{'+' if uma_pct_chg >= 0 else ''}{uma_pct_chg:.1f}% • 1w{base_date_str}"
        uma_rc_tip = _format_delta_tooltip(
            "1-Week Market Price Change",
            f"{'+' if uma_wow >= 0 else '-'}${abs(uma_wow):,.2f} ({uma_pct_chg:+.2f}%)",
            f"Prior week Friday market close on {prior_iso or 'prior week'} across all {len(acct_uma_items)} constituent stocks.",
            f"{today_iso} (market quote as of latest exchange close).",
            reported_date=today_iso,
        )

        uma_gid = f"rollup_uma_{uma_slug}"
        uma_b_cfg = ((cfg or {}).get("sim_buckets") or {}).get(f"uma_{uma_slug}") or {}
        uma_cfg_title = str(uma_b_cfg.get("default_title") or uma_b_cfg.get("title") or "").split("(")[0].strip()
        uma_stmt = uma_cfg_title or f"Direct-Indexing Equity SMA ({len(acct_uma_items)} Stocks)"
        uma_pref = (holding_prefs or {}).get(uma_gid) or (holding_prefs or {}).get(uma_aid) or {}
        uma_fn = uma_pref.get("friendly_name") or ""
        uma_fee = uma_pref.get("fee_pct")
        uma_comment = uma_pref.get("comment") or uma_pref.get("notes") or ""

        groups.append(
            {
                "group_id": uma_gid,
                "holding_key": uma_gid,
                "account_id": uma_aid,
                "security_id": f"SMA_{uma_slug}",
                "institution": uma_inst,
                "asset_class": "EQUITY",
                "investment_type": "Direct-Indexing SMA (US Large Cap)",
                "maturity_date": None,
                "asof_date": today_iso,
                "statement_name": uma_stmt,
                "friendly_name": uma_fn,
                "fee_pct": uma_fee,
                "comment": uma_comment,
                "title": uma_fn if uma_fn else uma_stmt,
                "subtitle": f"Direct-indexing SMA • Click to expand all {len(acct_uma_items)} exchange-priced equities",
                "maturity_str": None,
                "symbol": f"{len(acct_uma_items)} Stocks",
                "link": None,
                "account_label": _friendly_account_label(uma_aid, accounts, cfg) if uma_aid else f"{uma_inst_short} • {uma_aid}",
                "source_label": f"Live ({len(acct_uma_items)}/{len(acct_uma_items)})",
                "source_tooltip": (
                    f"100% Live Market Pricing: All {len(acct_uma_items)} constituent stocks in {uma_stmt} "
                    f"are priced directly from Yahoo Finance split-adjusted market quotes as of {today_iso} "
                    f"(statement equity total was ${sum(x['stmt_value'] for x in acct_uma_items):,.2f})."
                ),
                "is_scrapeable": True,
                "quantity_str": f"{sum(x['quantity'] for x in acct_uma_items):,.0f} total sh",
                "stmt_price_str": f"${sum(x['stmt_value'] for x in acct_uma_items):,.0f}",
                "principal_val": uma_p_val,
                "principal_date": uma_p_dt,
                "cagr_pct": uma_cagr_pct,
                "cagr_method": "Average annual growth from initial invested principal to current group value",
                "principal_str": uma_p_str,
                "principal_sub": uma_p_sub,
                "principal_tooltip": uma_p_tip,
                "current_value": uma_val,
                "wow_change": uma_wow,
                "recent_change_val": uma_wow,
                "recent_change_sub": uma_sub,
                "recent_change_tip": uma_rc_tip,
                "recent_change_sign": uma_sign,
                "growth_str": uma_g_str,
                "growth_sub": uma_g_sub,
                "growth_sign": uma_g_sign,
                "growth_tooltip": uma_g_tip,
                "pct_portfolio": uma_pct,
                "pct_tooltip": (
                    f"{uma_pct:.2f}% of Current Vested Portfolio (${uma_val:,.2f} ÷ ${total_now:,.2f}). "
                    f"Excludes unvested future stock grant releases."
                ),
                "children": [_child_from_item(x, uma_inst_short, holding_prefs, accounts=accounts, cfg=cfg) for x in acct_uma_items],
            }
        )

    muni_by_acct: dict[str, list[dict[str, Any]]] = {}
    for x in muni_items:
        muni_by_acct.setdefault(x["account_id"], []).append(x)

    for muni_aid, acct_muni_items in muni_by_acct.items():
        muni_slug = muni_aid.replace("-", "_")
        muni_acc = accounts.get(muni_aid, {})
        muni_inst = muni_acc.get("institution") or "Brokerage"
        muni_inst_short = muni_inst.replace(" Advisors", "")
        muni_val = round(sum(x["current_value"] for x in acct_muni_items), 2)
        muni_accr = round(sum(x["accrued_interest"] for x in acct_muni_items), 2)
        muni_par = sum(x["quantity"] for x in acct_muni_items)
        muni_pct = round(muni_val / total_now * 100.0, 2) if total_now else 0.0
        mat_dates = sorted(x["maturity_date"] for x in acct_muni_items if x.get("maturity_date"))
        mat_span = f"Maturities: {mat_dates[0]} → {mat_dates[-1]}" if mat_dates else None
        w_yld = (
            sum((x.get("yield_pct") or 0.0) * x["current_value"] for x in acct_muni_items) / muni_val
            if muni_val > 0
            else 0.0
        )
        prem_diff = round(muni_val - muni_par, 2)
        prem_pct = (prem_diff / muni_par * 100.0) if muni_par > 0 else 0.0
        muni_ovr = overrides.get(muni_aid) or (overrides.get("muni_bonds") if len(muni_by_acct) == 1 else None) or {}
        muni_eval_dt = acct_muni_items[0]["stmt_date"] or today_iso
        muni_p_val: float | None = None
        muni_p_dt: str | None = None
        muni_cagr_pct: float | None = None

        if muni_ovr.get("principal") and muni_ovr.get("inception_date"):
            p_v = float(muni_ovr["principal"])
            p_d = str(muni_ovr["inception_date"])
            cagr, yrs = solve_annualized_return([(p_d, p_v)], muni_eval_dt, muni_val)
            muni_p_val = p_v
            muni_p_dt = p_d
            muni_cagr_pct = round(cagr, 4) if cagr is not None else None
            gain = round(muni_val - p_v, 2)
            gpct = (gain / p_v * 100.0) if p_v > 0 else 0.0
            muni_p_str = f"${p_v:,.2f}"
            muni_p_sub = f"Acquired {p_d}"
            muni_p_tip = (
                f"Initial Principal Investment: ${p_v:,.2f} on {p_d} across the {len(acct_muni_items)} municipal bonds "
                f"(combined face par value: ${muni_par:,.2f})."
            )
            muni_g_str = f"{'+' if (cagr or 0) >= 0 else ''}{cagr or 0:.2f}% / yr"
            muni_g_sub = f"{'+' if gain >= 0 else '-'}${abs(gain):,.0f} ({'+' if gpct >= 0 else ''}{gpct:.1f}%)"
            muni_g_sign = 1 if (cagr or 0) >= 0 else -1
            muni_g_tip = (
                f"Annualized Average Growth (CAGR): {'+' if (cagr or 0) >= 0 else ''}{cagr or 0:.2f}% / yr over {yrs:.2f} yrs "
                f"({p_d} → {muni_eval_dt}). Initial Principal: ${p_v:,.2f} → Current Market Value: ${muni_val:,.2f} "
                f"(Total Gain: {'+' if gain >= 0 else '-'}${abs(gain):,.2f} / {'+' if gpct >= 0 else ''}{gpct:.2f}%). "
                f"Weighted-average tax-exempt coupon yield: {w_yld:.2f}% / yr."
            )
        else:
            muni_p_str = f"${muni_par:,.2f}"
            muni_p_sub = "Total par face value"
            muni_p_tip = (
                f"Combined Face Par Principal across all {len(acct_muni_items)} municipal bonds: ${muni_par:,.2f} "
                f"(trading at ${muni_val:,.2f}, a +${prem_diff:,.2f} / +{prem_pct:.2f}% premium due to tax-exempt coupons). "
                f"Original secondary-market purchase dates/costs are omitted on monthly statement snapshots."
            )
            muni_g_str = f"{w_yld:.2f}% Yld"
            muni_g_sub = f"+${prem_diff:,.0f} (+{prem_pct:.1f}% vs Par)"
            muni_g_sign = 1
            muni_g_tip = (
                f"Weighted-average tax-exempt current yield: {w_yld:.2f}% / yr. "
                f"Market value is +${prem_diff:,.2f} (+{prem_pct:.2f}%) above ${muni_par:,.2f} face par principal."
            )

        muni_stmt_dt = acct_muni_items[0]["stmt_date"] if acct_muni_items else today_iso
        muni_date_str = f" (as of {muni_stmt_dt})" if muni_stmt_dt else ""
        if muni_accr > 0:
            muni_rc_val = muni_accr
            muni_rc_sub = f"{w_yld:.2f}% Yld • Accrued{muni_date_str}"
            muni_rc_tip = _format_delta_tooltip(
                "Accrued Coupon Interest",
                f"+${muni_accr:,.2f} ({w_yld:.2f}%/yr weighted market yield)",
                f"Cumulative coupon interest earned since respective semi-annual coupon reset cycles through statement date {muni_stmt_dt}.",
                f"{muni_stmt_dt} ({muni_inst} monthly statement period close across all {len(acct_muni_items)} municipal bonds).",
                reported_date=muni_stmt_dt,
            )
            muni_rc_sign = 1
        else:
            muni_rc_val = prem_diff
            muni_rc_sub = f"{prem_pct:+.1f}% • vs Par{muni_date_str}"
            muni_rc_tip = _format_delta_tooltip(
                "Market Value vs. Par",
                f"{'+' if prem_diff >= 0 else '-'}${abs(prem_diff):,.2f} ({prem_pct:+.2f}% premium/discount, {w_yld:.2f}%/yr yield)",
                f"Combined face par principal of ${muni_par:,.2f} across all {len(acct_muni_items)} municipal bonds.",
                f"{muni_stmt_dt} ({muni_inst} monthly statement period close).",
                reported_date=muni_stmt_dt,
            )
            muni_rc_sign = 1 if prem_diff >= 0 else -1

        muni_gid = f"rollup_munis_{muni_slug}"
        muni_b_cfg = ((cfg or {}).get("sim_buckets") or {}).get(f"munis_{muni_slug}") or {}
        muni_stmt = str(muni_b_cfg.get("default_title") or muni_b_cfg.get("title") or f"Municipal Bond SMA ({len(acct_muni_items)} Bonds)")
        state_code = str(((cfg or {}).get("tax_rates") or {}).get("state_code") or "").strip().upper()
        muni_inv_type = f"Tax-Exempt {state_code} Municipal Bonds" if state_code else "Tax-Exempt Municipal Bonds"
        muni_pref = (holding_prefs or {}).get(muni_gid) or (holding_prefs or {}).get(muni_aid) or {}
        muni_fn = muni_pref.get("friendly_name") or ""
        muni_fee = muni_pref.get("fee_pct")
        muni_comment = muni_pref.get("comment") or muni_pref.get("notes") or ""

        groups.append(
            {
                "group_id": muni_gid,
                "holding_key": muni_gid,
                "account_id": muni_aid,
                "security_id": f"MUNIS_{muni_slug}",
                "institution": muni_inst,
                "asset_class": "MUNI_BOND",
                "investment_type": muni_inv_type,
                "maturity_date": mat_span,
                "accrued_interest": round(muni_accr, 2),
                "asof_date": acct_muni_items[0]["stmt_date"] if acct_muni_items else None,
                "statement_name": muni_stmt,
                "friendly_name": muni_fn,
                "fee_pct": muni_fee,
                "comment": muni_comment,
                "title": muni_fn if muni_fn else muni_stmt,
                "subtitle": f"Excludes +${muni_accr:,.2f} accrued coupon • Click to expand all {len(acct_muni_items)} municipal issues",
                "maturity_str": mat_span,
                "symbol": f"{len(acct_muni_items)} CUSIPs",
                "link": None,
                "account_label": _friendly_account_label(muni_aid, accounts, cfg) if muni_aid else f"{muni_inst_short} • {muni_aid}",
                "source_label": f"Statement ({acct_muni_items[0]['stmt_date']})",
                "source_tooltip": (
                    f"Statement Municipal Bond Basket: {len(acct_muni_items)} municipal bonds (${muni_val:,.2f} market value "
                    f"as of {acct_muni_items[0]['stmt_date']}; +${muni_accr:,.2f} accrued interest). Municipal CUSIPs do not have "
                    f"free real-time public REST quotes; values update automatically whenever a new statement is dropped into Statements/."
                ),
                "is_scrapeable": False,
                "quantity_str": f"${muni_par:,.0f} par",
                "stmt_price_str": f"${muni_val:,.0f}",
                "principal_val": muni_p_val,
                "principal_date": muni_p_dt,
                "cagr_pct": muni_cagr_pct,
                "cagr_end_date": muni_eval_dt,
                "cagr_method": "Average annual growth from initial invested principal to current group value",
                "principal_str": muni_p_str,
                "principal_sub": muni_p_sub,
                "principal_tooltip": muni_p_tip,
                "current_value": muni_val,
                "wow_change": 0.0,
                "recent_change_val": muni_rc_val,
                "recent_change_sub": muni_rc_sub,
                "recent_change_tip": muni_rc_tip,
                "recent_change_sign": muni_rc_sign,
                "growth_str": muni_g_str,
                "growth_sub": muni_g_sub,
                "growth_sign": muni_g_sign,
                "growth_tooltip": muni_g_tip,
                "pct_portfolio": muni_pct,
                "pct_tooltip": (
                    f"{muni_pct:.2f}% of Current Vested Portfolio (${muni_val:,.2f} ÷ ${total_now:,.2f}). "
                    f"Excludes unvested future stock grant releases."
                ),
                "children": [_child_from_item(x, muni_inst_short, holding_prefs, accounts=accounts, cfg=cfg) for x in acct_muni_items],
            }
        )

    cash_rate_cfg = (
        ((cfg or {}).get("sim_asset_rates") or {}).get("cash_sweeps")
        or ((cfg or {}).get("sim_buckets") or {}).get("cash_sweeps")
        or {}
    )
    cash_apy_pct = float(cash_rate_cfg["annual_rate_pct"]) if cash_rate_cfg.get("annual_rate_pct") is not None else 0.0
    cash_apy_dec = cash_apy_pct / 100.0

    if cash_items:
        c_val = round(sum(x["current_value"] for x in cash_items), 2)
        c_pct = round(c_val / total_now * 100.0, 2) if total_now else 0.0
        cash_stmt_dt = next((x["stmt_date"] for x in cash_items if x.get("stmt_date")), today_iso)
        cash_date_str = f" (as of {cash_stmt_dt})" if cash_stmt_dt else ""
        cash_mo_int = round(c_val * cash_apy_dec / 12.0, 2)
        cash_rc_sub = f"~{cash_apy_pct:.1f}% APY • 1mo{cash_date_str}"
        cash_rc_tip = _format_delta_tooltip(
            "Estimated Monthly Sweep Interest",
            f"+${cash_mo_int:,.2f}/mo (~{cash_apy_pct:.2f}% APY)",
            f"Combined liquid cash and FDIC sweep balance of ${c_val:,.2f} over trailing 30 days across {len(cash_items)} accounts.",
            f"{cash_stmt_dt} (monthly brokerage statement period close across {len(cash_items)} accounts).",
            reported_date=cash_stmt_dt,
        )
        cash_rc_sign = 1 if cash_mo_int > 0 else 0
        cash_gid = "rollup_cash_all"
        cash_stmt = "Cash, Bank Deposit Sweeps & Liquid Deposits"
        cash_pref = (holding_prefs or {}).get(cash_gid) or (holding_prefs or {}).get("cash_sweeps") or {}
        cash_fn = cash_pref.get("friendly_name") or ""
        cash_fee = cash_pref.get("fee_pct")
        cash_comment = cash_pref.get("comment") or cash_pref.get("notes") or ""
        cash_insts = list(
            dict.fromkeys(
                (accounts.get(x["account_id"]) or {}).get("institution", "").replace(" Advisors", "")
                for x in cash_items
                if (accounts.get(x["account_id"]) or {}).get("institution")
            )
        )
        cash_inst_str = " & ".join(cash_insts) if cash_insts else "All Custodians"

        groups.append(
            {
                "group_id": cash_gid,
                "holding_key": cash_gid,
                "account_id": "All Accounts",
                "security_id": "CASH_SWEEPS_ALL",
                "institution": cash_inst_str,
                "asset_class": "CASH",
                "investment_type": "Liquid Cash & Deposit Sweeps",
                "maturity_date": None,
                "asof_date": cash_items[0]["stmt_date"] if cash_items else None,
                "statement_name": cash_stmt,
                "friendly_name": cash_fn,
                "fee_pct": cash_fee,
                "comment": cash_comment,
                "title": cash_fn if cash_fn else cash_stmt,
                "subtitle": "Click to expand per-account cash & sweep balances",
                "maturity_str": None,
                "symbol": f"{len(cash_items)} Accounts",
                "link": None,
                "account_label": f"All {len(cash_items)} Accounts",
                "source_label": "Statement",
                "source_tooltip": (
                    f"Statement Cash & FDIC Sweep Balances across all {len(cash_items)} accounts. "
                    "Updated automatically whenever new statements are dropped into Statements/."
                ),
                "is_scrapeable": False,
                "quantity_str": f"${c_val:,.2f}",
                "stmt_price_str": "$1.00",
                "principal_str": f"${c_val:,.2f}",
                "principal_sub": "Liquid USD",
                "principal_tooltip": "1:1 USD cash and FDIC bank deposit sweep balances.",
                "current_value": c_val,
                "wow_change": 0.0,
                "recent_change_val": cash_mo_int,
                "recent_change_sub": cash_rc_sub,
                "recent_change_tip": cash_rc_tip,
                "recent_change_sign": cash_rc_sign,
                "growth_str": "Sweep Yield",
                "growth_sub": f"~0.1%–{cash_apy_pct:.2f}% APY",
                "growth_sign": 0,
                "growth_tooltip": (
                    f"Brokered Liquid Deposit and Bank Deposit Sweeps earn up to ~{cash_apy_pct:.2f}% APY, credited monthly into cash."
                ),
                "pct_portfolio": c_pct,
                "pct_tooltip": f"{c_pct:.2f}% of Current Vested Portfolio (${c_val:,.2f} ÷ ${total_now:,.2f}).",
                "children": [_child_from_item(x, "", holding_prefs, accounts=accounts, cfg=cfg) for x in cash_items],
            }
        )

    for g in groups:
        if g.get("children"):
            g["children"].sort(key=lambda c: (c["pct_portfolio"], c["current_value"]), reverse=True)
    groups.sort(key=lambda g: (g["pct_portfolio"], g["current_value"]), reverse=True)
    return groups


def _build_account_rollups(
    items: list[dict[str, Any]],
    accounts: dict[str, dict[str, Any]],
    total_now: float,
    today_iso: str,
    overrides: dict[str, Any],
    holding_prefs: dict[str, dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
    prior_iso: str = "",
) -> list[dict[str, Any]]:
    by_acct: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_acct.setdefault(it["account_id"], []).append(it)

    cash_rate_cfg = (
        ((cfg or {}).get("sim_asset_rates") or {}).get("cash_sweeps")
        or ((cfg or {}).get("sim_buckets") or {}).get("cash_sweeps")
        or {}
    )
    cash_apy_pct = float(cash_rate_cfg["annual_rate_pct"]) if cash_rate_cfg.get("annual_rate_pct") is not None else 0.0
    cash_apy_dec = cash_apy_pct / 100.0
    acct_cfg_map = (cfg or {}).get("accounts") or {}

    out: list[dict[str, Any]] = []
    for aid, acc in accounts.items():
        aitems = by_acct.get(aid, [])
        if not aitems:
            continue
        inst_short = (acc.get("institution") or "Brokerage").replace(" Advisors", "")
        aval = round(sum(x["current_value"] for x in aitems), 2)
        awow = round(sum(x["wow_change"] for x in aitems), 2)
        apct = round(aval / total_now * 100.0, 2) if total_now else 0.0
        live_cnt = sum(1 for x in aitems if x["is_scrapeable"])

        # Maturity summary across account if applicable
        mats = sorted(x["maturity_date"] for x in aitems if x.get("maturity_date"))
        acct_mat_str = (
            f"Maturities: {mats[0]} → {mats[-1]}"
            if len(mats) > 1
            else (f"Matures: {mats[0]}" if len(mats) == 1 else None)
        )

        account_cagr, account_start, account_method = None, None, None
        # Account-level principal & annualized Total Growth
        acct_ovr = overrides.get(aid) or {}
        non_cash = [x for x in aitems if x["asset_class"] != "CASH"]
        cash_sum = sum(x["current_value"] for x in aitems if x["asset_class"] == "CASH")
        is_sp_acct = (
            bool((acct_cfg_map.get(aid) or {}).get("is_stock_plan"))
            or "STOCK_PLAN" in (acc.get("account_type") or "").upper()
            or "STOCK PLAN" in (acc.get("account_name") or "").upper()
            or "STOCK PLAN" in (acc.get("account_type") or "").upper()
        )

        if acct_ovr.get("principal") and acct_ovr.get("inception_date"):
            p_tot = float(acct_ovr["principal"])
            p_dt = str(acct_ovr["inception_date"])
            cagr, yrs = solve_annualized_return([(p_dt, p_tot)], today_iso, aval)
            account_cagr, account_start = cagr, p_dt
            account_method = "Annualized account valuation relative to configured initial principal"
            gain = aval - p_tot
            gpct = (gain / p_tot) * 100.0
            p_str, p_sub = f"${p_tot:,.2f}", f"Inception {p_dt}"
            p_tip = f"Configured account initial principal: ${p_tot:,.2f} on {p_dt}."
            g_str = f"{'+' if (cagr or 0) >= 0 else ''}{cagr or 0:.2f}% / yr"
            g_sub = f"{'+' if gain >= 0 else '-'}${abs(gain):,.0f} ({'+' if gpct >= 0 else ''}{gpct:.1f}%)"
            g_sign = 1 if (cagr or 0) >= 0 else -1
            g_tip = f"Annualized account growth over {yrs:.2f} yrs since {p_dt}."
        elif non_cash and all(x.get("principal_val") is not None for x in non_cash):
            p_non_cash = sum(float(x["principal_val"]) for x in non_cash)
            p_tot = round(p_non_cash + cash_sum, 2)
            gain = round(aval - p_tot, 2)
            gpct = (gain / p_tot * 100.0) if p_tot > 0 else 0.0

            if all(x.get("principal_date") for x in non_cash):
                dated_lots = [(str(x["principal_date"]), float(x["principal_val"])) for x in non_cash]
                val_non_cash = sum(x["current_value"] for x in non_cash)
                cagr, yrs = solve_annualized_return(dated_lots, today_iso, val_non_cash)
                earliest_dt = min(d for d, _ in dated_lots)
                account_cagr, account_start = cagr, earliest_dt
                account_method = "Average annual return across dated investments (weighted by dollar amount and holding time)"
                p_str = f"${p_tot:,.2f}"
                p_sub = f"Since {earliest_dt}"
                p_tip = (
                    f"Sum of reported principals across {len(non_cash)} investment(s) (${p_non_cash:,.2f}) "
                    f"+ ${cash_sum:,.2f} cash = ${p_tot:,.2f}."
                )
                g_str = f"{'+' if (cagr or 0) >= 0 else ''}{cagr or 0:.2f}% / yr"
                g_sub = f"{'+' if gain >= 0 else '-'}${abs(gain):,.0f} ({'+' if gpct >= 0 else ''}{gpct:.1f}%)"
                g_sign = 1 if (cagr or 0) >= 0 else -1
                g_tip = (
                    f"Money-Weighted Annualized Growth across account investments: "
                    f"{'+' if (cagr or 0) >= 0 else ''}{cagr or 0:.2f}% / yr (avg duration {yrs:.2f} yrs, earliest lot {earliest_dt}). "
                    f"Total Gain: {'+' if gain >= 0 else '-'}${abs(gain):,.2f} ({'+' if gpct >= 0 else ''}{gpct:.2f}%)."
                )
            else:
                p_str = f"${p_tot:,.2f}"
                p_sub = "Reported cost/par + cash"
                p_tip = f"Combined statement cost/par (${p_non_cash:,.2f}) + cash (${cash_sum:,.2f}) = ${p_tot:,.2f}."
                g_str = f"{'+' if gpct >= 0 else ''}{gpct:.1f}% total"
                g_sub = f"{'+' if gain >= 0 else '-'}${abs(gain):,.0f} (part. dates)"
                g_sign = 1 if gain >= 0 else -1
                g_tip = (
                    f"Cumulative Account Gain vs. Reported Cost/Par: {'+' if gain >= 0 else '-'}${abs(gain):,.2f} "
                    f"({'+' if gpct >= 0 else ''}{gpct:.2f}%). One or more positions report consolidated 'Net Tax Lots' "
                    f"without an initial purchase date."
                )
        elif is_sp_acct:
            p_str, p_sub = "Equity Grants ($0)", "Stock plan awards"
            p_tip = "Employee stock plan account ($0 cash outlay for vested awards)."
            g_str, g_sub, g_sign = "Stock Plan Equity", "Vested awards", 0
            g_tip = p_tip
        else:
            p_str, p_sub = "—", "Cost N/A in statement"
            p_tip = "Monthly statement omits original cost basis & inception date."
            g_str, g_sub, g_sign = "—", "Inception N/A", 0
            g_tip = p_tip

        # Account recent change
        acct_stmt_dt = next((x["stmt_date"] for x in aitems if x.get("stmt_date")), today_iso)
        if live_cnt == len(aitems) or (live_cnt > 0 and awow != 0.0):
            aprev = aval - awow
            apct_chg = (awow / aprev * 100.0) if aprev > 0 else 0.0
            asign = 1 if awow > 0 else (-1 if awow < 0 else 0)
            acct_rc_val = awow
            base_date_str = f" (vs {prior_iso})" if prior_iso else ""
            acct_rc_sub = f"{'+' if apct_chg >= 0 else ''}{apct_chg:.1f}% • 1w{base_date_str}"
            acct_rc_tip = _format_delta_tooltip(
                "1-Week Market Price Change",
                f"{'+' if awow >= 0 else '-'}${abs(awow):,.2f} ({apct_chg:+.2f}%)",
                f"Prior week Friday market close on {prior_iso or 'prior week'}.",
                f"{today_iso} (market quotes as of latest exchange close).",
                reported_date=today_iso,
            )
            acct_rc_sign = asign
        elif all(x["asset_class"] == "CASH" for x in aitems):
            acct_mo_int = round(aval * cash_apy_dec / 12.0, 2)
            acct_rc_val = acct_mo_int
            acct_date_str = f" (as of {acct_stmt_dt})" if acct_stmt_dt else ""
            acct_rc_sub = f"~{cash_apy_pct:.1f}% APY • 1mo{acct_date_str}"
            acct_rc_tip = _format_delta_tooltip(
                "Estimated Monthly Cash Sweep Interest",
                f"+${acct_mo_int:,.2f}/mo (~{cash_apy_pct:.2f}% APY)",
                f"Account liquid cash balance of ${aval:,.2f} over trailing 30 days.",
                f"{acct_stmt_dt} ({acc.get('institution', 'Brokerage')} statement period close).",
                reported_date=acct_stmt_dt,
            )
            acct_rc_sign = 1 if acct_mo_int > 0 else 0
        else:
            sum_rc = round(sum(x.get("recent_change_val") or 0.0 for x in aitems), 2)
            acct_rc_val = sum_rc
            prev_aval = aval - sum_rc
            acct_pct = (sum_rc / prev_aval * 100.0) if prev_aval > 0 else 0.0
            acct_date_str = f" (as of {acct_stmt_dt})" if acct_stmt_dt else ""
            acct_pct_sign = "+" if acct_pct >= 0 else ""
            acct_rc_sub = f"{acct_pct_sign}{acct_pct:.1f}% • Combined{acct_date_str}" if aval > 0 else f"Combined{acct_date_str}"
            diff_child = next((x for x in aitems if x.get("reported_date") and x.get("stmt_date") and x.get("reported_date") != x.get("stmt_date")), None)
            acct_rep_dt = diff_child.get("reported_date") if diff_child else acct_stmt_dt
            acct_rc_tip = _format_delta_tooltip(
                "Combined Recent Performance",
                f"{'+' if sum_rc >= 0 else '-'}${abs(sum_rc):,.2f} across {len(aitems)} positions",
                "Respective position baseline dates (contract issue dates, coupon cycles, prior week closes).",
                f"{acct_stmt_dt} (latest position valuation date).",
                reported_date=acct_rep_dt,
                reported_source=f"{acc.get('institution', 'Brokerage')} statement",
            )
            acct_rc_sign = 1 if sum_rc > 0 else (-1 if sum_rc < 0 else 0)

        gid = f"acct_{aid}"
        pref = (holding_prefs or {}).get(gid) or (holding_prefs or {}).get(aid) or {}
        fn = pref.get("friendly_name") or ""
        stmt_name = acc["account_name"]
        disp_title = fn if fn else stmt_name
        acct_fee = pref.get("fee_pct")
        acct_comment = pref.get("comment") or pref.get("notes") or ""

        children_sorted = sorted(
            [_child_from_item(x, inst_short, holding_prefs, accounts=accounts, cfg=cfg) for x in aitems],
            key=lambda c: (c["pct_portfolio"], c["current_value"]),
            reverse=True,
        )
        out.append(
            {
                "group_id": gid,
                "holding_key": gid,
                "account_id": aid,
                "security_id": aid,
                "institution": acc.get("institution") or inst_short,
                "asset_class": acc.get("tax_status") or "ACCOUNT",
                "investment_type": f"{acc['account_type']} ({acc['tax_status']})",
                "maturity_date": acct_mat_str,
                "statement_name": stmt_name,
                "friendly_name": fn,
                "fee_pct": acct_fee,
                "comment": acct_comment,
                "title": disp_title,
                "subtitle": f"{acc['account_type']} • {acc['tax_status']} • {len(aitems)} position(s)",
                "maturity_str": acct_mat_str,
                "symbol": aid,
                "link": None,
                "account_label": _friendly_account_label(aid, accounts, cfg) if aid else inst_short,
                "source_label": f"{live_cnt}/{len(aitems)} Live",
                "source_tooltip": (
                    f"Account {aid} ({acc['institution']}): {live_cnt} of {len(aitems)} position(s) priced via live "
                    f"Yahoo Finance market quotes; {len(aitems) - live_cnt} anchored from ingested custodial statements."
                ),
                "is_scrapeable": live_cnt > 0,
                "quantity_str": f"{len(aitems)} pos",
                "stmt_price_str": f"${sum(x['stmt_value'] for x in aitems):,.0f}",
                "principal_str": p_str,
                "principal_sub": p_sub,
                "principal_tooltip": p_tip,
                "principal_date": account_start,
                "cagr_pct": account_cagr,
                "cagr_end_date": today_iso,
                "cagr_method": account_method,
                "current_value": aval,
                "wow_change": awow,
                "recent_change_val": acct_rc_val,
                "recent_change_sub": acct_rc_sub,
                "recent_change_tip": acct_rc_tip,
                "recent_change_sign": acct_rc_sign,
                "growth_str": g_str,
                "growth_sub": g_sub,
                "growth_sign": g_sign,
                "growth_tooltip": g_tip,
                "pct_portfolio": apct,
                "pct_tooltip": f"{apct:.2f}% of Current Vested Portfolio (${aval:,.2f} ÷ ${total_now:,.2f}).",
                "children": children_sorted,
            }
        )
    out.sort(key=lambda g: (g["pct_portfolio"], g["current_value"]), reverse=True)
    return out
