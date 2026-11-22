#!/usr/bin/env python3
"""Social Security Administration (SSA) statement XML parser and path resolver."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

try:
    from defusedxml import ElementTree as _DefusedET  # type: ignore
except ImportError:  # pragma: no cover
    _DefusedET = None

from config import get_portfolio_dir, get_root

_MAX_SSA_XML_BYTES = 10 * 1024 * 1024
_FORBIDDEN_XML_DECL_RE = re.compile(r"<!(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST)\b", re.IGNORECASE)


def _safe_parse_xml(xml_text: str) -> ET.Element:
    # SECURITY DO: Enforce XML size limit and forbid DTD/ENTITY declarations (`SA-07` XXE/billion-laughs protection).
    # SECURITY DON'T: Never call bare `ET.fromstring` on untrusted XML without `defusedxml` or DTD rejection.
    if len(xml_text.encode("utf-8", errors="ignore")) > _MAX_SSA_XML_BYTES:
        raise ValueError("XML payload exceeds maximum allowed size")
    if _DefusedET is not None:
        return _DefusedET.fromstring(xml_text)
    if _FORBIDDEN_XML_DECL_RE.search(xml_text):
        raise ValueError("XML DTD/ENTITY declarations are forbidden")
    return ET.fromstring(xml_text)


def parse_ssa_xml_text(xml_text: str, file_path: str = "", file_name: str = "") -> dict[str, Any]:
    """Parses an official ssa.gov statement XML file, extracting lifetime earnings and benefit estimates."""
    try:
        root = _safe_parse_xml(xml_text)
    except Exception as exc:
        return {"ok": False, "error": f"Invalid XML: {exc}"}

    name, dob = "", ""
    fra_age, fra_est = 67, 0.0
    early_est, delayed_est = 0.0, 0.0
    age_estimates: dict[str, float] = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "Name" and not name:
            name = (elem.text or "").strip()
        elif tag == "DateOfBirth" and not dob:
            dob = (elem.text or "").strip()
        elif tag == "FullRetirementEstimate":
            for child in elem:
                ctag = child.tag.split("}")[-1]
                if ctag == "Estimate":
                    try:
                        fra_est = float(child.text or 0.0)
                    except (ValueError, TypeError):
                        pass
                elif ctag == "RetirementAge":
                    for y in child:
                        if y.tag.split("}")[-1] == "Years":
                            try:
                                fra_age = int(y.text or 67)
                            except (ValueError, TypeError):
                                pass
        elif tag == "EarlyRetirementEstimate":
            for child in elem:
                if child.tag.split("}")[-1] == "Estimate":
                    try:
                        early_est = float(child.text or 0.0)
                    except (ValueError, TypeError):
                        pass
        elif tag == "DelayedRetirementEstimate":
            for child in elem:
                if child.tag.split("}")[-1] == "Estimate":
                    try:
                        delayed_est = float(child.text or 0.0)
                    except (ValueError, TypeError):
                        pass
        elif tag.startswith("Age") and tag.endswith("RetirementEstimate"):
            try:
                age_num = int(tag.replace("Age", "").replace("RetirementEstimate", ""))
                for child in elem:
                    if child.tag.split("}")[-1] == "Estimate":
                        age_estimates[str(age_num)] = float(child.text or 0.0)
            except (ValueError, TypeError):
                pass

    earnings: dict[str, float] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "Earnings":
            yr = elem.attrib.get("startYear")
            fica = -1.0
            for child in elem:
                if child.tag.split("}")[-1] == "FicaEarnings":
                    try:
                        fica = float(child.text or -1.0)
                    except (ValueError, TypeError):
                        pass
            if yr and fica > 0:
                earnings[str(yr)] = fica

    pos_years = [int(y) for y in earnings.keys()]
    min_yr = min(pos_years) if pos_years else 0
    max_yr = max(pos_years) if pos_years else 0
    avg_sal = round(sum(earnings.values()) / len(earnings), 0) if earnings else 0.0

    fn = file_name or (Path(file_path).name if file_path else "social-security-statement.xml")
    fp = file_path or f"Portfolios/Statements/SSN/{fn}"

    return {
        "ok": True,
        "name": name,
        "birth_date": dob,
        "fra_age": fra_age,
        "fra_estimate": fra_est,
        "early_estimate": early_est,
        "delayed_estimate": delayed_est,
        "age_estimates": age_estimates,
        "annual_earnings": earnings,
        "years_worked": len(earnings),
        "average_salary": avg_sal,
        "min_year": min_yr,
        "max_year": max_yr,
        "work_span_label": f"{min_yr} - {max_yr}" if min_yr else "",
        "file_name": fn,
        "file_path": fp,
    }


def parse_ssa_xml_path(rel_or_abs_path: str | Path, portfolio_id: str | None = None) -> dict[str, Any]:
    """Resolves an SSA XML statement path strictly confined within the active portfolio and parses it."""
    pdir = get_portfolio_dir(portfolio_id)
    if pdir is None:
        return {"ok": False, "error": "No active portfolio directory"}
    pdir_resolved = pdir.resolve()
    root_dir = get_root()
    raw = str(rel_or_abs_path or "").strip()
    if not raw or "\x00" in raw:
        return {"ok": False, "error": "Invalid path"}
    p = Path(raw)
    if not p.is_absolute():
        cand = (root_dir / p).resolve()
        if not cand.exists() or not cand.is_relative_to(pdir_resolved):
            cand = (pdir_resolved / p).resolve()
        p = cand
    else:
        p = p.resolve()
    if (not p.exists() or not p.is_file()) and not p.is_symlink():
        alt = (pdir_resolved / "Statements" / "SSN" / Path(raw).name).resolve()
        if alt.exists() and alt.is_file():
            p = alt
    # SECURITY DO: Confine `parse_ssa_xml_path` strictly inside `pdir.resolve()` (`SA-07` path traversal guard).
    # SECURITY DON'T: Never open paths outside `pdir_resolved` or trust unvalidated caller paths.
    if not p.is_relative_to(pdir_resolved):
        return {"ok": False, "error": "Access denied: path must reside within the active portfolio directory"}
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": f"File not found: {rel_or_abs_path}"}
    try:
        if p.stat().st_size > _MAX_SSA_XML_BYTES:
            return {"ok": False, "error": "File exceeds maximum allowed XML size"}
        content = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "error": f"Failed reading file: {exc}"}

    rel_p = str(p.relative_to(root_dir)) if root_dir in p.parents else str(p.relative_to(pdir_resolved))
    return parse_ssa_xml_text(content, file_path=rel_p, file_name=p.name)
