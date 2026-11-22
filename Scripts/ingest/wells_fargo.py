#!/usr/bin/env python3
"""Universal parser for Wells Fargo Advisors `SNAPSHOT` statements."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from config import load_config, yahoo_symbol
from ingest.classification import classify_public_security
from ingest.models import ParsedAdjustment, ParsedPosition, ParsedStatement


def _resolve_primary_symbol(cfg: dict[str, Any] | None) -> str:
    sym = str((cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    if sym:
        return sym
    try:
        return str(load_config().get("primary_equity_symbol") or "").strip().upper()
    except Exception:
        return ""


def _parse_long_date(s: str) -> str:
    return datetime.strptime(s.strip().title(), "%B %d, %Y").strftime("%Y-%m-%d")


def _parse_short_date(s: str) -> str:
    return datetime.strptime(s.strip(), "%m/%d/%y").strftime("%Y-%m-%d")


def _parse_mmddyy_2000s(s: str) -> str:
    mm, dd, yy = [int(x) for x in s.strip().split("/")]
    year = 2000 + yy if yy < 80 else 1900 + yy
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _add_years_iso(iso_dt: str, years: int) -> str:
    y, m, d = [int(x) for x in iso_dt.split("-")]
    try:
        return f"{y + years:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return f"{y + years:04d}-{m:02d}-28"


def _clean_annuity_title(lines: list[str]) -> str:
    """Normalize verbose uppercase brokerage annuity descriptions into concise readable titles."""
    s = re.sub(r"\s+", " ", " ".join(lines).strip())
    if not s:
        return s
    s = re.sub(r"\b(?:ANNUITY\s+AND\s+LIFE\s+CO|FINL\s+LIFE\s+INS\s+CO|INS\s+CO\s+INS\s+PRODS|LIFE\s+INS\s+CO)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bFNDTN\b", "Foundation", s, flags=re.IGNORECASE)
    s = re.sub(r"\bSTRAT\b", "Strategies", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:FIXED\s+INDEXED\s+ANNUITY|INDEXED\s+ANNUITY\s+FIXED)\b", "FIA", s, flags=re.IGNORECASE)
    if re.search(r"\bSTRUCTURED\s+CAP\b", s, flags=re.IGNORECASE):
        s = re.sub(r"\bVARIABLE\s+ANN(?:UITY)?\b", "(RILA)", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    acronyms = {"MYG", "MVA", "FIA", "RILA", "MYGA", "(RILA)", "PLUS", "II", "III"}
    words = s.split(" ")
    cleaned: list[str] = []
    for w in words:
        up = w.upper()
        if cleaned and cleaned[-1].upper() == up:
            continue
        cleaned.append(up if up in acronyms else w.title())
    return " ".join(cleaned)


def parse_wfa_snapshot(pages: list[str], cfg: dict[str, Any]) -> ParsedStatement:
    """Parses any Wells Fargo Advisors SNAPSHOT PDF and verifies $0.00 checksum against Closing value."""
    full_txt = "\n".join(pages)

    acct_m = re.search(r"ACCOUNT NUMBER:\s*(\d{4}-\d{4})", full_txt)
    if not acct_m:
        raise ValueError("Could not find Wells Fargo ACCOUNT NUMBER.")
    raw_account_id = acct_m.group(1)
    # DESIGN INTENT (DP-05): Preserve real account numbers and contract IDs in local `0600` SQLite storage
    # and Live View; aliasing occurs dynamically in Demo Mode (`DemoTranslator`) and AI Chat (`PrivacyTranslator`).
    account_id = raw_account_id

    period_m = re.search(
        r"([A-Z]+\s+\d{1,2},\s+\d{4})\s*-\s*([A-Z]+\s+\d{1,2},\s+\d{4})",
        full_txt,
    )
    if not period_m:
        raise ValueError(f"Could not find statement period in WFA {account_id}.")
    period_start = _parse_long_date(period_m.group(1))
    as_of_date = _parse_long_date(period_m.group(2))
    stmt_year = as_of_date[:4]

    close_m = re.search(r"Closing value\s+\$([\d,]+\.\d{2})", full_txt)
    if not close_m:
        raise ValueError(f"Could not find Closing value in WFA {account_id}.")
    reported_total = float(close_m.group(1).replace(",", ""))

    # Determine default account name, type, tax status, and UMA program
    is_uma = "PERSONALIZED UMA" in full_txt
    uma_style = ""
    style_m = re.search(r"Your style:\s*(.+)", full_txt)
    if style_m:
        uma_style = style_m.group(1).strip()

    if "ROTH IRA" in full_txt:
        default_tax_status = "ROTH_IRA"
        default_account_name = f"WFA Roth IRA ({account_id})"
        default_account_type = "Roth IRA"
        default_order = 60
    elif "TRAD IRA" in full_txt:
        default_tax_status = "TRAD_IRA"
        default_account_name = f"WFA Traditional IRA ({account_id})"
        default_account_type = "Traditional IRA"
        default_order = 40
    else:
        default_tax_status = "TAXABLE"
        if is_uma and "LARGE CAP" in uma_style.upper():
            default_account_name = f"WFA Large Cap Equity UMA ({account_id})"
            default_account_type = "Personalized UMA (Equities)"
            default_order = 30
        elif is_uma and "MUNICIPAL" in uma_style.upper():
            default_account_name = f"WFA Municipal Bond UMA ({account_id})"
            default_account_type = "Personalized UMA (Municipal Bonds)"
            default_order = 50
        else:
            default_account_name = f"WFA Brokerage ({account_id})"
            default_account_type = "Brokerage Cash Services"
            default_order = 20

    acct_cfg = (cfg.get("accounts") or {}).get(account_id) or (cfg.get("accounts") or {}).get(raw_account_id) or {}
    account_name = acct_cfg.get("account_name") or default_account_name
    account_type = acct_cfg.get("account_type") or default_account_type
    tax_status = acct_cfg.get("tax_status") or default_tax_status
    display_order = int(acct_cfg.get("display_order", default_order))

    positions: list[ParsedPosition] = []
    adjustments: list[ParsedAdjustment] = []

    # 1. Parse Cash and Sweep Balances
    cash_m = re.search(r"Total Cash and Sweep Balances\s+(?:[\d.]+\s+)?\$([\d,]+\.\d{2})", full_txt)
    if cash_m:
        cash_val = float(cash_m.group(1).replace(",", ""))
        if cash_val > 0:
            positions.append(
                ParsedPosition(
                    security_id=f"CASH:{account_id}",
                    symbol_or_code="USD",
                    canonical_name=f"Cash & Sweep Balances ({account_id})",
                    asset_class="CASH",
                    risk_class="cash_fixed",
                    stack_bucket="cash_sweeps",
                    is_scrapeable=False,
                    quote_symbol=None,
                    quantity=1.0,
                    stmt_price=cash_val,
                    market_value=cash_val,
                    value_date=as_of_date,
                )
            )

    # 2. Parse Stocks & Mutual Funds across pages
    uma_stock_re = re.compile(
        r"^(.+?)\s+([\d,]+)\s+([\d,]+\.\d{4})\s+([\d,]+\.\d{2})\s+(N/A|[\d,]+)\s+(N/A|[\d.]+)$"
    )
    taxlot_sec_re = re.compile(
        r"^(?:Acquired.+?|Reinvest.+?)\s+([\d.]+)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{4})\s+([\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s+(N/A|[\d,]+)\s+(N/A|[\d.]+)$"
    )
    bond_start_re = re.compile(
        r"^(.+?)\s+([\d,]+)\s+([\d,]+\.\d{4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+)\s+([\d.]+)$"
    )
    annuity_start_re = re.compile(
        r"^([A-Z][A-Z0-9 &/.-]+?)\s+(\d+\.\d{2})\s+(\d{1,2}/\d{1,2}/\d{2})\s+([\d,]+\.\d{2})$"
    )
    subfund_unit_re = re.compile(
        r"^(.+?)\s+(\d+\.\d{5})\s+([\d,]+\.\d{2})\s+([\d.]+)$"
    )

    for page_txt in pages:
        lines = [ln.strip() for ln in page_txt.splitlines() if ln.strip()]

        # A) UMA Stocks page (`DESCRIPTION QUANTITY` header without `% OF ACCOUNT`)
        if "Stocks and ETFs" in page_txt and "DESCRIPTION QUANTITY" in page_txt and "% OF" not in page_txt:
            start_i = next(i for i, ln in enumerate(lines) if ln.startswith("DESCRIPTION QUANTITY")) + 1
            cur_stock: dict[str, Any] | None = None
            for ln in lines[start_i:]:
                if ln.startswith(("Total Stocks", "PERSONALIZED UMA", "020 ND DE3O", "Activity detail")):
                    break
                m = uma_stock_re.match(ln)
                if m:
                    if cur_stock:
                        _finalize_uma_stock(cur_stock, account_id, as_of_date, is_uma, cfg, positions)
                    desc1, qty_s, pr_s, mv_s, inc_s, yld_s = m.groups()
                    cur_stock = {
                        "desc": [desc1],
                        "qty": float(qty_s.replace(",", "")),
                        "price": float(pr_s.replace(",", "")),
                        "mv": float(mv_s.replace(",", "")),
                        "inc": None if inc_s == "N/A" else float(inc_s.replace(",", "")),
                        "yld": None if yld_s == "N/A" else float(yld_s),
                    }
                elif cur_stock is not None:
                    cur_stock["desc"].append(ln)
            if cur_stock:
                _finalize_uma_stock(cur_stock, account_id, as_of_date, is_uma, cfg, positions)

        # B) Standard Tax-Lot Stocks or Open-End Mutual Funds (`% OF` + `ADJ COST/` header)
        if ("Stocks and ETFs" in page_txt or "Open End Mutual Funds" in page_txt) and "ORIG COST" in page_txt:
            in_mf = "Open End Mutual Funds" in page_txt and "Stocks and ETFs" not in page_txt
            try:
                start_i = next(i for i, ln in enumerate(lines) if "ORIG COST" in ln) + 1
            except StopIteration:
                start_i = 0
            buf_desc: list[str] = []
            for ln in lines[start_i:]:
                if ln.startswith(("Total Open End", "Total Mutual Funds", "Total Stocks", "Annuities/Insurance", "Activity detail", "020 ND DE3O", "nc Cost information")):
                    if ln.startswith("Annuities/Insurance"):
                        break
                    buf_desc = []
                    continue
                m_orig = re.match(r"^([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$", ln)
                if m_orig:
                    # Secondary orig_price / orig_cost line beneath Acquired Net Tax Lots
                    if positions and positions[-1].asset_class in ("MUTUAL_FUND", "EQUITY"):
                        positions[-1].metadata["orig_price"] = float(m_orig.group(1).replace(",", ""))
                        positions[-1].metadata["orig_cost"] = float(m_orig.group(2).replace(",", ""))
                    continue
                m = taxlot_sec_re.match(ln)
                if m and buf_desc:
                    _pct, qty_s, _adj_p, adj_cost_s, pr_s, mv_s, _ugl, inc_s, yld_s = m.groups()
                    # Extract ticker from buf_desc (last uppercase single-token line before 'On Reinvestment' if any)
                    filtered = [d for d in buf_desc if d not in ("On Reinvestment", "Reinvest")]
                    ticker = filtered[-1]
                    name = " ".join(filtered[:-1]) if len(filtered) > 1 else ticker
                    acq_m = re.search(r"Acquired\s+(\d{2}/\d{2}/\d{2})", ln)
                    acq_date = _parse_short_date(acq_m.group(1)) if acq_m else None
                    is_net_lots = "Net Tax Lots" in ln
                    raw_asset_cls = "MUTUAL_FUND" if in_mf else "EQUITY"
                    asset_cls, risk_cls = classify_public_security(ticker, name, raw_asset_cls)
                    primary_sym = _resolve_primary_symbol(cfg)
                    bucket = "mutual_funds" if in_mf else ("goog_combined" if (primary_sym and ticker.upper() == primary_sym) else "uma_equities")
                    adj_cost_val = float(adj_cost_s.replace(",", ""))
                    positions.append(
                        ParsedPosition(
                            security_id=f"TICKER:{ticker}",
                            symbol_or_code=ticker,
                            canonical_name=name.title(),
                            asset_class=asset_cls,
                            risk_class=risk_cls,
                            stack_bucket=bucket,
                            is_scrapeable=True,
                            quote_symbol=yahoo_symbol(ticker, cfg),
                            quantity=float(qty_s.replace(",", "")),
                            stmt_price=float(pr_s.replace(",", "")),
                            market_value=float(mv_s.replace(",", "")),
                            value_date=as_of_date,
                            cost_basis=adj_cost_val,
                            est_annual_income=None if inc_s == "N/A" else float(inc_s.replace(",", "")),
                            yield_pct=None if yld_s == "N/A" else float(yld_s),
                            issue_date=acq_date,
                            acquisition_date=acq_date,
                            metadata={
                                "inception_date": acq_date,
                                "adj_cost": adj_cost_val,
                                "is_net_tax_lots": is_net_lots,
                            },
                        )
                    )
                    buf_desc = []
                else:
                    buf_desc.append(ln)

        # C) Municipal Bonds (`Municipal Bonds` section)
        if "Municipal Bonds" in page_txt and "DESCRIPTION QUANTITY" in page_txt:
            start_i = next(i for i, ln in enumerate(lines) if ln.startswith("DESCRIPTION QUANTITY")) + 1
            cur_bond: dict[str, Any] | None = None
            for ln in lines[start_i:]:
                if ln.startswith(("Total Municipal Bonds", "Total Fixed Income", "Activity detail", "PERSONALIZED UMA", "020 ND DE3O")):
                    break
                m = bond_start_re.match(ln)
                if m:
                    if cur_bond:
                        _finalize_bond(cur_bond, as_of_date, positions)
                    desc1, par_s, pr_s, mv_s, acc_s, inc_s, yld_s = m.groups()
                    cur_bond = {
                        "desc": [desc1],
                        "par": float(par_s.replace(",", "")),
                        "price": float(pr_s.replace(",", "")),
                        "mv": float(mv_s.replace(",", "")),
                        "accrued": float(acc_s.replace(",", "")),
                        "inc": float(inc_s.replace(",", "")),
                        "yld": float(yld_s),
                    }
                elif cur_bond is not None:
                    cur_bond["desc"].append(ln)
            if cur_bond:
                _finalize_bond(cur_bond, as_of_date, positions)

    # 3. Parse Annuities/Insurance across entire document (since sub-funds can span a page break)
    ann_subtype = "FIXED_ANNUITY"
    cur_ann: dict[str, Any] | None = None
    for raw_ln in full_txt.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if ln in ("Fixed Annuities", "Indexed Annuities", "Variable Annuities"):
            next_subtype = (
                "FIXED_ANNUITY"
                if ln == "Fixed Annuities"
                else ("INDEXED_ANNUITY" if ln == "Indexed Annuities" else "RILA_ANNUITY")
            )
            if cur_ann and cur_ann["subtype"] != next_subtype:
                _finalize_annuity(cur_ann, account_id, positions, cfg=cfg)
                cur_ann = None
            ann_subtype = next_subtype
            continue
        if ln.startswith(("Total Fixed Annuities", "Total Indexed Annuities", "Total Variable Annuities", "Total Annuities/Insurance", "Activity detail")):
            if cur_ann:
                _finalize_annuity(cur_ann, account_id, positions, cfg=cfg)
                cur_ann = None
            continue

        am = annuity_start_re.match(ln)
        if am:
            if cur_ann:
                _finalize_annuity(cur_ann, account_id, positions, cfg=cfg)
            desc1, _pct_s, vdate_s, mv_s = am.groups()
            cur_ann = {
                "subtype": ann_subtype,
                "title_lines": [desc1],
                "value_date": _parse_short_date(vdate_s),
                "mv": float(mv_s.replace(",", "")),
                "contract_num": None,
                "issue_date": None,
                "initial_premium": None,
                "surrender_value": None,
                "sub_segments": [],
                "header_done": False,
            }
            continue

        if cur_ann is not None:
            if ln.startswith("CONTR#"):
                cur_ann["contract_num"] = ln.split("CONTR#", 1)[1].strip()
                cur_ann["header_done"] = True
            elif ln.startswith("ISSUE DATE"):
                dt_raw = ln.split("ISSUE DATE", 1)[1].strip()
                cur_ann["issue_date"] = _parse_short_date(dt_raw)
                cur_ann["header_done"] = True
            elif ln.startswith("TOTAL PREMIUM(S)"):
                pm = re.search(r"\$([\d,]+\.\d{2})", ln)
                if pm:
                    cur_ann["initial_premium"] = float(pm.group(1).replace(",", ""))
                cur_ann["header_done"] = True
            elif ln.startswith("SURRENDER VALUE"):
                sm = re.search(r"([\d,]+\.\d{2})$", ln)
                if sm:
                    cur_ann["surrender_value"] = float(sm.group(1).replace(",", ""))
            elif not cur_ann["header_done"] and not ln.startswith(("Page ", "020 ND", "Sub Funds", "DESCRIPTION")):
                cur_ann["title_lines"].append(ln)
            else:
                sfm = subfund_unit_re.match(ln)
                if sfm:
                    sname, units_s, est_s, pct_s = sfm.groups()
                    if float(units_s) > 0.0:
                        cur_ann["sub_segments"].append(
                            {
                                "name": sname.strip(),
                                "units": float(units_s),
                                "value": float(est_s.replace(",", "")),
                                "pct": float(pct_s),
                            }
                        )

    if cur_ann:
        _finalize_annuity(cur_ann, account_id, positions, cfg=cfg)

    # 4. Detect Reverse/Forward Splits in Activity detail (e.g. mutual fund REV SPLIT)
    if "REV SPLIT" in full_txt or "STOCK SPLIT" in full_txt:
        split_m = re.search(
            r"REV SPLIT.*?\n(\d{2}/\d{2})\s+\w+\s+(-?[\d,]+\.\d+)\s+([\d,]+\.\d{2})",
            full_txt,
        )
        if split_m:
            mmdd, delta_s, _amt = split_m.groups()
            delta_qty = float(delta_s.replace(",", ""))
            eff_date = f"{stmt_year}-{mmdd.replace('/', '-')}"
            # Match to the mutual fund / stock in this account (single-fund IRA or matched description)
            mf_positions = [p for p in positions if p.asset_class in ("MUTUAL_FUND", "EQUITY")]
            if len(mf_positions) == 1:
                target = mf_positions[0]
                pre_qty = round(target.quantity - delta_qty, 6)
                adjustments.append(
                    ParsedAdjustment(
                        security_id=target.security_id,
                        effective_date=eff_date,
                        adj_type="PRE_SPLIT_SHARES",
                        pre_quantity=pre_qty,
                        post_quantity=target.quantity,
                        notes=f"Statement REV SPLIT share adjustment ({delta_qty:+.5f}) on {eff_date}",
                    )
                )

    # 5. Penny-exact Checksum Verification against WFA `Closing value`
    calc_sum = round(sum(p.market_value for p in positions), 2)
    if abs(calc_sum - reported_total) > 0.02:
        raise ValueError(
            f"WFA {account_id} checksum mismatch: parsed positions sum to ${calc_sum:,.2f} "
            f"vs statement Closing value ${reported_total:,.2f}"
        )

    return ParsedStatement(
        doc_type="WFA_SNAPSHOT",
        institution="Wells Fargo Advisors",
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


def _finalize_uma_stock(
    item: dict[str, Any],
    account_id: str,
    as_of_date: str,
    is_uma: bool,
    cfg: dict[str, Any],
    out: list[ParsedPosition],
) -> None:
    desc_lines = item["desc"]
    ticker = desc_lines[-1]
    name = " ".join(desc_lines[:-1]) if len(desc_lines) > 1 else ticker
    primary_sym = _resolve_primary_symbol(cfg)
    bucket = "uma_equities" if is_uma else ("goog_combined" if (primary_sym and ticker.upper() == primary_sym) else "uma_equities")
    asset_cls, risk_cls = classify_public_security(ticker, name, "EQUITY")
    out.append(
        ParsedPosition(
            security_id=f"TICKER:{ticker}",
            symbol_or_code=ticker,
            canonical_name=name.title(),
            asset_class=asset_cls,
            risk_class=risk_cls,
            stack_bucket=bucket,
            is_scrapeable=True,
            quote_symbol=yahoo_symbol(ticker, cfg),
            quantity=item["qty"],
            stmt_price=item["price"],
            market_value=item["mv"],
            value_date=as_of_date,
            est_annual_income=item["inc"],
            yield_pct=item["yld"],
        )
    )


def _finalize_bond(item: dict[str, Any], as_of_date: str, out: list[ParsedPosition]) -> None:
    full_desc = " | ".join(item["desc"])
    cusip_m = re.search(r"CUSIP\s+([A-Z0-9]{9})", full_desc)
    cpn_m = re.search(r"CPN\s+([\d.]+%)\s+DUE\s+(\d{2}/\d{2}/\d{2})", full_desc)
    call_m = re.search(r"(?:CALL|PRE)\s+(\d{2}/\d{2}/\d{2})\s+@\s+([\d.]+)", full_desc)
    cusip = cusip_m.group(1) if cusip_m else item["desc"][0][:12].replace(" ", "_")
    issuer = item["desc"][0].title()
    coupon = cpn_m.group(1) if cpn_m else ""
    maturity = cpn_m.group(2) if cpn_m else ""
    maturity_iso = _parse_mmddyy_2000s(maturity) if maturity else None
    call_str = f"{_parse_mmddyy_2000s(call_m.group(1))} @ {call_m.group(2)}" if call_m else None

    out.append(
        ParsedPosition(
            security_id=f"CUSIP:{cusip}",
            symbol_or_code=cusip,
            canonical_name=f"{issuer} ({coupon})".strip(),
            asset_class="MUNI_BOND",
            risk_class="fixed_income",
            stack_bucket="muni_bonds",
            is_scrapeable=False,
            quote_symbol=None,
            quantity=item["par"],
            stmt_price=item["price"],
            market_value=item["mv"],
            value_date=as_of_date,
            accrued_interest=item["accrued"],
            est_annual_income=item["inc"],
            yield_pct=item["yld"],
            maturity_date=maturity_iso,
            metadata={
                "cusip": cusip,
                "coupon": coupon,
                "maturity": maturity,
                "maturity_date": maturity_iso,
                "call_info": call_str,
                "full_description": full_desc,
            },
        )
    )


def _finalize_annuity(
    item: dict[str, Any],
    account_id: str,
    out: list[ParsedPosition],
    cfg: dict[str, Any] | None = None,
) -> None:
    title = _clean_annuity_title(item["title_lines"])
    raw_upper = " ".join(item["title_lines"]).upper()
    cnum = item["contract_num"]
    # DESIGN INTENT (DP-05): Preserve real carrier contract numbers in local storage and Live View;
    # `DemoTranslator` and `PrivacyTranslator` alias `CONTRACT:<id>` in Demo Mode and AI Chat Mode B/C.
    if cnum:
        sec_id = f"CONTRACT:{cnum}"
        code = cnum
    else:
        slug = re.sub(r"[^A-Z0-9]+", "_", title.upper()).strip("_")[:32]
        sec_id = f"CONTRACT:{account_id}:{slug}"
        code = slug

    subtype = item["subtype"]
    if subtype == "FIXED_ANNUITY":
        bucket = "fixed_annuities"
        risk_cls = "cash_fixed"
    elif subtype == "INDEXED_ANNUITY":
        bucket = "indexed_annuities"
        risk_cls = "fixed_income"
    else:
        bucket = "rila_annuities"
        risk_cls = "hybrid_buffered"

    # Derive contract maturity / surrender-free term end date when issue_date + term are reported
    issue_dt = item["issue_date"]
    maturity_dt: str | None = None
    term_label: str | None = None
    if issue_dt:
        seg_years = sorted(
            {
                int(m.group(1))
                for s in item["sub_segments"]
                for m in re.finditer(r"\b([1-9]|1\d)\s*(?:YR|YEAR)\b", s["name"].upper().replace("-", " "))
            }
        )
        m_title_yr = re.search(r"\b([3-9]|1\d)\s*(?:YR|YEAR)\b", raw_upper.replace("-", " "))
        if not m_title_yr:
            m_title_yr = re.search(r"\b([3-9]|10)\b(?:\s*$)", raw_upper)
        term_yrs = max(seg_years) if seg_years else (int(m_title_yr.group(1)) if m_title_yr else None)
        if term_yrs:
            maturity_dt = _add_years_iso(issue_dt, term_yrs)
            if subtype == "FIXED_ANNUITY":
                term_label = f"{term_yrs}-Yr Fixed Guarantee"
            elif subtype == "INDEXED_ANNUITY":
                term_label = f"{term_yrs}-Yr FIA Term"
            else:
                seg_desc = (
                    " & ".join(f"{y}Yr" for y in seg_years) + " Segments"
                    if len(seg_years) > 1
                    else f"{term_yrs}Yr Segment"
                )
                term_label = f"{term_yrs}-Yr RILA • {seg_desc}"

    enriched_segments = []
    for seg in item["sub_segments"]:
        s_copy = dict(seg)
        s_up = s_copy["name"].upper().replace(" ", "")
        if issue_dt and "6YR" in s_up:
            s_copy["maturity_date"] = _add_years_iso(issue_dt, 6)
            s_copy["term_note"] = f"6-Yr Segment • Matures {s_copy['maturity_date']}"
        elif "1YR" in s_up or "1Y" in s_up:
            s_copy["maturity_date"] = None
            s_copy["term_note"] = "1-Yr Rolling Segment (Annual Reset)"
        enriched_segments.append(s_copy)

    out.append(
        ParsedPosition(
            security_id=sec_id,
            symbol_or_code=code,
            canonical_name=title,
            asset_class=subtype,
            risk_class=risk_cls,
            stack_bucket=bucket,
            is_scrapeable=False,
            quote_symbol=None,
            quantity=1.0,
            stmt_price=item["mv"],
            market_value=item["mv"],
            value_date=item["value_date"],
            surrender_value=item["surrender_value"],
            issue_date=issue_dt,
            acquisition_date=issue_dt,
            maturity_date=maturity_dt,
            initial_premium=item["initial_premium"],
            metadata={
                "contract_number": cnum,
                "sub_segments": enriched_segments,
                "raw_title": " ".join(item["title_lines"]),
                "maturity_date": maturity_dt,
                "term_label": term_label,
            },
        )
    )
