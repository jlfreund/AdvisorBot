#!/usr/bin/env python3
"""
Unified LLM Tool, Async Advisor Job Runner & Chat Execution Layer
for the Portfolio Tracker & Retirement Simulator.

Used by:
  1. `Scripts/mcp_server.py`        (Model Context Protocol stdio server for `agy`, Claude Code, etc.)
  2. `Scripts/advisorbot_server.py` (Local HTTP API & Right-Pane Advisor Drawer `/api/advisor/*`, `/api/chat`, `/api/ui/*`)
  3. `Scripts/holdings.py`          (CLI subcommands `holdings.py llm-*`)
"""

from __future__ import annotations

import copy
from contextvars import ContextVar, copy_context
from datetime import datetime
import http.client
import json
import logging
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from privacy import DemoTranslator, PrivacyTranslator, build_translator, load_privacy_config

from config import (  # noqa: E402
    HOLDINGS_ROOT,
    with_active_portfolio,
    get_active_portfolio_id,
    get_default_config_path,
    get_default_db_path,
    get_llm_dir,
    get_portfolio_dir,
    get_saves_dir,
    get_session_token_file,
    load_config,
    load_default_advisor_prompts,
    normalize_saved_prompts,
    resolve_advisor_prompts,
)
from settings import default_settings_snapshot  # noqa: F401
from advisorbot_server import (
    DEFAULT_PORT,
    append_model_upvote,
    delete_save_file,
    get_model_entry,
    is_scenario_protected,
    list_saves_payload,
    rename_model,
    sanitize_save_name,
    set_active_save,
    update_model_notes,
    write_save_file,
)
from valuation.simulator import (
    _load_family_config_data,
    build_effective_settings,
    compute_non_goog_weighted_growth,
    evaluate_active_model,
    get_reference_rates,
    load_live_payload,
    materialize_sim_assets,
    normalize_hypothetical_assets,
    normalize_scheduled_actions,
    optimize_sim_asset_order,
    query_positions,
    solve_simulation,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = HOLDINGS_ROOT
log = logging.getLogger(__name__)

_PAYLOAD_CACHE_LOCK = threading.Lock()
_PAYLOAD_GENERATION = 0
_CACHED_PAYLOAD: dict[str, Any] | None = None
_CACHED_PAYLOAD_KEY: tuple[Any, ...] | None = None
_CACHED_UNMASKED_PAYLOAD: dict[str, Any] | None = None
_CACHED_UNMASKED_KEY: tuple[Any, ...] | None = None

_UI_LOCK = threading.Lock()
_UI_SEQ: int = 0
_UI_EVENTS: list[tuple[str | None, dict[str, Any]]] = []
_BROWSER_STATES: dict[str | None, dict[str, Any]] = {}

_ADVISOR_LOCK = threading.Lock()
_advisor_draft: ContextVar[dict | None] = ContextVar("advisor_draft", default=None)
_ADVISOR_JOBS: dict[str, dict[str, Any]] = {}
_LATEST_ADVISOR_JOBS: dict[tuple[str | None, bool], str] = {}


def _latest_advisor_job_id(pid: str | None, demo_on: bool) -> str | None:
    jid = _LATEST_ADVISOR_JOBS.get((pid, demo_on))
    if jid:
        return jid
    legacy_jid = _LATEST_ADVISOR_JOBS.get(pid)  # type: ignore[arg-type]
    if legacy_jid and legacy_jid in _ADVISOR_JOBS:
        if bool(_ADVISOR_JOBS[legacy_jid].get("demo_enabled", False)) == demo_on:
            return legacy_jid
    return None


def clear_portfolio_advisor_jobs(portfolio_id: str | None) -> None:
    """Clears cached latest-job pointers and job records for `portfolio_id` across both live and demo scopes."""
    with _ADVISOR_LOCK:
        for key in list(_LATEST_ADVISOR_JOBS):
            key_pid = key[0] if isinstance(key, tuple) else key
            if key_pid == portfolio_id:
                _LATEST_ADVISOR_JOBS.pop(key, None)
        for jid in [jid for jid, rec in _ADVISOR_JOBS.items() if rec.get("portfolio_id") == portfolio_id]:
            _ADVISOR_JOBS.pop(jid, None)

# Set True inside the running advisorbot_server process so in-process enqueues append
# directly instead of HTTP-calling the same server.
IN_SERVER_PROCESS = False


def _session_token() -> str:
    """Reads the running server's session token, to authenticate local inter-process calls."""
    tok_path = get_session_token_file()
    if tok_path.is_file():
        try:
            return tok_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    pdir = get_portfolio_dir()
    if pdir is not None:
        try:
            return (pdir / ".session_token").read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def _active_demo_privacy_cfg() -> dict[str, Any] | None:
    """Returns active portfolio's privacy config when Demo Mode is enabled, else None."""
    pid = get_active_portfolio_id()
    if pid:
        pcfg = load_privacy_config(pid, auto_sync=False)
        if (pcfg.get("demo_mode") or {}).get("enabled"):
            return pcfg
    return None


def _project_demo_step_text(text: str, demo_cfg: dict[str, Any] | None) -> str:
    if not text or demo_cfg is None:
        return text
    proj = DemoTranslator(demo_cfg)
    return proj._scale_inline_usd_text(str(text))


def _project_demo_chat_response(
    res: dict[str, Any],
    demo_cfg: dict[str, Any] | None,
    *,
    already_value_scaled: bool = False,
) -> dict[str, Any]:
    """Projects outbound chat/advisor responses through `DemoTranslator` when Demo Mode is active (DP-02)."""
    if demo_cfg is None or not isinstance(res, dict):
        return res
    proj = DemoTranslator(demo_cfg)
    out = dict(res)
    if isinstance(out.get("reply"), str) and out["reply"]:
        out["reply"] = proj.map_text(out["reply"]) if already_value_scaled else proj._scale_inline_usd_text(out["reply"])
    if isinstance(out.get("error"), str) and out["error"]:
        out["error"] = proj.map_text(out["error"])
    if isinstance(out.get("tool_calls"), list):
        out["tool_calls"] = proj.project_obj(out["tool_calls"])
    if isinstance(out.get("ui_events"), list):
        out["ui_events"] = proj.project_obj(out["ui_events"])
    if out.get("privacy_shield") and out.get("privacy_shield") != "error_blocked":
        out["privacy_shield"] = str((demo_cfg.get("ai_shield") or {}).get("mode") or "identity_aliased")
    out["demo_mode"] = True
    return out


def _format_step_args_for_ui(
    t_args: dict[str, Any] | None,
    translator: PrivacyTranslator | None,
    demo_cfg: dict[str, Any] | None,
) -> dict[str, Any]:
    """De-aliases inbound tool arguments and projects via Demo Mode before displaying in live UI banners (SA2-09)."""
    disp = translator.from_alias(t_args) if translator is not None else dict(t_args or {})
    if not isinstance(disp, dict):
        return {}
    if demo_cfg is not None:
        disp = DemoTranslator(demo_cfg).project_obj(disp)
    return disp if isinstance(disp, dict) else {}


def _payload_cache_fingerprint() -> tuple[Any, ...]:
    """Returns a fast mtime fingerprint for active DB, portfolio config, and privacy config."""
    try:
        cfg_path = get_default_config_path()
        db_path = get_default_db_path()
        cfg_m = cfg_path.stat().st_mtime if cfg_path and cfg_path.is_file() else None
        db_m = db_path.stat().st_mtime if db_path and db_path.is_file() else None
        priv_path = cfg_path.parent / "privacy_registry.json" if cfg_path else None
        priv_m = priv_path.stat().st_mtime if priv_path and priv_path.is_file() else None
        return (str(cfg_path or ""), cfg_m, str(db_path or ""), db_m, priv_m)
    except OSError:
        return ()


def invalidate_payload_cache() -> None:
    global _CACHED_PAYLOAD, _CACHED_PAYLOAD_KEY, _CACHED_UNMASKED_PAYLOAD, _CACHED_UNMASKED_KEY
    global _PAYLOAD_GENERATION
    with _PAYLOAD_CACHE_LOCK:
        _PAYLOAD_GENERATION += 1
        _CACHED_PAYLOAD = None
        _CACHED_PAYLOAD_KEY = None
        _CACHED_UNMASKED_PAYLOAD = None
        _CACHED_UNMASKED_KEY = None


@with_active_portfolio
def get_cached_payload(force_reload: bool = False, *, bypass_export_masking: bool = False) -> dict[str, Any]:
    global _CACHED_PAYLOAD, _CACHED_PAYLOAD_KEY, _CACHED_UNMASKED_PAYLOAD, _CACHED_UNMASKED_KEY
    with _PAYLOAD_CACHE_LOCK:
        generation = _PAYLOAD_GENERATION
    fp = _payload_cache_fingerprint()
    if bypass_export_masking:
        with _PAYLOAD_CACHE_LOCK:
            if not force_reload and _CACHED_UNMASKED_PAYLOAD is not None and fp and _CACHED_UNMASKED_KEY == fp:
                return _CACHED_UNMASKED_PAYLOAD
        from datetime import date as _date
        from db.database import PortfolioDB
        from exporters.view_model import build_report_payload
        from valuation.simulator import refresh_family_config

        cfg_path = get_default_config_path()
        cfg = load_config(cfg_path)
        refresh_family_config(cfg)
        db_path = get_default_db_path()
        db_ctx = PortfolioDB(db_path) if db_path is not None else PortfolioDB(":memory:")
        with db_ctx as db:
            payload = build_report_payload(db, cfg, _date.today(), bypass_export_masking=True)
        with _PAYLOAD_CACHE_LOCK:
            if generation != _PAYLOAD_GENERATION:
                raise RuntimeError("Portfolio settings changed during evaluation. Retry the request.")
            _CACHED_UNMASKED_PAYLOAD = payload
            _CACHED_UNMASKED_KEY = fp
            return _CACHED_UNMASKED_PAYLOAD
    with _PAYLOAD_CACHE_LOCK:
        if not force_reload and _CACHED_PAYLOAD is not None and fp and _CACHED_PAYLOAD_KEY == fp:
            return _CACHED_PAYLOAD
    payload = load_live_payload()
    with _PAYLOAD_CACHE_LOCK:
        if generation != _PAYLOAD_GENERATION:
            raise RuntimeError("Portfolio settings changed during evaluation. Retry the request.")
        _CACHED_PAYLOAD = payload
        _CACHED_PAYLOAD_KEY = fp
        return _CACHED_PAYLOAD


def enqueue_ui_action(action: dict[str, Any], forward_to_http: bool = True) -> dict[str, Any]:
    """
    Enqueues a realtime UI command for the open `index.html` browser window.
    If called from a separate process (such as `mcp_server.py` or `holdings.py` CLI),
    forwards the action to `http://127.0.0.1:8765/api/ui/enqueue`.
    """
    global _UI_SEQ
    if forward_to_http and not IN_SERVER_PROCESS:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{DEFAULT_PORT}/api/ui/enqueue",
                data=json.dumps({"action": action, "source_portfolio_id": get_active_portfolio_id()}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Session-Token": _session_token(),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

    # Record advisor step on any running advisor job if telemetry arrives in-process
    if isinstance(action, dict) and action.get("type") == "advisor_tool_step":
        cur_pid = get_active_portfolio_id()
        cur_demo = _active_demo_privacy_cfg() is not None
        with _ADVISOR_LOCK:
            if (latest := _latest_advisor_job_id(cur_pid, cur_demo)) in _ADVISOR_JOBS:
                job = _ADVISOR_JOBS[latest]
                if (
                    job.get("status") == "running"
                    and job.get("portfolio_id") == cur_pid
                    and bool(job.get("demo_enabled", False)) == cur_demo
                ):
                    steps = job.setdefault("steps", [])
                    steps.append(
                        {
                            "ts": round(time.time(), 2),
                            "tool": action.get("tool"),
                            "summary": action.get("summary"),
                        }
                    )

    with _UI_LOCK:
        _UI_SEQ += 1
        entry = {
            "seq": _UI_SEQ,
            "ts": time.time(),
            "action": action,
        }
        _UI_EVENTS.append((get_active_portfolio_id(), entry))
        if len(_UI_EVENTS) > 150:
            del _UI_EVENTS[:-150]
        return {"ok": True, "seq": _UI_SEQ, "event": entry}


def poll_ui_actions(since_seq: int = 0, browser_state: dict[str, Any] | None = None) -> dict[str, Any]:
    with _UI_LOCK:
        if isinstance(browser_state, dict) and browser_state:
            _BROWSER_STATES[get_active_portfolio_id()] = browser_state
        scoped_events = [event for owner, event in _UI_EVENTS if owner == get_active_portfolio_id()]
        if since_seq <= 0 or since_seq > _UI_SEQ:
            return {
                "ok": True,
                "latest_seq": _UI_SEQ,
                "events": scoped_events if since_seq > _UI_SEQ else [],
                "advisor_job": _compact_latest_advisor_job(),
            }
        pending = [ev for ev in scoped_events if ev["seq"] > since_seq]
        return {
            "ok": True,
            "latest_seq": _UI_SEQ,
            "events": pending,
            "advisor_job": _compact_latest_advisor_job(),
        }


def get_latest_browser_state() -> dict[str, Any]:
    with _UI_LOCK:
        return copy.deepcopy(_BROWSER_STATES.get(get_active_portfolio_id(), {}))


def _compact_eval_for_llm(eval_res: dict[str, Any]) -> dict[str, Any]:
    """Strips bulky per-month `bars` and raw cohort lines so MCP/LLM responses remain compact & structured."""
    out = {k: v for k, v in eval_res.items() if k != "bars"}
    if isinstance(out.get("historical_cohorts"), dict):
        hc = dict(out["historical_cohorts"])
        hc.pop("cohorts", None)
        hc.pop("named", None)
        out["historical_cohorts"] = hc
    return out


# =============================================================================
# HIGH-LEVEL LLM TOOL IMPLEMENTATIONS
# =============================================================================


_PRIMARY_KEY_RE = re.compile(r"(non_)?goog", flags=re.IGNORECASE)


def _scrub_primary_symbol_keys(obj: Any) -> Any:
    """Rewrites internal `*goog*` dict keys to generic `primary` / `non_primary` keys on wire outputs (`SA-03`)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            k_str = str(k)
            if "goog" in k_str.lower():
                new_k = _PRIMARY_KEY_RE.sub(lambda m: "non_primary" if m.group(1) else "primary", k_str)
            else:
                new_k = k_str
            out[new_k] = _scrub_primary_symbol_keys(v)
        return out
    if isinstance(obj, list):
        return [_scrub_primary_symbol_keys(item) for item in obj]
    return obj


def _sanitize_error_for_llm(msg: Any) -> str:
    """Strips local filesystem / home directory paths from exception messages (`SA-19`)."""
    s = str(msg or "")
    s = re.sub(r"(/Users/|/home/|/var/folders/|/private/var/)[^\s\"':,;)}\]]+", "[LOCAL_PATH]", s)
    root_str = str(PROJECT_ROOT)
    if root_str and root_str in s:
        s = s.replace(root_str, "[PROJECT_ROOT]")
    return s


_OPAQUE_SIGNATURE_KEYS = frozenset({"thoughtSignature", "thought_signature", "signature"})
_OPAQUE_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9+/=_-]{16,}$")


def _strip_opaque_signatures_for_scan(node: Any) -> Any:
    """Removes strictly base64 cryptographic thinking-signature tokens from a scratch copy before leak scanning."""
    if isinstance(node, dict):
        return {
            k: (
                "[OPAQUE_SIG]"
                if (k in _OPAQUE_SIGNATURE_KEYS and isinstance(v, str) and _OPAQUE_SIGNATURE_RE.fullmatch(v))
                else _strip_opaque_signatures_for_scan(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_strip_opaque_signatures_for_scan(x) for x in node]
    return node


def _find_leak_field_paths(translator: PrivacyTranslator, node: Any, prefix: str = "", limit: int = 5) -> list[str]:
    """Returns structural JSON field paths containing leaks without exposing any raw leaked terms."""
    paths: list[str] = []

    def _walk(cur: Any, path: str) -> None:
        if len(paths) >= limit:
            return
        if isinstance(cur, dict):
            for k, v in cur.items():
                child_path = f"{path}.{k}" if path else str(k)
                if k in _OPAQUE_SIGNATURE_KEYS and isinstance(v, str) and _OPAQUE_SIGNATURE_RE.fullmatch(v):
                    continue
                if isinstance(v, (dict, list)):
                    if translator.find_leaked_terms(v):
                        _walk(v, child_path)
                elif isinstance(v, str) and translator.find_leaked_terms(v):
                    paths.append(child_path)
        elif isinstance(cur, list):
            for idx, item in enumerate(cur):
                child_path = f"{path}[{idx}]"
                if isinstance(item, (dict, list)):
                    if translator.find_leaked_terms(item):
                        _walk(item, child_path)
                elif isinstance(item, str) and translator.find_leaked_terms(item):
                    paths.append(child_path)
        elif isinstance(cur, str) and translator.find_leaked_terms(cur):
            paths.append(path or "<root>")

    _walk(node, prefix)
    return paths


def _scrub_model_turn_for_history(node: Any, translator: PrivacyTranslator | None, *, key_hint: str = "") -> Any:
    """Aliases assistant/model turn prose and tool-call args before echoing into Round 2+ history."""
    if translator is None:
        return node
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _OPAQUE_SIGNATURE_KEYS and isinstance(v, str):
                out[k] = v
            elif k == "arguments" and isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    out[k] = json.dumps(translator.to_alias(parsed), ensure_ascii=False)
                except Exception:
                    out[k] = translator.to_alias_text(v, scale_prose=False)
            elif k in ("args", "input") and isinstance(v, dict):
                out[k] = translator.to_alias(v, key_hint=k)
            else:
                out[k] = _scrub_model_turn_for_history(v, translator, key_hint=k)
        return out
    if isinstance(node, list):
        return [_scrub_model_turn_for_history(x, translator, key_hint=key_hint) for x in node]
    if isinstance(node, str):
        return translator.to_alias_text(node, scale_prose=False)
    return node


def _summarize_leak_categories(translator: PrivacyTranslator, leaks: list[str]) -> list[str]:
    """Summarizes leaked terms solely by entity category and character length (never exposing raw terms)."""
    ents = (translator.cfg.get("entities") or []) if isinstance(translator.cfg, dict) else []
    out: list[str] = []
    seen: set[str] = set()
    for lk in leaks[:8]:
        lk_low = str(lk).lower()
        cat = "dob_or_stem"
        for ent in ents:
            if not isinstance(ent, dict) or not ent.get("enabled", True):
                continue
            syns = ent.get("synonyms") or []
            canon = str(ent.get("canonical") or "").strip()
            if any(str(s).strip().lower() == lk_low for s in syns) or (canon and canon.lower() == lk_low):
                cat = str(ent.get("category") or "entity").lower()
                break
        tag = f"{cat}(len={len(str(lk))})"
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _with_round_diag(err_res: dict[str, Any], round_idx: int, provider: str, model_name: str) -> dict[str, Any]:
    """Attaches round, provider, and model metadata to an outbound error response."""
    diag = dict(err_res.get("diagnostics") or {})
    diag.update({"round": round_idx + 1, "resolved_provider": provider, "resolved_model": model_name})
    err_res["diagnostics"] = diag
    return err_res


def _assert_wire_leak_free(translator: PrivacyTranslator | None, wire_body: Any, context_label: str) -> dict[str, Any] | None:
    """Hard pre-egress leak gate (`SA-03`): checks serialized wire payload with `find_leaked_terms`."""
    # SECURITY DO: Call this right before every external HTTP/CLI inference call whenever Shield is active.
    # SECURITY DON'T: Never log raw leaked terms or bypass this check when Shield mode is `identity_aliased` or `identity_and_values`.
    if translator is None:
        return None
    scan_target = wire_body if isinstance(wire_body, str) else _strip_opaque_signatures_for_scan(wire_body)
    blob = scan_target if isinstance(scan_target, str) else json.dumps(scan_target, ensure_ascii=False)
    leaks = translator.find_leaked_terms(blob)
    if leaks:
        leak_paths = _find_leak_field_paths(translator, scan_target) if not isinstance(scan_target, str) else ["<text>"]
        leak_cats = _summarize_leak_categories(translator, leaks)
        logging.getLogger("llm_tools").error(
            "Privacy Shield blocked %s egress: %d leaked entity term(s) detected in serialized wire request (fields=%s, categories=%s).",
            context_label,
            len(leaks),
            ", ".join(leak_paths) or "<unknown>",
            ", ".join(leak_cats) or "<unknown>",
        )
        return {
            "ok": False,
            "engine": "privacy_shield",
            "privacy_shield": translator.mode,
            "error": (
                f"Blocked by Privacy Shield pre-egress gate ({context_label}): "
                f"{len(leaks)} protected term(s) detected in outbound payload."
            ),
            "diagnostics": {
                "gate": "pre_egress_leak_gate",
                "context": context_label,
                "shield_mode": translator.mode,
                "leak_count": len(leaks),
                "leak_fields": leak_paths,
                "leak_categories": leak_cats,
            },
        }
    return None


def tool_get_overview() -> dict[str, Any]:
    """
    Returns high-level portfolio KPIs, live equity quote, Reference growth rates (`Ref:`),
    per-holding `forecast_key` & `default_risk_class`, family birthdays, and canonical Scenarios in `Scenarios/`.
    """
    payload = get_cached_payload()
    refs = get_reference_rates(payload)
    saves_dir = get_saves_dir()
    saves_info = list_saves_payload(saves_dir) if saves_dir is not None else {
        "ok": True, "active_id": "", "active_save": "", "models": [], "saves": []
    }
    cfg = load_config(get_default_config_path())
    resolved_prompts = resolve_advisor_prompts(cfg)
    sim_assets_summary = [
        {
            "id": a["id"],
            "forecast_key": a.get("forecast_key") or a["id"],
            "parent_forecast_keys": a.get("parent_forecast_keys") or [],
            "default_rank": a["default_rank"],
            "short_title": a.get("short_title") or a.get("title"),
            "account_label": a.get("account_label"),
            "bucket": a.get("bucket"),
            "current_value": a.get("current_value"),
            "cost_basis": a.get("cost_basis"),
            "tax_wrapper": a.get("tax_wrapper"),
            "tax_model": a.get("tax_model"),
            "tax_rate": a.get("tax_rate"),
            "maturity_iso": a.get("maturity_iso"),
            "ref_growth_pct": a.get("ref_growth_pct"),
            "default_growth_pct": a.get("default_growth_pct"),
            "default_risk_class": a.get("default_risk_class"),
            "expense_ratio_pct": a.get("expense_ratio_pct", 0.0),
            "growth_evidence": [
                {
                    "tier": int(ge["tier"]),
                    "projected_return_pct": float(ge["projected_return_pct"]),
                    "source_detail": str(ge.get("source_detail") or "")[:220],
                }
                for ge in (a.get("growth_evidence") or [])
                if ge.get("tier") in (1, 2, 3) and ge.get("projected_return_pct") is not None
            ],
        }
        for a in (payload.get("retirement_sim_assets") or [])
    ]
    raw_ssn = payload.get("ssn_model") or {}
    ssn_summary = {
        "default_cola_pct": raw_ssn.get("default_cola_pct"),
        "cola_10y_avg_pct": raw_ssn.get("cola_10y_avg_pct"),
        "effective_ssn_tax_rate": raw_ssn.get("effective_ssn_tax_rate"),
        "tax_rule_label": raw_ssn.get("tax_rule_label"),
        "investors": [
            {
                "id": inv.get("id"),
                "name": inv.get("name"),
                "claim_setting_key": inv.get("claim_setting_key"),
                "retirement_setting_key": inv.get("retirement_setting_key"),
                "is_ssn_contributor": bool(inv.get("is_ssn_contributor", True)),
                "work_plan": inv.get("work_plan"),
                "fra_age": inv.get("fra_age"),
                "default_claim_age": inv.get("default_claim_age"),
                "milestone_age": inv.get("milestone_age"),
                "rmd_owner": bool(inv.get("rmd_owner")),
                "has_statement": bool(inv.get("has_statement")),
                "work_years": inv.get("work_years"),
                "zero_years": inv.get("zero_years"),
                "work_span_label": inv.get("work_span_label"),
                "pia_monthly_2026": inv.get("pia_monthly_2026"),
                "pia_annual_2026": inv.get("pia_annual_2026"),
            }
            for inv in (raw_ssn.get("investors") or [])
        ],
    }
    stock_grants = payload.get("stock_grants") or {}
    by_symbol = stock_grants.get("by_symbol") or {}
    unvested_companies = []
    for comp in (stock_grants.get("companies") or []):
        sym = str(comp.get("symbol") or "").strip()
        sym_detail = by_symbol.get(sym) or {}
        upcoming_bars = [
            bar for bar in (sym_detail.get("release_bars") or [])
            if str(bar.get("kind") or "").upper() == "ESTIMATED"
        ]
        unvested_companies.append(
            {
                "symbol": sym,
                "company_name": comp.get("company_name"),
                "award_type": comp.get("award_type"),
                "live_price": comp.get("live_price"),
                "net_retention_rate": comp.get("net_retention_rate"),
                "future_tranche_count": comp.get("future_tranche_count"),
                "future_gross_shares": comp.get("future_gross_shares"),
                "future_est_net_shares": comp.get("future_est_net_shares"),
                "future_gross_val": comp.get("future_gross_val"),
                "future_net_val": comp.get("future_net_val"),
                "grants": [
                    {
                        "grant_id": g.get("grant_id"),
                        "grant_date": g.get("grant_date"),
                        "first_vest": g.get("first_vest"),
                        "last_vest": g.get("last_vest"),
                        "rem_tranches": g.get("tranches"),
                        "rem_gross_shares": g.get("total_qty"),
                        "rem_est_net_shares": g.get("est_net_qty"),
                        "gross_val": g.get("gross_val"),
                        "net_val": g.get("net_val"),
                    }
                    for g in (sym_detail.get("by_grant") or [])
                ],
                "upcoming_vests": [
                    {
                        "vest_date": bar.get("iso"),
                        "tranche_count": bar.get("trancheCount"),
                        "gross_shares": bar.get("totGrossShares"),
                        "est_net_shares": bar.get("totNetShares"),
                        "gross_val": bar.get("totTodayGrossVal"),
                        "est_net_val": bar.get("totTodayNetVal"),
                    }
                    for bar in upcoming_bars[:16]
                ],
                "upcoming_vest_dates_count": len(upcoming_bars),
            }
        )
    unvested_summary = {
        "has_unvested": bool(stock_grants.get("has_unvested")),
        "companies": unvested_companies,
    }
    models_summary = [
        {
            "id": m["id"],
            "name": m["name"],
            "file": m["file"],
            "filename": m["filename"],
            "snapshot": m["snapshot"],
            "is_protected": m.get("is_protected", False),
            "created_at": m.get("created_at"),
            "updated_at": m["updated_at"],
            "author": m.get("author"),
            "primary_price_anchor": m.get("goog_price_anchor"),
            "kpi_summary": _scrub_primary_symbol_keys(m.get("kpi_summary")),
            "settings": m["settings"],
            "notes_md": m.get("notes_md", ""),
        }
        for m in (saves_info.get("models") or [])
    ]
    return {
        "ok": True,
        "today_iso": payload.get("today_iso"),
        "generated_at": payload.get("generated_at"),
        "primary_live_price": payload.get("goog_live_price"),
        "primary_prev_close": payload.get("goog_prev_close"),
        "kpis": _scrub_primary_symbol_keys(payload.get("kpis")),
        "reference_rates": _scrub_primary_symbol_keys(refs),
        "risk_classes": payload.get("risk_classes"),
        "family_birthdays": _load_family_config_data(cfg)[0],
        "ssn_model": _scrub_primary_symbol_keys(ssn_summary),
        "unvested_grants": _scrub_primary_symbol_keys(unvested_summary),
        "simulator_assets": sim_assets_summary,
        "active_id": saves_info.get("active_id"),
        "active_save": saves_info.get("active_save"),
        "models": models_summary,
        "saves": models_summary,
        "saved_prompts": normalize_saved_prompts(cfg),
        "advisor_prompts_meta": {
            k: {"has_project_override": bool((cfg.get("advisor_prompts") or {}).get(k))}
            for k in (resolved_prompts.get("effective") or resolved_prompts)
        },
        "live_browser_state": get_latest_browser_state(),
    }


def tool_query_positions(
    has_maturity: bool | None = None,
    account_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    payload = get_cached_payload()
    rows = query_positions(payload, has_maturity=has_maturity, account_id=account_id, symbol=symbol)
    total_val = round(sum(float(r.get("current_value") or 0.0) for r in rows), 2)
    return {
        "ok": True,
        "count": len(rows),
        "total_value_usd": total_val,
        "positions": rows,
    }


def _resolve_base_settings(base_save: str | None) -> dict[str, Any] | None:
    if not base_save:
        return None
    saves_dir = get_saves_dir()
    saves_info = list_saves_payload(saves_dir) if saves_dir is not None else {"models": []}
    clean_target = base_save.strip()
    t_low = clean_target.lower()
    t_pad = f"{int(clean_target):03d}" if clean_target.isdigit() else ""
    for s in saves_info.get("models") or []:
        if (
            s["id"].lower() == t_low
            or (t_pad and s["id"] == t_pad)
            or s["name"].strip().lower() == t_low
            or s["filename"].strip().lower() == t_low
            or Path(s["filename"]).stem.lower() == t_low
        ):
            return copy.deepcopy(s.get("settings") or {})
    raise ValueError(
        f"Scenario / save configuration {base_save!r} not found. Available scenarios: "
        + ", ".join(f"{s['id']}:{s['name']}" for s in (saves_info.get("models") or []))
    )


def tool_compute_non_primary_growth(
    rollovers: list[dict[str, Any]] | None = None,
    sleeve_rate_mode: str = "ytd_statement",
    muni_rate_mode: str = "price_cagr",
    include_cash: bool = False,
    asset_rate_overrides: dict[str, float] | None = None,
    uma_rate_mode: str | None = None,
) -> dict[str, Any]:
    eff_sleeve_mode = uma_rate_mode if uma_rate_mode is not None else sleeve_rate_mode
    payload = get_cached_payload()
    res = compute_non_goog_weighted_growth(
        payload,
        rollovers=rollovers,
        uma_rate_mode=eff_sleeve_mode,
        muni_rate_mode=muni_rate_mode,
        include_cash=include_cash,
        asset_rate_overrides=asset_rate_overrides,
    )
    return _scrub_primary_symbol_keys({
        "ok": True,
        **res,
    })


def tool_run_simulation(
    base_save: str | None = None,
    use_ref_rates: bool = False,
    overrides: dict[str, Any] | None = None,
    rollovers: list[dict[str, Any]] | None = None,
    reoptimize_order: bool = False,
    compare_with_baseline: bool = False,
    apply_non_primary_rate_mode: str = "none",
    sleeve_rate_mode: str = "ytd_statement",
    apply_non_goog_rate_mode: str | None = None,
    uma_rate_mode: str | None = None,
) -> dict[str, Any]:
    """
    Evaluates the 40-year + 100-year Retirement Simulator honoring `simActiveModel`
    (`single_path`, `stress`, `range`, or `historical`), per-holding `holdingForecasts`,
    unified `simScheduledActions`, and `$0`-start `simHypotheticalAssets`.
    """
    eff_rate_mode = apply_non_goog_rate_mode if apply_non_goog_rate_mode is not None else apply_non_primary_rate_mode
    eff_sleeve_mode = uma_rate_mode if uma_rate_mode is not None else sleeve_rate_mode
    payload = get_cached_payload()
    base_settings = _resolve_base_settings(base_save)
    eff_settings = build_effective_settings(
        payload,
        base_settings=base_settings,
        overrides=overrides,
        use_ref_rates=use_ref_rates,
    )

    sim_result = _compact_eval_for_llm(
        evaluate_active_model(
            payload,
            settings=eff_settings,
            rollovers=rollovers,
            reoptimize_order=reoptimize_order,
            apply_non_goog_rate_mode=eff_rate_mode,
            uma_rate_mode=eff_sleeve_mode,
        )
    )

    out: dict[str, Any] = {
        "ok": True,
        "kpi_summary": sim_result.get("kpi_summary"),
        "scenario": sim_result,
    }

    if rollovers and compare_with_baseline:
        baseline_res = _compact_eval_for_llm(
            evaluate_active_model(payload, settings=eff_settings, rollovers=None, reoptimize_order=False)
        )
        reopt_res = (
            sim_result
            if (reoptimize_order and eff_rate_mode == "none")
            else _compact_eval_for_llm(
                evaluate_active_model(
                    payload,
                    settings=eff_settings,
                    rollovers=rollovers,
                    reoptimize_order=True,
                    apply_non_goog_rate_mode="none",
                    uma_rate_mode=eff_sleeve_mode,
                )
            )
        )
        shifted_res = _compact_eval_for_llm(
            evaluate_active_model(
                payload,
                settings=eff_settings,
                rollovers=rollovers,
                reoptimize_order=reoptimize_order,
                apply_non_goog_rate_mode="shift",
                uma_rate_mode=eff_sleeve_mode,
            )
        )
        solved_res = _compact_eval_for_llm(
            evaluate_active_model(
                payload,
                settings=eff_settings,
                rollovers=rollovers,
                reoptimize_order=reoptimize_order,
                apply_non_goog_rate_mode="solved",
                uma_rate_mode=eff_sleeve_mode,
            )
        )
        b_j85 = baseline_res["milestones"].get("investor_1_milestone") or baseline_res["milestones"]["year_40"]
        s_j85 = sim_result["milestones"].get("investor_1_milestone") or sim_result["milestones"]["year_40"]
        r_j85 = reopt_res["milestones"].get("investor_1_milestone") or reopt_res["milestones"]["year_40"]
        sh_j85 = shifted_res["milestones"].get("investor_1_milestone") or shifted_res["milestones"]["year_40"]
        so_j85 = solved_res["milestones"].get("investor_1_milestone") or solved_res["milestones"]["year_40"]
        out["baseline"] = baseline_res
        out["rollover_reoptimized"] = reopt_res
        out["rollover_shifted_non_primary_rate"] = shifted_res
        out["rollover_solved_non_primary_rate"] = solved_res
        comp_dict = {
            "baseline_real_today_usd": b_j85["real_today_usd"],
            "baseline_nominal_usd": b_j85["nominal_usd"],
            "rollover_same_order_real_today_usd": s_j85["real_today_usd"],
            "rollover_same_order_nominal_usd": s_j85["nominal_usd"],
            "delta_same_order_real_today_usd": round(s_j85["real_today_usd"] - b_j85["real_today_usd"], 2),
            "delta_same_order_nominal_usd": round(s_j85["nominal_usd"] - b_j85["nominal_usd"], 2),
            "rollover_reoptimized_real_today_usd": r_j85["real_today_usd"],
            "rollover_reoptimized_nominal_usd": r_j85["nominal_usd"],
            "delta_reoptimized_real_today_usd": round(r_j85["real_today_usd"] - b_j85["real_today_usd"], 2),
            "delta_reoptimized_nominal_usd": round(r_j85["nominal_usd"] - b_j85["nominal_usd"], 2),
            "shifted_weighted_growth_pct": shifted_res.get("weighted_holding_growth_pct"),
            "rollover_shifted_rate_real_today_usd": sh_j85["real_today_usd"],
            "rollover_shifted_rate_nominal_usd": sh_j85["nominal_usd"],
            "delta_shifted_rate_real_today_usd": round(sh_j85["real_today_usd"] - b_j85["real_today_usd"], 2),
            "delta_shifted_rate_nominal_usd": round(sh_j85["nominal_usd"] - b_j85["nominal_usd"], 2),
            "solved_weighted_growth_pct": solved_res.get("weighted_holding_growth_pct"),
            "rollover_solved_rate_real_today_usd": so_j85["real_today_usd"],
            "rollover_solved_rate_nominal_usd": so_j85["nominal_usd"],
            "delta_solved_rate_real_today_usd": round(so_j85["real_today_usd"] - b_j85["real_today_usd"], 2),
            "delta_solved_rate_nominal_usd": round(so_j85["nominal_usd"] - b_j85["nominal_usd"], 2),
        }
        out["comparison_at_primary_milestone"] = comp_dict

    return _scrub_primary_symbol_keys(out)


def tool_solve_simulation(
    solve_for: str = "primary_equity",
    target_metric: str | None = None,
    target_value: float = 2_000_000.0,
    base_save: str | None = None,
    use_ref_rates: bool = False,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Solves for the exact model parameter (`primary_equity`, `broad_equity`, `all_holdings`, any `<forecast_key>`,
    `simAnnualBurn`, `simInflationPct`) required to reach `target_value` on `target_metric`, honoring `simActiveModel`.
    When `target_metric` is omitted/None, targets the primary investor's configured milestone in real dollars.
    """
    payload = get_cached_payload()
    eff_solve = solve_for
    if str(solve_for or "").strip().lower() in ("primary_equity", "primary", "primary_growth"):
        eff_solve = str(((payload.get("project") or {}).get("primary_equity_symbol")) or "").strip().upper() or "primary_growth"
    base_settings = _resolve_base_settings(base_save)
    eff_settings = build_effective_settings(
        payload,
        base_settings=base_settings,
        overrides=overrides,
        use_ref_rates=use_ref_rates,
    )
    solved = solve_simulation(
        payload,
        solve_for=eff_solve,
        target_metric=target_metric,
        target_value=float(target_value),
        settings=eff_settings,
    )
    if isinstance(solved.get("simulation_at_solved"), dict):
        solved["simulation_at_solved"] = _compact_eval_for_llm(solved["simulation_at_solved"])
    return _scrub_primary_symbol_keys({
        "ok": True,
        **solved,
    })


def _sanitize_saves_bundle_for_tool(bundle: dict[str, Any]) -> dict[str, Any]:
    """Strips local filesystem paths and portfolio-slug snapshot URLs from `tool_manage_saves` outputs (SA2-05)."""
    if not isinstance(bundle, dict):
        return bundle
    out = dict(bundle)
    out["saves_dir"] = ""
    for list_key in ("models", "saves"):
        if isinstance(out.get(list_key), list):
            cleaned_list: list[dict[str, Any]] = []
            for m in out[list_key]:
                if not isinstance(m, dict):
                    continue
                mc = dict(m)
                mc.pop("demo_snapshot", None)
                mc.pop("demo_snapshot_url", None)
                s_url = str(mc.get("snapshot_url") or "")
                if s_url.startswith("Portfolios/"):
                    parts = s_url.split("/", 2)
                    if len(parts) == 3:
                        mc["snapshot_url"] = parts[2]
                cleaned_list.append(mc)
            out[list_key] = cleaned_list
    return out


def tool_manage_saves(
    action: str = "list",
    name: str | None = None,
    base_save: str | None = None,
    use_ref_rates: bool = False,
    overrides: dict[str, Any] | None = None,
    reoptimize_order: bool = False,
    set_active: bool = True,
    overwrite_existing: bool = False,
    apply_to_live_ui: bool = True,
    *,
    id: str | None = None,
    new_name: str | None = None,
    notes_md: str | None = None,
    author: Any = None,
    model_name: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Manages canonical Scenarios in `Portfolios/<active>/Scenarios/` (`_index.json` + `<id>_<slug>.json` + `snapshots/<id>_<slug>.jpg`):
    - `action="list"`: Lists all Scenarios with their `id`, `name`, `kpi_summary`, `author`, and `notes_md`.
    - `action="get"`: Returns full Scenario details and live simulation summary for `id` or `name`.
    - `action="create"` or `"save"`: Creates or updates a canonical Scenario (auto-rendering its `1160x420` `.jpg` snapshot).
    - `action="load"`: Sets `id` or `name` as `active_id` and broadcasts it to the open browser UI.
    - `action="rename"`: Renames a Scenario's friendly display `name` without renaming disk files.
    - `action="upvote"`: Appends a timestamped AI concurrence signature under `## Upvotes & Concurrences` in `notes_md`
                         WITHOUT overwriting `settings` or auto-loading into the live UI (permitted on protected saves).
    - `action="notes"`: Updates `notes_md` on a Scenario (blocked on protected saves unless `overwrite_existing=True`).
    - `action="delete"`: Moves a non-protected Scenario to `Scenarios/.trash/`.
    """
    act = action.strip().lower()
    active_saves_dir = get_saves_dir()
    if active_saves_dir is None:
        raise ValueError("No active portfolio selected; select or create a portfolio first.")
    saves_info = _sanitize_saves_bundle_for_tool(list_saves_payload(active_saves_dir))
    models_list = saves_info.get("models") or []
    by_name = {m["name"].lower(): m for m in models_list}
    by_id = {m["id"].lower(): m for m in models_list}

    if act in ("list", "read"):
        return saves_info

    target_ref = str(id or name or "").strip()
    if not target_ref:
        raise ValueError(f"Scenario `name` or `id` is required for action={action!r}.")

    def _find_model(ref: str) -> dict[str, Any] | None:
        r_low = ref.lower()
        r_pad = f"{int(ref):03d}" if ref.isdigit() else ""
        if r_low in by_id:
            return by_id[r_low]
        if r_pad and r_pad in by_id:
            return by_id[r_pad]
        if r_low in by_name:
            return by_name[r_low]
        entry = get_model_entry(ref, saves_dir=active_saves_dir)
        if entry:
            return by_id.get(str(entry.get("id") or "").lower())
        return None

    if act == "get":
        entry = _find_model(target_ref)
        if not entry:
            raise ValueError(f"Scenario {target_ref!r} not found.")
        payload = get_cached_payload()
        sim = _compact_eval_for_llm(evaluate_active_model(payload, entry["settings"]))
        return {"ok": True, "model": entry, "save": entry, "simulation": sim}

    if act == "load":
        entry = _find_model(target_ref)
        if not entry:
            raise ValueError(f"Scenario {target_ref!r} not found.")
        res = set_active_save(entry["id"], saves_dir=active_saves_dir)
        if apply_to_live_ui:
            enqueue_ui_action(
                {
                    "type": "load_save",
                    "id": entry["id"],
                    "name": entry["name"],
                    "settings": entry["settings"],
                }
            )
        return {"ok": True, "loaded_id": entry["id"], "loaded_save": entry["name"], "saves_state": _sanitize_saves_bundle_for_tool(res)}

    if act == "upvote":
        entry = _find_model(target_ref)
        if not entry:
            raise ValueError(f"Scenario {target_ref!r} not found for upvote.")
        eff_model_name = str(model_name or (author.get("model") if isinstance(author, dict) else author) or "advisor")
        eff_reason = str(reason or notes_md or "Concurred after running independent simulation verification.")
        res = append_model_upvote(entry["id"], model_name=eff_model_name, reason=eff_reason, saves_dir=active_saves_dir)
        if apply_to_live_ui:
            enqueue_ui_action({"type": "sync_saves", "upvoted_id": entry["id"]})
        return {
            "ok": True,
            "upvoted_id": entry["id"],
            "upvoted_save": entry["name"],
            "signature": res.get("signature"),
            "saves_state": _sanitize_saves_bundle_for_tool(res),
        }

    if act == "rename":
        entry = _find_model(target_ref)
        if not entry:
            raise ValueError(f"Scenario {target_ref!r} not found.")
        if is_scenario_protected(entry["id"], saves_dir=active_saves_dir) and not overwrite_existing:
            raise ValueError(f"Refusing to rename protected Scenario {entry['name']!r} without `overwrite_existing=True`.")
        if not new_name:
            raise ValueError("`new_name` is required for action='rename'.")
        res = _sanitize_saves_bundle_for_tool(rename_model(entry["id"], new_name, saves_dir=active_saves_dir))
        if apply_to_live_ui:
            enqueue_ui_action({"type": "sync_saves", "renamed_id": entry["id"]})
        return res

    if act == "notes":
        entry = _find_model(target_ref)
        if not entry:
            raise ValueError(f"Scenario {target_ref!r} not found.")
        if is_scenario_protected(entry["id"], saves_dir=active_saves_dir) and not overwrite_existing:
            raise ValueError(
                f"Refusing to overwrite `notes_md` on protected Scenario {entry['name']!r}; use `action='upvote'` instead."
            )
        res = _sanitize_saves_bundle_for_tool(update_model_notes(entry["id"], str(notes_md or ""), saves_dir=active_saves_dir))
        if apply_to_live_ui:
            enqueue_ui_action({"type": "sync_saves", "updated_id": entry["id"]})
        return res

    if act == "delete":
        if is_scenario_protected(target_ref, saves_dir=active_saves_dir):
            raise ValueError(
                f"Refusing to delete protected Scenario {target_ref!r}. "
                f"Protected saves can only be removed manually."
            )
        res = _sanitize_saves_bundle_for_tool(delete_save_file(target_ref, saves_dir=active_saves_dir))
        if apply_to_live_ui:
            enqueue_ui_action({"type": "sync_saves"})
        return res

    if act in ("create", "save"):
        clean_name = sanitize_save_name(name or target_ref)
        existing_entry = _find_model(id) if id else None
        if existing_entry is None:
            existing_entry = by_name.get(clean_name.lower())
        if existing_entry is not None and not overwrite_existing:
            raise ValueError(
                f"Scenario {existing_entry['name']!r} (id={existing_entry['id']}) already exists. "
                f"Pass `overwrite_existing=True` to overwrite it, `action='upvote'` to concur with it, or choose a distinct name."
            )
        if (
            existing_entry is not None and is_scenario_protected(existing_entry["id"], saves_dir=active_saves_dir)
            or is_scenario_protected(clean_name, saves_dir=active_saves_dir)
        ) and not overwrite_existing:
            raise ValueError(f"Refusing to overwrite protected Scenario {clean_name!r}.")

        payload = get_cached_payload()
        base_settings = _resolve_base_settings(base_save)
        eff_settings = build_effective_settings(
            payload,
            base_settings=base_settings,
            overrides=overrides,
            use_ref_rates=use_ref_rates,
        )
        if reoptimize_order:
            mat_assets = materialize_sim_assets(payload.get("retirement_sim_assets") or [], eff_settings, payload)
            eff_settings["simAssetOrder"] = optimize_sim_asset_order(
                mat_assets,
                str(payload.get("today_iso")),
                eff_settings,
                payload.get("ssn_model") or {},
                payload=payload,
            )
            eff_settings["simOrderPreset"] = "auto_optimal"

        eval_full = evaluate_active_model(payload, eff_settings)
        goog_anchor = float(((payload.get("kpis") or {}).get("goog_price")) or 0.0) or None
        res = _sanitize_saves_bundle_for_tool(
            write_save_file(
                clean_name,
                eff_settings,
                set_active=set_active,
                saves_dir=active_saves_dir,
                model_id=existing_entry["id"] if existing_entry else None,
                create_new=(existing_entry is None),
                author=author,
                notes_md=notes_md,
                kpi_summary=eval_full.get("kpi_summary"),
                goog_price_anchor=goog_anchor,
            )
        )
        saved_id = res.get("saved_id")
        saved_entry = next((m for m in (res.get("models") or []) if m["id"] == saved_id), None)
        sim_compact = _compact_eval_for_llm(eval_full)

        if apply_to_live_ui:
            enqueue_ui_action(
                {
                    "type": "load_save" if set_active else "sync_saves",
                    "id": saved_id,
                    "name": clean_name,
                    "settings": eff_settings,
                }
            )
        return {
            "ok": True,
            "saved_id": saved_id,
            "created_save": clean_name,
            "filename": saved_entry["file"] if saved_entry else f"{saved_id}_{clean_name}.json",
            "snapshot": saved_entry["snapshot"] if saved_entry else None,
            "kpi_summary": eval_full.get("kpi_summary"),
            "settings": eff_settings,
            "simulation": sim_compact,
            "saves_state": res,
        }

    raise ValueError(
        f"Unsupported action={action!r}. Use 'list', 'get', 'create', 'save', 'load', 'rename', 'upvote', 'notes', or 'delete'."
    )


def tool_control_live_ui(
    settings_patch: dict[str, Any] | None = None,
    view_patch: dict[str, Any] | None = None,
    switch_screen: str | None = None,
    refresh_action: str | None = None,
    use_ref_rates: bool = False,
    reoptimize_order: bool = False,
) -> dict[str, Any]:
    """
    Tunes settings, switches screens (`models`, `portfolio`, `primary`, `retirement`, `income`),
    or triggers a live refresh in the open webpage (`index.html`) in realtime.
    """
    payload = get_cached_payload()
    today_iso = str(payload.get("today_iso"))
    patch: dict[str, Any] = {}
    if use_ref_rates:
        refs = get_reference_rates(payload)
        hf_ref: dict[str, dict[str, Any]] = {}
        proj_sym = str(((payload.get("project") or {}).get("primary_equity_symbol")) or "").strip().upper()
        if proj_sym:
            hf_ref[proj_sym] = {"growth_pct": float(refs["goog_ref_pct"]), "risk_class": "single_stock"}
        for a in payload.get("retirement_sim_assets") or []:
            fk = str(a.get("forecast_key") or a["id"])
            entry_ref: dict[str, Any] = {
                "growth_pct": float(a.get("ref_growth_pct", 0.0)),
                "risk_class": str(a.get("default_risk_class") or "broad_equity"),
            }
            if a.get("realized_vol_pct") is not None:
                entry_ref["vol_pct"] = round(float(a["realized_vol_pct"]), 2)
            hf_ref[fk] = entry_ref
        patch["holdingForecasts"] = hf_ref
        patch["holdingHalfLives"] = {}
        patch["simOptimismMultiplier"] = 1.0
        patch["simAnnualBurn"] = refs["burn_ref_usd"]
        patch["simInflationPct"] = refs["inflation_ref_pct"]
        patch["ssnColaPct"] = refs["ssn_cola_ref_pct"]
        # DA2-18: Reset scenario progressive-tax & retirement-age overrides to None so they inherit Portfolio Reference settings.
        patch["simMarriedFilingJointly"] = None
        patch["simMinTaxBracketFloor"] = None
        patch["simInvestor1RetirementAge"] = None
        patch["simInvestor2RetirementAge"] = None
        ssn_invs = (payload.get("ssn_model") or {}).get("investors") or []
        k0 = ssn_invs[0].get("claim_setting_key", "ssnInvestor1ClaimAge") if len(ssn_invs) > 0 else "ssnInvestor1ClaimAge"
        k1 = ssn_invs[1].get("claim_setting_key", "ssnInvestor2ClaimAge") if len(ssn_invs) > 1 else "ssnInvestor2ClaimAge"
        patch[k0] = refs.get("ssn_inv0_claim_ref_age", 67)
        patch[k1] = refs.get("ssn_inv1_claim_ref_age", 67)

    if isinstance(settings_patch, dict):
        for k, v in settings_patch.items():
            if k in ("withdrawals", "simScheduledWithdrawals", "scheduled_actions", "simScheduledActions"):
                patch["simScheduledActions"] = normalize_scheduled_actions(v, today_iso)
            elif k in ("hypothetical_assets", "simHypotheticalAssets"):
                patch["simHypotheticalAssets"] = normalize_hypothetical_assets(v)
            elif k == "holding_forecasts":
                patch["holdingForecasts"] = v
            else:
                patch[k] = v

    screen_aliases = {
        "main": "portfolio",
        "holdings": "portfolio",
        "portfolio": "portfolio",
        "sim": "retirement",
        "simulator": "retirement",
        "retirement": "retirement",
        "primary": "primary",
        "equity": "primary",
        "vesting": "primary",
        "gsu": "primary",
        "grants": "primary",
        "stock_grants": "primary",
        "google": "primary",
        "ssn": "income",
        "social-security": "income",
        "income": "income",
        "models": "models",
        "scenarios": "models",
        "saves": "models",
        "settings": "settings",
        "cfg": "settings",
    }
    clean_screen = screen_aliases.get(str(switch_screen).strip().lower(), switch_screen) if switch_screen else None

    raw_ra = str(refresh_action or "").strip()
    mapped_ra = "primary_quote" if raw_ra.lower() in ("primary_quote", "goog_quote") else (raw_ra or None)
    cmd = {
        "type": "control_ui",
        "settings_patch": patch,
        "view_patch": view_patch or {},
        "switch_screen": clean_screen,
        "refresh_action": mapped_ra,
        "reoptimize_order": bool(reoptimize_order),
    }
    q_res = enqueue_ui_action(cmd)
    return {
        "ok": True,
        "queued_command": cmd,
        "queue_seq": q_res.get("seq"),
    }


# =============================================================================
# TOOL DECLARATIONS & EXECUTION ROUTER
# =============================================================================

# SECURITY DO: Keep tool schema descriptions free of household member names, real account IDs, or real tickers (`SA-03`);
# `_shielded_tool_declarations(translator)` still scrubs declarations before wire egress as defense in depth.
# SECURITY DON'T: Never embed PII examples or raw account strings inside `GEMINI_TOOL_DECLARATIONS` / `TOOL_DECLARATIONS`.
GEMINI_TOOL_DECLARATIONS = [
    {
        "name": "holdings__get_overview",
        "description": (
            "Returns portfolio KPIs, live equity price, Reference (`Ref:`) growth rates, per-holding `forecast_key` "
            "& `default_risk_class`, family member birthdays, simulator asset IDs, and canonical Scenarios in `Scenarios/`."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "holdings__query_positions",
        "description": (
            "Lists portfolio positions and contracts with optional filters for maturity date (`has_maturity=True` "
            "returns all dated contracts/bonds sorted chronologically), `account_id`, or `symbol`."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "has_maturity": {
                    "type": "BOOLEAN",
                    "description": "True to return only holdings with a maturity date (Municipal Bonds + Annuities).",
                },
                "account_id": {
                    "type": "STRING",
                    "description": "Optional account ID substring filter.",
                },
                "symbol": {
                    "type": "STRING",
                    "description": "Optional symbol/title substring filter (e.g. 'AAPL', 'Annuity').",
                },
            },
        },
    },
    {
        "name": "holdings__run_simulation",
        "description": (
            "Evaluates the 40-year monthly Retirement Simulator (and 100-year solvency check) honoring `simActiveModel` "
            "(`single_path`, `stress`, `range`, or `historical`), Market Correction (`simStressScenario`), multi-tier "
            "`holdingForecasts` & `holdingHalfLives`, `simOptimismMultiplier`, liquidation order (`simOrderPreset`, "
            "`simAssetOrder`, `simBearShield`), `simScheduledActions`, and `$0`-start `simHypotheticalAssets`."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "base_save": {
                    "type": "STRING",
                    "description": "Optional Scenario ID or name (e.g. '001', 'Default').",
                },
                "use_ref_rates": {
                    "type": "BOOLEAN",
                    "description": "If True, initializes holding forecasts, burn, and inflation to Reference (`Ref:`) values.",
                },
                "overrides": {
                    "type": "OBJECT",
                    "description": (
                        "Optional settings overrides: "
                        "simActiveModel ('single_path'|'stress'|'range'|'historical'), "
                        "simStressScenario ('none'|'gfc2008'|'dotcom2000'|'covid2020'|'rates2022'|'custom'), "
                        "simCustomShockPct, simCustomDownMonths, simCustomBroadBounceMonths, simCustomStockBounceMonths, "
                        "simOptimismMultiplier (growth multiplier, default 1.0), "
                        "holdingForecasts ({forecast_key: {growth_pct, risk_class, vol_pct, fee_pct}}), "
                        "holdingHalfLives ({forecast_key: years}), "
                        "mcReturnModel ('normal'|'historical'), mcSimulations, mcSingleStockVolPct, mcBroadEquityVolPct, mcSingleStockCorr, "
                        "cohortCenterMode ('anchor'|'raw'), cohortStartFrom (year int, default 1947), cohortInflationMode ('real_cpi'|'nominal'), "
                        "simAnnualBurn, simInflationPct, taxMode ('net'|'gross'), "
                        "simIncludeUnvested (bool), simIncludeSSN (bool), ssnColaPct, <claim_setting_key> (from ssn_model.investors), "
                        "simInvestor1RetirementAge (int|null), simInvestor2RetirementAge (int|null), "
                        "simIncludeScheduledActions (bool), simMarriedFilingJointly (bool), "
                        "simMinTaxBracketFloor ('none'|'10'|'12'|'22'|'24'|'32'|'35'|'37'), "
                        "simBearShield (bool), simOrderPreset ('tax_optimal'|'auto_optimal'|'heir_legacy'|'custom'), "
                        "simAssetOrder ([bucket_or_asset_id, ...]), "
                        "simScheduledActions ([{type:'withdrawal'|'deposit'|'transfer', cadence:'one_time'|'monthly'|'quarterly'|'annual'|'on_vest', amount_mode:'usd'|'pct_of_source', amount, ym:'YYYY-MM', from_asset_id, to_asset_id, dollar_mode:'today'|'nominal'}]), "
                        "simHypotheticalAssets ([{id, label, risk_class, growth_pct, expense_ratio_pct}])."
                    ),
                },
                "rollovers": {
                    "type": "ARRAY",
                    "description": "Optional Day-0 hypothetical trades, e.g. [{'from_id': '<source_id>', 'to_id': '<dest_id>'}].",
                    "items": {"type": "OBJECT"},
                },
                "reoptimize_order": {
                    "type": "BOOLEAN",
                    "description": "If True, runs Auto-Optimize on the asset liquidation order.",
                },
                "compare_with_baseline": {
                    "type": "BOOLEAN",
                    "description": "If True, also returns the unchanged baseline summary alongside the simulation result.",
                },
                "apply_non_primary_rate_mode": {
                    "type": "STRING",
                    "description": "'none' (default), 'shift', or 'solved'.",
                },
                "sleeve_rate_mode": {
                    "type": "STRING",
                    "description": "'ytd_statement', 'matched_window', 'full_history', or 'excluded'.",
                },
            },
        },
    },
    {
        "name": "holdings__compute_non_primary_growth",
        "description": (
            "Computes the market-value-weighted average annualized growth rate across diversified (non-primary-equity) holdings "
            "BEFORE and AFTER a hypothetical transaction/rollover."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "rollovers": {
                    "type": "ARRAY",
                    "description": "Optional Day-0 hypothetical trades, e.g. [{'from_id': '<source_id>', 'to_id': '<dest_id>'}].",
                    "items": {"type": "OBJECT"},
                },
                "sleeve_rate_mode": {"type": "STRING", "description": "'ytd_statement', 'matched_window', 'full_history', or 'excluded'."},
                "muni_rate_mode": {"type": "STRING", "description": "'price_cagr' or 'coupon_yield'."},
                "include_cash": {"type": "BOOLEAN", "description": "Include cash_sweeps in weighted average denominator."},
                "asset_rate_overrides": {"type": "OBJECT", "description": "Optional custom {asset_id: pct} overrides."},
            },
        },
    },
    {
        "name": "holdings__solve_simulation",
        "description": (
            "Goal-seeks the exact parameter (`primary_equity`, `broad_equity`, `all_holdings`, any `<forecast_key>`, `simAnnualBurn`, "
            "`simInflationPct`, or `initial_portfolio_value`) needed to achieve `target_value` on `target_metric`, honoring `simActiveModel`."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "solve_for": {
                    "type": "STRING",
                    "description": "'primary_equity', 'broad_equity', 'all_holdings', any holding forecast_key, 'simAnnualBurn', 'simInflationPct', or 'initial_portfolio_value' (scales Day-0 vested portfolio value).",
                },
                "target_metric": {
                    "type": "STRING",
                    "description": "Milestone metric (e.g. 'investor_1_milestone_real', '<investor_id>_<milestone_age>_real', 'yr_40_real') or 'solvency_pct' (target % funded to primary milestone under range/historical, e.g. target_value=90).",
                },
                "target_value": {
                    "type": "NUMBER",
                    "description": "Target dollar balance at the milestone (e.g. 2000000), or target solvency % (0-100, e.g. 90) when target_metric='solvency_pct'.",
                },
                "base_save": {"type": "STRING", "description": "Optional base Scenario ID or name."},
                "use_ref_rates": {"type": "BOOLEAN", "description": "If True, uses Reference rates for non-solved parameters."},
                "overrides": {"type": "OBJECT", "description": "Optional overrides for locked parameters."},
            },
            "required": ["solve_for", "target_value"],
        },
    },
    {
        "name": "holdings__manage_saves",
        "description": (
            "Lists, inspects, creates new distinct candidate Scenarios, loads, or upvotes Scenarios in `Portfolios/<active>/Scenarios/` "
            "and syncs the open browser UI. Use `action='upvote'` to append a timestamped concurrence signature "
            "to an existing Scenario's `notes_md` without overwriting its settings or auto-loading it. "
            "Overwriting or deleting existing Scenarios requires explicit manual user confirmation in the Scenarios UI."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "'list', 'get', 'create', 'save', 'load', or 'upvote'.",
                },
                "id": {"type": "STRING", "description": "Optional 3-digit Scenario ID (e.g. '001')."},
                "name": {"type": "STRING", "description": "Scenario friendly display name."},
                "base_save": {"type": "STRING", "description": "Optional existing Scenario ID or name to branch from."},
                "use_ref_rates": {"type": "BOOLEAN", "description": "If True, populates Reference (`Ref:`) rates."},
                "overrides": {"type": "OBJECT", "description": "Settings overrides matching holdings__run_simulation.overrides (holdingForecasts with vol_pct/fee_pct, holdingHalfLives, simActiveModel, simStressScenario, simOptimismMultiplier, MC/cohort params, simOrderPreset, simAssetOrder, simBearShield, simMarriedFilingJointly, simMinTaxBracketFloor, simInvestor1RetirementAge, simInvestor2RetirementAge, SSN claim ages, simScheduledActions, simHypotheticalAssets)."},
                "notes_md": {"type": "STRING", "description": "Structured Markdown thesis/notes stored on the Scenario."},
                "author": {"type": "OBJECT", "description": "Author metadata ({type: 'ai_advisor'|'human', model: '...', prompt_type: '...'})."},
                "model_name": {"type": "STRING", "description": "LLM model identifier signing an `action='upvote'` concurrence."},
                "reason": {"type": "STRING", "description": "Concise 1-2 sentence concurrence rationale for `action='upvote'`."},
                "reoptimize_order": {"type": "BOOLEAN", "description": "If True, runs Auto-Optimize on liquidation order before saving."},
                "set_active": {"type": "BOOLEAN", "description": "Whether to mark the saved Scenario active (pass False for background candidate proposals)."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "holdings__control_live_ui",
        "description": (
            "Tunes settings, switches screens ('models', 'portfolio', 'primary', 'retirement', 'income'), "
            "or triggers a live refresh ('primary_quote', 'reload_page') in the open browser window."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "settings_patch": {"type": "OBJECT", "description": "Settings to update live in the browser (same keys as holdings__run_simulation.overrides: holdingForecasts, holdingHalfLives, simActiveModel, simStressScenario, simCustomShockPct, simOptimismMultiplier, simAnnualBurn, simInflationPct, taxMode, simIncludeUnvested, simIncludeSSN, claim-age keys, simBearShield, simOrderPreset, simAssetOrder, mcReturnModel, mcSimulations, cohortCenterMode, cohortStartFrom, simScheduledActions, simHypotheticalAssets)."},
                "view_patch": {"type": "OBJECT", "description": "View toggles: simDollarMode ('real'|'nominal'), simScaleMode ('auto'|'log'|'linear'), simActiveModel ('single_path'|'stress'|'range'|'historical'), simPreset, and stackMode."},
                "switch_screen": {"type": "STRING", "description": "'models', 'portfolio', 'primary', 'retirement', or 'income'."},
                "refresh_action": {"type": "STRING", "description": "'primary_quote' or 'reload_page'."},
                "use_ref_rates": {"type": "BOOLEAN", "description": "If True, sets live forecasts/burn/inflation to Reference (`Ref:`) values."},
                "reoptimize_order": {"type": "BOOLEAN", "description": "If True, triggers Auto-Optimize in the live browser UI."},
            },
        },
    },
]

TOOL_DECLARATIONS = GEMINI_TOOL_DECLARATIONS

_TOOL_ARG_ALIASES = {
    "base_save": ["base_save_name"],
    "overrides": ["settings_overrides"],
    "rollovers": ["hypothetical_rollovers"],
    "use_ref_rates": ["use_reference_rates"],
    "switch_screen": ["switch_tab"],
    "settings_patch": ["tune_settings"],
    "target_value": ["target_amount"],
    "overwrite_existing": ["allow_overwrite"],
    "apply_to_live_ui": ["push_to_live_ui"],
}


def _normalize_tool_args(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    a = dict(args or {})
    for canon, aliases in _TOOL_ARG_ALIASES.items():
        for al in aliases:
            if al in a and canon not in a:
                a[canon] = a.pop(al)
    if name in ("holdings__run_simulation", "holdings__solve_simulation"):
        for top_key, ov_key in (
            ("gifts", "scheduled_actions"),
            ("scheduled_actions", "scheduled_actions"),
            ("hypothetical_assets", "hypothetical_assets"),
            ("holding_forecasts", "holdingForecasts"),
        ):
            if a.get(top_key) is not None:
                ov = dict(a.get("overrides") or {})
                ov[ov_key] = a.pop(top_key)
                a["overrides"] = ov
    if name == "holdings__solve_simulation" and "target_metric" not in a and a.get("target_milestone"):
        ms = str(a.pop("target_milestone")).lower()
        dm = "nominal" if str(a.pop("target_dollar_mode", "real")).lower() == "nominal" else "real"
        a["target_metric"] = f"{ms}_{dm}"
    return a


def _describe_tool_step(name: str, a: dict[str, Any]) -> str:
    if name == "holdings__get_overview":
        return "Inspecting portfolio overview, holdings & existing Scenarios"
    if name == "holdings__query_positions":
        return "Querying positions & maturity schedule"
    if name == "holdings__run_simulation":
        bs = a.get("base_save") or "active state"
        return f"Running 40yr + 100yr simulation on {bs}"
    if name == "holdings__solve_simulation":
        return f"Goal-seeking {a.get('solve_for')} for {a.get('target_metric')}"
    if name == "holdings__manage_saves":
        act = a.get("action") or "list"
        nm = a.get("name") or a.get("id") or ""
        return f"Scenarios store: {act} {nm}".strip()
    if name == "holdings__control_live_ui":
        return "Updating live browser UI"
    return f"Executing {name}"


def execute_named_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    a = _normalize_tool_args(name, args)
    demo_cfg = _active_demo_privacy_cfg()
    # Emit lightweight progress telemetry so any active Advisor Drawer job shows real-time tool steps
    try:
        enqueue_ui_action(
            {
                "type": "advisor_tool_step",
                "tool": name,
                "summary": _project_demo_step_text(_describe_tool_step(name, a), demo_cfg),
            }
        )
    except Exception:
        pass

    if demo_cfg is not None:
        if name == "holdings__manage_saves":
            act_l = str(a.get("action") or "list").strip().lower()
            if act_l not in ("list", "read", "get"):
                return {
                    "ok": False,
                    "demo_blocked": True,
                    "error": "Demo Mode is read-only; saving or modifying scenarios is disabled.",
                }
        elif name == "holdings__control_live_ui":
            if (
                a.get("settings_patch")
                or a.get("load_save_id")
                or a.get("load_save")
                or a.get("load_save_name")
                or a.get("reoptimize_order")
                or a.get("use_ref_rates")
                or str(a.get("refresh_action") or "").strip().lower() == "rebuild_site"
            ):
                return {
                    "ok": False,
                    "demo_blocked": True,
                    "error": "Demo Mode is read-only; modifying live simulator settings or scenarios is disabled.",
                }

    draft = _advisor_draft.get()
    if draft is not None:
        if name == "holdings__control_live_ui":
            return {"ok": False, "error": "Use a simulation to compare settings during an Advisor review."}
        if name == "holdings__manage_saves":
            act = str(a.get("action") or "list").strip().lower()
            if act not in ("list", "get", "read"):
                if act not in ("create", "save", "upvote"):
                    return {"ok": False, "error": "Advisor reviews can propose a new scenario or a vote."}
                default_model = str(draft.get("model") or "Advisor").strip() or "Advisor"
                if act == "upvote":
                    target_ref = str(a.get("id") or a.get("name") or "").strip()
                    if not target_ref:
                        return {"ok": False, "error": "Provide the existing scenario ID."}
                    saves_bundle = list_saves_payload()
                    models_list = saves_bundle.get("models") or []
                    t_low = target_ref.lower()
                    t_pad = f"{int(target_ref):03d}" if target_ref.isdigit() else ""
                    matched_entry = next(
                        (
                            m
                            for m in models_list
                            if str(m.get("id") or "").lower() in (t_low, t_pad)
                            or str(m.get("name") or "").strip().lower() == t_low
                        ),
                        None,
                    )
                    if matched_entry is None:
                        matched_entry = get_model_entry(target_ref)
                    if matched_entry is None:
                        return {"ok": False, "error": f"Scenario {target_ref!r} was not found."}
                    a["id"] = str(matched_entry["id"])
                    if not a.get("model_name"):
                        a["model_name"] = default_model
                    else:
                        a["model_name"] = str(a["model_name"])
                    proposal = {k: a.get(k) for k in ("id", "model_name", "reason")}
                    proposal["action"] = "upvote"
                    label = f"Upvote scenario {a['id']}"
                else:
                    if not a.get("name"):
                        return {"ok": False, "error": "Provide a name for the proposed scenario."}
                    if not a.get("author"):
                        a["author"] = {"type": "ai", "model": default_model}
                    clean_base = sanitize_save_name(str(a["name"]))
                    disambig_name = clean_base
                    if not a.get("overwrite_existing"):
                        saves_bundle = list_saves_payload()
                        existing_names = {
                            sanitize_save_name(str(m.get("name") or "")).lower()
                            for m in (saves_bundle.get("models") or [])
                            if isinstance(m, dict) and m.get("name")
                        }
                        if disambig_name.lower() in existing_names:
                            cand = sanitize_save_name(f"{clean_base[:80]} (Advisor)")
                            suffix_idx = 2
                            while cand.lower() in existing_names:
                                cand = sanitize_save_name(f"{clean_base[:76]} (Advisor {suffix_idx})")
                                suffix_idx += 1
                            disambig_name = cand
                    a["name"] = disambig_name
                    proposal = {
                        k: a[k]
                        for k in ("name", "base_save", "overrides", "notes_md", "author", "use_ref_rates", "reoptimize_order")
                        if k in a
                    }
                    proposal.update(action="create", set_active=False, overwrite_existing=False, apply_to_live_ui=False)
                    label = f"Save scenario “{a['name']}”"
                draft.update(args=proposal, label=label, proposal_label=label)
                return {"ok": True, "pending_user_choice": True, "message": "Proposal prepared. Present the analysis to the user."}

    try:
        if name == "holdings__get_overview":
            return tool_get_overview()
        if name == "holdings__query_positions":
            return tool_query_positions(
                has_maturity=a.get("has_maturity"),
                account_id=a.get("account_id"),
                symbol=a.get("symbol"),
            )
        if name == "holdings__run_simulation":
            return tool_run_simulation(
                base_save=a.get("base_save"),
                use_ref_rates=bool(a.get("use_ref_rates", False)),
                overrides=a.get("overrides"),
                rollovers=a.get("rollovers"),
                reoptimize_order=bool(a.get("reoptimize_order", False)),
                compare_with_baseline=bool(a.get("compare_with_baseline", False)),
                apply_non_primary_rate_mode=str(a.get("apply_non_primary_rate_mode") or a.get("apply_non_goog_rate_mode") or "none"),
                sleeve_rate_mode=str(a.get("sleeve_rate_mode") or a.get("uma_rate_mode") or "ytd_statement"),
            )
        if name == "holdings__compute_non_primary_growth":
            return tool_compute_non_primary_growth(
                rollovers=a.get("rollovers"),
                sleeve_rate_mode=str(a.get("sleeve_rate_mode") or a.get("uma_rate_mode") or "ytd_statement"),
                muni_rate_mode=str(a.get("muni_rate_mode") or "price_cagr"),
                include_cash=bool(a.get("include_cash", False)),
                asset_rate_overrides=a.get("asset_rate_overrides"),
            )
        if name == "holdings__solve_simulation":
            return tool_solve_simulation(
                solve_for=str(a.get("solve_for") or "primary_equity"),
                target_metric=str(a["target_metric"]) if a.get("target_metric") else None,
                target_value=float(a.get("target_value") or 2_000_000.0),
                base_save=a.get("base_save"),
                use_ref_rates=bool(a.get("use_ref_rates", False)),
                overrides=a.get("overrides"),
            )
        if name == "holdings__manage_saves":
            return tool_manage_saves(
                action=str(a.get("action") or "list"),
                name=a.get("name"),
                base_save=a.get("base_save"),
                use_ref_rates=bool(a.get("use_ref_rates", False)),
                overrides=a.get("overrides"),
                reoptimize_order=bool(a.get("reoptimize_order", False)),
                set_active=bool(a.get("set_active", True)),
                overwrite_existing=bool(a.get("overwrite_existing", False)),
                apply_to_live_ui=bool(a.get("apply_to_live_ui", True)),
                id=a.get("id"),
                new_name=a.get("new_name"),
                notes_md=a.get("notes_md"),
                author=a.get("author"),
                model_name=a.get("model_name"),
                reason=a.get("reason"),
            )
        if name == "holdings__control_live_ui":
            return tool_control_live_ui(
                settings_patch=a.get("settings_patch"),
                view_patch=a.get("view_patch"),
                switch_screen=a.get("switch_screen"),
                refresh_action=a.get("refresh_action"),
                use_ref_rates=bool(a.get("use_ref_rates", False)),
                reoptimize_order=bool(a.get("reoptimize_order", False)),
            )
        return {"ok": False, "error": f"Unknown tool {name!r}", "error_class": "unknown_tool"}
    except Exception as exc:
        logging.getLogger("llm_tools").exception("tool %s failed: %s", name, exc)
        return {"ok": False, "error": _sanitize_error_for_llm(exc), "error_class": type(exc).__name__}


# =============================================================================
# QUICK CHIP FAST-PATHS
# =============================================================================


def _handle_fast_path_maturity() -> dict[str, Any]:
    pos_res = tool_query_positions(has_maturity=True)
    demo_cfg = _active_demo_privacy_cfg()
    if demo_cfg is not None:
        pos_res = DemoTranslator(demo_cfg).project_obj(pos_res)
    positions = pos_res.get("positions", [])
    lines = [
        "### Holdings with Maturity / Surrender-Free Dates\n",
        f"Found **{len(positions)} dated contracts and bonds** in the portfolio, sorted chronologically:\n",
        "| Maturity / Surrender Date | Account | Holding / Contract | Current Value | Category & Details |",
        "|---|---|---|---|---|",
    ]
    for p in positions:
        mat = p.get("maturity_date") or "N/A"
        acct = p.get("account_label") or p.get("account_id") or ""
        desc = p.get("title") or p.get("description") or p.get("symbol") or ""
        val = f"${p.get('current_value', p.get('market_value', 0)):,.2f}"
        cat = p.get("category") or p.get("investment_type") or ""
        growth = p.get("growth_str") or ""
        note = f"{cat} • {growth}" if growth else cat
        lines.append(f"| `{mat}` | {acct} | **{desc}** | {val} | {note} |")
    return {
        "ok": True,
        "engine": "agy",
        "reply": "\n".join(lines),
        "tool_calls": [{"name": "holdings__query_positions", "args": {"has_maturity": True}}],
        "ui_events": [],
    }


def try_quick_chip_fast_path(prompt: str, ui_state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    p = (prompt or "").strip().lower()
    if not p:
        return None
    if "list each holding with a maturity date" in p or "maturity dates" in p:
        return _handle_fast_path_maturity()
    return None


# =============================================================================
# LLM CONFIG DISCOVERY & PROBING
# =============================================================================


_OFFICIAL_PROVIDER_HOSTS: dict[str, set[str]] = {
    "gemini": {"generativelanguage.googleapis.com"},
    "claude": {"api.anthropic.com"},
    "anthropic": {"api.anthropic.com"},
    "openai": {"api.openai.com"},
}


def _check_base_url_trust(provider: str, base_url: str | None) -> str | None:
    """Returns a warning string if `base_url` is non-loopback and outside known provider domains (`SA-17`)."""
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        host = (urllib.parse.urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return f"Invalid api_base_url {raw!r}"
    if not host or host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return None
    allowed = _OFFICIAL_PROVIDER_HOSTS.get((provider or "").strip().lower(), set())
    if allowed and host not in allowed:
        return f"Non-standard {provider} host {host!r} (expected {', '.join(sorted(allowed))})"
    return None


def load_all_llm_configs() -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}

    # Portfolio-specific LLM configs override global Data/Config/LLM configs (`SA-17` / `DOC-10`)
    # SECURITY DO: Load portfolio-scoped overrides before global `Data/Config/LLM/` and warn on non-official hosts via `_check_base_url_trust`.
    # SECURITY DON'T: Never log or expose `api_key` values in serialized model lists (`list_available_models`) or UI responses.
    llm_dirs: list[tuple[Path, str]] = []
    active_pdir = get_portfolio_dir()
    if active_pdir is not None:
        llm_dirs.append((active_pdir / "LLM", f"Portfolios/{active_pdir.name}/LLM"))
    llm_dirs.append((get_llm_dir(), "Data/Config/LLM"))

    for cfg_dir, rel_label in llm_dirs:
        if not cfg_dir.is_dir():
            continue
        for path in sorted(cfg_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            stem = path.stem.lower()
            if stem.startswith("llm_config."):
                stem = stem[len("llm_config.") :]
            if stem in configs:
                continue
            provider = str(raw.get("provider") or stem).strip().lower()
            model_name = str(raw.get("model") or "").strip()
            api_key = str(raw.get("api_key") or "").strip()
            if (api_key.startswith('"') and api_key.endswith('"')) or (api_key.startswith("'") and api_key.endswith("'")):
                api_key = api_key[1:-1].strip()

            if not api_key or api_key.startswith("PASTE_YOUR_"):
                env_map = {
                    "gemini": "GEMINI_API_KEY",
                    "claude": "ANTHROPIC_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                    "openai": "OPENAI_API_KEY",
                }
                env_k = os.environ.get(env_map.get(provider, ""))
                if env_k:
                    api_key = env_k.strip()
                    if (api_key.startswith('"') and api_key.endswith('"')) or (api_key.startswith("'") and api_key.endswith("'")):
                        api_key = api_key[1:-1].strip()
                    raw["api_key"] = api_key

            is_placeholder = (
                any(api_key.upper().startswith(p) for p in ("PASTE_YOUR_", "YOUR_", "<YOUR", "REPLACE_", "TODO"))
                or len(api_key) < 10
            )
            has_key = bool(api_key and not is_placeholder)
            display_name = str(raw.get("display_name") or "").strip()
            if not display_name:
                labels = {
                    "gemini": f"Gemini ({model_name or '2.5-flash'})",
                    "claude": f"Claude ({model_name or 'sonnet-4-5'})",
                    "anthropic": f"Claude ({model_name or 'sonnet-4-5'})",
                    "openai": f"OpenAI ({model_name or 'gpt-4.1'})",
                }
                display_name = labels.get(provider, f"{provider.capitalize()} ({model_name})")

            configs[stem] = {
                "id": stem,
                "provider": provider,
                "model": model_name,
                "display_name": display_name,
                "api_key": api_key,
                "config_file": f"{rel_label}/{path.name}",
                "has_key": has_key,
                **raw,
            }

    legacy_path = (active_pdir / "llm_config.json") if active_pdir is not None else None
    if legacy_path is not None and legacy_path.exists():
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                p = str(raw.get("provider") or "legacy").strip().lower()
                if p not in configs:
                    raw["id"] = p
                    raw["config_file"] = f"Portfolios/{active_pdir.name}/llm_config.json"
                    raw["has_key"] = bool(raw.get("api_key") and not str(raw.get("api_key")).startswith("PASTE_YOUR_"))
                    configs[p] = raw
        except Exception:
            pass

    for env_var, prov, def_m, disp in (
        ("GEMINI_API_KEY", "gemini", "gemini-2.5-flash", "Gemini 2.5 Flash (env)"),
        ("ANTHROPIC_API_KEY", "claude", "claude-sonnet-4-5-20250929", "Claude Sonnet 4.5 (env)"),
        ("OPENAI_API_KEY", "openai", "gpt-4.1", "OpenAI GPT-4.1 (env)"),
    ):
        if prov not in configs:
            val = (os.environ.get(env_var) or "").strip()
            if val:
                configs[prov] = {
                    "id": prov,
                    "provider": prov,
                    "model": def_m,
                    "display_name": disp,
                    "api_key": val,
                    "config_file": f"ENV:{env_var}",
                    "has_key": True,
                }
    return configs


def _expand_cfg_models(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Expands a single provider JSON config into selectable Direct API model configs.
    When `models` is present, emits one entry per model preserving top-level credentials/settings.
    """
    stem = str(cfg.get("id") or cfg.get("provider") or "llm").strip().lower()
    provider = str(cfg.get("provider") or stem).strip().lower()
    base_model = str(cfg.get("model") or "").strip()
    raw_models = cfg.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        return [
            {
                **cfg,
                "raw_id": stem,
                "id": f"llm:{stem}",
                "model": base_model,
                "is_default": True,
            }
        ]

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_models:
        if isinstance(item, str):
            m_id = item.strip()
            meta: dict[str, Any] = {}
        elif isinstance(item, dict):
            m_id = str(item.get("id") or item.get("model") or "").strip()
            meta = item
        else:
            continue
        if not m_id or m_id.lower() in seen_ids:
            continue
        seen_ids.add(m_id.lower())
        disp = str(meta.get("display_name") or "").strip() or (
            str(cfg.get("display_name") or "").strip() if m_id == base_model else f"{provider.capitalize()} ({m_id})"
        )
        merged = dict(cfg)
        for ov_k in ("display_name", "tier", "description", "max_tokens", "max_tool_rounds", "temperature", "omit_temperature", "use_max_completion_tokens", "api_base_url"):
            if ov_k in meta and meta[ov_k] is not None:
                merged[ov_k] = meta[ov_k]
        merged["raw_id"] = stem
        merged["id"] = f"llm:{stem}:{m_id}"
        merged["model"] = m_id
        merged["display_name"] = disp
        merged["is_default"] = bool(base_model and m_id.lower() == base_model.lower())
        out.append(merged)

    if base_model and base_model.lower() not in seen_ids:
        out.insert(
            0,
            {
                **cfg,
                "raw_id": stem,
                "id": f"llm:{stem}:{base_model}",
                "model": base_model,
                "is_default": True,
            },
        )
    return out


def load_llm_config(model_or_provider: str | None = None) -> dict[str, Any]:
    all_cfgs = load_all_llm_configs()
    if not all_cfgs:
        return {}

    expanded_all: list[dict[str, Any]] = []
    for cfg in all_cfgs.values():
        expanded_all.extend(_expand_cfg_models(cfg))

    if model_or_provider:
        raw_target = str(model_or_provider).strip().lower()
        target = raw_target[4:] if raw_target.startswith("llm:") else raw_target
        # 1. Exact composite id (`llm:<stem>:<model_id>` or `<stem>:<model_id>`)
        for ec in expanded_all:
            if raw_target == str(ec.get("id") or "").lower() or target == f"{ec.get('raw_id')}:{ec.get('model')}".lower():
                return ec
        # 2. Exact stem key (`gemini`, `claude`, `openai`)
        if target in all_cfgs:
            return all_cfgs[target]
        # 3. Exact model identifier (`gemini-3.8-flash`, `claude-opus-5-5`, `gpt-5.4`, ...)
        for ec in expanded_all:
            if str(ec.get("model") or "").lower() == target:
                return ec
        # 4. Exact provider name
        for cfg in all_cfgs.values():
            if str(cfg.get("provider") or "").lower() == target:
                return cfg
        # 5. Substring fallback
        for ec in expanded_all:
            if target in str(ec.get("model") or "").lower() or target in str(ec.get("raw_id") or "").lower():
                return ec
        for k, cfg in all_cfgs.items():
            if target in k or target in str(cfg.get("model") or "").lower():
                return cfg

    for preferred in ("gemini", "claude", "anthropic", "openai"):
        if preferred in all_cfgs and all_cfgs[preferred].get("has_key"):
            return all_cfgs[preferred]
    for cfg in all_cfgs.values():
        if cfg.get("has_key"):
            return cfg
    return next(iter(all_cfgs.values()), {})


def probe_llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg.get("has_key"):
        return {
            "ok": False,
            "status": "missing_key",
            "error": f"API key not set in {cfg.get('config_file', 'Data/Config/LLM/')}",
        }
    provider = str(cfg.get("provider") or "").lower()
    api_key = str(cfg.get("api_key") or "").strip()
    t0 = time.time()
    try:
        if provider == "gemini":
            model = str(cfg.get("model") or "gemini-3.8-flash").strip()
            base_url = str(cfg.get("api_base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
            url = f"{base_url}/models/{model}:generateContent"
            is_thinking = any(t in model.lower() for t in ("gemini-4", "pro"))
            probe_body: dict[str, Any] = {"contents": [{"parts": [{"text": "ping"}]}]}
            if not is_thinking:
                probe_body["generationConfig"] = {"maxOutputTokens": 8}
            req = urllib.request.Request(
                url,
                data=json.dumps(probe_body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=18 if is_thinking else 10):
                return {"ok": True, "status": "working", "latency_ms": int((time.time() - t0) * 1000)}

        if provider in ("claude", "anthropic"):
            model = str(cfg.get("model") or "claude-sonnet-5-5").strip()
            url = str(cfg.get("api_base_url") or "https://api.anthropic.com/v1/messages").strip()
            req = urllib.request.Request(
                url,
                data=json.dumps({
                    "model": model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": str(cfg.get("anthropic_version") or "2023-06-01"),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                return {"ok": True, "status": "working", "latency_ms": int((time.time() - t0) * 1000)}

        if provider == "openai":
            model = str(cfg.get("model") or "gpt-4.1").strip()
            url = str(cfg.get("api_base_url") or "https://api.openai.com/v1/chat/completions").strip()
            tok_key = (
                "max_completion_tokens"
                if (cfg.get("use_max_completion_tokens") or model.lower().startswith(("gpt-5", "o1", "o3", "o4")))
                else "max_tokens"
            )
            req = urllib.request.Request(
                url,
                data=json.dumps({
                    "model": model,
                    tok_key: 1,
                    "messages": [{"role": "user", "content": "ping"}],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                return {"ok": True, "status": "working", "latency_ms": int((time.time() - t0) * 1000)}

        return {"ok": False, "status": "unknown_provider", "error": f"Unknown provider: {provider}"}
    except urllib.error.HTTPError as exc:
        lat = int((time.time() - t0) * 1000)
        err_msg = f"HTTP {exc.code}: {exc.reason}"
        try:
            j = json.loads(exc.read().decode("utf-8", errors="replace"))
            if "error" in j:
                err_msg = j["error"].get("message") or err_msg if isinstance(j["error"], dict) else str(j["error"])
        except Exception:
            pass
        status = "quota" if (exc.code == 429 or "quota" in err_msg.lower() or "rate" in err_msg.lower()) else "error"
        return {"ok": False, "status": status, "error": err_msg, "latency_ms": lat}
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc), "latency_ms": int((time.time() - t0) * 1000)}


def _is_loopback_url(url: str) -> bool:
    """Returns True if `url` targets localhost / loopback (safe for `local_only` mode)."""
    try:
        host = (urllib.parse.urlparse(str(url or "")).hostname or "").strip().lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _default_provider_url(provider: str) -> str:
    p = (provider or "").lower()
    if p == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta"
    if p in ("claude", "anthropic"):
        return "https://api.anthropic.com/v1/messages"
    return "https://api.openai.com/v1/chat/completions"


def _resolve_inapp_privacy_shield(
    is_cli_target: bool,
    llm_cfg: dict[str, Any],
    portfolio_id: str | None = None,
) -> tuple[dict[str, Any] | None, PrivacyTranslator | None, str]:
    """
    Evaluates the target (or active) portfolio's Cloud Privacy Shield (in-app chat / Advisor / badge gen).
    Fail-closed (`SA-02` / `SA5-04`): if privacy config cannot be loaded, blocks outbound cloud requests,
    and when Demo Mode is active, blocks CLI backends and upgrades API providers to `identity_and_values`.
    Returns `(error_response_or_None, translator_or_None, shield_mode)`.
    """
    # SECURITY DO: Fail-closed whenever privacy config cannot be read or mode is `local_only` against a non-loopback URL.
    # SECURITY DON'T: Never silently downgrade Shield mode to `full_trusted` on exception or leave `mode == "off"` unscaled in Demo Mode.
    try:
        privacy_cfg = load_privacy_config(portfolio_id, auto_sync=True)
    except Exception as exc:
        logging.getLogger("llm_tools").error("Failed to load privacy config; blocking outbound LLM request: %s", exc)
        return {
            "ok": False,
            "engine": "privacy_shield",
            "privacy_shield": "error_blocked",
            "error": f"Blocked by Privacy Shield: failed to load privacy configuration ({type(exc).__name__}).",
            "diagnostics": {"gate": "config_load", "is_cli_target": is_cli_target, "shield_mode": "error_blocked"},
        }, None, "error_blocked"
    shield = privacy_cfg.get("ai_shield") or {}
    mode = str(shield.get("mode") or "identity_aliased")
    block_cli = bool(shield.get("block_inapp_agy_cli", True))
    demo_on = bool((privacy_cfg.get("demo_mode") or {}).get("enabled"))

    if is_cli_target:
        if demo_on:
            return {
                "ok": False,
                "engine": "agy",
                "privacy_shield": "identity_and_values",
                "demo_blocked": True,
                "error": (
                    "Blocked by Demo Mode Privacy Shield: the in-app CLI backend can read raw workspace files "
                    "directly from disk. Select a Direct API model or exit Demo Mode."
                ),
                "diagnostics": {"gate": "cli_shield", "is_cli_target": True, "shield_mode": "identity_and_values", "demo_mode": True},
            }, None, "identity_and_values"
        if mode in ("identity_aliased", "identity_and_values") and block_cli:
            return {
                "ok": False,
                "engine": "agy",
                "privacy_shield": mode,
                "error": (
                    f"Blocked by In-App AI Privacy Shield ({mode}): the in-app CLI backend "
                    "can read raw workspace files directly from disk. Select a Direct API model (Gemini/Claude/OpenAI) "
                    "or adjust Portfolio Settings -> Privacy."
                ),
                "diagnostics": {"gate": "cli_shield", "is_cli_target": True, "shield_mode": mode, "demo_mode": False},
            }, None, mode
        if mode == "local_only":
            return {
                "ok": False,
                "engine": "agy",
                "privacy_shield": mode,
                "error": "Blocked by Local-Only Privacy Mode: select a localhost LLM in Data/Config/LLM/ or change Portfolio Settings -> Privacy.",
                "diagnostics": {"gate": "local_only", "is_cli_target": True, "shield_mode": mode, "demo_mode": False},
            }, None, mode
        return None, None, mode

    provider = str(llm_cfg.get("provider") or "").strip().lower()
    base_url = str(llm_cfg.get("api_base_url") or _default_provider_url(provider))
    base_warn = _check_base_url_trust(provider, base_url)
    if base_warn:
        logging.getLogger("llm_tools").warning("LLM config %s uses non-standard endpoint: %s", llm_cfg.get("id"), base_warn)
    if mode == "local_only" and not _is_loopback_url(base_url):
        return {
            "ok": False,
            "engine": provider or "api",
            "privacy_shield": mode,
            "error": (
                f"Blocked by Local-Only Privacy Mode: '{base_url}' is an external cloud endpoint. "
                "Choose a localhost OpenAI-compatible model or switch Settings -> Privacy to Obfuscated Names."
            ),
            "diagnostics": {"gate": "local_only", "is_cli_target": False, "resolved_provider": provider or "api", "shield_mode": mode, "demo_mode": demo_on},
        }, None, mode

    if demo_on or mode in ("identity_aliased", "identity_and_values"):
        # SECURITY INVARIANT (SA3-03 / SA5-04 / DP-02): When Demo Mode is active, ALWAYS upgrade to
        # `identity_and_values` scaled by `demo_mode.scale_factor`—EVEN IF `ai_shield.mode == "off"`—
        # so the LLM never receives 1.0x unscaled household balances or unaliased PII.
        eff_pcfg = copy.deepcopy(privacy_cfg)
        eff_mode = mode
        if demo_on:
            demo_k = float((eff_pcfg.get("demo_mode") or {}).get("scale_factor") or 0.25)
            eff_pcfg.setdefault("ai_shield", {})["mode"] = "identity_and_values"
            eff_pcfg["ai_shield"]["scale_factor"] = demo_k
            eff_pcfg["ai_shield"]["value_scale_factor"] = demo_k
            eff_mode = "identity_and_values"
        return None, build_translator(eff_pcfg), eff_mode
    return None, None, mode


_ALLOWED_SWITCH_SCREENS = {
    "holdings",
    "main",
    "portfolio",
    "vesting",
    "gsu",
    "grants",
    "stock_grants",
    "simulator",
    "sim",
    "retirement",
    "social-security",
    "ssn",
    "income",
    "scenarios",
    "saves",
    "models",
    "primary",
    "equity",
    "google",
    "settings",
    "cfg",
}


def _exec_shielded_tool(
    t_name: str,
    t_args: dict[str, Any],
    translator: PrivacyTranslator | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """De-aliases inbound tool arguments, blocks Demo Mode & unconfirmed destructive writes, and re-aliases tool outputs."""
    raw_args = translator.from_alias(t_args) if translator is not None else dict(t_args or {})
    if t_name == "holdings__control_live_ui" and isinstance(raw_args, dict):
        sw = str(raw_args.get("switch_screen") or "").strip().lower()
        if sw and sw not in _ALLOWED_SWITCH_SCREENS:
            raw_args["switch_screen"] = "primary"
        ra = str(raw_args.get("refresh_action") or "").strip().lower()
        if ra and ra not in ("primary_quote", "goog_quote", "reload_page", "rebuild_site") and "quote" in ra:
            raw_args["refresh_action"] = "primary_quote"

    # DESIGN INTENT (DP-02 / SA2-09 / SA2-12 — Read-Only Demo-Projected AI Chat & UI Isolation):
    # - AI Chat stays interactive during Demo Mode for Q&A and read-only `run_simulation` / `manage_saves(list|read)`.
    # - All mutating tool actions (`manage_saves` create/save/delete/rename/notes/upvote, `rebuild_site`) MUST
    #   return `demo_blocked: True` so `k`-scaled Demo Persona numbers in `browser_state` can never overwrite or
    #   contaminate real `Scenarios/*.json` on disk.
    # - Provider loops must de-alias `t_args` before emitting `on_step` progress banners (`SA2-09`) and project
    #   both chat replies and banners through `DemoTranslator` while Demo Mode is active (`DP-02`).
    # - Never add hardcoded household scenario/rollover/gift fast-paths in `run_advisor_chat` (`SA2-23`).
    demo_cfg = _active_demo_privacy_cfg()
    if demo_cfg is not None and isinstance(raw_args, dict):
        if t_name == "holdings__manage_saves":
            act_l = str(raw_args.get("action") or "list").strip().lower()
            if act_l not in ("list", "read", "get"):
                blocked_demo = {
                    "ok": False,
                    "demo_blocked": True,
                    "error": "Demo Mode is read-only; saving or modifying scenarios is disabled.",
                }
                wire_demo = translator.to_alias(blocked_demo) if translator is not None else blocked_demo
                return raw_args, wire_demo
        elif t_name == "holdings__control_live_ui":
            if (
                raw_args.get("settings_patch")
                or raw_args.get("tune_settings")
                or raw_args.get("load_save_id")
                or raw_args.get("load_save")
                or raw_args.get("load_save_name")
                or raw_args.get("reoptimize_order")
                or raw_args.get("use_ref_rates")
                or raw_args.get("use_reference_rates")
                or str(raw_args.get("refresh_action") or "").strip().lower() == "rebuild_site"
            ):
                blocked_demo = {
                    "ok": False,
                    "demo_blocked": True,
                    "error": "Demo Mode is read-only; modifying live simulator settings or scenarios is disabled.",
                }
                wire_demo = translator.to_alias(blocked_demo) if translator is not None else blocked_demo
                return raw_args, wire_demo

    # Server-side enforcement of SA-14: block unconfirmed destructive save mutations / rebuild_site from LLM calls
    if isinstance(raw_args, dict) and not bool(raw_args.get("confirmed_by_user")):
        if t_name == "holdings__manage_saves":
            act_l = str(raw_args.get("action") or "list").strip().lower()
            if act_l in ("delete", "rename", "notes") or bool(raw_args.get("overwrite_existing") or raw_args.get("allow_overwrite")):
                blocked_res = {
                    "ok": False,
                    "confirmation_required": True,
                    "error": (
                        f"Blocked unconfirmed mutating operation ({act_l!r} / overwrite) from LLM call. "
                        "Ask the user to confirm or perform overwrites, renames, note edits, and deletions manually in the Scenarios UI."
                    ),
                }
                wire_blocked = translator.to_alias(blocked_res) if translator is not None else blocked_res
                return raw_args, wire_blocked
        elif t_name == "holdings__control_live_ui":
            if str(raw_args.get("refresh_action") or "").strip().lower() == "rebuild_site":
                blocked_res = {
                    "ok": False,
                    "confirmation_required": True,
                    "error": "Blocked unconfirmed `rebuild_site` from LLM call; ask the user to trigger Refresh Site manually.",
                }
                wire_blocked = translator.to_alias(blocked_res) if translator is not None else blocked_res
                return raw_args, wire_blocked

    raw_res = execute_named_tool(t_name, raw_args)
    scrubbed_res = _scrub_primary_symbol_keys(raw_res) if translator is not None else raw_res
    wire_res = translator.to_alias(scrubbed_res) if translator is not None else scrubbed_res
    return raw_args, wire_res


def _shielded_tool_declarations(translator: PrivacyTranslator | None) -> list[dict[str, Any]]:
    """Returns TOOL_DECLARATIONS with descriptions scrubbed via PrivacyTranslator when Shield is active."""
    decls = copy.deepcopy(TOOL_DECLARATIONS)
    if translator is None:
        return decls

    def _scrub(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k == "description" and isinstance(v, str):
                    out[k] = translator.to_alias_text(v)
                else:
                    out[k] = _scrub(v)
            return out
        if isinstance(node, list):
            return [_scrub(x) for x in node]
        return node

    return _scrub(decls)


def list_available_models(probe: bool = False) -> list[dict[str, Any]]:
    """
    Enumerates all configured Direct API LLM options (expanding each provider config's `models` catalog).
    Never emits Antigravity CLI (`agy`) entries in the app UI.
    """
    all_cfgs = load_all_llm_configs()
    models_out: list[dict[str, Any]] = []
    try:
        p_cfg = load_privacy_config(auto_sync=False)
        shield = p_cfg.get("ai_shield") or {}
        shield_mode = str(shield.get("mode") or "identity_aliased")
    except Exception:
        shield_mode = "error_blocked"

    probe_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, cfg in all_cfgs.items():
        base_url = str(cfg.get("api_base_url") or _default_provider_url(str(cfg.get("provider") or "")))
        is_local = _is_loopback_url(base_url)
        if shield_mode == "error_blocked":
            s_blocked = True
            s_reason = "Blocked by Privacy Shield: failed to load privacy configuration"
        else:
            s_blocked = shield_mode == "local_only" and not is_local
            s_reason = "Blocked by Local-Only Privacy Mode (external endpoint)" if s_blocked else None
        top_url_warn = _check_base_url_trust(str(cfg.get("provider") or ""), base_url)
        if top_url_warn:
            logging.getLogger("llm_tools").warning("LLM config %s endpoint warning: %s", key, top_url_warn)

        probe_res: dict[str, Any] | None = None
        fallback_probe_res: dict[str, Any] | None = None
        fallback_model_used: str | None = None
        if probe and cfg.get("has_key"):
            ckey = (str(cfg.get("provider") or "").lower(), base_url, str(cfg.get("api_key") or ""))
            if ckey not in probe_cache:
                probe_cache[ckey] = probe_llm_config(cfg)
            probe_res = probe_cache[ckey]

            # If the primary model failed due to a model-specific quota limit (e.g. Free Tier cap on gemini-2.5-flash),
            # check healthy candidate models so we know if the API key is active for other model families.
            if probe_res and not probe_res.get("ok"):
                err_text = str(probe_res.get("error") or "").lower()
                is_quota = probe_res.get("status") == "quota" or "quota" in err_text or "rate" in err_text or "429" in err_text
                if is_quota:
                    provider = str(cfg.get("provider") or "").lower()
                    if provider == "gemini":
                        candidates = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
                        for cand in candidates:
                            if cand == str(cfg.get("model")):
                                continue
                            fb_key = (f"{provider}:{cand}", base_url, str(cfg.get("api_key") or ""))
                            if fb_key not in probe_cache:
                                fb_cfg = dict(cfg, model=cand)
                                probe_cache[fb_key] = probe_llm_config(fb_cfg)
                            fb_res = probe_cache[fb_key]
                            if fb_res and fb_res.get("ok"):
                                fallback_probe_res = fb_res
                                fallback_model_used = cand
                                break
                            elif fb_res and not fallback_probe_res:
                                fallback_probe_res = fb_res
                                fallback_model_used = cand

        for m_cfg in _expand_cfg_models(cfg):
            m_model = str(m_cfg.get("model") or "").strip().lower()
            m_base_url = str(m_cfg.get("api_base_url") or _default_provider_url(str(m_cfg.get("provider") or cfg.get("provider") or "")))
            m_url_warn = _check_base_url_trust(str(m_cfg.get("provider") or cfg.get("provider") or ""), m_base_url)
            entry = {
                "id": m_cfg.get("id") or f"llm:{cfg.get('id') or key}",
                "raw_id": m_cfg.get("raw_id") or cfg.get("id") or key,
                "provider": m_cfg.get("provider") or cfg.get("provider"),
                "model": m_cfg.get("model"),
                "display_name": m_cfg.get("display_name") or m_cfg.get("model"),
                "tier": m_cfg.get("tier") or "",
                "description": m_cfg.get("description") or "",
                "is_default": bool(m_cfg.get("is_default", False)),
                "config_file": cfg.get("config_file"),
                "has_key": cfg.get("has_key", False),
                "type": "api",
                "is_local": _is_loopback_url(m_base_url),
                "shield_blocked": s_blocked,
                "shield_reason": s_reason,
                "untrusted_base_url": bool(m_url_warn),
                "base_url_warning": m_url_warn,
                "status": "ready" if cfg.get("has_key") else "missing_key",
                "latency_ms": None,
                "error": None if cfg.get("has_key") else f"API key not set in {cfg.get('config_file')}",
            }
            if probe_res is not None:
                primary_m = str(cfg.get("model") or "").strip().lower()
                fb_m = fallback_model_used.strip().lower() if fallback_model_used else ""

                if m_model == primary_m:
                    entry["status"] = probe_res.get("status")
                    entry["latency_ms"] = probe_res.get("latency_ms")
                    entry["error"] = probe_res.get("error")
                elif fb_m and m_model == fb_m:
                    entry["status"] = fallback_probe_res.get("status") if fallback_probe_res else "ready"
                    entry["latency_ms"] = fallback_probe_res.get("latency_ms") if fallback_probe_res else None
                    entry["error"] = fallback_probe_res.get("error") if fallback_probe_res else None
                elif probe_res.get("ok"):
                    entry["status"] = "working"
                    entry["latency_ms"] = probe_res.get("latency_ms")
                    entry["error"] = None
                elif fallback_probe_res and fallback_probe_res.get("ok"):
                    entry["status"] = "ready"
                    entry["latency_ms"] = None
                    entry["error"] = None
                else:
                    err_low = str(probe_res.get("error") or "").lower()
                    is_auth_error = (
                        probe_res.get("status") == "missing_key"
                        or any(k in err_low for k in ("api_key_invalid", "invalid_api_key", "invalid api key", "permission_denied", "unauthorized"))
                    )
                    is_account_billing_exhausted = (
                        any(k in err_low for k in ("no credits", "insufficient_quota", "credit_exhausted", "billing details"))
                        and ("limit: 20" not in err_low and "model:" not in err_low)
                    )
                    if is_auth_error:
                        entry["status"] = "error"
                        entry["latency_ms"] = probe_res.get("latency_ms")
                        entry["error"] = probe_res.get("error")
                    elif is_account_billing_exhausted:
                        entry["status"] = "quota"
                        entry["latency_ms"] = probe_res.get("latency_ms")
                        entry["error"] = probe_res.get("error")
                    else:
                        entry["status"] = "ready"
                        entry["latency_ms"] = None
                        entry["error"] = None
            models_out.append(entry)

    demo_cfg = _active_demo_privacy_cfg()
    if demo_cfg is not None:
        proj = DemoTranslator(demo_cfg)
        for entry in models_out:
            cf = str(entry.get("config_file") or "")
            if cf.startswith("Portfolios/"):
                parts = cf.split("/", 2)
                if len(parts) == 3:
                    entry["config_file"] = f"Portfolios/demo_portfolio/{parts[2]}"
            for str_k in ("display_name", "description", "error"):
                if isinstance(entry.get(str_k), str) and entry[str_k]:
                    val_s = re.sub(r"Portfolios/[^/\s]+/", "Portfolios/demo_portfolio/", str(entry[str_k]))
                    entry[str_k] = proj.map_text(val_s)

    return models_out


def _lowercase_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif isinstance(v, dict):
            out[k] = _lowercase_json_schema(v)
        elif isinstance(v, list):
            out[k] = [_lowercase_json_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def _build_wire_conversation(
    history: list[dict[str, Any]] | None,
    wire_msg: str,
    translator: PrivacyTranslator | None,
    *,
    is_demo_active: bool = False,
    assistant_role: str = "assistant",
    gemini_parts: bool = False,
    max_turns: int = 16,
) -> list[dict[str, Any]]:
    """
    Normalizes, aliases, and strictly alternates prior chat turns plus the current `wire_msg`
    for Gemini (`model` + `parts`), Claude, and OpenAI (`assistant` + `content`).
    """
    cleaned: list[dict[str, str]] = []
    if isinstance(history, list):
        for item in history[-max_turns:]:
            if not isinstance(item, dict):
                continue
            raw_role = str(item.get("role") or "").strip().lower()
            if raw_role in ("user", "human"):
                role = "user"
            elif raw_role in ("assistant", "model"):
                role = "assistant"
            else:
                continue
            raw_text = str(item.get("content") or "").strip()
            if not raw_text:
                continue
            wire_text = translator.to_alias_text(raw_text, scale_prose=not is_demo_active) if translator is not None else raw_text
            if not wire_text.strip():
                continue
            if cleaned and cleaned[-1]["role"] == role:
                cleaned[-1]["content"] = f"{cleaned[-1]['content']}\n\n{wire_text}"
            else:
                cleaned.append({"role": role, "content": wire_text})
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    if cleaned and cleaned[-1]["role"] == "user":
        cleaned[-1]["content"] = f"{cleaned[-1]['content']}\n\n{wire_msg}"
    else:
        cleaned.append({"role": "user", "content": wire_msg})
    if gemini_parts:
        return [
            {"role": (assistant_role if t["role"] == "assistant" else "user"), "parts": [{"text": t["content"]}]}
            for t in cleaned
        ]
    return [
        {"role": (assistant_role if t["role"] == "assistant" else "user"), "content": t["content"]}
        for t in cleaned
    ]


def _build_api_system_prompt(ui_summary: str, *, shield_active: bool = False, translator: PrivacyTranslator | None = None) -> str:
    if translator is not None and getattr(translator, "scale_values", False):
        shield_note = (
            "PRIVACY ALIASES + SCALED VALUES ACTIVE: Tokens such as HOUSEHOLD_N, TRUST_N, INVESTOR_N, BENEFICIARY_N, "
            "BANK_N, ACCT_N, STOCK_N, FUND_N, MGR_N, CARRIER_N, CONTRACT_N, and SCENARIO_N are privacy aliases, and "
            "all dollar figures / share counts are uniformly scaled by Privacy Shield. Preserve tokens verbatim in tool arguments "
            "and prose; do not guess real underlying names.\n"
        )
    elif shield_active or translator is not None:
        shield_note = (
            "PRIVACY ALIASES ACTIVE: Tokens such as HOUSEHOLD_N, TRUST_N, INVESTOR_N, BENEFICIARY_N, "
            "BANK_N, ACCT_N, STOCK_N, FUND_N, MGR_N, CARRIER_N, CONTRACT_N, and SCENARIO_N are authoritative "
            "privacy aliases with unscaled true USD figures. Preserve and echo those tokens verbatim in tool arguments "
            "and prose; do not guess real underlying names.\n"
        )
    else:
        shield_note = ""
    prompt = (
        "You are the Portfolio Tracker & Retirement Simulator AI Financial Advisor.\n"
        "Always use the provided `holdings__*` tools (`holdings__get_overview`, `holdings__query_positions`, "
        "`holdings__run_simulation`, `holdings__compute_non_primary_growth`, `holdings__solve_simulation`, "
        "`holdings__manage_saves`, `holdings__control_live_ui`) to inspect `simulator_assets` (including multi-tier "
        "`growth_evidence` and forecast/half-life keys), `ssn_model` (per-investor PIA, FRA, `claim_setting_key`), "
        "`unvested_grants` (grants and upcoming vesting tranches), and existing Scenarios (`notes_md`, `kpi_summary`), "
        "run tax-aware 40-year simulations tuning Market Correction (`simStressScenario`), Simulation mode "
        "(`simActiveModel`, Monte Carlo & Historical cohort params), `simOptimismMultiplier`, `holdingHalfLives`, "
        "liquidation priority (`simOrderPreset`, `simAssetOrder`, `simBearShield`, `reoptimize_order`), "
        "`simHypotheticalAssets`, and `simScheduledActions`, and explain the results. Only save or upvote a Scenario "
        "when the user chooses that action.\n"
        "Never overwrite or delete existing save files.\n"
        f"{shield_note}"
        f"{ui_summary}"
    )
    if translator is not None:
        # Scrub system boilerplate as well so entity synonyms that happen to match
        # common financial words never trip the pre-egress leak gate (scale_prose=False
        # preserves already-scaled or non-USD prose like "40-year").
        return translator.to_alias_text(prompt, scale_prose=False)
    return prompt


_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
_GEMINI_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite")


def _retry_delay_seconds(attempt: int, base_delay: float, exc: Exception) -> float:
    """Computes bounded backoff delay, honoring numeric Retry-After headers when present."""
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        ra = exc.headers.get("Retry-After")
        if ra:
            try:
                return max(0.5, min(15.0, float(str(ra).strip())))
            except (TypeError, ValueError):
                pass
    return min(12.0, float(base_delay) * (2 ** attempt) + random.uniform(0.0, 0.5))


def _post_json_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int = 75,
    *,
    max_retries: int = 2,
    max_transport_retries: int = 0,
    base_delay: float = 1.0,
) -> dict[str, Any]:
    """POSTs JSON with bounded exponential-backoff retries on transient 429/5xx/network errors."""
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", **headers}
    log = logging.getLogger("llm_tools")
    transport_failures = 0

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_STATUS or attempt >= max_retries:
                raise
            delay = _retry_delay_seconds(attempt, base_delay, exc)
            log.warning(
                "Transient HTTP %s from LLM endpoint (attempt %d/%d); retrying in %.2fs",
                exc.code,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            transport_failures += 1
            if transport_failures > max_transport_retries or attempt >= max_retries:
                raise
            delay = _retry_delay_seconds(attempt, base_delay, exc)
            log.warning(
                "Transient transport error from LLM endpoint (%s, attempt %d/%d); retrying in %.2fs",
                type(exc).__name__,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable retry loop exit")


def _http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int = 75,
    *,
    max_retries: int = 2,
    base_delay: float = 1.0,
) -> dict[str, Any]:
    return _post_json_with_retry(
        url,
        headers,
        payload,
        timeout=timeout,
        max_retries=max_retries,
        base_delay=base_delay,
    )


def _run_chat_gemini_api(
    message: str,
    llm_cfg: dict[str, Any],
    ui_summary: str,
    start_seq: int,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    translator: PrivacyTranslator | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = str(llm_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
    model_name = str(llm_cfg.get("model") or "gemini-3-flash-preview").strip()
    base_url = str(llm_cfg.get("api_base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    max_rounds = int(llm_cfg.get("max_tool_rounds") or 10)
    temperature = float(llm_cfg.get("temperature", 0.2))

    url = f"{base_url}/models/{model_name}:generateContent"
    # SECURITY DO: Send Gemini keys exclusively in the `x-goog-api-key` HTTP header (`SA-16`).
    # SECURITY DON'T: Never append `?key=...` to the URL where proxies, browser tools, or trace logs can capture it.
    headers = {"x-goog-api-key": api_key}
    is_demo_active = _active_demo_privacy_cfg() is not None
    wire_msg = translator.to_alias_text(message, scale_prose=not is_demo_active) if translator is not None else message
    wire_ui_summary = translator.to_alias_text(ui_summary, scale_prose=not is_demo_active) if translator is not None else ui_summary
    sys_prompt = _build_api_system_prompt(wire_ui_summary, shield_active=translator is not None, translator=translator)
    contents = _build_wire_conversation(
        history, wire_msg, translator, is_demo_active=is_demo_active, assistant_role="model", gemini_parts=True
    )
    tool_calls_log: list[dict[str, Any]] = []
    final_text = ""

    model_disp = str(llm_cfg.get("display_name") or model_name)
    active_model = model_name
    fallback_note = ""
    gemini_tools = [{"functionDeclarations": _shielded_tool_declarations(translator)}]
    exhausted_rounds = True
    for round_idx in range(max_rounds):
        url = f"{base_url}/models/{active_model}:generateContent"
        if on_step:
            on_step({
                "tool": "model_inference",
                "summary": f"Querying {model_disp} (Round {round_idx + 1}/{max_rounds})...",
                "progress_pct": min(85, 10 + round_idx * 15),
                "status_label": f"Consulting {model_disp}",
            })
        body = {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": contents,
            "tools": gemini_tools,
            "generationConfig": {"temperature": temperature},
        }
        leak_err = _assert_wire_leak_free(translator, body, "Gemini chat")
        if leak_err:
            return _with_round_diag(leak_err, round_idx, "gemini", active_model)
        resp = None
        candidates_to_try = (
            [active_model, *[m for m in _GEMINI_FALLBACK_MODELS if m != active_model]]
            if round_idx == 0
            else [active_model]
        )
        last_err: Exception | None = None
        for cand_model in candidates_to_try:
            cand_url = f"{base_url}/models/{cand_model}:generateContent"
            if cand_model != active_model:
                logging.getLogger("llm_tools").warning(
                    "Falling back from Gemini %s to %s after transient failure",
                    active_model,
                    cand_model,
                )
                if on_step:
                    on_step({
                        "tool": "model_inference",
                        "summary": f"{active_model} busy/timed out; retrying with {cand_model}...",
                        "progress_pct": 12,
                        "status_label": f"Switching to {cand_model}",
                    })
            try:
                if llm_cfg.get("timeout_sec"):
                    resp = _http_post_json(cand_url, headers, body, timeout=int(llm_cfg["timeout_sec"]))
                else:
                    resp = _http_post_json(cand_url, headers, body)
                if cand_model != model_name:
                    active_model = cand_model
                    model_disp = cand_model
                    fallback_note = f"\n\n**Note:** Answered via `{cand_model}` because `{model_name}` was temporarily unavailable/slow."
                last_err = None
                break
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code == 404 and cand_model != model_name:
                    continue
                if exc.code not in _RETRYABLE_HTTP_STATUS:
                    err_body = exc.read().decode("utf-8", errors="replace")
                    return {"ok": False, "engine": "gemini", "error": _sanitize_error_for_llm(f"Gemini API HTTP {exc.code}: {err_body}")}
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
                last_err = exc
            except Exception as exc:
                return {"ok": False, "engine": "gemini", "error": _sanitize_error_for_llm(f"Gemini API error: {exc}")}
        if resp is None:
            if isinstance(last_err, urllib.error.HTTPError):
                err_body = last_err.read().decode("utf-8", errors="replace")
                return {"ok": False, "engine": "gemini", "error": _sanitize_error_for_llm(f"Gemini API HTTP {last_err.code}: {err_body}")}
            return {"ok": False, "engine": "gemini", "error": _sanitize_error_for_llm(f"Gemini API error: {last_err}")}

        candidates = resp.get("candidates") or []
        if not candidates:
            return {"ok": False, "engine": "gemini", "error": f"No candidates returned by Gemini: {resp}"}
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        contents.append({"role": "model", "parts": _scrub_model_turn_for_history(parts, translator)})

        fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        if text_parts:
            final_text = "\n".join(t for t in text_parts if t).strip()
        if not fn_calls:
            exhausted_rounds = False
            break

        fn_responses = []
        demo_cfg = _active_demo_privacy_cfg()
        for fc in fn_calls:
            t_name = str(fc.get("name") or "")
            t_args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
            pct = min(92, 15 + int((round_idx / max(1, max_rounds - 1)) * 75))
            disp_args = _format_step_args_for_ui(t_args, translator, demo_cfg)
            arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(disp_args.items())[:2])
            call_label = f"{t_name}({arg_preview})" if arg_preview else t_name
            if on_step:
                on_step({
                    "tool": t_name,
                    "summary": f"Running {call_label}",
                    "progress_pct": pct,
                    "status_label": "Tool Execution",
                })
            raw_args, wire_res = _exec_shielded_tool(t_name, t_args, translator)
            if on_step:
                on_step({
                    "tool": t_name,
                    "summary": f"Completed {t_name} • Evaluating output...",
                    "progress_pct": min(95, pct + 2),
                    "status_label": "Tool Output Received",
                })
            tool_calls_log.append({"name": t_name, "args": raw_args})
            fn_responses.append({"functionResponse": {"name": t_name, "response": wire_res}})
        contents.append({"role": "user", "parts": fn_responses})

    if exhausted_rounds and tool_calls_log and not final_text:
        if on_step:
            on_step({
                "tool": "synthesis",
                "summary": f"Synthesizing results from {len(tool_calls_log)} tool calls...",
                "progress_pct": 96,
                "status_label": "Finalizing Response",
            })
        synth_body = {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": contents,
            "tools": gemini_tools,
            "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
            "generationConfig": {"temperature": temperature},
        }
        leak_err = _assert_wire_leak_free(translator, synth_body, "Gemini chat synthesis")
        if leak_err:
            return _with_round_diag(leak_err, max_rounds, "gemini", active_model)
        try:
            synth_url = f"{base_url}/models/{active_model}:generateContent"
            if llm_cfg.get("timeout_sec"):
                synth_resp = _http_post_json(synth_url, headers, synth_body, timeout=int(llm_cfg["timeout_sec"]))
            else:
                synth_resp = _http_post_json(synth_url, headers, synth_body)
            s_cands = synth_resp.get("candidates") or []
            s_parts = ((s_cands[0].get("content") or {}).get("parts") or []) if s_cands else []
            s_text = [p.get("text", "") for p in s_parts if "text" in p]
            if s_text:
                final_text = "\n".join(t for t in s_text if t).strip()
        except Exception as exc:
            logging.getLogger("llm_tools").warning("Gemini post-round synthesis turn failed: %s", exc)

    if on_step:
        on_step({
            "tool": "synthesis",
            "summary": "Synthesizing recommendations...",
            "progress_pct": 98,
            "status_label": "Finalizing Response",
        })

    restored_reply = translator.from_alias_text(final_text) if translator is not None else final_text
    if fallback_note and restored_reply:
        restored_reply = f"{restored_reply}{fallback_note}"
    if not restored_reply and tool_calls_log:
        restored_reply = f"Completed {len(tool_calls_log)} tool calls (tool round limit {max_rounds} reached before a written summary was produced)."
    ui_delta = poll_ui_actions(since_seq=start_seq)
    return {
        "ok": True,
        "engine": "gemini",
        "privacy_shield": translator.mode if translator is not None else "off",
        "reply": restored_reply or "Completed.",
        "tool_calls": tool_calls_log,
        "ui_events": ui_delta.get("events", []),
        "latest_seq": ui_delta.get("latest_seq", start_seq),
    }


def _run_chat_claude_api(
    message: str,
    llm_cfg: dict[str, Any],
    ui_summary: str,
    start_seq: int,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    translator: PrivacyTranslator | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = str(llm_cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    model_name = str(llm_cfg.get("model") or "claude-sonnet-4-20250514").strip()
    url = str(llm_cfg.get("api_base_url") or "https://api.anthropic.com/v1/messages").strip()
    anth_ver = str(llm_cfg.get("anthropic_version") or "2023-06-01").strip()
    max_tokens = int(llm_cfg.get("max_tokens") or 4096)
    max_rounds = int(llm_cfg.get("max_tool_rounds") or 10)

    claude_tools = [
        {
            "name": td["name"],
            "description": td["description"],
            "input_schema": _lowercase_json_schema(td["parameters"]),
        }
        for td in _shielded_tool_declarations(translator)
    ]
    headers = {"x-api-key": api_key, "anthropic-version": anth_ver}
    is_demo_active = _active_demo_privacy_cfg() is not None
    wire_msg = translator.to_alias_text(message, scale_prose=not is_demo_active) if translator is not None else message
    wire_ui_summary = translator.to_alias_text(ui_summary, scale_prose=not is_demo_active) if translator is not None else ui_summary
    sys_prompt = _build_api_system_prompt(wire_ui_summary, shield_active=translator is not None, translator=translator)
    messages = _build_wire_conversation(history, wire_msg, translator, is_demo_active=is_demo_active)
    tool_calls_log: list[dict[str, Any]] = []
    final_text = ""

    model_disp = str(llm_cfg.get("display_name") or model_name)
    exhausted_rounds = True
    for round_idx in range(max_rounds):
        if on_step:
            on_step({
                "tool": "model_inference",
                "summary": f"Querying {model_disp} (Round {round_idx + 1}/{max_rounds})...",
                "progress_pct": min(85, 10 + round_idx * 15),
                "status_label": f"Consulting {model_disp}",
            })
        body: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "system": sys_prompt,
            "tools": claude_tools,
            "messages": messages,
        }
        leak_err = _assert_wire_leak_free(translator, body, "Claude chat")
        if leak_err:
            return _with_round_diag(leak_err, round_idx, "claude", model_name)
        try:
            resp = _http_post_json(url, headers, body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "engine": "claude", "error": _sanitize_error_for_llm(f"Claude API HTTP {exc.code}: {err_body}")}
        except Exception as exc:
            return {"ok": False, "engine": "claude", "error": _sanitize_error_for_llm(f"Claude API error: {exc}")}

        blocks = resp.get("content") or []
        messages.append({"role": "assistant", "content": _scrub_model_turn_for_history(blocks, translator)})
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        if text_parts:
            final_text = "\n".join(t for t in text_parts if t).strip()

        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not tool_uses:
            exhausted_rounds = False
            break

        tool_results = []
        demo_cfg = _active_demo_privacy_cfg()
        for tu in tool_uses:
            t_name = str(tu.get("name") or "")
            t_args = tu.get("input") if isinstance(tu.get("input"), dict) else {}
            pct = min(92, 15 + int((round_idx / max(1, max_rounds - 1)) * 75))
            disp_args = _format_step_args_for_ui(t_args, translator, demo_cfg)
            arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(disp_args.items())[:2])
            call_label = f"{t_name}({arg_preview})" if arg_preview else t_name
            if on_step:
                on_step({
                    "tool": t_name,
                    "summary": f"Running {call_label}",
                    "progress_pct": pct,
                    "status_label": "Tool Execution",
                })
            raw_args, wire_res = _exec_shielded_tool(t_name, t_args, translator)
            if on_step:
                on_step({
                    "tool": t_name,
                    "summary": f"Completed {t_name} • Evaluating output...",
                    "progress_pct": min(95, pct + 2),
                    "status_label": "Tool Output Received",
                })
            tool_calls_log.append({"name": t_name, "args": raw_args})
            tool_results.append({"type": "tool_result", "tool_use_id": tu.get("id"), "content": json.dumps(wire_res)})
        messages.append({"role": "user", "content": tool_results})

    if exhausted_rounds and tool_calls_log and not final_text:
        if on_step:
            on_step({
                "tool": "synthesis",
                "summary": f"Synthesizing results from {len(tool_calls_log)} tool calls...",
                "progress_pct": 96,
                "status_label": "Finalizing Response",
            })
        synth_body: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "system": sys_prompt,
            "tools": claude_tools,
            "tool_choice": {"type": "none"},
            "messages": messages,
        }
        leak_err = _assert_wire_leak_free(translator, synth_body, "Claude chat synthesis")
        if leak_err:
            return _with_round_diag(leak_err, max_rounds, "claude", model_name)
        try:
            synth_resp = _http_post_json(url, headers, synth_body)
            s_blocks = synth_resp.get("content") or []
            s_text = [b.get("text", "") for b in s_blocks if b.get("type") == "text"]
            if s_text:
                final_text = "\n".join(t for t in s_text if t).strip()
        except Exception as exc:
            logging.getLogger("llm_tools").warning("Claude post-round synthesis turn failed: %s", exc)

    if on_step:
        on_step({
            "tool": "synthesis",
            "summary": "Synthesizing recommendations...",
            "progress_pct": 98,
            "status_label": "Finalizing Response",
        })

    restored_reply = translator.from_alias_text(final_text) if translator is not None else final_text
    if not restored_reply and tool_calls_log:
        restored_reply = f"Completed {len(tool_calls_log)} tool calls (tool round limit {max_rounds} reached before a written summary was produced)."
    ui_delta = poll_ui_actions(since_seq=start_seq)
    return {
        "ok": True,
        "engine": "claude",
        "privacy_shield": translator.mode if translator is not None else "off",
        "reply": restored_reply or "Completed.",
        "tool_calls": tool_calls_log,
        "ui_events": ui_delta.get("events", []),
        "latest_seq": ui_delta.get("latest_seq", start_seq),
    }


def _run_chat_openai_api(
    message: str,
    llm_cfg: dict[str, Any],
    ui_summary: str,
    start_seq: int,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    translator: PrivacyTranslator | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = str(llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()
    model_name = str(llm_cfg.get("model") or "gpt-4.1").strip()
    url = str(llm_cfg.get("api_base_url") or "https://api.openai.com/v1/chat/completions").strip()
    max_rounds = int(llm_cfg.get("max_tool_rounds") or 10)
    temperature = float(llm_cfg.get("temperature", 0.2))

    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": td["name"],
                "description": td["description"],
                "parameters": _lowercase_json_schema(td["parameters"]),
            },
        }
        for td in _shielded_tool_declarations(translator)
    ]
    headers = {"Authorization": f"Bearer {api_key}"}
    is_demo_active = _active_demo_privacy_cfg() is not None
    wire_msg = translator.to_alias_text(message, scale_prose=not is_demo_active) if translator is not None else message
    wire_ui_summary = translator.to_alias_text(ui_summary, scale_prose=not is_demo_active) if translator is not None else ui_summary
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_api_system_prompt(wire_ui_summary, shield_active=translator is not None, translator=translator)},
        *_build_wire_conversation(history, wire_msg, translator, is_demo_active=is_demo_active),
    ]
    tool_calls_log: list[dict[str, Any]] = []
    final_text = ""

    model_disp = str(llm_cfg.get("display_name") or model_name)
    omit_temp = bool(llm_cfg.get("omit_temperature")) or model_name.lower().startswith(("gpt-5", "o1", "o3", "o4"))
    exhausted_rounds = True
    for round_idx in range(max_rounds):
        if on_step:
            on_step({
                "tool": "model_inference",
                "summary": f"Querying {model_disp} (Round {round_idx + 1}/{max_rounds})...",
                "progress_pct": min(85, 10 + round_idx * 15),
                "status_label": f"Consulting {model_disp}",
            })
        body: dict[str, Any] = {"model": model_name, "tools": openai_tools, "messages": messages}
        if not omit_temp:
            body["temperature"] = temperature
        leak_err = _assert_wire_leak_free(translator, body, "OpenAI chat")
        if leak_err:
            return _with_round_diag(leak_err, round_idx, "openai", model_name)
        try:
            resp = _http_post_json(url, headers, body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "engine": "openai", "error": _sanitize_error_for_llm(f"OpenAI API HTTP {exc.code}: {err_body}")}
        except Exception as exc:
            return {"ok": False, "engine": "openai", "error": _sanitize_error_for_llm(f"OpenAI API error: {exc}")}

        choices = resp.get("choices") or []
        if not choices:
            return {"ok": False, "engine": "openai", "error": f"No choices returned by OpenAI: {resp}"}
        msg = choices[0].get("message") or {}
        messages.append(_scrub_model_turn_for_history(msg, translator))
        if msg.get("content"):
            final_text = str(msg["content"]).strip()

        t_calls = msg.get("tool_calls") or []
        if not t_calls:
            exhausted_rounds = False
            break

        demo_cfg = _active_demo_privacy_cfg()
        for tc in t_calls:
            fn = tc.get("function") or {}
            t_name = str(fn.get("name") or "")
            try:
                t_args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                t_args = {}
            pct = min(92, 15 + int((round_idx / max(1, max_rounds - 1)) * 75))
            disp_args = _format_step_args_for_ui(t_args, translator, demo_cfg)
            arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(disp_args.items())[:2])
            call_label = f"{t_name}({arg_preview})" if arg_preview else t_name
            if on_step:
                on_step({
                    "tool": t_name,
                    "summary": f"Running {call_label}",
                    "progress_pct": pct,
                    "status_label": "Tool Execution",
                })
            raw_args, wire_res = _exec_shielded_tool(t_name, t_args, translator)
            if on_step:
                on_step({
                    "tool": t_name,
                    "summary": f"Completed {t_name} • Evaluating output...",
                    "progress_pct": min(95, pct + 2),
                    "status_label": "Tool Output Received",
                })
            tool_calls_log.append({"name": t_name, "args": raw_args})
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": json.dumps(wire_res)})

    if exhausted_rounds and tool_calls_log and not final_text:
        if on_step:
            on_step({
                "tool": "synthesis",
                "summary": f"Synthesizing results from {len(tool_calls_log)} tool calls...",
                "progress_pct": 96,
                "status_label": "Finalizing Response",
            })
        synth_body: dict[str, Any] = {
            "model": model_name,
            "tools": openai_tools,
            "tool_choice": "none",
            "messages": messages,
        }
        if not omit_temp:
            synth_body["temperature"] = temperature
        leak_err = _assert_wire_leak_free(translator, synth_body, "OpenAI chat synthesis")
        if leak_err:
            return _with_round_diag(leak_err, max_rounds, "openai", model_name)
        try:
            synth_resp = _http_post_json(url, headers, synth_body)
            s_choices = synth_resp.get("choices") or []
            s_msg = (s_choices[0].get("message") or {}) if s_choices else {}
            if s_msg.get("content"):
                final_text = str(s_msg["content"]).strip()
        except Exception as exc:
            logging.getLogger("llm_tools").warning("OpenAI post-round synthesis turn failed: %s", exc)

    if on_step:
        on_step({
            "tool": "synthesis",
            "summary": "Synthesizing recommendations...",
            "progress_pct": 98,
            "status_label": "Finalizing Response",
        })

    restored_reply = translator.from_alias_text(final_text) if translator is not None else final_text
    if not restored_reply and tool_calls_log:
        restored_reply = f"Completed {len(tool_calls_log)} tool calls (tool round limit {max_rounds} reached before a written summary was produced)."
    ui_delta = poll_ui_actions(since_seq=start_seq)
    return {
        "ok": True,
        "engine": "openai",
        "privacy_shield": translator.mode if translator is not None else "off",
        "reply": restored_reply or "Completed.",
        "tool_calls": tool_calls_log,
        "ui_events": ui_delta.get("events", []),
        "latest_seq": ui_delta.get("latest_seq", start_seq),
    }


VALID_AGY_MODELS: set[str] = {
    "default",
    "gemini-4-carbon-medium",
    "gemini-4-argon-medium",
    "gemini-4-hydrogen-medium",
    "gemini-3.8-flash-medium",
    "gemini-3.5-flash-medium",
    "gemini-2.5-flash-medium",
    "claude-sonnet-5-5-medium",
}

_VALID_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_VALID_MODEL_ARG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _is_valid_cli_session_id(session_id: str | None) -> bool:
    s = str(session_id or "").strip()
    return bool(s and not s.startswith("-") and _VALID_SESSION_ID_RE.fullmatch(s))


def _is_valid_cli_model_arg(model_name: str | None) -> bool:
    s = str(model_name or "").strip()
    return bool(s and not s.startswith("-") and _VALID_MODEL_ARG_RE.fullmatch(s))


def _find_agy_brain_transcript(session_id: str | None) -> Path | None:
    """Safely resolves a local `agy` brain transcript file after strict session_id validation (SA2-17)."""
    if not _is_valid_cli_session_id(session_id):
        return None
    s = str(session_id).strip()
    brain_root = (Path.home() / ".gemini" / "antigravity-cli" / "brain").resolve()
    cand = (brain_root / s / ".system_generated" / "logs" / "transcript.jsonl").resolve()
    if not cand.is_relative_to(brain_root):
        return None
    return cand if cand.is_file() else None


@with_active_portfolio
def run_chat_via_agy_cli(
    message: str,
    conversation_id: str | None = None,
    model: str | None = None,
    ui_state: dict[str, Any] | None = None,
    *,
    allow_fast_path: bool = True,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        demo_cfg = _active_demo_privacy_cfg()
    except Exception:
        return {"ok": False, "privacy_shield": "error_blocked", "error": "Blocked by Privacy Shield. Restore the portfolio privacy registry."}
    eff_on_step: Callable[[dict[str, Any]], None] | None = on_step
    if on_step is not None and demo_cfg is not None:
        def _demo_wrapped_step(step_info: dict[str, Any]) -> None:
            mapped = dict(step_info)
            if isinstance(mapped.get("summary"), str):
                mapped["summary"] = _project_demo_step_text(mapped["summary"], demo_cfg)
            if isinstance(mapped.get("status_label"), str):
                mapped["status_label"] = _project_demo_step_text(mapped["status_label"], demo_cfg)
            on_step(mapped)
        eff_on_step = _demo_wrapped_step

    if allow_fast_path:
        fast = try_quick_chip_fast_path(message, ui_state)
        if fast:
            return _project_demo_chat_response(fast, demo_cfg, already_value_scaled=True)

    with _UI_LOCK:
        start_seq = _UI_SEQ
        if isinstance(ui_state, dict) and ui_state:
            _BROWSER_STATES[get_active_portfolio_id()] = ui_state

    ui_summary = ""
    if isinstance(ui_state, dict) and ui_state:
        cur_save = ui_state.get("currentSaveName") or "Default"
        cur_screen = ui_state.get("currentScreen") or "models"
        cur_settings = ui_state.get("settings") or {}
        ui_summary = (
            f"\n[Live Browser Context: screen={cur_screen}, activeModel={cur_save!r}, "
            f"simActiveModel={cur_settings.get('simActiveModel', 'single_path')}, "
            f"simAnnualBurn={cur_settings.get('simAnnualBurn')}, "
            f"simInflationPct={cur_settings.get('simInflationPct')}]\n"
        )

    m_clean = str(model or "").strip().lower()
    is_explicit_cli = (m_clean == "default" or m_clean.startswith("agy:"))

    llm_cfg: dict[str, Any] = {}
    if not is_explicit_cli:
        llm_cfg = load_llm_config(model_or_provider=model)

    is_cli_target = is_explicit_cli or not llm_cfg
    shield_err, translator, _shield_mode = _resolve_inapp_privacy_shield(is_cli_target, llm_cfg)
    if shield_err:
        diag = dict(shield_err.get("diagnostics") or {})
        diag.update({
            "requested_model": str(model or ""),
            "is_explicit_cli": is_explicit_cli,
            "has_llm_cfg": bool(llm_cfg),
            "resolved_provider": "agy_cli" if is_cli_target else str(llm_cfg.get("provider") or ""),
            "resolved_model": "" if is_cli_target else str(llm_cfg.get("model") or ""),
        })
        shield_err["diagnostics"] = diag
        return shield_err

    if not llm_cfg:
        return {
            "ok": False,
            "error": f"No Direct API LLM configuration found for model {model!r}. Configure a provider JSON in Data/Config/LLM/.",
            "diagnostics": {"gate": "missing_config", "requested_model": str(model or ""), "is_cli_target": is_cli_target},
        }

    already_scaled = bool(translator is not None and getattr(translator, "scale_values", False))
    if llm_cfg and not is_explicit_cli:
        provider = str(llm_cfg.get("provider") or "").strip().lower()
        api_key = str(llm_cfg.get("api_key") or "").strip()
        if not api_key or api_key.startswith("PASTE_YOUR_"):
            err_msg = f"API key is not configured for {llm_cfg.get('display_name', provider)}. Please edit {llm_cfg.get('config_file', 'Data/Config/LLM/')}."
            if demo_cfg is not None:
                err_msg = re.sub(r"Portfolios/[^/\s]+/", "Portfolios/demo_portfolio/", err_msg)
            return {"ok": False, "error": err_msg}
        if provider == "gemini":
            res = _run_chat_gemini_api(message, llm_cfg, ui_summary, start_seq, on_step=eff_on_step, translator=translator, history=history)
            return _project_demo_chat_response(res, demo_cfg, already_value_scaled=already_scaled)
        if provider in ("claude", "anthropic"):
            res = _run_chat_claude_api(message, llm_cfg, ui_summary, start_seq, on_step=eff_on_step, translator=translator, history=history)
            return _project_demo_chat_response(res, demo_cfg, already_value_scaled=already_scaled)
        if provider == "openai":
            res = _run_chat_openai_api(message, llm_cfg, ui_summary, start_seq, on_step=eff_on_step, translator=translator, history=history)
            return _project_demo_chat_response(res, demo_cfg, already_value_scaled=already_scaled)

    if _advisor_draft.get() is not None:
        return {"ok": False, "error": "Choose a direct tool-enabled provider for an Advisor review."}

    if conversation_id and not _is_valid_cli_session_id(conversation_id):
        return {"ok": False, "error": "Invalid conversation_id format."}
    if model and str(model).strip().startswith("-"):
        return {"ok": False, "error": "Invalid model identifier."}

    agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    if not Path(agy_bin).exists():
        agy_bin = "agy"

    full_prompt = (
        f"{message}\n"
        f"{ui_summary}"
        "(Use the `holdings-tools` MCP tools or `python3 Scripts/holdings.py llm-*` CLI commands to inspect existing Scenarios, "
        "run tax-aware simulations honoring `simActiveModel`, save genuinely distinct candidate Scenarios with `set_active=False`, "
        "or concur with an existing Scenario via `action='upvote'`. Never overwrite or delete protected saves unless explicitly asked.)"
    )

    log_fd, log_file_path = tempfile.mkstemp(prefix="advisor_cli_", suffix=".log")
    os.close(log_fd)

    cmd = [
        agy_bin,
        "--print",
        "--input-format",
        "text",
        "--output-format",
        "json",
        "--disable-slash-commands",
        "--log-file",
        log_file_path,
    ]
    if conversation_id and _is_valid_cli_session_id(conversation_id):
        cmd.extend(["--conversation", str(conversation_id).strip()])
    clean_model_arg = str(model or "").strip()
    if clean_model_arg and _is_valid_cli_model_arg(clean_model_arg) and (
        clean_model_arg.lower() in VALID_AGY_MODELS or clean_model_arg.endswith("-medium")
    ):
        cmd.extend(["--model", clean_model_arg])

    # SECURITY DO: Pass untrusted prompts via stdin (`--input-format text`) with `--disable-slash-commands` and never `shell=True`.
    # SECURITY DON'T: Never re-add skip-permissions flags or pass user/model text on CLI argv (`SA-06`).
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(full_prompt)
            proc.stdin.close()
            proc.stdin = None
    except Exception as exc:
        try:
            os.remove(log_file_path)
        except Exception:
            pass
        return {"ok": False, "error": _sanitize_error_for_llm(f"Failed to invoke `agy` CLI: {exc}")}

    conv_id = conversation_id
    transcript_path: Path | None = None
    transcript_line_idx = 0
    cur_pct = 8
    cur_phase = "Phase 1/5: Auditing Positions & Tax Basis"
    start_t = time.time()
    last_pulse_t = start_t
    last_display_step = ""
    timeout_sec = 420.0
    tool_calls: list[dict[str, Any]] = []

    if eff_on_step:
        eff_on_step({
            "tool": "agy_init",
            "summary": f"Starting AGY agent ({model or 'default'})...",
            "progress_pct": 5,
            "status_label": f"Model: {model or 'default'}",
            "conversation_id": conv_id,
        })

    def _process_new_transcript_lines() -> None:
        nonlocal cur_pct, cur_phase, transcript_line_idx, last_display_step
        if not transcript_path or not transcript_path.is_file():
            return
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as tf:
                lines = tf.readlines()
            while transcript_line_idx < len(lines):
                raw_line = lines[transcript_line_idx].strip()
                transcript_line_idx += 1
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue

                tc = obj.get("tool_calls") or []
                if tc:
                    for t_call in tc:
                        t_name = str(t_call.get("name") or "")
                        t_args = t_call.get("args") or {}
                        if isinstance(t_args, str):
                            try:
                                t_args = json.loads(t_args)
                            except Exception:
                                t_args = {}

                        disp_t_args = _format_step_args_for_ui(t_args if isinstance(t_args, dict) else {}, translator, demo_cfg)
                        summary = str(disp_t_args.get("toolSummary") or "")
                        if summary.startswith('"') and summary.endswith('"'):
                            summary = summary[1:-1]
                        cmd_str = str(disp_t_args.get("CommandLine") or "")
                        mcp_tool = str(disp_t_args.get("ToolName") or "")
                        mcp_args = disp_t_args.get("Arguments") if isinstance(disp_t_args.get("Arguments"), dict) else {}

                        if mcp_tool:
                            disp_mcp_args = _format_step_args_for_ui(mcp_args, translator, demo_cfg)
                            arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(disp_mcp_args.items())[:2])
                            display_step = f"{mcp_tool}({arg_preview})" if arg_preview else mcp_tool
                        elif summary:
                            display_step = summary
                        elif cmd_str:
                            display_step = cmd_str.strip().split("\n")[0][:60]
                        else:
                            display_step = t_name or "Executing tool"

                        last_display_step = display_step
                        text_sig = f"{summary} {cmd_str} {mcp_tool} {t_name}".lower()

                        if any(k in text_sig for k in ["upvote", "concur", "signature"]):
                            cur_phase = "Phase 5/5: Concurring & Finalizing Recommendations"
                            cur_pct = max(cur_pct, 85)
                        elif any(k in text_sig for k in ["run sim", "llm-sim", "simulation", "lenses", "monte-carlo", "stress", "historical"]):
                            cur_phase = "Phase 3/5: Running Simulation Experiments"
                            cur_pct = max(cur_pct, 50)
                        elif any(k in text_sig for k in ["drag", "hurdle", "after-tax", "kpi_summary", "solvency"]):
                            cur_phase = "Phase 4/5: Evaluating Risk Drag & Solvency"
                            cur_pct = max(cur_pct, 75)
                        elif any(k in text_sig for k in ["saves", "models", "notes_md", "existing saves"]):
                            if cur_pct < 50:
                                cur_phase = "Phase 2/5: Reviewing Model Library"
                                cur_pct = max(cur_pct, 30)
                        elif any(k in text_sig for k in ["overview", "positions", "project.json", "birthdays", "assets"]):
                            if cur_pct < 30:
                                cur_phase = "Phase 1/5: Auditing Positions & Tax Basis"
                                cur_pct = max(cur_pct, 15)

                        cur_pct = min(95, cur_pct + 1)
                        tool_calls.append({"name": t_name, "args": disp_t_args, "summary": display_step})

                        if eff_on_step:
                            eff_on_step({
                                "tool": mcp_tool or t_name,
                                "summary": f"Running {display_step}",
                                "progress_pct": cur_pct,
                                "status_label": cur_phase,
                                "conversation_id": conv_id,
                            })
                elif obj.get("type") == "GENERIC" and obj.get("content"):
                    if eff_on_step:
                        eff_on_step({
                            "tool": "tool_return",
                            "summary": "Completed tool • Evaluating result...",
                            "progress_pct": min(95, cur_pct + 1),
                            "status_label": cur_phase,
                            "conversation_id": conv_id,
                        })
                elif obj.get("content") and obj.get("type") == "PLANNER_RESPONSE":
                    cur_phase = "Phase 5/5: Scenario Comparison"
                    cur_pct = max(cur_pct, 92)
                    last_display_step = "Comparing scenario outcomes..."
                    if eff_on_step:
                        eff_on_step({
                            "tool": "synthesis",
                            "summary": "Comparing scenario outcomes...",
                            "progress_pct": cur_pct,
                            "status_label": cur_phase,
                            "conversation_id": conv_id,
                        })
        except Exception:
            pass

    while proc.poll() is None:
        if not conv_id and Path(log_file_path).is_file():
            try:
                with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                    log_prefix = f.read(65536)
                m = re.search(r"Created conversation ([a-f0-9\-]+)", log_prefix) or re.search(r"found conversation ([a-f0-9\-]+)", log_prefix)
                if m and _is_valid_cli_session_id(m.group(1)):
                    conv_id = m.group(1)
            except Exception:
                pass

        if conv_id and not transcript_path:
            transcript_path = _find_agy_brain_transcript(conv_id)

        _process_new_transcript_lines()

        now_t = time.time()
        if now_t - last_pulse_t >= 2.0:
            last_pulse_t = now_t
            elapsed = int(now_t - start_t)
            if eff_on_step:
                if not conv_id:
                    pulse_summary = f"Starting AGY agent ({model or 'default'})... ({elapsed}s)"
                    pulse_label = "Agent Startup"
                elif not transcript_path or transcript_line_idx <= 1:
                    pulse_summary = f"Analyzing workspace & planning approach... ({elapsed}s)"
                    pulse_label = cur_phase or "Model Planning"
                else:
                    pulse_summary = f"{last_display_step or 'Executing plan'} ({elapsed}s)"
                    pulse_label = cur_phase
                eff_on_step({
                    "tool": "pulse",
                    "summary": pulse_summary,
                    "progress_pct": cur_pct,
                    "status_label": pulse_label,
                    "conversation_id": conv_id,
                })

        if time.time() - start_t > timeout_sec:
            proc.kill()
            try:
                os.remove(log_file_path)
            except Exception:
                pass
            return {"ok": False, "error": f"Advisor CLI process timed out after {int(timeout_sec)}s"}

        time.sleep(0.3)

    _process_new_transcript_lines()

    stdout, stderr = proc.communicate()
    try:
        os.remove(log_file_path)
    except Exception:
        pass

    raw_out = (stdout or "").strip()
    reply_text = raw_out
    if not conv_id:
        conv_id = conversation_id

    if raw_out:
        try:
            parsed = json.loads(raw_out)
            if isinstance(parsed, dict):
                if parsed.get("status") == "ERROR" or parsed.get("error"):
                    return {
                        "ok": False,
                        "error": str(parsed.get("error") or "AGY CLI returned an error"),
                        "engine": "agy",
                    }
                reply_text = str(
                    parsed.get("response")
                    or parsed.get("result")
                    or parsed.get("content")
                    or parsed.get("text")
                    or raw_out
                )
                conv_id = str(parsed.get("conversation_id") or parsed.get("conversationId") or conv_id or "") or None
        except Exception:
            lines = [ln.strip() for ln in raw_out.splitlines() if ln.strip()]
            for ln in reversed(lines):
                if ln.startswith("{") and ln.endswith("}"):
                    try:
                        obj = json.loads(ln)
                        if isinstance(obj, dict) and (obj.get("response") or obj.get("result") or obj.get("content")):
                            reply_text = str(obj.get("response") or obj.get("result") or obj.get("content"))
                            conv_id = str(obj.get("conversation_id") or obj.get("conversationId") or conv_id or "") or None
                            break
                    except Exception:
                        pass

    if proc.returncode != 0 and not reply_text:
        err_txt = (stderr or "").strip() or f"agy exited with code {proc.returncode}"
        return {"ok": False, "error": err_txt}

    ui_delta = poll_ui_actions(since_seq=start_seq)
    agy_res = {
        "ok": True,
        "engine": "agy",
        "conversation_id": conv_id,
        "reply": reply_text or "Completed.",
        "tool_calls": tool_calls,
        "ui_events": ui_delta.get("events", []),
        "latest_seq": ui_delta.get("latest_seq", start_seq),
    }
    return _project_demo_chat_response(agy_res, demo_cfg, already_value_scaled=already_scaled)


def _call_provider_single_prompt(
    provider: str,
    prompt: str,
    cfg: dict[str, Any] | None = None,
    *,
    timeout_sec: int = 60,
) -> tuple[str | None, str | None]:
    """Issues a one-shot text completion against a configured API provider (`gemini`, `claude`, `openai`)."""
    cfg = cfg or {}
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key or api_key.startswith("PASTE_"):
        return None, None
    try:
        if provider == "gemini":
            model_name = str(cfg.get("model") or "gemini-3-flash-preview").strip()
            base_url = str(cfg.get("api_base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
            url = f"{base_url}/models/{model_name}:generateContent"
            headers = {"x-goog-api-key": api_key}
            gemini_payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": float(cfg.get("temperature", 0.7)),
                },
            }
            if "2.5" in model_name:
                gemini_payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
            try:
                resp = _http_post_json(
                    url,
                    headers,
                    gemini_payload,
                    timeout=timeout_sec,
                )
            except Exception as g_exc:
                if "thinkingConfig" in gemini_payload.get("generationConfig", {}):
                    gemini_payload["generationConfig"].pop("thinkingConfig", None)
                    resp = _http_post_json(
                        url,
                        headers,
                        gemini_payload,
                        timeout=timeout_sec,
                    )
                else:
                    raise g_exc
            parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            txt = "".join([p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")])
            label = str(cfg.get("display_name") or f"Gemini ({model_name})")
            return (txt.strip() or None), label
        if provider in ("claude", "anthropic"):
            model_name = str(cfg.get("model") or "claude-sonnet-5-5").strip()
            url = str(cfg.get("api_base_url") or "https://api.anthropic.com/v1/messages").strip()
            anth_ver = str(cfg.get("anthropic_version") or "2023-06-01").strip()
            max_tokens = int(cfg.get("max_tokens") or 4096)
            resp = _http_post_json(
                url,
                {"x-api-key": api_key, "anthropic-version": anth_ver},
                {
                    "model": model_name,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout_sec,
            )
            content_blocks = resp.get("content", [])
            txt = "".join([b.get("text", "") for b in content_blocks if isinstance(b, dict) and b.get("text")])
            label = str(cfg.get("display_name") or f"Claude ({model_name})")
            return (txt.strip() or None), label
        if provider == "openai":
            model_name = str(cfg.get("model") or "gpt-4.1").strip()
            url = str(cfg.get("api_base_url") or "https://api.openai.com/v1/chat/completions").strip()
            resp = _http_post_json(
                url,
                {"Authorization": f"Bearer {api_key}"},
                {
                    "model": model_name,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout_sec,
            )
            txt = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            label = str(cfg.get("display_name") or f"OpenAI ({model_name})")
            return (txt.strip() or None), label
    except Exception as exc:
        log.warning("%s single-prompt generation failed: %s", provider, exc)
    return None, None


def _call_agy_single_prompt(prompt: str, model_id: str) -> tuple[str | None, str | None]:
    """Issues a shielded one-shot text completion via the local `agy` CLI subprocess (`SA-05`)."""
    shield_err, translator, _ = _resolve_inapp_privacy_shield(True, {})
    if shield_err:
        log.warning("AGY single-prompt blocked by Privacy Shield: %s", shield_err.get("error"))
        return None, None
    agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    if not Path(agy_bin).exists():
        if not shutil.which("agy"):
            return None, None
        agy_bin = "agy"
    # SECURITY DO: Keep `--disable-slash-commands`, stdin prompt passing, and active Privacy Shield checks (`SA-05`/`SA-06`).
    # SECURITY DON'T: Never add skip-permissions flags, `shell=True`, or pass prompts on argv.
    clean_mid = str(model_id or "").strip()
    if clean_mid and clean_mid.startswith("-"):
        return None, None
    cmd = [agy_bin, "--print", "--input-format", "text", "--output-format", "json", "--disable-slash-commands"]
    if clean_mid and clean_mid not in ("default", "agy") and _is_valid_cli_model_arg(clean_mid):
        cmd.extend(["--model", clean_mid])
    wire_prompt = translator.to_alias_text(prompt) if translator is not None else prompt
    if _assert_wire_leak_free(translator, wire_prompt, "agy one-shot"):
        return None, None
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            input=wire_prompt,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        raw_out = (proc.stdout or "").strip()
        txt = raw_out
        if raw_out:
            try:
                parsed = json.loads(raw_out)
                if isinstance(parsed, dict):
                    txt = str(parsed.get("response") or parsed.get("result") or parsed.get("content") or raw_out)
            except Exception:
                pass
        if txt.strip():
            restored = translator.from_alias_text(txt.strip()) if translator is not None else txt.strip()
            return restored, f"Antigravity CLI ({model_id})"
    except Exception as exc:
        log.warning("AGY CLI single-prompt generation failed: %s", exc)
    return None, None


def generate_text_with_model(
    prompt: str,
    model_id: str = "gemini",
    *,
    validator: Callable[[str], str | None] | None = None,
    portfolio_id: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Runs a Privacy-Shield-enforced single-turn generation prompt across configured LLM providers (`SA-10` / `SA5-04`).
    Enforces `ai_shield.mode` (`local_only` endpoint check, alias translation, Demo Mode upgrade, and pre-egress leak gate).
    Returns `(text_or_validated_match, model_label)` or `(None, None)`.
    """
    m_clean = str(model_id or "gemini").strip().lower()
    if m_clean in ("parametric", "offline", "mock"):
        return None, None

    all_cfgs = load_all_llm_configs()
    provider_cfgs: dict[str, dict[str, Any]] = {}
    for cfg in all_cfgs.values():
        prov = str(cfg.get("provider") or "").strip().lower()
        if prov in ("anthropic", "claude"):
            prov = "claude"
        if prov and cfg.get("has_key") and prov not in provider_cfgs:
            provider_cfgs[prov] = cfg

    chosen_cfg = load_llm_config(model_or_provider=model_id) if model_id else {}
    chosen_prov = str(chosen_cfg.get("provider") or "").strip().lower()
    if chosen_prov in ("anthropic", "claude"):
        chosen_prov = "claude"

    if chosen_prov == "claude" or "claude" in m_clean or "anthropic" in m_clean:
        order = ["claude", "gemini", "openai"]
    elif chosen_prov == "openai" or "openai" in m_clean or "gpt" in m_clean or m_clean.startswith(("o1", "o3", "o4")):
        order = ["openai", "gemini", "claude"]
    else:
        order = ["gemini", "claude", "openai"]

    candidate_cfgs: list[tuple[str, dict[str, Any]]] = []
    if chosen_cfg and chosen_cfg.get("has_key") and chosen_prov:
        candidate_cfgs.append((chosen_prov, chosen_cfg))
    for prov in order:
        if prov in provider_cfgs:
            cfg = provider_cfgs[prov]
            if chosen_cfg and prov == chosen_prov and cfg.get("model") == chosen_cfg.get("model"):
                continue
            candidate_cfgs.append((prov, cfg))

    for prov, cfg in candidate_cfgs:
        shield_err, translator, _mode = _resolve_inapp_privacy_shield(False, cfg, portfolio_id=portfolio_id)
        if shield_err:
            log.warning("One-shot %s skipped by Privacy Shield: %s", prov, shield_err.get("error"))
            continue
        wire_prompt = translator.to_alias_text(prompt) if translator is not None else prompt
        if _assert_wire_leak_free(translator, wire_prompt, f"{prov} one-shot"):
            continue
        txt, lbl = _call_provider_single_prompt(prov, wire_prompt, cfg)
        if not txt:
            continue
        restored = translator.from_alias_text(txt) if translator is not None else txt
        if validator is None:
            return restored, lbl
        matched = validator(restored)
        if matched:
            return matched, lbl
        log.warning("Provider %s returned text (len=%d) but failed validator: %s...", lbl, len(restored), repr(restored[:120]))
    return None, None


# =============================================================================
# ASYNC BACKGROUND ADVISOR JOB RUNNER (STAGE 6 / MOCKUP 5A)
# =============================================================================


def _compact_latest_advisor_job() -> dict[str, Any] | None:
    cur_pid = get_active_portfolio_id()
    cur_demo = _active_demo_privacy_cfg() is not None
    with _ADVISOR_LOCK:
        latest = _latest_advisor_job_id(cur_pid, cur_demo)
        if not latest or latest not in _ADVISOR_JOBS:
            return None
        j = _ADVISOR_JOBS[latest]
        if j.get("portfolio_id") != cur_pid or bool(j.get("demo_enabled", False)) != cur_demo:
            return None
        steps = j.get("steps") or []
        is_running = j.get("status") == "running"
        default_status = "Phase 1/5: Auditing Positions & Tax Basis" if is_running else ("Complete" if j.get("status") == "completed" else "Idle")
        default_pct = 100 if j.get("status") == "completed" else (8 if is_running else 0)
        return {
            "job_id": j["job_id"],
            "proposal_label": j.get("proposal_label"),
            "applying": bool(j.get("applying", False)),
            "prompt_type": j.get("prompt_type"),
            "title": j.get("title"),
            "model": j.get("model"),
            "status": j.get("status"),
            "started_at": j.get("started_at"),
            "elapsed_sec": round(
                (j.get("finished_ts") or time.time()) - float(j.get("started_ts") or time.time()), 1
            ),
            "step_count": len(steps),
            "progress_pct": j.get("progress_pct", default_pct),
            "status_label": j.get("status_label") or default_status,
            "last_step": steps[-1]["summary"] if steps else j.get("last_step", "Initializing Advisor..."),
        }


def get_advisor_job_status(job_id: str | None = None) -> dict[str, Any]:
    cur_pid = get_active_portfolio_id()
    cur_demo = _active_demo_privacy_cfg() is not None
    with _ADVISOR_LOCK:
        jid = job_id or _latest_advisor_job_id(cur_pid, cur_demo)
        if not jid or jid not in _ADVISOR_JOBS:
            return {"ok": True, "job": None}
        raw_j = _ADVISOR_JOBS[jid]
        if raw_j.get("portfolio_id") != cur_pid or bool(raw_j.get("demo_enabled", False)) != cur_demo:
            return {"ok": True, "job": None}
        j = copy.deepcopy(raw_j)
        j.pop("portfolio_id", None)
        j.pop("_proposal", None)
        j.pop("_demo_scaled", None)
        j["elapsed_sec"] = round(
            (j.get("finished_ts") or time.time()) - float(j.get("started_ts") or time.time()), 1
        )
        return {"ok": True, "job": j}


@with_active_portfolio
def apply_advisor_proposal(job_id: str) -> dict[str, Any]:
    if _active_demo_privacy_cfg() is not None:
        return {"ok": False, "error": "Demo Mode is read-only."}
    with _ADVISOR_LOCK:
        job = _ADVISOR_JOBS.get(job_id)
        if not job or job.get("portfolio_id") != get_active_portfolio_id() or job.get("status") != "completed":
            return {"ok": False, "error": "Choose a completed review for this portfolio."}
        if bool(job.get("demo_enabled", False)):
            return {"ok": False, "demo_blocked": True, "error": "Demo Mode is read-only."}
        if job.get("proposal_applied"):
            return {"ok": True}
        if job.get("applying"):
            return {"ok": True, "applying": True}
        if not job.get("_proposal"):
            return {"ok": False, "error": "No proposal is available to apply."}
        args = job["_proposal"]
        job["applying"] = True
    try:
        result = tool_manage_saves(**args)
        if result.get("ok"):
            with _ADVISOR_LOCK:
                job["proposal_applied"] = True
                job.pop("_proposal", None)
                job.pop("proposal_label", None)
            if IN_SERVER_PROCESS:
                enqueue_ui_action({"type": "sync_saves", "saved_id": result.get("saved_id"), "upvoted_id": result.get("upvoted_id")}, forward_to_http=False)
            return {"ok": True}
        return {"ok": False, "error": result.get("error", "Could not apply proposal.")}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        with _ADVISOR_LOCK:
            job.pop("applying", None)


@with_active_portfolio
def start_advisor_job(
    prompt_type: str | None = None,
    custom_prompt: str | None = None,
    model: str | None = None,
    ui_state: dict[str, Any] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    Launches a non-blocking background Advisor run (`preferred_forecast` or `portfolio_improvements`),
    allowing the user to continue browsing/editing in `index.html` while telemetry streams over `/api/ui/poll`.
    """

    cfg = load_config(get_default_config_path())
    prompts = resolve_advisor_prompts(cfg)
    eff_prompts = prompts.get("effective") if isinstance(prompts.get("effective"), dict) else prompts
    note_tmpl = eff_prompts.get("model_note_template") or load_default_advisor_prompts().get("model_note_template", "")
    saved_map = {str(p.get("id") or "").lower(): p for p in normalize_saved_prompts(cfg)}

    ptype = str(prompt_type or "").strip().lower()
    title_map = {
        "preferred_forecast": "Preferred Simulation Settings",
        "portfolio_improvements": "Recommended Portfolio Improvements",
    }

    if custom_prompt and str(custom_prompt).strip():
        base_prompt = str(custom_prompt).strip()
        saved_entry = saved_map.get(ptype)
        title = (saved_entry or {}).get("title") or title_map.get(ptype, "Custom Advisor Run")
    elif ptype in saved_map and str(saved_map[ptype].get("prompt") or "").strip():
        base_prompt = str(saved_map[ptype]["prompt"]).strip()
        title = str(saved_map[ptype].get("title") or title_map.get(ptype, ptype))
    elif ptype in eff_prompts and str(eff_prompts[ptype]).strip():
        base_prompt = str(eff_prompts[ptype]).strip()
        title = title_map.get(ptype, ptype)
    else:
        return {"ok": False, "error": f"Unknown or empty advisor prompt_type={prompt_type!r}"}

    eff_model = str(model or "default").strip()
    demo_cfg = _active_demo_privacy_cfg()
    demo_enabled = bool(demo_cfg is not None)
    active_pid = get_active_portfolio_id()
    if demo_cfg is not None:
        full_advisor_prompt = (
            f"{base_prompt}\n\n"
            f"---\n"
            f"### Demo Mode Read-Only Execution Protocol\n"
            f"1. Call `holdings__get_overview` and `holdings__query_positions(has_maturity=True)` to inspect holdings and existing Scenarios.\n"
            f"2. Run read-only simulations (`holdings__run_simulation`, `holdings__solve_simulation`) to evaluate strategies.\n"
            f"3. DEMO MODE IS ACTIVE (READ-ONLY): Do NOT call `holdings__manage_saves` with `create`, `save`, `upvote`, `rename`, `notes`, or `delete`. "
            f"Instead, present your complete simulation analysis and recommendations directly in your response formatted with the Scenario Note Template below.\n\n"
            f"### Scenario Note Markdown Template\n"
            f"{note_tmpl}\n"
        )
    else:
        full_advisor_prompt = (
            f"{base_prompt}\n\n"
            "Inspect the existing scenarios and compare simulations. Explain the results before the user chooses an action. "
            "If warranted, prepare one optional proposal using manage_saves with create or upvote. "
            "The application holds this proposal until the user clicks its button. An analysis alone is a complete result. "
            "Earlier custom prompt instructions about mandatory publication are superseded.\n"
            f"Scenario note format for a proposal:\n{note_tmpl}"
        )

    with _ADVISOR_LOCK:
        # Prevent accidental double-click duplicate runs while a job is actively running
        if (latest := _latest_advisor_job_id(active_pid, demo_enabled)) in _ADVISOR_JOBS:
            cur = _ADVISOR_JOBS[latest]
            if cur.get("status") == "running":
                return {
                    "ok": False,
                    "error": f"Advisor job '{cur.get('title')}' is already running in the background.",
                    "job": copy.deepcopy(cur),
                }

        job_id = f"adv_{datetime.now().strftime('%H%M%S')}_{secrets.token_hex(3)}"
        now_ts = time.time()
        job_rec: dict[str, Any] = {
            "portfolio_id": active_pid,
            "demo_enabled": demo_enabled,
            "job_id": job_id,
            "prompt_type": ptype or "custom",
            "title": title,
            "model": eff_model,
            "status": "running",
            "started_at": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
            "started_ts": now_ts,
            "finished_ts": None,
            "progress_pct": 8,
            "status_label": "Phase 1/5: Auditing Positions & Tax Basis",
            "last_step": f"Launching {eff_model} Advisor...",
            "steps": [{"ts": round(now_ts, 2), "tool": "start", "summary": f"Started '{title}' ({eff_model})"}],
            "reply": None,
            "error": None,
            "conversation_id": conversation_id,
        }
        _ADVISOR_JOBS[job_id] = job_rec
        _LATEST_ADVISOR_JOBS[(active_pid, demo_enabled)] = job_id

    enqueue_ui_action(
        {
            "type": "advisor_job_started",
            "job_id": job_id,
            "prompt_type": ptype or "custom",
            "title": title,
            "model": eff_model,
            "progress_pct": 8,
            "status_label": "Phase 1/5: Auditing Positions & Tax Basis",
        }
    )

    def _worker() -> None:
        draft: dict[str, Any] = {"model": eff_model}
        _advisor_draft.set(draft)

        def _on_step(step_info: dict[str, Any]) -> None:
            with _ADVISOR_LOCK:
                job = _ADVISOR_JOBS.get(job_id)
                if not job or job.get("status") != "running":
                    return
                steps = job.setdefault("steps", [])
                steps.append(
                    {
                        "ts": round(time.time(), 2),
                        "tool": step_info.get("tool", "tool_call"),
                        "summary": step_info.get("summary", "Executing tool..."),
                    }
                )
                if step_info.get("conversation_id"):
                    job["conversation_id"] = step_info["conversation_id"]
                if "progress_pct" in step_info:
                    job["progress_pct"] = step_info["progress_pct"]
                if "status_label" in step_info:
                    job["status_label"] = step_info["status_label"]
                job["last_step"] = step_info.get("summary") or job.get("last_step")
                job["step_count"] = len(steps)
                cur_step_cnt = job["step_count"]
                cur_last_step = job["last_step"]
                cur_pct = job.get("progress_pct", 8)
                cur_status = job.get("status_label", "")

            enqueue_ui_action(
                {
                    "type": "advisor_job_step",
                    "job_id": job_id,
                    "step_count": cur_step_cnt,
                    "last_step": cur_last_step,
                    "progress_pct": cur_pct,
                    "status_label": cur_status,
                }
            )

        try:
            res = run_chat_via_agy_cli(
                message=full_advisor_prompt,
                conversation_id=conversation_id,
                model=eff_model,
                ui_state=ui_state,
                allow_fast_path=False,
                on_step=_on_step,
            )
            fin_ts = time.time()
            pre_scaled = bool(demo_enabled and res.get("demo_mode"))
            with _ADVISOR_LOCK:
                job = _ADVISOR_JOBS[job_id]
                job["finished_ts"] = fin_ts
                if pre_scaled:
                    job["_demo_scaled"] = True
                if res.get("ok"):
                    job["status"] = "completed"
                    job["progress_pct"] = 100
                    job["status_label"] = "Complete"
                    job["reply"] = res.get("reply") or "Advisor run completed."
                    job["conversation_id"] = res.get("conversation_id") or conversation_id
                    job["last_step"] = "Analysis ready"
                    if draft.get("args") and demo_cfg is None:
                        job["_proposal"] = draft["args"]
                        job["proposal_label"] = draft["label"]
                else:
                    job["status"] = "error"
                    job["status_label"] = "Execution Failed"
                    job["error"] = res.get("error") or "Advisor run failed."
                    job["last_step"] = f"Error: {job['error'][:80]}"

            done_ev: dict[str, Any] = {
                "type": "advisor_job_done",
                "job_id": job_id,
                "proposal_label": draft.get("label") if demo_cfg is None and res.get("ok") and draft.get("args") else None,
                "ok": bool(res.get("ok")),
                "prompt_type": ptype or "custom",
                "title": title,
                "model": eff_model,
                "progress_pct": 100 if res.get("ok") else job.get("progress_pct", 0),
                "status_label": "Complete" if res.get("ok") else "Execution Failed",
                "reply": res.get("reply"),
                "error": res.get("error"),
                "conversation_id": res.get("conversation_id") or conversation_id,
            }
            if pre_scaled:
                done_ev["_demo_scaled"] = True
            enqueue_ui_action(done_ev)
        except Exception as exc:
            logging.getLogger("llm_tools").exception("Advisor background job %s failed: %s", job_id, exc)
            with _ADVISOR_LOCK:
                job = _ADVISOR_JOBS[job_id]
                job["finished_ts"] = time.time()
                job["status"] = "error"
                job["status_label"] = "Execution Failed"
                job["error"] = str(exc)
                job["last_step"] = f"Error: {str(exc)[:80]}"
            enqueue_ui_action(
                {
                    "type": "advisor_job_done",
                    "job_id": job_id,
                    "ok": False,
                    "prompt_type": ptype or "custom",
                    "title": title,
                    "model": eff_model,
                    "progress_pct": job.get("progress_pct", 0),
                    "status_label": "Execution Failed",
                    "error": str(exc),
                }
            )

    t = threading.Thread(target=copy_context().run, args=(_worker,), name=f"advisor-{job_id}", daemon=True)
    t.start()

    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "title": title,
        "model": eff_model,
        "job": get_advisor_job_status(job_id).get("job"),
    }
