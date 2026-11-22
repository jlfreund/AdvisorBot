#!/usr/bin/env python3
"""Parsers for Morgan Stanley at Work quarterly statements & unvested equity award schedules."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from config import load_config, yahoo_symbol
from ingest.models import ParsedAdjustment, ParsedGSUTranche, ParsedPosition, ParsedStatement


def _parse_mmddyy(s: str) -> str:
    return datetime.strptime(s.strip(), "%m/%d/%y").strftime("%Y-%m-%d")


def _parse_dd_mon_yyyy(s: str) -> str:
    return datetime.strptime(s.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")


def _grant_forecast_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    c = cfg or {}
    gf = c.get("grant_forecast") or c.get("gsu_forecast") or {}
    return gf if isinstance(gf, dict) else {}


def _fallback_primary_symbol(cfg: dict[str, Any] | None) -> str:
    gsu_cfg = _grant_forecast_cfg(cfg)
    sym = str(gsu_cfg.get("symbol") or (cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    if sym:
        return sym
    try:
        loaded = load_config()
        loaded_gf = _grant_forecast_cfg(loaded)
        return str(loaded_gf.get("symbol") or loaded.get("primary_equity_symbol") or "").strip().upper()
    except Exception:
        return ""


def _resolve_statement_ticker_and_company(
    text: str,
    issuer_desc: str,
    cfg: dict[str, Any] | None,
) -> tuple[str, str]:
    gsu_cfg = _grant_forecast_cfg(cfg)
    companies = ((cfg or {}).get("stock_grants") or {}).get("companies") or []

    sym_m = re.search(r"(?:Ticker|Stock\s+Symbol|Symbol):\s*([A-Z]{1,6})\b", text)
    paren_m = re.search(r"\(([A-Z]{1,6})\)", issuer_desc) if issuer_desc else None
    parsed_sym = (sym_m.group(1) if sym_m else (paren_m.group(1) if paren_m else "")).strip().upper()

    issuer_clean = issuer_desc.strip()
    issuer_low = issuer_clean.lower()
    if not parsed_sym and issuer_low:
        for comp in companies:
            if not isinstance(comp, dict):
                continue
            c_sym = str(comp.get("symbol") or "").strip().upper()
            c_name = str(comp.get("company_name") or "").strip().lower()
            c_short = str(comp.get("short_name") or "").strip().lower()
            if c_sym and ((c_name and c_name in issuer_low) or (c_short and c_short in issuer_low)):
                parsed_sym = c_sym
                break

    ticker = parsed_sym or _fallback_primary_symbol(cfg)
    if not ticker and issuer_clean:
        first_word = re.sub(r"[^A-Za-z0-9]", "", issuer_clean.split()[0]).upper()
        ticker = first_word[:5]

    company_name = (
        issuer_clean.title()
        if issuer_clean
        else str(gsu_cfg.get("company_name") or ticker or "Equity Plan").strip()
    )
    return ticker, company_name


def parse_ms_quarterly(pages: list[str], cfg: dict[str, Any]) -> ParsedStatement:
    """
    Parses stock-plan quarterly PDF statements (Holdings & Release Activity).
    PORTABILITY DO (SA2-19): Extract ticker, issuer name, and award type dynamically from statement text
    or `cfg["gsu_forecast"]` / `cfg["primary_equity_symbol"]`; never hardcode a single employer's symbol or award label.
    """
    p1 = pages[0]
    full_txt = "\n".join(pages)

    gf_cfg = _grant_forecast_cfg(cfg)
    default_acct_id = gf_cfg.get("target_vested_account_id") or "MS_STOCK_PLAN"
    acct_m = re.search(r"Account Number:\s*([A-Z0-9-]+)", p1)
    raw_account_id = acct_m.group(1) if acct_m else default_acct_id
    account_id = raw_account_id
    acct_cfg = (cfg.get("accounts") or {}).get(account_id) or (cfg.get("accounts") or {}).get(raw_account_id) or {}
    account_name = acct_cfg.get("account_name") or f"Stock Plan ({account_id})"
    account_type = acct_cfg.get("account_type") or "Global Stock Plan Services"
    tax_status = acct_cfg.get("tax_status") or "TAXABLE"
    display_order = int(acct_cfg.get("display_order", 10))

    dates_m = re.findall(r"\(as of (\d{1,2}/\d{1,2}/\d{2})\)", p1)
    if len(dates_m) >= 2:
        period_start = _parse_mmddyy(dates_m[0])
        as_of_date = _parse_mmddyy(dates_m[1])
    elif len(dates_m) == 1:
        period_start = None
        as_of_date = _parse_mmddyy(dates_m[0])
    else:
        # PORTABILITY INVARIANT (SA3-13): Fail loudly rather than falling back to a hardcoded statement date.
        raise ValueError("Failed to parse statement period `(as of MM/DD/YY)` header dates.")

    sh_m = re.search(r"Number of Shares\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)", p1)
    pr_m = re.search(r"Share Price\s+\$([\d,]+\.\d+)\s+\$([\d,]+\.\d+)", p1)
    sv_m = re.search(r"Share Value\s+\$([\d,]+\.\d+)\s+\$([\d,]+\.\d+)", p1)
    cv_m = re.search(r"Cash Value\s+\$([\d,]+\.\d+)\s+\$([\d,]+\.\d+)", p1)
    tot_m = re.search(r"Total Account Value\s+\$([\d,]+\.\d+)\s+\$([\d,]+\.\d+)", p1)

    if not (sh_m and pr_m and sv_m and cv_m and tot_m):
        raise ValueError("Failed to parse Share Purchase and Holdings Summary table.")

    open_shares = float(sh_m.group(1).replace(",", ""))
    close_shares = float(sh_m.group(2).replace(",", ""))
    close_price = float(pr_m.group(2).replace(",", ""))
    close_share_val = float(sv_m.group(2).replace(",", ""))
    open_cash = float(cv_m.group(1).replace(",", ""))
    close_cash = float(cv_m.group(2).replace(",", ""))
    reported_total = float(tot_m.group(2).replace(",", ""))

    calc_total = round(close_share_val + close_cash, 2)
    if abs(calc_total - reported_total) > 0.02:
        raise ValueError(f"Stock plan checksum mismatch: {calc_total} != {reported_total}")

    issuer_m = re.search(r"Issuer Description:\s*(.+)", p1)
    issuer_desc = issuer_m.group(1).strip() if issuer_m else ""
    ticker, company_title = _resolve_statement_ticker_and_company(full_txt, issuer_desc, cfg)
    aw_m = re.search(r"\b(RSU|GSU|PSU)\b", full_txt)
    award_type = (
        aw_m.group(1).upper()
        if aw_m
        else str(gf_cfg.get("award_type") or "RSU").strip().upper()
    )

    positions: list[ParsedPosition] = [
        ParsedPosition(
            security_id=f"TICKER:{ticker}",
            symbol_or_code=ticker,
            canonical_name=f"{company_title} (Vested Stock Plan)",
            asset_class="EQUITY",
            risk_class="single_stock",
            stack_bucket="goog_combined",
            is_scrapeable=True,
            quote_symbol=yahoo_symbol(ticker, cfg) if ticker else None,
            quantity=close_shares,
            stmt_price=close_price,
            market_value=close_share_val,
            value_date=as_of_date,
            metadata={"opening_shares": open_shares, "period_start": period_start, "award_type": award_type},
        ),
        ParsedPosition(
            security_id=f"CASH:{account_id}",
            symbol_or_code="USD",
            canonical_name=f"Stock Plan Cash Balance ({account_id})",
            asset_class="CASH",
            risk_class="cash_fixed",
            stack_bucket="cash_sweeps",
            is_scrapeable=False,
            quote_symbol=None,
            quantity=1.0,
            stmt_price=close_cash,
            market_value=close_cash,
            value_date=as_of_date,
            metadata={"opening_cash": open_cash},
        ),
    ]

    # Parse individual quarterly release lines so historical weeks inside the quarter have exact share steps
    rel_by_date: dict[str, float] = {}
    for rm in re.finditer(r"(\d{1,2}/\d{1,2}/\d{2})\s+Release\s+([\d,]+\.\d+)", full_txt):
        r_dt = _parse_mmddyy(rm.group(1))
        r_qty = float(rm.group(2).replace(",", ""))
        rel_by_date[r_dt] = round(rel_by_date.get(r_dt, 0.0) + r_qty, 6)

    adjustments: list[ParsedAdjustment] = []
    if period_start:
        running = open_shares
        for r_dt in sorted(rel_by_date.keys()):
            pre_q = running
            running = round(running + rel_by_date[r_dt], 6)
            adjustments.append(
                ParsedAdjustment(
                    security_id=f"TICKER:{ticker}",
                    effective_date=r_dt,
                    adj_type="GSU_RELEASE_STEP",
                    pre_quantity=pre_q,
                    post_quantity=running,
                    notes=f"Stock plan {award_type} net release +{rel_by_date[r_dt]:.3f} sh on {r_dt}",
                )
            )

    return ParsedStatement(
        doc_type="MS_QUARTERLY",
        institution="Morgan Stanley",
        account_id=account_id,
        account_name=account_name,
        account_type=account_type,
        tax_status=tax_status,
        display_order=display_order,
        period_start=period_start,
        as_of_date=as_of_date,
        reported_total=reported_total,
        positions=positions,
        adjustments=adjustments,
    )


def parse_ms_unvested_gsu(pages: list[str], cfg: dict[str, Any]) -> ParsedStatement:
    """Parses `Unvested Shares As At Date.pdf` (Restricted Shares Vesting schedule)."""
    full_txt = "\n".join(pages)

    run_m = re.search(r"report run on (\d{2}-[A-Za-z]{3}-\d{4})", full_txt)
    report_date = _parse_dd_mon_yyyy(run_m.group(1)) if run_m else datetime.now().strftime("%Y-%m-%d")

    # Map each grant ID on page to its grant date column when two DD-Mon-YYYY columns are present
    known_grant_dates: dict[str, str] = {}
    for page_txt in pages:
        g_ids = [m.group(2) for m in re.finditer(r"\b(RSU|GSU|PSU)\s+([A-Za-z0-9-]+)", page_txt)]
        all_dates = re.findall(r"\b(\d{2}-[A-Za-z]{3}-\d{4})\b", page_txt)
        if run_m and all_dates and all_dates[0] == run_m.group(1):
            all_dates = all_dates[1:]
        if g_ids and len(all_dates) == 2 * len(g_ids):
            grant_col = all_dates[len(g_ids) :]
            for gid, gdt in zip(g_ids, grant_col):
                known_grant_dates[gid] = _parse_dd_mon_yyyy(gdt)

    hdr_comp_m = re.search(r"(?m)^([A-Za-z0-9 .,&'\-]+?)\s+Restricted Shares Vesting\b", full_txt)
    issuer_hint = hdr_comp_m.group(1).strip() if hdr_comp_m else ""
    parsed_sym, parsed_company = _resolve_statement_ticker_and_company(full_txt, issuer_hint, cfg)

    row_re = re.compile(r"\b(RSU|GSU|PSU)\s+([A-Za-z0-9-]+)\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+([\d,.]+)\s+([\d,.]+)")
    gf_cfg = _grant_forecast_cfg(cfg)
    raw_account_id = gf_cfg.get("target_vested_account_id") or "MS_STOCK_PLAN"
    account_id = raw_account_id
    symbol = str(gf_cfg.get("symbol") or parsed_sym or _fallback_primary_symbol(cfg)).strip().upper()
    company_name = str(gf_cfg.get("company_name") or parsed_company or symbol).strip()
    default_award = str(gf_cfg.get("award_type") or "RSU").strip().upper()

    tranches: list[ParsedGSUTranche] = []
    for m in row_re.finditer(full_txt):
        aw_raw, gid, vdate_raw, tot_s, orig_s = m.groups()
        vdate = _parse_dd_mon_yyyy(vdate_raw)
        tot_q = float(tot_s.replace(",", ""))
        orig_q = float(orig_s.replace(",", ""))
        deus = round(tot_q - orig_q, 6)
        gdate = known_grant_dates.get(gid, vdate)
        tranches.append(
            ParsedGSUTranche(
                grant_id=gid,
                grant_date=gdate,
                vest_date=vdate,
                orig_qty=orig_q,
                deus=deus,
                total_qty=tot_q,
                symbol=symbol,
                company_name=company_name,
                award_type=(aw_raw or default_award).upper(),
                account_id=account_id,
            )
        )

    if not tranches:
        raise ValueError("No equity award vesting rows found in Restricted Shares Vesting PDF.")

    return ParsedStatement(
        doc_type="MS_UNVESTED_GSU",
        institution="Morgan Stanley",
        account_id=account_id,
        account_name=None,
        account_type=None,
        tax_status=None,
        display_order=10,
        period_start=None,
        as_of_date=report_date,
        reported_total=None,
        gsu_tranches=tranches,
    )
