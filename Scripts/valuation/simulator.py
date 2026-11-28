#!/usr/bin/env python3
"""
Deterministic Python Retirement Simulator, Goal-Seek Solver, Holdings Query, and What-If Rollover Engine.

Mirrors the exact 480-month (and 100-year solvency) simulation math of the interactive web UI
in `Scripts/exporters/html_report.py` so LLM tools, MCP servers, CLI commands, and HTTP endpoints
produce identical results to the browser dashboard.
"""

from __future__ import annotations

import calendar
import copy
import math
import re
from contextvars import ContextVar
from datetime import date
from typing import Any

from valuation import federal_tax_2026

SIM_TOTAL_MONTHS = 480
MONTH_NAMES_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# CodeReview A1 / DA-C01 — Required Minimum Distributions (SECURE 2.0 Act §107: age 73 for <=1959, age 75 for >=1960).
RMD_AGE = 73


def rmd_start_age_for_birth_year(birth_year: int) -> int:
    """SECURE 2.0 Act §107 statutory RMD starting age (`75` for birth years >= 1960; `73` otherwise)."""
    return 75 if int(birth_year) >= 1960 else 73
# IRS Uniform Lifetime Table (2022+) divisors by age reached in the distribution year.
ULT_DIVISORS = {
    73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2,
    81: 19.4, 82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2, 87: 14.4, 88: 13.7,
    89: 12.9, 90: 12.2, 91: 11.5, 92: 10.8, 93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4,
    97: 7.8, 98: 7.3, 99: 6.8, 100: 6.4, 101: 6.0, 102: 5.6, 103: 5.2, 104: 4.9,
    105: 4.6, 106: 4.3, 107: 4.1, 108: 3.9, 109: 3.7, 110: 3.5, 111: 3.4, 112: 3.3,
    113: 3.1, 114: 3.0, 115: 2.9, 116: 2.8, 117: 2.7, 118: 2.5, 119: 2.3, 120: 2.0,
}


def _load_family_config_data(cfg: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Loads family birthdays, RMD owner birth year, and investor metadata from project config."""
    if cfg is None:
        try:
            from config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    investors_raw = cfg.get("investors") or []
    beneficiaries_raw = cfg.get("beneficiaries") or []
    family: list[dict[str, Any]] = []
    investors_meta: list[dict[str, Any]] = []
    rmd_birth_yr = 1975
    rmd_found_explicit = False
    from config import resolve_family_members, sanitize_work_plan

    resolved_fams = resolve_family_members(cfg, clamp_work_plan=True)
    for fm in resolved_fams:
        if fm.get("is_rmd_owner"):
            b_str_fm = str(fm.get("birth_date") or "1975-01-01")
            try:
                rmd_birth_yr = int(b_str_fm.split("-")[0])
                rmd_found_explicit = True
            except Exception:
                pass
            break

    eff_investors_raw = list(investors_raw)
    if not eff_investors_raw and resolved_fams:
        primary_fm = next((m for m in resolved_fams if m.get("is_rmd_owner")), resolved_fams[0])
        eff_investors_raw = [primary_fm]

    for idx, inv in enumerate(eff_investors_raw):
        b_str = str(inv.get("birth_date") or "1975-01-01")
        try:
            parts = [int(x) for x in b_str.split("-")[:3]]
            y = parts[0] if len(parts) > 0 else 1975
            m = parts[1] if len(parts) > 1 else 1
            d = parts[2] if len(parts) > 2 else 1
        except Exception:
            y, m, d = 1975, 1, 1
        name = str(inv.get("name") or f"Investor {idx + 1}")
        inv_id = str(inv.get("id") or name).strip().lower().replace(" ", "_")
        is_rmd = bool(inv.get("is_rmd_owner", inv.get("rmd_owner", idx == 0 and not rmd_found_explicit)))
        if not rmd_found_explicit and (is_rmd or idx == 0):
            if is_rmd or rmd_birth_yr == 1975:
                rmd_birth_yr = y
        ssn_cfg = inv.get("ssn") or {}
        claim_key = f"ssn{name.strip().title().replace(' ', '')}ClaimAge"
        ms_age = int(inv.get("milestone_age", 85))
        investors_meta.append(
            {
                "id": inv_id,
                "name": name,
                "birth_year": y,
                "birth_month": m,
                "birth_day": d,
                "milestone_age": ms_age,
                "rmd_owner": is_rmd,
                "claim_setting_key": claim_key,
                "default_claim_age": int(ssn_cfg.get("default_claim_age", 67)),
                "work_plan": sanitize_work_plan(inv.get("work_plan"), milestone_age=ms_age, clamp_to_milestone=True),
            }
        )
        if investors_raw:
            family.append({"name": name, "y": y, "m": m, "d": d, "role": "investor"})

    if resolved_fams and not investors_raw and not beneficiaries_raw:
        for fm in resolved_fams:
            b_str = str(fm.get("birth_date") or "1975-01-01")
            try:
                pts = [int(x) for x in b_str.split("-")[:3]]
                fy = pts[0] if len(pts) > 0 else 1975
                fm_m = pts[1] if len(pts) > 1 else 1
                fd = pts[2] if len(pts) > 2 else 1
            except Exception:
                fy, fm_m, fd = 1975, 1, 1
            family.append({
                "name": str(fm.get("name") or "Member"),
                "y": fy,
                "m": fm_m,
                "d": fd,
                "role": "investor" if fm.get("is_ssn_contributor") else "beneficiary",
            })
    else:
        for idx, ben in enumerate(beneficiaries_raw):
            b_str = str(ben.get("birth_date") or "2005-01-01")
            try:
                pts = [int(x) for x in b_str.split("-")[:3]]
                y = pts[0] if len(pts) > 0 else 2005
                m = pts[1] if len(pts) > 1 else 1
                d = pts[2] if len(pts) > 2 else 1
            except Exception:
                y, m, d = 2005, 1, 1
            name = str(ben.get("name") or f"Beneficiary {idx + 1}")
            family.append({"name": name, "y": y, "m": m, "d": d, "role": "beneficiary"})

    family.sort(key=lambda item: (item["y"], item["m"], item["d"]))
    return family, rmd_birth_yr, investors_meta


_family_config = ContextVar("simulation_family_config", default=None)
_project_config_ctx = ContextVar("simulation_project_config", default=None)


def refresh_family_config(cfg: dict[str, Any] | None = None) -> None:
    """Keep household defaults local to the current evaluation."""
    _family_config.set(_load_family_config_data(cfg))
    _project_config_ctx.set(copy.deepcopy(cfg) if isinstance(cfg, dict) else None)


def _family_defaults():
    defaults = _family_config.get()
    if defaults is None:
        defaults = _load_family_config_data()
        _family_config.set(defaults)
    return defaults


def _get_investors_meta(
    payload: dict[str, Any] | None = None,
    ssn_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from config import sanitize_work_plan

    proj_invs = ((payload or {}).get("project") or {}).get("investors") if isinstance((payload or {}).get("project"), dict) else None
    if ssn_model and isinstance(ssn_model.get("investors"), list) and ssn_model["investors"]:
        out = []
        for idx, inv in enumerate(ssn_model["investors"]):
            name = str(inv.get("name") or f"Investor {idx + 1}")
            inv_id = str(inv.get("id") or name).strip().lower().replace(" ", "_")
            ms_age = int(inv.get("milestone_age", 85))
            p_inv = proj_invs[idx] if (isinstance(proj_invs, list) and idx < len(proj_invs) and isinstance(proj_invs[idx], dict)) else {}
            raw_wp = p_inv.get("work_plan") if isinstance(p_inv.get("work_plan"), dict) else inv.get("work_plan")
            out.append(
                {
                    "id": inv_id,
                    "name": name,
                    "birth_year": int(inv.get("birth_year", 1975)),
                    "birth_month": int(inv.get("birth_month", 1)),
                    "birth_day": int(inv.get("birth_day", 1)),
                    "milestone_age": ms_age,
                    "rmd_owner": bool(inv.get("rmd_owner", idx == 0)),
                    "claim_setting_key": str(
                        inv.get("claim_setting_key") or f"ssn{name.strip().title().replace(' ', '')}ClaimAge"
                    ),
                    "default_claim_age": int(inv.get("default_claim_age", 67)),
                    "work_plan": sanitize_work_plan(raw_wp, milestone_age=ms_age, clamp_to_milestone=True),
                }
            )
        return out
    if ssn_model is None and payload and isinstance((payload.get("ssn_model") or {}).get("investors"), list) and payload["ssn_model"]["investors"]:
        return _get_investors_meta(payload, payload["ssn_model"])
    if payload and isinstance((payload.get("project") or {}).get("investors"), list):
        _, _, meta = _load_family_config_data(payload["project"])
        if meta:
            return meta
    return list(_family_defaults()[2])


def _resolve_investor_claim_ages(
    settings: dict[str, Any],
    ssn_model: dict[str, Any] | None = None,
) -> tuple[int, int]:
    meta = _get_investors_meta(None, ssn_model)
    ages: list[int] = []
    for idx in range(2):
        inv = meta[idx] if idx < len(meta) else None
        val = None
        gen_k = f"ssnInvestor{idx + 1}ClaimAge"
        if inv:
            ck = inv["claim_setting_key"]
            first_part = str(inv.get("name") or "").strip().split()[0] if str(inv.get("name") or "").strip() else ""
            first_ck = f"ssn{first_part.title()}ClaimAge" if first_part else ""
            if ck in settings and settings[ck] is not None:
                val = int(settings[ck])
            elif first_ck and first_ck in settings and settings[first_ck] is not None:
                val = int(settings[first_ck])
        if val is None and gen_k in settings and settings[gen_k] is not None:
            val = int(settings[gen_k])
        if val is None and inv:
            val = int(inv.get("default_claim_age", 67))
        ages.append(val if val is not None else 67)
    return ages[0], ages[1]


def load_live_payload(today: date | None = None) -> dict[str, Any]:
    """Loads the canonical `ReportPayload` from the active portfolio DB (or empty in-memory DB if none active)."""
    from config import get_default_config_path, get_default_db_path, load_config
    from db.database import PortfolioDB
    from exporters.view_model import build_report_payload

    cfg_path = get_default_config_path()
    cfg = load_config(cfg_path)
    refresh_family_config(cfg)
    db_path = get_default_db_path()
    if db_path is None:
        db = PortfolioDB(":memory:")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = PortfolioDB(db_path)
    try:
        return build_report_payload(db, cfg, today or date.today())
    finally:
        db.close()


def age_on_iso(birth_y: int, birth_m: int, birth_d: int, iso_str: str, bar_idx: int = 0) -> int:
    yr = int(iso_str[:4]) if len(iso_str) >= 4 else 2026
    mo = int(iso_str[5:7]) if len(iso_str) >= 7 else 1
    dy = int(iso_str[8:10]) if len(iso_str) >= 10 else 1
    age = yr - birth_y
    # A11: only bar 0 (today) compares the day-of-month; from bar 1 on, age ticks at the
    # start of the birth month. Intentional so milestone months land predictably.
    if mo < birth_m or (bar_idx == 0 and mo == birth_m and dy < birth_d):
        age -= 1
    return age


def ssa_fra_for_birth_year(birth_year: int) -> float:
    if birth_year <= 1937:
        return 65.0
    if birth_year <= 1942:
        return 65.0 + (birth_year - 1937) * (2.0 / 12.0)
    if birth_year <= 1954:
        return 66.0
    if birth_year <= 1959:
        return 66.0 + (birth_year - 1954) * (2.0 / 12.0)
    return 67.0


def ssa_claim_multiplier(claim_age: float, fra_age: float = 67.0) -> float:
    m_diff = round((claim_age - fra_age) * 12.0)
    if m_diff == 0:
        return 1.0
    if m_diff < 0:
        early_mo = min(60, abs(m_diff))
        first_36 = min(36, early_mo)
        rem_24 = max(0, early_mo - 36)
        return max(0.70, 1.0 - (first_36 * (5.0 / 900.0) + rem_24 * (5.0 / 1200.0)))
    max_delay = max(0, round((70.0 - fra_age) * 12.0))
    delay_mo = min(max_delay, m_diff)
    return 1.0 + delay_mo * (2.0 / 300.0)


def ssa_spousal_multiplier(claim_age: float, fra_age: float = 67.0) -> float:
    m_diff = round((claim_age - fra_age) * 12.0)
    if m_diff >= 0:
        return 1.0
    early_mo = min(60, abs(m_diff))
    first_36 = min(36, early_mo)
    rem_24 = max(0, early_mo - 36)
    return max(0.65, 1.0 - (first_36 * (25.0 / 3600.0) + rem_24 * (5.0 / 1200.0)))


def compute_monthly_ssn_for_iso(
    iso: str,
    month_idx: int,
    inv0_claim_age: int,
    inv1_claim_age: int,
    cola_pct: float,
    ssn_model: dict[str, Any],
) -> dict[str, Any]:
    investors = ssn_model.get("investors")
    if not isinstance(investors, list) or not investors:
        investors = [
            v for v in ssn_model.values()
            if isinstance(v, dict) and "pia_monthly_2026" in v
        ]
    meta = _get_investors_meta(None, ssn_model)

    inv0 = investors[0] if len(investors) > 0 else {}
    inv1 = investors[1] if len(investors) > 1 else {}
    m0 = meta[0] if len(meta) > 0 else {"birth_year": 1975, "birth_month": 1, "birth_day": 1}
    m1 = meta[1] if len(meta) > 1 else {"birth_year": 1975, "birth_month": 1, "birth_day": 1}

    # PORTABILITY DO: Default missing investor PIA to `0.0` and read `effective_ssn_tax_rate` from `ssn_model` (`LL-08`, `LL-10`).
    # PORTABILITY DON'T: Substitute household-specific monthly PIA dollar literals when an investor has no SSA profile.
    inv0_pia = float(inv0.get("pia_monthly_2026") or 0.0)
    inv1_pia = float(inv1.get("pia_monthly_2026") or 0.0) if len(investors) > 1 else 0.0
    eff_tax_rate = (
        float(ssn_model["effective_ssn_tax_rate"])
        if ssn_model.get("effective_ssn_tax_rate") is not None
        else 0.3145
    )

    b0_y = int(inv0.get("birth_year") or m0["birth_year"])
    b0_m = int(inv0.get("birth_month") or m0["birth_month"])
    b0_d = int(inv0.get("birth_day") or m0["birth_day"])

    b1_y = int(inv1.get("birth_year") or m1["birth_year"])
    b1_m = int(inv1.get("birth_month") or m1["birth_month"])
    b1_d = int(inv1.get("birth_day") or m1["birth_day"])

    fra0 = float(inv0["fra_age"]) if inv0.get("fra_age") is not None else ssa_fra_for_birth_year(b0_y)
    fra1 = float(inv1["fra_age"]) if inv1.get("fra_age") is not None else ssa_fra_for_birth_year(b1_y)

    cola_deflator = math.pow(1.0 + cola_pct / 100.0, month_idx / 12.0)
    inv0_age = age_on_iso(b0_y, b0_m, b0_d, iso, month_idx)
    inv1_age = age_on_iso(b1_y, b1_m, b1_d, iso, month_idx)
    # DESIGN & PARITY INVARIANT (`DA2-05` & `DA2-14` — lockstep with `computeMonthlySsnForIso` in `app_simulator.js`):
    # 1. `month_idx >= 0` (NOT `> 0`) so Bar 0 (`m == 0` dry-run preview) displays active Month-0 Social Security
    #    checks for retirees already claiming today (`run_simulation` only accumulates `cum_ssn_*` for `m > 0`).
    # 2. If an investor has an optional `mortality_age` configured and `age >= mortality_age`, transition to the
    #    SSA §202(e)/(f) 100% survivor benefit (`max(own_benefit, deceased_spouse_benefit)`).
    m0_mort = float(inv0["mortality_age"]) if inv0.get("mortality_age") is not None else None
    m1_mort = float(inv1["mortality_age"]) if (len(investors) > 1 and inv1.get("mortality_age") is not None) else None
    inv0_alive = m0_mort is None or inv0_age < m0_mort
    inv1_alive = bool(inv1) and (m1_mort is None or inv1_age < m1_mort)
    inv0_claimed = month_idx >= 0 and inv0_age >= inv0_claim_age
    inv1_claimed = month_idx >= 0 and inv1_age >= inv1_claim_age and bool(inv1)
    inv0_active = inv0_claimed and inv0_alive
    inv1_active = inv1_claimed and inv1_alive
    inv0_mult = ssa_claim_multiplier(inv0_claim_age, fra0)
    inv1_mult = ssa_claim_multiplier(inv1_claim_age, fra1)
    inv0_own_real = inv0_pia * inv0_mult
    inv1_own_real = inv1_pia * inv1_mult if bool(inv1) else 0.0
    inv0_real_mo = inv0_own_real if inv0_active else 0.0
    inv1_real_mo = inv1_own_real if inv1_active else 0.0
    # DA-C07: SSA §202(b) Spousal Top-Up Floor when both spouses are alive & claiming and one PIA < 50% of the other
    if inv0_active and inv1_active:
        if inv0_pia > 0.0 and 0.5 * inv0_pia > inv1_pia:
            inv1_real_mo += (0.5 * inv0_pia - inv1_pia) * ssa_spousal_multiplier(inv1_claim_age, fra1)
        elif inv1_pia > 0.0 and 0.5 * inv1_pia > inv0_pia:
            inv0_real_mo += (0.5 * inv1_pia - inv0_pia) * ssa_spousal_multiplier(inv0_claim_age, fra0)
    elif inv0_active and bool(inv1) and not inv1_alive:
        # SSA §202(e)/(f): Surviving spouse steps up to 100% of deceased spouse's benefit if higher
        inv0_real_mo = max(inv0_real_mo, inv1_own_real)
    elif inv1_active and not inv0_alive:
        inv1_real_mo = max(inv1_real_mo, inv0_own_real)
    inv0_nom_mo = inv0_real_mo * cola_deflator
    inv1_nom_mo = inv1_real_mo * cola_deflator
    tot_real_gross = inv0_real_mo + inv1_real_mo
    tot_nom_gross = inv0_nom_mo + inv1_nom_mo
    tot_nom_tax = tot_nom_gross * eff_tax_rate
    tot_nom_net = tot_nom_gross - tot_nom_tax
    tot_real_tax = tot_real_gross * eff_tax_rate
    tot_real_net = tot_real_gross - tot_real_tax
    return {
        "jAge": inv0_age,
        "yAge": inv1_age,
        "jActive": inv0_active,
        "yActive": inv1_active,
        "colaDeflator": cola_deflator,
        "jRealMo": inv0_real_mo,
        "yRealMo": inv1_real_mo,
        "jNomMo": inv0_nom_mo,
        "yNomMo": inv1_nom_mo,
        "totRealGross": tot_real_gross,
        "totNomGross": tot_nom_gross,
        "totNomTax": tot_nom_tax,
        "totNomNet": tot_nom_net,
        "totRealTax": tot_real_tax,
        "totRealNet": tot_real_net,
    }


def _coerce_birthday_candidates(family_birthdays: Any = None) -> list[dict[str, Any]]:
    if family_birthdays is None:
        return list(_family_defaults()[0])
    out: list[dict[str, Any]] = []
    if isinstance(family_birthdays, dict):
        for raw_name, raw_dob in family_birthdays.items():
            nm = str(raw_name or "").strip()
            if not nm:
                continue
            pts = str(raw_dob or "").strip().split("-")
            by = int(pts[0]) if len(pts) >= 1 and pts[0].isdigit() else 1975
            bm = int(pts[1]) if len(pts) >= 2 and pts[1].isdigit() else 1
            out.append({"name": nm, "y": by, "m": max(1, min(12, bm))})
        return out
    if isinstance(family_birthdays, (list, tuple)):
        for it in family_birthdays:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("name") or "").strip()
            if not nm:
                continue
            if it.get("y") is not None and it.get("m") is not None:
                out.append({"name": nm, "y": int(it["y"]), "m": max(1, min(12, int(it["m"])))})
            elif it.get("birth_year") is not None:
                out.append({"name": nm, "y": int(it["birth_year"]), "m": max(1, min(12, int(it.get("birth_month") or 1)))})
            else:
                pts = str(it.get("birth_date") or it.get("birth_iso") or it.get("dob") or "").strip().split("-")
                by = int(pts[0]) if len(pts) >= 1 and pts[0].isdigit() else 1975
                bm = int(pts[1]) if len(pts) >= 2 and pts[1].isdigit() else 1
                out.append({"name": nm, "y": by, "m": max(1, min(12, bm))})
        return out
    return list(_family_defaults()[0])


def _payload_family_birthdays(payload: dict[str, Any] | None) -> Any:
    if not isinstance(payload, dict):
        return None
    if payload.get("family_birthdays"):
        return payload["family_birthdays"]
    proj = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    if proj.get("family_birthdays"):
        return proj["family_birthdays"]
    if proj.get("family_members") or proj.get("investors") or proj.get("beneficiaries"):
        return _load_family_config_data(proj)[0]
    ssn_invs = ((payload.get("ssn_model") or {}).get("investors") or []) if isinstance(payload.get("ssn_model"), dict) else []
    if ssn_invs:
        return [
            {
                "name": str(x.get("name") or ""),
                "y": int(x.get("birth_year") or 1975),
                "m": int(x.get("birth_month") or 1),
            }
            for x in ssn_invs
            if isinstance(x, dict) and x.get("name")
        ]
    return None


def resolve_family_birthday_ym(person: str, age_years: float, family_birthdays: Any = None) -> str:
    """
    Computes the `YYYY-MM` date when a configured family member (investor or beneficiary)
    reaches `age_years` (supports integer or fractional ages; exact match first, then first-name/word-prefix).
    """
    p_low = str(person or "").strip().lower()
    if not p_low:
        raise ValueError("Empty family member name.")
    candidates = _coerce_birthday_candidates(family_birthdays)
    match = next((b for b in candidates if str(b.get("name") or "").strip().lower() == p_low), None)
    if not match:
        match = next(
            (
                b
                for b in candidates
                if (
                    (str(b.get("name") or "").strip().lower().split() or [""])[0] == p_low
                    or str(b.get("name") or "").strip().lower().startswith(p_low + " ")
                )
            ),
            None,
        )
    if not match:
        valid_names = ", ".join(str(b.get("name") or "") for b in candidates)
        raise ValueError(f"Unknown family member '{person}'. Valid names: {valid_names}.")
    age_f = float(age_years)
    if not math.isfinite(age_f):
        raise ValueError(f"Non-finite age '{age_years}'.")
    whole_years = int(math.floor(age_f))
    rem_months = int(round((age_f - whole_years) * 12.0))
    tot_months = (int(match["y"]) + whole_years) * 12 + (int(match["m"]) - 1) + rem_months
    return f"{tot_months // 12:04d}-{(tot_months % 12) + 1:02d}"


def format_family_ages_at_ym(ym: str, day_hint: int = 10) -> str:
    parts = ym.strip().split("-")
    yr = int(parts[0])
    mo = int(parts[1])
    days_in_mo = calendar.monthrange(yr, mo)[1]
    dy = min(day_hint, days_in_mo)
    target_dt = date(yr, mo, dy)
    ages = []
    for p in _family_defaults()[0]:
        birth_dt = date(p["y"], p["m"], p["d"])
        age_yrs = (target_dt - birth_dt).days / 365.2425
        ages.append((p["name"], age_yrs))
    ages.sort(key=lambda x: x[1], reverse=True)
    return ", ".join(
        f"{name}{' ' if idx == 0 else ': '}{age:.1f}yrs"
        for idx, (name, age) in enumerate(ages)
    )


def compute_trailing_inflation_info(
    payload: dict[str, Any] | None = None,
    years_count: int = 10,
    fallback_pct: float = 3.1,
    as_of_year: int | None = None,
) -> tuple[float, str]:
    """
    Computes trailing N-year compound annual growth rate (CAGR) of CPI inflation
    from historical cohorts annual data (`DA-B05`: exact `years_count` annual observations,
    with accurate range label matching the actual years sampled).
    """
    fallback_range = "2016–2025"
    if as_of_year is None:
        if isinstance(payload, dict) and payload.get("today_iso"):
            try:
                as_of_year = int(str(payload["today_iso"])[:4])
            except (ValueError, TypeError):
                as_of_year = None
    if as_of_year is None:
        try:
            import datetime
            as_of_year = datetime.date.today().year
        except Exception:
            as_of_year = 2026

    cohorts_data = (payload.get("cohorts") if isinstance(payload, dict) else None)
    if not cohorts_data:
        try:
            import json
            from config import DATA_DIR
            path = DATA_DIR / "historical_cohorts.json"
            if path.exists():
                cohorts_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cohorts_data = None

    if not isinstance(cohorts_data, dict):
        return (fallback_pct, fallback_range)

    avail = [
        y for y in cohorts_data.get("years", [])
        if isinstance(y, dict) and y.get("inflation_pct") is not None and int(y.get("year", 0)) <= as_of_year
    ]
    avail.sort(key=lambda x: int(x.get("year", 0)))
    matching = avail[-years_count:] if years_count > 0 else avail

    if len(matching) < max(5, years_count - 2):
        return (fallback_pct, fallback_range)

    prod = 1.0
    for y in matching:
        try:
            prod *= (1.0 + float(y["inflation_pct"]) / 100.0)
        except (ValueError, TypeError):
            return (fallback_pct, fallback_range)

    cagr = (prod ** (1.0 / len(matching)) - 1.0) * 100.0
    actual_range = f"{matching[0]['year']}–{matching[-1]['year']}"
    return (round(cagr, 1), actual_range)


def compute_trailing_inflation_cagr(
    payload: dict[str, Any] | None = None,
    years_count: int = 10,
    fallback_pct: float = 3.1,
    as_of_year: int | None = None,
) -> float:
    """
    Computes trailing N-year compound annual growth rate (CAGR) of CPI inflation
    from historical cohorts annual data (or payload['cohorts']), falling back to
    `fallback_pct` (3.1%) if data is unavailable or thin.
    """
    return compute_trailing_inflation_info(
        payload=payload,
        years_count=years_count,
        fallback_pct=fallback_pct,
        as_of_year=as_of_year,
    )[0]


# Official SSA Cost-of-Living Adjustments (effective January of distribution year)
# Source: Social Security Administration (SSA.gov/oact/cola/colahistory.html)
SSA_COLA_HISTORY: dict[int, float] = {
    2014: 1.5,
    2015: 1.7,
    2016: 0.0,
    2017: 0.3,
    2018: 2.0,
    2019: 2.8,
    2020: 1.6,
    2021: 1.3,
    2022: 5.9,
    2023: 8.7,
    2024: 3.2,
    2025: 2.5,
    2026: 2.8,
}


def compute_ssn_cola_rate(
    current_year: int | None = None,
    payload: dict[str, Any] | None = None,
    fallback_10y_avg: float = 3.1,
) -> dict[str, Any]:
    """
    Computes Social Security COLA rate as the exact 10-year geometric compound average
    across the 10 most recent published COLA years up to `current_year` (`DA-B04`: e.g. 2017–2026 = 10 COLAs).
    """
    if current_year is None:
        try:
            today_str = (payload.get("today_iso") if isinstance(payload, dict) else None)
            if today_str:
                current_year = int(str(today_str)[:4])
            else:
                from datetime import date as _dt_date
                current_year = _dt_date.today().year
        except Exception:
            current_year = 2026

    avail_yrs = sorted(y for y in SSA_COLA_HISTORY if y <= current_year)
    chosen_yrs = avail_yrs[-10:]
    if chosen_yrs:
        start_year = chosen_yrs[0]
        end_year = chosen_yrs[-1]
        span_label = f"{start_year}–{end_year}"
        prod = 1.0
        for y in chosen_yrs:
            prod *= 1.0 + (SSA_COLA_HISTORY[y] / 100.0)
        avg_rate = round((math.pow(prod, 1.0 / len(chosen_yrs)) - 1.0) * 100.0, 1)
        source = "SSA_HISTORICAL_AVERAGE"
    else:
        start_year = current_year - 9
        span_label = f"{start_year}–{current_year}"
        avg_rate = fallback_10y_avg
        source = "FALLBACK_10Y_AVERAGE"

    return {
        "cola_pct": avg_rate,
        "current_year": current_year,
        "start_year": start_year,
        "span_label": span_label,
        "ten_yr_avg_pct": avg_rate,
        "source": source,
    }


def get_reference_rates(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Returns the exact reference growth rates used by the `Ref:` buttons on the web UI:
    - `goog_ref_pct`: 5-year GOOG CAGR rounded to 0.1%/yr
    - `non_goog_ref_pct`: Weighted-average Non-GOOG CAGR rounded to 0.1%/yr
    """
    bs = payload.get("bucket_summary") or {}
    g = bs.get("goog") or {}
    ng = bs.get("non_goog") or {}

    g_raw = float(g.get("five_yr_cagr_pct") if g.get("five_yr_cagr_pct") is not None else (g.get("annual_appreciation_pct") or 0.0))
    ng_raw = float(ng.get("annual_appreciation_pct") or 0.0)

    g_ref = max(-20.0, min(20.0, round(g_raw * 10.0) / 10.0))
    ng_ref = max(-20.0, min(20.0, round(ng_raw * 10.0) / 10.0))

    meta = _get_investors_meta(payload)
    inf_ref = compute_trailing_inflation_cagr(payload, years_count=10, fallback_pct=3.1)
    burn_ref = 0.0
    if isinstance(payload, dict):
        if payload.get("reference_annual_burn") is not None:
            burn_ref = float(payload["reference_annual_burn"])
        elif payload.get("retirement_annual_burn") is not None:
            burn_ref = float(payload["retirement_annual_burn"])
        elif isinstance(payload.get("project"), dict):
            p = payload["project"]
            if p.get("reference_annual_burn") is not None:
                burn_ref = float(p["reference_annual_burn"])
            elif p.get("retirement_annual_burn") is not None:
                burn_ref = float(p["retirement_annual_burn"])
    if not burn_ref or burn_ref <= 0:
        try:
            from config import load_config
            cfg = load_config()
            burn_ref = float((cfg or {}).get("reference_annual_burn") or (cfg or {}).get("retirement_annual_burn") or 50000.0)
        except Exception:
            burn_ref = 50000.0
    cola_meta = compute_ssn_cola_rate(payload=payload)
    out: dict[str, Any] = {
        "goog_ref_pct": round(g_ref, 1),
        "goog_raw_cagr_pct": round(g_raw, 2),
        "goog_five_yr_start_date": g.get("five_yr_start_date"),
        "goog_five_yr_start_price": g.get("five_yr_start_price"),
        "goog_live_price": payload.get("goog_live_price"),
        "non_goog_ref_pct": round(ng_ref, 1),
        "non_goog_raw_cagr_pct": round(ng_raw, 2),
        "burn_ref_usd": round(burn_ref),
        "inflation_ref_pct": inf_ref,
        "ssn_cola_ref_pct": cola_meta["cola_pct"],
        "ssn_cola_span_label": cola_meta["span_label"],
        "ssn_cola_10y_avg_pct": cola_meta["ten_yr_avg_pct"],
    }
    for inv in meta:
        out[f"ssn_{inv['id']}_claim_ref_age"] = int(inv.get("default_claim_age", 67))
    return out


_VALID_YM_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _ym_to_abs_months(ym: str) -> int | None:
    s = str(ym or "").strip()
    m = _VALID_YM_RE.match(s)
    if not m:
        return None
    return int(m.group(1)) * 12 + (int(m.group(2)) - 1)


def _abs_months_to_ym(tot_m: int) -> str:
    return f"{tot_m // 12:04d}-{(tot_m % 12) + 1:02d}"


def normalize_scheduled_withdrawals(
    raw_list: list[dict[str, Any]] | None,
    today_iso: str,
    family_birthdays: Any = None,
) -> list[dict[str, Any]]:
    if not raw_list:
        return []
    start_year = int(today_iso[:4])
    start_month0 = int(today_iso[5:7]) - 1

    cleaned: list[dict[str, Any]] = []
    for it in raw_list:
        if not isinstance(it, dict):
            continue
        ym = str(it.get("ym") or it.get("date") or "").strip()
        if ym.lower() in {"next_month", "immediate", "now", "day_1"}:
            ym = _abs_months_to_ym(start_year * 12 + start_month0 + 1)
        abs_m = _ym_to_abs_months(ym)
        if abs_m is None and it.get("person") and it.get("age") is not None and not isinstance(it.get("age"), bool):
            try:
                ym = resolve_family_birthday_ym(str(it["person"]), float(it["age"]), family_birthdays=family_birthdays)
            except (TypeError, ValueError):
                ym = ""
            abs_m = _ym_to_abs_months(ym)
        if abs_m is None:
            continue
        norm_ym = _abs_months_to_ym(abs_m)

        amt = int(round(float(it.get("amount") or 0)))
        if amt <= 0:
            continue
        mode = "nominal" if str(it.get("dollar_mode") or "").lower() == "nominal" else "today"
        comment = str(it.get("comment") or it.get("purpose") or "").strip()[:120] or "Scheduled withdrawal"
        cleaned.append(
            {
                "ym": norm_ym,
                "amount": amt,
                "dollar_mode": mode,
                "comment": comment,
            }
        )

    cleaned.sort(key=lambda x: (x["ym"], -x["amount"], x["comment"]))
    return cleaned


def normalize_scheduled_actions(
    raw_list: list[dict[str, Any]] | None,
    today_iso: str,
    family_birthdays: Any = None,
) -> list[dict[str, Any]]:
    """
    Canonical normalizer for unified `simScheduledActions` (`withdrawal`, `deposit`, `transfer`
    across `one_time`, `monthly`, `quarterly`, `annual`, and `on_vest` cadences).
    """
    if not raw_list:
        return []
    start_abs = int(today_iso[:4]) * 12 + (int(today_iso[5:7]) - 1)
    min_abs = start_abs + 1
    max_abs = start_abs + SIM_TOTAL_MONTHS

    def _resolve_ym(raw_ym: Any, person: Any = None, age: Any = None, default_abs: int | None = None) -> str:
        s = str(raw_ym or "").strip()
        if s.lower() in {"next_month", "immediate", "now", "day_1"}:
            return _abs_months_to_ym(min_abs)
        am = _ym_to_abs_months(s)
        if am is not None:
            return _abs_months_to_ym(am)
        if person and age is not None and not isinstance(age, bool):
            try:
                resolved = resolve_family_birthday_ym(str(person), float(age), family_birthdays=family_birthdays)
            except (TypeError, ValueError):
                resolved = ""
            am_res = _ym_to_abs_months(resolved)
            if am_res is not None:
                return _abs_months_to_ym(am_res)
        if default_abs is None:
            return ""
        return _abs_months_to_ym(default_abs)

    cleaned: list[dict[str, Any]] = []
    for idx, it in enumerate(raw_list):
        if not isinstance(it, dict):
            continue
        act_type = str(it.get("type") or "withdrawal").strip().lower()
        if act_type not in ("withdrawal", "deposit", "transfer"):
            act_type = "withdrawal"
        cadence = str(it.get("cadence") or "one_time").strip().lower()
        if cadence not in ("one_time", "monthly", "quarterly", "annual", "on_vest"):
            cadence = "one_time"

        amount_mode = str(it.get("amount_mode") or "usd").strip().lower()
        if amount_mode not in ("usd", "pct_of_source"):
            amount_mode = "usd"
        try:
            amt = float(it.get("amount") or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            continue
        if amount_mode == "pct_of_source":
            amt = min(100.0, round(amt, 2))
        else:
            amt = round(amt, 2)

        dollar_mode = "nominal" if str(it.get("dollar_mode") or "").lower() == "nominal" else "today"
        from_id = str(it.get("from_asset_id") or it.get("from_id") or it.get("source") or ("auto" if act_type == "withdrawal" else "")).strip()
        to_id = str(it.get("to_asset_id") or it.get("to_id") or it.get("dest") or "").strip()
        if act_type == "transfer" and (not from_id or not to_id or from_id == to_id):
            continue
        if act_type == "deposit" and not to_id:
            continue
        if act_type == "withdrawal" and not from_id:
            from_id = "auto"

        if cadence == "one_time":
            ym = _resolve_ym(it.get("ym") or it.get("date"), it.get("person"), it.get("age"))
            if not ym:
                continue
            start_ym = ym
            end_ym = ym
        else:
            start_ym = _resolve_ym(
                it.get("start_ym") or it.get("ym") or it.get("date"),
                it.get("person"),
                it.get("start_age") if it.get("start_age") is not None else it.get("age"),
                default_abs=min_abs,
            )
            end_ym = _resolve_ym(
                it.get("end_ym"),
                it.get("person"),
                it.get("end_age"),
                default_abs=max_abs,
            )
            s_abs = _ym_to_abs_months(start_ym) or min_abs
            e_abs = _ym_to_abs_months(end_ym) or max_abs
            if e_abs < s_abs:
                end_ym = start_ym

        default_lbl = {
            "withdrawal": "Scheduled withdrawal",
            "deposit": "Scheduled deposit",
            "transfer": f"Transfer {from_id} → {to_id}",
        }[act_type]
        comment = str(it.get("comment") or it.get("purpose") or "").strip()[:140] or default_lbl
        entry: dict[str, Any] = {
            "id": str(it.get("id") or f"act_{idx + 1}"),
            "type": act_type,
            "cadence": cadence,
            "ym": start_ym if cadence == "one_time" else "",
            "start_ym": start_ym,
            "end_ym": end_ym,
            "amount": amt,
            "amount_mode": amount_mode,
            "dollar_mode": dollar_mode,
            "from_asset_id": from_id,
            "to_asset_id": to_id,
            "comment": comment,
        }
        if it.get("person"):
            entry["person"] = str(it["person"])
        if it.get("age") is not None:
            entry["age"] = float(it["age"])
        cleaned.append(entry)

    cleaned.sort(key=lambda x: (x["start_ym"], 0 if x["type"] == "deposit" else (1 if x["type"] == "transfer" else 2), x["id"]))
    return cleaned


def _action_triggers_in_ym(action: dict[str, Any], ym: str, vested_this_month: bool = False) -> bool:
    cad = action.get("cadence") or "one_time"
    if cad == "one_time":
        return action.get("ym") == ym
    cur_abs = _ym_to_abs_months(ym)
    s_abs = _ym_to_abs_months(action.get("start_ym") or "")
    e_abs = _ym_to_abs_months(action.get("end_ym") or "")
    if cur_abs is None or s_abs is None or e_abs is None:
        return False
    if cur_abs < s_abs or cur_abs > e_abs:
        return False
    diff = cur_abs - s_abs
    if cad == "monthly":
        return True
    if cad == "quarterly":
        return (diff % 3) == 0
    if cad == "annual":
        return (diff % 12) == 0
    if cad == "on_vest":
        return bool(vested_this_month)
    return False


def normalize_hypothetical_assets(
    raw_hyps: list[dict[str, Any]] | None,
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Normalizes `simHypotheticalAssets` entries. Every hypothetical product strictly starts at
    `initial_value: 0.0` on Day 0 (conservation of money; funded via `simScheduledActions`).
    """
    if not raw_hyps:
        return []
    from settings import RISK_CLASSES

    proj = (payload or {}).get("project") or {}
    if not proj:
        try:
            from config import load_config

            proj = load_config() or {}
        except Exception:
            proj = {}
    tax_rates = proj.get("tax_rates") or {}
    ltcg_top = float(tax_rates.get("ltcg_top") if tax_rates.get("ltcg_top") is not None else 0.0)
    ord_top = float(tax_rates.get("ordinary_top") if tax_rates.get("ordinary_top") is not None else 0.0)
    niit_rate = float(tax_rates.get("niit_rate") if tax_rates.get("niit_rate") is not None else 0.038)
    state_rate = float(tax_rates.get("state_rate") if tax_rates.get("state_rate") is not None else 0.0)
    cash_int_tax = float(proj.get("sim_cash_interest_tax_rate") if proj.get("sim_cash_interest_tax_rate") is not None else ord_top)

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_hyps):
        if not isinstance(raw, dict):
            continue
        raw_id = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw.get("id") or raw.get("label") or f"hyp_{idx + 1}").strip().lower()).strip("_")
        if not raw_id.startswith("hyp_"):
            raw_id = f"hyp_{raw_id}"
        if raw_id in seen:
            continue
        seen.add(raw_id)

        label = str(raw.get("label") or raw.get("title") or raw_id).strip()[:80]
        inst = str(raw.get("institution") or "Hypothetical Account").strip()[:60]
        prod_ref = str(raw.get("real_product_ref") or raw.get("subtitle") or "Real-world hypothetical product").strip()[:160]
        rc = str(raw.get("risk_class") or "broad_equity").strip().lower()
        if rc not in RISK_CLASSES:
            rc = "broad_equity"
        try:
            gp = round(max(-20.0, min(25.0, float(raw.get("growth_pct", raw.get("rate_or_spread_pct", 0.0))))), 2)
        except (TypeError, ValueError):
            gp = 0.0
        try:
            er = round(max(0.0, min(5.0, float(raw.get("expense_ratio_pct", 0.0) or 0.0))), 4)
        except (TypeError, ValueError):
            er = 0.0

        # PORTABILITY DO: Subtract configured `state_rate` / `niit_rate` (from `project.json -> tax_rates`) (`LL-10`).
        # DESIGN & PARITY INVARIANT (`DA2-03` — lockstep with `materializeSimAssets` in `app_simulator.js`):
        #   - Hypothetical `TRAD_IRA` (`ORDINARY_FULL`) is exempt from 3.8% NIIT under IRC §1411(c)(5)
        #     (`tax_rate = max(0, ord_top - niit_rate)`), matching `view_model.py`.
        #   - Labels describe progressive tax brackets (`0%..ceiling`) rather than obsolete flat rates.
        tw = str(raw.get("tax_wrapper") or "taxable_ltcg").strip().lower()
        if tw in ("state_tax_exempt", "treasury", "us_treasury"):
            tax_model = "TREASURY"
            tax_rate = round(max(0.0, ord_top - state_rate), 4)
            tax_title = f"US Treasury (Prog. Fed Ord.+NIIT up to {tax_rate * 100:.1f}% on Yield, 0% State)"
        elif tw in ("taxable_yield", "taxable_bond"):
            tax_model = "TAXABLE_YIELD"
            tax_rate = ord_top
            tax_title = f"Taxable Fixed Income (Prog. Ord.+NIIT up to {ord_top * 100:.1f}% on Yield)"
        elif tw in ("dual_tax_exempt", "muni"):
            tax_model = "MUNI"
            tax_rate = ltcg_top
            tax_title = "0% Fed/State Tax-Exempt Yield"
        elif tw in ("tax_free", "roth", "roth_ira"):
            tax_model = "TAX_FREE"
            tax_rate = 0.0
            tax_title = "0% Tax-Free Wrapper"
        elif tw in ("annuity_lifo", "ordinary_deferred"):
            tax_model = "ANNUITY_LIFO"
            tax_rate = ord_top
            tax_title = f"IRS §72 LIFO (Prog. Ord.+NIIT up to {ord_top * 100:.1f}% on Gain)"
        elif tw in ("trad_ira", "traditional_ira", "ordinary_full", "403b", "401k", "trad_401k", "sep_ira", "simple_ira"):
            tw = tw.upper() if tw in ("403b", "401k", "trad_401k", "sep_ira", "simple_ira") else "TRAD_IRA"
            tax_model = "ORDINARY_FULL"
            tax_rate = round(max(0.0, ord_top - niit_rate), 4)
            tax_title = f"Traditional IRA (Prog. Ord. Income up to {tax_rate * 100:.1f}% + RMD, 0% NIIT)"
        elif tw in ("cash_sweep", "cash"):
            tax_model = "CASH"
            tax_rate = cash_int_tax
            tax_title = "0% Tax on Principal (Prog. Ord. Tax on Sweep Interest)"
        else:
            tw = "taxable_ltcg"
            tax_model = "LTCG_POOL"
            tax_rate = ltcg_top
            tax_title = f"Progressive LTCG (0%–{ltcg_top * 100:.1f}% ceiling) above Stepped-Up Basis"

        from settings import parse_rmd_owner_index

        owner_id = str(raw.get("owner_id") or "").strip()
        rmd_owner_index = parse_rmd_owner_index(raw.get("rmd_owner_index"))

        unlock_ym = str(raw.get("unlock_ym") or "").strip() or None
        maturity_iso = f"{unlock_ym}-01" if (unlock_ym and len(unlock_ym) >= 7) else None
        unlock_disp = (
            f"{unlock_ym[5:7]}-{unlock_ym[0:4]}"
            if (unlock_ym and re.fullmatch(r"\d{4}-\d{2}", unlock_ym))
            else unlock_ym
        )

        out.append(
            {
                "id": raw_id,
                "label": label,
                "institution": inst,
                "real_product_ref": prod_ref,
                "initial_value": 0.0,
                "growth_pct": gp,
                "risk_class": rc,
                "expense_ratio_pct": er,
                "mc_vol_pct": float(raw.get("mc_vol_pct", RISK_CLASSES[rc]["default_vol_pct"])),
                "mc_corr_primary": float(raw.get("mc_corr_primary", RISK_CLASSES[rc]["default_corr_mkt"])),
                "tax_wrapper": tw,
                "unlock_ym": unlock_ym,
                "owner_id": owner_id,
                "rmd_owner_index": rmd_owner_index,
                # Runtime simulator bucket fields:
                "default_rank": 90 + idx,
                "color": "#2dd4bf",
                "title": f"✦ {label}",
                "short_title": f"✦ {label[:28]}",
                "subtitle": f"{inst} • {prod_ref}",
                "account_label": f"{inst} (Hypothetical)",
                "bucket": "goog" if rc == "single_stock" else "non_goog",
                "is_hypothetical": True,
                "is_unvested_goog": False,
                "is_goog_priced": False,
                "shares": None,
                "current_value": 0.0,
                "cost_basis": 0.0,
                "basis_sub": "Starts at $0 • Funded via Scheduled Actions",
                "maturity_iso": maturity_iso,
                "liquidity_label": f"Locked until {unlock_disp}" if unlock_disp else "Liquid ($0 Day 0)",
                "liquidity_sub": prod_ref,
                "tax_model": tax_model,
                "tax_rate": tax_rate,
                "tax_rule_title": tax_title,
                "tax_rule_detail": f"Hypothetical product ({prod_ref}). Starts at $0 and receives net proceeds from Scheduled Actions.",
                "rate_model": "slider",
                "annual_rate_pct": gp,
                "forecast_key": raw_id,
                "parent_forecast_keys": [],
                "ref_growth_pct": RISK_CLASSES[rc]["benchmark_return_pct"],
                "default_growth_pct": gp,
                "default_risk_class": rc,
                "tranches": [],
            }
        )
    return out


_HYP_OWNER_INHERIT_WRAPPERS = {
    "TRAD_IRA",
    "ROTH_IRA",
    "TAX_FREE",
    "ROTH",
    "403B",
    "401K",
    "TRAD_401K",
    "SEP_IRA",
    "SIMPLE_IRA",
}


def _inherit_rollover_owner_if_unset(src_obj: dict[str, Any], dst_obj: dict[str, Any]) -> None:
    """Copy owner RMD/age attribution from `src_obj` onto `dst_obj` when `dst_obj` has no explicit owner."""
    if dst_obj.get("rmd_owner_index") is not None or str(dst_obj.get("owner_id") or "").strip():
        return
    src_idx = src_obj.get("rmd_owner_index")
    src_oid = str(src_obj.get("owner_id") or "").strip()
    src_oby = src_obj.get("owner_birth_year")
    if src_idx is None and not src_oid and not src_oby:
        return
    if src_idx is not None:
        dst_obj["rmd_owner_index"] = int(src_idx)
    if src_oid:
        dst_obj["owner_id"] = src_oid
    if src_oby:
        dst_obj["owner_birth_year"] = int(src_oby)
    if src_obj.get("owner_birth_month"):
        dst_obj["owner_birth_month"] = int(src_obj["owner_birth_month"])
    if src_obj.get("owner_birth_day"):
        dst_obj["owner_birth_day"] = int(src_obj["owner_birth_day"])


def materialize_sim_assets(
    base_sim_assets: list[dict[str, Any]],
    eff: dict[str, Any],
    payload: dict[str, Any] | None = None,
    rollovers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Combines actual `retirement_sim_assets` with `$0`-initial-value `simHypotheticalAssets`."""
    from settings import parse_rmd_owner_index

    eff = eff or {}
    assets = [copy.deepcopy(a) for a in (base_sim_assets or []) if not a.get("is_hypothetical")]
    hyps = normalize_hypothetical_assets(eff.get("simHypotheticalAssets") or [], payload)
    base_by_id = {a["id"]: a for a in assets if a.get("id")}
    inv_list = _get_investors_meta(payload) if payload else []
    proj_invs = ((payload or {}).get("project") or {}).get("investors") if isinstance((payload or {}).get("project"), dict) else None

    transfer_specs: list[dict[str, Any]] = []
    for r_item in [*(rollovers or []), *(eff.get("hypothetical_rollovers") or [])]:
        if isinstance(r_item, dict):
            transfer_specs.append(r_item)
    for act in eff.get("simScheduledActions") or []:
        if isinstance(act, dict) and str(act.get("type") or "").strip().lower() == "transfer":
            transfer_specs.append(act)

    existing_ids = {a["id"] for a in assets}
    for h in hyps:
        if h["id"] in existing_ids:
            continue
        h_oid = str(h.get("owner_id") or "").strip()
        h_idx = parse_rmd_owner_index(h.get("rmd_owner_index"))
        if h_oid or h_idx is not None:
            matched_idx: int | None = None
            matched_inv: dict[str, Any] | None = None
            if h_oid:
                oid_low = h_oid.lower()
                for idx_i, inv_i in enumerate(inv_list):
                    raw_pid = (
                        str(proj_invs[idx_i].get("id") or "").strip().lower()
                        if isinstance(proj_invs, list) and idx_i < len(proj_invs) and isinstance(proj_invs[idx_i], dict)
                        else ""
                    )
                    if (
                        str(inv_i.get("id") or "").strip().lower() == oid_low
                        or raw_pid == oid_low
                        or str(inv_i.get("name") or "").strip().lower() == oid_low
                    ):
                        matched_idx = idx_i
                        matched_inv = inv_i
                        break
            if matched_inv is None and h_idx is not None and 0 <= h_idx < len(inv_list):
                matched_idx = h_idx
                matched_inv = inv_list[h_idx]
            if matched_inv is not None:
                h["owner_id"] = h_oid or str(matched_inv.get("id") or "")
                h["rmd_owner_index"] = matched_idx
                h["owner_birth_year"] = int(matched_inv.get("birth_year") or 1975)
                h["owner_birth_month"] = int(matched_inv.get("birth_month") or 1)
                h["owner_birth_day"] = 1
            else:
                src_match = next(
                    (
                        a
                        for a in assets
                        if (h_idx is not None and a.get("rmd_owner_index") == h_idx)
                        or (h_oid and str(a.get("owner_id") or "").strip().lower() == h_oid.lower())
                    ),
                    None,
                )
                if src_match is not None:
                    if h_idx is None and src_match.get("rmd_owner_index") is not None:
                        h["rmd_owner_index"] = int(src_match["rmd_owner_index"])
                    if src_match.get("owner_birth_year"):
                        h["owner_birth_year"] = int(src_match["owner_birth_year"])
                    if src_match.get("owner_birth_month"):
                        h["owner_birth_month"] = int(src_match["owner_birth_month"])
                    h["owner_birth_day"] = 1
        elif str(h.get("tax_wrapper") or "").upper() in _HYP_OWNER_INHERIT_WRAPPERS:
            for tr in transfer_specs:
                to_cand = str(
                    tr.get("to_asset_id") or tr.get("to_id") or tr.get("target_asset_id") or tr.get("to") or tr.get("dest") or ""
                ).strip()
                if to_cand != h["id"]:
                    continue
                from_cand = str(
                    tr.get("from_asset_id") or tr.get("from_id") or tr.get("source_asset_id") or tr.get("from") or tr.get("source") or ""
                ).strip()
                src_a = base_by_id.get(from_cand)
                if src_a and (src_a.get("rmd_owner_index") is not None or src_a.get("owner_birth_year")):
                    _inherit_rollover_owner_if_unset(src_a, h)
                    break

        assets.append(h)
        existing_ids.add(h["id"])

    # DA-A01: Guarantee a taxable CASH sink exists whenever pre-tax retirement buckets exist so
    # surplus after-tax January RMD cash can never vanish even on portfolios with 0 cash sweeps.
    has_pretax = any(
        str(a.get("tax_wrapper") or "").upper() in {"TRAD_IRA", "401K", "TRAD_401K", "SEP_IRA", "SIMPLE_IRA", "403B"}
        for a in assets
    )
    has_taxable_cash = any(
        a.get("tax_model") == "CASH" and str(a.get("tax_wrapper") or "TAXABLE").upper() == "TAXABLE"
        for a in assets
    )
    if has_pretax and not has_taxable_cash and "rmd_cash_sweep" not in existing_ids:
        proj = (payload or {}).get("project") or {}
        tax_rates = proj.get("tax_rates") or {}
        fed_ord = float(tax_rates.get("fed_ordinary_top") if tax_rates.get("fed_ordinary_top") is not None else 0.37)
        niit_r = float(tax_rates.get("niit_rate") if tax_rates.get("niit_rate") is not None else 0.038)
        state_r = float(tax_rates.get("state_rate") if tax_rates.get("state_rate") is not None else 0.0)
        ord_top = float(tax_rates["ordinary_top"]) if tax_rates.get("ordinary_top") is not None else (fed_ord + niit_r + state_r)
        cash_int_tax = float(proj["sim_cash_interest_tax_rate"]) if proj.get("sim_cash_interest_tax_rate") is not None else ord_top
        assets.append(
            {
                "id": "rmd_cash_sweep",
                "account_id": None,
                "tax_wrapper": "TAXABLE",
                "default_rank": 45,
                "color": "#94a3b8",
                "title": "Taxable Cash Sweep (RMD Reinvestment)",
                "short_title": "Cash Sweep (RMD)",
                "subtitle": "Taxable FDIC Sweep • Receives Surplus After-Tax RMDs",
                "account_label": "Taxable Sweep",
                "bucket": "non_goog",
                "is_unvested_goog": False,
                "is_goog_priced": False,
                "shares": None,
                "current_value": 0.0,
                "cost_basis": 0.0,
                "basis_sub": "1:1 USD Principal (0% gain)",
                "maturity_iso": None,
                "liquidity_label": "Liquid Now",
                "liquidity_sub": "Immediate cash availability",
                "tax_model": "CASH",
                "tax_rate": cash_int_tax,
                "tax_rule_title": "0% Tax on Principal",
                "tax_rule_detail": "Receives surplus after-tax RMD distributions; only future sweep interest is taxed.",
                "rate_model": "slider",
                "annual_rate_pct": None,
                "forecast_key": "rmd_cash_sweep",
                "parent_forecast_keys": [],
                "ref_growth_pct": 3.5,
                "default_growth_pct": 3.5,
                "default_risk_class": "cash_fixed",
                "tranches": [],
            }
        )
    return assets


def resolve_asset_forecast(
    asset: dict[str, Any],
    eff: dict[str, Any],
    primary_sym: str = "",
) -> dict[str, Any]:
    """Resolves `(growth_pct, risk_class, expense_ratio_pct, is_override, risk_overridden)` for any simulator bucket, including DP-5 bi-directional sleeve roll-ups."""
    from settings import infer_risk_class, resolve_holding_forecast

    aid = str(asset.get("id") or "")
    fk = str(asset.get("forecast_key") or aid)
    parent_keys = list(asset.get("parent_forecast_keys") or [])
    if not asset.get("is_hypothetical") and asset.get("bucket") in ("primary_equity", "goog") and primary_sym and primary_sym not in parent_keys and fk != primary_sym:
        parent_keys.append(primary_sym)

    def_rc = str(
        asset.get("default_risk_class")
        or asset.get("risk_class")
        or infer_risk_class(
            asset_id=aid,
            bucket=str(asset.get("bucket") or ""),
            rate_model=str(asset.get("rate_model") or ""),
            tax_model=str(asset.get("tax_model") or ""),
            primary_symbol=primary_sym,
        )
    )
    if asset.get("is_hypothetical"):
        def_gp = float(asset.get("growth_pct", 0.0))
    elif asset.get("rate_model") == "fixed" and asset.get("annual_rate_pct") is not None and not asset.get("constituent_forecasts"):
        def_gp = float(asset["annual_rate_pct"])
    elif ("primaryGrowthPct" in eff or "futureGoogGrowthPct" in eff) and def_rc == "single_stock" and "holdingForecasts" not in eff:
        def_gp = float(eff.get("primaryGrowthPct") if eff.get("primaryGrowthPct") is not None else eff.get("futureGoogGrowthPct") or 0.0)
    elif ("diversifiedGrowthPct" in eff or "futureNonGoogGrowthPct" in eff) and def_rc != "single_stock" and "holdingForecasts" not in eff:
        def_gp = float(eff.get("diversifiedGrowthPct") if eff.get("diversifiedGrowthPct") is not None else eff.get("futureNonGoogGrowthPct") or 0.0)
    else:
        def_gp = float(asset.get("default_growth_pct") if asset.get("default_growth_pct") is not None else (asset.get("annual_rate_pct") or 0.0))

    hf = eff.get("holdingForecasts") if isinstance(eff.get("holdingForecasts"), dict) else {}
    resolved = resolve_holding_forecast(
        hf,
        fk,
        parent_keys=parent_keys,
        fallback_growth_pct=def_gp,
        fallback_risk_class=def_rc,
    )
    final_gp = float(resolved["growth_pct"])
    constituents = asset.get("constituent_forecasts") or []
    if constituents and not (isinstance(hf.get(fk), dict) and hf[fk].get("use_reference")) and any(str(c.get("forecast_key") or "") in hf for c in constituents):
        parent_has_override = bool(resolved.get("is_override"))
        parent_gp = final_gp
        w_total = 0.0
        w_gp_sum = 0.0
        for c in constituents:
            ck = str(c.get("forecast_key") or "")
            cw = float(c.get("weight") or 0.0)
            if cw <= 0.0:
                continue
            w_total += cw
            if ck and ck in hf:
                cres = resolve_holding_forecast(
                    hf,
                    ck,
                    parent_keys=(),
                    fallback_growth_pct=float(c.get("default_growth_pct") or 0.0),
                    fallback_risk_class=str(c.get("default_risk_class") or def_rc),
                )
                cgp = float(cres["growth_pct"])
            elif parent_has_override:
                cgp = parent_gp
            else:
                cgp = float(c.get("default_growth_pct") or 0.0)
            w_gp_sum += cw * cgp
        if w_total > 0.0:
            final_gp = max(-20.0, min(25.0, w_gp_sum / w_total))

    opt_mult = float(eff.get("simOptimismMultiplier") if eff.get("simOptimismMultiplier") is not None else 1.0)
    resolved_rc = str(resolved["risk_class"])
    is_contractual_fixed = (
        asset.get("rate_model") == "fixed"
        or str(asset.get("return_source") or "") == "CONTRACT_CREDITED_RATE"
        or resolved_rc == "cash_fixed"
    )
    base_gp = final_gp
    multiplier = 1.0 if is_contractual_fixed else opt_mult
    final_gp *= multiplier

    # DA-B03: Deduct user-entered advisory/contract management fee (`fee_pct`).
    # Do not double-deduct fund-level `expense_ratio_pct` on trailing net-NAV ETF CAGRs.
    adv_fee = float(asset.get("fee_pct") or 0.0)
    er = adv_fee if asset.get("is_hypothetical") else (adv_fee + float(asset.get("expense_ratio_pct") or 0.0))
    return {
        "growth_pct": final_gp,
        "base_growth_pct": base_gp,
        "growth_multiplier": multiplier,
        "risk_class": resolved_rc,
        "vol_pct": resolved.get("vol_pct"),
        "expense_ratio_pct": er,
        "fee_pct": adv_fee,
        "is_override": bool(resolved.get("is_override")),
        "risk_overridden": bool(resolved.get("risk_overridden")),
    }


def _utc_days_between(start_iso: str, end_iso: str) -> int:
    try:
        d0 = date.fromisoformat(str(start_iso)[:10])
        d1 = date.fromisoformat(str(end_iso)[:10])
        return (d1 - d0).days
    except (TypeError, ValueError):
        return 0


def _holding_evidence_by_tier(asset: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, float]]:
    entries: list[dict[str, Any]] = []
    by_tier: dict[int, float] = {}
    for row in asset.get("growth_evidence") or []:
        tier, rate = row.get("tier"), row.get("projected_return_pct")
        if tier in (1, 2, 3) and isinstance(rate, (float, int)) and math.isfinite(rate):
            entries.append(row)
            by_tier.setdefault(int(tier), float(rate))
    entries.sort(key=lambda r: int(r["tier"]))
    return entries, by_tier


def _contractual_lock_years(asset: dict[str, Any], today_iso: str = "") -> int:
    mat_raw = str(asset.get("maturity_iso") or asset.get("maturity_date") or "")[:10]
    t_str = str(today_iso or "")[:10]
    if not mat_raw or not t_str or mat_raw <= t_str:
        return 0
    rm = str(asset.get("rate_model") or "")
    src = str(asset.get("return_source") or "")
    if rm == "fixed" or src in ("CONTRACT_CREDITED_RATE", "STATEMENT_YTM", "STATEMENT_COUPON_YIELD"):
        days = _utc_days_between(t_str, mat_raw)
        if days > 0:
            return max(1, math.ceil(days / 365.2425))
    return 0


def _has_maturing_constituents(asset: dict[str, Any], today_iso: str = "") -> bool:
    t_str = str(today_iso or "")[:10]
    if not t_str:
        return False
    for c in asset.get("constituent_forecasts") or []:
        if _contractual_lock_years(c, t_str) > 0:
            return True
    return False


def _holding_fade_target(asset: dict[str, Any], today_iso: str = "") -> float | None:
    if asset.get("is_hypothetical"):
        return None
    entries, by_tier = _holding_evidence_by_tier(asset)
    if not entries:
        return None
    sel_tier = int(entries[0]["tier"])
    lower_tiers = [t for t in (2, 3) if t > sel_tier and t in by_tier]
    if not lower_tiers:
        return None
    rc = str(asset.get("default_risk_class") or asset.get("risk_class") or "")
    rm = str(asset.get("rate_model") or "")
    src = str(asset.get("return_source") or "")
    lock_yrs = _contractual_lock_years(asset, today_iso)
    has_mat_children = _has_maturing_constituents(asset, today_iso)
    if not has_mat_children and lock_yrs == 0 and (rc == "cash_fixed" or rm == "fixed" or src == "CONTRACT_CREDITED_RATE"):
        return None
    return by_tier[lower_tiers[-1]]


def _single_holding_curve_at(
    asset: dict[str, Any],
    start: float,
    half_life: float,
    year: int,
    today_iso: str = "",
) -> float:
    from settings import FLAT_HALF_LIFE_YEARS

    if half_life >= FLAT_HALF_LIFE_YEARS:
        return float(start)
    entries, by_tier = _holding_evidence_by_tier(asset)
    if not entries:
        return float(start)
    sel_tier = int(entries[0]["tier"])
    lower_tiers = [t for t in (2, 3) if t > sel_tier and t in by_tier]
    if not lower_tiers:
        return float(start)
    h = max(1.0, min(30.0, float(half_life or 5.0)))
    lock_yrs = _contractual_lock_years(asset, today_iso)
    if lock_yrs > 0:
        if year < lock_yrs:
            return float(start)
        u = year - lock_yrs
        if 2 in by_tier and 3 in by_tier:
            t2, t3 = by_tier[2], by_tier[3]
            return t3 + (t2 - t3) * (0.5 ** (u / (2.0 * h)))
        return float(by_tier[lower_tiers[-1]])
    if sel_tier == 1 and 2 in by_tier and 3 in by_tier:
        t2, t3 = by_tier[2], by_tier[3]
        return t3 + (t2 - t3) * (0.5 ** (year / (2.0 * h))) + (float(start) - t2) * (0.5 ** (year / h))
    dest = by_tier[lower_tiers[0]]
    return dest + (float(start) - dest) * (0.5 ** (year / h))


def _holding_return_curve(
    asset: dict[str, Any],
    start: float,
    half_life: float,
    today_iso: str = "",
    holding_forecasts: dict[str, Any] | None = None,
    years_by_key: dict[str, Any] | None = None,
):
    from settings import FLAT_HALF_LIFE_YEARS, resolve_holding_forecast

    target = _holding_fade_target(asset, today_iso)
    if target is None or half_life >= FLAT_HALF_LIFE_YEARS:
        return lambda _year: float(start)

    hf = holding_forecasts or {}
    ybk = years_by_key or {}
    parent_key = str(asset.get("forecast_key") or asset.get("id") or "")

    if _has_maturing_constituents(asset, today_iso):
        c_specs = []
        w_sum = 0.0
        base_w_start = 0.0
        for c in asset.get("constituent_forecasts") or []:
            cw = float(c.get("weight") or 0.0)
            if cw <= 0:
                continue
            ck = str(c.get("forecast_key") or "")
            child_res = resolve_holding_forecast(
                hf,
                ck,
                parent_keys=[parent_key] if parent_key else (),
                fallback_growth_pct=float(c.get("default_growth_pct") or 0.0),
                fallback_risk_class=str(c.get("default_risk_class") or "fixed_income"),
            )
            c_start = float(child_res["growth_pct"])
            child_years = next((ybk[k] for k in [ck, *(c.get("parent_forecast_keys") or [])] if k in ybk), None)
            child_h = float(child_years if child_years is not None else half_life)
            w_sum += cw
            base_w_start += cw * c_start
            c_specs.append((cw, c, c_start, child_h))
        if w_sum > 0 and c_specs:
            shift = float(start) - (base_w_start / w_sum)

            def _ladder_curve(year: int) -> float:
                tot = 0.0
                for cw, c, c_start, child_h in c_specs:
                    tot += cw * _single_holding_curve_at(c, c_start + shift, child_h, year, today_iso)
                return tot / w_sum

            return _ladder_curve

    child_deltas = []
    for c in asset.get("constituent_forecasts") or []:
        ck = str(c.get("forecast_key") or "")
        child_years = next((ybk[k] for k in [ck, *(c.get("parent_forecast_keys") or [])] if k in ybk), None)
        if child_years is None or child_years == half_life:
            continue
        if _holding_fade_target(c, today_iso) is None:
            continue
        cw = float(c.get("weight") or 0.0)
        if cw <= 0:
            continue
        child_res = resolve_holding_forecast(
            hf,
            ck,
            parent_keys=[parent_key] if parent_key else (),
            fallback_growth_pct=float(c.get("default_growth_pct") or 0.0),
            fallback_risk_class=str(c.get("default_risk_class") or "broad_equity"),
        )
        child_deltas.append((cw, c, float(child_res["growth_pct"]), float(child_years)))

    def _curve(year: int) -> float:
        rate = _single_holding_curve_at(asset, start, half_life, year, today_iso)
        for cw, c, c_start, child_h in child_deltas:
            rate += cw * (
                _single_holding_curve_at(c, c_start, child_h, year, today_iso)
                - _single_holding_curve_at(c, c_start, half_life, year, today_iso)
            )
        return rate

    return _curve


def _build_year_rate_adjuster(assets, settings, primary_sym, mc=None, today_iso: str = ""):
    """Recenter annual gross draws on the year's forecast, preserving shocks and fees."""
    years_by_key = settings.get("holdingHalfLives") or {}
    hf = settings.get("holdingForecasts") or {}
    mc = mc or {}
    curves = []
    for a in assets:
        target = _holding_fade_target(a, today_iso)
        if target is None:
            continue
        fc = resolve_asset_forecast(a, settings, primary_sym)
        forced = (mc.get("googExpectedPct", mc.get("singleStockExpectedPct")) if fc["risk_class"] == "single_stock"
                  else mc.get("nonGoogExpectedPct", mc.get("broadEquityExpectedPct")))
        lock_yrs = _contractual_lock_years(a, today_iso)
        if forced is not None or fc["growth_multiplier"] == 0 or (fc["risk_class"] == "cash_fixed" and (fc.get("risk_overridden") or lock_yrs == 0)):
            continue
        key = a.get("forecast_key") or a["id"]
        years = next((years_by_key[k] for k in [key, *(a.get("parent_forecast_keys") or [])]
                      if k in years_by_key), 5)
        curve_fn = _holding_return_curve(a, fc["base_growth_pct"], years, today_iso, hf, years_by_key)
        curves.append((a["id"], fc["growth_pct"] / 100, fc["expense_ratio_pct"] / 100, fc["growth_multiplier"], curve_fn))

    factors = {}
    last = {}
    def adjust(rates, year):
        if year <= 0 or not curves:
            return rates
        if year in last and last[year][0] is rates:
            return last[year][1]
        if year not in factors:
            factors[year] = []
            for aid, start, fee, multiplier, curve_fn in curves:
                rate = curve_fn(year)
                factors[year].append((aid, fee, (1 + rate * multiplier / 100) / (1 + start)))
        out = rates.copy()
        for aid, fee, ratio in factors[year]:
            if ratio != 1 and aid in rates:
                gross = (1 + rates[aid]) ** 12 + fee
                out[aid] = max(0.0001, gross * ratio - fee) ** (1 / 12) - 1
        last[year] = rates, out
        return out
    return adjust


def compute_asset_monthly_rates(
    sim_assets: list[dict[str, Any]],
    eff: dict[str, Any],
    primary_sym: str = "",
    *,
    risk_class_annual_overrides: dict[str, float] | None = None,
) -> dict[str, float]:
    """Computes per-bucket monthly compounding rate (net of `expense_ratio_pct` for hypothetical products)."""
    rates: dict[str, float] = {}
    for a in sim_assets:
        fc = resolve_asset_forecast(a, eff, primary_sym)
        a["_is_forecast_override"] = bool(fc.get("is_override"))
        rc = fc["risk_class"]
        if risk_class_annual_overrides and rc in risk_class_annual_overrides:
            ann_dec = float(risk_class_annual_overrides[rc])
        else:
            ann_dec = fc["growth_pct"] / 100.0
        net_ann = ann_dec - (fc["expense_ratio_pct"] / 100.0)
        if 1.0 + net_ann <= 0.0:
            rates[a["id"]] = -0.9999
        else:
            rates[a["id"]] = math.pow(1.0 + net_ann, 1.0 / 12.0) - 1.0
    return rates


STRESS_NO_BOUNCE_MONTHS = 75

STRESS_PRESET_PROFILES: dict[str, dict[str, Any]] = {
    "gfc2008": {
        "down_months": 18,
        "broad_bounce_months": 36,
        "stock_bounce_months": 54,
        "shocks": {
            "single_stock": -0.60,
            "crypto_speculative": -0.72,
            "broad_equity": -0.50,
            "hybrid_buffered": -0.25,
            "fixed_income": 0.05,
            "real_diversifier": 0.05,
        },
    },
    "dotcom2000": {
        "down_months": 30,
        "broad_bounce_months": 48,
        "stock_bounce_months": STRESS_NO_BOUNCE_MONTHS,
        "shocks": {
            "single_stock": -0.75,
            "crypto_speculative": -0.80,
            "broad_equity": -0.45,
            "hybrid_buffered": -0.20,
            "fixed_income": 0.24,
            "real_diversifier": 0.04,
        },
    },
    "covid2020": {
        "down_months": 3,
        "broad_bounce_months": 6,
        "stock_bounce_months": 6,
        "shocks": {
            "single_stock": -0.34,
            "crypto_speculative": -0.48,
            "broad_equity": -0.34,
            "hybrid_buffered": -0.16,
            "fixed_income": 0.03,
            "real_diversifier": 0.08,
        },
    },
    "rates2022": {
        "down_months": 12,
        "broad_bounce_months": 15,
        "stock_bounce_months": 18,
        "shocks": {
            "single_stock": -0.38,
            "crypto_speculative": -0.64,
            "broad_equity": -0.24,
            "hybrid_buffered": -0.16,
            "fixed_income": -0.13,
            "real_diversifier": 0.01,
        },
    },
}


def resolve_stress_profile(eff: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve 3-phase Market Correction profile (`down_months`, `broad_bounce_months`, `stock_bounce_months`, `bear_shield_months`, `shocks`)."""
    scen = str(eff.get("simStressScenario") or "gfc2008").strip().lower()
    if scen == "none":
        return None
    if scen == "custom":
        raw_c = eff.get("simCustomShockPct")
        c = max(-90.0, min(0.0, float(raw_c if raw_c is not None else -30.0))) / 100.0
        raw_dm = eff.get("simCustomDownMonths")
        down_m = max(3, min(36, int(round(float(raw_dm if raw_dm is not None else 12)))))
        raw_bb = eff.get("simCustomBroadBounceMonths")
        broad_b = max(3, min(STRESS_NO_BOUNCE_MONTHS, int(round(float(raw_bb if raw_bb is not None else 24)))))
        raw_sb = eff.get("simCustomStockBounceMonths")
        stock_b = max(3, min(STRESS_NO_BOUNCE_MONTHS, int(round(float(raw_sb if raw_sb is not None else 36)))))
        shocks = {
            "single_stock": c,
            "crypto_speculative": max(-0.95, 1.35 * c),
            "broad_equity": 0.60 * c,
            "hybrid_buffered": 0.32 * c,
            "fixed_income": 0.10 * c,
            "real_diversifier": -0.05 * c,
        }
    else:
        preset = STRESS_PRESET_PROFILES.get(scen)
        if not preset:
            return None
        down_m = int(preset["down_months"])
        broad_b = int(preset["broad_bounce_months"])
        stock_b = int(preset["stock_bounce_months"])
        shocks = dict(preset["shocks"])

    has_drop = float(shocks.get("single_stock", 0.0)) < -1e-6 or float(shocks.get("broad_equity", 0.0)) < -1e-6
    bear_extra = (broad_b // 2) if (broad_b < STRESS_NO_BOUNCE_MONTHS and float(shocks.get("broad_equity", 0.0)) < -1e-6) else 0
    bear_shield_m = (down_m + bear_extra) if has_drop else 0
    return {
        "scenario": scen,
        "down_months": down_m,
        "broad_bounce_months": broad_b,
        "stock_bounce_months": stock_b,
        "bear_shield_months": bear_shield_m,
        "shocks": shocks,
    }


def stress_year1_shocks_by_risk_class(eff: dict[str, Any]) -> dict[str, float] | None:
    """Peak-to-trough crash shock (decimal cumulative return over `down_months`) for the 7 risk profiles (`cash_fixed` omitted so contractual rates stay unshocked)."""
    prof = resolve_stress_profile(eff)
    return dict(prof["shocks"]) if prof else None


def _build_stress_month_resolver(
    sim_assets: list[dict[str, Any]],
    eff: dict[str, Any],
    primary_sym: str,
    base_asset_rates: dict[str, float],
    adjust_year: Any,
    *,
    enabled: bool = True,
    start_month: int = 1,
) -> tuple[dict[str, Any] | None, Any]:
    """
    Precompute O(1) monthly rate maps across Downfall (`start_month .. start_month+D_down-1`) and
    Bounce-Back (`start_month+D_down .. start_month+D_down+D_rec-1`).
    PARITY INVARIANT: Must match `buildStressMonthResolver()` in `app_simulator.js` 1:1 (`start_month=1` when retired today).
    """
    prof = resolve_stress_profile(eff) if enabled else None
    if not prof:
        return None, None
    down_m = int(prof["down_months"])
    broad_b = int(prof["broad_bounce_months"])
    stock_b = int(prof["stock_bounce_months"])
    shocks = prof["shocks"]
    s_m = max(1, int(start_month or 1))

    asset_specs: list[tuple[str, float | None, int, float | None]] = []
    max_override_dur = down_m
    for a in sim_assets:
        aid = str(a["id"])
        fc = resolve_asset_forecast(a, eff, primary_sym)
        rc = str(fc["risk_class"])
        if rc not in shocks:
            asset_specs.append((aid, None, 0, None))
            continue
        s = float(shocks[rc])
        er_dec = max(0.0, min(0.99, float(fc["expense_ratio_pct"]) / 100.0))
        fee_mo = math.pow(1.0 - er_dec, 1.0 / 12.0)
        down_gross_mo = math.pow(max(1e-6, 1.0 + s), 1.0 / down_m) - 1.0
        down_net_mo = max(-0.9999, (1.0 + down_gross_mo) * fee_mo - 1.0)

        raw_b = stock_b if rc in ("single_stock", "crypto_speculative") else broad_b
        if s < -1e-6 and raw_b < STRESS_NO_BOUNCE_MONTHS:
            b_end = down_m + raw_b
            rec_gross_mo = math.pow(1.0 / max(1e-6, 1.0 + s), 1.0 / raw_b) - 1.0
            rec_net_mo: float | None = max(-0.9999, (1.0 + rec_gross_mo) * fee_mo - 1.0)
            if b_end > max_override_dur:
                max_override_dur = b_end
        else:
            b_end = down_m
            rec_net_mo = None
        asset_specs.append((aid, down_net_mo, b_end, rec_net_mo))

    max_end_m = min(SIM_TOTAL_MONTHS, s_m + max_override_dur - 1)
    monthly_maps: dict[int, dict[str, float]] = {}
    for m in range(s_m, max_end_m + 1):
        rel_m = m - s_m + 1
        norm_m = adjust_year(base_asset_rates, (m - 1) // 12)
        rm: dict[str, float] = {}
        for aid, down_net_mo, b_end, rec_net_mo in asset_specs:
            base_r = norm_m[aid]
            if down_net_mo is None:
                rm[aid] = base_r
            elif rel_m <= down_m:
                rm[aid] = down_net_mo
            elif rec_net_mo is not None and rel_m <= b_end:
                rm[aid] = max(base_r, rec_net_mo)
            else:
                rm[aid] = base_r
        monthly_maps[m] = rm

    def get_rates_for_month(m: int) -> dict[str, float]:
        if m in monthly_maps:
            return monthly_maps[m]
        return adjust_year(base_asset_rates, max(0, (m - 1) // 12))

    return prof, get_rates_for_month


def compute_weighted_holding_growth_pct(
    sim_assets: list[dict[str, Any]],
    eff: dict[str, Any],
    primary_sym: str = "",
) -> float:
    """Value-weighted projected annual growth across currently funded buckets."""
    inc_unv = bool(eff.get("simIncludeUnvested", True))
    num = 0.0
    den = 0.0
    for a in sim_assets:
        if a.get("is_unvested_goog") and not inc_unv:
            continue
        v = float(a.get("current_value") or 0.0)
        if v <= 0:
            continue
        fc = resolve_asset_forecast(a, eff, primary_sym)
        num += v * (fc["growth_pct"] - fc["expense_ratio_pct"])
        den += v
    return round(num / den, 2) if den > 0 else 0.0


# ==============================================================================
# PROGRESSIVE TAX ENGINE & HOUSEHOLD FILING INVARIANTS (DA-C03 / Topic A)
# ==============================================================================
# DESIGN INTENT & ANTI-DRIFT RULES:
#   1. CPI-Indexed vs. Statutorily Unindexed Thresholds:
#      - Standard deduction (`federal_tax_2026.STD_DEDUCTION`), ordinary brackets (`_FED_ORD_TOPS_*`),
#        and LTCG 0%/15%/20% cutoffs (`_FED_LTCG_TOPS_*`) ARE indexed by `deflator` (`dfl`).
#      - DO NOT index the 3.8% NIIT MAGI threshold ($250k MFJ / $200k Single, IRC §1411),
#        the Social Security provisional-income thresholds ($32k/$44k MFJ, $25k/$34k Single,
#        IRC §86), or the $3,000/yr ordinary capital-loss offset (IRC §1211) — Congress left
#        all three unindexed in nominal dollars.
#   2. Married Filing Jointly (`MFJ`) Eligibility & Default (User Decision — Topic A):
#      - `MFJ` is eligible ONLY when `total_family > 1` (1-person portfolios force `Single`).
#      - When `total_family > 1` and unset, `is_mfj` defaults to `True` iff `adult_count >= 2`
#        (members aged >= 18), and `False` otherwise (e.g. single parent + minor child).
#   3. Minimum Tax Bracket Floor (`min_tax_bracket_floor` / `simMinTaxBracketFloor` — Topic A):
#      - Models uningested outside retirement income (pensions, rentals, consulting) by seeding
#        each calendar year's starting `ytd["agi_ord"] = (std_ded + floor_start) * dfl` in
#        `_ensure_tax_ytd` WITHOUT charging tax on that synthetic outside-income floor itself.
#        Portfolio ordinary income and stacked LTCG then begin at the chosen marginal bracket.
#   4. Standalone Optimizer Fallback (`payload=None`):
#      - Callers like `optimize_sim_asset_order(..., payload=None)` omit `payload.project`;
#        `build_tax_schedule_cfg` MUST infer `state_rate` and `net_retention_rate` from
#        `retirement_sim_assets` + `ssn_model` so Python & JS (`app_simulator.js`) stay 1:1.
# ==============================================================================

def build_tax_schedule_cfg(
    payload: dict[str, Any] | None = None,
    eff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolves household filing status (`MFJ` vs `Single`) and minimum tax bracket floor (`DA-C03`).
    MFJ is eligible only when >1 family members exist; defaults to True when >=2 adult family members exist.
    """
    proj = (payload or {}).get("project") if isinstance((payload or {}).get("project"), dict) else {}
    tr = proj.get("tax_rates") if isinstance(proj.get("tax_rates"), dict) else {}
    eff = eff or {}
    today_iso = str((payload or {}).get("today_iso") or "2026-11-11")
    cur_yr = int(today_iso[:4]) if len(today_iso) >= 4 and today_iso[:4].isdigit() else 2026

    fam_members = proj.get("family_members") if isinstance(proj.get("family_members"), list) else []
    invs = proj.get("investors") if isinstance(proj.get("investors"), list) else []
    bens = proj.get("beneficiaries") if isinstance(proj.get("beneficiaries"), list) else []

    if fam_members:
        total_family = len(fam_members)
        adult_count = 0
        for m in fam_members:
            b_str = str((m or {}).get("birth_date") or "")[:4]
            by = int(b_str) if b_str.isdigit() else (cur_yr - 30)
            if (cur_yr - by) >= 18:
                adult_count += 1
    else:
        total_family = len(invs) + len(bens)
        adult_count = 0
        for p in [*invs, *bens]:
            b_str = str((p or {}).get("birth_date") or "")[:4]
            by = int(b_str) if b_str.isdigit() else (cur_yr - 30)
            if (cur_yr - by) >= 18:
                adult_count += 1
        if total_family == 0:
            ssn_invs = (((payload or {}).get("ssn_model") or {}).get("investors") or [])
            total_family = len(ssn_invs) or 1
            adult_count = len(ssn_invs) or 1

    mfj_eligible = total_family > 1
    if not mfj_eligible:
        is_mfj = False
    elif eff.get("simMarriedFilingJointly") is not None:
        is_mfj = bool(eff["simMarriedFilingJointly"])
    elif tr.get("married_filing_jointly") is not None:
        is_mfj = bool(tr["married_filing_jointly"])
    else:
        is_mfj = adult_count >= 2

    raw_floor = eff.get("simMinTaxBracketFloor")
    if raw_floor is None or str(raw_floor).strip() == "":
        raw_floor = tr.get("min_tax_bracket_floor", "none")
    floor_key = str(raw_floor).strip().lower().replace("%", "")
    if floor_key not in federal_tax_2026.FLOORS_JOINT:
        floor_key = "none"

    ssn_m = ((payload or {}).get("ssn_model") or {}) if isinstance((payload or {}).get("ssn_model"), dict) else {}
    sim_list = (payload or {}).get("retirement_sim_assets") or []

    fed_ltcg_top = float(tr["fed_ltcg_top"]) if tr.get("fed_ltcg_top") is not None else 0.20
    niit_rate = float(tr["niit_rate"]) if tr.get("niit_rate") is not None else 0.038
    fed_ord_top = (
        float(tr["fed_ordinary_top"])
        if tr.get("fed_ordinary_top") is not None
        else float(ssn_m.get("fed_top_rate") if ssn_m.get("fed_top_rate") is not None else 0.37)
    )
    if tr.get("state_rate") is not None:
        state_rate = float(tr["state_rate"])
    elif ssn_m.get("state_rate") is not None:
        state_rate = float(ssn_m["state_rate"])
    elif tr.get("ltcg_top") is not None:
        state_rate = max(0.0, round(float(tr["ltcg_top"]) - fed_ltcg_top - niit_rate, 4))
    else:
        ltcg_bucket = next(
            (a for a in sim_list if isinstance(a, dict) and a.get("tax_model") in ("LTCG_POOL", "LTCG_TRANCHES") and a.get("tax_rate") is not None),
            None,
        )
        ord_bucket = next(
            (a for a in sim_list if isinstance(a, dict) and a.get("tax_model") == "ORDINARY_FULL" and a.get("tax_rate") is not None),
            None,
        )
        if ltcg_bucket is not None:
            state_rate = max(0.0, round(float(ltcg_bucket["tax_rate"]) - fed_ltcg_top - niit_rate, 4))
        elif ord_bucket is not None:
            state_rate = max(0.0, round(float(ord_bucket["tax_rate"]) - fed_ord_top, 4))
        else:
            state_rate = 0.0
    state_ssn_rate = float(
        tr["state_ssn_rate"]
        if tr.get("state_ssn_rate") is not None
        else (ssn_m.get("state_ssn_rate") or 0.0)
    )
    ssn_fed_cap = float(
        tr["ssn_fed_taxable_portion"]
        if tr.get("ssn_fed_taxable_portion") is not None
        else (ssn_m.get("fed_taxable_portion") or 0.85)
    )
    if (payload or {}).get("net_retention_rate") is not None:
        net_ret_rate = float((payload or {})["net_retention_rate"])
    else:
        unv_b = next((a for a in sim_list if isinstance(a, dict) and a.get("is_unvested_goog") and a.get("tranches")), None)
        if unv_b:
            tot_n = sum(float(t.get("shares") or 0.0) for t in unv_b["tranches"])
            tot_g = sum(float(t.get("gross_shares") or 0.0) for t in unv_b["tranches"])
            net_ret_rate = (tot_n / tot_g) if tot_g > 0 else 0.50
        else:
            net_ret_rate = 0.50

    def_by, def_bm, def_bd = _family_defaults()[1], 1, 1
    ssn_invs = (((payload or {}).get("ssn_model") or {}).get("investors") or invs or [])
    rmd_candidates = list(fam_members) if fam_members else [*invs, *bens, *ssn_invs]
    for cand in rmd_candidates:
        if isinstance(cand, dict) and (cand.get("is_rmd_owner") or cand.get("rmd_owner")):
            b_raw = str(cand.get("birth_date") or cand.get("birth_iso") or "")
            def_by = int(cand.get("birth_year") or (b_raw[:4] if len(b_raw) >= 4 and b_raw[:4].isdigit() else 0) or 1975)
            b_pts = b_raw.split("-")
            def_bm = int(cand.get("birth_month") or (b_pts[1] if len(b_pts) >= 2 and b_pts[1].isdigit() else 1) or 1)
            def_bd = int(cand.get("birth_day") or 1)
            break
    else:
        first_cand = rmd_candidates[0] if (rmd_candidates and isinstance(rmd_candidates[0], dict)) else None
        if first_cand:
            b_raw = str(first_cand.get("birth_date") or first_cand.get("birth_iso") or "")
            def_by = int(first_cand.get("birth_year") or (b_raw[:4] if len(b_raw) >= 4 and b_raw[:4].isdigit() else 0) or 1975)
            b_pts = b_raw.split("-")
            def_bm = int(first_cand.get("birth_month") or (b_pts[1] if len(b_pts) >= 2 and b_pts[1].isdigit() else 1) or 1)
            def_bd = int(first_cand.get("birth_day") or 1)

    return {
        "progressive": True,
        "is_mfj": is_mfj,
        "mfj_eligible": mfj_eligible,
        "total_family": total_family,
        "adult_count": adult_count,
        "floor_key": floor_key,
        "fed_ltcg_top": fed_ltcg_top,
        "fed_ord_top": fed_ord_top,
        "niit_rate": niit_rate,
        "state_rate": state_rate,
        "state_ssn_rate": state_ssn_rate,
        "ssn_fed_cap": ssn_fed_cap,
        "net_retention_rate": max(0.25, min(0.95, net_ret_rate)),
        "default_birth_year": def_by,
        "default_birth_month": def_bm,
        "default_birth_day": def_bd,
        "work_plan": build_work_plan_cfg(payload, eff),
    }


IRS_CATCHUP_50_USD = 8000.0
IRS_SUPER_CATCHUP_60_63_USD = 11250.0


def build_work_plan_cfg(
    payload: dict[str, Any] | None,
    eff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve active pre-retirement work plans uniformly across ALL household family members.

    PARITY & SECURITY INVARIANT (lockstep with `buildWorkPlanConfig` in `app_simulator.js`):
    - Any family member (regardless of `is_ssn_contributor` or list position) may configure `work_plan`.
    - Positional scenario map `simMemberRetirementAges["member_<idx>_retirement_age"]` (plus legacy
      `simInvestor1RetirementAge` / `simInvestor2RetirementAge`) overrides `work_plan.default_retirement_age`
      (clamped to `[18, milestone_age]`).
    - Stop-working / retirement occurs at `ret_ym = YYYY-MM` (`birth_year + eff_ret_age`, `birth_month`);
      a member is actively working in simulation month `ym` iff `ym < ret_ym` (zero `birth_day` dependency).
    """
    from config import sanitize_work_plan

    eff = eff or {}
    today_iso = str((payload or {}).get("today_iso") or date.today().isoformat())
    start_ym = today_iso[:7]
    proj = (payload or {}).get("project") if isinstance((payload or {}).get("project"), dict) else {}
    proj_fams = proj.get("family_members") if isinstance(proj.get("family_members"), list) else []
    proj_invs = proj.get("investors") if isinstance(proj.get("investors"), list) else []
    proj_bens = proj.get("beneficiaries") if isinstance(proj.get("beneficiaries"), list) else []
    ssn_m = (payload or {}).get("ssn_model") if isinstance((payload or {}).get("ssn_model"), dict) else {}
    ssn_invs = ssn_m.get("investors") if isinstance(ssn_m.get("investors"), list) else []

    raw_members: list[dict[str, Any]] = []
    if proj_fams:
        for idx, m in enumerate(proj_fams):
            if isinstance(m, dict):
                raw_members.append({
                    **m,
                    "is_ssn_contributor": bool(m.get("is_ssn_contributor", idx < len(proj_invs))),
                })
    elif proj_invs or proj_bens:
        for inv in proj_invs:
            if isinstance(inv, dict):
                raw_members.append({**inv, "is_ssn_contributor": True})
        for ben in proj_bens:
            if isinstance(ben, dict):
                raw_members.append({**ben, "is_ssn_contributor": False})
    else:
        for s_inv in ssn_invs:
            if isinstance(s_inv, dict):
                raw_members.append({**s_inv, "is_ssn_contributor": bool(s_inv.get("is_ssn_contributor", True))})

    any_ssn = any(bool(m.get("is_ssn_contributor")) for m in raw_members)
    mem_ovr_map = eff.get("simMemberRetirementAges") if isinstance(eff.get("simMemberRetirementAges"), dict) else {}

    workers: list[dict[str, Any]] = []
    all_investors: list[dict[str, Any]] = []
    ssn_rank = 0

    for idx, p_mem in enumerate(raw_members):
        is_ssn = bool(p_mem.get("is_ssn_contributor"))
        s_inv: dict[str, Any] = {}
        if is_ssn and ssn_rank < len(ssn_invs) and isinstance(ssn_invs[ssn_rank], dict):
            s_inv = ssn_invs[ssn_rank]
        elif not any_ssn and idx == 0 and ssn_invs and isinstance(ssn_invs[0], dict):
            s_inv = ssn_invs[0]

        b_str = str(p_mem.get("birth_date") or p_mem.get("birth_iso") or s_inv.get("birth_iso") or "")
        b_yr = int(p_mem.get("birth_year") or 0)
        b_mo = int(p_mem.get("birth_month") or 0)
        if (b_yr <= 0 or b_mo <= 0) and b_str:
            try:
                parts = [int(x) for x in b_str.split("-")[:2]]
                if b_yr <= 0 and len(parts) >= 1:
                    b_yr = parts[0]
                if b_mo <= 0 and len(parts) >= 2:
                    b_mo = parts[1]
            except Exception:
                pass
        if b_yr <= 0:
            b_yr = int(s_inv.get("birth_year") or 1975)
        if b_mo <= 0:
            b_mo = int(s_inv.get("birth_month") or 1)
        b_mo = max(1, min(12, b_mo))

        try:
            ms_age = max(18, min(100, int(round(float(p_mem.get("milestone_age") or s_inv.get("milestone_age") or 85)))))
        except (TypeError, ValueError):
            ms_age = 85

        raw_wp = p_mem.get("work_plan") if isinstance(p_mem.get("work_plan"), dict) else s_inv.get("work_plan")
        wp = sanitize_work_plan(raw_wp, milestone_age=ms_age, clamp_to_milestone=True)

        mem_key = f"member_{idx}_retirement_age"
        scen_ovr = mem_ovr_map.get(mem_key)
        if scen_ovr is None:
            scen_ovr = mem_ovr_map.get(str(idx))
        if scen_ovr is None:
            if is_ssn and ssn_rank < 2:
                legacy_key = "simInvestor1RetirementAge" if ssn_rank == 0 else "simInvestor2RetirementAge"
                scen_ovr = eff.get(legacy_key)
            elif not any_ssn and idx < 2:
                legacy_key = "simInvestor1RetirementAge" if idx == 0 else "simInvestor2RetirementAge"
                scen_ovr = eff.get(legacy_key)

        if is_ssn:
            ssn_rank += 1

        if scen_ovr is not None and not isinstance(scen_ovr, bool) and scen_ovr != "":
            try:
                eff_ret_age = max(18, min(ms_age, int(round(float(scen_ovr)))))
                is_active = True
            except (TypeError, ValueError):
                eff_ret_age = int(wp["default_retirement_age"])
                is_active = bool(wp["is_working"])
        else:
            eff_ret_age = int(wp["default_retirement_age"])
            is_active = bool(wp["is_working"])

        ret_ym = f"{b_yr + eff_ret_age:04d}-{b_mo:02d}"
        name = str(p_mem.get("name") or s_inv.get("name") or f"Member {idx + 1}")
        entry = {
            "idx": idx,
            "member_key": mem_key,
            "is_ssn_contributor": is_ssn,
            "name": name,
            "birth_year": b_yr,
            "birth_month": b_mo,
            "milestone_age": ms_age,
            "retirement_age": eff_ret_age,
            "default_retirement_age": int(wp["default_retirement_age"]),
            "ret_ym": ret_ym,
            "is_working": is_active and (ret_ym > start_ym),
            "annual_take_home": float(wp.get("annual_take_home") or 0.0),
            "covers_full_burn": bool(wp["covers_full_burn"]),
            "annual_budget_offset": float(wp["annual_budget_offset"]),
            "contributions": list(wp["contributions"]),
        }
        all_investors.append(entry)
        if entry["is_working"]:
            workers.append(entry)

    return {
        "start_ym": start_ym,
        "workers": workers,
        "investors": all_investors,
    }


def compute_drawdown_start_month(
    wp_cfg: dict[str, Any] | None,
    today_iso: str,
    ann_burn: float,
    inf_monthly_rate: float,
) -> int:
    """
    First simulation month `m >= 1` where living-expense drawdown after active paycheck offsets is `> $0.01`.
    Defaults to `1` when everyone is retired today (`workers == []`), and to the earliest retirement month
    when `ann_burn <= 0.01` while someone is actively working.
    """
    workers = (wp_cfg or {}).get("workers") or []
    if not workers:
        return 1
    start_year = int(today_iso[:4])
    start_month0 = int(today_iso[5:7]) - 1
    if ann_burn <= 0.01:
        for m in range(1, SIM_TOTAL_MONTHS + 1):
            tot_m = start_year * 12 + start_month0 + m
            ym = f"{tot_m // 12:04d}-{(tot_m % 12) + 1:02d}"
            active_w = [w for w in workers if ym < w["ret_ym"]]
            if not active_w or not any(w["covers_full_burn"] for w in active_w):
                return m
        return SIM_TOTAL_MONTHS + 1

    for m in range(1, SIM_TOTAL_MONTHS + 1):
        tot_m = start_year * 12 + start_month0 + m
        ym = f"{tot_m // 12:04d}-{(tot_m % 12) + 1:02d}"
        active_w = [w for w in workers if ym < w["ret_ym"]]
        if not active_w:
            return m
        if any(w["covers_full_burn"] for w in active_w):
            continue
        dfl = math.pow(1.0 + inf_monthly_rate, m)
        raw_mo_burn = (ann_burn * dfl) / 12.0
        off_mo = sum((w["annual_budget_offset"] * dfl) / 12.0 for w in active_w)
        if raw_mo_burn - off_mo > 0.01:
            return m
    return SIM_TOTAL_MONTHS + 1


def _deposit_work_plan_leg(
    state_map: dict[str, Any],
    target_id: str,
    iso: str,
    nom_amt: float,
    include_unvested: bool,
    *,
    is_employer_match: bool = False,
) -> float:
    """
    Deposit a monthly pre-retirement contribution leg into `target_id` honoring wrapper basis rules:
    - `ORDINARY_FULL` (Pre-Tax 401k / Traditional IRA): `+nom_amt` to `val`, `+$0` to `costBasis`.
    - `TAX_FREE` (Roth 401k / Roth IRA): employee leg adds `+nom_amt` to both `val` & `costBasis`;
      employer match leg adds `+nom_amt` to `val` only (`+$0` `costBasis`).
    - Taxable (`LTCG_TRANCHES`, `LTCG_POOL`, `TAXABLE_YIELD`, `MUNI`, `TREASURY`, `CASH`, unlocked `ANNUITY_LIFO`):
      adds `+nom_amt` to both `val` & `costBasis` (appending a dated lot on `LTCG_TRANCHES`).
    Skips missing/stale bucket IDs, unvested equity grants, and surrender-locked annuity contracts.
    """
    if nom_amt <= 0.0 or not target_id:
        return 0.0
    st = state_map.get(target_id)
    if not st or st.get("is_unvested_goog") or _is_bucket_locked(st, iso):
        return 0.0
    tm = str(st.get("tax_model") or "")
    if tm == "ORDINARY_FULL":
        st["val"] += nom_amt
    elif tm == "TAX_FREE":
        st["val"] += nom_amt
        if not is_employer_match:
            st["costBasis"] += nom_amt
    elif st.get("tranches") is not None and tm == "LTCG_TRANCHES":
        st["tranches"].append({
            "vestIso": iso,
            "shares": 0.0,
            "vested": True,
            "val": nom_amt,
            "costBasis": nom_amt,
        })
        st["val"] = sum(t["val"] for t in st["tranches"])
        st["costBasis"] = sum(t["costBasis"] for t in st["tranches"])
    else:
        st["val"] += nom_amt
        st["costBasis"] += nom_amt
    return nom_amt


def _ensure_tax_ytd(state_map: dict[str, Any], iso: str, dfl: float) -> dict[str, Any] | None:
    tc = state_map.get("_taxCfg")
    if not tc or not tc.get("progressive"):
        return None
    yr = int(iso[:4])
    ytd = state_map.get("_taxYtd")
    if not ytd or ytd.get("year") != yr:
        is_mfj = bool(tc["is_mfj"])
        std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
        fl_map = federal_tax_2026.FLOORS_JOINT if is_mfj else federal_tax_2026.FLOORS_SINGLE
        fl_start = fl_map.get(str(tc.get("floor_key") or "none"), -1.0)
        init_agi = 0.0 if fl_start < 0 else (std_ded + fl_start * dfl)
        ytd = {
            "year": yr,
            "dfl": dfl,
            "agi_ord": init_agi,
            "init_floor_ord": init_agi,
            "rmd_ord": 0.0,
            "vest_ord": 0.0,
            "yield_ord": 0.0,
            "ssn_taxable": 0.0,
            "ltcg": 0.0,
            "muni_int": 0.0,
            "ord_loss_offset_used": 0.0,
        }
        state_map["_taxYtd"] = ytd
    else:
        ytd["dfl"] = dfl
    return ytd


def _owner_is_pre_59_half(st: dict[str, Any], state_map: dict[str, Any], iso: str) -> bool:
    tc = state_map.get("_taxCfg")
    if not tc:
        return False
    oby = int(st.get("owner_birth_year") or tc.get("default_birth_year") or 1975)
    obm = int(st.get("owner_birth_month") or tc.get("default_birth_month") or 1)
    obd = int(st.get("owner_birth_day") or tc.get("default_birth_day") or 1)
    yr = int(iso[:4])
    mo = int(iso[5:7])
    dy = int(iso[8:10])
    age = (yr - oby) + ((mo - obm) + (dy - obd) / 30.4375) / 12.0
    return age < (59.5 - 1e-6)


def _is_short_term_lot(lot: dict[str, Any], iso: str) -> bool:
    viso = str(lot.get("vestIso") or "")
    if len(viso) < 10 or len(iso) < 10:
        return False
    try:
        d0 = date(int(viso[:4]), int(viso[5:7]), int(viso[8:10]))
        d1 = date(int(iso[:4]), int(iso[5:7]), int(iso[8:10]))
    except ValueError:
        return False
    anniversary_day = min(d0.day, calendar.monthrange(d0.year + 1, d0.month)[1])
    return d0 <= d1 <= date(d0.year + 1, d0.month, anniversary_day)


def _ord_marginal_rate_at(
    agi_ord_pos: float,
    magi_pos: float,
    tc: dict[str, Any],
    dfl: float,
    apply_niit: bool,
    eff_state_rate: float,
    extra_penalty: float,
) -> float:
    is_mfj = bool(tc["is_mfj"])
    std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
    rate = extra_penalty
    if agi_ord_pos >= std_ded - 1e-9:
        tax_ord = max(0.0, agi_ord_pos - std_ded)
        tops = federal_tax_2026.ORDINARY_JOINT if is_mfj else federal_tax_2026.ORDINARY_SINGLE
        fed_r = float(tc["fed_ord_top"])
        for idx_b, top_b in enumerate(tops):
            if tax_ord < top_b * dfl - 1e-9:
                fed_r = federal_tax_2026.LOW_RATES[idx_b]
                break
        rate += fed_r + eff_state_rate
    if apply_niit:
        niit_t = 250000.0 if is_mfj else 200000.0
        if magi_pos >= niit_t - 1e-9:
            rate += float(tc["niit_rate"])
    return min(0.95, max(0.0, rate))


def _ord_dx_breakpoints(
    cur_ord: float,
    cur_magi: float,
    max_dx: float,
    loss_cap: float,
    tc: dict[str, Any],
    dfl: float,
    apply_niit: bool,
) -> list[float]:
    is_mfj = bool(tc["is_mfj"])
    std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
    tops = federal_tax_2026.ORDINARY_JOINT if is_mfj else federal_tax_2026.ORDINARY_SINGLE
    pts = [0.0, max_dx]
    if 0.0 < loss_cap < max_dx:
        pts.append(loss_cap)
    for raw_t in (std_ded, *[std_ded + t * dfl for t in tops]):
        dx = loss_cap + (raw_t - cur_ord)
        if 0.0 < dx < max_dx:
            pts.append(dx)
    if apply_niit:
        niit_t = 250000.0 if is_mfj else 200000.0
        dx_n = loss_cap + (niit_t - cur_magi)
        if 0.0 < dx_n < max_dx:
            pts.append(dx_n)
    return sorted(set(pts))


def _tax_ordinary_increment(
    state_map: dict[str, Any],
    gross_amount: float,
    *,
    iso: str,
    dfl: float,
    apply_niit: bool,
    state_rate_override: float | None = None,
    extra_penalty: float = 0.0,
    allow_loss_offset: bool = True,
    commit: bool = True,
) -> dict[str, float]:
    if gross_amount <= 1e-9:
        return {"tax": 0.0, "lossSheltered": 0.0, "taxSaved": 0.0}
    tc = state_map.get("_taxCfg")
    ytd = _ensure_tax_ytd(state_map, iso, dfl)
    if not tc or not ytd:
        return {"tax": 0.0, "lossSheltered": 0.0, "taxSaved": 0.0}
    eff_state = float(tc["state_rate"]) if state_rate_override is None else float(state_rate_override)
    avail_loss = float(state_map.get("_lossCarryforward") or 0.0)
    rem_ord_offset = max(0.0, 3000.0 - float(ytd.get("ord_loss_offset_used") or 0.0)) if allow_loss_offset else 0.0
    loss_cap = min(gross_amount, avail_loss, rem_ord_offset)

    cur_ord = float(ytd["agi_ord"])
    cur_magi = cur_ord + float(ytd["ltcg"])
    pts = _ord_dx_breakpoints(cur_ord, cur_magi, gross_amount, loss_cap, tc, dfl, apply_niit)
    tax = 0.0
    for i in range(len(pts) - 1):
        a0, a1 = pts[i], pts[i + 1]
        seg = a1 - a0
        if seg <= 1e-12:
            continue
        mid = 0.5 * (a0 + a1)
        if mid < loss_cap:
            tax += seg * extra_penalty
        else:
            taxable_dx = mid - loss_cap
            mr = _ord_marginal_rate_at(cur_ord + taxable_dx, cur_magi + taxable_dx, tc, dfl, apply_niit, eff_state, extra_penalty)
            tax += seg * mr

    tax_saved = 0.0
    if loss_cap > 0.005:
        mr_saved = _ord_marginal_rate_at(cur_ord + 0.5 * loss_cap, cur_magi + 0.5 * loss_cap, tc, dfl, apply_niit, eff_state, 0.0)
        tax_saved = loss_cap * mr_saved
        if commit:
            state_map["_lossCarryforward"] = max(0.0, avail_loss - loss_cap)
            state_map["_totalLossUsed"] = float(state_map.get("_totalLossUsed") or 0.0) + loss_cap
            state_map["_totalTaxSavedByLoss"] = float(state_map.get("_totalTaxSavedByLoss") or 0.0) + tax_saved
            ytd["ord_loss_offset_used"] = float(ytd.get("ord_loss_offset_used") or 0.0) + loss_cap

    if commit:
        ytd["agi_ord"] = cur_ord + max(0.0, gross_amount - loss_cap)
    return {"tax": tax, "lossSheltered": loss_cap, "taxSaved": tax_saved}


def _gross_for_net_stcg(
    state_map: dict[str, Any],
    lot_val: float,
    gain_ratio: float,
    target_net: float,
    *,
    iso: str,
    dfl: float,
    apply_state: bool = True,
) -> float:
    """
    Exact piecewise net-to-gross solver for `<365d` Short-Term Capital Gains (`DA2-06`).
    PARITY INVARIANT: Must match `grossForNetStcg` in `app_simulator.js` 1:1.
    Integrates across federal standard deduction, ordinary `10%..37%` brackets, state tax, and
    the `$200k/$250k` NIIT threshold scaled by `gain_ratio` so `gross - tax == target_net` to `< $0.01`.
    """
    if lot_val <= 1e-9 or target_net <= 1e-9 or gain_ratio <= 1e-9:
        return min(lot_val, max(0.0, target_net))
    tc = state_map.get("_taxCfg")
    ytd = _ensure_tax_ytd(state_map, iso, dfl)
    if not tc or not ytd:
        return min(lot_val, target_net)
    eff_state = float(tc["state_rate"]) if apply_state else 0.0
    cur_ord = float(ytd["agi_ord"])
    cur_magi = cur_ord + float(ytd["ltcg"])
    max_u = lot_val * gain_ratio
    pts = _ord_dx_breakpoints(cur_ord, cur_magi, max_u, 0.0, tc, dfl, True)
    rem_net = target_net
    gross_acc = 0.0
    for i in range(len(pts) - 1):
        u0, u1 = pts[i], pts[i + 1]
        seg_u = u1 - u0
        if seg_u <= 1e-12:
            continue
        seg_gross = seg_u / gain_ratio
        mid = 0.5 * (u0 + u1)
        mr_gain = _ord_marginal_rate_at(cur_ord + mid, cur_magi + mid, tc, dfl, True, eff_state, 0.0)
        nf = max(0.01, 1.0 - mr_gain * gain_ratio)
        seg_net = seg_gross * nf
        if rem_net <= seg_net + 1e-9:
            gross_acc += min(seg_gross, rem_net / nf)
            return min(lot_val, gross_acc)
        gross_acc += seg_gross
        rem_net -= seg_net
    return lot_val


def _gross_for_net_ordinary(
    state_map: dict[str, Any],
    max_gross: float,
    target_net: float,
    *,
    iso: str,
    dfl: float,
    apply_niit: bool,
    state_rate_override: float | None = None,
    extra_penalty: float = 0.0,
    allow_loss_offset: bool = True,
) -> float:
    if max_gross <= 1e-9 or target_net <= 1e-9:
        return 0.0
    tc = state_map.get("_taxCfg")
    ytd = _ensure_tax_ytd(state_map, iso, dfl)
    if not tc or not ytd:
        return min(max_gross, target_net)
    eff_state = float(tc["state_rate"]) if state_rate_override is None else float(state_rate_override)
    avail_loss = float(state_map.get("_lossCarryforward") or 0.0)
    rem_ord_offset = max(0.0, 3000.0 - float(ytd.get("ord_loss_offset_used") or 0.0)) if allow_loss_offset else 0.0
    loss_cap = min(max_gross, avail_loss, rem_ord_offset)

    cur_ord = float(ytd["agi_ord"])
    cur_magi = cur_ord + float(ytd["ltcg"])
    pts = _ord_dx_breakpoints(cur_ord, cur_magi, max_gross, loss_cap, tc, dfl, apply_niit)
    rem_net = target_net
    gross_acc = 0.0
    for i in range(len(pts) - 1):
        a0, a1 = pts[i], pts[i + 1]
        seg = a1 - a0
        if seg <= 1e-12:
            continue
        mid = 0.5 * (a0 + a1)
        if mid < loss_cap:
            mr = extra_penalty
        else:
            taxable_dx = mid - loss_cap
            mr = _ord_marginal_rate_at(cur_ord + taxable_dx, cur_magi + taxable_dx, tc, dfl, apply_niit, eff_state, extra_penalty)
        nf = max(0.01, 1.0 - mr)
        seg_net = seg * nf
        if rem_net <= seg_net + 1e-9:
            gross_acc += min(seg, rem_net / nf)
            return min(max_gross, gross_acc)
        gross_acc += seg
        rem_net -= seg_net
    return max_gross


def _ltcg_marginal_rate_at(
    stack_pos: float,
    magi_pos: float,
    tc: dict[str, Any],
    dfl: float,
    eff_state_rate: float,
) -> float:
    is_mfj = bool(tc["is_mfj"])
    std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
    tops = federal_tax_2026.GAINS_JOINT if is_mfj else federal_tax_2026.GAINS_SINGLE
    if stack_pos < tops[0] * dfl - 1e-9:
        fed_r = 0.0
    elif stack_pos < tops[1] * dfl - 1e-9:
        fed_r = 0.15
    else:
        fed_r = float(tc["fed_ltcg_top"])
    niit_t = 250000.0 if is_mfj else 200000.0
    niit_r = float(tc["niit_rate"]) if magi_pos >= niit_t - 1e-9 else 0.0
    st_r = eff_state_rate if magi_pos >= std_ded - 1e-9 else 0.0
    return min(0.95, max(0.0, fed_r + niit_r + st_r))


def _ltcg_u_breakpoints(
    stack_base: float,
    cur_magi: float,
    max_u: float,
    tc: dict[str, Any],
    dfl: float,
) -> list[float]:
    is_mfj = bool(tc["is_mfj"])
    std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
    tops = federal_tax_2026.GAINS_JOINT if is_mfj else federal_tax_2026.GAINS_SINGLE
    niit_t = 250000.0 if is_mfj else 200000.0
    pts = [0.0, max_u]
    for t in tops:
        du = t * dfl - stack_base
        if 0.0 < du < max_u:
            pts.append(du)
    for m_t in (std_ded, niit_t):
        du_m = m_t - cur_magi
        if 0.0 < du_m < max_u:
            pts.append(du_m)
    return sorted(set(pts))


def _tax_ltcg_increment(
    state_map: dict[str, Any],
    taxable_gain: float,
    *,
    iso: str,
    dfl: float,
    apply_state: bool = True,
    commit: bool = True,
) -> float:
    if taxable_gain <= 1e-9:
        return 0.0
    tc = state_map.get("_taxCfg")
    ytd = _ensure_tax_ytd(state_map, iso, dfl)
    if not tc or not ytd:
        return 0.0
    is_mfj = bool(tc["is_mfj"])
    std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
    eff_state = float(tc["state_rate"]) if apply_state else 0.0
    cur_ord_taxable = max(0.0, float(ytd["agi_ord"]) - std_ded)
    stack_base = cur_ord_taxable + float(ytd["ltcg"])
    cur_magi = float(ytd["agi_ord"]) + float(ytd["ltcg"])
    pts = _ltcg_u_breakpoints(stack_base, cur_magi, taxable_gain, tc, dfl)
    tax = 0.0
    for i in range(len(pts) - 1):
        u0, u1 = pts[i], pts[i + 1]
        seg = u1 - u0
        if seg <= 1e-12:
            continue
        mid = 0.5 * (u0 + u1)
        mr = _ltcg_marginal_rate_at(stack_base + mid, cur_magi + mid, tc, dfl, eff_state)
        tax += seg * mr
    if commit:
        ytd["ltcg"] = float(ytd["ltcg"]) + taxable_gain
    return tax


def _gross_for_net_ltcg(
    state_map: dict[str, Any],
    lot_val: float,
    gain_ratio: float,
    target_net: float,
    *,
    iso: str,
    dfl: float,
    apply_state: bool = True,
) -> float:
    if lot_val <= 1e-9 or target_net <= 1e-9 or gain_ratio <= 1e-9:
        return min(lot_val, max(0.0, target_net))
    tc = state_map.get("_taxCfg")
    ytd = _ensure_tax_ytd(state_map, iso, dfl)
    if not tc or not ytd:
        return min(lot_val, target_net)
    is_mfj = bool(tc["is_mfj"])
    std_ded = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
    eff_state = float(tc["state_rate"]) if apply_state else 0.0
    cur_ord_taxable = max(0.0, float(ytd["agi_ord"]) - std_ded)
    stack_base = cur_ord_taxable + float(ytd["ltcg"])
    cur_magi = float(ytd["agi_ord"]) + float(ytd["ltcg"])
    max_u = lot_val * gain_ratio
    pts = _ltcg_u_breakpoints(stack_base, cur_magi, max_u, tc, dfl)
    rem_net = target_net
    gross_acc = 0.0
    for i in range(len(pts) - 1):
        u0, u1 = pts[i], pts[i + 1]
        seg_u = u1 - u0
        if seg_u <= 1e-12:
            continue
        seg_gross = seg_u / gain_ratio
        mid = 0.5 * (u0 + u1)
        mr_gain = _ltcg_marginal_rate_at(stack_base + mid, cur_magi + mid, tc, dfl, eff_state)
        nf = max(0.01, 1.0 - mr_gain * gain_ratio)
        seg_net = seg_gross * nf
        if rem_net <= seg_net + 1e-9:
            gross_acc += min(seg_gross, rem_net / nf)
            return min(lot_val, gross_acc)
        gross_acc += seg_gross
        rem_net -= seg_net
    return lot_val


def _compute_progressive_ssn_mo(
    state_map: dict[str, Any],
    ssn_gross_mo: float,
    iso: str,
    dfl: float,
    est_jan_non_ssn_income: float = 0.0,
) -> tuple[float, float]:
    """
    Computes IRC §86 provisional-income Social Security tax & after-tax monthly SSN (`DA-C03`, `DA2-05`).
    DESIGN & PARITY INVARIANT (lockstep with `computeProgressiveSsnMo` in `app_simulator.js`):
      - IRC §86 thresholds (`$32k/$44k` MFJ, `$25k/$34k` Single) are FULL-YEAR unindexed statutory thresholds.
      - DO NOT compare raw partial-year `ytd["agi_ord"]` in Q1 against `$32k/$44k` without annualizing recurring
        non-SSN income; otherwise January/Q1 months receive `0%` SSN tax (`taxable_frac = 0`) even for high-income
        IRA/LTCG households, permanently omitting Q1 taxable SSN from annual `agi_ord`.
      - Lump-sum annual items (`init_floor_ord`, `rmd_ord`, `vest_ord`) enter at `1.0x`, monthly yields (`yield_ord`,
        `muni_int`) annualize by `12 / mo_num`, and prior-month drawdowns (`draw_ord`, `ltcg`) annualize by
        `12 / (mo_num - 1)` (with `est_jan_non_ssn_income` supplying Month-1's annualized drawdown estimate).
    """
    if ssn_gross_mo <= 1e-6:
        return 0.0, 0.0
    tc = state_map.get("_taxCfg")
    ytd = _ensure_tax_ytd(state_map, iso, dfl)
    if not tc or not ytd:
        return 0.0, ssn_gross_mo
    is_mfj = bool(tc["is_mfj"])
    t1, t2 = (32000.0, 44000.0) if is_mfj else (25000.0, 34000.0)
    ann_ssn = ssn_gross_mo * 12.0
    mo_num = max(1, min(12, int(iso[5:7])))
    lump_ord = (
        float(ytd.get("init_floor_ord") or 0.0)
        + float(ytd.get("rmd_ord") or 0.0)
        + float(ytd.get("vest_ord") or 0.0)
    )
    yield_ord_ytd = float(ytd.get("yield_ord") or 0.0)
    muni_ytd = float(ytd.get("muni_int") or 0.0)
    ssn_taxable_ytd = float(ytd.get("ssn_taxable") or 0.0)
    ann_yield_and_muni = (yield_ord_ytd + muni_ytd) * (12.0 / mo_num)
    if mo_num > 1:
        draw_ord_ytd = max(0.0, float(ytd["agi_ord"]) - lump_ord - yield_ord_ytd - ssn_taxable_ytd)
        ann_draw = (draw_ord_ytd + float(ytd["ltcg"])) * (12.0 / (mo_num - 1.0))
    else:
        ann_draw = max(0.0, est_jan_non_ssn_income)
    prov = lump_ord + ann_yield_and_muni + ann_draw + 0.5 * ann_ssn
    if prov <= t1:
        taxable_frac = 0.0
    elif prov <= t2:
        taxable_frac = min(0.5, (0.5 * (prov - t1)) / ann_ssn)
    else:
        tier1_max = 0.5 * (t2 - t1)
        taxable_frac = min(0.85, (tier1_max + 0.85 * (prov - t2)) / ann_ssn)
    taxable_frac = min(float(tc.get("ssn_fed_cap", 0.85)), max(0.0, taxable_frac))
    taxable_mo = ssn_gross_mo * taxable_frac
    ytd["ssn_taxable"] = ssn_taxable_ytd + taxable_mo
    fed_res = _tax_ordinary_increment(
        state_map,
        taxable_mo,
        iso=iso,
        dfl=dfl,
        apply_niit=False,
        state_rate_override=0.0,
        extra_penalty=0.0,
        allow_loss_offset=False,
    )
    st_tax = ssn_gross_mo * float(tc.get("state_ssn_rate", 0.0))
    tot_tax = min(ssn_gross_mo, fed_res["tax"] + st_tax)
    return tot_tax, max(0.0, ssn_gross_mo - tot_tax)


def _clone_sim_asset_state(
    sim_assets: list[dict[str, Any]],
    today_iso: str,
    include_unvested: bool,
    initial_loss_carryforward: float = 0.0,
    tax_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_map: dict[str, Any] = {
        "_lossCarryforward": float(initial_loss_carryforward),
        "_totalLossHarvested": float(initial_loss_carryforward),
        "_totalLossUsed": 0.0,
        "_totalTaxSavedByLoss": 0.0,
    }
    if tax_cfg is not None:
        state_map["_taxCfg"] = copy.deepcopy(tax_cfg)
    for a in sim_assets:
        base_stats = {
            "firstTappedLbl": None,
            "firstTappedIso": None,
            "lastTappedLbl": None,
            "lastTappedIso": None,
            "depletedLbl": None,
            "depletedIso": None,
            "totalGrossSold": 0.0,
            "totalNetProceeds": 0.0,
            "totalTaxPaid": 0.0,
            "totalLossHarvested": 0.0,
            "totalLossSheltered": 0.0,
            "totalTaxSaved": 0.0,
        }
        if a.get("is_unvested_goog"):
            if not include_unvested:
                state_map[a["id"]] = {
                    **a,
                    **base_stats,
                    "val": 0.0,
                    "costBasis": 0.0,
                    "tranches": [],
                }
            else:
                trs = [
                    {
                        "vestIso": t["vest_date"],
                        "shares": float(t.get("shares") or 0.0),
                        "vested": str(t["vest_date"]) <= today_iso,
                        "val": float(t.get("current_value") or 0.0),
                        "costBasis": float(t.get("current_value") or 0.0),
                    }
                    for t in (a.get("tranches") or [])
                ]
                v_sum = sum(t["val"] for t in trs)
                state_map[a["id"]] = {
                    **a,
                    **base_stats,
                    "val": v_sum,
                    "costBasis": v_sum,
                    "tranches": trs,
                }
        elif a.get("tax_model") == "LTCG_TRANCHES":
            trs = [
                {
                    "vestIso": t["vest_date"],
                    "shares": float(t.get("shares") or 0.0),
                    "vested": True,
                    "val": float(t.get("current_value") or 0.0),
                    "costBasis": float(t.get("cost_basis") or 0.0),
                }
                for t in (a.get("tranches") or [])
            ]
            # Fallback to bucket current_value/cost_basis if lot history is absent so Month-0 value isn't zeroed
            if not trs and float(a.get("current_value") or 0.0) > 0:
                trs = [
                    {
                        "vestIso": "",
                        "shares": float(a.get("shares") or 0.0),
                        "vested": True,
                        "val": float(a.get("current_value") or 0.0),
                        "costBasis": float(a.get("cost_basis") or 0.0),
                    }
                ]
            v_sum = sum(t["val"] for t in trs)
            b_sum = sum(t["costBasis"] for t in trs)
            bucket_cv = float(a.get("current_value") or 0.0)
            bucket_cb = float(a.get("cost_basis") or 0.0)
            if trs and bucket_cv - v_sum > 0.01:
                diff_v = bucket_cv - v_sum
                diff_b = max(0.0, bucket_cb - b_sum)
                trs.append(
                    {
                        "vestIso": "",
                        "shares": 0.0,
                        "vested": True,
                        "val": diff_v,
                        "costBasis": diff_b,
                    }
                )
                v_sum += diff_v
                b_sum += diff_b
            state_map[a["id"]] = {
                **a,
                **base_stats,
                "val": v_sum,
                "costBasis": b_sum,
                "tranches": trs,
            }
        else:
            state_map[a["id"]] = {
                **a,
                **base_stats,
                "val": float(a.get("current_value") or 0.0),
                "costBasis": float(a.get("cost_basis") or 0.0),
                "tranches": [],
            }
    return state_map


def _sell_capital_gain_lot(
    lot: dict[str, Any],
    rem_net_needed: float,
    tax_rate: float,
    state_map: dict[str, Any],
    *,
    iso: str = "",
    dfl: float = 1.0,
    apply_state: bool = True,
) -> dict[str, float]:
    gross_sold = 0.0
    net_proceeds = 0.0
    tax_paid = 0.0
    loss_harvested = 0.0
    loss_sheltered = 0.0
    tax_saved = 0.0

    if lot["val"] <= 0.005 or rem_net_needed <= 0.005:
        return {
            "grossSold": gross_sold,
            "netProceeds": net_proceeds,
            "taxPaid": tax_paid,
            "lossHarvested": loss_harvested,
            "lossSheltered": loss_sheltered,
            "taxSaved": tax_saved,
        }

    tc = state_map.get("_taxCfg")
    use_prog = bool(tc and tc.get("progressive") and iso)

    if lot["val"] <= lot["costBasis"] + 1e-6 or (not use_prog and tax_rate <= 0):
        take_gross = min(rem_net_needed, lot["val"])
        frac = min(1.0, take_gross / lot["val"])
        basis_sold = lot["costBasis"] * frac
        realized_loss = max(0.0, basis_sold - take_gross)
        lot["val"] = max(0.0, lot["val"] - take_gross)
        lot["costBasis"] = max(0.0, lot["costBasis"] - basis_sold)
        gross_sold += take_gross
        net_proceeds += take_gross
        if realized_loss > 0.005:
            loss_harvested += realized_loss
            state_map["_lossCarryforward"] = float(state_map.get("_lossCarryforward") or 0.0) + realized_loss
            state_map["_totalLossHarvested"] = float(state_map.get("_totalLossHarvested") or 0.0) + realized_loss
        return {
            "grossSold": gross_sold,
            "netProceeds": net_proceeds,
            "taxPaid": tax_paid,
            "lossHarvested": loss_harvested,
            "lossSheltered": loss_sheltered,
            "taxSaved": tax_saved,
        }

    gain_ratio = max(0.0, (lot["val"] - lot["costBasis"]) / lot["val"])
    rem_need = rem_net_needed
    is_stcg = use_prog and _is_short_term_lot(lot, iso)

    avail_loss = float(state_map.get("_lossCarryforward") or 0.0)
    if avail_loss > 0.005 and gain_ratio > 1e-6 and rem_need > 0.005 and lot["val"] > 0.005:
        max_gross_for_loss = avail_loss / gain_ratio
        sheltered_gross = min(rem_need, lot["val"], max_gross_for_loss)
        frac1 = min(1.0, sheltered_gross / lot["val"])
        basis_sold1 = lot["costBasis"] * frac1
        gain_realized1 = max(0.0, sheltered_gross - basis_sold1)
        if use_prog:
            if is_stcg:
                st_ovr_s = None if apply_state else 0.0
                saved_tax1 = _tax_ordinary_increment(
                    state_map,
                    gain_realized1,
                    iso=iso,
                    dfl=dfl,
                    apply_niit=True,
                    state_rate_override=st_ovr_s,
                    extra_penalty=0.0,
                    allow_loss_offset=False,
                    commit=False,
                )["tax"]
            else:
                saved_tax1 = _tax_ltcg_increment(state_map, gain_realized1, iso=iso, dfl=dfl, apply_state=apply_state, commit=False)
        else:
            saved_tax1 = gain_realized1 * tax_rate

        lot["val"] = max(0.0, lot["val"] - sheltered_gross)
        lot["costBasis"] = max(0.0, lot["costBasis"] - basis_sold1)
        state_map["_lossCarryforward"] = max(0.0, avail_loss - gain_realized1)
        state_map["_totalLossUsed"] = float(state_map.get("_totalLossUsed") or 0.0) + gain_realized1
        state_map["_totalTaxSavedByLoss"] = float(state_map.get("_totalTaxSavedByLoss") or 0.0) + saved_tax1

        gross_sold += sheltered_gross
        net_proceeds += sheltered_gross
        loss_sheltered += gain_realized1
        tax_saved += saved_tax1
        rem_need -= sheltered_gross

    if rem_need > 0.005 and lot["val"] > 0.005:
        cur_gain_ratio = max(0.0, (lot["val"] - lot["costBasis"]) / lot["val"])
        if use_prog:
            st_ovr = None if apply_state else 0.0
            if is_stcg and cur_gain_ratio > 1e-6:
                # DA2-06: Short-term gain (<365 days) solved via exact piecewise ordinary + NIIT integration
                gross_taken = _gross_for_net_stcg(
                    state_map, lot["val"], cur_gain_ratio, rem_need, iso=iso, dfl=dfl, apply_state=apply_state
                )
                frac2 = min(1.0, gross_taken / lot["val"])
                gain_taken = max(0.0, gross_taken - lot["costBasis"] * frac2)
                ord_res = _tax_ordinary_increment(
                    state_map, gain_taken, iso=iso, dfl=dfl, apply_niit=True, state_rate_override=st_ovr, extra_penalty=0.0, allow_loss_offset=False
                )
                tax_taken = ord_res["tax"]
                net_taken = max(0.0, gross_taken - tax_taken)
            else:
                gross_taken = _gross_for_net_ltcg(
                    state_map, lot["val"], cur_gain_ratio, rem_need, iso=iso, dfl=dfl, apply_state=apply_state
                )
                frac2 = min(1.0, gross_taken / lot["val"])
                gain_taken = max(0.0, gross_taken - lot["costBasis"] * frac2)
                tax_taken = _tax_ltcg_increment(state_map, gain_taken, iso=iso, dfl=dfl, apply_state=apply_state, commit=True)
                net_taken = max(0.0, gross_taken - tax_taken)
        else:
            eff_rate = tax_rate * cur_gain_ratio
            net_factor = max(0.01, 1.0 - eff_rate)
            max_net = lot["val"] * net_factor
            net_taken = min(rem_need, max_net)
            gross_taken = net_taken / net_factor
            tax_taken = gross_taken - net_taken
            frac2 = min(1.0, gross_taken / lot["val"])

        lot["val"] = max(0.0, lot["val"] - gross_taken)
        lot["costBasis"] = max(0.0, lot["costBasis"] * (1.0 - frac2))
        gross_sold += gross_taken
        net_proceeds += net_taken
        tax_paid += tax_taken

    return {
        "grossSold": gross_sold,
        "netProceeds": net_proceeds,
        "taxPaid": tax_paid,
        "lossHarvested": loss_harvested,
        "lossSheltered": loss_sheltered,
        "taxSaved": tax_saved,
    }


def _sell_capital_gain_lot_gross(
    lot: dict[str, Any],
    gross_to_sell: float,
    tax_rate: float,
    state_map: dict[str, Any],
    *,
    iso: str = "",
    dfl: float = 1.0,
    apply_state: bool = True,
) -> dict[str, float]:
    """Sells a specified GROSS dollar amount from a capital-gain lot/pool, deducting LTCG/STCG tax from proceeds."""
    gross_sold = 0.0
    net_proceeds = 0.0
    tax_paid = 0.0
    loss_harvested = 0.0
    loss_sheltered = 0.0
    tax_saved = 0.0

    if lot["val"] <= 0.005 or gross_to_sell <= 0.005:
        return {
            "grossSold": gross_sold,
            "netProceeds": net_proceeds,
            "taxPaid": tax_paid,
            "lossHarvested": loss_harvested,
            "lossSheltered": loss_sheltered,
            "taxSaved": tax_saved,
        }

    tc = state_map.get("_taxCfg")
    use_prog = bool(tc and tc.get("progressive") and iso)
    is_stcg = use_prog and _is_short_term_lot(lot, iso)

    take_gross = min(gross_to_sell, lot["val"])
    frac = min(1.0, take_gross / lot["val"])
    basis_sold = lot["costBasis"] * frac
    gain_or_loss = take_gross - basis_sold
    lot["val"] = max(0.0, lot["val"] - take_gross)
    lot["costBasis"] = max(0.0, lot["costBasis"] - basis_sold)
    gross_sold = take_gross

    if gain_or_loss < -0.005:
        loss_harvested = abs(gain_or_loss)
        state_map["_lossCarryforward"] = float(state_map.get("_lossCarryforward") or 0.0) + loss_harvested
        state_map["_totalLossHarvested"] = float(state_map.get("_totalLossHarvested") or 0.0) + loss_harvested
        net_proceeds = take_gross
    elif gain_or_loss > 0.005 and (use_prog or tax_rate > 0):
        avail_loss = float(state_map.get("_lossCarryforward") or 0.0)
        sheltered = min(avail_loss, gain_or_loss)
        if sheltered > 0.005:
            state_map["_lossCarryforward"] = max(0.0, avail_loss - sheltered)
            state_map["_totalLossUsed"] = float(state_map.get("_totalLossUsed") or 0.0) + sheltered
            if use_prog:
                if is_stcg:
                    st_ovr_s = None if apply_state else 0.0
                    tax_saved = _tax_ordinary_increment(
                        state_map,
                        sheltered,
                        iso=iso,
                        dfl=dfl,
                        apply_niit=True,
                        state_rate_override=st_ovr_s,
                        extra_penalty=0.0,
                        allow_loss_offset=False,
                        commit=False,
                    )["tax"]
                else:
                    tax_saved = _tax_ltcg_increment(state_map, sheltered, iso=iso, dfl=dfl, apply_state=apply_state, commit=False)
            else:
                tax_saved = sheltered * tax_rate
            state_map["_totalTaxSavedByLoss"] = float(state_map.get("_totalTaxSavedByLoss") or 0.0) + tax_saved
            loss_sheltered = sheltered
        taxable_gain = max(0.0, gain_or_loss - sheltered)
        if taxable_gain > 0.005:
            if use_prog:
                if is_stcg:
                    st_ovr = None if apply_state else 0.0
                    tax_paid = _tax_ordinary_increment(
                        state_map, taxable_gain, iso=iso, dfl=dfl, apply_niit=True, state_rate_override=st_ovr, extra_penalty=0.0, allow_loss_offset=False
                    )["tax"]
                else:
                    tax_paid = _tax_ltcg_increment(state_map, taxable_gain, iso=iso, dfl=dfl, apply_state=apply_state, commit=True)
            else:
                tax_paid = taxable_gain * tax_rate
        net_proceeds = max(0.0, take_gross - tax_paid)
    else:
        net_proceeds = take_gross

    return {
        "grossSold": gross_sold,
        "netProceeds": net_proceeds,
        "taxPaid": tax_paid,
        "lossHarvested": loss_harvested,
        "lossSheltered": loss_sheltered,
        "taxSaved": tax_saved,
    }


def _is_bucket_locked(st: dict[str, Any], iso: str) -> bool:
    unlock_ym = st.get("unlock_ym")
    if unlock_ym and iso[:7] < str(unlock_ym):
        return True
    mat_iso = st.get("maturity_iso")
    if mat_iso and iso < str(mat_iso) and (
        st.get("tax_model") in ("ANNUITY_LIFO", "ORDINARY_FULL")
        or st.get("is_hypothetical")
        or st.get("maturity_locked")
    ):
        return True
    return False


def _record_bucket_tap_stats(
    st: dict[str, Any],
    iso: str,
    full_lbl: str,
    row_gross: float,
    row_net: float,
    row_tax: float,
    row_loss_harv: float,
    row_loss_shelt: float,
    row_tax_sav: float,
) -> None:
    if not st["firstTappedLbl"]:
        st["firstTappedLbl"] = full_lbl
        st["firstTappedIso"] = iso
    st["lastTappedLbl"] = full_lbl
    st["lastTappedIso"] = iso
    st["totalGrossSold"] += row_gross
    st["totalNetProceeds"] += row_net
    st["totalTaxPaid"] += row_tax
    st["totalLossHarvested"] += row_loss_harv
    st["totalLossSheltered"] += row_loss_shelt
    st["totalTaxSaved"] += row_tax_sav
    has_unvested_left = bool(
        st.get("is_unvested_goog")
        and any((not t["vested"] and t["val"] > 0.5) for t in (st.get("tranches") or []))
    )
    if st["val"] <= 1.0 and not has_unvested_left and not st["depletedLbl"]:
        st["depletedLbl"] = full_lbl
        st["depletedIso"] = iso


def _lot_sort_key(lot: dict[str, Any], iso: str, use_prog: bool) -> tuple[int, float]:
    """
    Tax-aware lot liquidation order (`DA-E04`, `DA2-09` — lockstep with `lotSortKey` in `app_simulator.js`):
      1. Underwater / 0%-gain lots (`gain_ratio <= 0`) sell FIRST (`st_tier = 0`, negative/zero ratio) to harvest losses.
      2. Long-term appreciated lots (`>= 365` days, preferential LTCG) sell NEXT (`st_tier = 0`, ascending `gain_ratio`).
      3. Short-term appreciated lots (`< 365` days, ordinary rates + NIIT) sell LAST (`st_tier = 1`, ascending `gain_ratio`).
    """
    gr = (float(lot["val"]) - float(lot["costBasis"])) / float(lot["val"])
    st_tier = 1 if (use_prog and gr > 1e-6 and _is_short_term_lot(lot, iso)) else 0
    return (st_tier, gr)


def _liquidate_net_from_bucket(
    st: dict[str, Any],
    net_needed: float,
    state_map: dict[str, Any],
    iso: str,
    full_lbl: str,
    record_stats: bool,
    dfl: float = 1.0,
) -> dict[str, float]:
    """
    Liquidates enough gross value from bucket `st` to deliver up to `net_needed` after-tax cash.

    STATUTORY & PRODUCT INVARIANTS (DO NOT REGRESS):
      - `DA-E04` / `DA2-09` (IRC §1222): `_lot_sort_key` prioritizes loss/zero-gain and `>=365d` LTCG lots ahead of
        appreciated `<365d` STCG lots; `_sell_capital_gain_lot` integrates piecewise STCG/LTCG brackets cleanly.
      - `DA-C02` (User Decision A3 — IRC §72(t)/§72(q) & §408A): Prior to owner age `59.5`, `early_pen = +10%`
        applies ONLY to taxable `ORDINARY_FULL` distributions, `ANNUITY_LIFO` earnings (before 0%-tax basis),
        and `ROTH_IRA` (`TAX_FREE`) earnings AFTER 100% tax- & penalty-free recovery of `costBasis` contributions.
        Taxable brokerage (`LTCG_*`), `CASH`, `MUNI`, and `TREASURY` are never subject to the 59.5 penalty.
      - `DA-A02`: `TREASURY` buckets set `apply_state = False` (`0%` state income tax under 31 U.S.C. §3124).
    """
    rem_net_needed = net_needed
    row_gross = 0.0
    row_net = 0.0
    row_tax = 0.0
    row_loss_harv = 0.0
    row_loss_shelt = 0.0
    row_tax_sav = 0.0

    tc = state_map.get("_taxCfg")
    use_prog = bool(tc and tc.get("progressive") and iso)
    early_pen = 0.10 if (use_prog and _owner_is_pre_59_half(st, state_map, iso)) else 0.0
    apply_state = st.get("tax_model") != "TREASURY"

    trs = st.get("tranches") or []
    if trs:
        avail = [t for t in trs if t["vested"] and t["val"] > 0.005]
        avail.sort(key=lambda t: _lot_sort_key(t, iso, use_prog))
        for t in avail:
            if rem_net_needed <= 0.005:
                break
            lot_res = _sell_capital_gain_lot(
                t, rem_net_needed, float(st["tax_rate"]), state_map, iso=iso, dfl=dfl, apply_state=apply_state
            )
            rem_net_needed -= lot_res["netProceeds"]
            row_gross += lot_res["grossSold"]
            row_net += lot_res["netProceeds"]
            row_tax += lot_res["taxPaid"]
            row_loss_harv += lot_res["lossHarvested"]
            row_loss_shelt += lot_res["lossSheltered"]
            row_tax_sav += lot_res["taxSaved"]
        st["val"] = sum(t["val"] for t in trs)
        st["costBasis"] = sum(t["costBasis"] for t in trs)
    elif st.get("tax_model") == "ANNUITY_LIFO":
        if st["val"] > 0.005:
            earnings = max(0.0, st["val"] - st["costBasis"])
            if earnings > 0.005 and rem_net_needed > 0.005:
                if use_prog:
                    gross_earn = _gross_for_net_ordinary(
                        state_map, earnings, rem_net_needed, iso=iso, dfl=dfl, apply_niit=True, extra_penalty=early_pen
                    )
                    ord_res = _tax_ordinary_increment(
                        state_map, gross_earn, iso=iso, dfl=dfl, apply_niit=True, extra_penalty=early_pen
                    )
                    tax_earn = ord_res["tax"]
                    net_earn = max(0.0, gross_earn - tax_earn)
                    row_loss_shelt += ord_res["lossSheltered"]
                    row_tax_sav += ord_res["taxSaved"]
                else:
                    net_factor = max(0.01, 1.0 - float(st["tax_rate"]))
                    max_net_earn = earnings * net_factor
                    net_earn = min(rem_net_needed, max_net_earn)
                    gross_earn = net_earn / net_factor
                    tax_earn = gross_earn - net_earn
                st["val"] = max(0.0, st["val"] - gross_earn)
                rem_net_needed -= net_earn
                row_gross += gross_earn
                row_net += net_earn
                row_tax += tax_earn
            if st["val"] > 0.005 and rem_net_needed > 0.005:
                net_prin = min(rem_net_needed, st["val"])
                st["val"] = max(0.0, st["val"] - net_prin)
                st["costBasis"] = max(0.0, st["costBasis"] - net_prin)
                rem_net_needed -= net_prin
                row_gross += net_prin
                row_net += net_prin
    elif st.get("tax_model") == "TAX_FREE":
        if st["val"] > 0.005 and rem_net_needed > 0.005:
            if early_pen > 0.0 and st["val"] > st["costBasis"] + 0.005:
                # IRC §408A ordering: Roth contributions (`costBasis`) withdraw 0% tax & 0% penalty first;
                # pre-59.5 earnings above basis incur ordinary income tax + 10% early-withdrawal penalty.
                contrib = min(st["val"], max(0.0, st["costBasis"]))
                if contrib > 0.005:
                    take_c = min(rem_net_needed, contrib)
                    st["val"] = max(0.0, st["val"] - take_c)
                    st["costBasis"] = max(0.0, st["costBasis"] - take_c)
                    rem_net_needed -= take_c
                    row_gross += take_c
                    row_net += take_c
                if st["val"] > 0.005 and rem_net_needed > 0.005:
                    gross_e = _gross_for_net_ordinary(
                        state_map, st["val"], rem_net_needed, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
                    )
                    ord_e = _tax_ordinary_increment(
                        state_map, gross_e, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
                    )
                    tax_e = ord_e["tax"]
                    net_e = max(0.0, gross_e - tax_e)
                    st["val"] = max(0.0, st["val"] - gross_e)
                    rem_net_needed -= net_e
                    row_gross += gross_e
                    row_net += net_e
                    row_tax += tax_e
                    row_loss_shelt += ord_e["lossSheltered"]
                    row_tax_sav += ord_e["taxSaved"]
            else:
                net_take = min(rem_net_needed, st["val"])
                frac = min(1.0, net_take / st["val"])
                st["val"] = max(0.0, st["val"] - net_take)
                st["costBasis"] = max(0.0, st["costBasis"] * (1.0 - frac))
                rem_net_needed -= net_take
                row_gross += net_take
                row_net += net_take
    elif st.get("tax_model") == "ORDINARY_FULL":
        if st["val"] > 0.005 and rem_net_needed > 0.005:
            if use_prog:
                gross = _gross_for_net_ordinary(
                    state_map, st["val"], rem_net_needed, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
                )
                ord_res = _tax_ordinary_increment(
                    state_map, gross, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
                )
                tax = ord_res["tax"]
                net_take = max(0.0, gross - tax)
                row_loss_shelt += ord_res["lossSheltered"]
                row_tax_sav += ord_res["taxSaved"]
            else:
                net_factor = max(0.01, 1.0 - float(st.get("tax_rate") or 0.0))
                max_net = st["val"] * net_factor
                net_take = min(rem_net_needed, max_net)
                gross = net_take / net_factor
                tax = gross - net_take
            frac = min(1.0, gross / st["val"])
            st["val"] = max(0.0, st["val"] - gross)
            st["costBasis"] = max(0.0, st["costBasis"] * (1.0 - frac))
            rem_net_needed -= net_take
            row_gross += gross
            row_net += net_take
            row_tax += tax
    elif st["val"] > 0.005:
        lot_res = _sell_capital_gain_lot(
            st, rem_net_needed, float(st["tax_rate"]), state_map, iso=iso, dfl=dfl, apply_state=apply_state
        )
        rem_net_needed -= lot_res["netProceeds"]
        row_gross += lot_res["grossSold"]
        row_net += lot_res["netProceeds"]
        row_tax += lot_res["taxPaid"]
        row_loss_harv += lot_res["lossHarvested"]
        row_loss_shelt += lot_res["lossSheltered"]
        row_tax_sav += lot_res["taxSaved"]

    if row_gross > 0.01 and record_stats:
        _record_bucket_tap_stats(
            st, iso, full_lbl, row_gross, row_net, row_tax, row_loss_harv, row_loss_shelt, row_tax_sav
        )
    return {
        "grossSold": row_gross,
        "netProceeds": row_net,
        "taxPaid": row_tax,
        "lossHarvested": row_loss_harv,
        "lossSheltered": row_loss_shelt,
        "taxSaved": row_tax_sav,
    }


def _liquidate_gross_from_bucket(
    st: dict[str, Any],
    gross_target: float,
    state_map: dict[str, Any],
    iso: str,
    full_lbl: str,
    record_stats: bool,
    newly_vested_only_ym: str | None = None,
    dfl: float = 1.0,
    suppress_early_penalty: bool = False,
) -> dict[str, float]:
    rem_gross = gross_target
    row_gross = 0.0
    row_net = 0.0
    row_tax = 0.0
    row_loss_harv = 0.0
    row_loss_shelt = 0.0
    row_tax_sav = 0.0

    tc = state_map.get("_taxCfg")
    use_prog = bool(tc and tc.get("progressive") and iso)
    early_pen = (
        0.0
        if suppress_early_penalty
        else (0.10 if (use_prog and _owner_is_pre_59_half(st, state_map, iso)) else 0.0)
    )
    apply_state = st.get("tax_model") != "TREASURY"

    trs = st.get("tranches") or []
    if trs:
        avail = [t for t in trs if t["vested"] and t["val"] > 0.005]
        if newly_vested_only_ym:
            avail = [t for t in avail if t.get("_vestedYm") == newly_vested_only_ym]
        avail.sort(key=lambda t: _lot_sort_key(t, iso, use_prog))
        for t in avail:
            if rem_gross <= 0.005:
                break
            lot_res = _sell_capital_gain_lot_gross(
                t, rem_gross, float(st["tax_rate"]), state_map, iso=iso, dfl=dfl, apply_state=apply_state
            )
            rem_gross -= lot_res["grossSold"]
            row_gross += lot_res["grossSold"]
            row_net += lot_res["netProceeds"]
            row_tax += lot_res["taxPaid"]
            row_loss_harv += lot_res["lossHarvested"]
            row_loss_shelt += lot_res["lossSheltered"]
            row_tax_sav += lot_res["taxSaved"]
        st["val"] = sum(t["val"] for t in trs)
        st["costBasis"] = sum(t["costBasis"] for t in trs)
    elif st.get("tax_model") == "ANNUITY_LIFO":
        if st["val"] > 0.005 and rem_gross > 0.005:
            earnings = max(0.0, st["val"] - st["costBasis"])
            if earnings > 0.005:
                gross_earn = min(rem_gross, earnings)
                if use_prog:
                    ord_res = _tax_ordinary_increment(
                        state_map, gross_earn, iso=iso, dfl=dfl, apply_niit=True, extra_penalty=early_pen
                    )
                    tax_earn = ord_res["tax"]
                    row_loss_shelt += ord_res["lossSheltered"]
                    row_tax_sav += ord_res["taxSaved"]
                else:
                    tax_earn = gross_earn * float(st["tax_rate"])
                net_earn = max(0.0, gross_earn - tax_earn)
                st["val"] = max(0.0, st["val"] - gross_earn)
                rem_gross -= gross_earn
                row_gross += gross_earn
                row_net += net_earn
                row_tax += tax_earn
            if st["val"] > 0.005 and rem_gross > 0.005:
                gross_prin = min(rem_gross, st["val"])
                st["val"] = max(0.0, st["val"] - gross_prin)
                st["costBasis"] = max(0.0, st["costBasis"] - gross_prin)
                rem_gross -= gross_prin
                row_gross += gross_prin
                row_net += gross_prin
    elif st.get("tax_model") == "TAX_FREE":
        if st["val"] > 0.005 and rem_gross > 0.005:
            if early_pen > 0.0 and st["val"] > st["costBasis"] + 0.005:
                contrib = min(st["val"], max(0.0, st["costBasis"]))
                take_c = min(rem_gross, contrib)
                if take_c > 0.005:
                    st["val"] = max(0.0, st["val"] - take_c)
                    st["costBasis"] = max(0.0, st["costBasis"] - take_c)
                    rem_gross -= take_c
                    row_gross += take_c
                    row_net += take_c
                if st["val"] > 0.005 and rem_gross > 0.005:
                    gross_e = min(rem_gross, st["val"])
                    ord_e = _tax_ordinary_increment(
                        state_map, gross_e, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
                    )
                    tax_e = ord_e["tax"]
                    net_e = max(0.0, gross_e - tax_e)
                    st["val"] = max(0.0, st["val"] - gross_e)
                    row_gross += gross_e
                    row_net += net_e
                    row_tax += tax_e
                    row_loss_shelt += ord_e["lossSheltered"]
                    row_tax_sav += ord_e["taxSaved"]
            else:
                gross = min(rem_gross, st["val"])
                frac = min(1.0, gross / st["val"])
                st["val"] = max(0.0, st["val"] - gross)
                st["costBasis"] = max(0.0, st["costBasis"] * (1.0 - frac))
                row_gross += gross
                row_net += gross
    elif st.get("tax_model") == "ORDINARY_FULL":
        if st["val"] > 0.005 and rem_gross > 0.005:
            gross = min(rem_gross, st["val"])
            if use_prog:
                ord_res = _tax_ordinary_increment(
                    state_map, gross, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
                )
                tax = ord_res["tax"]
                row_loss_shelt += ord_res["lossSheltered"]
                row_tax_sav += ord_res["taxSaved"]
            else:
                tax = gross * float(st.get("tax_rate") or 0.0)
            net = max(0.0, gross - tax)
            frac = min(1.0, gross / st["val"])
            st["val"] = max(0.0, st["val"] - gross)
            st["costBasis"] = max(0.0, st["costBasis"] * (1.0 - frac))
            row_gross += gross
            row_net += net
            row_tax += tax
    elif st["val"] > 0.005 and rem_gross > 0.005:
        lot_res = _sell_capital_gain_lot_gross(
            st, rem_gross, float(st["tax_rate"]), state_map, iso=iso, dfl=dfl, apply_state=apply_state
        )
        row_gross += lot_res["grossSold"]
        row_net += lot_res["netProceeds"]
        row_tax += lot_res["taxPaid"]
        row_loss_harv += lot_res["lossHarvested"]
        row_loss_shelt += lot_res["lossSheltered"]
        row_tax_sav += lot_res["taxSaved"]

    if row_gross > 0.01 and record_stats:
        _record_bucket_tap_stats(
            st, iso, full_lbl, row_gross, row_net, row_tax, row_loss_harv, row_loss_shelt, row_tax_sav
        )
    return {
        "grossSold": row_gross,
        "netProceeds": row_net,
        "taxPaid": row_tax,
        "lossHarvested": row_loss_harv,
        "lossSheltered": row_loss_shelt,
        "taxSaved": row_tax_sav,
    }


def _liquidate_locked_annuity_corridor(
    st: dict[str, Any],
    net_needed: float,
    max_gross: float,
    state_map: dict[str, Any],
    iso: str,
    full_lbl: str,
    record_stats: bool,
    dfl: float = 1.0,
    surr_fee_rate: float = 0.0,
) -> dict[str, float]:
    """
    Draws up to `max_gross` from a surrender-locked annuity (`ANNUITY_LIFO` or `ORDINARY_FULL`)
    under either its annual penalty-free corridor (`surr_fee_rate == 0.0`) or emergency surrender
    (`surr_fee_rate > 0.0`) when all unlocked liquid buckets are exhausted (`DA2-09`).
    PARITY INVARIANT: Must match `liquidateLockedAnnuityCorridor` in `app_simulator.js` 1:1.
    """
    cap_g = min(float(st["val"]), max_gross)
    if cap_g <= 0.005 or net_needed <= 0.005:
        return {"grossSold": 0.0, "netProceeds": 0.0, "taxPaid": 0.0, "lossHarvested": 0.0, "lossSheltered": 0.0, "taxSaved": 0.0}
    keep_mult = max(0.50, 1.0 - max(0.0, surr_fee_rate))
    rem_pre_surr = net_needed / keep_mult
    row_gross = 0.0
    row_net = 0.0
    row_tax = 0.0
    row_loss_shelt = 0.0
    row_tax_sav = 0.0
    tc = state_map.get("_taxCfg")
    use_prog = bool(tc and tc.get("progressive") and iso)
    early_pen = 0.10 if (use_prog and _owner_is_pre_59_half(st, state_map, iso)) else 0.0

    if st.get("tax_model") == "ANNUITY_LIFO":
        earnings = min(cap_g, max(0.0, float(st["val"]) - float(st["costBasis"])))
        if earnings > 0.005 and rem_pre_surr > 0.005:
            if use_prog:
                gross_e = _gross_for_net_ordinary(
                    state_map, earnings, rem_pre_surr, iso=iso, dfl=dfl, apply_niit=True, extra_penalty=early_pen
                )
                ord_res = _tax_ordinary_increment(
                    state_map, gross_e, iso=iso, dfl=dfl, apply_niit=True, extra_penalty=early_pen
                )
                tax_e = ord_res["tax"]
                net_e = max(0.0, gross_e - tax_e)
                row_loss_shelt += ord_res["lossSheltered"]
                row_tax_sav += ord_res["taxSaved"]
            else:
                nf = max(0.01, 1.0 - float(st.get("tax_rate") or 0.0))
                net_e = min(rem_pre_surr, earnings * nf)
                gross_e = net_e / nf
                tax_e = gross_e - net_e
            st["val"] = max(0.0, float(st["val"]) - gross_e)
            cap_g = max(0.0, cap_g - gross_e)
            rem_pre_surr -= net_e
            row_gross += gross_e
            row_net += net_e
            row_tax += tax_e
        if cap_g > 0.005 and rem_pre_surr > 0.005 and float(st["val"]) <= float(st["costBasis"]) + 0.01:
            prin = min(rem_pre_surr, cap_g, float(st["val"]))
            st["val"] = max(0.0, float(st["val"]) - prin)
            st["costBasis"] = max(0.0, float(st["costBasis"]) - prin)
            row_gross += prin
            row_net += prin
    elif st.get("tax_model") == "ORDINARY_FULL":
        if use_prog:
            gross = _gross_for_net_ordinary(
                state_map, cap_g, rem_pre_surr, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
            )
            ord_res = _tax_ordinary_increment(
                state_map, gross, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=early_pen
            )
            tax = ord_res["tax"]
            net_t = max(0.0, gross - tax)
            row_loss_shelt += ord_res["lossSheltered"]
            row_tax_sav += ord_res["taxSaved"]
        else:
            nf = max(0.01, 1.0 - float(st.get("tax_rate") or 0.0))
            net_t = min(rem_pre_surr, cap_g * nf)
            gross = net_t / nf
            tax = gross - net_t
        frac = min(1.0, gross / float(st["val"])) if float(st["val"]) > 0.005 else 0.0
        st["val"] = max(0.0, float(st["val"]) - gross)
        st["costBasis"] = max(0.0, float(st["costBasis"]) * (1.0 - frac))
        row_gross += gross
        row_net += net_t
        row_tax += tax

    if surr_fee_rate > 1e-6 and row_gross > 0.005:
        surr_fee = min(row_net, row_gross * surr_fee_rate)
        row_net = max(0.0, row_net - surr_fee)
        row_tax += surr_fee

    if row_gross > 0.01 and record_stats:
        _record_bucket_tap_stats(st, iso, full_lbl, row_gross, row_net, row_tax, 0.0, row_loss_shelt, row_tax_sav)
    return {
        "grossSold": row_gross,
        "netProceeds": row_net,
        "taxPaid": row_tax,
        "lossHarvested": 0.0,
        "lossSheltered": row_loss_shelt,
        "taxSaved": row_tax_sav,
    }


def _step_sim_month(
    sim_assets: list[dict[str, Any]],
    state_map: dict[str, Any],
    iso: str,
    full_lbl: str,
    g_monthly_rate: float,
    ng_monthly_rate: float,
    target_net_burn: float,
    include_unvested: bool,
    active_order: list[str],
    record_stats: bool = True,
    *,
    asset_monthly_rates: dict[str, float] | None = None,
    scheduled_actions: list[dict[str, Any]] | None = None,
    include_scheduled_actions: bool = False,
    deflator: float = 1.0,
    ssn_gross_mo: float | None = None,
    matured_roll_rate_pct: float | None = None,
) -> dict[str, Any]:
    growth_gain = 0.0
    ym = iso[:7]
    dfl = deflator if deflator > 0 else 1.0
    vested_by_asset: dict[str, float] = {}
    tc = state_map.get("_taxCfg")
    use_prog = bool(tc and tc.get("progressive") and iso)
    ytd = _ensure_tax_ytd(state_map, iso, dfl) if use_prog else None

    january_opening = {a["id"]: state_map[a["id"]]["val"] for a in sim_assets if a["id"] in state_map} if iso[5:7] == "01" else {}

    # 1. Compound each active asset for 1 month
    monthly_yield_tax = 0.0
    tot_loss_sheltered = 0.0
    tot_tax_saved = 0.0
    for a in sim_assets:
        is_unv = bool(a.get("is_unvested_goog"))
        if is_unv and not include_unvested:
            continue
        aid = a["id"]
        st = state_map.get(aid)
        if not st:
            continue
        trs = st.get("tranches")
        if not trs and st["val"] <= 0.0:
            continue
        if trs and st["val"] <= 0.0 and not st.get("_has_unvested", is_unv):
            continue
        # DA-E05 / DA2-19: Post-Maturity Fixed-Rate Rollover.
        # Historical Cohorts pass explicit `matured_roll_rate_pct` (`tbill_3m_pct`). In Single-Path / MC,
        # enriched contracts with multi-tier `growth_evidence` follow their Tier-2 -> Tier-3 rollover curve
        # via `asset_monthly_rates[aid]`, while un-enriched legacy fixtures fall back to `3.5%`.
        mat_iso_a = a.get("maturity_iso")
        has_tier_rollover = bool(
            asset_monthly_rates is not None
            and aid in asset_monthly_rates
            and len({int(e["tier"]) for e in (a.get("growth_evidence") or []) if e.get("tier") in (1, 2, 3)}) > 1
        )
        is_matured_fixed = bool(
            mat_iso_a
            and iso >= str(mat_iso_a)
            and not a.get("_is_forecast_override")
            and (matured_roll_rate_pct is not None or not has_tier_rollover)
            and (
                a.get("rate_model") == "fixed"
                or str(a.get("return_source") or "") == "CONTRACT_CREDITED_RATE"
            )
        )
        if is_matured_fixed:
            eff_roll_pct = 3.5 if matured_roll_rate_pct is None else matured_roll_rate_pct
            roll_ann = (eff_roll_pct - float(a.get("fee_pct") or 0.0)) / 100.0
            r = math.pow(1.0 + max(-0.9999, roll_ann), 1.0 / 12.0) - 1.0
        elif asset_monthly_rates is not None and aid in asset_monthly_rates:
            r = asset_monthly_rates[aid]
        elif a.get("rate_model") == "fixed" and a.get("annual_rate_pct") is not None:
            r = math.pow(1.0 + float(a["annual_rate_pct"]) / 100.0, 1.0 / 12.0) - 1.0
        elif a.get("bucket") == "goog":
            r = g_monthly_rate
        else:
            r = ng_monthly_rate
        if trs:
            v_sum = 0.0
            b_sum = 0.0
            any_unvested = False
            for t in trs:
                tv = t["val"]
                if tv <= 0.0:
                    continue
                new_v = tv * (1.0 + r)
                t["val"] = new_v
                growth_gain += new_v - tv
                if is_unv and not t["vested"]:
                    t["costBasis"] = new_v
                    if str(t.get("vestIso") or "")[:7] <= ym:
                        t["vested"] = True
                        t["_vestedYm"] = ym
                        vested_by_asset[aid] = vested_by_asset.get(aid, 0.0) + new_v
                        # Record gross W-2 income recognized at vest into YTD ordinary bracket base (tax already withheld via shares)
                        if use_prog and ytd is not None:
                            net_ret = float(tc.get("net_retention_rate", 0.50))
                            gross_vest = new_v / max(0.25, net_ret)
                            ytd["agi_ord"] = float(ytd["agi_ord"]) + gross_vest
                            ytd["vest_ord"] = float(ytd.get("vest_ord") or 0.0) + gross_vest
                    else:
                        any_unvested = True
                v_sum += new_v
                b_sum += t["costBasis"]
            st["val"] = v_sum
            st["costBasis"] = b_sum
            st["_has_unvested"] = any_unvested
        elif st["val"] > 0.0:
            old_v = st["val"]
            st["val"] = old_v * (1.0 + r)
            gain = st["val"] - old_v
            growth_gain += gain
            tm_a = a.get("tax_model")
            # DA-A02 / DA-C06 / DA-A07 (User Decision A7 — Ongoing Yield Tax):
            # - `MUNI`: Coupon steps up `costBasis` tax-free, but feeds `ytd["muni_int"]` because IRC §86(b)(2)(B)
            #   includes tax-exempt municipal interest in Social Security provisional income!
            # - `CASH`, `TREASURY` (0% state), `TAXABLE_YIELD`: Tax interest monthly as ordinary income (+ NIIT),
            #   deduct tax from reinvested balance, step up `costBasis` by net reinvested yield, and record in
            #   `monthly_yield_tax` so `monthlyTax` / `cumTax` never under-report ongoing yield taxes.
            # - Equity `LTCG_POOL`: Intentionally omitted here so total return compounds deferred until lot sale.
            if tm_a == "MUNI" and gain > 0.0:
                st["costBasis"] += gain
                if use_prog and ytd is not None:
                    ytd["muni_int"] = float(ytd.get("muni_int") or 0.0) + gain
            elif tm_a in ("CASH", "TREASURY", "TAXABLE_YIELD") and gain > 0.0:
                if use_prog:
                    st_ovr = 0.0 if tm_a == "TREASURY" else None
                    ord_y = _tax_ordinary_increment(
                        state_map, gain, iso=iso, dfl=dfl, apply_niit=True, state_rate_override=st_ovr, extra_penalty=0.0
                    )
                    int_tax = ord_y["tax"]
                    tot_loss_sheltered += ord_y["lossSheltered"]
                    tot_tax_saved += ord_y["taxSaved"]
                    if ytd is not None:
                        ytd["yield_ord"] = float(ytd.get("yield_ord") or 0.0) + max(0.0, gain - ord_y["lossSheltered"])
                else:
                    int_tax = gain * float(st.get("tax_rate") or 0.0)
                st["val"] = max(0.0, st["val"] - int_tax)
                if tm_a == "CASH":
                    st["costBasis"] = st["val"]
                else:
                    st["costBasis"] += max(0.0, gain - int_tax)
                monthly_yield_tax += int_tax
                if record_stats:
                    st["totalTaxPaid"] += int_tax

    # 1b. Required Minimum Distribution (`DA-C01` SECURE 2.0 age 73/75 per owner + `DA-E05` locked IRA contracts + `DA2-05` BEFORE Step 1a-2 SSN tax!)
    rmd_gross = 0.0
    rmd_tax = 0.0
    rmd_net = 0.0
    if iso[5:7] == "01":
        cur_yr_i = int(iso[:4])
        pretax_all = [
            a for a in sim_assets
            if state_map.get(a["id"])
            and str(a.get("tax_wrapper") or "").upper() in {"TRAD_IRA", "401K", "TRAD_401K", "SEP_IRA", "SIMPLE_IRA", "403B"}
        ]
        by_groups: dict[tuple, list[dict[str, Any]]] = {}
        for a in pretax_all:
            st_a = state_map[a["id"]]
            oby = int(
                st_a.get("owner_birth_year")
                or a.get("owner_birth_year")
                or (tc.get("default_birth_year") if tc else _family_defaults()[1])
                or _family_defaults()[1]
            )
            account = a.get("account_id") or a["id"]
            owner = st_a.get("rmd_owner_index")
            if owner is None:
                owner = a.get("rmd_owner_index")
            if owner is None:
                raw_oid = st_a.get("owner_id") or a.get("owner_id")
                owner = raw_oid if (raw_oid is not None and str(raw_oid).strip() != "") else None
            wrapper = a["tax_wrapper"].upper()
            group = ("account", account)
            if owner is not None and wrapper not in {"401K", "TRAD_401K"}:
                group = ("owner", owner, "403B" if wrapper == "403B" else "IRA")
            by_groups.setdefault((oby, group), []).append(a)

        for (oby, _group), owner_buckets in by_groups.items():
            age_reached = cur_yr_i - oby
            req_age = rmd_start_age_for_birth_year(oby)
            div = ULT_DIVISORS.get(min(age_reached, 120)) if age_reached >= req_age else None
            if not div or div <= 0:
                continue
            # Include all owner Traditional IRAs (even surrender-locked IRA annuities) in RMD calculation,
            # then satisfy the aggregate RMD from unlocked IRAs first before touching locked contracts.
            trad_bal = sum(january_opening[a["id"]] for a in owner_buckets)
            remaining = trad_bal / div
            ordered_ir = sorted(owner_buckets, key=lambda x: (1 if _is_bucket_locked(state_map[x["id"]], iso) else 0))
            for a in ordered_ir:
                if remaining <= 0.005:
                    break
                st = state_map[a["id"]]
                if st["val"] <= 0.005:
                    continue
                gross = min(remaining, st["val"])
                frac = min(1.0, gross / st["val"])
                if use_prog:
                    ord_r = _tax_ordinary_increment(
                        state_map, gross, iso=iso, dfl=dfl, apply_niit=False, extra_penalty=0.0
                    )
                    tax = ord_r["tax"]
                    tot_loss_sheltered += ord_r["lossSheltered"]
                    tot_tax_saved += ord_r["taxSaved"]
                    if ytd is not None:
                        ytd["rmd_ord"] = float(ytd.get("rmd_ord") or 0.0) + max(0.0, gross - ord_r["lossSheltered"])
                else:
                    tax = gross * float(st.get("tax_rate") or 0.0)
                net = max(0.0, gross - tax)
                st["val"] = max(0.0, st["val"] - gross)
                st["costBasis"] = max(0.0, st["costBasis"] * (1.0 - frac))
                remaining -= gross
                rmd_gross += gross
                rmd_tax += tax
                rmd_net += net
                if record_stats:
                    if not st["firstTappedLbl"]:
                        st["firstTappedLbl"] = full_lbl
                        st["firstTappedIso"] = iso
                    st["lastTappedLbl"] = full_lbl
                    st["lastTappedIso"] = iso
                    st["totalGrossSold"] += gross
                    st["totalTaxPaid"] += tax

    # 1a-1. Pre-Retirement Work Plan (Ongoing Contributions & Paycheck Burn Offset)
    # PARITY INVARIANT (lockstep with `stepSimMonth` in `app_simulator.js`):
    # - Paycheck burn offsets reduce recurring `target_net_burn` BEFORE Step 1a-2 SSN tax and Step 2 liquidation,
    #   never below `$0`, and never suppress explicit Scheduled Withdrawals / Gifts (`extra_auto_withdraw_net`).
    # - Ongoing work contributions deposit strictly when `ym > start_ym` (`m >= 1`) so Month-0 starting balances
    #   remain exact on Bar 0.
    monthly_work_contrib = 0.0
    monthly_paycheck_offset = 0.0
    wp_cfg = tc.get("work_plan") if isinstance(tc, dict) else None
    if wp_cfg and wp_cfg.get("workers"):
        active_w = [w for w in wp_cfg["workers"] if ym < w["ret_ym"]]
        if active_w:
            if any(w["covers_full_burn"] for w in active_w):
                monthly_paycheck_offset = target_net_burn
                target_net_burn = 0.0
            else:
                raw_off = sum((w["annual_budget_offset"] * dfl) / 12.0 for w in active_w)
                monthly_paycheck_offset = min(target_net_burn, raw_off)
                target_net_burn = max(0.0, target_net_burn - monthly_paycheck_offset)
            if ym > str(wp_cfg.get("start_ym") or ""):
                cur_yr_w = int(iso[:4])
                cal_mo_w = int(iso[5:7])
                for w in active_w:
                    attained_age_w = cur_yr_w - int(w["birth_year"])
                    for crow in w["contributions"]:
                        pacing_mode = str(crow.get("pacing") or "monthly")
                        if pacing_mode == "q1_frontloaded":
                            pace_mult = (1.0 / 3.0) if cal_mo_w in (1, 2, 3) else 0.0
                        else:
                            pace_mult = 1.0 / 12.0
                        if pace_mult <= 0.0:
                            continue
                        emp_base = float(crow["employee_annual"])
                        catchup_w = (
                            IRS_SUPER_CATCHUP_60_63_USD
                            if (60 <= attained_age_w <= 63)
                            else (IRS_CATCHUP_50_USD if attained_age_w >= 50 else 0.0)
                        ) if crow.get("age_50_catchup") else 0.0
                        emp_tot = emp_base + catchup_w
                        if emp_tot > 0.0:
                            emp_mo = (emp_tot * dfl) * pace_mult
                            monthly_work_contrib += _deposit_work_plan_leg(
                                state_map, crow["target_asset_id"], iso, emp_mo, include_unvested, is_employer_match=False
                            )
                        match_base = float(crow["employer_match_annual"])
                        match_rate_pct = float(crow.get("match_rate_pct") or 0.0)
                        if match_rate_pct > 0.0:
                            raw_match = emp_base * (match_rate_pct / 100.0)
                            eff_match = min(match_base, raw_match) if match_base > 0.0 else raw_match
                        else:
                            eff_match = match_base
                        if eff_match > 0.0:
                            match_mo = (eff_match * dfl) * pace_mult
                            match_tid = crow["match_target_asset_id"] or crow["target_asset_id"]
                            monthly_work_contrib += _deposit_work_plan_leg(
                                state_map, match_tid, iso, match_mo, include_unvested, is_employer_match=True
                            )

    # 1a-2. Progressive Social Security Provisional-Income Tax (`DA-C03` + `DA2-05` annualized non-SSN income)
    ssn_tax_mo = 0.0
    ssn_net_mo = 0.0
    if ssn_gross_mo is not None:
        if ssn_gross_mo > 0.005:
            if use_prog:
                est_jan_draw = 0.0
                if iso[5:7] == "01":
                    est_mo_gap = max(0.0, target_net_burn - ssn_gross_mo)
                    est_ann_gap = max(0.0, 12.0 * est_mo_gap - rmd_net)
                    if est_ann_gap > 0.005:
                        for lead_aid in active_order:
                            st_l = state_map.get(lead_aid)
                            if not st_l or st_l["val"] <= 0.005:
                                continue
                            if st_l.get("is_unvested_goog") and not include_unvested:
                                continue
                            if _is_bucket_locked(st_l, iso):
                                continue
                            tm_l = st_l.get("tax_model")
                            if tm_l == "ORDINARY_FULL":
                                est_jan_draw = est_ann_gap
                            elif tm_l == "ANNUITY_LIFO" and st_l["val"] > st_l["costBasis"] + 0.005:
                                est_jan_draw = min(est_ann_gap, float(st_l["val"]) - float(st_l["costBasis"]))
                            elif tm_l not in ("TAX_FREE", "CASH", "MUNI"):
                                gr_l = max(0.0, (float(st_l["val"]) - float(st_l["costBasis"])) / float(st_l["val"]))
                                est_jan_draw = est_ann_gap * gr_l
                            break
                ssn_tax_mo, ssn_net_mo = _compute_progressive_ssn_mo(state_map, ssn_gross_mo, iso, dfl, est_jan_draw)
            else:
                ssn_net_mo = ssn_gross_mo
        target_net_burn = max(0.0, target_net_burn - ssn_net_mo)

    # 1c. Unified Scheduled Actions (Deposits, Transfers, Withdrawals)
    tot_gross_sold = 0.0
    tot_net_proceeds = 0.0
    tot_tax_paid = 0.0
    tot_loss_harvested = 0.0

    extra_auto_withdraw_net = 0.0
    sched_withdraw_nom = 0.0
    sched_withdraw_real = 0.0
    sched_deposit_nom = 0.0
    sched_transfer_nom = 0.0
    action_events: list[dict[str, Any]] = []

    if include_scheduled_actions and scheduled_actions:
        for idx, act in enumerate(scheduled_actions):
            act_type = act.get("type") or "withdrawal"
            cad = act.get("cadence") or "one_time"
            from_id = str(act.get("from_asset_id") or "")
            to_id = str(act.get("to_asset_id") or "")
            vest_amt = vested_by_asset.get(from_id, 0.0)
            if not _action_triggers_in_ym(act, ym, vested_this_month=(vest_amt > 0.005)):
                continue

            if act_type == "deposit":
                to_st = state_map.get(to_id)
                if not to_st or (to_st.get("is_unvested_goog") and not include_unvested):
                    continue
                raw_amt = float(act.get("amount") or 0.0)
                if raw_amt <= 0:
                    continue
                nom_amt = raw_amt if act.get("dollar_mode") == "nominal" else raw_amt * dfl
                today_amt = nom_amt / dfl
                if to_st.get("tranches") is not None and to_st.get("tax_model") == "LTCG_TRANCHES":
                    to_st["tranches"].append({
                        "vestIso": iso,
                        "shares": 0.0,
                        "vested": True,
                        "val": nom_amt,
                        "costBasis": nom_amt,
                    })
                    to_st["val"] = sum(t["val"] for t in to_st["tranches"])
                    to_st["costBasis"] = sum(t["costBasis"] for t in to_st["tranches"])
                else:
                    to_st["val"] += nom_amt
                    if (to_st.get("tax_model") or to_st.get("taxModel")) != "ORDINARY_FULL":
                        to_st["costBasis"] += nom_amt
                sched_deposit_nom += nom_amt
                action_events.append({
                    **act,
                    "rowNum": idx + 1,
                    "ym": ym,
                    "nomAmt": nom_amt,
                    "todayAmt": today_amt,
                    "netAmt": nom_amt,
                    "taxPaid": 0.0,
                })

            elif act_type == "transfer":
                from_st = state_map.get(from_id)
                to_st = state_map.get(to_id)
                if not from_st or not to_st or from_st is to_st:
                    continue
                if (from_st.get("is_unvested_goog") and not include_unvested) or _is_bucket_locked(from_st, iso):
                    continue
                from_tm = str(from_st.get("tax_model") or "")
                to_tm = str(to_st.get("tax_model") or "")
                is_same_wrapper_rollover = (from_tm == to_tm) and (from_tm in ("ORDINARY_FULL", "TAX_FREE", "ANNUITY_LIFO"))
                is_roth_conv = (from_tm == "ORDINARY_FULL") and (to_tm == "TAX_FREE")
                if cad == "on_vest":
                    avail_base = vest_amt
                    if avail_base <= 0.005:
                        continue
                    if act.get("amount_mode") == "pct_of_source":
                        gross_target = avail_base * (float(act.get("amount") or 0.0) / 100.0)
                    else:
                        nom_req = float(act["amount"]) if act.get("dollar_mode") == "nominal" else float(act["amount"]) * dfl
                        gross_target = min(avail_base, nom_req)
                    newly_ym_arg: str | None = ym
                else:
                    if from_st.get("is_unvested_goog") and from_st.get("tranches"):
                        avail_base = sum(float(t["val"]) for t in from_st["tranches"] if t["vested"])
                    else:
                        avail_base = float(from_st["val"])
                    if avail_base <= 0.005:
                        continue
                    if act.get("amount_mode") == "pct_of_source":
                        gross_target = avail_base * (float(act.get("amount") or 0.0) / 100.0)
                    else:
                        nom_req = float(act["amount"]) if act.get("dollar_mode") == "nominal" else float(act["amount"]) * dfl
                        gross_target = min(avail_base, nom_req)
                    newly_ym_arg = None

                # DA2-09 (Statutory Rollover & Conversion Invariants — lockstep with `stepSimMonth` in `app_simulator.js`):
                # 1. Trustee-to-trustee IRA->IRA, Roth->Roth, and §1035 Annuity->Annuity transfers move `0%` tax and
                #    carry over original `costBasis` (NEVER treated as taxable distributions or stepped-up basis).
                # 2. Direct pre-59.5 `ORDINARY_FULL -> TAX_FREE` Roth conversions are exempt from the +10% early
                #    distribution penalty on converted principal under IRC §408A(d)(3)(C); only cash withheld from
                #    the IRA to pay tax (`pay_tax_from_proceeds=True` while pre-59.5) incurs the 10% penalty.
                if is_same_wrapper_rollover:
                    gross_moved = min(gross_target, float(from_st["val"]))
                    if gross_moved > 0.01:
                        _inherit_rollover_owner_if_unset(from_st, to_st)
                        frac_mv = min(1.0, gross_moved / float(from_st["val"]))
                        basis_mv = float(from_st["costBasis"]) * frac_mv
                        from_st["val"] = max(0.0, float(from_st["val"]) - gross_moved)
                        from_st["costBasis"] = max(0.0, float(from_st["costBasis"]) - basis_mv)
                        to_st["val"] += gross_moved
                        to_st["costBasis"] += basis_mv
                        if record_stats:
                            _record_bucket_tap_stats(from_st, iso, full_lbl, gross_moved, gross_moved, 0.0, 0.0, 0.0, 0.0)
                        sched_transfer_nom += gross_moved
                        action_events.append({
                            **act,
                            "rowNum": idx + 1,
                            "ym": ym,
                            "nomAmt": gross_moved,
                            "todayAmt": gross_moved / dfl,
                            "netAmt": gross_moved,
                            "taxPaid": 0.0,
                        })
                    continue

                liq = _liquidate_gross_from_bucket(
                    from_st,
                    gross_target,
                    state_map,
                    iso,
                    full_lbl,
                    record_stats,
                    newly_vested_only_ym=newly_ym_arg,
                    dfl=dfl,
                    suppress_early_penalty=is_roth_conv,
                )
                if (
                    is_roth_conv
                    and act.get("pay_tax_from_proceeds", True) is not False
                    and use_prog
                    and _owner_is_pre_59_half(from_st, state_map, iso)
                    and liq["taxPaid"] > 0.005
                ):
                    pen_wh = min(liq["netProceeds"], liq["taxPaid"] * 0.10)
                    liq["taxPaid"] += pen_wh
                    liq["netProceeds"] -= pen_wh
                    if record_stats:
                        from_st["totalTaxPaid"] += pen_wh
                        from_st["totalNetProceeds"] = max(0.0, from_st["totalNetProceeds"] - pen_wh)

                if liq["grossSold"] > 0.01:
                    _inherit_rollover_owner_if_unset(from_st, to_st)
                    net_in = liq["netProceeds"]
                    if to_st.get("tranches") is not None and to_st.get("tax_model") == "LTCG_TRANCHES":
                        to_st["tranches"].append({
                            "vestIso": iso,
                            "shares": 0.0,
                            "vested": True,
                            "val": net_in,
                            "costBasis": net_in,
                        })
                        to_st["val"] = sum(t["val"] for t in to_st["tranches"])
                        to_st["costBasis"] = sum(t["costBasis"] for t in to_st["tranches"])
                    else:
                        to_st["val"] += net_in
                        to_st["costBasis"] += net_in
                    tot_gross_sold += liq["grossSold"]
                    tot_tax_paid += liq["taxPaid"]
                    tot_loss_harvested += liq["lossHarvested"]
                    tot_loss_sheltered += liq["lossSheltered"]
                    tot_tax_saved += liq["taxSaved"]
                    sched_transfer_nom += liq["grossSold"]
                    action_events.append({
                        **act,
                        "rowNum": idx + 1,
                        "ym": ym,
                        "nomAmt": liq["grossSold"],
                        "todayAmt": liq["grossSold"] / dfl,
                        "netAmt": net_in,
                        "taxPaid": liq["taxPaid"],
                    })

            else:  # withdrawal
                if act.get("amount_mode") == "pct_of_source" and from_id and from_id != "auto":
                    src_st = state_map.get(from_id)
                    src_val = float(src_st["val"]) if (src_st and not _is_bucket_locked(src_st, iso)) else 0.0
                    nom_amt = src_val * (float(act.get("amount") or 0.0) / 100.0)
                else:
                    raw_amt = float(act.get("amount") or 0.0)
                    nom_amt = raw_amt if act.get("dollar_mode") == "nominal" else raw_amt * dfl
                if nom_amt <= 0.005:
                    continue
                today_amt = nom_amt / dfl
                sched_withdraw_nom += nom_amt
                sched_withdraw_real += today_amt

                got_net = 0.0
                direct_tax = 0.0
                if from_id and from_id != "auto":
                    src_st = state_map.get(from_id)
                    if src_st and not (src_st.get("is_unvested_goog") and not include_unvested) and not _is_bucket_locked(src_st, iso):
                        liq = _liquidate_net_from_bucket(src_st, nom_amt, state_map, iso, full_lbl, record_stats, dfl=dfl)
                        got_net = liq["netProceeds"]
                        direct_tax = liq["taxPaid"]
                        tot_gross_sold += liq["grossSold"]
                        tot_net_proceeds += liq["netProceeds"]
                        tot_tax_paid += liq["taxPaid"]
                        tot_loss_harvested += liq["lossHarvested"]
                        tot_loss_sheltered += liq["lossSheltered"]
                        tot_tax_saved += liq["taxSaved"]
                    if act.get("amount_mode") != "pct_of_source" and nom_amt - got_net > 0.005:
                        extra_auto_withdraw_net += nom_amt - got_net
                else:
                    extra_auto_withdraw_net += nom_amt

                action_events.append({
                    **act,
                    "rowNum": idx + 1,
                    "ym": ym,
                    "nomAmt": nom_amt,
                    "todayAmt": today_amt,
                    "netAmt": got_net,
                    "taxPaid": direct_tax,
                })

    # 2. Conservation of January RMD Cash (`DA-A01` — CRITICAL INVARIANT):
    # Mandatory after-tax RMD cash (`rmd_net`) MUST offset this month's net spending need FIRST before
    # selling any other taxable bucket (avoiding double taxation), and any surplus (`rmd_surplus`) MUST
    # be deposited into a taxable `CASH` sink (guaranteed by `materialize_sim_assets`) or fallback taxable
    # bucket — NEVER dropped when a portfolio lacks an ingested cash sweep!
    direct_event_net = tot_net_proceeds
    before_pool_tax = tot_tax_paid
    effective_target_net_burn = target_net_burn + extra_auto_withdraw_net
    rmd_used_for_burn = min(rmd_net, effective_target_net_burn)
    rem_net_needed = effective_target_net_burn - rmd_used_for_burn
    tot_net_proceeds += rmd_used_for_burn
    rmd_surplus = rmd_net - rmd_used_for_burn
    if rmd_surplus > 0.005:
        cash_st = next(
            (
                state_map[a["id"]]
                for a in sim_assets
                if state_map.get(a["id"])
                and a.get("tax_model") == "CASH"
                and str(a.get("tax_wrapper") or "TAXABLE").upper() == "TAXABLE"
            ),
            None,
        )
        if cash_st is None:
            cash_st = next(
                (
                    state_map[a["id"]]
                    for a in sim_assets
                    if state_map.get(a["id"])
                    and str(a.get("tax_wrapper") or "TAXABLE").upper() == "TAXABLE"
                    and not a.get("is_unvested_goog")
                    and not _is_bucket_locked(state_map[a["id"]], iso)
                ),
                None,
            )
        if cash_st is not None:
            if cash_st.get("tranches") and cash_st.get("tax_model") == "LTCG_TRANCHES":
                cash_st["tranches"].append({"vestIso": iso, "shares": 0.0, "vested": True, "val": rmd_surplus, "costBasis": rmd_surplus})
                cash_st["val"] = sum(t["val"] for t in cash_st["tranches"])
                cash_st["costBasis"] = sum(t["costBasis"] for t in cash_st["tranches"])
            else:
                cash_st["val"] += rmd_surplus
                cash_st["costBasis"] += rmd_surplus

    for asset_id in active_order:
        if rem_net_needed <= 0.005:
            break
        st = state_map.get(asset_id)
        if not st or st["val"] <= 0.005:
            continue
        if st.get("is_unvested_goog") and not include_unvested:
            continue
        if _is_bucket_locked(st, iso):
            continue

        liq = _liquidate_net_from_bucket(st, rem_net_needed, state_map, iso, full_lbl, record_stats, dfl=dfl)
        if liq["grossSold"] > 0.01:
            rem_net_needed -= liq["netProceeds"]
            tot_gross_sold += liq["grossSold"]
            tot_net_proceeds += liq["netProceeds"]
            tot_tax_paid += liq["taxPaid"]
            tot_loss_harvested += liq["lossHarvested"]
            tot_loss_sheltered += liq["lossSheltered"]
            tot_tax_saved += liq["taxSaved"]

    # DA2-09: When ALL unlocked buckets are exhausted (`rem_net_needed > 0.005`), tap surrender-locked
    # annuities via their contractual 10%/yr penalty-free corridor first, then emergency surrender at
    # `surrender_value` (or 7% default surrender fee); any remaining `unmetNetBurn > $1` flags insolvency.
    if rem_net_needed > 0.005:
        cur_yr_ann = int(iso[:4])
        for asset_id in active_order:
            if rem_net_needed <= 0.005:
                break
            st = state_map.get(asset_id)
            if not st or st["val"] <= 0.005 or not _is_bucket_locked(st, iso):
                continue
            is_ann = (
                st.get("tax_model") in ("ANNUITY_LIFO", "ORDINARY_FULL")
                and (float(st.get("free_withdrawal_pct") or 0.0) > 0.0 or str(st.get("id") or "").startswith("ann_"))
            )
            if not is_ann:
                continue
            if st.get("_annFreeYr") != cur_yr_ann:
                free_pct = float(st.get("free_withdrawal_pct") or 10.0)
                st["_annFreeYr"] = cur_yr_ann
                st["_annFreeRem"] = float(st["val"]) * (free_pct / 100.0)
            avail_free = float(st.get("_annFreeRem") or 0.0)
            if avail_free <= 0.005:
                continue
            liq_f = _liquidate_locked_annuity_corridor(
                st, rem_net_needed, avail_free, state_map, iso, full_lbl, record_stats, dfl=dfl, surr_fee_rate=0.0
            )
            if liq_f["grossSold"] > 0.01:
                st["_annFreeRem"] = max(0.0, avail_free - liq_f["grossSold"])
                rem_net_needed -= liq_f["netProceeds"]
                tot_gross_sold += liq_f["grossSold"]
                tot_net_proceeds += liq_f["netProceeds"]
                tot_tax_paid += liq_f["taxPaid"]
                tot_loss_sheltered += liq_f["lossSheltered"]
                tot_tax_saved += liq_f["taxSaved"]

    if rem_net_needed > 0.005:
        for asset_id in active_order:
            if rem_net_needed <= 0.005:
                break
            st = state_map.get(asset_id)
            if not st or st["val"] <= 0.005 or not _is_bucket_locked(st, iso):
                continue
            is_surr_ann = (
                st.get("tax_model") in ("ANNUITY_LIFO", "ORDINARY_FULL")
                and (st.get("surrender_value") is not None or float(st.get("free_withdrawal_pct") or 0.0) > 0.0 or str(st.get("id") or "").startswith("ann_"))
            )
            if not is_surr_ann:
                continue
            if st.get("surrender_value") is not None and float(st.get("current_value") or 0.0) > 0.0:
                surr_keep = max(0.75, min(1.0, float(st["surrender_value"]) / float(st["current_value"])))
            else:
                surr_keep = 0.93
            liq_s = _liquidate_locked_annuity_corridor(
                st, rem_net_needed, float(st["val"]), state_map, iso, full_lbl, record_stats, dfl=dfl, surr_fee_rate=max(0.0, 1.0 - surr_keep)
            )
            if liq_s["grossSold"] > 0.01:
                rem_net_needed -= liq_s["netProceeds"]
                tot_gross_sold += liq_s["grossSold"]
                tot_net_proceeds += liq_s["netProceeds"]
                tot_tax_paid += liq_s["taxPaid"]
                tot_loss_sheltered += liq_s["lossSheltered"]
                tot_tax_saved += liq_s["taxSaved"]

    if action_events:
        # Allocate pooled proceeds for reporting in spending order. Direct debits stay with their event.
        rmd_for_living = min(rmd_used_for_burn, target_net_burn)
        rmd_for_events = max(0.0, rmd_used_for_burn - rmd_for_living)
        pool_net = max(0.0, tot_net_proceeds - direct_event_net)
        remaining_pool = max(0.0, pool_net - target_net_burn)
        initial_event_pool = remaining_pool
        waterfall_net_total = max(0.0, tot_net_proceeds - direct_event_net - rmd_used_for_burn)
        waterfall_for_living = max(0.0, target_net_burn - rmd_for_living)
        waterfall_for_events = max(0.0, waterfall_net_total - waterfall_for_living)
        waterfall_tax_total = max(0.0, tot_tax_paid - before_pool_tax)
        event_pool_tax = (
            (rmd_tax * (rmd_for_events / rmd_net) if rmd_net > 0 else 0.0)
            + (waterfall_tax_total * (waterfall_for_events / waterfall_net_total) if waterfall_net_total > 0 else 0.0)
        )
        for event in action_events:
            if event.get("type", "withdrawal") != "withdrawal":
                continue
            need = max(0.0, event["nomAmt"] - event["netAmt"])
            source_only = event.get("amount_mode") == "pct_of_source" and event.get("from_asset_id") not in (None, "", "auto")
            pooled = 0.0 if source_only else min(need, remaining_pool)
            remaining_pool -= pooled
            event["netAmt"] += pooled
            event["pooledNet"] = pooled
            event["shortfall"] = need - pooled
            if initial_event_pool > 0:
                event["taxPaid"] += event_pool_tax * (pooled / initial_event_pool)

    total_target_burn = target_net_burn + sched_withdraw_nom
    return {
        "growthGain": growth_gain,
        "monthlyBurn": tot_net_proceeds,
        "targetMonthlyBurn": total_target_burn,
        "unmetNetBurn": max(0.0, total_target_burn - tot_net_proceeds),
        "monthlyTax": tot_tax_paid + rmd_tax + monthly_yield_tax,
        "monthlyGrossSold": tot_gross_sold + rmd_gross,
        "monthlyRmdGross": rmd_gross,
        "monthlyRmdTax": rmd_tax,
        "ssnTaxMo": ssn_tax_mo,
        "ssnNetMo": ssn_net_mo,
        "monthlyWorkContrib": monthly_work_contrib,
        "monthlyPaycheckOffset": monthly_paycheck_offset,
        "monthlyLossHarvested": tot_loss_harvested,
        "monthlyLossSheltered": tot_loss_sheltered,
        "monthlyTaxSaved": tot_tax_saved,
        "schedWithdrawNom": sched_withdraw_nom,
        "schedWithdrawReal": sched_withdraw_real,
        "schedDepositNom": sched_deposit_nom,
        "schedTransferNom": sched_transfer_nom,
        "schedActionEvents": action_events,
    }


def _summarize_state_buckets(
    sim_assets: list[dict[str, Any]],
    state_map: dict[str, Any],
    include_unvested: bool,
) -> dict[str, Any]:
    goog_vested_val = 0.0
    goog_unvested_val = 0.0
    non_goog_val = 0.0
    total_basis = 0.0
    asset_vals: dict[str, float] = {}

    for a in sim_assets:
        aid = a["id"]
        if a.get("is_unvested_goog") and not include_unvested:
            asset_vals[aid] = 0.0
            continue
        st = state_map.get(aid)
        if not st:
            asset_vals[aid] = 0.0
            continue
        # DA2-11 INVARIANT (lockstep with `summarizeSimStateBuckets` in `app_simulator.js`):
        # `assetVals[aid]` MUST record ONLY vested spendable value so `sum(assetVals.values()) == totalVal`
        # at every month; unvested RSU value is tracked separately in `googUnvestedVal`.
        if a.get("is_unvested_goog") and st.get("tranches"):
            v_only = 0.0
            u_only = 0.0
            b_v_only = 0.0
            for t in st["tranches"]:
                if t["vested"]:
                    v_only += float(t["val"])
                    b_v_only += float(t["costBasis"])
                else:
                    u_only += float(t["val"])
            asset_vals[aid] = v_only
            total_basis += b_v_only
            if a.get("bucket") == "goog":
                goog_vested_val += v_only
                goog_unvested_val += u_only
            else:
                non_goog_val += v_only
        else:
            asset_vals[aid] = float(st["val"])
            total_basis += float(st["costBasis"])
            if a.get("bucket") == "goog":
                goog_vested_val += float(st["val"])
            else:
                non_goog_val += float(st["val"])

    goog_val = goog_vested_val + goog_unvested_val
    # CodeReview A8: unvested GSUs grow in the background but are not counted as spendable
    # net worth until they vest (matching the Timeline screen). Milestones at/after the primary
    # investor's milestone age are unaffected since all tranches vest by 2029.
    total_val = goog_vested_val + non_goog_val
    return {
        "googVestedVal": goog_vested_val,
        "googUnvestedVal": goog_unvested_val,
        "googVal": goog_val,
        "nonGoogVal": non_goog_val,
        "totalVal": total_val,
        "totalBasis": total_basis,
        "assetVals": asset_vals,
    }


def _compute_monthly_scheduled_withdrawals(
    ym: str,
    deflator: float,
    withdrawals: list[dict[str, Any]],
    include_withdrawals: bool,
) -> dict[str, Any]:
    if not include_withdrawals or not withdrawals:
        return {"totNom": 0.0, "totReal": 0.0, "events": []}
    d = deflator if deflator > 0 else 1.0
    tot_nom = 0.0
    tot_real = 0.0
    events = []
    for idx, ev in enumerate(withdrawals):
        if ev.get("ym") == ym:
            raw_amt = float(ev.get("amount") or 0.0)
            if raw_amt <= 0:
                continue
            if ev.get("dollar_mode") == "nominal":
                nom_amt = raw_amt
                today_amt = raw_amt / d
            else:
                today_amt = raw_amt
                nom_amt = raw_amt * d
            tot_nom += nom_amt
            tot_real += today_amt
            events.append(
                {
                    **ev,
                    "rowNum": idx + 1,
                    "nomAmt": nom_amt,
                    "todayAmt": today_amt,
                }
            )
    return {"totNom": tot_nom, "totReal": tot_real, "events": events}


def apply_hypothetical_rollovers(
    sim_assets: list[dict[str, Any]],
    rollovers: list[dict[str, Any]] | None,
    *,
    payload: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], float, list[dict[str, Any]]]:
    """
    Applies hypothetical Day-0 asset rollovers (`DA2-04`):
      - Supports both full (`sell_all=True`) and partial (`amount=...`, `sell_all=False`) sales.
      - Trustee-to-trustee same-wrapper rollovers (`ORDINARY_FULL -> ORDINARY_FULL`, `TAX_FREE -> TAX_FREE`,
        `ANNUITY_LIFO -> ANNUITY_LIFO` §1035) incur `0%` immediate tax and carry over proportional `cost_basis`.
      - `ORDINARY_FULL -> TAX_FREE` (Roth conversion) or taxable bucket taxes **100% of `gross_val`** (never just
        `gross - basis`) via the progressive ordinary schedule, honoring IRC §408A(d)(3)(C) pre-59.5 conversion
        principal exemption (applying +10% early penalty only to Non-Roth distributions or tax withheld from IRA).
      - `LTCG_*` buckets use progressive `<365d` STCG / `>=365d` LTCG brackets and `_lossCarryforward` sheltering.
    Returns `(modified_sim_assets, initial_loss_carryforward, trade_audit_list)`.
    """
    assets_copy = copy.deepcopy(sim_assets)
    if not rollovers:
        return assets_copy, 0.0, []

    today_iso = str((payload or {}).get("today_iso") or date.today().isoformat())
    tax_cfg = build_tax_schedule_cfg(payload or {"retirement_sim_assets": assets_copy, "today_iso": today_iso}, settings or {})
    day0_state: dict[str, Any] = {
        "_lossCarryforward": 0.0,
        "_totalLossHarvested": 0.0,
        "_totalLossUsed": 0.0,
        "_totalTaxSavedByLoss": 0.0,
        "_taxCfg": tax_cfg,
    }
    _ensure_tax_ytd(day0_state, today_iso, 1.0)

    by_id = {a["id"]: a for a in assets_copy}
    audit_log: list[dict[str, Any]] = []

    for tr in rollovers:
        from_id = str(tr.get("from_id") or tr.get("from_asset_id") or tr.get("from") or "").strip()
        to_id = str(tr.get("to_id") or tr.get("to_asset_id") or tr.get("to") or "").strip()
        deduct_tax = bool(tr.get("deduct_tax", tr.get("pay_tax_from_proceeds", True)))
        if from_id not in by_id or to_id not in by_id:
            raise ValueError(f"Invalid rollover asset IDs: from_id={from_id!r}, to_id={to_id!r}")
        src = by_id[from_id]
        dst = by_id[to_id]

        cur_val = float(src.get("current_value") or 0.0)
        cur_basis = float(src.get("cost_basis") or 0.0)
        raw_amt = tr.get("amount")
        sell_all = bool(tr.get("sell_all", raw_amt is None))
        if sell_all:
            gross_val = cur_val
        else:
            gross_val = min(cur_val, max(0.0, float(raw_amt or 0.0)))
        frac_sold = (gross_val / cur_val) if cur_val > 1e-9 else 0.0
        basis_val = cur_basis * frac_sold

        src_tm = str(src.get("tax_model") or "LTCG_POOL")
        dst_tm = str(dst.get("tax_model") or "LTCG_POOL")
        is_pre_595 = _owner_is_pre_59_half(src, day0_state, today_iso)
        tax_incurred = 0.0
        loss_harvested = 0.0
        realized_gain_or_loss = gross_val - basis_val
        is_same_wrapper = (src_tm == dst_tm) and (src_tm in ("ORDINARY_FULL", "TAX_FREE", "ANNUITY_LIFO"))

        if is_same_wrapper or gross_val <= 0.005:
            realized_gain_or_loss = 0.0
            tax_incurred = 0.0
        elif src_tm == "ORDINARY_FULL":
            # 100% of Traditional IRA / 401(k) rollover to Roth or taxable is ordinary income (`DA2-04`)
            realized_gain_or_loss = gross_val
            is_roth_conv = dst_tm == "TAX_FREE"
            early_p = 0.0 if is_roth_conv else (0.10 if is_pre_595 else 0.0)
            ord_res = _tax_ordinary_increment(
                day0_state, gross_val, iso=today_iso, dfl=1.0, apply_niit=False, extra_penalty=early_p
            )
            tax_incurred = ord_res["tax"]
            if is_roth_conv and deduct_tax and is_pre_595 and tax_incurred > 0.005:
                tax_incurred = min(gross_val, tax_incurred + min(max(0.0, gross_val - tax_incurred), tax_incurred * 0.10))
        elif src_tm == "ANNUITY_LIFO":
            earn_avail = max(0.0, cur_val - cur_basis)
            earn_sold = min(gross_val, earn_avail)
            basis_val = max(0.0, gross_val - earn_sold)
            realized_gain_or_loss = earn_sold
            if earn_sold > 0.005:
                ord_res = _tax_ordinary_increment(
                    day0_state, earn_sold, iso=today_iso, dfl=1.0, apply_niit=True, extra_penalty=(0.10 if is_pre_595 else 0.0)
                )
                tax_incurred = ord_res["tax"]
        elif src_tm == "TAX_FREE":
            contrib_rec = min(gross_val, cur_basis)
            basis_val = contrib_rec
            earn_sold = max(0.0, gross_val - contrib_rec)
            realized_gain_or_loss = earn_sold if is_pre_595 else 0.0
            if is_pre_595 and earn_sold > 0.005:
                ord_res = _tax_ordinary_increment(
                    day0_state, earn_sold, iso=today_iso, dfl=1.0, apply_niit=False, extra_penalty=0.10
                )
                tax_incurred = ord_res["tax"]
        elif src.get("tranches"):
            trs_work = [
                {
                    "vestIso": t.get("vest_date") or t.get("vestIso") or "",
                    "shares": float(t.get("shares") or 0.0),
                    "vested": bool(t.get("vested", True)),
                    "val": float(t.get("current_value") if t.get("current_value") is not None else t.get("val") or 0.0),
                    "costBasis": float(t.get("cost_basis") if t.get("cost_basis") is not None else t.get("costBasis") or 0.0),
                    "_orig": t,
                }
                for t in (src.get("tranches") or [])
            ]
            avail_t = [t for t in trs_work if t["vested"] and t["val"] > 0.005]
            avail_t.sort(key=lambda t: _lot_sort_key(t, today_iso, True))
            rem_g = gross_val
            basis_acc = 0.0
            for tw in avail_t:
                if rem_g <= 0.005:
                    break
                old_tv, old_tb = tw["val"], tw["costBasis"]
                lr = _sell_capital_gain_lot_gross(
                    tw, rem_g, float(src.get("tax_rate") or 0.0), day0_state,
                    iso=today_iso, dfl=1.0, apply_state=(src_tm != "TREASURY"),
                )
                rem_g -= lr["grossSold"]
                basis_acc += max(0.0, old_tb - tw["costBasis"])
                tax_incurred += lr["taxPaid"]
                loss_harvested += lr["lossHarvested"]
                tw["_orig"]["current_value"] = round(tw["val"], 2)
                tw["_orig"]["cost_basis"] = round(tw["costBasis"], 2)
            basis_val = basis_acc
            realized_gain_or_loss = gross_val - basis_val
        else:
            if realized_gain_or_loss < -0.005:
                loss_harvested = abs(realized_gain_or_loss)
                day0_state["_lossCarryforward"] = float(day0_state.get("_lossCarryforward") or 0.0) + loss_harvested
            elif realized_gain_or_loss > 0.005 and src_tm not in ("CASH", "MUNI"):
                avail_l = float(day0_state.get("_lossCarryforward") or 0.0)
                shelt = min(avail_l, realized_gain_or_loss)
                day0_state["_lossCarryforward"] = max(0.0, avail_l - shelt)
                taxable_g = max(0.0, realized_gain_or_loss - shelt)
                if taxable_g > 0.005:
                    tax_incurred = _tax_ltcg_increment(
                        day0_state, taxable_g, iso=today_iso, dfl=1.0, apply_state=(src_tm != "TREASURY"), commit=True
                    )

        tax_deducted = tax_incurred if deduct_tax else 0.0
        net_reinvested = max(0.0, gross_val - tax_deducted)

        # Update source position (full zeroing vs partial sale)
        if sell_all or (cur_val - gross_val) <= 0.005:
            src["current_value"] = 0.0
            src["cost_basis"] = 0.0
            src["tranches"] = []
        else:
            if src.get("tranches"):
                src["tranches"] = [
                    t for t in src["tranches"]
                    if float(t.get("current_value") if t.get("current_value") is not None else t.get("val") or 0.0) > 0.005
                ]
                src["current_value"] = round(sum(float(t.get("current_value") or 0.0) for t in src["tranches"]), 2)
                src["cost_basis"] = round(sum(float(t.get("cost_basis") or 0.0) for t in src["tranches"]), 2)
            else:
                src["current_value"] = round(max(0.0, cur_val - gross_val), 2)
                src["cost_basis"] = round(max(0.0, cur_basis - basis_val), 2)

        # Update destination position (carry over basis for same-wrapper rollovers; step up basis otherwise)
        _inherit_rollover_owner_if_unset(src, dst)
        dst_basis_add = basis_val if is_same_wrapper and dst_tm != "ORDINARY_FULL" else (basis_val if is_same_wrapper else (0.0 if dst_tm == "ORDINARY_FULL" else net_reinvested))
        dst["current_value"] = round(float(dst.get("current_value") or 0.0) + net_reinvested, 2)
        dst["cost_basis"] = round(float(dst.get("cost_basis") or 0.0) + dst_basis_add, 2)

        audit_log.append(
            {
                "from_id": from_id,
                "from_title": src.get("short_title") or src.get("title"),
                "to_id": to_id,
                "to_title": dst.get("short_title") or dst.get("title"),
                "gross_sold_today": round(gross_val, 2),
                "cost_basis_sold": round(basis_val, 2),
                "realized_gain_or_loss": round(realized_gain_or_loss, 2),
                "immediate_tax_paid": round(tax_incurred, 2),
                "tax_deducted_from_proceeds": round(tax_deducted, 2),
                "capital_loss_harvested": round(loss_harvested, 2),
                "net_rolled_into_dest": round(net_reinvested, 2),
                "_day0_ytd": copy.deepcopy(day0_state.get("_taxYtd")),
            }
        )

    return assets_copy, float(day0_state.get("_lossCarryforward") or 0.0), audit_log


def _compute_uma_historical_rates(
    today_iso: str,
    current_uma_val: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Computes empirical annualized return estimates for a Direct-Indexing / Equity SMA basket
    from the portfolio SQLite database and statement / config basis metadata:
      1. `ytd_statement`: Money-weighted annualized return solved from the account's
         configured cash flows or statement cost basis to `current_uma_val`.
      2. `matched_window`: Constant-share basket price CAGR over the comparison holding window.
      3. `full_history`: Constant-share basket price CAGR over the full database price history.
    """
    from config import get_default_db_path
    from db.database import PortfolioDB
    from exporters.view_model import solve_annualized_return

    # PORTABILITY DO: Read managed-sleeve cash flows, basis, and historical yields from `project.json` or SQLite (`LL-07`, `LL-18`).
    # PORTABILITY DON'T: Hardcode dated household contribution schedules or fixed yield literals in `simulator.py`.
    end_iso = today_iso[:10]
    proj_cfg = (payload or {}).get("project") if isinstance((payload or {}).get("project"), dict) else {}
    uma_asset = next(
        (a for a in ((payload or {}).get("retirement_sim_assets") or []) if str(a.get("id") or "").startswith("uma_")),
        {},
    )
    uma_default_gp = float(uma_asset.get("default_growth_pct") or 0.0)
    uma_basis = float(uma_asset.get("cost_basis") or 0.0)
    uma_cashflows: list[tuple[str, float]] = []
    for acct_meta in (proj_cfg.get("accounts") or {}).values():
        if isinstance(acct_meta, dict) and acct_meta.get("uma_rollup_basis"):
            if not uma_basis:
                uma_basis = float(acct_meta["uma_rollup_basis"])
            raw_cfs = acct_meta.get("uma_cashflows")
            if isinstance(raw_cfs, list):
                for cf in raw_cfs:
                    if isinstance(cf, (list, tuple)) and len(cf) >= 2:
                        uma_cashflows.append((str(cf[0]), float(cf[1])))
                    elif isinstance(cf, dict) and cf.get("date") and cf.get("amount") is not None:
                        uma_cashflows.append((str(cf["date"]), float(cf["amount"])))
            break

    if not uma_cashflows and uma_basis > 0:
        basis_sub = str(uma_asset.get("basis_sub") or "")
        m_dep = re.search(r"\+\s*\$([\d,.]+)\s*([KkMm])?\s*deposit", basis_sub, flags=re.IGNORECASE)
        if m_dep:
            dep_val = float(m_dep.group(1).replace(",", ""))
            mult = 1_000.0 if (m_dep.group(2) or "").upper() == "K" else (1_000_000.0 if (m_dep.group(2) or "").upper() == "M" else 1.0)
            dep_amt = dep_val * mult
            if 0 < dep_amt < uma_basis:
                uma_cashflows = [(f"{end_iso[:4]}-01-01", round(uma_basis - dep_amt, 2)), (f"{end_iso[:4]}-06-01", round(dep_amt, 2))]
        if not uma_cashflows:
            uma_cashflows = [(f"{end_iso[:4]}-01-01", uma_basis)]

    ytd_rate: float | None = None
    if uma_cashflows and current_uma_val > 0:
        ytd_rate, _ = solve_annualized_return(uma_cashflows, end_iso, current_uma_val)

    p_overrides = proj_cfg.get("principal_overrides") or {}
    fund_dates = sorted(
        str(v.get("inception_date"))
        for k, v in p_overrides.items()
        if isinstance(v, dict) and v.get("inception_date") and "TICKER:" in str(k)
    )
    matched_start_iso = fund_dates[-1] if fund_dates else str((proj_cfg.get("historical_defaults") or {}).get("default_start_date") or "2024-01-01")
    full_start_iso = str((proj_cfg.get("historical_defaults") or {}).get("default_start_date") or "2024-01-01")

    matched_rate = uma_default_gp
    matched_start_val = round(uma_basis, 2)
    full_rate = uma_default_gp
    full_start_val = round(uma_basis, 2)
    stock_count = len(uma_asset.get("constituent_forecasts") or [])

    db_path = get_default_db_path()
    try:
        if db_path is None or not db_path.exists():
            raise FileNotFoundError("No active portfolio DB")
        with PortfolioDB(db_path) as db:
            conn = db.conn
            snap_row = conn.execute("SELECT MAX(as_of_date) FROM position_snapshots").fetchone()
            snap = snap_row[0] if snap_row else None
            uma_acct_row = (
                conn.execute(
                    """
                    SELECT p.account_id
                    FROM position_snapshots p
                    JOIN securities s USING (security_id)
                    WHERE p.as_of_date = ?
                      AND s.is_scrapeable = 1 AND s.asset_class = 'EQUITY' AND p.quantity > 0
                    GROUP BY p.account_id
                    HAVING COUNT(*) > 5
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                    """,
                    (snap,),
                ).fetchone()
                if snap
                else None
            )
            uma_acct_id = uma_acct_row[0] if uma_acct_row else None
            rows = (
                conn.execute(
                    """
                    SELECT s.quote_symbol, p.quantity
                    FROM position_snapshots p
                    JOIN securities s USING (security_id)
                    WHERE p.account_id = ? AND p.as_of_date = ?
                      AND s.is_scrapeable = 1 AND s.asset_class = 'EQUITY' AND p.quantity > 0
                    """,
                    (uma_acct_id, snap),
                ).fetchall()
                if uma_acct_id
                else []
            )
            qty_map = {r[0]: float(r[1]) for r in rows}
            if qty_map:
                stock_count = len(qty_map)

            def basket_val_on(target_iso: str) -> float:
                tot = 0.0
                for sym, qty in qty_map.items():
                    pr = conn.execute(
                        "SELECT COALESCE(adj_close, close_price) FROM market_prices WHERE quote_symbol = ? AND price_date <= ? ORDER BY price_date DESC LIMIT 1",
                        (sym, target_iso),
                    ).fetchone()
                    if pr and pr[0] is not None:
                        tot += qty * float(pr[0])
                return tot

            if qty_map:
                v_end = basket_val_on(end_iso) or current_uma_val
                v_match = basket_val_on(matched_start_iso)
                if v_match > 0:
                    matched_start_val = round(v_match, 2)
                    r_m, _ = solve_annualized_return([(matched_start_iso, v_match)], end_iso, v_end)
                    if r_m is not None:
                        matched_rate = r_m
                v_full = basket_val_on(full_start_iso)
                if v_full > 0:
                    full_start_val = round(v_full, 2)
                    r_f, _ = solve_annualized_return([(full_start_iso, v_full)], end_iso, v_end)
                    if r_f is not None:
                        full_rate = r_f
    except Exception:
        pass

    eff_ytd = round(float(ytd_rate if ytd_rate is not None else uma_default_gp), 4)
    basket_prefix = f"{stock_count}-Stock Basket" if stock_count > 0 else "Equity Basket"
    return {
        "ytd_statement": {
            "annual_rate_pct": eff_ytd,
            "label": f"{end_iso[:4]} Statement Money-Weighted Return",
        },
        "matched_window": {
            "annual_rate_pct": round(float(matched_rate), 4),
            "start_date": matched_start_iso,
            "start_basket_value": matched_start_val,
            "label": f"{basket_prefix} Constant-Share CAGR Matched Window ({matched_start_iso} -> Today)",
        },
        "full_history": {
            "annual_rate_pct": round(float(full_rate), 4),
            "start_date": full_start_iso,
            "start_basket_value": full_start_val,
            "label": f"{basket_prefix} Constant-Share CAGR Over Full DB History ({full_start_iso} -> Today)",
        },
        "excluded": {
            "annual_rate_pct": None,
            "label": "Excluded from Diversified Reference Average",
        },
    }


def compute_non_goog_weighted_growth(
    payload: dict[str, Any],
    rollovers: list[dict[str, Any]] | None = None,
    uma_rate_mode: str = "ytd_statement",
    muni_rate_mode: str = "price_cagr",
    include_cash: bool = False,
    asset_rate_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Python solver for computing the market-value-weighted average annualized growth rate
    across non-primary (diversified) holdings, both BEFORE and AFTER a hypothetical transaction/rollover.

    Parameters:
      - `uma_rate_mode`: How to model a Direct-Indexing UMA basket (`'ytd_statement'`, `'matched_window'`, `'full_history'`, `'excluded'`)
      - `muni_rate_mode`: How to model a Municipal Bond sleeve (`'price_cagr'`, `'coupon_yield'`)
      - `include_cash`: Whether to include `cash_sweeps` at 0.0%/yr in the denominator.
      - `asset_rate_overrides`: Optional dict of `{asset_id: annual_rate_pct}` to override any specific holding.
    """
    today_iso = str(payload.get("today_iso") or date.today().isoformat())
    base_assets = [a for a in (payload.get("retirement_sim_assets") or []) if a.get("bucket") not in ("primary_equity", "goog")]
    after_all_assets, _, rollover_audit = apply_hypothetical_rollovers(
        payload.get("retirement_sim_assets") or [],
        rollovers,
        payload=payload,
    )
    after_assets_by_id = {a["id"]: a for a in after_all_assets if a.get("bucket") not in ("primary_equity", "goog")}

    # Dynamically map strategy rollups to retirement_sim_assets IDs
    strat_cagr_by_sim_id: dict[str, tuple[float, str]] = {}
    base_ids = [a["id"] for a in base_assets]
    uma_sim_id = next((aid for aid in base_ids if aid.startswith("uma_")), "")
    muni_sim_id = next((aid for aid in base_ids if aid.startswith("munis_")), "")

    for grp in payload.get("holdings_by_strategy") or []:
        gid = str(grp.get("group_id") or "")
        sym = str(grp.get("symbol") or "").strip()
        acct_id = str(grp.get("account_id") or "").strip()
        acct_slug = re.sub(r"[^a-zA-Z0-9]+", "_", acct_id).strip("_").lower()
        sim_id = None
        if gid == "rollup_cash_all":
            sim_id = "cash_sweeps"
        elif gid.startswith("rollup_munis_"):
            sim_id = muni_sim_id or f"munis_{acct_slug}"
        elif gid.startswith("rollup_uma_"):
            sim_id = uma_sim_id or f"uma_{acct_slug}"
        else:
            cand_exact = f"{sym.lower()}_{acct_slug}" if sym and acct_slug else ""
            if cand_exact in base_ids:
                sim_id = cand_exact
            elif sym:
                sim_id = next(
                    (aid for aid in base_ids if aid.endswith(f"_{sym}") or aid.startswith(f"{sym.lower()}_")),
                    None,
                )
        if not sim_id:
            continue
        p_date_str = grp.get("principal_date") or grp.get("purchase_date")
        p_val = grp.get("principal_val") if grp.get("principal_val") is not None else grp.get("purchase_value")
        cur_val = float(grp.get("current_value") or 0.0)
        if grp.get("cagr_pct") is not None:
            c_pct = float(grp["cagr_pct"])
            src_lbl = (
                f"Historical CAGR since {p_date_str} (${float(p_val):,.0f} -> ${cur_val:,.0f})"
                if (p_date_str and p_val)
                else f"Historical CAGR ({grp.get('growth_str') or ''})"
            )
            strat_cagr_by_sim_id[sim_id] = (c_pct, src_lbl)

    uma_initial_val = next(
        (float(a.get("current_value") or 0.0) for a in base_assets if a["id"].startswith("uma_")),
        0.0,
    )
    uma_modes = _compute_uma_historical_rates(today_iso, uma_initial_val, payload=payload)
    overrides_map = {str(k): float(v) for k, v in (asset_rate_overrides or {}).items() if v is not None}

    muni_annual_income = sum(
        float(h.get("est_annual_income") or 0.0)
        for h in (payload.get("holdings_detail") or [])
        if isinstance(h, dict) and str(h.get("category") or "").lower().startswith("muni")
    )
    if muni_annual_income <= 0.0:
        for grp in payload.get("holdings_by_strategy") or []:
            if str(grp.get("group_id") or "").startswith("rollup_munis_"):
                for ch in grp.get("children") or []:
                    cv_ch = float(ch.get("current_value") or 0.0)
                    gp_ch = float(ch.get("default_growth_pct") or 0.0)
                    if cv_ch > 0 and gp_ch > 0:
                        muni_annual_income += cv_ch * (gp_ch / 100.0)

    def eval_for_mode(u_mode: str, m_mode: str) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        before_num = 0.0
        before_den = 0.0
        after_num = 0.0
        after_den = 0.0

        for a in base_assets:
            aid = a["id"]
            b_val = float(a.get("current_value") or 0.0)
            a_val = float((after_assets_by_id.get(aid) or {}).get("current_value") or 0.0)

            rate_pct: float | None = None
            rate_source = ""

            if aid in overrides_map:
                rate_pct = overrides_map[aid]
                rate_source = "Custom user override"
            elif aid == "cash_sweeps" or a.get("tax_model") == "CASH":
                if include_cash:
                    rate_pct = 0.0
                    rate_source = "Cash sweep (0.0% real return)"
                else:
                    rate_pct = None
                    rate_source = "Excluded (cash sweep)"
            elif aid.startswith("uma_"):
                u_info = uma_modes.get(u_mode) or uma_modes["ytd_statement"]
                rate_pct = u_info["annual_rate_pct"]
                rate_source = u_info["label"]
            elif aid.startswith("munis_") or a.get("tax_model") == "MUNI":
                if m_mode == "coupon_yield":
                    if muni_annual_income > 0 and b_val > 0:
                        rate_pct = round(muni_annual_income / b_val * 100.0, 4)
                        rate_source = f"Tax-exempt annual coupon yield (${muni_annual_income:,.2f} / yr)"
                    else:
                        fallback_yld = float(a.get("annual_rate_pct") or a.get("default_growth_pct") or 0.0)
                        rate_pct = round(fallback_yld, 4)
                        rate_source = "Configured tax-exempt yield"
                else:
                    fallback_cagr = float(a.get("default_growth_pct") or a.get("annual_rate_pct") or 0.0)
                    rate_pct, rate_source = strat_cagr_by_sim_id.get(aid, (fallback_cagr, "Historical price CAGR"))
            else:
                fallback_gp = a.get("default_growth_pct") if a.get("default_growth_pct") is not None else a.get("annual_rate_pct")
                rate_pct, rate_source = strat_cagr_by_sim_id.get(
                    aid,
                    (float(fallback_gp) if fallback_gp is not None else None, "Holding baseline forecast"),
                )

            included = rate_pct is not None
            if included and rate_pct is not None:
                before_num += b_val * rate_pct
                before_den += b_val
                after_num += a_val * rate_pct
                after_den += a_val

            rows.append(
                {
                    "id": aid,
                    "title": a.get("short_title") or a.get("title"),
                    "account_label": a.get("account_label"),
                    "included_in_weighted_avg": included,
                    "annual_rate_pct": round(rate_pct, 4) if rate_pct is not None else None,
                    "annual_rate_2dec_pct": round(rate_pct, 2) if rate_pct is not None else None,
                    "rate_source": rate_source,
                    "before_value_usd": round(b_val, 2),
                    "after_value_usd": round(a_val, 2),
                    "delta_value_usd": round(a_val - b_val, 2),
                }
            )

        b_rate = (before_num / before_den) if before_den > 0 else 0.0
        a_rate = (after_num / after_den) if after_den > 0 else 0.0
        delta_rate = a_rate - b_rate

        for r in rows:
            if r["included_in_weighted_avg"]:
                r["before_weight_pct"] = round(r["before_value_usd"] / before_den * 100.0, 2) if before_den > 0 else 0.0
                r["after_weight_pct"] = round(r["after_value_usd"] / after_den * 100.0, 2) if after_den > 0 else 0.0
            else:
                r["before_weight_pct"] = 0.0
                r["after_weight_pct"] = 0.0

        return {
            "uma_rate_mode": u_mode,
            "muni_rate_mode": m_mode,
            "before_modeled_principal_usd": round(before_den, 2),
            "after_modeled_principal_usd": round(after_den, 2),
            "before_weighted_rate_pct": round(b_rate, 4),
            "before_weighted_rate_2dec_pct": round(b_rate, 2),
            "before_slider_step_pct": round(b_rate, 1),
            "after_weighted_rate_pct": round(a_rate, 4),
            "after_weighted_rate_2dec_pct": round(a_rate, 2),
            "after_slider_step_pct": round(a_rate, 1),
            "rate_delta_pct": round(delta_rate, 4),
            "rate_delta_2dec_pct": round(delta_rate, 2),
            "holdings": rows,
        }

    selected = eval_for_mode(uma_rate_mode, muni_rate_mode)
    modes_comparison = {
        mode_key: {
            "uma_annual_rate_pct": uma_modes[mode_key]["annual_rate_pct"],
            "uma_rate_label": uma_modes[mode_key]["label"],
            "before_weighted_rate_pct": res["before_weighted_rate_pct"],
            "after_weighted_rate_pct": res["after_weighted_rate_pct"],
            "rate_delta_pct": res["rate_delta_pct"],
            "before_slider_step_pct": res["before_slider_step_pct"],
            "after_slider_step_pct": res["after_slider_step_pct"],
        }
        for mode_key, res in (
            (mk, eval_for_mode(mk, muni_rate_mode))
            for mk in ("ytd_statement", "matched_window", "full_history", "excluded")
        )
    }

    return {
        "today_iso": today_iso,
        "rollovers_applied": rollover_audit,
        "selected_mode": selected,
        "modes_comparison": modes_comparison,
    }


def _embedded_tax_liability(
    st: dict[str, Any],
    asset: dict[str, Any],
    state_map: dict[str, Any] | None = None,
) -> float:
    """
    Heir after-tax income-in-respect-of-a-decedent (IRD) liability honoring IRS §1014 Step-Up (`DA2-14`):
      - `TAX_FREE` (Roth/HSA), `MUNI`, `CASH`, and taxable `LTCG_*` pass with stepped-up basis (`$0` heir LTCG).
      - `ORDINARY_FULL` (Traditional IRA) is distributed over the SECURE Act 10-year window (`10%`/yr) across
        progressive ordinary brackets (+ state, 0% NIIT).
      - `ANNUITY_LIFO` taxes accumulated earnings above contract basis across progressive ordinary + NIIT brackets.
    PARITY INVARIANT: Must match `embeddedTaxLiability` in `app_simulator.js` 1:1.
    """
    model = asset.get("tax_model")
    rate = float(asset.get("tax_rate") or 0.0)
    val = float(st.get("val") or 0.0)
    basis = float(st.get("costBasis") or 0.0)
    if val <= 0.0 or model in ("TAX_FREE", "MUNI", "CASH", "LTCG_POOL", "LTCG_TRANCHES", "UNVESTED_GOOG"):
        return 0.0
    taxable_amt = val if model == "ORDINARY_FULL" else max(0.0, val - basis)
    if taxable_amt <= 0.005:
        return 0.0
    tc = state_map.get("_taxCfg") if isinstance(state_map, dict) else None
    if tc and tc.get("progressive"):
        dfl = float(((state_map or {}).get("_taxYtd") or {}).get("dfl") or 1.0)
        is_mfj = bool(tc["is_mfj"])
        cur_ord = federal_tax_2026.STD_DEDUCTION[is_mfj] * dfl
        ann_chunk = 0.10 * taxable_amt
        apply_niit = model == "ANNUITY_LIFO"
        eff_st = float(tc["state_rate"])
        pts = _ord_dx_breakpoints(cur_ord, cur_ord, ann_chunk, 0.0, tc, dfl, apply_niit)
        ann_tax = 0.0
        for i in range(len(pts) - 1):
            a0, a1 = pts[i], pts[i + 1]
            seg = a1 - a0
            if seg <= 1e-12:
                continue
            mid = 0.5 * (a0 + a1)
            ann_tax += seg * _ord_marginal_rate_at(cur_ord + mid, cur_ord + mid, tc, dfl, apply_niit, eff_st, 0.0)
        return 10.0 * ann_tax
    return rate * taxable_amt


def _opt_total_embedded_liability(
    state_map: dict[str, Any],
    active_assets: list[dict[str, Any]],
    spendable_tot: float,
) -> float:
    """
    Total heir IRD + IRC §2001 40% Federal Estate Tax above CPI-indexed unified exemption (`$14M` Single / `$28M` MFJ)
    after IRC §691(c) IRD deduction (`DA2-14` — lockstep with `_optEmbeddedTot` in `app_simulator.js`).
    """
    ir_emb = sum(_embedded_tax_liability(state_map[a["id"]], a, state_map) for a in active_assets if state_map.get(a["id"]))
    tc = state_map.get("_taxCfg")
    if tc and tc.get("progressive"):
        dfl = float((state_map.get("_taxYtd") or {}).get("dfl") or 1.0)
        exemption = (28_000_000.0 if bool(tc["is_mfj"]) else 14_000_000.0) * dfl
        estate_tax = 0.40 * max(0.0, max(0.0, spendable_tot - ir_emb) - exemption)
        return ir_emb + estate_tax
    return ir_emb


def _build_heir_legacy_order(
    sim_assets: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    primary_sym: str = "",
) -> list[str]:
    """
    Heir Legacy (§1014) drawdown order:
    High-basis/unvested (0% gain) -> low-yield annuities (<5%) & diversified SMA -> munis
    -> Trad IRA & higher-yield annuities (avoiding heir ordinary tax) -> low-basis core equity
    (held for §1014 tax-free basis step-up) -> Cash Reserve -> Roth IRA.
    """
    eff_s = settings or {}
    def _key(a: dict[str, Any]) -> tuple[int, float, int, str]:
        aid = str(a["id"])
        tm = a.get("tax_model")
        tw = a.get("tax_wrapper")
        gp = float(resolve_asset_forecast(a, eff_s, primary_sym)["growth_pct"]) if eff_s else float(a.get("default_growth_pct") or 0.0)
        is_ss = (a.get("bucket") in ("primary_equity", "goog")) or (resolve_asset_forecast(a, eff_s, primary_sym)["risk_class"] == "single_stock" if eff_s else False)
        if "high_basis" in aid or a.get("is_unvested_goog"):
            tier = 1
            sub = -gp
        elif tm == "ANNUITY_LIFO" and gp < 5.0:
            tier = 2
            sub = gp
        elif tm == "LTCG_POOL" and not is_ss:
            tier = 3
            sub = gp
        elif tm == "MUNI":
            tier = 4
            sub = gp
        elif tw == "TRAD_IRA" or tm == "ORDINARY_FULL":
            tier = 5
            sub = gp
        elif tm == "ANNUITY_LIFO":
            tier = 6
            sub = gp
        elif tm == "CASH":
            tier = 8
            sub = 0.0
        elif tw == "ROTH_IRA" or tm == "TAX_FREE":
            tier = 9
            sub = gp
        else:
            tier = 7
            sub = -gp
        return (tier, sub, int(a.get("default_rank", 999)), aid)
    return [a["id"] for a in sorted(sim_assets, key=_key)]


def _compute_bear_shield_order(sim_assets: list[dict[str, Any]], base_order: list[str]) -> list[str]:
    """
    Dynamic Bear-Shield ordering: routes withdrawals in downturns through defensive liquid buckets
    (Cash -> Munis -> Fixed/Cash annuities) ahead of equities while keeping relative rank within tiers.
    """
    by_id = {a["id"]: a for a in sim_assets}
    def _tier(aid: str) -> int:
        a = by_id.get(aid) or {}
        tm = a.get("tax_model")
        tw = a.get("tax_wrapper")
        rc = a.get("default_risk_class") or ""
        if tm == "CASH":
            return 0
        if tm == "MUNI":
            return 1
        if tm == "ANNUITY_LIFO" or (a.get("rate_model") == "fixed" and tw != "TRAD_IRA") or rc in ("cash_fixed", "fixed_income"):
            return 2 if tw != "TRAD_IRA" else 4
        if tw == "ROTH_IRA" or tm == "TAX_FREE":
            return 5
        return 3
    pos = {aid: idx for idx, aid in enumerate(base_order)}
    return sorted(base_order, key=lambda aid: (_tier(aid), pos.get(aid, 999)))


def optimize_sim_asset_order(
    sim_assets: list[dict[str, Any]],
    today_iso: str,
    settings: dict[str, Any],
    ssn_model: dict[str, Any],
    initial_loss_carryforward: float = 0.0,
    primary_sym: str = "",
    payload: dict[str, Any] | None = None,
) -> list[str]:
    """
    Matches `optimizeSimAssetOrder()` in `html_report.py`: multi-seed steepest-ascent
    pairwise-swap + single-item insertion search maximizing solvency and net score at the primary investor's milestone age.
    """
    start_year = int(today_iso[:4])
    start_month0 = int(today_iso[5:7]) - 1
    start_day = int(today_iso[8:10])
    fallback_proj = _project_config_ctx.get()
    if not isinstance(fallback_proj, dict):
        try:
            from config import load_config

            loaded_cfg = load_config()
            fallback_proj = loaded_cfg if isinstance(loaded_cfg, dict) else {}
        except Exception:
            fallback_proj = {}
    eff_payload_pre = payload if isinstance(payload, dict) else {
        "project": fallback_proj or {},
        "ssn_model": ssn_model,
        "today_iso": today_iso,
        "retirement_sim_assets": sim_assets,
    }
    sim_assets = materialize_sim_assets(sim_assets, settings, eff_payload_pre)
    eff_payload = payload if isinstance(payload, dict) else {
        **eff_payload_pre,
        "retirement_sim_assets": sim_assets,
    }
    if not primary_sym:
        proj_o = eff_payload.get("project") if isinstance(eff_payload.get("project"), dict) else {}
        primary_sym = str(proj_o.get("primary_equity_symbol") or "").strip().upper()
    if not primary_sym:
        for a in sim_assets:
            if a.get("bucket") in ("goog", "primary_equity"):
                cand_s = str(a.get("symbol") or a.get("forecast_key") or "").strip().upper()
                if cand_s and not cand_s.startswith("#") and not cand_s.startswith("FUTURE_") and " " not in cand_s:
                    primary_sym = cand_s
                    break
    tax_cfg = build_tax_schedule_cfg(eff_payload, settings)

    for a in sim_assets:
        fc_a = resolve_asset_forecast(a, settings, primary_sym)
        a["_is_forecast_override"] = bool(fc_a.get("is_override"))
    adjust_year = _build_year_rate_adjuster(sim_assets, settings, primary_sym, today_iso=today_iso)
    asset_rates = compute_asset_monthly_rates(sim_assets, settings, primary_sym)
    is_stress = str(settings.get("simActiveModel") or "") == "stress"

    inf_rate = math.pow(1.0 + float(settings["simInflationPct"]) / 100.0, 1.0 / 12.0) - 1.0
    ann_burn = float(settings["simAnnualBurn"])
    dd_start_opt = compute_drawdown_start_month(tax_cfg.get("work_plan"), today_iso, ann_burn, inf_rate)
    stress_prof, get_stress_rates = _build_stress_month_resolver(
        sim_assets, settings, primary_sym, asset_rates, adjust_year, enabled=is_stress, start_month=dd_start_opt
    )
    inc_unv = bool(settings["simIncludeUnvested"])
    inc_ssn = bool(settings["simIncludeSSN"])
    inc_sa = bool(settings.get("simIncludeScheduledActions", True))
    actions = normalize_scheduled_actions(
        settings.get("simScheduledActions") or [],
        today_iso,
        family_birthdays=_payload_family_birthdays(eff_payload),
    )
    j_claim, y_claim = _resolve_investor_claim_ages(settings, ssn_model)
    cola_pct = float(settings["ssnColaPct"])

    inv_meta = _get_investors_meta(eff_payload, ssn_model)
    p_inv = inv_meta[0] if inv_meta else {"birth_year": 1975, "birth_month": 1, "birth_day": 1, "milestone_age": 85}
    p_by, p_bm, p_bd, p_ma = p_inv["birth_year"], p_inv["birth_month"], p_inv["birth_day"], p_inv["milestone_age"]

    milestone_idx: int | None = None
    monthly_schedule: list[tuple[str, float, float, float]] = []
    for m in range(1, SIM_TOTAL_MONTHS + 1):
        tot_m = start_year * 12 + start_month0 + m
        yr = tot_m // 12
        mo0 = tot_m % 12
        days_in_mo = calendar.monthrange(yr, mo0 + 1)[1]
        iso = f"{yr:04d}-{mo0 + 1:02d}-{min(start_day, days_in_mo):02d}"
        if milestone_idx is None and age_on_iso(p_by, p_bm, p_bd, iso, m) >= p_ma:
            milestone_idx = m
        deflator = math.pow(1.0 + inf_rate, m)
        ann_burn_m = ann_burn * deflator
        ssn_gross = (
            compute_monthly_ssn_for_iso(iso, m, j_claim, y_claim, cola_pct, ssn_model)["totNomGross"]
            if inc_ssn
            else 0.0
        )
        monthly_schedule.append((iso, ann_burn_m / 12.0, ssn_gross, deflator))

    if milestone_idx is None:
        milestone_idx = SIM_TOTAL_MONTHS

    active_assets = [a for a in sim_assets if not (a.get("is_unvested_goog") and not inc_unv)]
    active_lens = str(settings.get("simActiveModel") or "single_path").strip().lower()
    use_bs = bool(settings.get("simBearShield", True))
    ms_deflator_opt = monthly_schedule[milestone_idx - 1][3] if milestone_idx >= 1 else 1.0

    # DA-C04 (User Decision A8 — Mode-Aware Auto-Optimize & Lockstep Parity Invariant):
    # - `single_path` / `stress`: Evaluates deterministic 480m path (`stress` applies Year-1 shocks + `bearOrder`).
    # - `range` (Monte Carlo): Evaluates 4 Common-Random-Numbers (CRN) paths seeded with `_Mulberry32Rng(1)`
    #   and scores `(survived_count, 0.5*(P25+P50), P50, mean)` so high-volatility single-stock hoarding is
    #   penalized when concentration risk hurts P25/solvency; runs 1 pairwise-swap pass from the best of 4 seeds.
    # - `historical`: Evaluates 4 stratified crisis cohorts (`[1929, 1966, 1973, 2000]`) with the same 1-pass budget.
    # PARITY WARNING: Changing CRN seed (`1`), path count (`4`), cohort anchors, or pass count (`1` vs `4`) here
    # REQUIRES an identical edit in `_buildOptimizerContext` / `findAutoOptimizedOrder` in `app_simulator.js`!
    mc_opt_paths: list[tuple[list[dict[str, float]], list[bool]]] | None = None
    hist_opt_cohorts: list[tuple[int, int]] | None = None
    hist_opt_ydata: dict[int, dict[str, Any]] = {}
    hist_opt_cy_rates: dict[int, dict[str, float]] = {}
    hist_opt_cy_inf: dict[int, float] = {}
    hist_opt_cy_bear: dict[int, bool] = {}
    hist_opt_cy_tbill: dict[int, float] = {}
    hist_opt_ssn_base: list[float] = []

    if active_lens == "range":
        raw_ss_vol = float(settings.get("mcSingleStockVolPct") if settings.get("mcSingleStockVolPct") is not None else 36.2)
        raw_mkt_vol = float(settings.get("mcBroadEquityVolPct") if settings.get("mcBroadEquityVolPct") is not None else 13.9)
        raw_corr = max(-0.999, min(0.999, float(settings.get("mcSingleStockCorr") if settings.get("mcSingleStockCorr") is not None else 0.52)))
        zero_vol_opt = (raw_ss_vol <= 0.0 and raw_mkt_vol <= 0.0)
        default_rho_opt = {
            "cash_fixed": 0.0, "fixed_income": 0.20, "hybrid_buffered": 0.75,
            "broad_equity": 1.00, "real_diversifier": 0.05, "single_stock": 0.55, "crypto_speculative": 0.45,
        }
        default_vol_opt = {
            "cash_fixed": 0.0, "fixed_income": 0.045, "hybrid_buffered": 0.085,
            "broad_equity": 0.140, "real_diversifier": 0.150, "single_stock": 0.360, "crypto_speculative": 0.680,
        }
        opt_specs = []
        for a in sim_assets:
            fc = resolve_asset_forecast(a, settings, primary_sym)
            rc = fc["risk_class"]
            mean_dec = fc["growth_pct"] / 100.0
            m_log = math.log(1.0 + mean_dec) if mean_dec > -0.999 else -6.9
            er_dec = fc["expense_ratio_pct"] / 100.0
            rvol = a.get("realized_vol_pct")
            cvol = fc.get("vol_pct")
            if rc == "cash_fixed" or zero_vol_opt:
                sd_a, rho_a = 0.0, 0.0
            elif cvol is not None:
                sd_a = max(0.0, float(cvol) / 100.0)
                rho_a = float(a["corr_spy"]) if a.get("corr_spy") is not None else default_rho_opt.get(rc, 0.55)
            elif rvol is not None and float(rvol) > 0.0 and not fc.get("risk_overridden") and a.get("rate_model") != "fixed":
                sd_a = max(0.0, float(rvol) / 100.0)
                rho_a = float(a["corr_spy"]) if a.get("corr_spy") is not None else default_rho_opt.get(rc, 0.55)
            elif rc == "single_stock":
                sd_a = max(0.0, raw_ss_vol / 100.0)
                rho_a = raw_corr
            elif rc == "broad_equity":
                sd_a = max(0.0, raw_mkt_vol / 100.0)
                rho_a = 1.00
            else:
                sd_a = default_vol_opt.get(rc, 0.140)
                rho_a = default_rho_opt.get(rc, 0.55)
            rho_a = max(-0.999, min(0.999, rho_a))
            is_det = (rc == "cash_fixed") or (sd_a <= 0.0)
            c_mkt = sd_a * rho_a
            c_orth = sd_a * math.sqrt(max(0.0, 1.0 - rho_a * rho_a))
            raw_sym = str(a.get("symbol") or "").strip().upper()
            pfk = a.get("parent_forecast_keys") or []
            if raw_sym and not raw_sym.startswith("#") and " " not in raw_sym:
                sgrp = f"SYM:{raw_sym}"
            elif pfk and str(pfk[0]).strip():
                sgrp = f"SYM:{str(pfk[0]).strip().upper()}"
            elif a.get("bucket") in ("goog", "primary_equity"):
                sgrp = f"SYM:{primary_sym}" if primary_sym else "BUCKET:PRIMARY_EQUITY"
            else:
                sgrp = f"ID:{a['id']}"
            opt_specs.append((a["id"], mean_dec, m_log, er_dec, is_det, c_mkt, c_orth, sgrp))

        rng_opt = _Mulberry32Rng(1)
        n_opt_yrs = math.ceil(milestone_idx / 12)
        mc_opt_paths = []
        for _ in range(4):
            yr_rates_p: list[dict[str, float]] = []
            yr_bear_p: list[bool] = []
            for _y in range(n_opt_yrs):
                z_m = rng_opt.gauss()
                yr_bear_p.append(use_bs and (not zero_vol_opt) and (z_m < -0.50))
                rmap_y: dict[str, float] = {}
                geps: dict[str, float] = {}
                for aid, m_dec, m_lg, er_d, is_d, cm, co, sg in opt_specs:
                    if is_d:
                        na = m_dec - er_d
                    else:
                        if sg not in geps:
                            geps[sg] = rng_opt.gauss()
                        na = (math.exp(m_lg + cm * z_m + co * geps[sg]) - 1.0) - er_d
                    rmap_y[aid] = math.pow(1.0 + max(-0.9999, na), 1.0 / 12.0) - 1.0
                yr_rates_p.append(adjust_year(rmap_y, _y))
            mc_opt_paths.append((yr_rates_p, yr_bear_p))

    elif active_lens == "historical":
        coh_src = ((payload or {}).get("cohorts") or {}).get("years") or []
        if not coh_src:
            try:
                from config import DATA_DIR
                c_path = DATA_DIR / "historical_cohorts.json"
                if c_path.is_file():
                    coh_src = (json.loads(c_path.read_text(encoding="utf-8")) or {}).get("years") or []
            except Exception:
                coh_src = []
        if coh_src:
            hist_opt_ydata = {int(r["year"]): r for r in coh_src}
            all_y_opt = sorted(hist_opt_ydata.keys())
            first_yo, last_yo = all_y_opt[0], all_y_opt[-1]
            cm_opt = str(settings.get("cohortCenterMode") or "anchor").strip().lower()
            im_opt = str(settings.get("cohortInflationMode") or "real_cpi").strip().lower()
            by_yr_rates = _build_cohort_asset_monthly_rates(sim_assets, settings, primary_sym, all_y_opt, hist_opt_ydata, cm_opt)
            for yk in all_y_opt:
                rw = hist_opt_ydata[yk]
                hist_opt_cy_rates[yk] = by_yr_rates[yk]
                hist_opt_cy_tbill[yk] = float(rw.get("tbill_3m_pct", 3.5))
                hist_opt_cy_inf[yk] = (
                    math.pow(1.0 + float(rw["inflation_pct"]) / 100.0, 1.0 / 12.0) - 1.0
                    if im_opt == "real_cpi"
                    else inf_rate
                )
                sp_tp = float(rw.get("sp_total_ret_pct", rw["sp_ret_pct"]))
                ss_p = float(rw.get("single_stock_ret_pct", rw.get("goog_ret_pct", 0.0)))
                hist_opt_cy_bear[yk] = use_bs and (sp_tp < -5.0 or float(rw["sp_ret_pct"]) < -5.0 or ss_p < -10.0)
            for m in range(1, milestone_idx + 1):
                iso_m = monthly_schedule[m - 1][0]
                hist_opt_ssn_base.append(
                    compute_monthly_ssn_for_iso(iso_m, m, j_claim, y_claim, 0.0 if im_opt == "real_cpi" else cola_pct, ssn_model)["totNomGross"]
                    if inc_ssn else 0.0
                )
            anchors = [1929, 1966, 1973, 2000]
            from_yo = max(first_yo, int(settings.get("cohortStartFrom") or first_yo))
            chosen_y = [y for y in anchors if from_yo <= y <= last_yo - 5]
            if len(chosen_y) < 3:
                chosen_y = [y for y in all_y_opt[::max(1, len(all_y_opt) // 4)] if y >= from_yo and (last_yo - y + 1) * 12 >= 60][:4]
            hist_opt_cohorts = [(y, min(milestone_idx, (last_yo - y + 1) * 12)) for y in chosen_y]

    def _opt_spendable_tot(sm_dict: dict[str, dict[str, Any]]) -> float:
        tot = 0.0
        for a in active_assets:
            st = sm_dict.get(a["id"])
            if not st:
                continue
            if a.get("bucket") == "goog" and a.get("is_unvested_goog") and st.get("tranches"):
                for t in st["tranches"]:
                    if t.get("vested"):
                        tot += float(t["val"])
            else:
                tot += float(st["val"])
        return tot

    score_cache: dict[tuple[str, ...], tuple[int, float, float, float]] = {}

    def eval_order(cand_order: list[str]) -> tuple[int, float, float, float]:
        key = tuple(cand_order)
        if key in score_cache:
            return score_cache[key]

        if mc_opt_paths is not None:
            bear_cand = _compute_bear_shield_order(sim_assets, cand_order) if use_bs else cand_order
            survived = 0
            net_vals: list[float] = []
            for yr_rates_p, yr_bear_p in mc_opt_paths:
                sm = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, initial_loss_carryforward, tax_cfg=tax_cfg)
                dep_p = False
                for m in range(1, milestone_idx + 1):
                    iso, raw_burn_mo, ssn_gross, deflator = monthly_schedule[m - 1]
                    yi = (m - 1) // 12
                    sres = _step_sim_month(
                        sim_assets, sm, iso, "", 0.0, 0.0, raw_burn_mo, inc_unv,
                        bear_cand if yi > 0 and yr_bear_p[yi - 1] else cand_order,
                        record_stats=False, asset_monthly_rates=yr_rates_p[yi],
                        scheduled_actions=actions, include_scheduled_actions=inc_sa,
                        deflator=deflator, ssn_gross_mo=ssn_gross,
                    )
                    if (not dep_p) and sres["targetMonthlyBurn"] > 1.0 and sres["monthlyBurn"] < sres["targetMonthlyBurn"] - 1.0:
                        dep_p = True
                if not dep_p:
                    survived += 1
                tot_v = _opt_spendable_tot(sm)
                emb_v = _opt_total_embedded_liability(sm, active_assets, tot_v)
                net_vals.append((tot_v - emb_v) / ms_deflator_opt)
            sv = sorted(net_vals)
            p25 = _mc_percentile(sv, 0.25)
            p50 = _mc_percentile(sv, 0.50)
            mean_v = sum(sv) / len(sv)
            res = (survived, round(0.5 * (p25 + p50), 1), round(p50, 1), round(mean_v, 1))
            score_cache[key] = res
            return res

        if hist_opt_cohorts:
            bear_cand = _compute_bear_shield_order(sim_assets, cand_order) if use_bs else cand_order
            im_opt = str(settings.get("cohortInflationMode") or "real_cpi").strip().lower()
            last_yo = max(hist_opt_ydata.keys())
            survived = 0
            net_vals = []
            for y_start, run_m in hist_opt_cohorts:
                sm = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, initial_loss_carryforward, tax_cfg=tax_cfg)
                dfl_c = 1.0
                dep_c = False
                cy_r = hist_opt_cy_rates[y_start]
                cy_i = hist_opt_cy_inf[y_start]
                cy_b = False
                cy_tb = hist_opt_cy_tbill.get(y_start, 3.5)
                for m in range(1, run_m + 1):
                    if (m - 1) % 12 == 0:
                        hy = min(last_yo, y_start + (m - 1) // 12)
                        cy_r = hist_opt_cy_rates[hy]
                        if cm_opt == "anchor":
                            cy_r = adjust_year(cy_r, hy - y_start)
                        cy_i = hist_opt_cy_inf[hy]
                        cy_b = hy > y_start and hist_opt_cy_bear.get(hy - 1, False)
                        cy_tb = hist_opt_cy_tbill.get(hy, 3.5)
                    dfl_c *= 1.0 + cy_i
                    iso = monthly_schedule[m - 1][0]
                    raw_b = (ann_burn * dfl_c) / 12.0
                    ssn_g = (hist_opt_ssn_base[m - 1] * dfl_c) if im_opt == "real_cpi" else hist_opt_ssn_base[m - 1]
                    sres = _step_sim_month(
                        sim_assets, sm, iso, "", 0.0, 0.0, raw_b, inc_unv,
                        bear_cand if cy_b else cand_order,
                        record_stats=False, asset_monthly_rates=cy_r,
                        scheduled_actions=actions, include_scheduled_actions=inc_sa,
                        deflator=dfl_c, ssn_gross_mo=ssn_g, matured_roll_rate_pct=cy_tb,
                    )
                    if (not dep_c) and sres["targetMonthlyBurn"] > 1.0 and sres["monthlyBurn"] < sres["targetMonthlyBurn"] - 1.0:
                        dep_c = True
                if not dep_c:
                    survived += 1
                tot_v = _opt_spendable_tot(sm)
                emb_v = _opt_total_embedded_liability(sm, active_assets, tot_v)
                net_vals.append((tot_v - emb_v) / dfl_c)
            sv = sorted(net_vals)
            p25 = _mc_percentile(sv, 0.25)
            p50 = _mc_percentile(sv, 0.50)
            mean_v = sum(sv) / len(sv)
            res = (survived, round(0.5 * (p25 + p50), 1), round(p50, 1), round(mean_v, 1))
            score_cache[key] = res
            return res

        state_map = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, initial_loss_carryforward, tax_cfg=tax_cfg)
        dep_month = SIM_TOTAL_MONTHS + 1
        val_85 = 0.0
        embed_85 = 0.0
        val_40 = 0.0
        embed_40 = 0.0

        bear_cand = _compute_bear_shield_order(sim_assets, cand_order) if (use_bs and stress_prof) else cand_order
        bs_end_opt_incl = (dd_start_opt + int(stress_prof["bear_shield_months"]) - 1) if stress_prof else 0
        for m, (iso, raw_burn_mo, ssn_gross, deflator) in enumerate(monthly_schedule, start=1):
            rates_m = get_stress_rates(m) if stress_prof else adjust_year(asset_rates, (m - 1) // 12)
            ord_m = bear_cand if (stress_prof and dd_start_opt <= m <= bs_end_opt_incl) else cand_order
            sres_d = _step_sim_month(
                sim_assets,
                state_map,
                iso,
                "",
                0.0,
                0.0,
                raw_burn_mo,
                inc_unv,
                ord_m,
                record_stats=False,
                asset_monthly_rates=rates_m,
                scheduled_actions=actions,
                include_scheduled_actions=inc_sa,
                deflator=deflator,
                ssn_gross_mo=ssn_gross,
            )
            tot_val = _opt_spendable_tot(state_map)
            if dep_month > SIM_TOTAL_MONTHS and (tot_val <= 1.0 or sres_d["unmetNetBurn"] > 1.0):
                dep_month = m
            if m == milestone_idx:
                val_85 = tot_val
                embed_85 = _opt_total_embedded_liability(state_map, active_assets, tot_val)
            if m == SIM_TOTAL_MONTHS:
                val_40 = tot_val
                embed_40 = _opt_total_embedded_liability(state_map, active_assets, tot_val)

        res = (dep_month, round(val_85 - embed_85, 1), round(val_85, 1), round(val_40 - embed_40, 1))
        score_cache[key] = res
        return res

    def is_better(cand: tuple[int, float, float, float], best: tuple[int, float, float, float]) -> bool:
        if cand[0] != best[0]:
            return cand[0] > best[0]
        for i in (1, 2, 3):
            tol = max(1.0, 0.0005 * abs(best[i]))
            if abs(cand[i] - best[i]) > tol:
                return cand[i] > best[i]
        return False

    def _is_ss_opt(a: dict[str, Any]) -> bool:
        return resolve_asset_forecast(a, settings, primary_sym)["risk_class"] == "single_stock" or a.get("bucket") == "goog"

    def_order = [a["id"] for a in sorted(sim_assets, key=lambda x: (x["default_rank"], x["id"]))]
    heir_order = _build_heir_legacy_order(sim_assets, settings, primary_sym=primary_sym)
    g_first = [a["id"] for a in sorted(sim_assets, key=lambda x: (0 if _is_ss_opt(x) else 1, x["default_rank"], x["id"]))]
    cur_seed = [aid for aid in (settings.get("simAssetOrder") or def_order) if aid in def_order]
    for aid in def_order:
        if aid not in cur_seed:
            cur_seed.append(aid)

    # Guard Cash Sweeps (before Roth) and Roth IRA (strictly last) so optimization preserves emergency buffer & tax-free compounding.
    by_id = {a["id"]: a for a in sim_assets}
    cash_tail = [aid for aid in def_order if (by_id.get(aid) or {}).get("tax_model") == "CASH"]
    roth_tail = [aid for aid in def_order if (by_id.get(aid) or {}).get("tax_model") == "TAX_FREE" and aid not in cash_tail]
    locked_tail = cash_tail + roth_tail
    locked_set = set(locked_tail)

    seeds = [def_order, heir_order, g_first, cur_seed]
    global_best_order = [x for x in def_order if x not in locked_set] + locked_tail
    global_best_score = eval_order(global_best_order)

    is_multi_opt = (mc_opt_paths is not None) or bool(hist_opt_cohorts)
    if is_multi_opt:
        for seed in seeds:
            cand_ord = [x for x in seed if x not in locked_set] + locked_tail
            sc_s = eval_order(cand_ord)
            if is_better(sc_s, global_best_score):
                global_best_score = sc_s
                global_best_order = cand_ord
        search_seeds = [global_best_order]
        max_opt_passes = 1
    else:
        search_seeds = seeds
        max_opt_passes = 4

    for seed in search_seeds:
        cur_mov = [x for x in seed if x not in locked_set]
        cur_order = cur_mov + locked_tail
        cur_score = eval_order(cur_order)
        n = len(cur_mov)
        for _ in range(max_opt_passes):
            best_neighbor_mov = None
            best_neighbor_score = cur_score
            for i in range(n):
                for j in range(i + 1, n):
                    cand_mov = list(cur_mov)
                    cand_mov[i], cand_mov[j] = cand_mov[j], cand_mov[i]
                    sc = eval_order(cand_mov + locked_tail)
                    if is_better(sc, best_neighbor_score):
                        best_neighbor_score = sc
                        best_neighbor_mov = cand_mov
            if not is_multi_opt:
                for from_idx in range(n):
                    for to_idx in range(n):
                        if from_idx == to_idx:
                            continue
                        cand_mov = list(cur_mov)
                        item = cand_mov.pop(from_idx)
                        cand_mov.insert(to_idx, item)
                        sc = eval_order(cand_mov + locked_tail)
                        if is_better(sc, best_neighbor_score):
                            best_neighbor_score = sc
                            best_neighbor_mov = cand_mov
            if best_neighbor_mov is None:
                break
            cur_mov = best_neighbor_mov
            cur_order = cur_mov + locked_tail
            cur_score = best_neighbor_score
        if is_better(cur_score, global_best_score):
            global_best_score = cur_score
            global_best_order = cur_order

    return global_best_order


def build_effective_settings(
    payload: dict[str, Any],
    base_settings: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    use_ref_rates: bool = False,
) -> dict[str, Any]:
    """Merges default settings, optional base save settings, reference rates, and explicit overrides into the single canonical schema."""
    from settings import RISK_CLASSES, canonicalize_model_settings, normalize_half_lives

    today_iso = str(payload.get("today_iso") or date.today().isoformat())
    proj_cfg = payload.get("project") if isinstance(payload.get("project"), dict) else None
    refresh_family_config(proj_cfg)
    base_sim_assets = payload.get("retirement_sim_assets") or []
    prim = str((proj_cfg or {}).get("primary_equity_symbol") or "").strip().upper()
    if not prim and isinstance((proj_cfg or {}).get("symbols"), list) and proj_cfg["symbols"]:
        prim = str(proj_cfg["symbols"][0] or "").strip().upper()

    merged = canonicalize_model_settings(base_settings, cfg=proj_cfg, sim_assets=base_sim_assets)

    if use_ref_rates:
        refs = get_reference_rates(payload)
        hf = {}
        if prim:
            hf[prim] = {"growth_pct": round(float(refs["goog_ref_pct"]), 2), "risk_class": "single_stock"}
        for a in base_sim_assets:
            aid = str(a.get("id") or "")
            fk = str(a.get("forecast_key") or aid)
            rc = str(a.get("default_risk_class") or "broad_equity")
            if a.get("ref_growth_pct") is not None:
                gp = float(a["ref_growth_pct"])
            elif a.get("rate_model") == "fixed" and a.get("annual_rate_pct") is not None:
                gp = float(a["annual_rate_pct"])
            elif rc == "single_stock":
                gp = float(refs["goog_ref_pct"])
            else:
                gp = float(refs["non_goog_ref_pct"])
            ref_entry: dict[str, Any] = {"growth_pct": round(gp, 2), "risk_class": rc}
            if a.get("realized_vol_pct") is not None and float(a.get("realized_vol_pct") or 0.0) > 0.0:
                ref_entry["vol_pct"] = round(float(a["realized_vol_pct"]), 2)
            hf[fk] = copy.deepcopy(ref_entry)
            if aid != fk:
                hf[aid] = copy.deepcopy(ref_entry)
        for grp in payload.get("holdings_by_strategy") or []:
            fk = str(grp.get("forecast_key") or "")
            if fk and fk not in hf and grp.get("ref_growth_pct") is not None:
                rc = str(grp.get("default_risk_class") or "broad_equity")
                hf[fk] = {"growth_pct": round(float(grp["ref_growth_pct"]), 2), "risk_class": rc}
        merged["holdingForecasts"] = hf
        merged["holdingHalfLives"] = {}
        merged["simOptimismMultiplier"] = 1.0
        merged["simAnnualBurn"] = refs["burn_ref_usd"]
        merged["simInflationPct"] = refs["inflation_ref_pct"]
        merged["ssnColaPct"] = refs["ssn_cola_ref_pct"]
        merged["simMarriedFilingJointly"] = None
        merged["simMinTaxBracketFloor"] = None
        merged["simInvestor1RetirementAge"] = None
        merged["simInvestor2RetirementAge"] = None
        merged["simMemberRetirementAges"] = {}
        for inv in _get_investors_meta(payload):
            merged[inv["claim_setting_key"]] = int(inv.get("default_claim_age", 67))

    if overrides:
        ov = dict(overrides)
        # Convenience aliases for MCP / CLI / tests:
        if "withdrawals" in ov and "simScheduledActions" not in ov:
            ov["simScheduledWithdrawals"] = ov.pop("withdrawals")
        if "scheduled_actions" in ov and "simScheduledActions" not in ov:
            ov["simScheduledActions"] = ov.pop("scheduled_actions")
        if "hypothetical_assets" in ov and "simHypotheticalAssets" not in ov:
            ov["simHypotheticalAssets"] = ov.pop("hypothetical_assets")
        if "holding_forecasts" in ov and "holdingForecasts" not in ov:
            ov["holdingForecasts"] = ov.pop("holding_forecasts")

        # Apply holdingForecasts patch cleanly without losing existing holdings:
        if isinstance(ov.get("holdingForecasts"), dict):
            hf_cur = copy.deepcopy(merged.get("holdingForecasts") or {})
            sim_by_key = {str(a.get("forecast_key") or a.get("id") or ""): a for a in base_sim_assets}
            for a in base_sim_assets:
                if a.get("id"):
                    sim_by_key.setdefault(str(a["id"]), a)
            for k, v in ov.pop("holdingForecasts").items():
                ks = str(k)
                prev = hf_cur.get(ks) or {}
                asset_meta = sim_by_key.get(ks) or {}
                def_rc_k = str(prev.get("risk_class") or asset_meta.get("default_risk_class") or "broad_equity")
                def_gp_k = float(prev.get("growth_pct") if "growth_pct" in prev else (asset_meta.get("default_growth_pct") or 0.0))
                if isinstance(v, (int, float)):
                    hf_cur[ks] = {"growth_pct": round(float(v), 2), "risk_class": def_rc_k}
                elif isinstance(v, dict):
                    rc = str(v.get("risk_class") or def_rc_k).lower()
                    if rc not in RISK_CLASSES:
                        rc = "broad_equity"
                    gp = float(v.get("growth_pct", def_gp_k))
                    entry_out: dict[str, Any] = {"growth_pct": round(gp, 2), "risk_class": rc}
                    if v.get("use_reference"):
                        entry_out["use_reference"] = True
                    if v.get("risk_overridden") or ("risk_class" in v and asset_meta.get("default_risk_class") and rc != asset_meta.get("default_risk_class")):
                        entry_out["risk_overridden"] = True
                    if v.get("vol_pct") is not None:
                        try:
                            entry_out["vol_pct"] = round(float(v["vol_pct"]), 2)
                        except (TypeError, ValueError):
                            pass
                    hf_cur[ks] = entry_out
            merged["holdingForecasts"] = hf_cur

        # Support convenience overrides (primaryGrowthPct / diversifiedGrowthPct and legacy futureGoogGrowthPct / futureNonGoogGrowthPct)
        raw_ov_g = ov.pop("primaryGrowthPct", None)
        if raw_ov_g is None and "futureGoogGrowthPct" in ov:
            raw_ov_g = ov.pop("futureGoogGrowthPct")
        if raw_ov_g is not None:
            g_val = round(float(raw_ov_g), 4)
            hf_cur = copy.deepcopy(merged.get("holdingForecasts") or {})
            if prim:
                hf_cur[prim] = {"growth_pct": g_val, "risk_class": "single_stock"}
            for a in base_sim_assets:
                if a.get("bucket") in ("primary_equity", "goog") or a.get("default_risk_class") == "single_stock":
                    hf_cur[a["id"]] = {"growth_pct": g_val, "risk_class": "single_stock"}
                    if a.get("forecast_key"):
                        hf_cur[str(a["forecast_key"])] = {"growth_pct": g_val, "risk_class": "single_stock"}
            merged["holdingForecasts"] = hf_cur

        raw_ov_ng = ov.pop("diversifiedGrowthPct", None)
        if raw_ov_ng is None and "futureNonGoogGrowthPct" in ov:
            raw_ov_ng = ov.pop("futureNonGoogGrowthPct")
        if raw_ov_ng is not None:
            ng_val = round(float(raw_ov_ng), 4)
            hf_cur = copy.deepcopy(merged.get("holdingForecasts") or {})
            for a in base_sim_assets:
                if a.get("bucket") not in ("primary_equity", "goog") and a.get("rate_model") != "fixed":
                    rc = str(a.get("default_risk_class") or "broad_equity")
                    hf_cur[a["id"]] = {"growth_pct": ng_val, "risk_class": rc}
                    if a.get("forecast_key"):
                        hf_cur[str(a["forecast_key"])] = {"growth_pct": ng_val, "risk_class": rc}
            merged["holdingForecasts"] = hf_cur

        if "simScheduledWithdrawals" in ov and "simScheduledActions" not in ov:
            raw_ws = ov.pop("simScheduledWithdrawals") or []
            merged["simScheduledActions"] = [
                {
                    "id": str(w.get("id") or f"act_ov_{i + 1}"),
                    "type": "withdrawal",
                    "cadence": "one_time",
                    "ym": w.get("ym") or w.get("date") or "",
                    "person": w.get("person"),
                    "age": w.get("age"),
                    "amount": w.get("amount"),
                    "amount_mode": "usd",
                    "dollar_mode": w.get("dollar_mode", "today"),
                    "from_asset_id": w.get("from_asset_id") or "auto",
                    "comment": w.get("comment") or w.get("purpose") or "",
                }
                for i, w in enumerate(raw_ws)
                if isinstance(w, dict)
            ]
        if "simIncludeScheduledWithdrawals" in ov and "simIncludeScheduledActions" not in ov:
            merged["simIncludeScheduledActions"] = bool(ov.pop("simIncludeScheduledWithdrawals"))

        if "mcGoogVolPct" in ov and "mcSingleStockVolPct" not in ov:
            merged["mcSingleStockVolPct"] = float(ov.pop("mcGoogVolPct"))
        if "mcNonGoogVolPct" in ov and "mcBroadEquityVolPct" not in ov:
            merged["mcBroadEquityVolPct"] = float(ov.pop("mcNonGoogVolPct"))
        if "mcGoogRestCorr" in ov and "mcSingleStockCorr" not in ov:
            merged["mcSingleStockCorr"] = float(ov.pop("mcGoogRestCorr"))

        if "holdingHalfLives" in ov:
            ov["holdingHalfLives"] = normalize_half_lives(ov["holdingHalfLives"])
        for k, v in ov.items():
            merged[k] = copy.deepcopy(v)

        inv_meta_ov = _get_investors_meta(payload)
        for idx, neutral_ck in enumerate(("ssnInvestor1ClaimAge", "ssnInvestor2ClaimAge")):
            if neutral_ck in ov and ov[neutral_ck] is not None and idx < len(inv_meta_ov):
                target_ck = inv_meta_ov[idx]["claim_setting_key"]
                if target_ck not in ov or ov[target_ck] is None:
                    merged[target_ck] = int(round(float(ov[neutral_ck])))

    merged["simHypotheticalAssets"] = [
        {
            "id": h["id"],
            "label": h["label"],
            "institution": h["institution"],
            "real_product_ref": h["real_product_ref"],
            "initial_value": 0.0,
            "growth_pct": h["growth_pct"],
            "risk_class": h["risk_class"],
            "expense_ratio_pct": h["expense_ratio_pct"],
            "tax_wrapper": h["tax_wrapper"],
            "unlock_ym": h["unlock_ym"],
            "owner_id": h["owner_id"],
            "rmd_owner_index": h["rmd_owner_index"],
        }
        for h in normalize_hypothetical_assets(merged.get("simHypotheticalAssets") or [], payload)
    ]
    # Sync hypothetical growth_pct into holdingForecasts if holdingForecasts didn't already specify it
    hf_final = copy.deepcopy(merged.get("holdingForecasts") or {})
    for h in merged["simHypotheticalAssets"]:
        hid = h["id"]
        if hid not in hf_final:
            hf_final[hid] = {"growth_pct": h["growth_pct"], "risk_class": h["risk_class"]}
        else:
            h["growth_pct"] = float(hf_final[hid].get("growth_pct", h["growth_pct"]))
            h["risk_class"] = str(hf_final[hid].get("risk_class", h["risk_class"]))
    merged["holdingForecasts"] = hf_final

    merged["simScheduledActions"] = normalize_scheduled_actions(
        merged.get("simScheduledActions") or [],
        today_iso,
        family_birthdays=_payload_family_birthdays(payload),
    )
    if "simIncludeScheduledActions" not in merged:
        merged["simIncludeScheduledActions"] = True

    order_list = [str(x) for x in (merged.get("simAssetOrder") or []) if x]
    for h in merged["simHypotheticalAssets"]:
        if h["id"] not in order_list:
            order_list.append(h["id"])
    merged["simAssetOrder"] = order_list
    return merged


def run_simulation(
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
    rollovers: list[dict[str, Any]] | None = None,
    reoptimize_order: bool = False,
    apply_non_goog_rate_mode: str = "none",
    uma_rate_mode: str = "ytd_statement",
    stress_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    Runs the full 40-year (480-month) + 100-year solvency Retirement Simulator in Python
    and returns structured milestone metrics, tax totals, and per-asset drawdown details.
    """
    today_iso = str(payload.get("today_iso") or date.today().isoformat())
    eff = build_effective_settings(payload, settings)
    ssn_model = payload.get("ssn_model") or {}
    proj_cfg = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    primary_sym = str(proj_cfg.get("primary_equity_symbol") or "").strip().upper()
    if not primary_sym and isinstance(proj_cfg.get("symbols"), list) and proj_cfg["symbols"]:
        primary_sym = str(proj_cfg["symbols"][0] or "").strip().upper()

    non_goog_growth_analysis: dict[str, Any] | None = None
    if rollovers:
        non_goog_growth_analysis = compute_non_goog_weighted_growth(
            payload,
            rollovers=rollovers,
            uma_rate_mode=uma_rate_mode,
        )
        sel = non_goog_growth_analysis["selected_mode"]
        mode_clean = str(apply_non_goog_rate_mode or "none").strip().lower()
        if mode_clean in ("shift", "solved"):
            delta = float(sel["rate_delta_pct"])
            solved_rate = float(sel["after_weighted_rate_pct"])
            for a in payload.get("retirement_sim_assets") or []:
                if a.get("bucket") not in ("primary_equity", "goog") and a.get("rate_model") != "fixed":
                    fk = str(a.get("forecast_key") or a["id"])
                    cur = resolve_asset_forecast(a, eff, primary_sym)
                    new_gp = round(cur["growth_pct"] + delta, 4) if mode_clean == "shift" else round(solved_rate, 4)
                    eff["holdingForecasts"][fk] = {"growth_pct": new_gp, "risk_class": cur["risk_class"]}
                    eff["holdingForecasts"][a["id"]] = {"growth_pct": new_gp, "risk_class": cur["risk_class"]}

    materialized = materialize_sim_assets(payload.get("retirement_sim_assets") or [], eff, payload, rollovers=rollovers)
    sim_assets, init_loss_cf, rollover_audit = apply_hypothetical_rollovers(
        materialized,
        rollovers,
        payload=payload,
        settings=eff,
    )
    init_day0_ytd = rollover_audit[-1].get("_day0_ytd") if rollover_audit else None
    for ra in rollover_audit:
        ra.pop("_day0_ytd", None)

    valid_ids = [a["id"] for a in sim_assets]
    active_order = [aid for aid in (eff.get("simAssetOrder") or []) if aid in valid_ids]
    for aid in valid_ids:
        if aid not in active_order:
            active_order.append(aid)

    if reoptimize_order:
        eff["simAssetOrder"] = active_order
        active_order = optimize_sim_asset_order(
            sim_assets, today_iso, eff, ssn_model, init_loss_cf, primary_sym=primary_sym, payload=payload
        )
        eff["simAssetOrder"] = active_order
        eff["simOrderPreset"] = "auto_optimal"

    tax_cfg = build_tax_schedule_cfg(payload, eff)
    inf_monthly = math.pow(1.0 + float(eff["simInflationPct"]) / 100.0, 1.0 / 12.0) - 1.0
    ann_burn = float(eff["simAnnualBurn"])
    dd_start_m = compute_drawdown_start_month(tax_cfg.get("work_plan"), today_iso, ann_burn, inf_monthly)
    use_stress = (str(eff.get("simActiveModel") or "") == "stress") if stress_enabled is None else bool(stress_enabled)
    adjust_year = _build_year_rate_adjuster(sim_assets, eff, primary_sym, today_iso=today_iso)
    base_asset_rates = compute_asset_monthly_rates(sim_assets, eff, primary_sym)
    stress_prof, get_stress_rates = _build_stress_month_resolver(
        sim_assets, eff, primary_sym, base_asset_rates, adjust_year, enabled=use_stress, start_month=dd_start_m
    )
    bs_end_m_incl = (dd_start_m + int(stress_prof["bear_shield_months"]) - 1) if stress_prof else 0
    use_bear_shield = bool(eff.get("simBearShield", True))
    bear_order = _compute_bear_shield_order(sim_assets, active_order) if use_bear_shield else active_order

    start_year = int(today_iso[:4])
    start_month0 = int(today_iso[5:7]) - 1
    start_day = int(today_iso[8:10])

    inc_unv = bool(eff["simIncludeUnvested"])
    inc_ssn = bool(eff["simIncludeSSN"])
    inc_sa = bool(eff.get("simIncludeScheduledActions", True))
    actions = eff.get("simScheduledActions") or []
    j_claim, y_claim = _resolve_investor_claim_ages(eff, ssn_model)
    cola_pct = float(eff["ssnColaPct"])

    state_map = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, init_loss_cf, tax_cfg=tax_cfg)
    if init_day0_ytd:
        state_map["_taxYtd"] = copy.deepcopy(init_day0_ytd)
    bars: list[dict[str, Any]] = []
    depleted_at: dict[str, Any] | None = None
    cum_gross = 0.0
    cum_net = 0.0
    cum_tax = 0.0
    cum_real_gross = 0.0
    cum_real_net = 0.0
    cum_real_tax = 0.0
    cum_ssn_gross = 0.0
    cum_ssn_net = 0.0
    cum_work_contrib = 0.0
    cum_real_work_contrib = 0.0

    for m in range(0, SIM_TOTAL_MONTHS + 1):
        tot_m = start_year * 12 + start_month0 + m
        yr = tot_m // 12
        mo0 = tot_m % 12
        days_in_mo = calendar.monthrange(yr, mo0 + 1)[1]
        day = min(start_day, days_in_mo)
        iso = f"{yr:04d}-{mo0 + 1:02d}-{day:02d}"
        full_lbl = f"{MONTH_NAMES_SHORT[mo0]} {yr}"
        deflator = math.pow(1.0 + inf_monthly, m)
        ann_burn_m = ann_burn * deflator
        raw_target_net = ann_burn_m / 12.0
        ssn_res = (
            compute_monthly_ssn_for_iso(iso, m, j_claim, y_claim, cola_pct, ssn_model)
            if inc_ssn
            else {"totNomGross": 0.0, "totNomTax": 0.0, "totNomNet": 0.0}
        )
        ssn_gross_mo = float(ssn_res["totNomGross"])
        rates_m = get_stress_rates(m) if (stress_prof and m >= 1) else adjust_year(base_asset_rates, max(0, (m - 1) // 12))
        ord_m = bear_order if (use_bear_shield and stress_prof and dd_start_m <= m <= bs_end_m_incl) else active_order

        if m == 0:
            preview_map = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, init_loss_cf, tax_cfg=tax_cfg)
            step_res = _step_sim_month(
                sim_assets,
                preview_map,
                iso,
                full_lbl,
                0.0,
                0.0,
                raw_target_net,
                inc_unv,
                ord_m,
                record_stats=False,
                asset_monthly_rates=rates_m,
                scheduled_actions=[],
                include_scheduled_actions=False,
                deflator=deflator,
                ssn_gross_mo=ssn_gross_mo,
            )
        else:
            step_res = _step_sim_month(
                sim_assets,
                state_map,
                iso,
                full_lbl,
                0.0,
                0.0,
                raw_target_net,
                inc_unv,
                ord_m,
                record_stats=True,
                asset_monthly_rates=rates_m,
                scheduled_actions=actions,
                include_scheduled_actions=inc_sa,
                deflator=deflator,
                ssn_gross_mo=ssn_gross_mo,
            )
            cum_gross += step_res["monthlyGrossSold"]
            cum_net += step_res["monthlyBurn"]
            cum_tax += step_res["monthlyTax"]
            cum_real_gross += step_res["monthlyGrossSold"] / deflator
            cum_real_net += step_res["monthlyBurn"] / deflator
            cum_real_tax += step_res["monthlyTax"] / deflator
            cum_ssn_gross += ssn_gross_mo
            cum_ssn_net += step_res["ssnNetMo"]
            cum_work_contrib += step_res["monthlyWorkContrib"]
            cum_real_work_contrib += step_res["monthlyWorkContrib"] / deflator

        sums = _summarize_state_buckets(sim_assets, state_map, inc_unv)
        if m > 0 and (sums["totalVal"] <= 1.0 or step_res["unmetNetBurn"] > 1.0) and depleted_at is None:
            depleted_at = {
                "monthIdx": m,
                "iso": iso,
                "fullLbl": full_lbl,
                "years": round(m / 12.0, 1),
            }

        bars.append(
            {
                "idx": m,
                "iso": iso,
                "fullLbl": full_lbl,
                "yearsOut": round(m / 12.0, 2),
                "deflator": deflator,
                "totalVal": sums["totalVal"],
                "totalRealVal": sums["totalVal"] / deflator,
                "googVal": sums["googVal"],
                "nonGoogVal": sums["nonGoogVal"],
                "assetVals": sums.get("assetVals") or {},
                "cumGross": cum_gross,
                "cumNet": cum_net,
                "cumTax": cum_tax,
                "cumRealGross": cum_real_gross,
                "cumRealNet": cum_real_net,
                "cumRealTax": cum_real_tax,
                "monthlyWorkContrib": step_res["monthlyWorkContrib"] if m > 0 else 0.0,
                "monthlyPaycheckOffset": step_res["monthlyPaycheckOffset"],
                "cumWorkContrib": cum_work_contrib,
                "cumRealWorkContrib": cum_real_work_contrib,
                "schedWithdrawNom": step_res["schedWithdrawNom"] if m > 0 else 0.0,
                "schedWithdrawReal": step_res["schedWithdrawReal"] if m > 0 else 0.0,
                "schedDepositNom": step_res["schedDepositNom"] if m > 0 else 0.0,
                "schedTransferNom": step_res["schedTransferNom"] if m > 0 else 0.0,
                "schedActionEvents": step_res["schedActionEvents"] if m > 0 else [],
            }
        )

    solvency_depleted = depleted_at
    if solvency_depleted is None:
        ext_map = copy.deepcopy(state_map)
        for m in range(SIM_TOTAL_MONTHS + 1, 1201):
            tot_m = start_year * 12 + start_month0 + m
            yr = tot_m // 12
            mo0 = tot_m % 12
            days_in_mo = calendar.monthrange(yr, mo0 + 1)[1]
            iso = f"{yr:04d}-{mo0 + 1:02d}-{min(start_day, days_in_mo):02d}"
            full_lbl = f"{MONTH_NAMES_SHORT[mo0]} {yr}"
            deflator = math.pow(1.0 + inf_monthly, m)
            ann_burn_m = ann_burn * deflator
            ssn_gross_ext = (
                compute_monthly_ssn_for_iso(iso, m, j_claim, y_claim, cola_pct, ssn_model)["totNomGross"]
                if inc_ssn
                else 0.0
            )
            step_ext = _step_sim_month(
                sim_assets,
                ext_map,
                iso,
                full_lbl,
                0.0,
                0.0,
                ann_burn_m / 12.0,
                inc_unv,
                active_order,
                record_stats=False,
                asset_monthly_rates=adjust_year(base_asset_rates, (m - 1) // 12),
                scheduled_actions=actions,
                include_scheduled_actions=inc_sa,
                deflator=deflator,
                ssn_gross_mo=ssn_gross_ext,
            )
            sums = _summarize_state_buckets(sim_assets, ext_map, inc_unv)
            if sums["totalVal"] <= 1.0 or step_ext["unmetNetBurn"] > 1.0:
                solvency_depleted = {
                    "monthIdx": m,
                    "iso": iso,
                    "fullLbl": full_lbl,
                    "years": round(m / 12.0, 1),
                }
                break

    yr40_bar = bars[-1]
    milestones_dict: dict[str, Any] = {
        "start_today": {
            "date": bars[0]["fullLbl"],
            "iso": bars[0]["iso"],
            "nominal_usd": round(bars[0]["totalVal"], 2),
            "real_today_usd": round(bars[0]["totalRealVal"], 2),
        },
    }
    inv_meta = _get_investors_meta(payload, ssn_model)
    for idx, inv in enumerate(inv_meta):
        m_age = int(inv.get("milestone_age", 85))
        m_bar = next(
            (b for b in bars if age_on_iso(inv["birth_year"], inv["birth_month"], inv["birth_day"], b["iso"], b["idx"]) >= m_age),
            bars[-1],
        )
        m_entry = {
            "investor_id": inv["id"],
            "investor_name": inv["name"],
            "milestone_age": m_age,
            "date": m_bar["fullLbl"],
            "iso": m_bar["iso"],
            "month_idx": m_bar["idx"],
            "nominal_usd": round(m_bar["totalVal"], 2),
            "real_today_usd": round(m_bar["totalRealVal"], 2),
            "cum_tax_nominal_usd": round(m_bar["cumTax"], 2),
            "cum_tax_real_usd": round(m_bar["cumRealTax"], 2),
            "cum_gross_sold_nominal_usd": round(m_bar["cumGross"], 2),
            "cum_gross_sold_real_usd": round(m_bar["cumRealGross"], 2),
            "effective_tax_rate_pct": round((m_bar["cumRealTax"] / m_bar["cumRealGross"] * 100.0) if m_bar["cumRealGross"] > 0 else 0.0, 2),
        }
        milestones_dict[f"{inv['id']}_{m_age}th"] = m_entry
        milestones_dict[f"investor_{idx + 1}_milestone"] = m_entry

    milestones_dict["year_40"] = {
        "date": yr40_bar["fullLbl"],
        "iso": yr40_bar["iso"],
        "month_idx": yr40_bar["idx"],
        "nominal_usd": round(yr40_bar["totalVal"], 2),
        "real_today_usd": round(yr40_bar["totalRealVal"], 2),
        "cum_tax_nominal_usd": round(yr40_bar["cumTax"], 2),
        "cum_tax_real_usd": round(yr40_bar["cumRealTax"], 2),
        "cum_gross_sold_nominal_usd": round(cum_gross, 2),
        "cum_gross_sold_real_usd": round(cum_real_gross, 2),
        "cum_net_spend_nominal_usd": round(cum_net, 2),
        "effective_tax_rate_pct": round((cum_real_tax / cum_real_gross * 100.0) if cum_real_gross > 0 else 0.0, 2),
    }

    asset_drawdowns = []
    for rank_idx, aid in enumerate(active_order, start=1):
        st = state_map.get(aid)
        if not st:
            continue
        asset_drawdowns.append(
            {
                "rank": rank_idx,
                "id": aid,
                "title": st.get("short_title") or st.get("title"),
                "is_hypothetical": bool(st.get("is_hypothetical")),
                "start_value": round(float(st.get("current_value") or 0.0), 2),
                "end_yr40_nominal": round(float(st.get("val") or 0.0), 2),
                "end_yr40_real": round(float(st.get("val") or 0.0) / yr40_bar["deflator"], 2),
                "first_tapped": st.get("firstTappedLbl"),
                "depleted_at": st.get("depletedLbl"),
                "total_gross_sold": round(float(st.get("totalGrossSold") or 0.0), 2),
                "total_tax_paid": round(float(st.get("totalTaxPaid") or 0.0), 2),
                "loss_harvested": round(float(st.get("totalLossHarvested") or 0.0), 2),
                "tax_saved_by_loss": round(float(st.get("totalTaxSaved") or 0.0), 2),
            }
        )

    return {
        "settings": eff,
        "drawdown_start_month": dd_start_m,
        "work_plan_summary": {
            "has_active_workers": bool((tax_cfg.get("work_plan") or {}).get("workers")),
            "drawdown_start_month": dd_start_m,
            "cum_work_contrib_nominal_usd": round(cum_work_contrib, 2),
            "cum_work_contrib_real_usd": round(cum_real_work_contrib, 2),
            "workers": [
                {
                    "idx": w["idx"],
                    "name": w["name"],
                    "retirement_age": w["retirement_age"],
                    "retirement_ym": w["ret_ym"],
                    "covers_full_burn": w["covers_full_burn"],
                    "annual_budget_offset": w["annual_budget_offset"],
                }
                for w in ((tax_cfg.get("work_plan") or {}).get("workers") or [])
            ],
        },
        "weighted_holding_growth_pct": compute_weighted_holding_growth_pct(sim_assets, eff, primary_sym),
        "rollovers_applied": rollover_audit,
        "non_goog_growth_analysis": non_goog_growth_analysis,
        "solvency": {
            "sustains_40_years": depleted_at is None,
            "sustains_100_years": solvency_depleted is None,
            "depleted_at": solvency_depleted,
        },
        "milestones": milestones_dict,
        "asset_drawdowns": asset_drawdowns,
        "bars": bars,
    }


def _imul32(x: int, y: int) -> int:
    return ((x & 0xFFFFFFFF) * (y & 0xFFFFFFFF)) & 0xFFFFFFFF


class _Mulberry32Rng:
    """Exact 1:1 Python mirror of `mcSeededRng(seed)` in `html_report.py` (32-bit mulberry32 + Box-Muller)."""

    def __init__(self, seed: int | None = 1) -> None:
        s = (int(1 if seed is None else seed)) & 0xFFFFFFFF
        self.a = s if s != 0 else 0x9E3779B9
        self.spare: float | None = None

    def u(self) -> float:
        self.a = (self.a + 0x6D2B79F5) & 0xFFFFFFFF
        t = _imul32(self.a ^ (self.a >> 15), 1 | self.a)
        t = ((t + _imul32(t ^ (t >> 7), 61 | t)) ^ t) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    def gauss(self) -> float:
        if self.spare is not None:
            s = self.spare
            self.spare = None
            return s
        u1 = 0.0
        while u1 <= 1e-12:
            u1 = self.u()
        u2 = self.u()
        mag = math.sqrt(-2.0 * math.log(u1))
        self.spare = mag * math.sin(2.0 * math.pi * u2)
        return mag * math.cos(2.0 * math.pi * u2)

    def rand_int(self, n_exclusive: int) -> int:
        return int(math.floor(self.u() * n_exclusive))


def _mc_percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0,1]) over an ascending-sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _filter_weekly_price_series(rows: list[tuple[Any, Any]]) -> list[tuple[date, float]]:
    """
    Filters `(price_date, price)` rows into true weekly cadence (`4 <= gap <= 11` days),
    skipping 1-day grant-date snapshots and mid-week live quote rows (`DA-B07`).
    """
    parsed: list[tuple[date, float]] = []
    for raw_dt, raw_px in rows:
        if raw_px is None:
            continue
        try:
            px = float(raw_px)
            dt = date.fromisoformat(str(raw_dt)[:10])
        except (TypeError, ValueError):
            continue
        if px > 0:
            parsed.append((dt, px))
    if len(parsed) < 2:
        return parsed
    weekly: list[tuple[date, float]] = [parsed[0]]
    prev_dt = parsed[0][0]
    for dt, px in parsed[1:]:
        gap = (dt - prev_dt).days
        if gap >= 4:
            weekly.append((dt, px))
            prev_dt = dt
    return weekly


def _mc_load_weekly_pairs(
    primary_symbol: str = "",
    bench_symbol: str = "SPY",
    db: Any = None,
) -> list[tuple[float, float]]:
    """
    Loads date-aligned weekly simple returns `(primary_ret, bench_ret)` from cached `market_prices`
    (`4 <= gap <= 11` days) for the Historical block-bootstrap Monte Carlo model.
    """
    if not primary_symbol:
        return []

    def _query(conn) -> tuple[dict[date, float], dict[date, float]]:
        def series(sym: str) -> dict[date, float]:
            rows = conn.execute(
                "SELECT price_date, COALESCE(adj_close, close_price) FROM market_prices "
                "WHERE quote_symbol = ? ORDER BY price_date ASC",
                (sym,),
            ).fetchall()
            return dict(_filter_weekly_price_series(rows))

        return series(primary_symbol), series(bench_symbol)

    try:
        if db is not None and getattr(db, "conn", None) is not None:
            g, b = _query(db.conn)
        else:
            from config import get_default_db_path
            from db.database import PortfolioDB

            db_path = get_default_db_path()
            if db_path is None or not db_path.exists():
                return []
            with PortfolioDB(db_path) as opened_db:
                g, b = _query(opened_db.conn)
    except Exception:
        return []

    common = sorted(set(g.keys()) & set(b.keys()))
    pairs: list[tuple[float, float]] = []
    for i in range(1, len(common)):
        d0, d1 = common[i - 1], common[i]
        gap = (d1 - d0).days
        if 4 <= gap <= 11:
            rg = g[d1] / g[d0] - 1.0
            rb = b[d1] / b[d0] - 1.0
            if abs(rg) < 0.85 and abs(rb) < 0.85:
                pairs.append((rg, rb))
    return pairs


def _mc_realized_stats(
    primary_symbol: str = "",
    bench_symbol: str = "SPY",
    db: Any = None,
) -> dict[str, Any]:
    """
    Realized Monte Carlo slider Ref values from cached `market_prices`:
      - each bucket's annualized volatility from its OWN fullest weekly history (`4 <= gap <= 11` days, sd x sqrt(52)),
      - the primary-symbol<->benchmark Pearson correlation from their date-aligned weekly overlap.
    Returns {} when history is unavailable.
    """
    def _vol(conn, sym: str) -> tuple[float, int]:
        if not sym:
            return 0.0, 0
        rows = conn.execute(
            "SELECT price_date, COALESCE(adj_close, close_price) FROM market_prices WHERE quote_symbol = ? ORDER BY price_date ASC",
            (sym,),
        ).fetchall()
        wk = _filter_weekly_price_series(rows)
        rets: list[float] = []
        for i in range(1, len(wk)):
            gap = (wk[i][0] - wk[i - 1][0]).days
            if 4 <= gap <= 11:
                r_i = wk[i][1] / wk[i - 1][1] - 1.0
                if abs(r_i) < 0.85:
                    rets.append(r_i)
        n = len(rets)
        if n < 8:
            return 0.0, n
        m = sum(rets) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (n - 1))
        return sd * math.sqrt(52.0) * 100.0, n

    out: dict[str, Any] = {}
    try:
        if db is not None and getattr(db, "conn", None) is not None:
            gv, gn = _vol(db.conn, primary_symbol)
            bv, bn = _vol(db.conn, bench_symbol)
        else:
            from config import get_default_db_path
            from db.database import PortfolioDB

            db_path = get_default_db_path()
            if db_path is None or not db_path.exists():
                return {}
            with PortfolioDB(db_path) as opened_db:
                gv, gn = _vol(opened_db.conn, primary_symbol)
                bv, bn = _vol(opened_db.conn, bench_symbol)
    except Exception:
        return {}
    pairs = _mc_load_weekly_pairs(primary_symbol, bench_symbol, db=db)
    corr = 0.0
    if len(pairs) >= 8:
        n = len(pairs)
        gs = [p[0] for p in pairs]
        bs = [p[1] for p in pairs]
        mg, mb = sum(gs) / n, sum(bs) / n
        sg = math.sqrt(sum((x - mg) ** 2 for x in gs) / (n - 1))
        sb = math.sqrt(sum((x - mb) ** 2 for x in bs) / (n - 1))
        cov = sum((gs[i] - mg) * (bs[i] - mb) for i in range(n)) / (n - 1)
        corr = cov / (sg * sb) if sg > 0 and sb > 0 else 0.0
    if gv > 0:
        out["ref_goog_vol_pct"] = round(gv, 1)
        out["ref_single_stock_vol_pct"] = round(gv, 1)
    if bv > 0:
        out["ref_nongoog_vol_pct"] = round(bv, 1)
        out["ref_broad_equity_vol_pct"] = round(bv, 1)
    if pairs:
        out["ref_corr"] = round(corr, 2)
    out["weeks_goog"] = gn
    out["weeks_bench"] = bn
    out["weeks_aligned"] = len(pairs)
    return out


def _collect_scheduled_checkpoint_months(
    m: int,
    iso: str,
    inc_sa: bool,
    actions: list[dict[str, Any]],
    cp_set: set[int],
) -> None:
    """Adds annual checkpoints plus `(m-1, m)` around non-monthly scheduled actions so event cliffs render crisply."""
    if m == 0 or m % 12 == 0:
        cp_set.add(m)
    if m >= 1 and inc_sa and actions:
        ym = iso[:7]
        for act in actions:
            if not isinstance(act, dict):
                continue
            cad = str(act.get("cadence") or "one_time").strip().lower()
            if cad == "monthly":
                continue
            if _action_triggers_in_ym(act, ym, vested_this_month=False):
                cp_set.add(m - 1)
                cp_set.add(m)
                break


def run_monte_carlo(
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
    mc: dict[str, Any] | None = None,
    n_paths: int = 300,
    seed: int | None = 1,
    horizon_months: int = SIM_TOTAL_MONTHS,
    rollovers: list[dict[str, Any]] | None = None,
    *,
    stop_at_milestone: bool = False,
) -> dict[str, Any]:
    """
    Generalized 4-risk-class Monte Carlo projection matching `runMonteCarlo()` in `html_report.py` 1:1
    via shared 32-bit `mulberry32` + Box-Muller PRNG (`_Mulberry32Rng`).
    """
    today_iso = str(payload.get("today_iso") or date.today().isoformat())
    eff = build_effective_settings(payload, settings)
    ssn_model = payload.get("ssn_model") or {}
    proj_cfg = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    primary_sym = str(proj_cfg.get("primary_equity_symbol") or "").strip().upper()
    if not primary_sym and isinstance(proj_cfg.get("symbols"), list) and proj_cfg["symbols"]:
        primary_sym = str(proj_cfg["symbols"][0] or "").strip().upper()

    materialized = materialize_sim_assets(payload.get("retirement_sim_assets") or [], eff, payload, rollovers=rollovers)
    sim_assets, init_loss_cf, mc_roll_audit = apply_hypothetical_rollovers(
        materialized, rollovers, payload=payload, settings=eff
    )
    init_day0_ytd_mc = mc_roll_audit[-1].get("_day0_ytd") if mc_roll_audit else None

    valid_ids = [a["id"] for a in sim_assets]
    active_order = [aid for aid in (eff.get("simAssetOrder") or []) if aid in valid_ids]
    for aid in valid_ids:
        if aid not in active_order:
            active_order.append(aid)

    inc_unv = bool(eff["simIncludeUnvested"])
    inc_ssn = bool(eff["simIncludeSSN"])
    inc_sa = bool(eff.get("simIncludeScheduledActions", True))
    actions = eff.get("simScheduledActions") or []
    ann_burn = float(eff["simAnnualBurn"])
    j_claim, y_claim = _resolve_investor_claim_ages(eff, ssn_model)
    cola_pct = float(eff["ssnColaPct"])
    inf_monthly = math.pow(1.0 + float(eff["simInflationPct"]) / 100.0, 1.0 / 12.0) - 1.0

    start_year = int(today_iso[:4])
    start_month0 = int(today_iso[5:7]) - 1
    start_day = int(today_iso[8:10])
    horizon = int(horizon_months)

    inv_meta = _get_investors_meta(payload, ssn_model)
    p_inv = inv_meta[0] if inv_meta else {"id": "investor_1", "birth_year": 1975, "birth_month": 1, "birth_day": 1, "milestone_age": 85}
    p_id, p_by, p_bm, p_bd, p_ma = p_inv["id"], p_inv["birth_year"], p_inv["birth_month"], p_inv["birth_day"], p_inv["milestone_age"]

    tax_cfg = build_tax_schedule_cfg(payload, eff)
    sched: list[dict[str, Any]] = []
    cp_set: set[int] = set()
    p_milestone_month = horizon
    for m in range(0, horizon + 1):
        tot_m = start_year * 12 + start_month0 + m
        yr = tot_m // 12
        mo0 = tot_m % 12
        days_in_mo = calendar.monthrange(yr, mo0 + 1)[1]
        day = min(start_day, days_in_mo)
        iso = f"{yr:04d}-{mo0 + 1:02d}-{day:02d}"
        full_lbl = f"{MONTH_NAMES_SHORT[mo0]} {yr}"
        deflator = math.pow(1.0 + inf_monthly, m)
        raw_target_net = (ann_burn * deflator) / 12.0
        ssn_gross = (
            compute_monthly_ssn_for_iso(iso, m, j_claim, y_claim, cola_pct, ssn_model)["totNomGross"]
            if inc_ssn
            else 0.0
        )
        sched.append({"iso": iso, "full_lbl": full_lbl, "deflator": deflator, "raw_target": raw_target_net, "ssn_gross": ssn_gross})
        _collect_scheduled_checkpoint_months(m, iso, inc_sa, actions, cp_set)
        if m < p_milestone_month and age_on_iso(p_by, p_bm, p_bd, iso, m) >= p_ma:
            p_milestone_month = m
    cp_set.add(horizon)
    checkpoints = sorted(cp_set)

    mc = dict(mc or {})
    adjust_year = _build_year_rate_adjuster(sim_assets, eff, primary_sym, mc, today_iso=today_iso)
    ss_sd = max(
        0.0,
        float(mc.get("singleStockVolPct", mc.get("googVolPct", eff.get("mcSingleStockVolPct", 36.2)))) / 100.0,
    )
    mkt_sd = max(
        0.0,
        float(mc.get("broadEquityVolPct", mc.get("nonGoogVolPct", eff.get("mcBroadEquityVolPct", 13.9)))) / 100.0,
    )
    zero_vol_mode = ss_sd == 0.0 and mkt_sd == 0.0 and ("fixedIncomeVolPct" not in mc)
    fi_sd_default = 0.0 if zero_vol_mode else 4.5
    fi_sd = max(0.0, float(mc.get("fixedIncomeVolPct", fi_sd_default)) / 100.0)
    corr = max(
        -0.999,
        min(0.999, float(mc.get("singleStockCorr", mc.get("googRestCorr", eff.get("mcSingleStockCorr", 0.52))))),
    )
    model = str(mc.get("returnModel", eff.get("mcReturnModel", "normal"))).strip().lower()
    n_paths = max(1, int(mc.get("simulations", eff.get("mcSimulations", n_paths)) if "simulations" in mc else n_paths))

    override_ss_pct = mc.get("googExpectedPct", mc.get("singleStockExpectedPct"))
    override_mkt_pct = mc.get("nonGoogExpectedPct", mc.get("broadEquityExpectedPct"))
    force_preset_vols = bool(mc.get("useClassPresetVols"))

    default_rho_map = {
        "cash_fixed": 0.0,
        "fixed_income": 0.20,
        "hybrid_buffered": 0.75,
        "broad_equity": 1.00,
        "real_diversifier": 0.05,
        "single_stock": 0.55,
        "crypto_speculative": 0.45,
    }
    default_beta_map = {
        "broad_equity": 1.00,
        "hybrid_buffered": 0.55,
        "fixed_income": 0.30,
        "real_diversifier": 0.05,
    }
    default_vol_map = {
        "cash_fixed": 0.0,
        "fixed_income": 0.045,
        "hybrid_buffered": 0.085,
        "broad_equity": 0.140,
        "real_diversifier": 0.150,
        "single_stock": 0.360,
        "crypto_speculative": 0.680,
    }

    # Tuple: (aid, rc, mean_dec, m_log, er_dec, is_det_normal, c_mkt, c_orth, hist_kind, hist_mult, shock_group)
    asset_specs: list[tuple[str, str, float, float, float, bool, float, float, str, float, str]] = []
    for a in sim_assets:
        fc = resolve_asset_forecast(a, eff, primary_sym)
        a["_is_forecast_override"] = bool(fc.get("is_override"))
        rc = fc["risk_class"]
        gp = fc["growth_pct"]
        if override_ss_pct is not None and rc == "single_stock":
            gp = float(override_ss_pct)
        elif override_mkt_pct is not None and rc == "broad_equity" and a.get("rate_model") != "fixed":
            gp = float(override_mkt_pct)
        mean_dec = gp / 100.0
        m_log = math.log(1.0 + mean_dec) if mean_dec > -0.999 else -6.9
        er_dec = fc["expense_ratio_pct"] / 100.0

        rvol_raw = a.get("realized_vol_pct")
        custom_vol = fc.get("vol_pct")
        if rc == "cash_fixed" or zero_vol_mode:
            sd_a, rho_a = 0.0, 0.0
        elif custom_vol is not None:
            sd_a = max(0.0, float(custom_vol) / 100.0)
            c_raw = a.get("corr_spy")
            rho_a = float(c_raw) if c_raw is not None else default_rho_map.get(rc, 0.55)
        elif (
            rvol_raw is not None
            and float(rvol_raw or 0.0) > 0.0
            and not force_preset_vols
            and not fc.get("risk_overridden")
            and a.get("rate_model") != "fixed"
        ):
            sd_a = max(0.0, float(rvol_raw) / 100.0)
            c_raw = a.get("corr_spy")
            rho_a = float(c_raw) if c_raw is not None else default_rho_map.get(rc, 0.55)
        elif rc == "single_stock":
            # DA2-08 PARITY INVARIANT (lockstep with `runMonteCarlo` in `app_simulator.js`):
            # Always use `ss_sd` and `corr` (resolved from `mc` falling back to `eff["mcSingleStockVolPct"]` /
            # `eff["mcSingleStockCorr"]`), NEVER gated on `"singleStockVolPct" in mc`!
            sd_a = ss_sd
            rho_a = corr
        elif rc == "broad_equity":
            sd_a = mkt_sd
            rho_a = 1.00
        elif rc == "fixed_income" and "fixedIncomeVolPct" in mc:
            sd_a = fi_sd
            rho_a = default_rho_map.get(rc, 0.20)
        else:
            sd_a = default_vol_map.get(rc, 0.140)
            rho_a = default_rho_map.get(rc, 0.55)
        rho_a = max(-0.999, min(0.999, float(rho_a)))

        is_det_normal = (rc == "cash_fixed") or (sd_a <= 0.0)
        c_mkt = sd_a * rho_a
        c_orth = sd_a * math.sqrt(max(0.0, 1.0 - rho_a * rho_a))

        if rc == "cash_fixed":
            hist_kind, hist_mult = "zero", 0.0
        elif rc == "single_stock":
            hist_kind = "ss"
            hist_mult = (sd_a / 0.36) if sd_a > 1e-6 else 1.0
        elif rc == "crypto_speculative":
            hist_kind, hist_mult = "ss", 1.65
        else:
            hist_kind = "mkt"
            b_raw = a.get("beta_spy")
            hist_mult = float(b_raw) if b_raw is not None else default_beta_map.get(rc, 1.0)

        # Group buckets that hold the same underlying ticker / primary equity so they share a single
        # idiosyncratic `eps` draw per year instead of artificially diversifying across lots/accounts.
        raw_sym = str(a.get("symbol") or "").strip().upper()
        pfk = a.get("parent_forecast_keys") or []
        if raw_sym and not raw_sym.startswith("#") and " " not in raw_sym:
            shock_group = f"SYM:{raw_sym}"
        elif pfk and str(pfk[0] or "").strip():
            shock_group = f"SYM:{str(pfk[0]).strip().upper()}"
        elif a.get("bucket") in ("goog", "primary_equity"):
            shock_group = f"SYM:{primary_sym}" if primary_sym else "BUCKET:PRIMARY_EQUITY"
        else:
            shock_group = f"ID:{a['id']}"

        asset_specs.append(
            (a["id"], rc, mean_dec, m_log, er_dec, is_det_normal, c_mkt, c_orth, hist_kind, hist_mult, shock_group)
        )

    hist_pairs: list[tuple[float, float]] = list(mc.get("histPairs") or [])
    block = 52
    hist_mu_log_g = hist_mu_log_b = 0.0
    if model == "historical":
        if not hist_pairs:
            hist_pairs = _mc_load_weekly_pairs(primary_sym, "SPY")
        if len(hist_pairs) <= block:
            model = "normal"
        else:
            n_hp = len(hist_pairs)
            windows = n_hp - block + 1
            for i, (stock, market) in enumerate(hist_pairs):
                weight = min(i + 1, block, windows, n_hp - i) / windows
                hist_mu_log_g += weight * math.log1p(stock)
                hist_mu_log_b += weight * math.log1p(market)

    eff_seed = 1 if seed is None else int(seed)
    rng = _Mulberry32Rng(eff_seed)
    n_years = (horizon + 11) // 12
    def to_monthly(annual: float) -> float:
        base = 1.0 + annual
        if base <= 0.0:
            return -0.9999
        return math.pow(base, 1.0 / 12.0) - 1.0

    cp_nominal: dict[int, list[float]] = {cp: [] for cp in checkpoints}
    ms_real_vals: list[float] = []
    ms_nom_vals: list[float] = []
    ms_tax_real_vals: list[float] = []
    ms_eff_tax_vals: list[float] = []
    ending_real: list[float] = []
    ending_nom: list[float] = []
    deplete_months: list[int] = []
    end_deflator = sched[horizon]["deflator"]
    ms_deflator = sched[p_milestone_month]["deflator"]
    path_limit = p_milestone_month if stop_at_milestone else horizon

    use_bear_shield = bool(eff.get("simBearShield", True))
    bear_order = _compute_bear_shield_order(sim_assets, active_order) if use_bear_shield else active_order

    for _p in range(n_paths):
        yearly_rates: list[dict[str, float]] = []
        yearly_bear: list[bool] = []
        for _y in range(n_years):
            if model == "historical":
                start = rng.rand_int(len(hist_pairs) - block + 1)
                lg, lb = 0.0, 0.0
                for k in range(start, start + block):
                    lg += math.log(1.0 + hist_pairs[k][0])
                    lb += math.log(1.0 + hist_pairs[k][1])
                f_ss = lg - hist_mu_log_g
                f_mkt = lb - hist_mu_log_b
                z_mkt = 0.0
                yearly_bear.append(
                    use_bear_shield and ((math.exp(0.08 + f_mkt) - 1.0) < -0.05 or (math.exp(0.08 + f_ss) - 1.0) < -0.10)
                )
            else:
                z_mkt = rng.gauss()
                f_ss, f_mkt = 0.0, 0.0
                yearly_bear.append(use_bear_shield and (not zero_vol_mode) and (z_mkt < -0.50))

            rmap: dict[str, float] = {}
            grp_eps: dict[str, float] = {}
            for aid, _rc, mean_dec, m_log, er_dec, is_det_normal, c_mkt, c_orth, hist_kind, hist_mult, shock_group in asset_specs:
                if model == "historical":
                    if hist_kind == "zero":
                        net_ann = mean_dec - er_dec
                    elif hist_kind == "ss":
                        net_ann = (math.exp(m_log + hist_mult * f_ss) - 1.0) - er_dec
                    else:
                        net_ann = (math.exp(m_log + hist_mult * f_mkt) - 1.0) - er_dec
                else:
                    if is_det_normal:
                        net_ann = mean_dec - er_dec
                    else:
                        if shock_group not in grp_eps:
                            grp_eps[shock_group] = rng.gauss()
                        eps = grp_eps[shock_group]
                        delta = c_mkt * z_mkt + c_orth * eps
                        net_ann = (math.exp(m_log + delta) - 1.0) - er_dec
                rmap[aid] = to_monthly(net_ann)
            yearly_rates.append(adjust_year(rmap, _y))

        state_map = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, init_loss_cf, tax_cfg=tax_cfg)
        if init_day0_ytd_mc:
            state_map["_taxYtd"] = copy.deepcopy(init_day0_ytd_mc)
        first_deplete = horizon + 1
        cp_i = 0
        last_total = 0.0
        path_ms_gross_real = 0.0
        path_ms_tax_real = 0.0

        for m in range(0, path_limit + 1):
            sc = sched[m]
            if m > 0:
                yr_i = (m - 1) // 12
                rates_m = yearly_rates[yr_i]
                ord_m = bear_order if yr_i > 0 and yearly_bear[yr_i - 1] else active_order
                step_res = _step_sim_month(
                    sim_assets,
                    state_map,
                    sc["iso"],
                    sc["full_lbl"],
                    0.0,
                    0.0,
                    sc["raw_target"],
                    inc_unv,
                    ord_m,
                    record_stats=False,
                    asset_monthly_rates=rates_m,
                    scheduled_actions=actions,
                    include_scheduled_actions=inc_sa,
                    deflator=sc["deflator"],
                    ssn_gross_mo=sc["ssn_gross"],
                )
                tgt = step_res["targetMonthlyBurn"]
                if first_deplete > horizon and tgt > 1.0 and step_res["monthlyBurn"] < tgt - 1.0:
                    first_deplete = m
                if m <= p_milestone_month:
                    path_ms_gross_real += step_res["monthlyGrossSold"] / sc["deflator"]
                    path_ms_tax_real += step_res["monthlyTax"] / sc["deflator"]

            need_cp = (not stop_at_milestone) and cp_i < len(checkpoints) and m == checkpoints[cp_i]
            need_ms = m == p_milestone_month
            if need_cp or need_ms:
                total = _summarize_state_buckets(sim_assets, state_map, inc_unv)["totalVal"]
                if need_cp:
                    cp_nominal[checkpoints[cp_i]].append(total)
                    last_total = total
                    cp_i += 1
                if need_ms:
                    ms_real_vals.append(total / ms_deflator)
                    ms_nom_vals.append(total)
                    if stop_at_milestone:
                        last_total = total

        ending_real.append(last_total / (ms_deflator if stop_at_milestone else end_deflator))
        ending_nom.append(last_total)
        deplete_months.append(first_deplete)
        ms_tax_real_vals.append(path_ms_tax_real)
        ms_eff_tax_vals.append((path_ms_tax_real / path_ms_gross_real * 100.0) if path_ms_gross_real > 0 else 0.0)

    qs = (0.10, 0.25, 0.50, 0.75, 0.90)
    key = {0.10: "p10", 0.25: "p25", 0.50: "p50", 0.75: "p75", 0.90: "p90"}
    band: list[dict[str, Any]] = []
    if not stop_at_milestone:
        for cp in checkpoints:
            vals = sorted(cp_nominal[cp])
            dfl = sched[cp]["deflator"]
            pv = {q: _mc_percentile(vals, q) for q in qs}
            band.append({
                "monthIdx": cp,
                "yearsOut": round(cp / 12.0, 1),
                "iso": sched[cp]["iso"],
                "nominal": {key[q]: round(pv[q], 2) for q in qs},
                "real": {key[q]: round(pv[q] / dfl, 2) for q in qs},
            })

    def success_through(month_idx: int) -> float:
        ok = sum(1 for d in deplete_months if d > month_idx)
        return round(100.0 * ok / len(deplete_months), 1)

    threshold = max(0.0, float(mc.get("successThresholdUsd", eff.get("mcSuccessThresholdUsd", 0.0)) or 0.0))
    thr_ok = sum(1 for d, v in zip(deplete_months, ending_real) if d > horizon and v >= threshold)
    success_threshold_pct = round(100.0 * thr_ok / len(ending_real), 1) if ending_real else 0.0

    ms_sorted = sorted(ms_real_vals)
    ms_nom_sorted = sorted(ms_nom_vals)
    ending_nom_sorted = sorted(ending_nom)
    ms_tax_sorted = sorted(ms_tax_real_vals)
    ms_eff_sorted = sorted(ms_eff_tax_vals)
    p50_eff_tax = round(_mc_percentile(ms_eff_sorted, 0.50), 1) if ms_eff_sorted else 0.0
    p50_tax_real = round(_mc_percentile(ms_tax_sorted, 0.50), 2) if ms_tax_sorted else 0.0

    ending_sorted = sorted(ending_real)
    hi = _mc_percentile(ending_sorted, 0.99)
    if hi <= 0.0:
        hi = ending_sorted[-1] if ending_sorted else 1.0
    nb = 20
    width = (hi / nb) if hi > 0 else 1.0
    bins = [0] * nb
    for v in ending_sorted:
        bi = int(v / width) if width > 0 else 0
        bins[min(nb - 1, max(0, bi))] += 1
    histogram = [{"lo": round(i * width, 2), "hi": round((i + 1) * width, 2), "count": bins[i]} for i in range(nb)]

    dep_sorted = sorted(float(d) for d in deplete_months)
    med_dep_mo = _mc_percentile(dep_sorted, 0.50) if dep_sorted else float(horizon + 1)
    med_years_solvent = round(horizon / 12.0, 1) if med_dep_mo > horizon else round(med_dep_mo / 12.0, 1)

    return {
        "model": model,
        "n_paths": n_paths,
        "seed": eff_seed,
        "horizon_months": horizon,
        "median_years_solvent": med_years_solvent,
        "params": {
            "singleStockVolPct": round(ss_sd * 100, 3),
            "broadEquityVolPct": round(mkt_sd * 100, 3),
            "singleStockCorr": round(corr, 4),
            "simulations": n_paths,
            "historical_weeks": len(hist_pairs),
        },
        f"success_to_{p_id}{p_ma}_pct": success_through(p_milestone_month),
        "success_to_ms_pct": success_through(p_milestone_month),
        "success_to_primary_milestone_pct": success_through(p_milestone_month),
        "success_to_horizon_pct": success_through(horizon),
        "success_threshold_pct": success_threshold_pct,
        "success_threshold_usd": round(threshold, 2),
        f"{p_id}{p_ma}_month": p_milestone_month,
        "ms_month": p_milestone_month,
        "primary_milestone_month": p_milestone_month,
        "ms_real": {key[q]: round(_mc_percentile(ms_sorted, q), 2) for q in qs} if ms_sorted else {"p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0},
        "ms_nominal": {key[q]: round(_mc_percentile(ms_nom_sorted, q), 2) for q in qs} if ms_nom_sorted else {"p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0},
        "tax_to_ms": {
            "net_kept_pct": round(100.0 - p50_eff_tax, 1),
            "eff_tax_pct": p50_eff_tax,
            "cum_tax_real": round(p50_tax_real),
        },
        "band": band,
        "ending_real": {
            "p10": round(_mc_percentile(ending_sorted, 0.10), 2),
            "p25": round(_mc_percentile(ending_sorted, 0.25), 2),
            "p50": round(_mc_percentile(ending_sorted, 0.50), 2),
            "p75": round(_mc_percentile(ending_sorted, 0.75), 2),
            "p90": round(_mc_percentile(ending_sorted, 0.90), 2),
            "min": round(ending_sorted[0], 2) if ending_sorted else 0.0,
            "max": round(ending_sorted[-1], 2) if ending_sorted else 0.0,
        },
        "ending_nominal": {
            "p10": round(_mc_percentile(ending_nom_sorted, 0.10), 2),
            "p25": round(_mc_percentile(ending_nom_sorted, 0.25), 2),
            "p50": round(_mc_percentile(ending_nom_sorted, 0.50), 2),
            "p75": round(_mc_percentile(ending_nom_sorted, 0.75), 2),
            "p90": round(_mc_percentile(ending_nom_sorted, 0.90), 2),
        } if ending_nom_sorted else {"p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0},
        "ending_histogram": histogram,
    }


def _build_cohort_asset_monthly_rates(
    sim_assets: list[dict[str, Any]],
    eff: dict[str, Any],
    primary_sym: str,
    all_years: list[int],
    ydata: dict[int, dict[str, Any]],
    center_mode: str,
) -> dict[int, dict[str, float]]:
    """
    DA-C05 (Topic B) 3-Tier Historical Cohort Specificity Cascade:
      Tier 1: Exact ticker / multi-constituent sleeve calendar-year `adj_close` total return in `hist_year`.
      Tier 2: Contractual pre-maturity fixed rate (MYGA/CD) and structural cap/buffer payoff (RILA/FIA).
      Tier 3: 1929–2025 asset-class total-return series (`sp_total_ret_pct`, `ust_10y_ret_pct`,
              `tbill_3m_pct`, `gold_ret_pct`) scaled by each holding's own realized `beta_spy` / `vol_pct`.
    Returns `{hist_year: {asset_id: monthly_rate}}`.
    """
    n_all = len(all_years)
    sp_tot_map: dict[int, float] = {
        y: float(ydata[y].get("sp_total_ret_pct", ydata[y]["sp_ret_pct"])) / 100.0
        for y in all_years
    }
    mean_sp_tot = sum(sp_tot_map.values()) / n_all
    sd_sp_tot = (
        math.sqrt(sum((sp_tot_map[y] - mean_sp_tot) ** 2 for y in all_years) / max(1, n_all - 1))
        if n_all > 1
        else 0.18
    )
    mean_log_sp_tot = sum(math.log(max(0.01, 1.0 + sp_tot_map[y])) for y in all_years) / n_all

    by_year_out: dict[int, dict[str, float]] = {y: {} for y in all_years}

    for a in sim_assets:
        fc = resolve_asset_forecast(a, eff, primary_sym)
        a["_is_forecast_override"] = bool(fc.get("is_override"))
        aid = a["id"]
        rc = fc["risk_class"]
        mean_dec = fc["growth_pct"] / 100.0
        m_log = math.log(1.0 + mean_dec) if mean_dec > -0.999 else -6.9
        er_dec = fc["expense_ratio_pct"] / 100.0

        is_contractual_fixed = (
            a.get("rate_model") == "fixed"
            or str(a.get("return_source") or "") == "CONTRACT_CREDITED_RATE"
        )
        if is_contractual_fixed:
            m_rate = math.pow(1.0 + max(-0.9999, mean_dec - er_dec), 1.0 / 12.0) - 1.0
            for y in all_years:
                by_year_out[y][aid] = m_rate
            continue

        # Tier 1 lookup map
        tier1_map: dict[int, float] = {}
        for entry in (a.get("hist_annual_returns") or []):
            if isinstance(entry, dict) and entry.get("year") is not None and entry.get("ret_pct") is not None:
                tier1_map[int(entry["year"])] = float(entry["ret_pct"]) / 100.0

        ac_up = str(a.get("asset_class") or "").upper()
        is_rila = ("RILA" in ac_up) or (aid.startswith("ann_") and rc == "hybrid_buffered")
        is_fia = ("INDEXED_ANNUITY" in ac_up) or (aid.startswith("ann_") and rc == "fixed_income")

        raw_series: dict[int, float] = {}
        for y in all_years:
            if y in tier1_map:
                raw_series[y] = tier1_map[y]
                continue
            row = ydata[y]
            sp_r = sp_tot_map[y]
            if is_rila:
                raw_series[y] = min(sp_r, 0.16) if sp_r >= 0.0 else (0.0 if sp_r >= -0.15 else sp_r + 0.15)
                continue
            if is_fia:
                raw_series[y] = max(0.0, min(sp_r, 0.095))
                continue

            ust_r = float(row.get("ust_10y_ret_pct", 0.25 * float(row["sp_ret_pct"]))) / 100.0
            tbill_r = float(row.get("tbill_3m_pct", 3.0)) / 100.0
            gold_r = float(row.get("gold_ret_pct", 0.0)) / 100.0

            if rc == "cash_fixed":
                raw_series[y] = tbill_r
            elif rc == "fixed_income":
                raw_series[y] = (0.85 * ust_r) if a.get("tax_model") == "MUNI" else ust_r
            elif rc == "hybrid_buffered":
                raw_series[y] = 0.60 * sp_r + 0.40 * ust_r
            elif rc == "real_diversifier":
                raw_series[y] = gold_r
            elif rc == "broad_equity":
                beta = float(a["beta_spy"]) if a.get("beta_spy") is not None else 1.0
                raw_series[y] = max(-0.95, tbill_r + beta * (sp_r - tbill_r))
            else:
                def_v = 35.0 if rc == "single_stock" else 68.0
                rvol_a = a.get("realized_vol_pct")
                hv = (
                    float(fc["vol_pct"])
                    if fc.get("vol_pct") is not None
                    else (float(rvol_a) if (rvol_a is not None and float(rvol_a) > 0.0 and not fc.get("risk_overridden")) else def_v)
                )
                k_h = (hv / 100.0) / max(0.08, sd_sp_tot)
                raw_series[y] = math.exp(mean_log_sp_tot + k_h * (math.log(max(0.01, 1.0 + sp_r)) - mean_log_sp_tot)) - 1.0

        avg_log = sum(math.log(max(0.01, 1.0 + raw_series[y])) for y in all_years) / n_all
        for y in all_years:
            if center_mode == "anchor":
                inn_y = math.log(max(0.01, 1.0 + raw_series[y])) - avg_log
                net_ann = (math.exp(m_log + inn_y) - 1.0) - er_dec
            else:
                net_ann = raw_series[y] - er_dec
            by_year_out[y][aid] = math.pow(1.0 + max(-0.9999, net_ann), 1.0 / 12.0) - 1.0

    return by_year_out


def run_historical_cohorts(
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
    opts: dict[str, Any] | None = None,
    rollovers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    3-Tier Multi-Asset Historical Cohort projection matching `runHistoricalCohorts()` in `app_simulator.js` (`DA-C05`).
    """
    opts = dict(opts or {})
    cohorts = opts.get("cohorts") or payload.get("cohorts") or {}
    years_rows = cohorts.get("years") or []
    if not years_rows:
        return {"available": False, "cohorts": [], "band": [], "named": []}

    ydata = {int(r["year"]): r for r in years_rows}
    all_years = sorted(ydata.keys())
    first_y, last_y = all_years[0], all_years[-1]

    today_iso = str(payload.get("today_iso") or date.today().isoformat())
    eff = build_effective_settings(payload, settings)
    ssn_model = payload.get("ssn_model") or {}
    proj_cfg = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    primary_sym = str(proj_cfg.get("primary_equity_symbol") or "").strip().upper()
    if not primary_sym and isinstance(proj_cfg.get("symbols"), list) and proj_cfg["symbols"]:
        primary_sym = str(proj_cfg["symbols"][0] or "").strip().upper()

    materialized = materialize_sim_assets(payload.get("retirement_sim_assets") or [], eff, payload, rollovers=rollovers)
    sim_assets, init_loss_cf, mc_roll_audit = apply_hypothetical_rollovers(
        materialized, rollovers, payload=payload, settings=eff
    )
    init_day0_ytd_mc = mc_roll_audit[-1].get("_day0_ytd") if mc_roll_audit else None

    valid_ids = [a["id"] for a in sim_assets]
    active_order = [aid for aid in (eff.get("simAssetOrder") or []) if aid in valid_ids]
    for aid in valid_ids:
        if aid not in active_order:
            active_order.append(aid)

    inc_unv = bool(eff["simIncludeUnvested"])
    inc_ssn = bool(eff["simIncludeSSN"])
    inc_sa = bool(eff.get("simIncludeScheduledActions", True))
    actions = eff.get("simScheduledActions") or []
    ann_burn = float(eff["simAnnualBurn"])
    j_claim, y_claim = _resolve_investor_claim_ages(eff, ssn_model)
    cola_pct = float(eff["ssnColaPct"])
    flat_inf_monthly = math.pow(1.0 + float(eff["simInflationPct"]) / 100.0, 1.0 / 12.0) - 1.0

    start_year = int(today_iso[:4])
    start_month0 = int(today_iso[5:7]) - 1
    start_day = int(today_iso[8:10])
    horizon = int(opts.get("horizon_months", SIM_TOTAL_MONTHS))

    inflation_mode = str(opts.get("inflation_mode", eff.get("cohortInflationMode", "real_cpi"))).strip().lower()
    center_mode = str(opts.get("center_mode", eff.get("cohortCenterMode", "anchor"))).strip().lower()
    spotlight = set(int(y) for y in (opts.get("spotlight") or []))

    def _ss_ret(c: dict[str, Any]) -> float:
        return float(c.get("single_stock_ret_pct", c.get("goog_ret_pct", 0.0)))

    adjust_year = _build_year_rate_adjuster(sim_assets, eff, primary_sym, today_iso=today_iso)
    by_year_asset_rates = _build_cohort_asset_monthly_rates(
        sim_assets, eff, primary_sym, all_years, ydata, center_mode
    )

    tax_cfg = build_tax_schedule_cfg(payload, eff)
    inv_meta = _get_investors_meta(payload, ssn_model)
    p_inv = inv_meta[0] if inv_meta else {"id": "investor_1", "birth_year": 1975, "birth_month": 1, "birth_day": 1, "milestone_age": 85}
    p_id, p_by, p_bm, p_bd, p_ma = p_inv["id"], p_inv["birth_year"], p_inv["birth_month"], p_inv["birth_day"], p_inv["milestone_age"]

    skel: list[dict[str, Any]] = []
    cp_set_coh: set[int] = set()
    p_milestone_month = horizon
    for m in range(0, horizon + 1):
        tot_m = start_year * 12 + start_month0 + m
        yr = tot_m // 12
        mo0 = tot_m % 12
        days_in_mo = calendar.monthrange(yr, mo0 + 1)[1]
        iso = f"{yr:04d}-{mo0 + 1:02d}-{min(start_day, days_in_mo):02d}"
        full_lbl = f"{MONTH_NAMES_SHORT[mo0]} {yr}"
        ssn_gross_flat = (
            compute_monthly_ssn_for_iso(iso, m, j_claim, y_claim, cola_pct, ssn_model)["totNomGross"]
            if inc_ssn else 0.0
        )
        ssn_gross_real_base = (
            compute_monthly_ssn_for_iso(iso, m, j_claim, y_claim, 0.0, ssn_model)["totNomGross"]
            if inc_ssn else 0.0
        )
        skel.append({
            "iso": iso,
            "full_lbl": full_lbl,
            "ssn_gross_flat": ssn_gross_flat,
            "ssn_gross_real_base": ssn_gross_real_base,
            "year_offset": (m - 1) // 12 if m > 0 else 0,
        })
        _collect_scheduled_checkpoint_months(m, iso, inc_sa, actions, cp_set_coh)
        if m < p_milestone_month and age_on_iso(p_by, p_bm, p_bd, iso, m) >= p_ma:
            p_milestone_month = m
    cp_set_coh.add(horizon)
    checkpoints = sorted(cp_set_coh)

    qs = (0.10, 0.25, 0.50, 0.75, 0.90)
    qkey = {0.10: "p10", 0.25: "p25", 0.50: "p50", 0.75: "p75", 0.90: "p90"}

    cohort_out: list[dict[str, Any]] = []
    named_out: list[dict[str, Any]] = []
    cp_nom: dict[int, list[float]] = {cp: [] for cp in checkpoints}
    cp_real: dict[int, list[float]] = {cp: [] for cp in checkpoints}
    ms_real_vals: list[float] = []
    ms_nom_vals: list[float] = []
    ms_tax_real_vals: list[float] = []
    ms_eff_tax_vals: list[float] = []
    full_cohorts = 0
    full_success = 0
    j85_cohorts = 0
    j85_success = 0

    y_from = max(first_y, int(opts.get("start_year_from", eff.get("cohortStartFrom", first_y))))
    y_to = min(last_y, int(opts.get("start_year_to", last_y)))

    use_bear_shield = bool(eff.get("simBearShield", True))
    bear_order = _compute_bear_shield_order(sim_assets, active_order) if use_bear_shield else active_order

    for Y in range(y_from, y_to + 1):
        # DA-C05 / User Decision Q2: Incomplete recent cohorts (e.g. Y > 1986 for a 40-yr horizon when
        # `last_y == 2025`) MUST stop cleanly at `last_y` (`run_months = avail_years * 12`) rather than
        # wrapping around to 1929 or extrapolating synthetic post-2025 years.
        avail_years = last_y - Y + 1
        run_months = min(horizon, avail_years * 12)
        if run_months < 12:
            continue

        state_map = _clone_sim_asset_state(sim_assets, today_iso, inc_unv, init_loss_cf, tax_cfg=tax_cfg)
        if init_day0_ytd_mc:
            state_map["_taxYtd"] = copy.deepcopy(init_day0_ytd_mc)
        deflator = 1.0
        depleted_month: int | None = None
        line_nom: list[dict[str, Any]] = []
        cy_rates: dict[str, float] = {}
        cy_inf = 0.0
        cy_bear = False
        cy_tbill = 3.5
        cohort_ms_gross_real = 0.0
        cohort_ms_tax_real = 0.0

        for m in range(0, run_months + 1):
            sc = skel[m]
            if m == 0 or (m - 1) % 12 == 0:
                hist_year = min(last_y, Y + (0 if m == 0 else (m - 1) // 12))
                row = ydata.get(hist_year, ydata[last_y])
                previous = ydata.get(hist_year - 1) if hist_year > Y else None
                cy_bear = bool(use_bear_shield and previous and (
                    float(previous.get("sp_total_ret_pct", previous["sp_ret_pct"])) < -5.0
                    or float(previous["sp_ret_pct"]) < -5.0 or _ss_ret(previous) < -10.0
                ))
                cy_rates = by_year_asset_rates.get(hist_year, by_year_asset_rates[last_y])
                if center_mode == "anchor":
                    cy_rates = adjust_year(cy_rates, hist_year - Y)
                cy_tbill = float(row.get("tbill_3m_pct", 3.5))
                if inflation_mode == "real_cpi":
                    cy_inf = math.pow(1.0 + row["inflation_pct"] / 100.0, 1.0 / 12.0) - 1.0
                else:
                    cy_inf = flat_inf_monthly

            if m > 0:
                deflator *= 1.0 + cy_inf
                raw_target_net = (ann_burn * deflator) / 12.0
                # DA-A03: In Real CPI mode, index Social Security COLA along the cohort's historical CPI path
                ssn_gross_c = (sc["ssn_gross_real_base"] * deflator) if inflation_mode == "real_cpi" else sc["ssn_gross_flat"]
                step_res = _step_sim_month(
                    sim_assets,
                    state_map,
                    sc["iso"],
                    sc["full_lbl"],
                    0.0,
                    0.0,
                    raw_target_net,
                    inc_unv,
                    bear_order if cy_bear else active_order,
                    record_stats=False,
                    asset_monthly_rates=cy_rates,
                    scheduled_actions=actions,
                    include_scheduled_actions=inc_sa,
                    deflator=deflator,
                    ssn_gross_mo=ssn_gross_c,
                    matured_roll_rate_pct=cy_tbill,
                )
                tgt = step_res["targetMonthlyBurn"]
                if depleted_month is None and tgt > 1.0 and step_res["monthlyBurn"] < tgt - 1.0:
                    depleted_month = m
                if m <= p_milestone_month:
                    cohort_ms_gross_real += step_res["monthlyGrossSold"] / deflator
                    cohort_ms_tax_real += step_res["monthlyTax"] / deflator

            if m in cp_nom or m == p_milestone_month:
                total = _summarize_state_buckets(sim_assets, state_map, inc_unv)["totalVal"]
                if m in cp_nom:
                    cp_nom[m].append(total)
                    cp_real[m].append(total / deflator)
                    line_nom.append({"monthIdx": m, "nominal": round(total, 2), "real": round(total / deflator, 2)})
                if m == p_milestone_month:
                    ms_real_vals.append(total / deflator)
                    ms_nom_vals.append(total)
                    ms_tax_real_vals.append(cohort_ms_tax_real)
                    ms_eff_tax_vals.append(
                        (cohort_ms_tax_real / cohort_ms_gross_real * 100.0) if cohort_ms_gross_real > 0 else 0.0
                    )

        reached_h = run_months >= horizon
        reached_j85 = run_months >= p_milestone_month
        survived_h = depleted_month is None
        survived_j85 = depleted_month is None or depleted_month > p_milestone_month
        if reached_h:
            full_cohorts += 1
            full_success += 1 if survived_h else 0
        if reached_j85:
            j85_cohorts += 1
            j85_success += 1 if survived_j85 else 0

        rec = {
            "startYear": Y,
            "runMonths": run_months,
            "depletedMonth": depleted_month,
            "endNominal": line_nom[-1]["nominal"] if line_nom else 0.0,
            "endReal": line_nom[-1]["real"] if line_nom else 0.0,
            "spotlight": Y in spotlight,
            "line": line_nom,
        }
        cohort_out.append(rec)
        if Y in spotlight:
            named_out.append(rec)

    band: list[dict[str, Any]] = []
    for cp in checkpoints:
        nvals = sorted(cp_nom[cp])
        rvals = sorted(cp_real[cp])
        if not nvals:
            continue
        band.append({
            "monthIdx": cp,
            "yearsOut": round(cp / 12.0, 1),
            "nCohorts": len(nvals),
            "nominal": {qkey[q]: round(_mc_percentile(nvals, q), 2) for q in qs},
            "real": {qkey[q]: round(_mc_percentile(rvals, q), 2) for q in qs},
        })

    ranked = [c for c in cohort_out if c["runMonths"] >= horizon] or cohort_out
    best = max(ranked, key=lambda c: c["endReal"], default=None)
    worst = min(ranked, key=lambda c: c["endReal"], default=None)

    ms_sorted = sorted(ms_real_vals)
    ms_nom_coh_sorted = sorted(ms_nom_vals)
    ms_tax_sorted = sorted(ms_tax_real_vals)
    ms_eff_sorted = sorted(ms_eff_tax_vals)
    p50_eff_tax = round(_mc_percentile(ms_eff_sorted, 0.50), 1) if ms_eff_sorted else 0.0
    p50_tax_real = round(_mc_percentile(ms_tax_sorted, 0.50), 2) if ms_tax_sorted else 0.0

    coh_dep_vals = sorted(
        float(c["depletedMonth"] if c["depletedMonth"] is not None else (horizon + 1))
        for c in ranked
    )
    coh_med_dep = _mc_percentile(coh_dep_vals, 0.50) if coh_dep_vals else float(horizon + 1)
    coh_med_years_solvent = round(horizon / 12.0, 1) if coh_med_dep > horizon else round(coh_med_dep / 12.0, 1)

    return {
        "available": True,
        "center_mode": center_mode,
        "inflation_mode": inflation_mode,
        "horizon_months": horizon,
        "median_years_solvent": coh_med_years_solvent,
        f"{p_id}{p_ma}_month": p_milestone_month,
        "ms_month": p_milestone_month,
        "primary_milestone_month": p_milestone_month,
        "data_range": [first_y, last_y],
        "start_year_from": y_from,
        "start_year_to": y_to,
        "n_cohorts": len(cohort_out),
        f"success_to_{p_id}{p_ma}_pct": round(100.0 * j85_success / j85_cohorts, 1) if j85_cohorts else None,
        f"success_to_{p_id}{p_ma}_n": [j85_success, j85_cohorts],
        "success_to_ms_pct": round(100.0 * j85_success / j85_cohorts, 1) if j85_cohorts else None,
        "success_to_ms_n": [j85_success, j85_cohorts],
        "success_to_primary_milestone_pct": round(100.0 * j85_success / j85_cohorts, 1) if j85_cohorts else None,
        "success_full_pct": round(100.0 * full_success / full_cohorts, 1) if full_cohorts else None,
        "success_full_n": [full_success, full_cohorts],
        "ms_real": {qkey[q]: round(_mc_percentile(ms_sorted, q), 2) for q in qs} if ms_sorted else {"p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0},
        "ms_nominal": {qkey[q]: round(_mc_percentile(ms_nom_coh_sorted, q), 2) for q in qs} if ms_nom_coh_sorted else {"p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0},
        "tax_to_ms": {
            "net_kept_pct": round(100.0 - p50_eff_tax, 1),
            "eff_tax_pct": p50_eff_tax,
            "cum_tax_real": round(p50_tax_real),
        },
        "best_cohort": {"startYear": best["startYear"], "endReal": best["endReal"]} if best else None,
        "worst_cohort": {"startYear": worst["startYear"], "endReal": worst["endReal"], "depletedMonth": worst["depletedMonth"]} if worst else None,
        "band": band,
        "cohorts": cohort_out,
        "named": named_out,
    }


def build_action_summary(eff: dict[str, Any]) -> dict[str, Any]:
    """Summarizes hypothetical products and scheduled actions for instant Scenario Card badges & pills."""
    hyps = eff.get("simHypotheticalAssets") or []
    acts = eff.get("simScheduledActions") or []
    inc_sa = bool(eff.get("simIncludeScheduledActions", True))
    active_acts = acts if inc_sa else []
    return {
        "hypothetical_count": len(hyps),
        "hypothetical_labels": [str(h.get("label") or h.get("id") or "") for h in hyps if isinstance(h, dict)],
        "actions_included": inc_sa,
        "transfers": sum(1 for a in active_acts if isinstance(a, dict) and a.get("type") == "transfer"),
        "deposits": sum(1 for a in active_acts if isinstance(a, dict) and a.get("type") == "deposit"),
        "withdrawals": sum(1 for a in active_acts if isinstance(a, dict) and a.get("type", "withdrawal") == "withdrawal"),
    }


def evaluate_active_model(
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
    rollovers: list[dict[str, Any]] | None = None,
    reoptimize_order: bool = False,
    apply_non_goog_rate_mode: str = "none",
    uma_rate_mode: str = "ytd_statement",
    seed: int = 1,
) -> dict[str, Any]:
    """
    Evaluates a Scenario through its saved `simActiveModel` projection lens (`single_path`, `stress`,
    `range`, or `historical`) and returns both detailed engine output and canonical `kpi_summary`.
    """
    eff = build_effective_settings(payload, settings)
    active_lens = str(eff.get("simActiveModel") or "single_path").strip().lower()
    if active_lens not in ("single_path", "stress", "range", "historical"):
        active_lens = "single_path"

    base_sim = run_simulation(
        payload,
        eff,
        rollovers=rollovers,
        reoptimize_order=reoptimize_order,
        apply_non_goog_rate_mode=apply_non_goog_rate_mode,
        uma_rate_mode=uma_rate_mode,
        stress_enabled=(active_lens == "stress"),
    )
    eff = base_sim["settings"]
    sol = base_sim["solvency"]
    years_solvent = 100.0 if sol["sustains_100_years"] else float((sol.get("depleted_at") or {}).get("years") or 0.0)
    ms = base_sim["milestones"].get("investor_1_milestone") or base_sim["milestones"]["year_40"]
    ms_idx = int(ms.get("month_idx") or SIM_TOTAL_MONTHS)
    action_summary = build_action_summary(eff)

    mc_result: dict[str, Any] | None = None
    cohort_result: dict[str, Any] | None = None

    if active_lens == "range":
        mc_result = run_monte_carlo(
            payload,
            eff,
            n_paths=int(eff.get("mcSimulations") or 500),
            seed=seed,
            rollovers=rollovers,
        )
        ms_q = mc_result["ms_real"]
        kpi_summary = {
            "active_lens": "range",
            "age_85_real": {
                "has_quantiles": True,
                "value": round(ms_q["p50"]),
                "p25": round(ms_q["p25"]),
                "p50": round(ms_q["p50"]),
                "p75": round(ms_q["p75"]),
            },
            "solvency": {
                "funded_to_85_pct": float(mc_result["success_to_ms_pct"]),
                "years_solvent": float(mc_result.get("median_years_solvent", years_solvent)),
            },
            "tax_to_85": mc_result["tax_to_ms"],
            "action_summary": action_summary,
        }
    elif active_lens == "historical":
        cohort_result = run_historical_cohorts(
            payload,
            eff,
            opts={
                "center_mode": eff.get("cohortCenterMode", "anchor"),
                "inflation_mode": eff.get("cohortInflationMode", "real_cpi"),
                "start_year_from": eff.get("cohortStartFrom", 1947),
            },
            rollovers=rollovers,
        )
        if cohort_result.get("available"):
            ms_q = cohort_result["ms_real"]
            kpi_summary = {
                "active_lens": "historical",
                "age_85_real": {
                    "has_quantiles": True,
                    "value": round(ms_q["p50"]),
                    "p25": round(ms_q["p25"]),
                    "p50": round(ms_q["p50"]),
                    "p75": round(ms_q["p75"]),
                },
                "solvency": {
                    "funded_to_85_pct": float(cohort_result.get("success_to_ms_pct") or 0.0),
                    "years_solvent": float(cohort_result.get("median_years_solvent", years_solvent)),
                },
                "tax_to_85": cohort_result["tax_to_ms"],
                "action_summary": action_summary,
            }
        else:
            active_lens = "single_path"
            cohort_result = None

    if active_lens in ("single_path", "stress"):
        val_85 = float(ms["real_today_usd"])
        eff_tax = round(float(ms.get("effective_tax_rate_pct") or 0.0), 1)
        dep_info = sol.get("depleted_at")
        funded_pct = 100.0 if (dep_info is None or int(dep_info.get("monthIdx", 9999)) > ms_idx) else 0.0
        kpi_summary = {
            "active_lens": active_lens,
            "age_85_real": {
                "has_quantiles": False,
                "value": round(val_85),
                "p25": round(val_85),
                "p50": round(val_85),
                "p75": round(val_85),
            },
            "solvency": {
                "funded_to_85_pct": funded_pct,
                "years_solvent": years_solvent,
            },
            "tax_to_85": {
                "net_kept_pct": round(100.0 - eff_tax, 1),
                "eff_tax_pct": eff_tax,
                "cum_tax_real": round(float(ms["cum_tax_real_usd"])),
            },
            "action_summary": action_summary,
        }

    return {
        **base_sim,
        "active_lens": active_lens,
        "kpi_summary": kpi_summary,
        "monte_carlo": mc_result,
        "historical_cohorts": cohort_result,
    }


def _scale_payload_for_portfolio_value(
    payload: dict[str, Any],
    target_portfolio_usd: float,
    base_port_val: float,
) -> dict[str, Any]:
    """Returns a shallow payload copy whose vested `retirement_sim_assets` are scaled proportionally to `target_portfolio_usd`."""
    alpha = max(0.0, float(target_portfolio_usd) / max(1.0, float(base_port_val)))
    scaled_assets: list[dict[str, Any]] = []
    for a in (payload.get("retirement_sim_assets") or []):
        a_c = dict(a)
        if not a.get("is_unvested_goog"):
            a_c["current_value"] = round(float(a.get("current_value") or 0.0) * alpha, 2)
            if a.get("cost_basis") is not None:
                a_c["cost_basis"] = round(float(a["cost_basis"]) * alpha, 2)
            if a.get("shares") is not None:
                a_c["shares"] = round(float(a["shares"]) * alpha, 6)
            if isinstance(a.get("tranches"), list):
                scaled_tranches: list[dict[str, Any]] = []
                for tr in a["tranches"]:
                    if not isinstance(tr, dict):
                        continue
                    tr_c = dict(tr)
                    if tr_c.get("current_value") is not None:
                        tr_c["current_value"] = round(float(tr_c["current_value"]) * alpha, 2)
                    if tr_c.get("cost_basis") is not None:
                        tr_c["cost_basis"] = round(float(tr_c["cost_basis"]) * alpha, 2)
                    if tr_c.get("shares") is not None:
                        tr_c["shares"] = round(float(tr_c["shares"]) * alpha, 6)
                    scaled_tranches.append(tr_c)
                a_c["tranches"] = scaled_tranches
        scaled_assets.append(a_c)
    out_payload = dict(payload)
    out_payload["retirement_sim_assets"] = scaled_assets
    return out_payload


def solve_simulation(
    payload: dict[str, Any],
    solve_for: str = "primary_growth",
    target_metric: str | None = None,
    target_value: float = 2_000_000.0,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Bisection solver finding the exact model parameter (`primary_growth`, `broad_equity`, `all_holdings`,
    any `<forecast_key>`, `simAnnualBurn`, `simInflationPct`, `initial_portfolio_value`, plus legacy aliases)
    that achieves `target_value` on `target_metric` (dollar balance or `solvency_pct`), honoring `simActiveModel`.
    """
    proj_cfg = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    primary_sym = str(proj_cfg.get("primary_equity_symbol") or "").strip().upper()
    if not primary_sym and isinstance(proj_cfg.get("symbols"), list) and proj_cfg["symbols"]:
        primary_sym = str(proj_cfg["symbols"][0] or "").strip().upper()
    base_eff = build_effective_settings(payload, settings)
    known_fks = set((base_eff.get("holdingForecasts") or {}).keys()) | {
        str(a.get("forecast_key") or a["id"]) for a in (payload.get("retirement_sim_assets") or [])
    }

    port_start_val = sum(
        float(a.get("current_value") or 0.0)
        for a in (payload.get("retirement_sim_assets") or [])
        if not a.get("is_unvested_goog")
    )
    if port_start_val <= 0.0:
        port_start_val = sum(float(a.get("current_value") or 0.0) for a in (payload.get("retirement_sim_assets") or []))
    burn_hi = max(5_000_000.0, round(port_start_val * 0.25, -3))
    port_lo = max(10_000.0, round(port_start_val * 0.01, -3))
    port_hi = max(100_000.0, round(port_start_val * 3.0, -3))
    portfolio_value_aliases = {
        "initial_portfolio_value",
        "starting_portfolio_value",
        "portfolio_value",
        "portfolio_dollar_value",
    }
    bounds_map: dict[str, tuple[float, float, bool, float]] = {
        "primary_growth": (-20.0, 20.0, True, 0.1),
        "primaryGrowthPct": (-20.0, 20.0, True, 0.1),
        "diversified_growth": (-20.0, 20.0, True, 0.1),
        "diversifiedGrowthPct": (-20.0, 20.0, True, 0.1),
        "GOOG": (-20.0, 20.0, True, 0.1),
        "single_stock": (-20.0, 20.0, True, 0.1),
        "broad_equity": (-20.0, 20.0, True, 0.1),
        "all_holdings": (-20.0, 20.0, True, 0.1),
        "futureGoogGrowthPct": (-20.0, 20.0, True, 0.1),
        "futureNonGoogGrowthPct": (-20.0, 20.0, True, 0.1),
        "simAnnualBurn": (10_000.0, burn_hi, False, 1000.0),
        "simInflationPct": (0.0, 6.0, False, 0.1),
        **{k: (port_lo, port_hi, True, 1000.0) for k in portfolio_value_aliases},
    }
    if primary_sym:
        bounds_map[primary_sym] = (-20.0, 20.0, True, 0.1)
    if solve_for not in bounds_map and solve_for in known_fks:
        bounds_map[solve_for] = (-20.0, 20.0, True, 0.1)
    if solve_for not in bounds_map:
        raise ValueError(
            f"Unsupported solve_for={solve_for!r}. Choose from ['primary_growth', 'broad_equity', 'all_holdings', 'simAnnualBurn', 'simInflationPct', 'initial_portfolio_value'] or a holding forecast_key."
        )

    inv_meta = _get_investors_meta(payload)
    p_inv = inv_meta[0] if inv_meta else {"id": "investor_1", "milestone_age": 85}
    if not target_metric:
        target_metric = f"{p_inv['id']}_{p_inv['milestone_age']}_real"

    lo, hi, increasing, step = bounds_map[solve_for]
    is_portfolio_solve = solve_for in portfolio_value_aliases

    def _apply_candidate(val: float) -> tuple[dict[str, Any], dict[str, Any]]:
        if is_portfolio_solve:
            return _scale_payload_for_portfolio_value(payload, val, port_start_val), copy.deepcopy(base_eff)
        cand = copy.deepcopy(base_eff)
        if solve_for in ("simAnnualBurn", "simInflationPct"):
            cand[solve_for] = val
            return payload, cand
        hf = copy.deepcopy(cand.get("holdingForecasts") or {})
        # DA-A09: Seed sparse `holdingForecasts` from resolved asset defaults so solving for
        # `broad_equity` or `all_holdings` mutates every matching bucket even when `hf` started as `{}`.
        for a in payload.get("retirement_sim_assets") or []:
            fk_a = str(a.get("forecast_key") or a["id"])
            if fk_a not in hf:
                res_a = resolve_asset_forecast(a, base_eff, primary_sym)
                hf[fk_a] = {"growth_pct": res_a["growth_pct"], "risk_class": res_a["risk_class"]}
        if solve_for in ("primary_growth", "primaryGrowthPct", "GOOG", "single_stock", "futureGoogGrowthPct") or (primary_sym and solve_for == primary_sym):
            if primary_sym:
                hf[primary_sym] = {"growth_pct": val, "risk_class": "single_stock"}
            for k, meta in list(hf.items()):
                if (meta or {}).get("risk_class") == "single_stock":
                    hf[k] = {"growth_pct": val, "risk_class": "single_stock"}
        elif solve_for in ("diversified_growth", "diversifiedGrowthPct", "broad_equity", "futureNonGoogGrowthPct"):
            fixed_ids = {
                a["id"] for a in (payload.get("retirement_sim_assets") or []) if a.get("rate_model") == "fixed"
            }
            for k, meta in list(hf.items()):
                if k not in fixed_ids and (meta or {}).get("risk_class") == "broad_equity":
                    hf[k] = {"growth_pct": val, "risk_class": "broad_equity"}
        elif solve_for == "all_holdings":
            for k, meta in list(hf.items()):
                rc = str((meta or {}).get("risk_class") or "broad_equity")
                if rc != "cash_fixed":
                    hf[k] = {"growth_pct": val, "risk_class": rc}
        else:
            prev_rc = str((hf.get(solve_for) or {}).get("risk_class") or "broad_equity")
            hf[solve_for] = {"growth_pct": val, "risk_class": prev_rc}
        cand["holdingForecasts"] = hf
        return payload, cand

    active_lens_base = str(base_eff.get("simActiveModel") or "single_path").strip().lower()
    full_mc_paths = int(base_eff.get("mcSimulations") or 300)
    coarse_mc_paths = min(full_mc_paths, 150)
    tm_norm = target_metric.strip().lower()
    is_solvency_metric = tm_norm in ("solvency_pct", "funded_to_ms_pct", "funded_pct", "solvency", "success_to_ms_pct")

    def eval_metric(
        val: float,
        *,
        fast: bool = False,
        mc_paths_override: int | None = None,
    ) -> tuple[float, dict[str, Any] | None]:
        cand_payload, cand = _apply_candidate(val)
        tm = tm_norm
        is_nom = tm.endswith("_nominal")
        val_field = "nominal_usd" if is_nom else "real_today_usd"
        prefix = tm[: -len("_nominal")] if is_nom else (tm[: -len("_real")] if tm.endswith("_real") else tm)

        # DA2-11: Support BOTH real (`ms_real`/`ending_real`) AND nominal (`ms_nominal`/`ending_nominal`
        # & `band[-1]["nominal"]`) goal-seeking when `simActiveModel` is `range` (Monte Carlo) or `historical`.
        ms_q_key = "ms_nominal" if is_nom else "ms_real"
        end_q_key = "ending_nominal" if is_nom else "ending_real"
        band_sp_key = "nominal" if is_nom else "real"

        if fast and active_lens_base == "range":
            is_yr40 = prefix in ("yr_40", "year_40")
            n_p = mc_paths_override or full_mc_paths
            cand["mcSimulations"] = n_p
            mc_fast = run_monte_carlo(
                cand_payload,
                cand,
                n_paths=n_p,
                seed=1,
                stop_at_milestone=(not is_yr40),
            )
            if is_solvency_metric:
                return float(mc_fast.get("success_to_ms_pct") or 0.0), None
            q_src = mc_fast.get(ms_q_key) or {}
            end_src = mc_fast.get(end_q_key) or {}
            if tm in ("age_85_p25", "p25"):
                return float(q_src.get("p25", 0.0)), None
            if tm in ("age_85_p75", "p75"):
                return float(q_src.get("p75", 0.0)), None
            if is_yr40:
                return float(end_src.get("p50", 0.0)), None
            return float(q_src.get("p50", 0.0)), None

        res = evaluate_active_model(cand_payload, cand, seed=1)
        active_lens = res["active_lens"]
        if active_lens in ("range", "historical"):
            lens_obj = (res.get("monte_carlo") or {}) if active_lens == "range" else (res.get("historical_cohorts") or {})
            if is_solvency_metric:
                return float(lens_obj.get("success_to_ms_pct") or 0.0), res
            q_src = lens_obj.get(ms_q_key)
            end_src = lens_obj.get(end_q_key)
            if not end_src and lens_obj.get("band"):
                end_src = (lens_obj["band"][-1] or {}).get(band_sp_key)
            if tm in ("age_85_p25", "p25"):
                return float((q_src or {}).get("p25", 0.0)), res
            if tm in ("age_85_p50", "p50"):
                return float((q_src or {}).get("p50", 0.0)), res
            if tm in ("age_85_p75", "p75"):
                return float((q_src or {}).get("p75", 0.0)), res
            if prefix in ("yr_40", "year_40") and end_src:
                return float(end_src.get("p50", 0.0)), res
            if q_src and prefix not in ("yr_40", "year_40"):
                return float(q_src.get("p50", 0.0)), res

        if is_solvency_metric:
            return float((res.get("kpi_summary") or {}).get("solvency", {}).get("funded_to_85_pct") or 0.0), res

        ms = res["milestones"]
        if prefix in ("yr_40", "year_40"):
            m_val = ms["year_40"][val_field]
        elif f"{prefix}th" in ms:
            m_val = ms[f"{prefix}th"][val_field]
        elif prefix in ms:
            m_val = ms[prefix][val_field]
        elif f"{prefix}_milestone" in ms:
            m_val = ms[f"{prefix}_milestone"][val_field]
        else:
            raise ValueError(f"Unknown target_metric={target_metric!r}")
        return float(m_val), res

    orig_lo, orig_hi = lo, hi
    val_lo, _ = eval_metric(lo, fast=True, mc_paths_override=coarse_mc_paths)
    val_hi, _ = eval_metric(hi, fast=True, mc_paths_override=coarse_mc_paths)
    f_min, f_max = (min(val_lo, val_hi), max(val_lo, val_hi))
    converged = True
    note = ""
    move_tol = 0.05 if is_solvency_metric else max(1.0, 0.0005 * max(abs(f_min), abs(f_max)))
    bound_tol = 0.05 if is_solvency_metric else 1.0
    if abs(val_hi - val_lo) < move_tol:
        converged = False
        if is_solvency_metric:
            note = (
                f"The {target_metric} metric stays at {f_min:.1f}%–{f_max:.1f}% across the full {solve_for} range; "
                f"no single value hits {target_value:.1f}%."
            )
        else:
            note = (
                f"The {target_metric} metric barely moves across the full {solve_for} range "
                f"(${f_min:,.0f}–${f_max:,.0f}); no single value hits ${target_value:,.0f}."
            )
    elif target_value < f_min - bound_tol or target_value > f_max + bound_tol:
        converged = False
        closest = lo if abs(val_lo - target_value) <= abs(val_hi - target_value) else hi
        if is_solvency_metric:
            note = (
                f"Target {target_value:.1f}% is outside the achievable range for {solve_for} "
                f"[{lo:g}, {hi:g}] -> solvency spans {f_min:.1f}%–{f_max:.1f}%. "
                f"Closest at {solve_for}={closest:g}."
            )
        else:
            note = (
                f"Target ${target_value:,.0f} is outside the achievable range for {solve_for} "
                f"[{lo:g}, {hi:g}] -> metric spans ${f_min:,.0f}–${f_max:,.0f}. "
                f"Closest at {solve_for}={closest:g}."
            )

    iters = 0
    val_tol = 0.25 if is_solvency_metric else max(250.0, 0.0015 * abs(target_value))
    step_stop = max(step * 5.0, 0.005 * port_start_val) if is_portfolio_solve else (step / 10.0)
    max_solve_iters = 16 if (is_portfolio_solve or is_solvency_metric) else 20
    solved_exact: float
    if converged:
        f_lo = val_lo - target_value
        f_hi = val_hi - target_value
        side = 0
        solved_exact = 0.5 * (lo + hi)
        while (hi - lo) > step_stop and iters < max_solve_iters:
            iters += 1
            if (not is_solvency_metric) and iters % 4 != 0 and abs(f_hi - f_lo) > 1e-9:
                sec = lo - f_lo * (hi - lo) / (f_hi - f_lo)
                pad = 0.06 * (hi - lo)
                mid = max(lo + pad, min(hi - pad, sec))
            else:
                mid = 0.5 * (lo + hi)
            val_mid, _ = eval_metric(mid, fast=True, mc_paths_override=coarse_mc_paths)
            f_mid = val_mid - target_value
            solved_exact = mid
            if abs(f_mid) <= val_tol:
                break
            if (f_lo <= 0.0 <= f_mid) or (f_mid <= 0.0 <= f_lo):
                hi = mid
                f_hi = f_mid
                if side == -1:
                    f_lo *= 0.5
                side = -1
            else:
                lo = mid
                f_lo = f_mid
                if side == 1:
                    f_hi *= 0.5
                side = 1
        else:
            solved_exact = 0.5 * (lo + hi)

        # Local 2-point secant polish on full_mc_paths when range lens used coarse_mc_paths
        if active_lens_base == "range" and full_mc_paths > coarse_mc_paths and (not is_solvency_metric):
            span = max(step * 2.0, 0.025 * (orig_hi - orig_lo))
            x1 = max(orig_lo, solved_exact - span)
            x2 = min(orig_hi, solved_exact + span)
            iters += 2
            y1, _ = eval_metric(x1, fast=True, mc_paths_override=full_mc_paths)
            y2, _ = eval_metric(x2, fast=True, mc_paths_override=full_mc_paths)
            if abs(y2 - y1) > 1e-6:
                sec_full = x1 + (target_value - y1) * (x2 - x1) / (y2 - y1)
                solved_exact = max(orig_lo, min(orig_hi, sec_full))
                # 1 optional refinement step if residual at interpolated point exceeds 0.4%
                y_chk, _ = eval_metric(solved_exact, fast=True, mc_paths_override=full_mc_paths)
                iters += 1
                if abs(y_chk - target_value) > max(500.0, 0.004 * abs(target_value)):
                    if abs(y_chk - y1) > abs(y_chk - y2) and abs(y_chk - y1) > 1e-6:
                        sec2 = x1 + (target_value - y1) * (solved_exact - x1) / (y_chk - y1)
                    elif abs(y2 - y_chk) > 1e-6:
                        sec2 = solved_exact + (target_value - y_chk) * (x2 - solved_exact) / (y2 - y_chk)
                    else:
                        sec2 = solved_exact
                    solved_exact = max(orig_lo, min(orig_hi, sec2))
    else:
        solved_closest = lo if abs(val_lo - target_value) <= abs(val_hi - target_value) else hi
        lo = hi = solved_closest
        solved_exact = solved_closest

    achieved_val, final_sim = eval_metric(solved_exact, fast=False)
    assert final_sim is not None
    is_dollar_solve = solve_for == "simAnnualBurn" or is_portfolio_solve
    slider_step_val = round(solved_exact / 1000.0) * 1000 if is_dollar_solve else round(solved_exact, 1)
    if abs(slider_step_val - solved_exact) < 1e-6:
        slider_achieved = achieved_val
    elif (active_lens_base == "range" or is_portfolio_solve) and abs(slider_step_val - solved_exact) <= step * 0.55:
        # Linear local estimate avoids an extra 1000-path full MC pass just for rounded slider display
        slider_achieved = achieved_val
    else:
        slider_achieved, _ = eval_metric(slider_step_val, fast=True)
    residual = achieved_val - target_value

    out_dict: dict[str, Any] = {
        "solve_for": solve_for,
        "active_lens": final_sim.get("active_lens", "single_path"),
        "target_metric": target_metric,
        "target_value": target_value,
        "converged": converged,
        "note": note,
        "iterations": iters,
        "solved_value_exact": round(solved_exact, 4),
        "solved_value_2dec": round(solved_exact, 2),
        "solved_slider_step": slider_step_val,
        "achieved_at_exact": round(achieved_val, 2),
        "achieved_at_slider_step": round(slider_achieved, 2),
        "simulation_at_solved": final_sim,
    }
    if is_solvency_metric:
        out_dict["residual_pct_points"] = round(residual, 2)
        out_dict["achievable_range_pct"] = [round(f_min, 2), round(f_max, 2)]
        out_dict["residual_pct"] = round(residual, 2)
        out_dict["achievable_range_usd"] = [round(orig_lo, 2), round(orig_hi, 2)]
    else:
        out_dict["residual_usd"] = round(residual, 2)
        out_dict["residual_pct"] = round((residual / target_value * 100.0) if target_value else 0.0, 3)
        out_dict["achievable_range_usd"] = [round(f_min, 2), round(f_max, 2)]
    if is_portfolio_solve:
        out_dict["baseline_portfolio_value_usd"] = round(port_start_val, 2)
        out_dict["portfolio_scale_factor"] = round(solved_exact / max(1.0, port_start_val), 4)
    return out_dict


def query_positions(
    payload: dict[str, Any],
    has_maturity: bool | None = None,
    account_id: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """
    Returns flattened positions across all strategy rollups and child lots, optionally
    filtered by `has_maturity`, `account_id`, or `symbol`.
    """
    results: list[dict[str, Any]] = []
    for grp in payload.get("holdings_by_strategy") or []:
        children = grp.get("children") or []
        gid = str(grp.get("group_id") or "")
        is_muni_rollup = gid.startswith("rollup_munis_")
        is_uma_rollup = gid.startswith("rollup_uma_")
        is_cash_rollup = gid == "rollup_cash_all"
        is_plan_rollup = gid.startswith("rollup_plan_")

        if is_muni_rollup or is_uma_rollup or is_cash_rollup or is_plan_rollup:
            for c in children:
                mat_str = c.get("maturity_str")
                mat_iso = None
                if mat_str and "Matures:" in mat_str:
                    mat_iso = mat_str.split("Matures:")[1].strip().split()[0]
                results.append(
                    {
                        "category": grp.get("title"),
                        "symbol": c.get("symbol"),
                        "title": c.get("title"),
                        "subtitle": c.get("subtitle"),
                        "account_label": c.get("account_label"),
                        "quantity_str": c.get("quantity_str"),
                        "current_value": c.get("current_value"),
                        "pct_portfolio": c.get("pct_portfolio"),
                        "maturity_date": mat_iso,
                        "maturity_str": mat_str,
                        "growth_str": c.get("growth_str"),
                    }
                )
        else:
            mat_str = grp.get("maturity_str")
            mat_iso = None
            if mat_str and "Matures:" in mat_str:
                mat_iso = mat_str.split("Matures:")[1].strip().split()[0]
            results.append(
                {
                    "category": "Direct Position / Contract",
                    "symbol": grp.get("symbol"),
                    "title": grp.get("title"),
                    "subtitle": grp.get("subtitle"),
                    "account_label": grp.get("account_label"),
                    "quantity_str": grp.get("quantity_str"),
                    "current_value": grp.get("current_value"),
                    "pct_portfolio": grp.get("pct_portfolio"),
                    "maturity_date": mat_iso,
                    "maturity_str": mat_str,
                    "growth_str": grp.get("growth_str"),
                }
            )

    if has_maturity is True:
        results = [r for r in results if r.get("maturity_date")]
        results.sort(key=lambda r: (str(r.get("maturity_date") or "9999-99-99"), -float(r.get("current_value") or 0)))
    elif has_maturity is False:
        results = [r for r in results if not r.get("maturity_date")]

    if account_id:
        q_acct = account_id.strip().lower()
        results = [r for r in results if q_acct in str(r.get("account_label") or "").lower()]

    if symbol:
        q_sym = symbol.strip().lower()
        results = [
            r
            for r in results
            if q_sym in str(r.get("symbol") or "").lower() or q_sym in str(r.get("title") or "").lower()
        ]

    return results
