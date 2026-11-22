#!/usr/bin/env python3
"""Deterministic, persistent masking for brokerage account numbers and annuity contract IDs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

_ALREADY_MASKED_ACCT_RE = re.compile(r"^(?:ACCT|WFA|MS|ANN)-(?:\d{2,}|[A-F0-9]{6,8})$")
_RAW_WFA_ACCT_RE = re.compile(r"\b\d{4}-\d{4}\b")
_SAFE_ACCOUNT_CONSTANTS = {"", "MS_STOCK_PLAN", "SSA"}


def _chmod_0600(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


def resolve_account_map_path(cfg: dict[str, Any] | None = None, db_path: Path | None = None) -> Path | None:
    if isinstance(cfg, dict) and cfg.get("_account_map_path"):
        return Path(str(cfg["_account_map_path"]))
    if db_path is not None:
        return Path(db_path).resolve().parent / "account_map.json"
    try:
        from config import get_active_portfolio_id, get_portfolio_dir

        pid = get_active_portfolio_id()
        if pid:
            pdir = get_portfolio_dir(pid)
            if pdir is not None:
                return pdir / "account_map.json"
    except Exception:
        pass
    return None


def load_account_map(map_path: Path | None) -> dict[str, dict[str, str]]:
    empty: dict[str, dict[str, str]] = {"accounts": {}, "contracts": {}}
    if map_path is None or not map_path.exists():
        return empty
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(raw, dict):
        return empty
    accts = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
    contracts = raw.get("contracts") if isinstance(raw.get("contracts"), dict) else {}
    return {
        "accounts": {str(k): str(v) for k, v in accts.items()},
        "contracts": {str(k): str(v) for k, v in contracts.items()},
    }


def save_account_map(map_path: Path | None, data: dict[str, dict[str, str]]) -> None:
    if map_path is None:
        return
    map_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = map_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _chmod_0600(tmp_path)
    tmp_path.replace(map_path)
    _chmod_0600(map_path)


def _deterministic_code(prefix: str, raw_val: str) -> str:
    digest = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()[:6].upper()
    return f"{prefix}-{digest}"


def mask_account_id(
    raw_id: str,
    cfg: dict[str, Any] | None = None,
    map_path: Path | None = None,
) -> str:
    val = str(raw_id or "").strip()
    if val in _SAFE_ACCOUNT_CONSTANTS or _ALREADY_MASKED_ACCT_RE.match(val):
        return val
    resolved_path = map_path or resolve_account_map_path(cfg=cfg)
    if resolved_path is None:
        return _deterministic_code("ACCT", val)
    data = load_account_map(resolved_path)
    accts = data["accounts"]
    if val in accts:
        return accts[val]
    used = set(accts.values())
    idx = len(accts) + 1
    while f"ACCT-{idx:02d}" in used:
        idx += 1
    masked = f"ACCT-{idx:02d}"
    accts[val] = masked
    save_account_map(resolved_path, data)
    return masked


def mask_contract_id(
    raw_contract: str,
    cfg: dict[str, Any] | None = None,
    map_path: Path | None = None,
) -> str:
    val = str(raw_contract or "").strip()
    if not val or _ALREADY_MASKED_ACCT_RE.match(val):
        return val
    resolved_path = map_path or resolve_account_map_path(cfg=cfg)
    if resolved_path is None:
        return _deterministic_code("ANN", val)
    data = load_account_map(resolved_path)
    contracts = data["contracts"]
    if val in contracts:
        return contracts[val]
    used = set(contracts.values())
    idx = len(contracts) + 1
    while f"ANN-{idx:02d}" in used:
        idx += 1
    masked = f"ANN-{idx:02d}"
    contracts[val] = masked
    save_account_map(resolved_path, data)
    return masked


def resolve_target_account_id(
    configured_id: str,
    known_accounts: Iterable[str],
    cfg: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> str:
    known_list = [str(x) for x in known_accounts if x]
    known_set = set(known_list)
    cand = str(configured_id or "").strip()
    if cand and cand in known_set:
        return cand
    if cand:
        resolved_path = resolve_account_map_path(cfg=cfg, db_path=db_path)
        if resolved_path is not None and resolved_path.exists():
            mapped = load_account_map(resolved_path)["accounts"].get(cand)
            if mapped and mapped in known_set:
                return mapped
    return ""


def mask_raw_ids_in_text(
    text: str,
    raw_account_id: str = "",
    masked_account_id: str = "",
    cfg: dict[str, Any] | None = None,
    map_path: Path | None = None,
) -> str:
    out = str(text or "")
    if not out:
        return out
    if raw_account_id and masked_account_id and raw_account_id != masked_account_id:
        out = out.replace(raw_account_id, masked_account_id)
    resolved_path = map_path or resolve_account_map_path(cfg=cfg)
    if resolved_path is not None and resolved_path.exists():
        data = load_account_map(resolved_path)
        for raw_k, sym_v in sorted(
            {**data["accounts"], **data["contracts"]}.items(),
            key=lambda kv: -len(kv[0]),
        ):
            if len(raw_k) >= 4 and raw_k in out:
                out = out.replace(raw_k, sym_v)
    return out
