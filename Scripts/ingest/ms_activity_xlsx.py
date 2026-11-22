#!/usr/bin/env python3
"""Parser for Morgan Stanley at Work `Activity Report` (.xlsx) workbooks."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile

try:
    from defusedxml import ElementTree as _DefusedET  # type: ignore
except ImportError:  # pragma: no cover
    _DefusedET = None

_MAX_XLSX_MEMBER_BYTES = 25 * 1024 * 1024
_FORBIDDEN_XML_DECL_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST)\b", re.IGNORECASE)


def _read_zip_member_limited(zf: zipfile.ZipFile, member: str, max_bytes: int = _MAX_XLSX_MEMBER_BYTES) -> bytes:
    # SECURITY DO: Bound decompressed XLSX XML bytes and reject `..` zip member paths (`SA-08` zip-bomb guard).
    # SECURITY DON'T: Never call unbounded `zf.read(member)` or `ET.fromstring` on untrusted spreadsheet XML.
    clean = member.lstrip("/")
    if ".." in Path(clean).parts:
        raise ValueError(f"Unsafe zip member path: {member!r}")
    info = zf.getinfo(clean)
    if info.file_size > max_bytes:
        raise ValueError(f"XLSX member {clean!r} exceeds size limit")
    with zf.open(info, "r") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"XLSX member {clean!r} exceeded decompressed size limit")
    return data


def _safe_parse_xml_bytes(raw: bytes) -> ET.Element:
    if len(raw) > _MAX_XLSX_MEMBER_BYTES:
        raise ValueError("XML payload exceeds maximum allowed size")
    if _DefusedET is not None:
        return _DefusedET.fromstring(raw)
    if _FORBIDDEN_XML_DECL_RE.search(raw):
        raise ValueError("XML DTD/ENTITY declarations are forbidden")
    return ET.fromstring(raw)
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import load_config
from ingest.models import ParsedGSURelease, ParsedGSUWithdrawal, ParsedStatement

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN_NS}


def _parse_excel_date(raw: str) -> str:
    s = str(raw).strip()
    try:
        serial = int(float(s))
        if 30000 <= serial <= 70000:
            return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date value in Activity Report xlsx: {raw!r}")


def _read_xlsx_sheets(xlsx_path: Path) -> dict[str, list[dict[str, str | None]]]:
    with zipfile.ZipFile(xlsx_path) as zf:
        wb_root = _safe_parse_xml_bytes(_read_zip_member_limited(zf, "xl/workbook.xml"))
        rels_root = _safe_parse_xml_bytes(_read_zip_member_limited(zf, "xl/_rels/workbook.xml.rels"))
        rel_map = {el.attrib["Id"]: el.attrib["Target"] for el in rels_root}

        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = _safe_parse_xml_bytes(_read_zip_member_limited(zf, "xl/sharedStrings.xml"))
            for si in ss_root:
                shared.append("".join(si.itertext()))

        sheets: dict[str, list[dict[str, str | None]]] = {}
        for sh in wb_root.findall(".//m:sheet", NS):
            name = sh.attrib["name"]
            rid = sh.attrib[f"{{{REL_NS}}}id"]
            target = "xl/" + rel_map[rid].lstrip("/")
            if target.startswith("xl/xl/"):
                target = target[3:]
            ws_root = _safe_parse_xml_bytes(_read_zip_member_limited(zf, target))
            rows: list[dict[str, str | None]] = []
            for row in ws_root.findall(".//m:row", NS):
                cells: dict[str, str | None] = {}
                for c in row:
                    ref = c.attrib.get("r", "")
                    col = "".join(ch for ch in ref if ch.isalpha())
                    t = c.attrib.get("t", "")
                    v_el = c.find("m:v", NS)
                    is_el = c.find("m:is", NS)
                    if t == "s" and v_el is not None and v_el.text is not None:
                        val: str | None = shared[int(v_el.text)]
                    elif t == "inlineStr" and is_el is not None:
                        val = "".join(is_el.itertext())
                    elif v_el is not None:
                        val = v_el.text
                    else:
                        val = None
                    cells[col] = val
                if any(v is not None and str(v).strip() != "" for v in cells.values()):
                    rows.append(cells)
            sheets[name] = rows
        return sheets


def is_ms_activity_xlsx(path: Path) -> bool:
    if path.suffix.lower() != ".xlsx":
        return False
    try:
        sheets = _read_xlsx_sheets(path)
        return "Releases Report" in sheets and "Withdrawals Report" in sheets
    except Exception:
        return False


def _header_col_lookup(hdr_row: dict[str, str | None], keywords: tuple[str, ...]) -> str | None:
    for col, label in hdr_row.items():
        low = str(label or "").strip().lower()
        if low and any(kw in low for kw in keywords):
            return col
    return None


def parse_ms_activity_xlsx(path: Path, cfg: dict[str, Any]) -> ParsedStatement:
    """Extracts all historical equity award releases and share/cash withdrawals from Activity Report workbooks."""
    sheets = _read_xlsx_sheets(path)
    rel_all = sheets.get("Releases Report", [])
    wdr_all = sheets.get("Withdrawals Report", [])
    rel_hdr = rel_all[0] if rel_all else {}
    wdr_hdr = wdr_all[0] if wdr_all else {}
    rel_rows = rel_all[1:]
    wdr_rows = wdr_all[1:]

    eff_cfg = cfg or {}
    eff_gf = eff_cfg.get("grant_forecast") or eff_cfg.get("gsu_forecast") or {}
    if not eff_cfg.get("primary_equity_symbol") and not eff_gf.get("symbol"):
        try:
            loaded_cfg = load_config()
        except Exception:
            loaded_cfg = {}
    else:
        loaded_cfg = {}

    gsu_cfg = eff_gf or loaded_cfg.get("grant_forecast") or loaded_cfg.get("gsu_forecast") or {}
    raw_account_id = gsu_cfg.get("target_vested_account_id") or "MS_STOCK_PLAN"
    account_id = raw_account_id
    default_sym = str(
        gsu_cfg.get("symbol")
        or eff_cfg.get("primary_equity_symbol")
        or loaded_cfg.get("primary_equity_symbol")
        or ""
    ).strip().upper()
    default_company = str(gsu_cfg.get("company_name") or default_sym).strip()

    sym_col = _header_col_lookup(rel_hdr, ("symbol", "ticker"))
    comp_col = _header_col_lookup(rel_hdr, ("company", "issuer"))
    award_col = _header_col_lookup(rel_hdr, ("award type", "award", "plan"))
    wdr_sym_col = _header_col_lookup(wdr_hdr, ("symbol", "ticker"))

    inferred_award = str(gsu_cfg.get("award_type") or "").strip().upper()
    if not inferred_award:
        for wr in wdr_rows:
            plan_txt = str(wr.get("C") or "").strip()
            m_aw = re.search(r"\b(RSU|GSU|PSU)\b", plan_txt, flags=re.IGNORECASE)
            if m_aw:
                inferred_award = m_aw.group(1).upper()
                break
    default_award_type = inferred_award or "RSU"

    releases: list[ParsedGSURelease] = []
    for r in rel_rows:
        order_num = (r.get("B") or "").strip()
        if not order_num or r.get("A") is None:
            continue
        status = (r.get("E") or "").strip().lower()
        if status and status not in ("complete", "completed"):
            continue
        vdate = _parse_excel_date(str(r["A"]))
        price = float(str(r["F"]).replace(",", "").replace("$", ""))
        gross = float(str(r["G"]).replace(",", ""))
        net = float(str(r["I"]).replace(",", ""))
        tax_method = (r.get("J") or "").strip()
        row_sym = str(r.get(sym_col) or "").strip().upper() if sym_col else ""
        row_comp = str(r.get(comp_col) or "").strip() if comp_col else ""
        row_aw_raw = str(r.get(award_col) or "").strip() if award_col else ""
        m_row_aw = re.search(r"\b(RSU|GSU|PSU)\b", row_aw_raw, flags=re.IGNORECASE)
        eff_sym = row_sym or default_sym
        eff_comp = row_comp or default_company or eff_sym
        eff_aw = m_row_aw.group(1).upper() if m_row_aw else default_award_type
        releases.append(
            ParsedGSURelease(
                order_number=order_num,
                vest_date=vdate,
                release_price=price,
                gross_shares=gross,
                net_shares=net,
                tax_method=tax_method,
                symbol=eff_sym,
                company_name=eff_comp,
                award_type=eff_aw,
                account_id=account_id,
            )
        )

    withdrawals: list[ParsedGSUWithdrawal] = []
    for r in wdr_rows:
        order_num = (r.get("B") or "").strip()
        if not order_num or r.get("A") is None or "split" in str(r.get("A")).lower():
            continue
        status = (r.get("E") or "").strip().lower()
        if status and status not in ("complete", "completed"):
            continue
        edate = _parse_excel_date(str(r["A"]))
        plan_name = (r.get("C") or "").strip()
        raw_type = (r.get("D") or "").strip()
        net_amt_raw = r.get("H")
        net_amt = float(str(net_amt_raw).replace(",", "").replace("$", "")) if net_amt_raw not in (None, "") else None
        tx_type = raw_type if raw_type else ("Transfer" if net_amt is None else "Sale")
        price = float(str(r["F"]).replace(",", "").replace("$", ""))
        qty = float(str(r["G"]).replace(",", ""))
        row_sym = str(r.get(wdr_sym_col) or "").strip().upper() if wdr_sym_col else ""
        withdrawals.append(
            ParsedGSUWithdrawal(
                order_number=order_num,
                exec_date=edate,
                plan_name=plan_name,
                tx_type=tx_type,
                price=price,
                quantity=qty,
                net_amount=net_amt,
                symbol=row_sym or default_sym,
                account_id=account_id,
            )
        )

    all_dates = [x.vest_date for x in releases] + [x.exec_date for x in withdrawals]
    period_start = min(all_dates) if all_dates else None
    as_of_date = max(all_dates) if all_dates else datetime.now().strftime("%Y-%m-%d")

    return ParsedStatement(
        doc_type="MS_ACTIVITY_XLSX",
        institution="Morgan Stanley",
        account_id=account_id,
        account_name=None,
        account_type=None,
        tax_status=None,
        display_order=10,
        period_start=period_start,
        as_of_date=as_of_date,
        reported_total=None,
        gsu_releases=releases,
        gsu_withdrawals=withdrawals,
    )


def match_releases_for_date(
    vest_date: str,
    date_releases: list[dict[str, Any]],
    active_grants: list[dict[str, Any]],
) -> dict[str, str]:
    """
    Maps `order_number -> grant_id` for all release rows on `vest_date` using one-to-one
    matching against active grants (`1x` base monthly tranche or `2x` front-loaded Year-1 tier),
    falling back to `'PRIOR-LOTS'` for grants that retired prior to the unvested schedule.
    """
    eligible = [
        g for g in active_grants
        if g["grant_date"] <= vest_date <= g["last_vest"] and g["base_orig"] > 0
    ]

    # Build candidate `(rel_err, order_number, grant_id)` pairs
    pairs: list[tuple[float, str, str]] = []
    for r in date_releases:
        gross = float(r["gross_shares"])
        for g in eligible:
            base = float(g["base_orig"])
            err_1x = abs(gross - base) / base
            err_2x = abs(gross - 2.0 * base) / (2.0 * base)
            best_err = min(err_1x, err_2x)
            if best_err <= 0.045:
                pairs.append((best_err, r["order_number"], g["grant_id"]))

    pairs.sort(key=lambda x: x[0])
    assigned_orders: dict[str, str] = {}
    used_grants: set[str] = set()

    for _err, onum, gid in pairs:
        if onum not in assigned_orders and gid not in used_grants:
            assigned_orders[onum] = gid
            used_grants.add(gid)

    for r in date_releases:
        onum = r["order_number"]
        if onum not in assigned_orders:
            # PORTABILITY INVARIANT (SA2-23 / SA3-13): Use household-neutral `'PRIOR-LOTS'` matching `forecast_enricher.py`.
            assigned_orders[onum] = "PRIOR-LOTS"

    return assigned_orders
