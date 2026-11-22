#!/usr/bin/env python3
"""
Post-Ingest Holding Forecast & Volatility Enrichment Engine (`IngestPipeline.html` Section 4).

Populates `holding_forecasts_baseline` in SQLite for every active holding (plus global ticker
fallbacks) using a 3-tier asset-specific waterfall:
  - Track A (Public Scrapeable Tickers):
      10-Year Yahoo Finance `adj_close` (dividend- and split-adjusted) Total-Return CAGR,
      plus 10-Year weekly `realized_vol_pct` (annualized sigma), `beta_spy`, and `corr_spy`.
      (Money-market / T-bill tickers in `cash_fixed` use trailing 1Y yield/return instead of ZIRP 10Y.)
  - Track B (Individual Bonds, CDs, Cash Sweeps & Fixed MYGAs):
      Forward contractual `credited_rate_pct` / statement `yield_pct` (nominal YTM) primary,
      falling back to coupon current yield or inception CAGR.
  - Track C (Non-Public Variable / Hybrid Contracts — FIAs, RILAs, Private Funds):
      Statement `reported_return_pct` primary, falling back to statement inception CAGR
      `(market_value / initial_premium_or_basis)**(1/yrs) - 1` when age >= 180 days, or
      `LOW`-confidence risk-class benchmark proxy with prominent CLI warning (`DP-6: 6A`).
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date, timedelta
from typing import Any

from db.database import PortfolioDB, utc_now_iso
from ingest.classification import VALID_RISK_CLASSES, default_risk_class_for_asset_class
from settings import MUNI_EQUILIBRIUM_RETURN_PCT, RISK_CLASSES
from valuation.market_data import sync_market_prices

EVIDENCE_VERSION = 4
MIN_HISTORY_DAYS_FOR_CAGR = 180
CLAMP_MIN_PCT = -20.0
CLAMP_MAX_PCT = 25.0
PEER_BENCHMARK_SYMBOLS = ("SPY", "ACWI", "AGG", "MUB", "BIL", "GLD")
_GLOBAL_EQ_NAME_RE = re.compile(
    r"\b(global|international|intl|world|emerging|ex[- ]us|foreign)\b",
    re.IGNORECASE,
)

EQUILIBRIUM_LABELS: dict[str, str] = {
    "single_stock": "Long-run historical average for US stocks (50–100 year baseline",
    "broad_equity": "Long-run historical average for diversified equities (50–100 year baseline",
    "hybrid_buffered": "Long-run historical average for balanced / buffered strategies (baseline",
    "fixed_income": "Long-run historical average for taxable investment-grade bonds (baseline",
    "cash_fixed": "Long-run historical average for cash and short-term reserves (baseline",
    "real_diversifier": "Long-run historical average for gold and real assets (baseline",
    "crypto_speculative": "Long-run baseline assumption for high-volatility / speculative assets (as",
}


def format_equilibrium_detail(risk_class: str, as_of_str: str, *, is_muni: bool = False) -> str:
    if is_muni:
        return f"Long-run historical average for tax-exempt municipal bonds (baseline as of {as_of_str})"
    prefix = EQUILIBRIUM_LABELS.get(risk_class)
    if prefix:
        if prefix.endswith("(as"):
            return f"{prefix} of {as_of_str})"
        return f"{prefix} as of {as_of_str})"
    return f"Long-run historical average for {risk_class.replace('_', ' ')} holdings (as of {as_of_str})"


def clamp_forecast_return(raw_pct: float, risk_class: str) -> tuple[float, bool]:
    if risk_class == "cash_fixed":
        clamped = max(-5.0, min(15.0, float(raw_pct)))
    else:
        clamped = max(CLAMP_MIN_PCT, min(CLAMP_MAX_PCT, float(raw_pct)))
    return round(clamped, 2), abs(clamped - float(raw_pct)) > 0.015


def _metadata(raw: str | None) -> dict[str, Any]:
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _safe_parse_iso(d_str: str | None) -> date | None:
    if not d_str:
        return None
    s = str(d_str).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _load_weekly_adj_series(
    db: PortfolioDB,
    quote_symbol: str,
    start_iso: str,
    end_iso: str,
) -> list[tuple[date, float]]:
    """Loads chronological `(price_date, adj_or_close)` rows for `quote_symbol` in `[start_iso .. end_iso]`."""
    rows = db.conn.execute(
        """
        SELECT price_date, close_price, adj_close
          FROM market_prices
         WHERE quote_symbol = ?
           AND price_date >= ?
           AND price_date <= ?
         ORDER BY price_date ASC
        """,
        (quote_symbol, start_iso, end_iso),
    ).fetchall()
    out: list[tuple[date, float]] = []
    for r in rows:
        dt = _safe_parse_iso(r["price_date"])
        px = r["adj_close"] if r["adj_close"] is not None and float(r["adj_close"]) > 0 else r["close_price"]
        if dt is not None and px is not None and float(px) > 0:
            out.append((dt, float(px)))
    return out


def _market_period(symbol: str, series: list[tuple[date, float]]) -> str:
    if not series:
        return ""
    first, last = series[0][0], series[-1][0]
    years = (last - first).days / 365.2425
    mult = series[-1][1] / series[0][1]
    cum_pct = (mult - 1.0) * 100.0
    return (
        f"Average annual total return for {symbol} from {first} to {last} ({years:.1f} years on Yahoo Finance), "
        f"reflecting {mult:.2f}× total growth ({cum_pct:+.1f}%) including stock splits and reinvested dividends"
    )


def _compute_weekly_log_returns(series: list[tuple[date, float]]) -> dict[date, float]:
    """
    Computes weekly log returns keyed by Friday date, skipping multi-week gaps (`>11 days`)
    or unadjusted split glitches (`|ln(p_t/p_{t-1})| > 0.85`).
    """
    rets: dict[date, float] = {}
    if len(series) < 2:
        return rets
    prev_dt, prev_px = series[0]
    for dt, px in series[1:]:
        gap = (dt - prev_dt).days
        if 4 <= gap <= 11 and prev_px > 0 and px > 0:
            lr = math.log(px / prev_px)
            if abs(lr) <= 0.85:
                rets[dt] = lr
        if gap >= 4:
            prev_dt, prev_px = dt, px
    return rets


def _compute_public_metrics(
    series: list[tuple[date, float]],
    spy_rets: dict[date, float],
    spy_var: float,
) -> dict[str, float | None]:
    """Computes annualized total-return CAGR, lookback years, realized sigma %, beta_spy, and corr_spy."""
    if len(series) < 2:
        return {
            "cagr_pct": None,
            "lookback_years": 0.0,
            "realized_vol_pct": None,
            "beta_spy": None,
            "corr_spy": None,
        }
    first_dt, first_px = series[0]
    last_dt, last_px = series[-1]
    yrs = (last_dt - first_dt).days / 365.2425
    cagr_pct: float | None = None
    if yrs >= (MIN_HISTORY_DAYS_FOR_CAGR / 365.2425) and first_px > 0 and last_px > 0:
        cagr_pct = (math.pow(last_px / first_px, 1.0 / yrs) - 1.0) * 100.0

    rets = _compute_weekly_log_returns(series)
    realized_vol_pct: float | None = None
    beta_spy: float | None = None
    corr_spy: float | None = None

    if len(rets) >= 20:
        vals = list(rets.values())
        sd_wk = statistics.stdev(vals)
        realized_vol_pct = max(0.5, min(120.0, sd_wk * math.sqrt(52.0) * 100.0))

        common_dts = [d for d in rets.keys() if d in spy_rets]
        if len(common_dts) >= 20 and spy_var > 1e-12:
            xs = [spy_rets[d] for d in common_dts]
            ys = [rets[d] for d in common_dts]
            mx = sum(xs) / len(xs)
            my = sum(ys) / len(ys)
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(common_dts) - 1)
            vx = sum((x - mx) ** 2 for x in xs) / (len(common_dts) - 1)
            vy = sum((y - my) ** 2 for y in ys) / (len(common_dts) - 1)
            if vx > 1e-12:
                beta_spy = max(-2.5, min(3.5, cov / vx))
            if vx > 1e-12 and vy > 1e-12:
                corr_spy = max(-0.95, min(0.999, cov / math.sqrt(vx * vy)))

    return {
        "cagr_pct": cagr_pct,
        "lookback_years": yrs,
        "realized_vol_pct": realized_vol_pct,
        "beta_spy": beta_spy,
        "corr_spy": corr_spy,
    }


def estimate_benchmark_vol_pct(
    db: PortfolioDB,
    today_date: date,
    benchmark: str = "SPY",
    default_vol_pct: float = 14.5,
) -> float:
    """Estimates 10Y annualized volatility (%) for `benchmark` from consecutive weekly prices."""
    try:
        ten_yr_iso = date(today_date.year - 10, today_date.month, min(today_date.day, 28)).isoformat()
        series = _load_weekly_adj_series(db, benchmark, ten_yr_iso, today_date.isoformat())
        lrs: list[float] = []
        for idx in range(1, len(series)):
            d0, p0 = series[idx - 1]
            d1, p1 = series[idx]
            if 4 <= (d1 - d0).days <= 11 and p0 > 0 and p1 > 0:
                lr = math.log(p1 / p0)
                if abs(lr) <= 0.85:
                    lrs.append(lr)
        if len(lrs) >= 20:
            sd_wk = statistics.stdev(lrs)
            return round(max(5.0, min(40.0, sd_wk * math.sqrt(52.0) * 100.0)), 2)
    except Exception:
        pass
    return default_vol_pct


def _find_configured_fixed_rate(
    cfg: dict[str, Any],
    account_id: str,
    security_id: str,
    symbol_or_code: str,
    asset_class: str,
) -> float | None:
    """Checks `sim_asset_rates` and `sim_buckets` for an explicit contractual fixed rate (e.g. cash sweep or MYGA)."""
    rates = cfg.get("sim_asset_rates") or {}
    buckets = cfg.get("sim_buckets") or {}
    ac = str(asset_class or "").upper()
    code_low = str(symbol_or_code or "").lower()

    if ac in ("CASH", "MONEY_MARKET"):
        cs = rates.get("cash_sweeps") or buckets.get("cash_sweeps") or {}
        if str(cs.get("rate_model") or "").lower() == "fixed" and cs.get("annual_rate_pct") is not None:
            return float(cs["annual_rate_pct"])
        return None

    # Match contract number or account suffix on `rate_model == 'fixed'` buckets (excluding muni sleeves which have per-bond YTM)
    if ac in ("FIXED_ANNUITY", "MYGA", "CD"):
        for bid, meta in {**buckets, **rates}.items():
            if not isinstance(meta, dict):
                continue
            b_meta = {**(buckets.get(bid) or {}), **(rates.get(bid) or {})}
            if str(b_meta.get("rate_model") or "").lower() != "fixed":
                continue
            if code_low and code_low in bid.lower() and b_meta.get("annual_rate_pct") is not None:
                return float(b_meta["annual_rate_pct"])
    return None


def _compute_trailing_yield_or_return(
    db: PortfolioDB,
    quote_symbol: str,
    one_yr_iso: str,
    three_yr_iso: str,
    end_iso: str,
) -> dict[str, Any] | None:
    """
    Computes trailing 1Y cash distribution yield for fixed-income / cash benchmark ETFs from
    `(adj_close / close_price)` in `market_prices` (stripping mark-to-market duration swings),
    falling back to 1Y T-bill total return or 3Y annualized total return in synthetic fixtures.
    """
    rows_1y = db.conn.execute(
        """
        SELECT price_date, close_price, adj_close
          FROM market_prices
         WHERE quote_symbol = ?
           AND price_date >= ?
           AND price_date <= ?
         ORDER BY price_date ASC
        """,
        (quote_symbol, one_yr_iso, end_iso),
    ).fetchall()
    valid_1y = [
        (
            _safe_parse_iso(r["price_date"]),
            float(r["close_price"]),
            float(r["adj_close"]) if r["adj_close"] is not None and float(r["adj_close"]) > 0 else float(r["close_price"]),
        )
        for r in rows_1y
        if _safe_parse_iso(r["price_date"]) is not None and r["close_price"] is not None and float(r["close_price"]) > 0
    ]
    if len(valid_1y) >= 2:
        d0, c0, a0 = valid_1y[0]
        d1, c1, a1 = valid_1y[-1]
        assert d0 is not None and d1 is not None
        yrs = (d1 - d0).days / 365.2425
        if yrs >= (MIN_HISTORY_DAYS_FOR_CAGR / 365.2425) and c0 > 0 and a0 > 0 and c1 > 0 and a1 > 0:
            price_ratio = c1 / c0
            tot_ratio = a1 / a0
            if abs(tot_ratio - price_ratio) > 1e-4:
                dist_ratio = tot_ratio / price_ratio
                if dist_ratio > 0:
                    yld_pct = (math.pow(dist_ratio, 1.0 / yrs) - 1.0) * 100.0
                    return {
                        "symbol": quote_symbol,
                        "rate_pct": yld_pct,
                        "years": yrs,
                        "detail": (
                            f"Trailing 1-year cash income yield paid by {quote_symbol} from "
                            f"{d0.isoformat()} to {d1.isoformat()} (excluding bond price swings)"
                        ),
                    }
            if quote_symbol in ("BIL", "SHV", "SGOV"):
                yld_pct = (math.pow(tot_ratio, 1.0 / yrs) - 1.0) * 100.0
                return {
                    "symbol": quote_symbol,
                    "rate_pct": yld_pct,
                    "years": yrs,
                    "detail": (
                        f"Trailing 1-year interest yield on {quote_symbol} short-term Treasury bills from "
                        f"{d0.isoformat()} to {d1.isoformat()}"
                    ),
                }

    ser_3y = _load_weekly_adj_series(db, quote_symbol, three_yr_iso, end_iso)
    if len(ser_3y) >= 2:
        d0, p0 = ser_3y[0]
        d1, p1 = ser_3y[-1]
        yrs = (d1 - d0).days / 365.2425
        if yrs >= (MIN_HISTORY_DAYS_FOR_CAGR / 365.2425) and p0 > 0 and p1 > 0:
            cagr_3y = (math.pow(p1 / p0, 1.0 / yrs) - 1.0) * 100.0
            return {
                "symbol": quote_symbol,
                "rate_pct": cagr_3y,
                "years": yrs,
                "detail": f"{quote_symbol} peer benchmark ({_market_period(quote_symbol, ser_3y)})",
            }
    return None


def _build_peer_benchmarks(
    db: PortfolioDB,
    ten_yr_iso: str,
    three_yr_iso: str,
    one_yr_iso: str,
    today_iso: str,
    spy_rets: dict[date, float],
    spy_var: float,
) -> dict[str, dict[str, Any]]:
    """Precomputes canonical Tier-2 Peer Market Benchmarks across equity, hybrid, bond, muni, real, and cash classes."""
    peers: dict[str, dict[str, Any]] = {}

    def _first_10y(candidates: tuple[str, ...], label: str) -> dict[str, Any] | None:
        for sym in candidates:
            ser = _load_weekly_adj_series(db, sym, ten_yr_iso, today_iso)
            m = _compute_public_metrics(ser, spy_rets, spy_var)
            if m["cagr_pct"] is not None:
                return {
                    "symbol": sym,
                    "rate_pct": float(m["cagr_pct"]),
                    "years": float(m["lookback_years"] or 0.0),
                    "detail": f"{label} peer index ({_market_period(sym, ser)})",
                }
        return None

    def _first_yield(candidates: tuple[str, ...], label: str) -> dict[str, Any] | None:
        for sym in candidates:
            y = _compute_trailing_yield_or_return(db, sym, one_yr_iso, three_yr_iso, today_iso)
            if y is not None:
                return {
                    **y,
                    "detail": f"{label} ({y['detail']})",
                }
        return None

    us_eq = _first_10y(("SPY", "VOO", "IVV", "VTI", "ITOT", "VTSAX"), "US large-cap equity")
    if us_eq is not None:
        peers["us_equity"] = us_eq

    glob_eq = _first_10y(("ACWI", "VT", "VXUS", "IEFA"), "Global equity")
    if glob_eq is not None:
        peers["global_equity"] = glob_eq
    elif us_eq is not None:
        peers["global_equity"] = us_eq

    real_div = _first_10y(("GLD", "IAU", "DBC"), "Real assets / gold")
    if real_div is not None:
        peers["real_diversifier"] = real_div

    core_bond = _first_yield(("AGG", "BND", "VBTLX", "BNDX"), "Core investment-grade bond index")
    if core_bond is not None:
        peers["core_bond"] = core_bond

    muni_bond = _first_yield(("MUB", "VTEB"), "National tax-exempt municipal bond index")
    if muni_bond is not None:
        peers["muni_bond"] = muni_bond

    cash_tbill = _first_yield(("BIL", "SHV", "SGOV"), "Short-term US T-bill index")
    if cash_tbill is not None:
        peers["cash_tbill"] = cash_tbill

    if us_eq is not None:
        bond_rate = float(core_bond["rate_pct"]) if core_bond is not None else float(RISK_CLASSES["fixed_income"]["benchmark_return_pct"])
        bond_lbl = f"{core_bond['symbol']} bonds ({bond_rate:.2f}%/yr income yield)" if core_bond is not None else f"bonds ({bond_rate:.2f}%/yr benchmark)"
        bal_rate = 0.60 * float(us_eq["rate_pct"]) + 0.40 * bond_rate
        peers["hybrid_buffered"] = {
            "symbol": f"60/40 {us_eq['symbol']}",
            "rate_pct": bal_rate,
            "years": us_eq["years"],
            "detail": (
                f"Blended 60/40 balanced index combining 60% US stocks ({us_eq['symbol']}, {us_eq['rate_pct']:.2f}%/yr over 10 years) "
                f"and 40% {bond_lbl} as of {today_iso}"
            ),
        }

    return peers


def _resolve_peer_benchmark_for_holding(
    ac: str,
    rc: str,
    cname: str,
    peer_benchmarks: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if ac in ("CASH", "MONEY_MARKET") or rc == "cash_fixed":
        return peer_benchmarks.get("cash_tbill")
    if "MUNI" in ac:
        return peer_benchmarks.get("muni_bond")
    if rc == "fixed_income":
        return peer_benchmarks.get("core_bond")
    if rc == "hybrid_buffered":
        return peer_benchmarks.get("hybrid_buffered")
    if rc == "real_diversifier":
        return peer_benchmarks.get("real_diversifier")
    if rc == "broad_equity" and _GLOBAL_EQ_NAME_RE.search(cname or ""):
        return peer_benchmarks.get("global_equity")
    return peer_benchmarks.get("us_equity")


def _enrich_single_holding(
    account_id: str,
    sec: dict[str, Any],
    snap: dict[str, Any] | None,
    *,
    eff_cfg: dict[str, Any],
    today: date,
    today_iso: str,
    anchors_map: dict[tuple[str, str], dict[str, Any]],
    first_snap_map: dict[tuple[str, str], str],
    principal_overrides: dict[str, Any],
    pub_cache: dict[str, dict[str, Any]],
    peer_benchmarks: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    """
    Evaluates the 3-tier specificity-to-equilibrium forecast hierarchy for a holding:
      - Tier 1 (`Holding or contractual rate`): Security 10Y market return, forward contractual APY/YTM,
        or non-public contract inception/reported return (with secondary account-lot comparisons kept in Tier 1).
      - Tier 2 (`Peer market benchmark`): Empirical 10Y peer index total return (equities/hybrids/real assets)
        or trailing 1Y peer index distribution yield (bonds/cash).
      - Tier 3 (`Asset-class equilibrium`): Secular 50-100 yr asset-class equilibrium assumption.
    """
    sec_id = str(sec["security_id"])
    sym = str(sec.get("symbol_or_code") or "")
    cname = str(sec.get("canonical_name") or sec_id)
    ac = str(sec.get("asset_class") or "EQUITY").upper()
    rc = str(sec.get("risk_class") or "").strip().lower()
    if rc not in VALID_RISK_CLASSES:
        rc = default_risk_class_for_asset_class(ac)
    is_scrape = int(sec.get("is_scrapeable") or 0) == 1
    qsym = str(sec.get("quote_symbol") or sym).strip()

    anc = anchors_map.get((account_id, sec_id)) or {}
    if account_id:
        meta = {**_metadata(anc.get("metadata_json")), **_metadata((snap or {}).get("details_json"))}
    else:
        meta = _metadata(sec.get("metadata_json"))

    mv = float(snap["stmt_market_value"]) if snap and snap.get("stmt_market_value") is not None else float(anc.get("market_value") or 0.0)
    cb = float(snap["cost_basis"]) if snap and snap.get("cost_basis") is not None else None
    yld = float(snap["yield_pct"]) if snap and snap.get("yield_pct") is not None else None
    est_inc = float(snap["est_annual_income"]) if snap and snap.get("est_annual_income") is not None else None
    credited = meta.get("credited_rate_pct")
    rep_ret = meta.get("reported_return_pct")
    rep_win = str(meta.get("reported_return_window") or "statement")

    # Principal / inception date lookup (anchor -> metadata -> principal_overrides -> earliest snapshot)
    ov_entry = principal_overrides.get(f"{account_id}:{sec_id}") or {}
    init_prem = anc.get("initial_premium")
    basis_ref = None
    basis_label = ""
    for value, label in ((init_prem, "initial premium"), (ov_entry.get("principal"), "configured principal"),
                         (cb, "cost basis"), (meta.get("orig_cost"), "original statement cost")):
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(amount) and amount > 0:
            basis_ref, basis_label = amount, label
            break
    start_iso = (
        anc.get("issue_date")
        or meta.get("acquisition_date")
        or meta.get("issue_date")
        or meta.get("inception_date")
        or ov_entry.get("inception_date")
        or first_snap_map.get((account_id, sec_id))
    )
    end_iso = (
        anc.get("value_date")
        or (snap.get("as_of_date") if snap else None)
        or today_iso
    )
    s_dt = _safe_parse_iso(start_iso)
    e_dt = _safe_parse_iso(end_iso) or today
    age_days = (e_dt - s_dt).days if s_dt else 0
    age_yrs = age_days / 365.2425 if age_days > 0 else 0.0

    pm = pub_cache.get(qsym, {}) if is_scrape else {}
    yrs = float(pm.get("lookback_years") or 0)
    candidates: list[dict[str, Any]] = []
    public_cash = bool(pm) and rc == "cash_fixed"

    # Order within each tier sets the primary (`Used`) selection while retaining `Also available` comparisons.
    def add(tier: int, source: str, raw: float, detail: str, confidence: str,
            *, fixed: bool = False, years: float | None = None, clamp_class: str = "",
            market_risk: bool = True) -> None:
        gp, capped = clamp_forecast_return(float(raw), clamp_class or rc)
        if capped:
            detail += f" (capped from {raw:+.2f}% to {gp:+.2f}% to stay within realistic long-term bounds)"
        metrics = pm if market_risk else {}
        zero_risk = fixed or (public_cash and market_risk)
        candidates.append({
            "tier": tier, "return_source": source, "projected_return_pct": gp,
            "source_detail": detail, "confidence": confidence, "lookback_years": years,
            "realized_vol_pct": 0.0 if zero_risk else metrics.get("realized_vol_pct"),
            "beta_spy": 0.0 if zero_risk else metrics.get("beta_spy"),
            "corr_spy": 0.0 if zero_risk else metrics.get("corr_spy"),
        })

    statement_date = (snap or {}).get("as_of_date")
    statement = f"statement dated {statement_date}" if statement_date else "statement date unavailable"
    cash = ac in ("CASH", "MONEY_MARKET")
    bond = ac in ("MUNI_BOND", "BOND", "TREASURY", "TIPS", "CD")
    annuity = ac in ("FIXED_ANNUITY", "MYGA")
    is_muni = "MUNI" in ac

    # --- Tier 1: Holding-Specific Market, Contractual, or Contract-Inception Return ---
    if public_cash and credited is not None:
        add(1, "CONTRACT_CREDITED_RATE", float(credited), f"Credited cash APY, {statement}", "HIGH", fixed=True, years=1.0)
    if public_cash and yld is not None and yld >= 0.1:
        add(1, "STATEMENT_YTM", yld, f"Reported cash yield, {statement}", "HIGH", fixed=True, years=1.0)
    if public_cash and pm.get("cagr_1y_pct") is not None:
        add(1, "PUBLIC_1Y_CASH_YIELD", pm["cagr_1y_pct"], pm["cash_period"], "HIGH", fixed=True, years=1.0)
    if is_scrape and rc == "fixed_income" and yld is not None and 0.1 <= yld <= 15:
        add(1, "STATEMENT_YTM", yld, f"Reported SEC yield or yield to maturity, {statement}", "HIGH", years=yrs)

    cagr = pm.get("cagr_pct")
    if cagr is not None:
        source = "PUBLIC_10Y_TOTAL_RETURN" if yrs >= 3 else "PUBLIC_SHORT_TOTAL_RETURN"
        add(1, source, cagr, pm["market_period"], "HIGH" if yrs >= 5 else "MEDIUM", years=yrs, fixed=public_cash)

    if credited is not None and not public_cash and (cash or bond or annuity or ac.endswith("_ANNUITY")):
        add(1, "CONTRACT_CREDITED_RATE", float(credited), f"Contractual credited rate, {statement}", "HIGH",
            fixed=cash or annuity, years=(age_yrs or None) if annuity else None,
            clamp_class="cash_fixed" if cash or annuity else rc, market_risk=False)
    if cash and not public_cash and yld is not None and yld >= 0.25:
        add(1, "STATEMENT_YTM", yld, f"Reported cash yield, {statement}", "HIGH", fixed=True, clamp_class="cash_fixed")
    if cash or annuity or ac == "CD":
        cfg_rate = _find_configured_fixed_rate(eff_cfg, account_id, sec_id, sym, ac)
        if cfg_rate is not None:
            add(1, "CONTRACT_CREDITED_RATE", cfg_rate, f"Configured fixed rate as of {today_iso}", "HIGH",
                fixed=True, clamp_class="cash_fixed", years=(age_yrs or None) if annuity else None)
    if bond:
        if yld is not None and not any(c["return_source"] == "STATEMENT_YTM" for c in candidates):
            add(1, "STATEMENT_YTM", yld, f"Yield to maturity if held to redemption, from {statement}", "HIGH", market_risk=False)
        if est_inc is not None and est_inc > 0 and mv > 0:
            add(1, "STATEMENT_COUPON_YIELD", est_inc / mv * 100,
                f"Annual interest payments as a percentage of market value, from {statement}", "MEDIUM", market_risk=False)

    if (cash or public_cash or (is_scrape and rc == "fixed_income")) and yld is not None and 0 <= yld < 0.25:
        if not any(c["return_source"] == "STATEMENT_YTM" for c in candidates):
            add(1, "STATEMENT_YTM", yld, f"Reported interest yield, from {statement}", "HIGH", fixed=cash or public_cash)

    return_statement = statement
    if rep_ret is None and snap and snap.get("reported_history"):
        rep_ret, rep_win, reported_date = snap["reported_history"]
        return_statement = f"statement dated {reported_date}"
    if rep_ret is not None:
        add(1, "STATEMENT_REPORTED_RETURN", float(rep_ret),
            f"Annualized return reported on your statement ({rep_win}), {return_statement}", "HIGH", years=age_yrs or None)
    if basis_ref is not None and basis_ref > 0 and mv > 0 and age_days >= MIN_HISTORY_DAYS_FOR_CAGR:
        # Statement inception / cost-basis CAGR supplies Tier 1 for non-public contracts (FIAs, RILAs)
        # and stays inside Tier 1 as an informational comparison when market/contractual Tier 1 exists.
        log_return = (math.log(mv) - math.log(basis_ref)) / age_yrs
        if log_return < 700:
            raw = math.expm1(log_return) * 100
            source = "STATEMENT_INCEPTION_CAGR" if basis_label == "initial premium" else "STATEMENT_COST_BASIS_CAGR"
            dated_origin = any((anc.get("issue_date"), meta.get("acquisition_date"), meta.get("issue_date"),
                                meta.get("inception_date"), ov_entry.get("inception_date")))
            start_label = "purchase / issue date" if dated_origin else "first recorded statement"
            cum_pct = (mv / basis_ref - 1.0) * 100.0
            add(1, source, raw,
                f"Average annual growth from your {basis_label} to statement value ({cum_pct:+.1f}% total) "
                f"between {s_dt.isoformat()} ({start_label}) and {e_dt.isoformat()} ({age_yrs:.1f} years)",
                "HIGH" if age_yrs >= 2 else "MEDIUM", years=age_yrs)

    has_tier1 = any(c["tier"] == 1 for c in candidates)

    # --- Tier 2: Empirical Peer / Parent Market Benchmark ---
    peer_info = _resolve_peer_benchmark_for_holding(ac, rc, cname, peer_benchmarks)
    if peer_info is not None:
        add(
            2,
            "PEER_MARKET_BENCHMARK",
            float(peer_info["rate_pct"]),
            str(peer_info["detail"]),
            "MEDIUM",
            fixed=cash or (rc == "cash_fixed"),
            years=peer_info.get("years"),
            clamp_class="cash_fixed" if (cash or rc == "cash_fixed") else rc,
            market_risk=not (cash or bond or annuity),
        )

    # --- Tier 3: Secular Asset-Class Equilibrium ---
    bench_rc = "cash_fixed" if cash else rc
    if cash:
        bench = float(RISK_CLASSES["cash_fixed"]["benchmark_return_pct"])
        class_detail = format_equilibrium_detail("cash_fixed", today_iso)
        if not candidates:
            class_detail += ". Used when a statement yield or configured interest rate is unavailable"
        add(3, "RISK_CLASS_BENCHMARK", bench, class_detail, "MEDIUM", fixed=True)
    else:
        if is_muni:
            bench = float(MUNI_EQUILIBRIUM_RETURN_PCT)
            class_detail = format_equilibrium_detail(bench_rc, today_iso, is_muni=True)
        else:
            bench = float(RISK_CLASSES[bench_rc]["benchmark_return_pct"])
            class_detail = format_equilibrium_detail(bench_rc, today_iso)
        if not has_tier1:
            reason = f"only {age_days} days of holding history" if 0 < age_days < MIN_HISTORY_DAYS_FOR_CAGR else "no qualifying market or statement rate"
            if pm.get("market_period") and yrs < MIN_HISTORY_DAYS_FOR_CAGR / 365.2425:
                reason += f". Available price dates {pm['first_price_date']} to {pm['last_price_date']} fall short of 180 days"
            fallback_note = f"{class_detail if not candidates else 'Peer market benchmark'}. Used because there is {reason}"
            if not candidates:
                class_detail += f". Used because there is {reason}"
            if account_id:
                warnings.append(f"LOW confidence forecast for {cname} ({account_id}:{sec_id}, {rc}): {fallback_note}")
        add(3, "RISK_CLASS_BENCHMARK", bench, class_detail, "LOW", years=age_yrs or None)

    candidates.sort(key=lambda entry: entry["tier"])
    selected = candidates[0].copy()
    selected["growth_evidence"] = [
        {key: c[key] for key in ("tier", "projected_return_pct", "source_detail")}
        for c in candidates
    ]
    selected["growth_evidence"][0]["version"] = EVIDENCE_VERSION
    return selected


def enrich_holding_forecasts(
    db: PortfolioDB,
    cfg: dict[str, Any] | None = None,
    today_date: date | None = None,
    *,
    sync_prices: bool = True,
    force_price_sync: bool = False,
    skip_symbols: set[str] | None = None,
    max_workers: int = 20,
) -> dict[str, Any]:
    """
    Computes and upserts `holding_forecasts_baseline` rows for all open holdings in `db`
    (plus global `account_id=''` baselines per security and grant ticker).

    Returns summary dict:
      `{"holdings_enriched": int, "symbols_synced_10y": int, "low_confidence_warnings": list[str], "by_source": dict[str, int]}`
    """
    eff_cfg = cfg or {}
    today = today_date or date.today()
    ten_yr_start = date(today.year - 10, today.month, min(today.day, 28))
    three_yr_start = date(today.year - 3, today.month, min(today.day, 28))
    one_yr_start = today - timedelta(days=370)
    ten_yr_iso = ten_yr_start.isoformat()
    three_yr_iso = three_yr_start.isoformat()
    one_yr_iso = one_yr_start.isoformat()
    today_iso = today.isoformat()

    # 1. Gather all securities & latest OPEN position snapshots per account
    sec_rows = db.conn.execute("SELECT * FROM securities").fetchall()
    securities_by_id: dict[str, dict[str, Any]] = {str(r["security_id"]): dict(r) for r in sec_rows}

    pos_rows = db.conn.execute(
        """
        SELECT ps.*
          FROM position_snapshots ps
          JOIN (
              SELECT account_id, MAX(as_of_date) AS max_dt
                FROM position_snapshots
               GROUP BY account_id
          ) latest
            ON ps.account_id = latest.account_id
           AND ps.as_of_date = latest.max_dt
         WHERE ps.status = 'OPEN'
        """
    ).fetchall()

    # Latest valuation anchors per `(account_id, security_id)`
    anc_rows = db.conn.execute(
        """
        SELECT va.*
          FROM valuation_anchors va
          JOIN (
              SELECT account_id, security_id, MAX(value_date) AS max_vd
                FROM valuation_anchors
               GROUP BY account_id, security_id
          ) latest
            ON va.account_id = latest.account_id
           AND va.security_id = latest.security_id
           AND va.value_date = latest.max_vd
        """
    ).fetchall()
    anchors_map: dict[tuple[str, str], dict[str, Any]] = {
        (str(r["account_id"]), str(r["security_id"])): dict(r) for r in anc_rows
    }

    # Earliest observed snapshot per `(account_id, security_id)` for fallback holding age
    first_snap_rows = db.conn.execute(
        """
        SELECT account_id, security_id, MIN(as_of_date) AS min_dt
          FROM position_snapshots
         WHERE status = 'OPEN'
         GROUP BY account_id, security_id
        """
    ).fetchall()
    first_snap_map: dict[tuple[str, str], str] = {
        (str(r["account_id"]), str(r["security_id"])): str(r["min_dt"]) for r in first_snap_rows
    }

    # 2. 10-Year Yahoo `adj_close` backfill for scrapeable symbols not yet 10Y-enriched
    scrapeable_syms: set[str] = {
        str(s.get("quote_symbol") or s["symbol_or_code"]).strip()
        for s in securities_by_id.values()
        if s.get("is_scrapeable") and (s.get("quote_symbol") or s.get("symbol_or_code"))
    }
    prim_sym = str(eff_cfg.get("primary_equity_symbol") or "").strip().upper()
    if prim_sym:
        scrapeable_syms.add(prim_sym)

    symbols_synced_10y = 0
    if sync_prices and (scrapeable_syms or securities_by_id):
        # The price cache checks coverage per ticker, including tickers previously
        # enriched from a statement while market history was unavailable, plus canonical peer benchmarks.
        syms_to_sync = sorted(scrapeable_syms | set(PEER_BENCHMARK_SYMBOLS))
        if syms_to_sync and skip_symbols:
            syms_to_sync = [s for s in syms_to_sync if s not in skip_symbols]
        if syms_to_sync:
            sync_res = sync_market_prices(
                db=db,
                symbols=syms_to_sync,
                start_date=ten_yr_start,
                end_date=today,
                force_full=force_price_sync,
                max_workers=max_workers,
            )
            symbols_synced_10y = int(sync_res.get("symbols_queried") or 0)

    # 3. Precompute SPY 10Y benchmark return, weekly log-return variance, & canonical Tier-2 Peer Benchmarks
    spy_series = _load_weekly_adj_series(db, "SPY", ten_yr_iso, today_iso)
    spy_rets = _compute_weekly_log_returns(spy_series)
    spy_vals = list(spy_rets.values())
    spy_var = statistics.variance(spy_vals) if len(spy_vals) >= 20 else 0.0
    spy_metrics = _compute_public_metrics(spy_series, spy_rets, spy_var)
    spy_10y_cagr = float(spy_metrics["cagr_pct"]) if spy_metrics["cagr_pct"] is not None else float(
        RISK_CLASSES["broad_equity"]["benchmark_return_pct"]
    )
    peer_benchmarks = _build_peer_benchmarks(
        db=db,
        ten_yr_iso=ten_yr_iso,
        three_yr_iso=three_yr_iso,
        one_yr_iso=one_yr_iso,
        today_iso=today_iso,
        spy_rets=spy_rets,
        spy_var=spy_var,
    )

    # Cache per-symbol public metrics so multi-account tickers (like GOOG) compute once
    pub_cache: dict[str, dict[str, Any]] = {}
    for qsym in sorted(scrapeable_syms):
        ser_10y = _load_weekly_adj_series(db, qsym, ten_yr_iso, today_iso)
        ser_1y = [pt for pt in ser_10y if pt[0].isoformat() >= one_yr_iso]
        m10 = _compute_public_metrics(ser_10y, spy_rets, spy_var)
        cagr_1y: float | None = None
        if len(ser_1y) >= 2:
            y1 = (ser_1y[-1][0] - ser_1y[0][0]).days / 365.2425
            if y1 >= 0.25 and ser_1y[0][1] > 0:
                cagr_1y = (math.pow(ser_1y[-1][1] / ser_1y[0][1], 1.0 / y1) - 1.0) * 100.0
        pub_cache[qsym] = {**m10, "cagr_1y_pct": cagr_1y, "market_period": _market_period(qsym, ser_10y),
                           "cash_period": _market_period(qsym, ser_1y),
                           "first_price_date": ser_10y[0][0] if ser_10y else None,
                           "last_price_date": ser_10y[-1][0] if ser_10y else None}

    principal_overrides = eff_cfg.get("principal_overrides") or {}
    computed_ts = utc_now_iso()
    by_source: dict[str, int] = {}
    warnings: list[str] = []
    enriched_count = 0
    global_fallbacks: dict[str, dict[str, Any]] = {}
    enrich_kwargs = {
        "eff_cfg": eff_cfg,
        "today": today,
        "today_iso": today_iso,
        "anchors_map": anchors_map,
        "first_snap_map": first_snap_map,
        "principal_overrides": principal_overrides,
        "pub_cache": pub_cache,
        "peer_benchmarks": peer_benchmarks,
        "warnings": warnings,
    }

    def coverage(result: dict[str, Any]) -> tuple[bool, ...]:
        tiers = {entry["tier"] for entry in result["growth_evidence"]}
        return (1 in tiers, 2 in tiers, result["confidence"] == "HIGH")

    # Keep the date attached to the historical return even when later statements omit it.
    last_reported = {}
    for row in db.conn.execute("""
        SELECT account_id, security_id, as_of_date, details_json FROM position_snapshots
        WHERE status = 'OPEN' AND details_json LIKE '%"reported_return_pct"%'
        ORDER BY as_of_date
    """):
        metadata = _metadata(row["details_json"])
        if metadata.get("reported_return_pct") is not None:
            last_reported[(row["account_id"], row["security_id"])] = (
                metadata["reported_return_pct"], metadata.get("reported_return_window") or "statement", row["as_of_date"])

    # Upsert per-account open holdings
    for pr in pos_rows:
        aid = str(pr["account_id"])
        sid = str(pr["security_id"])
        sec = securities_by_id.get(sid)
        if not sec:
            continue
        rc = str(sec.get("risk_class") or default_risk_class_for_asset_class(sec.get("asset_class") or "EQUITY"))
        snapshot = dict(pr)
        reported = last_reported.get((aid, sid))
        if reported:
            snapshot["reported_history"] = reported
        res = _enrich_single_holding(aid, sec, snapshot, **enrich_kwargs)
        db.upsert_holding_forecast_baseline(
            account_id=aid,
            security_id=sid,
            risk_class=rc,
            projected_return_pct=res["projected_return_pct"],
            realized_vol_pct=res["realized_vol_pct"],
            beta_spy=res["beta_spy"],
            corr_spy=res["corr_spy"],
            lookback_years=res["lookback_years"],
            return_source=res["return_source"],
            confidence=res["confidence"],
            source_detail=res["source_detail"],
            growth_evidence=res["growth_evidence"],
            computed_at_iso=computed_ts,
        )
        enriched_count += 1
        by_source[res["return_source"]] = by_source.get(res["return_source"], 0) + 1
        prior = global_fallbacks.get(sid)
        if prior is None or coverage(res) > coverage(prior):
            global_fallbacks[sid] = {"risk_class": rc, **res}

    # Also upsert `account_id=''` global baselines for every security (preferring pure market/contractual
    # Tier-1 baselines so account-specific purchase-lot returns do not leak across accounts).
    for sid, sec in securities_by_id.items():
        rc = str(sec.get("risk_class") or default_risk_class_for_asset_class(sec.get("asset_class") or "EQUITY"))
        clean_global = _enrich_single_holding("", sec, None, **enrich_kwargs)
        has_clean_t1 = any(e["tier"] == 1 for e in clean_global["growth_evidence"])
        res = clean_global if has_clean_t1 else (global_fallbacks.get(sid) or clean_global)
        db.upsert_holding_forecast_baseline(
            account_id="",
            security_id=sid,
            risk_class=rc,
            projected_return_pct=res["projected_return_pct"],
            realized_vol_pct=res["realized_vol_pct"],
            beta_spy=res["beta_spy"],
            corr_spy=res["corr_spy"],
            lookback_years=res["lookback_years"],
            return_source=res["return_source"],
            confidence=res["confidence"],
            source_detail=res["source_detail"],
            growth_evidence=res["growth_evidence"],
            computed_at_iso=computed_ts,
        )

    db.conn.commit()
    return {
        "holdings_enriched": enriched_count,
        "symbols_synced_10y": symbols_synced_10y,
        "spy_10y_cagr_pct": round(spy_10y_cagr, 2),
        "spy_vol_pct": round(float(spy_metrics["realized_vol_pct"] or 14.5), 2),
        "low_confidence_warnings": warnings,
        "by_source": by_source,
    }
