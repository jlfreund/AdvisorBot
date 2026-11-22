#!/usr/bin/env python3
"""
Entity & Synonym Registry for the Privacy Shield (Case B) and Demo Mode (Case A).

Auto-discovers personal, custodian, account, equity, fund, carrier, contract, and scenario
entities from `project.json`, `portfolio.sqlite`, and `Scenarios/*.json`, persists custom user
overrides per portfolio in `Portfolios/<slug>/privacy_registry.json`, and deterministically assigns
collision-free Demo Mode proper names.
"""

from __future__ import annotations

import copy
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import re
from typing import Any

from config import (
    get_active_portfolio_id,
    get_default_config_path,
    get_portfolio_dir,
    get_saves_dir,
    is_valid_portfolio_dir,
    load_config,
)

_POOLS_PATH = Path(__file__).resolve().parent / "demo_pools.json"
_KNOWN_MANAGERS = (
    "BlackRock", "BNY Mellon", "Capital Group", "Dimensional", "Fidelity",
    "Goldman Sachs", "Morgan Stanley", "Northern Trust", "Nuveen", "PIMCO",
    "Schwab", "State Street", "T. Rowe Price", "Vanguard", "Wellington", "Wells Fargo",
)
_KNOWN_CARRIER_PATTERNS: tuple[tuple[str, list[str]], ...] = (
    ("Allianz", ["Allianz Life", "Allianz"]),
    ("Jackson", ["Jackson National", "Jackson"]),
    ("Lincoln", ["Lincoln Financial", "Lincoln"]),
    ("MassMutual", ["MassMutual"]),
    ("Nationwide", ["Nationwide"]),
    ("NewYorkLife", ["New York Life", "NYL"]),
    ("Principal", ["Principal Financial", "Principal"]),
    ("Prudential", ["Prudential"]),
)
_ANNUITY_PRODUCT_STOPWORDS = frozenset({
    "FIXED", "INDEX", "INDEXED", "STRUCTURED", "CAPITAL", "POWER", "PLUS",
    "FOUNDATION", "FNDTN", "ADVANTAGE", "PEAK", "ANNUITY", "MYG", "MYGA",
    "MVA", "FIA", "RILA", "BUFFERED", "VARIABLE", "INCOME", "SELECT",
    "CHOICE", "SERIES", "CONTRACT", "DEFERRED", "GUARANTEE", "GUARANTEED",
})
_KNOWN_EQUITY_NAMES: dict[str, list[str]] = {
    "GOOG": ["Alphabet Inc. Class C", "Alphabet Stock Plan", "Alphabet Inc.", "Alphabet", "Google", "GOOGL", "GOOG", "GSU"],
    "GOOGL": ["Alphabet Inc.", "Alphabet", "Google", "GOOGL", "GOOG", "GSU"],
    "AAPL": ["Apple Inc.", "Apple", "AAPL"],
    "META": ["Meta Platforms, Inc.", "Meta Platforms", "Facebook", "META"],
    "NVDA": ["NVIDIA Corporation", "Nvidia", "NVDA"],
    "MSFT": ["Microsoft Corporation", "Microsoft", "MSFT"],
    "AMZN": ["Amazon.com, Inc.", "Amazon", "AMZN"],
}
_KNOWN_BANK_SYNONYMS: dict[str, list[str]] = {
    "wells fargo advisors": [
        "Wells Fargo Advisors", "Wells Fargo Bank", "Wells Fargo", "WellsFargoAdvisors", "WellsFargo", "WFA", "WFB", "WF"
    ],
    "wells fargo": [
        "Wells Fargo Advisors", "Wells Fargo Bank", "Wells Fargo", "WellsFargoAdvisors", "WellsFargo", "WFA", "WFB", "WF"
    ],
    "morgan stanley": [
        "Morgan Stanley at Work", "Morgan Stanley", "MorganStanley", "Morgan_Stanley", "MS"
    ],
    "charles schwab": ["Charles Schwab", "Schwab", "CS"],
    "fidelity": ["Fidelity Investments", "Fidelity"],
    "bank of america": ["Bank of America", "Merrill Lynch", "Merrill", "BofA"],
    "citibank": ["Citibank", "Citi Personal Wealth", "Citi"],
    "interactive brokers": ["Interactive Brokers", "IBKR"],
}


def _load_demo_pools() -> dict[str, Any]:
    raw = json.loads(_POOLS_PATH.read_text(encoding="utf-8"))
    # Normalize capitalization from json
    raw["surnames"] = [s[:1].upper() + s[1:] for s in raw.get("surnames", []) if s]
    raw["beneficiary_given_names"] = [s[:1].upper() + s[1:] for s in raw.get("beneficiary_given_names", []) if s]
    return raw


def default_privacy_config() -> dict[str, Any]:
    return {
        "version": 1,
        "ai_shield": {
            "mode": "identity_aliased",  # "off" | "local_only" | "identity_aliased" | "identity_and_values"
            "block_inapp_agy_cli": True,
            "dob_granularity": "year_month",  # "year_month" | "full" | "age_only" | "shift_years"
            "notes_policy": "translate",  # "translate" | "strip"
            "alias_products_and_tickers": True,
            "scale_factor": 0.25,
            "birthday_shift_years": 3,
        },
        "mcp_server": {
            "enabled": False,
        },
        "demo_mode": {
            "enabled": False,
            "demo_seed": "demo-4821",
            "scale_factor": 0.25,
            "round_to": 1000,
        },
        "entities": [],
    }


def _privacy_file_path(portfolio_id: str | None = None) -> Path | None:
    pdir = get_portfolio_dir(portfolio_id)
    if pdir is None:
        return None
    return pdir / "privacy_registry.json"


def _dedupe_preserve_longest(items: list[str]) -> list[str]:
    seen_low: set[str] = set()
    cleaned: list[str] = []
    for raw in items:
        val = str(raw or "").strip()
        if not val:
            continue
        low = val.lower()
        # Preserve distinct case only when <= 3 chars and same spelling wouldn't collide;
        # otherwise dedupe case-insensitively keeping first/most canonical form.
        if low in seen_low:
            continue
        seen_low.add(low)
        cleaned.append(val)
    cleaned.sort(key=lambda s: (-len(s), s.lower()))
    return cleaned


_HOUSEHOLD_STOPWORDS = {
    "the", "my", "our", "default", "demo", "sample", "household", "portfolio",
    "family", "personal", "retirement", "joint", "trust", "living", "revocable",
    "primary", "active", "new", "test", "client", "empty",
}

_GENERIC_COMPANY_WORDS = {
    "target", "general", "southern", "northern", "western", "eastern", "american",
    "national", "united", "first", "citizens", "regions", "principal", "discover",
    "progressive", "premier", "select", "summit", "pinnacle", "state", "public",
    "global", "international", "consolidated", "applied", "advanced", "digital",
    "realty", "equity", "trust", "fund", "income", "growth", "value", "total",
    "prime", "core", "next", "open", "fast", "best", "true", "pure", "ally",
    "block", "square", "match", "booking", "service", "waste", "waters", "ball",
    "key", "all", "now", "low", "cost", "cat", "net", "new", "one", "see", "top",
}

_GENERIC_HOLDING_LABELS = {
    "cash", "cash sweep", "cash sweeps", "cash & sweep", "cash and sweep",
    "money market", "municipal bonds", "muni bonds", "munis", "bonds",
    "fixed income", "fixed annuity", "indexed annuity", "rila", "annuity",
    "roth ira", "traditional ira", "trad ira", "rollover ira", "brokerage",
    "taxable", "direct indexing", "equities", "broad equity", "mutual funds",
    "stock plan", "equity plan",
}


def _chmod_0600(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_write_json_0600(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _chmod_0600(tmp_path)
    tmp_path.replace(path)
    _chmod_0600(path)


def _extract_household_surname(hh_name: str, cfg: dict[str, Any]) -> str:
    raw = str(hh_name or "").strip()
    if raw:
        core = re.sub(r"^the\s+", "", raw, flags=re.IGNORECASE).strip()
        m_pair = re.match(
            r"^[A-Za-z][A-Za-z'\-]*\s+(?:&|and)\s+[A-Za-z][A-Za-z'\-]*\s+([A-Za-z][A-Za-z'\-]+)"
            r"(?:\s+(?:Portfolio|Household|Family|Trust|Living\s+Trust))?\s*$",
            core,
            flags=re.IGNORECASE,
        )
        if m_pair and m_pair.group(1).lower() not in _HOUSEHOLD_STOPWORDS:
            return m_pair.group(1)
        m_hh = re.match(
            r"^([A-Za-z][A-Za-z'\-]+?)(?:'s|s)?\s+"
            r"(?:Portfolio|Household|Family|Living\s+Trust|Revocable\s+Trust|Rev\.?\s+Trust|Trust|Wealth|Estate)\b",
            core,
            flags=re.IGNORECASE,
        )
        if m_hh and m_hh.group(1).lower() not in _HOUSEHOLD_STOPWORDS:
            return m_hh.group(1)
        m_full = re.match(
            r"^[A-Za-z][A-Za-z'\-]+\s+([A-Za-z][A-Za-z'\-]+?)(?:'s|s)?\s+(?:Portfolio|Household|Family)\b",
            core,
            flags=re.IGNORECASE,
        )
        if m_full and m_full.group(1).lower() not in _HOUSEHOLD_STOPWORDS:
            return m_full.group(1)
        if re.match(r"^[A-Za-z][A-Za-z'\-]+$", core) and core.lower() not in _HOUSEHOLD_STOPWORDS:
            return core

    for group_key in ("investors", "family_members", "beneficiaries"):
        for person in cfg.get(group_key) or []:
            if not isinstance(person, dict):
                continue
            parts = str(person.get("name") or "").strip().split()
            if len(parts) >= 2:
                cand_last = re.sub(r"[^A-Za-z'\-]", "", parts[-1])
                if len(cand_last) >= 2 and cand_last.lower() not in (_HOUSEHOLD_STOPWORDS | {"jr", "sr", "ii", "iii", "iv"}):
                    return cand_last
    return ""


def _clean_equity_company_name(cname: str) -> str:
    s = str(cname or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(
            r"[\s,]+(?:Inc\.?|Incorporated|Corp\.?|Corporation|Co\.?|Company|Ltd\.?|Limited|PLC|N\.?V\.?|S\.?A\.?|"
            r"Holdings?|Group|Class\s+[A-Z0-9]+|Cl\s+[A-Z0-9]+|Common\s+Stock|Capital\s+Stock|Ordinary\s+Shares|ADR)\s*$",
            "",
            s,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
    if len(s) < 3 or s.lower() in _GENERIC_COMPANY_WORDS:
        return ""
    return s


def _account_id_variants(raw_aid: str) -> list[str]:
    aid = str(raw_aid or "").strip()
    if not aid:
        return []
    syns = [aid]
    if "-" in aid:
        syns.append(aid.replace("-", "_"))
    if "_" in aid and re.match(r"^(?:ACCT|ANN|WFA|MS)_\d{2,3}$", aid, flags=re.IGNORECASE):
        syns.append(aid.replace("_", "-"))
    if re.match(r"^(?:ACCT|ANN|WFA|MS)[-_]\d{2,3}$", aid, flags=re.IGNORECASE):
        return syns
    compact_id = re.sub(r"[^A-Za-z0-9]", "", aid)
    if compact_id and compact_id != aid and len(compact_id) >= 6:
        syns.append(compact_id)
    digits_only = re.sub(r"\D", "", aid)
    if len(digits_only) >= 4:
        last4 = digits_only[-4:]
        syns.extend([
            f"ending in {last4}",
            f"ending {last4}",
            f"acct {last4}",
            f"account {last4}",
            f"*{last4}",
            f"x{last4}",
            f"xxxx{last4}",
            f"...{last4}",
            f"Brokerage_{last4}",
            f"Brokerage {last4}",
            f"Brokerage-{last4}",
            f"_{last4}",
        ])
    return syns


def _inspect_portfolio_sources(portfolio_id: str | None = None) -> dict[str, Any]:
    """Gather accounts, holdings, sim_assets, preferences, account_map, and scenarios cleanly without heavy chart generation."""
    pid = portfolio_id or get_active_portfolio_id()
    pdir = get_portfolio_dir(pid)
    cfg = load_config(get_default_config_path(pid) if pid else None, portfolio_id=pid)
    accounts: dict[str, dict[str, Any]] = {}
    securities: list[dict[str, Any]] = []
    sim_assets: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    grant_ids: list[str] = []
    account_map: dict[str, dict[str, str]] = {"accounts": {}, "contracts": {}}
    holding_prefs: list[dict[str, Any]] = []

    if pdir is not None:
        amap_path = pdir / "account_map.json"
        if amap_path.is_file():
            try:
                from ingest.account_masking import load_account_map

                account_map = load_account_map(amap_path)
            except Exception:
                account_map = {"accounts": {}, "contracts": {}}

        db_path = pdir / "portfolio.sqlite"
        if db_path.is_file():
            try:
                from db.database import PortfolioDB

                with PortfolioDB(db_path) as db:
                    for r in db.conn.execute(
                        "SELECT account_id, account_name, institution, account_type, tax_status FROM accounts ORDER BY display_order ASC, account_id ASC"
                    ).fetchall():
                        aid_s = str(r["account_id"] or "")
                        inst_s = str(r["institution"] or "")
                        if aid_s.upper() == "SSA" or "social security" in inst_s.lower():
                            continue
                        accounts[aid_s] = {
                            "account_id": aid_s,
                            "display_name": str(r["account_name"] or ""),
                            "institution": inst_s,
                            "owner": str(r["account_name"] or ""),
                            "account_type": str(r["account_type"] or ""),
                            "tax_wrapper": str(r["tax_status"] or ""),
                        }
                    for r in db.conn.execute(
                        "SELECT security_id, symbol_or_code, canonical_name, asset_class, metadata_json FROM securities"
                    ).fetchall():
                        securities.append(
                            {
                                "security_id": str(r["security_id"]),
                                "symbol": str(r["symbol_or_code"] or ""),
                                "canonical_name": str(r["canonical_name"] or ""),
                                "asset_class": str(r["asset_class"] or ""),
                                "metadata_json": str(r["metadata_json"] or "{}"),
                            }
                        )
                    grant_ids = []
                    try:
                        rows = db.conn.execute(
                            "SELECT DISTINCT grant_id FROM gsu_tranches UNION SELECT DISTINCT grant_id FROM gsu_releases_history ORDER BY grant_id"
                        ).fetchall()
                        grant_ids = [str(r["grant_id"] or "").strip() for r in rows if str(r["grant_id"] or "").strip()]
                    except Exception:
                        grant_ids = []
                    try:
                        db_hp = db.get_holding_preferences()
                        for hk, hrow in (db_hp or {}).items():
                            if isinstance(hrow, dict):
                                holding_prefs.append(
                                    {
                                        "holding_key": str(hk),
                                        "friendly_name": str(hrow.get("friendly_name") or "").strip(),
                                        "statement_name": str(hrow.get("statement_name") or "").strip(),
                                        "account_id": str(hrow.get("account_id") or "").strip(),
                                        "security_id": str(hrow.get("security_id") or "").strip(),
                                        "institution": str(hrow.get("institution") or "").strip(),
                                    }
                                )
                    except Exception:
                        pass
            except Exception as exc:
                import logging

                logging.getLogger("privacy.registry").warning("DB inspect failed: %s", exc)

        # Read Scenarios/*.json and global_preferences.json directly
        for sub in ("Scenarios", "Saves"):
            sdir = pdir / sub
            if sdir.is_dir():
                gpref_fp = sdir / "global_preferences.json"
                if gpref_fp.is_file():
                    try:
                        gp_obj = json.loads(gpref_fp.read_text(encoding="utf-8"))
                        if isinstance(gp_obj, dict):
                            for map_key in ("friendly_names", "holding_preferences"):
                                raw_map = gp_obj.get(map_key)
                                if isinstance(raw_map, dict):
                                    for hk, entry in raw_map.items():
                                        if isinstance(entry, str):
                                            holding_prefs.append({"holding_key": str(hk), "friendly_name": entry.strip()})
                                        elif isinstance(entry, dict):
                                            holding_prefs.append(
                                                {
                                                    "holding_key": str(hk),
                                                    "friendly_name": str(entry.get("friendly_name") or "").strip(),
                                                    "statement_name": str(entry.get("statement_name") or "").strip(),
                                                    "account_id": str(entry.get("account_id") or "").strip(),
                                                    "security_id": str(entry.get("security_id") or "").strip(),
                                                    "institution": str(entry.get("institution") or "").strip(),
                                                }
                                            )
                    except Exception:
                        pass

                for fp in sorted(sdir.glob("*.json")):
                    if fp.name.startswith("_") or fp.name == "global_preferences.json":
                        continue
                    try:
                        obj = json.loads(fp.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    scenarios.append(
                        {
                            "id": str(obj.get("id") or ""),
                            "name": str(obj.get("name") or fp.stem),
                            "filename": fp.name,
                            "notes_md": str(obj.get("notes_md") or ""),
                        }
                    )
                break

        # Collect sim_asset / forecast_key rows if payload can be built quickly
        try:
            if db_path.is_file():
                from datetime import date
                from db.database import PortfolioDB
                from exporters.view_model import build_report_payload

                with PortfolioDB(db_path) as db:
                    payload = build_report_payload(db, cfg, date.today())
                sim_assets = list(payload.get("retirement_sim_assets") or [])
        except Exception:
            sim_assets = []

    return {
        "portfolio_id": pid or "default",
        "cfg": cfg,
        "accounts": accounts,
        "securities": securities,
        "grants": grant_ids,
        "sim_assets": sim_assets,
        "scenarios": scenarios,
        "account_map": account_map,
        "holding_preferences": holding_prefs,
    }


def _discover_entities(sources: dict[str, Any]) -> list[dict[str, Any]]:
    cfg: dict[str, Any] = sources["cfg"]
    accounts: dict[str, dict[str, Any]] = sources["accounts"]
    securities: list[dict[str, Any]] = sources["securities"]
    grants: list[str] = sources.get("grants") or []
    sim_assets: list[dict[str, Any]] = sources["sim_assets"]
    scenarios: list[dict[str, Any]] = sources["scenarios"]
    account_map: dict[str, dict[str, str]] = sources.get("account_map") or {"accounts": {}, "contracts": {}}
    holding_prefs: list[dict[str, Any]] = sources.get("holding_preferences") or []

    entities: list[dict[str, Any]] = []

    def _add(
        token: str,
        category: str,
        canonical: str,
        synonyms: list[str],
        *,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ent = {
            "token": token,
            "category": category,
            "canonical": canonical,
            "synonyms": _dedupe_preserve_longest([canonical] + synonyms),
            "demo_value": "",
            "demo_meta": {},
            "meta": meta or {},
            "auto_detected": True,
            "enabled": True,
        }
        entities.append(ent)
        return ent

    pref_texts = [
        f"{hp.get('friendly_name', '')} {hp.get('statement_name', '')}".strip()
        for hp in holding_prefs
        if hp.get("friendly_name") or hp.get("statement_name")
    ]

    # 1. Household
    hh_name = str(cfg.get("household_name") or "").strip()
    surname_guess = _extract_household_surname(hh_name, cfg)
    if hh_name:
        hh_syns = [hh_name]
        if surname_guess:
            hh_syns.extend([
                f"{surname_guess} Portfolio",
                f"{surname_guess} Household",
                f"{surname_guess} Family",
                surname_guess,
            ])
        _add("HOUSEHOLD_1", "household", hh_name, hh_syns, meta={"surname": surname_guess})

    # 2. Legal Trusts discovered in account display names / owners / holding preferences
    trust_names: list[str] = []
    trust_scan_texts = [
        *(acc.get("display_name") or "" for acc in accounts.values()),
        *(acc.get("owner") or "" for acc in accounts.values()),
        *pref_texts,
    ]
    for txt in trust_scan_texts:
        m_tr = re.search(r"([A-Za-z0-9 '&.\-]+?\b(?:Revocable|Rev\.?|Living|Family)?\s*Trust)\b", txt, flags=re.IGNORECASE)
        if m_tr:
            t_val = re.sub(r"\s+", " ", m_tr.group(1)).strip(" -—•")
            if t_val and t_val not in trust_names:
                trust_names.append(t_val)
    for idx, t_val in enumerate(trust_names, start=1):
        t_syns = [t_val]
        if surname_guess:
            t_syns.extend([f"{surname_guess} Rev Trust", f"{surname_guess} Family Trust"])
        for tr_kind in ("Revocable", "Living", "Family"):
            if tr_kind[:3].lower() in t_val.lower():
                t_syns.append(f"{tr_kind} Trust")
        _add(f"TRUST_{idx}", "trust", t_val, t_syns)

    # 3. Investors, 4. Beneficiaries, & additional family_members
    all_owner_texts = " | ".join(
        [acc.get("display_name", "") + " " + acc.get("owner", "") for acc in accounts.values()]
        + [sc.get("name", "") + " " + sc.get("notes_md", "") for sc in scenarios]
        + pref_texts
    )
    person_first_names: list[str] = []
    person_ent_by_key: dict[str, dict[str, Any]] = {}
    inv_idx = 0
    ben_idx = 0

    def _register_person(raw_p: dict[str, Any], default_cat: str) -> None:
        nonlocal inv_idx, ben_idx
        name = str(raw_p.get("name") or "").strip()
        if not name:
            return
        p_id = str(raw_p.get("id") or "").strip()
        bdate = str(raw_p.get("birth_date") or "").strip()
        first = name.split()[0]
        lookup_keys = [k.lower() for k in (name, first, p_id) if k]
        existing = next((person_ent_by_key[lk] for lk in lookup_keys if lk in person_ent_by_key), None)

        syns = [name, first, f"{first}s", f"{first}'s"]
        import unicodedata as _ud

        def _fold_ascii(val: str) -> str:
            rep = val.replace("ø", "o").replace("Ø", "O").replace("æ", "ae").replace("Æ", "AE").replace("ß", "ss")
            return "".join(ch for ch in _ud.normalize("NFKD", rep) if not _ud.combining(ch))

        ascii_name = _fold_ascii(name)
        ascii_first = _fold_ascii(first)
        if ascii_name and ascii_name != name:
            syns.append(ascii_name)
        if ascii_first and ascii_first != first:
            syns.extend([ascii_first, f"{ascii_first}s", f"{ascii_first}'s"])
        if surname_guess and surname_guess.lower() not in name.lower():
            syns.append(f"{first} {surname_guess}")
        if surname_guess:
            for m_full in re.finditer(
                rf"\b{re.escape(first)}\s+(?:[A-Za-z]\.?\s+)?{re.escape(surname_guess)}\b",
                all_owner_texts,
                flags=re.IGNORECASE,
            ):
                syns.append(re.sub(r"\s+", " ", m_full.group(0)).strip())
        if p_id and p_id.lower() != name.lower():
            syns.append(p_id)

        if existing is not None:
            existing["synonyms"] = _dedupe_preserve_longest(list(existing["synonyms"]) + syns)
            if bdate and not existing["meta"].get("birth_date"):
                existing["meta"]["birth_date"] = bdate
            if p_id and not existing["meta"].get("id"):
                existing["meta"]["id"] = p_id
            for lk in lookup_keys:
                person_ent_by_key.setdefault(lk, existing)
            return

        person_first_names.append(first)
        if default_cat == "investor":
            inv_idx += 1
            tok = f"INVESTOR_{inv_idx}"
        else:
            ben_idx += 1
            tok = f"BENEFICIARY_{ben_idx}"
        ent = _add(tok, default_cat, name, syns, meta={"first": first, "id": p_id, "birth_date": bdate})
        for lk in lookup_keys:
            person_ent_by_key.setdefault(lk, ent)

    for inv in cfg.get("investors") or []:
        if isinstance(inv, dict):
            _register_person(inv, "investor")

    for ben in cfg.get("beneficiaries") or []:
        if isinstance(ben, dict):
            _register_person(ben, "beneficiary")

    for fam in cfg.get("family_members") or []:
        if isinstance(fam, dict):
            role_low = str(fam.get("role") or "").strip().lower()
            is_inv = role_low == "investor" or bool(fam.get("is_ssn_contributor"))
            _register_person(fam, "investor" if is_inv else "beneficiary")

    # 5. Custodians (Banks)
    bank_token_by_norm: dict[str, str] = {}
    bank_idx = 0
    inst_counts: dict[str, int] = {}
    inst_canonical: dict[str, str] = {}
    for acc in accounts.values():
        inst = str(acc.get("institution") or "").strip()
        if not inst:
            continue
        key = inst.lower()
        inst_counts[key] = inst_counts.get(key, 0) + 1
        inst_canonical.setdefault(key, inst)
    for hp in holding_prefs:
        inst = str(hp.get("institution") or "").strip()
        if inst and "social security" not in inst.lower():
            key = inst.lower()
            inst_counts.setdefault(key, 0)
            inst_canonical.setdefault(key, inst)

    for inst_key, _cnt in sorted(inst_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        bank_idx += 1
        token = f"BANK_{bank_idx}"
        canon = inst_canonical[inst_key]
        bank_token_by_norm[inst_key] = token
        syns = list(_KNOWN_BANK_SYNONYMS.get(inst_key, [canon]))
        if syns == [canon]:
            for known_k, known_syns in _KNOWN_BANK_SYNONYMS.items():
                if known_k in inst_key:
                    syns = list(known_syns) + [canon]
                    break
        _add(token, "custodian", canon, syns, meta={"inst_key": inst_key})

    # 6. Accounts (including account_map.json raw <-> ACCT-XX pairs)
    amap_accts = {str(k).strip(): str(v).strip() for k, v in (account_map.get("accounts") or {}).items() if k and v}
    acct_ent_by_id: dict[str, dict[str, Any]] = {}
    acct_idx = 0
    matched_amap_raw: set[str] = set()
    for aid, acc in accounts.items():
        acct_idx += 1
        token = f"ACCT_{acct_idx}"
        syns = _account_id_variants(aid)
        for raw_k, masked_v in amap_accts.items():
            if aid == raw_k or aid == masked_v:
                matched_amap_raw.add(raw_k)
                syns.extend(_account_id_variants(raw_k))
                syns.extend(_account_id_variants(masked_v))
        inst_key = str(acc.get("institution") or "").strip().lower()
        bank_tok = bank_token_by_norm.get(inst_key, "")
        ent = _add(
            token,
            "account",
            aid,
            syns,
            meta={
                "bank_token": bank_tok,
                "owner": acc.get("owner") or "",
                "account_type": acc.get("account_type") or "",
            },
        )
        acct_ent_by_id[aid] = ent
        for s in ent["synonyms"]:
            acct_ent_by_id.setdefault(s, ent)

    for raw_k, masked_v in sorted(amap_accts.items()):
        if raw_k in matched_amap_raw or raw_k in acct_ent_by_id or masked_v in acct_ent_by_id:
            continue
        acct_idx += 1
        token = f"ACCT_{acct_idx}"
        syns = _account_id_variants(raw_k) + _account_id_variants(masked_v)
        ent = _add(token, "account", masked_v, syns, meta={"bank_token": "", "owner": "", "account_type": ""})
        acct_ent_by_id[raw_k] = ent
        acct_ent_by_id[masked_v] = ent

    # 7. Equities (Primary Grant Company + Tracked Symbols first so STOCK_1 stays deterministic, then all SQLite EQUITY securities)
    sym_idx = 0
    seen_syms: set[str] = set()
    equity_ent_by_sym: dict[str, dict[str, Any]] = {}
    stock_grants_syms = [
        str(c.get("symbol") or "").upper()
        for c in ((cfg.get("stock_grants") or {}).get("companies") or [])
        if c.get("symbol")
    ]
    ordered_syms: list[str] = []
    for s in [cfg.get("primary_equity_symbol"), *stock_grants_syms, *(cfg.get("symbols") or [])]:
        su = str(s or "").strip().upper()
        if su and su not in seen_syms:
            seen_syms.add(su)
            ordered_syms.append(su)

    for sym in ordered_syms:
        sym_idx += 1
        token = f"STOCK_{sym_idx}"
        syns = list(_KNOWN_EQUITY_NAMES.get(sym, [sym]))
        syns.append(f"TICKER:{sym}")
        for comp in (cfg.get("stock_grants") or {}).get("companies") or []:
            if str(comp.get("symbol") or "").upper() == sym:
                for fld in ("company_name", "short_name", "award_type"):
                    if comp.get(fld):
                        syns.append(str(comp[fld]))
        ent = _add(token, "equity", sym, syns, meta={"is_primary": sym == str(cfg.get("primary_equity_symbol") or "").upper()})
        equity_ent_by_sym[sym] = ent

    extra_eq_order: list[str] = []
    extra_eq_syns: dict[str, list[str]] = {}
    for sec in securities:
        sym = str(sec.get("symbol") or "").strip().upper()
        cname = str(sec.get("canonical_name") or "").strip()
        aclass = str(sec.get("asset_class") or "").strip().upper()
        sec_id = str(sec.get("security_id") or "").strip()
        if not sym or sym in ("USD", "CASH") or sym.startswith(("CASH:", "CONTRACT:", "CUSIP:")):
            continue
        if aclass not in ("EQUITY", "PUBLIC EQUITY", "STOCK"):
            continue
        cand_syns: list[str] = []
        if sec_id and sec_id.upper().startswith("TICKER:"):
            cand_syns.append(sec_id)
        else:
            cand_syns.append(f"TICKER:{sym}")
        if cname and cname.upper() != sym:
            cand_syns.append(cname)
            short_corp = _clean_equity_company_name(cname)
            if short_corp and short_corp.upper() != sym:
                cand_syns.append(short_corp)
        if sym in seen_syms:
            existing_eq = equity_ent_by_sym.get(sym)
            if existing_eq is not None and cand_syns:
                existing_eq["synonyms"] = _dedupe_preserve_longest(list(existing_eq["synonyms"]) + cand_syns)
            continue
        if sym not in extra_eq_syns:
            extra_eq_order.append(sym)
            extra_eq_syns[sym] = list(_KNOWN_EQUITY_NAMES.get(sym, [sym]))
        extra_eq_syns[sym].extend(cand_syns)

    for sym in extra_eq_order:
        seen_syms.add(sym)
        sym_idx += 1
        token = f"STOCK_{sym_idx}"
        ent = _add(token, "equity", sym, extra_eq_syns[sym], meta={"is_primary": False})
        equity_ent_by_sym[sym] = ent

    # 8. Mutual Funds & ETFs (e.g., 5-letter fund tickers ending in X)
    fund_syms: list[str] = []
    fund_names_by_sym: dict[str, list[str]] = {}
    fund_ent_by_sym: dict[str, dict[str, Any]] = {}
    for sec in securities:
        sym = str(sec.get("symbol") or "").strip().upper()
        cname = str(sec.get("canonical_name") or "").strip()
        aclass = str(sec.get("asset_class") or "").lower()
        if not sym or sym in seen_syms or sym.startswith("CASH") or sym.startswith("CONTRACT:"):
            continue
        if aclass in ("mutual_fund", "fund", "etf") or (len(sym) == 5 and sym.endswith("X")):
            if sym not in fund_syms:
                fund_syms.append(sym)
            fund_names_by_sym.setdefault(sym, []).extend([sym, cname])
    for sa in sim_assets:
        for txt in (sa.get("short_title") or "", sa.get("title") or ""):
            m_f = re.search(r"\b([A-Z]{4}X)\b", txt)
            if m_f:
                sym = m_f.group(1)
                if sym not in seen_syms:
                    if sym not in fund_syms:
                        fund_syms.append(sym)
                    fund_names_by_sym.setdefault(sym, []).append(sym)
                    if sa.get("short_title"):
                        fund_names_by_sym[sym].append(str(sa["short_title"]))
    for txt in pref_texts:
        m_f = re.search(r"\b([A-Z]{4}X)\b", txt)
        if m_f and m_f.group(1) not in seen_syms:
            sym = m_f.group(1)
            if sym not in fund_syms:
                fund_syms.append(sym)
            fund_names_by_sym.setdefault(sym, []).append(sym)

    for idx, sym in enumerate(fund_syms, start=1):
        ent = _add(f"FUND_{idx}", "fund", sym, fund_names_by_sym.get(sym, [sym]))
        fund_ent_by_sym[sym] = ent

    # 9. Asset Managers & Bond Brands
    mgr_text = " | ".join(
        [sec.get("canonical_name", "") for sec in securities]
        + [(sa.get("short_title") or "") + " " + (sa.get("title") or "") for sa in sim_assets]
        + pref_texts
    )
    mgr_idx = 0
    seen_mgr_keys: set[str] = set()
    for mgr in _KNOWN_MANAGERS:
        if mgr.lower() in {b.lower() for inst_s in bank_token_by_norm for b in _KNOWN_BANK_SYNONYMS.get(inst_s, [inst_s])}:
            continue
        if re.search(rf"\b{re.escape(mgr)}\b", mgr_text, flags=re.IGNORECASE):
            low = mgr.lower()
            if low in seen_mgr_keys:
                continue
            seen_mgr_keys.add(low)
            mgr_idx += 1
            syns = [mgr]
            _add(f"MGR_{mgr_idx}", "manager", mgr, syns)

    # 10. Annuity Carriers & Contracts (including account_map.json raw <-> ANN-XX pairs)
    annuity_blobs = [
        str(sec.get("security_id") or "") + " " + str(sec.get("canonical_name") or "")
        for sec in securities
        if "ann" in str(sec.get("asset_class") or "").lower()
        or "contract:" in str(sec.get("security_id") or "").lower()
    ] + [
        str(sa.get("id") or "") + " " + str(sa.get("forecast_key") or "") + " " + str(sa.get("short_title") or "") + " " + str(sa.get("title") or "")
        for sa in sim_assets
        if str(sa.get("id") or "").startswith("ann_") or "contract:" in str(sa.get("forecast_key") or "").lower()
    ] + pref_texts
    all_ann_txt = " | ".join(annuity_blobs)

    carrier_idx = 0
    seen_carrier_low: set[str] = set()
    for c_canon, c_syns in _KNOWN_CARRIER_PATTERNS:
        if any(re.search(rf"\b{re.escape(s)}\b", all_ann_txt, flags=re.IGNORECASE) for s in c_syns):
            carrier_idx += 1
            for s in c_syns:
                seen_carrier_low.add(s.lower())
            _add(f"CARRIER_{carrier_idx}", "carrier", c_canon, c_syns)

    # Dynamically extract issuer names from annuity security / bucket titles so any carrier is
    # protected without hardcoding portfolio-specific insurer names in source code.
    ann_titles = [
        str(sec.get("canonical_name") or "")
        for sec in securities
        if "ann" in str(sec.get("asset_class") or "").lower()
        or "contract:" in str(sec.get("security_id") or "").lower()
    ] + [
        str(sa.get("title") or "")
        for sa in sim_assets
        if str(sa.get("id") or "").startswith("ann_")
    ]
    for raw_t in ann_titles:
        clean_t = raw_t.split("—", 1)[0].split("(", 1)[0].strip()
        words = clean_t.split()
        issuer_words: list[str] = []
        for w in words:
            w_up = re.sub(r"[^A-Z0-9]", "", w.upper())
            if not w_up or w_up.isdigit() or w_up in _ANNUITY_PRODUCT_STOPWORDS:
                break
            issuer_words.append(w)
            if len(issuer_words) >= 3:
                break
        if issuer_words:
            full_issuer = " ".join(issuer_words)
            core_words = [
                w for w in issuer_words
                if w.lower() not in {"life", "insurance", "annuity", "financial", "company", "co", "co."}
            ]
            short_issuer = " ".join(core_words) if core_words else issuer_words[0]
            if full_issuer.lower() not in seen_carrier_low and short_issuer.lower() not in seen_carrier_low and len(short_issuer) >= 3:
                carrier_idx += 1
                syns = list(dict.fromkeys([full_issuer, short_issuer]))
                for s in syns:
                    seen_carrier_low.add(s.lower())
                _add(f"CARRIER_{carrier_idx}", "carrier", short_issuer, syns)

    amap_contracts = {str(k).strip(): str(v).strip() for k, v in (account_map.get("contracts") or {}).items() if k and v}
    contract_ids: list[str] = []
    for blob in annuity_blobs:
        for m_c in re.finditer(r"CONTRACT:([A-Za-z0-9-]+)", blob):
            cid = m_c.group(1)
            if cid not in contract_ids:
                contract_ids.append(cid)
        for m_a in re.finditer(r"\bann_[a-z0-9]+_([A-Za-z0-9-]{5,})\b", blob, flags=re.IGNORECASE):
            cid = m_a.group(1)
            if cid not in contract_ids:
                contract_ids.append(cid)

    contract_ent_by_id: dict[str, dict[str, Any]] = {}
    matched_amap_contracts: set[str] = set()
    contract_idx = 0
    for cid in contract_ids:
        contract_idx += 1
        syns = [f"CONTRACT:{cid}", cid]
        if "-" in cid:
            syns.append(cid.replace("-", "_"))
        for raw_c, masked_c in amap_contracts.items():
            if cid == raw_c or cid == masked_c:
                matched_amap_contracts.add(raw_c)
                syns.extend([f"CONTRACT:{raw_c}", raw_c, f"CONTRACT:{masked_c}", masked_c, masked_c.replace("-", "_")])
        ent = _add(f"CONTRACT_{contract_idx}", "contract", cid, syns)
        contract_ent_by_id[cid] = ent
        for s in ent["synonyms"]:
            contract_ent_by_id.setdefault(s, ent)

    for raw_c, masked_c in sorted(amap_contracts.items()):
        if raw_c in matched_amap_contracts or raw_c in contract_ent_by_id or masked_c in contract_ent_by_id:
            continue
        contract_idx += 1
        syns = [f"CONTRACT:{raw_c}", raw_c, f"CONTRACT:{masked_c}", masked_c, masked_c.replace("-", "_")]
        ent = _add(f"CONTRACT_{contract_idx}", "contract", masked_c, syns)
        contract_ent_by_id[raw_c] = ent
        contract_ent_by_id[masked_c] = ent

    # Attach or register custom holding_preferences friendly_name strings
    custom_pref_idx = 0
    for hp in holding_prefs:
        fname = str(hp.get("friendly_name") or "").strip()
        if not fname or len(fname) < 3 or fname.lower() in _GENERIC_HOLDING_LABELS:
            continue
        sec_id = str(hp.get("security_id") or "").strip()
        target_ent: dict[str, Any] | None = None
        if sec_id.upper().startswith("TICKER:"):
            sym_k = sec_id.split(":", 1)[1].strip().upper()
            target_ent = equity_ent_by_sym.get(sym_k) or fund_ent_by_sym.get(sym_k)
        elif sec_id.upper().startswith("CONTRACT:"):
            cid_k = sec_id.split(":", 1)[1].strip()
            target_ent = contract_ent_by_id.get(cid_k)
        if target_ent is not None:
            target_ent["synonyms"] = _dedupe_preserve_longest(list(target_ent["synonyms"]) + [fname])
            continue
        existing_syns_low = {
            str(s).strip().lower()
            for e in entities
            for s in ([e.get("canonical", "")] + list(e.get("synonyms") or []))
            if len(str(s).strip()) >= 3
        }
        if fname.lower() not in existing_syns_low and not any(es in fname.lower() for es in existing_syns_low):
            custom_pref_idx += 1
            _add(f"HOLDING_PREF_{custom_pref_idx}", "custom", fname, [fname])

    # 11. Scenarios containing personal or household names
    pii_triggers = [p for p in person_first_names + ([surname_guess] if surname_guess else []) if len(p) >= 3]
    scen_idx = 0
    for sc in scenarios:
        sname = sc["name"]
        sfile = sc["filename"]
        hay = f"{sname} {sfile}"
        if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(trig)}", hay, flags=re.IGNORECASE) for trig in pii_triggers):
            scen_idx += 1
            _add(
                f"SCENARIO_{scen_idx}",
                "scenario",
                sname,
                [sfile, sname],
                meta={"filename": sfile, "scenario_id": sc["id"]},
            )

    # 12. OS Username / home directory leaf (prevents leaking username via exception paths)
    # SECURITY DO: Include OS login/home username in alias extraction and exclude all real entity tokens
    # in `assign_demo_personas` so neither error messages nor Demo personas can collide with real household PII.
    # SECURITY DON'T: Never relax the `real_terms_low` exclusion filter when adding new synthetic pools.
    os_users: list[str] = []
    for cand_u in (getpass.getuser(), Path.home().name):
        u_clean = str(cand_u or "").strip()
        if len(u_clean) >= 3 and u_clean.lower() not in {"root", "user", "admin", "users", "home"}:
            if u_clean not in os_users:
                os_users.append(u_clean)
    already_covered = {
        str(s).strip().lower()
        for e in entities
        for s in ([e.get("canonical", "")] + list(e.get("synonyms") or []))
    }
    uncovered_users = [u for u in os_users if u.lower() not in already_covered]
    if uncovered_users:
        _add("OS_USER_1", "household", uncovered_users[0], uncovered_users)

    # 13. Stock Grant IDs (e.g. GRANT-01, PRIOR-01)
    for idx, gid in enumerate(grants, start=1):
        _add(f"GRANT_{idx}", "grant", gid, [gid])

    return entities


def build_auto_registry(portfolio_id: str | None = None) -> list[dict[str, Any]]:
    """Auto-discovers privacy registry entities for `portfolio_id` (or active portfolio)."""
    return _discover_entities(_inspect_portfolio_sources(portfolio_id))


def assign_demo_personas(
    entities: list[dict[str, Any]],
    *,
    demo_seed: str = "demo-4821",
    portfolio_slug: str = "default",
) -> list[dict[str, Any]]:
    """
    Deterministically allocates realistic, collision-free proper names & synthetic IDs
    for Demo Mode (Case A), respecting shared custodian clustering, surname inheritance,
    and full equity/fund/carrier replacement.
    """
    pools = _load_demo_pools()
    digest = hashlib.sha256(f"{portfolio_slug}:{demo_seed}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))

    # Build lowercase exclusion blocklist from ALL real synonyms/canonicals so Demo can never collide with real PII
    real_terms_low: set[str] = set()
    for ent in entities:
        for s in [ent.get("canonical", ""), *(ent.get("synonyms") or [])]:
            tok = str(s or "").strip().lower()
            if len(tok) >= 2:
                real_terms_low.add(tok)

    def _clean_pool(candidates: list[str]) -> list[str]:
        safe = []
        for cand in candidates:
            clow = cand.lower()
            if clow in real_terms_low:
                continue
            if any(len(rt) >= 4 and (rt in clow or clow in rt) for rt in real_terms_low):
                continue
            safe.append(cand)
        return safe or list(candidates)

    surnames = _clean_pool(pools["surnames"])
    rng.shuffle(surnames)
    demo_surname = surnames[0] if surnames else "Rivera"

    adults = [n for n in _clean_pool(pools["adult_given_names"]) if n.lower() != demo_surname.lower()]
    rng.shuffle(adults)
    adult_idx = 0

    kids = [n for n in _clean_pool(pools["beneficiary_given_names"]) if n.lower() != demo_surname.lower()]
    rng.shuffle(kids)
    kid_idx = 0

    cust_pool = [
        c for c in pools["custodians"]
        if c["name"].lower() not in real_terms_low
        and c["short"].lower() not in real_terms_low
        and not any(str(code).lower() in real_terms_low for code in (c.get("codes") or []))
    ] or list(pools["custodians"])
    rng.shuffle(cust_pool)
    cust_idx = 0

    eq_pool = [
        e for e in pools["equities"]
        if e["symbol"].lower() not in real_terms_low
        and e["short"].lower() not in real_terms_low
        and str(e.get("company") or "").lower() not in real_terms_low
        and str(e.get("plan") or "").lower() not in real_terms_low
    ] or list(pools["equities"])
    # Preserve ACME first if not excluded
    acme = [e for e in eq_pool if e["symbol"] == "ACME"]
    others = [e for e in eq_pool if e["symbol"] != "ACME"]
    rng.shuffle(others)
    eq_pool = acme + others
    eq_idx = 0

    fund_pool = [
        f for f in pools["funds"]
        if f["symbol"].lower() not in real_terms_low
        and str(f.get("name") or "").lower() not in real_terms_low
    ] or list(pools["funds"])
    mgr_pool = _clean_pool(pools["managers"])
    rng.shuffle(mgr_pool)
    carrier_pool = _clean_pool(pools["carriers"])
    rng.shuffle(carrier_pool)

    fund_idx = 0
    mgr_idx = 0
    carrier_idx = 0

    custodian_by_token: dict[str, dict[str, Any]] = {}
    investor_firsts: dict[str, str] = {}

    out_entities = copy.deepcopy(entities)
    acct_counter = 1
    contract_counter = 1
    grant_counter = 1

    # Assign households/trusts/people/custodians/products first so accounts & scenarios inherit cleanly
    for ent in out_entities:
        cat = ent.get("category")
        tok = ent.get("token", "")
        canon = str(ent.get("canonical") or "")
        if cat == "household":
            ent["demo_value"] = f"{demo_surname} Portfolio"
            ent["demo_meta"] = {"surname": demo_surname}
        elif cat == "trust":
            ent["demo_value"] = f"{demo_surname} Family Rev Trust" if "family" in canon.lower() else f"{demo_surname} Rev Trust"
        elif cat == "investor":
            given = adults[adult_idx % len(adults)]
            adult_idx += 1
            investor_firsts[tok] = given
            ent["demo_value"] = given if " " not in canon else f"{given} {demo_surname}"
            ent["demo_meta"] = {"first": given, "full": f"{given} {demo_surname}", "surname": demo_surname}
        elif cat == "beneficiary":
            given = kids[kid_idx % len(kids)]
            kid_idx += 1
            ent["demo_value"] = given if " " not in canon else f"{given} {demo_surname}"
            ent["demo_meta"] = {"first": given, "full": f"{given} {demo_surname}", "surname": demo_surname}
        elif cat == "custodian":
            c_obj = cust_pool[cust_idx % len(cust_pool)]
            cust_idx += 1
            custodian_by_token[tok] = c_obj
            ent["demo_value"] = c_obj["name"]
            ent["demo_meta"] = c_obj
        elif cat == "equity":
            e_obj = eq_pool[eq_idx % len(eq_pool)]
            eq_idx += 1
            ent["demo_value"] = e_obj["symbol"]
            ent["demo_meta"] = e_obj
        elif cat == "fund":
            f_obj = fund_pool[fund_idx % len(fund_pool)]
            fund_idx += 1
            ent["demo_value"] = f_obj["symbol"]
            ent["demo_meta"] = f_obj
        elif cat == "manager":
            m_name = mgr_pool[mgr_idx % len(mgr_pool)]
            mgr_idx += 1
            ent["demo_value"] = m_name
            ent["demo_meta"] = {"name": m_name}
        elif cat == "carrier":
            cr_name = carrier_pool[carrier_idx % len(carrier_pool)]
            carrier_idx += 1
            ent["demo_value"] = cr_name
            ent["demo_meta"] = {"name": cr_name}
        elif cat == "grant":
            if canon.upper().startswith("PRIOR"):
                ent["demo_value"] = "RET-PRIOR"
            else:
                ent["demo_value"] = f"GR-{4410 + grant_counter}"
                grant_counter += 1
            ent["demo_meta"] = {"grant_id": ent["demo_value"]}

    # Second pass: shape-preserving accounts, contracts, scenarios inheriting custodian/person names
    for ent in out_entities:
        cat = ent.get("category")
        canon = str(ent.get("canonical") or "")
        meta = ent.get("meta") or {}
        if cat == "account":
            bank_tok = meta.get("bank_token") or ""
            c_obj = custodian_by_token.get(bank_tok) or {}
            c_code = (c_obj.get("codes") or ["DEM"])[0]
            if "-" in canon:
                prefix_num = 4810 + (int.from_bytes(hashlib.sha256(f"{demo_seed}:p".encode()).digest()[:2], "big") % 400)
                suffix_num = 2000 + acct_counter * 1117 + (rng.randint(10, 89))
                syn_acct = f"{prefix_num}-{(suffix_num % 8999) + 1000}"
            else:
                syn_acct = f"{c_code}{900 + acct_counter}D{400 + rng.randint(1, 99)}"
            acct_counter += 1
            ent["demo_value"] = syn_acct
            ent["demo_meta"] = {"dash": syn_acct, "underscore": syn_acct.replace("-", "_"), "bank_code": c_code}
        elif cat == "contract":
            syn_pol = f"POL-{7040 + contract_counter}"
            contract_counter += 1
            ent["demo_value"] = syn_pol
            ent["demo_meta"] = {"policy": syn_pol}
        elif cat == "scenario":
            inv_first = investor_firsts.get("INVESTOR_1") or "Alex"
            if "default" in canon.lower():
                ent["demo_value"] = f"{inv_first}'s Default"
            else:
                ent["demo_value"] = f"{inv_first} Strategy"
            ent["demo_meta"] = {"filename": f"{re.sub(r'[^a-z0-9]+', '_', ent['demo_value'].lower()).strip('_')}.json"}
        elif cat == "custom" and not ent.get("demo_value"):
            ent["demo_value"] = f"[Demo {ent.get('token', 'Entity')}]"

    return out_entities


class PrivacyConfigError(ValueError):
    """An existing privacy policy cannot be applied safely."""


def _read_registry(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise PrivacyConfigError("Restore a readable privacy_registry.json for this portfolio.") from exc
    try:
        stored = json.loads(raw)
        if not isinstance(stored, dict):
            raise ValueError("Expected a policy object")
        shield = stored.get("ai_shield", {})
        if not isinstance(shield, dict) or shield.get("mode", "identity_aliased") not in (
            "off", "identity_aliased", "identity_and_values", "local_only",
        ):
            raise ValueError("Invalid AI shield mode")
        for key in ("demo_mode", "mcp_server"):
            section = stored.get(key, {})
            if not isinstance(section, dict) or not isinstance(section.get("enabled", False), bool):
                raise ValueError("Invalid privacy setting")
        return stored
    except (ValueError, TypeError) as exc:
        raise PrivacyConfigError("Restore a valid privacy_registry.json for this portfolio.") from exc


def sync_privacy_registry(
    portfolio_id: str | None = None,
    *,
    save: bool = False,
) -> dict[str, Any]:
    """
    Loads `Portfolios/<pid>/privacy_registry.json`, merges freshly discovered entities
    while preserving any manual edits/custom entries, assigns Demo Mode personas, and
    optionally saves back to disk.
    """
    pid = portfolio_id or get_active_portfolio_id()
    sources = _inspect_portfolio_sources(pid)
    discovered = _discover_entities(sources)

    reg_path = _privacy_file_path(pid)
    stored = _read_registry(reg_path)

    cfg = default_privacy_config()
    if isinstance(stored.get("ai_shield"), dict):
        cfg["ai_shield"].update(stored["ai_shield"])
    if isinstance(stored.get("mcp_server"), dict):
        cfg["mcp_server"].update(stored["mcp_server"])
    if isinstance(stored.get("demo_mode"), dict):
        cfg["demo_mode"].update(stored["demo_mode"])

    extra_syn_map: dict[str, list[str]] = {}
    if isinstance(stored.get("synonyms"), dict):
        cfg["synonyms"] = copy.deepcopy(stored["synonyms"])
        for sk, sv in stored["synonyms"].items():
            if isinstance(sv, list):
                extra_syn_map[str(sk).strip().lower()] = [str(x).strip() for x in sv if str(x).strip()]

    # Index previously stored entities by (token) and (category, canonical.lower())
    old_by_token: dict[str, dict[str, Any]] = {}
    custom_entities: list[dict[str, Any]] = []
    for raw_ent in stored.get("entities") or []:
        if not isinstance(raw_ent, dict) or not raw_ent.get("token"):
            continue
        tok = str(raw_ent["token"])
        old_by_token[tok] = raw_ent
        if not raw_ent.get("auto_detected", True) or raw_ent.get("category") == "custom":
            custom_entities.append(copy.deepcopy(raw_ent))

    merged: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for d_ent in discovered:
        tok = d_ent["token"]
        seen_tokens.add(tok)
        prev = old_by_token.get(tok)
        if prev:
            combined_syns = list(d_ent.get("synonyms") or []) + list(prev.get("synonyms") or [])
            # Respect user-edited canonical & enabled flag if present
            if prev.get("canonical"):
                d_ent["canonical"] = str(prev["canonical"])
            if "enabled" in prev:
                d_ent["enabled"] = bool(prev["enabled"])
            if isinstance(prev.get("synonyms"), list) and prev.get("synonyms_edited"):
                d_ent["synonyms"] = _dedupe_preserve_longest(
                    [d_ent["canonical"]] + [str(s) for s in prev["synonyms"] if str(s).strip()]
                )
                d_ent["synonyms_edited"] = True
            else:
                d_ent["synonyms"] = _dedupe_preserve_longest(combined_syns)
        if extra_syn_map and not d_ent.get("synonyms_edited"):
            cur_keys = {str(d_ent.get("canonical") or "").strip().lower()} | {
                str(s).strip().lower() for s in (d_ent.get("synonyms") or [])
            }
            add_s: list[str] = []
            for ck in cur_keys:
                if ck in extra_syn_map:
                    add_s.extend(extra_syn_map[ck])
            if add_s:
                d_ent["synonyms"] = _dedupe_preserve_longest(list(d_ent.get("synonyms") or []) + add_s)
        merged.append(d_ent)

    for c_ent in custom_entities:
        if c_ent["token"] not in seen_tokens:
            seen_tokens.add(c_ent["token"])
            c_ent["synonyms"] = _dedupe_preserve_longest([c_ent.get("canonical", "")] + list(c_ent.get("synonyms") or []))
            merged.append(c_ent)

    demo_seed = str(cfg["demo_mode"].get("demo_seed") or cfg["demo_mode"].get("seed") or "demo-4821")
    cfg["entities"] = assign_demo_personas(
        merged,
        demo_seed=demo_seed,
        portfolio_slug=str(pid or "default"),
    )
    cfg["portfolio_id"] = pid or ""

    if save and reg_path is not None:
        _atomic_write_json_0600(reg_path, cfg)

    return cfg


def load_privacy_config(portfolio_id: str | None = None, auto_sync: bool = True) -> dict[str, Any]:
    """
    Returns the effective privacy config + entity registry for `portfolio_id` (without unexpected disk writes).

    DESIGN INTENT (SA2-04 / SA2-05 / SA2-06 / DP-05):
    - Live View always displays real account names, account numbers, and annuity contract numbers.
    - `sync_privacy_registry` registers every account (`ACCT_N`), annuity contract (`CONTRACT_N`),
      individual `EQUITY` ticker (including managed SMA sleeves), `friendly_name` overrides,
      `family_members`, and ASCII-folded person stems so `DemoTranslator` projects them in Demo Mode
      and `PrivacyTranslator` obfuscates them in AI Chat / MCP Mode B & C—while preserving
      `mcp_server.enabled` across resyncs.
    """
    if auto_sync:
        return sync_privacy_registry(portfolio_id, save=False)
    pid = portfolio_id or get_active_portfolio_id()
    reg_path = _privacy_file_path(pid)
    cfg = default_privacy_config()
    stored = _read_registry(reg_path)
    for key in ("ai_shield", "mcp_server", "demo_mode"):
        cfg[key].update(stored.get(key, {}))
    if isinstance(stored.get("entities"), list):
        cfg["entities"] = stored["entities"]
    cfg["portfolio_id"] = pid or ""
    return cfg


def save_privacy_config(update_body: dict[str, Any], portfolio_id: str | None = None) -> dict[str, Any]:
    """Applies partial or full updates to `privacy_registry.json` and writes atomically to disk."""
    req_pid = portfolio_id or str(update_body.get("portfolio_id") or "").strip() or None
    if req_pid:
        cand_dir = get_portfolio_dir(req_pid)
        if cand_dir is None or not is_valid_portfolio_dir(cand_dir):
            req_pid = None
    pid = req_pid or get_active_portfolio_id()
    reg_path = _privacy_file_path(pid)
    if reg_path is None:
        return {"ok": False, "error": "No active portfolio selected."}

    cur = sync_privacy_registry(pid, save=False)

    if isinstance(update_body.get("ai_shield"), dict):
        cur["ai_shield"].update(update_body["ai_shield"])
        if cur["ai_shield"].get("mode") not in ("off", "identity_aliased", "identity_and_values", "local_only"):
            cur["ai_shield"]["mode"] = "identity_aliased"
        if cur["ai_shield"].get("dob_granularity") not in ("year_month", "full", "age_only", "shift_years"):
            cur["ai_shield"]["dob_granularity"] = "year_month"
        if cur["ai_shield"].get("notes_policy") not in ("translate", "strip"):
            cur["ai_shield"]["notes_policy"] = "translate"
        if "scale_factor" in cur["ai_shield"] and cur["ai_shield"]["scale_factor"] is not None:
            try:
                sk = float(cur["ai_shield"]["scale_factor"])
                if 0.01 <= sk <= 100.0:
                    cur["ai_shield"]["scale_factor"] = round(sk, 4)
            except (TypeError, ValueError):
                cur["ai_shield"]["scale_factor"] = 0.25
        if "birthday_shift_years" in cur["ai_shield"] and cur["ai_shield"]["birthday_shift_years"] is not None:
            try:
                bs = int(cur["ai_shield"]["birthday_shift_years"])
                cur["ai_shield"]["birthday_shift_years"] = max(-30, min(30, bs))
            except (TypeError, ValueError):
                cur["ai_shield"]["birthday_shift_years"] = 3

    if isinstance(update_body.get("mcp_server"), dict):
        ms = update_body["mcp_server"]
        if "enabled" in ms:
            cur.setdefault("mcp_server", {})["enabled"] = bool(ms["enabled"])

    if isinstance(update_body.get("demo_mode"), dict):
        dm = update_body["demo_mode"]
        if "enabled" in dm:
            cur["demo_mode"]["enabled"] = bool(dm["enabled"])
        # SECURITY INVARIANT (SA3-01): Preserve custom/regenerated `demo_seed` (accepting either `"demo_seed"` or `"seed"`)
        # rather than overwriting with a static constant, and clamp `scale_factor` to [0.05, 5.0].
        incoming_seed = str(dm.get("demo_seed") or dm.get("seed") or "").strip()
        if incoming_seed and re.match(r"^[A-Za-z0-9_-]{3,48}$", incoming_seed):
            cur["demo_mode"]["demo_seed"] = incoming_seed
        else:
            cur["demo_mode"]["demo_seed"] = str(cur["demo_mode"].get("demo_seed") or "demo-4821")
        if "scale_factor" in dm and dm["scale_factor"] is not None:
            try:
                k_val = float(dm["scale_factor"])
                if 0.05 <= k_val <= 5.0:
                    cur["demo_mode"]["scale_factor"] = round(k_val, 4)
            except (TypeError, ValueError):
                pass
        cur["demo_mode"]["round_to"] = 1000

    # SECURITY INVARIANT (SA3-01 — Never Persist Demo-Redacted Placeholder Entities):
    # `GET /api/privacy` masks every entity's `canonical` to `"[Hidden in Demo Mode]"` and `synonyms` to `[]` while
    # Demo Mode is active. If a redacted client payload ever reaches `save_privacy_config`, reject the entire `entities`
    # replacement (or skip masked rows) so real custom entities and user-curated synonyms on disk are NEVER wiped!
    if isinstance(update_body.get("entities"), list):
        has_demo_redacted = any(
            isinstance(raw_ent, dict) and str(raw_ent.get("canonical") or "").strip() == "[Hidden in Demo Mode]"
            for raw_ent in update_body["entities"]
        )
        if not has_demo_redacted:
            clean_ents: list[dict[str, Any]] = []
            for idx, raw_ent in enumerate(update_body["entities"], start=1):
                if not isinstance(raw_ent, dict):
                    continue
                tok = str(raw_ent.get("token") or f"CUSTOM_{idx}").strip().upper().replace(" ", "_")
                tok = re.sub(r"[^A-Z0-9_]", "_", tok)
                canon = str(raw_ent.get("canonical") or "").strip()
                syns = raw_ent.get("synonyms")
                if isinstance(syns, str):
                    syns = [s.strip() for s in syns.split(",") if s.strip()]
                elif not isinstance(syns, list):
                    syns = [canon] if canon else []
                if canon and all(s.lower() != canon.lower() for s in syns):
                    syns = [canon] + list(syns)
                clean_ents.append(
                    {
                        "token": tok,
                        "category": str(raw_ent.get("category") or "custom").strip().lower(),
                        "canonical": canon or (syns[0] if syns else tok),
                        "synonyms": _dedupe_preserve_longest(syns),
                        "synonyms_edited": bool(raw_ent.get("synonyms_edited", True)),
                        "demo_value": str(raw_ent.get("demo_value") or ""),
                        "demo_meta": raw_ent.get("demo_meta") if isinstance(raw_ent.get("demo_meta"), dict) else {},
                        "meta": raw_ent.get("meta") if isinstance(raw_ent.get("meta"), dict) else {},
                        "auto_detected": bool(raw_ent.get("auto_detected", False)),
                        "enabled": bool(raw_ent.get("enabled", True)),
                    }
                )
            cur["entities"] = clean_ents

    cur["entities"] = assign_demo_personas(
        cur["entities"],
        demo_seed=str(cur["demo_mode"].get("demo_seed") or "demo-4821"),
        portfolio_slug=str(pid or "default"),
    )
    cur["portfolio_id"] = pid or ""
    _atomic_write_json_0600(reg_path, cur)
    return {"ok": True, "privacy": cur}
