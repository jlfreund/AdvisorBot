#!/usr/bin/env python3.11
"""
mcp_server.py — Model Context Protocol (MCP) Server for the Portfolio Tracker & Retirement Simulator.

Exposes seven tools over stdio transport (privacy-translated per `ai_shield.mode`):
  - holdings__get_overview              : Portfolio KPIs, primary equity quote, Reference (`Ref:`) rates, birthdays, Scenarios
  - holdings__query_positions           : Filter holdings by `has_maturity`, `account_id`, or `symbol`
  - holdings__run_simulation            : Run 40-yr Retirement Simulator with optional hypothetical Day-0 rollovers/trades
  - holdings__compute_non_primary_growth: Compute value-weighted CAGR of diversified (non-primary) holdings
  - holdings__solve_simulation          : Bisection goal-seek solver for any slider to hit a target milestone balance
  - holdings__manage_saves              : List, inspect, create candidate, load, or upvote Scenarios
  - holdings__control_live_ui           : Tune sliders, add withdrawals, switch screens, or refresh browser UI
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

# SECURITY INVARIANT (SA-08 / SA3-09): Enforce restrictive `0o077` process umask immediately on MCP startup
# so any scenario JSON, index, snapshot, or cache file created via external MCP clients defaults to `0o600`.
os.umask(0o077)

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import with_active_portfolio
import llm_tools  # noqa: E402
from privacy import build_translator, load_privacy_config  # noqa: E402

SERVER_INSTRUCTIONS = (
    "Use `holdings-tools` to inspect the portfolio, query positions (including dated contracts/bonds "
    "with maturity dates), run the 40-year monthly Retirement Simulator, goal-seek parameters, "
    "test hypothetical asset rollovers, manage distinct candidate Scenarios in `Portfolios/<active>/Scenarios/*.json`, "
    "and control the live open browser dashboard in realtime.\n"
    "IMPORTANT: Overwriting, deleting, renaming existing save files, or rebuilding index.html requires manual user action."
)

_MCP_DISABLED_ERROR = (
    "AdvisorBot MCP Server is disabled in Privacy Settings. "
    "To connect external agents (Claude Desktop, Cursor, Gemini CLI), open Settings > Privacy & AI Security "
    "and toggle the Model Context Protocol (MCP) service to Enabled."
)


def _check_mcp_access() -> tuple[dict[str, Any] | None, Any]:
    """
    Fail-closed (`SA-02`) MCP gate + privacy translator resolver (`SA-12`).
    Returns `(error_dict_or_None, translator_or_None)`.
    """
    # SECURITY DO: Always validate MCP enablement AND load PrivacyTranslator fail-closed before running any tool.
    # SECURITY DON'T: Never return `(None, None)` when config parsing fails or bypass `is_mcp_enabled` checks.
    try:
        pcfg = load_privacy_config(auto_sync=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{_MCP_DISABLED_ERROR} (Privacy config load failed: {type(exc).__name__})",
            "mcp_enabled": False,
        }, None

    mcp_cfg = pcfg.get("mcp_server") or {}
    if not bool(mcp_cfg.get("enabled", False)):
        return {
            "ok": False,
            "error": _MCP_DISABLED_ERROR,
            "mcp_enabled": False,
        }, None

    shield = pcfg.get("ai_shield") or {}
    mode = str(shield.get("mode") or "identity_aliased")
    if mode == "local_only":
        return {"ok": False, "error": "Local Only blocks external MCP clients."}, None
    # SECURITY INVARIANT (SA5-04): When Demo Mode is active, force `identity_and_values` scaled by
    # `demo_mode.scale_factor` even when `ai_shield.mode` is `"off"` or `"identity_aliased"`.
    if bool((pcfg.get("demo_mode") or {}).get("enabled")):
        eff_pcfg = copy.deepcopy(pcfg)
        demo_k = float((eff_pcfg.get("demo_mode") or {}).get("scale_factor") or 0.25)
        eff_shield = eff_pcfg.setdefault("ai_shield", {})
        eff_shield["mode"] = "identity_and_values"
        eff_shield["scale_factor"] = demo_k
        eff_shield["value_scale_factor"] = demo_k
        return None, build_translator(eff_pcfg)
    translator = build_translator(pcfg) if mode in ("identity_aliased", "identity_and_values") else None
    return None, translator


def _sanitize_mcp_error(exc: Any, translator: Any = None) -> str:
    """Sanitizes local paths & entity names from MCP tool error strings (`SA-19` / `SA2-06`)."""
    raw_msg = str(exc or "")
    fn = llm_tools._sanitize_error_for_llm
    try:
        sig = inspect.signature(fn)
        if len(sig.parameters) >= 2:
            clean = fn(raw_msg, translator)
        else:
            clean = fn(raw_msg)
    except TypeError:
        clean = fn(raw_msg)
    if translator is not None and isinstance(clean, str):
        clean = translator.to_alias_text(clean)
    return clean


def _enforce_outbound_leak_gate(serialized: str, translator: Any, tool_name: str) -> dict[str, Any] | None:
    """Runs `llm_tools._assert_wire_leak_free` on serialized MCP response payloads (`SA2-06`)."""
    if translator is None:
        return None
    fn = llm_tools._assert_wire_leak_free
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("translator", "tr"):
        if len(params) >= 3 and params[2].default is inspect.Parameter.empty:
            return fn(translator, serialized, f"MCP:{tool_name}")
        return fn(translator, serialized)
    if len(params) >= 3 and params[2].default is inspect.Parameter.empty:
        return fn(serialized, translator, f"MCP:{tool_name}")
    return fn(serialized, translator)


@with_active_portfolio
def _invoke_mcp_tool(t_name: str, t_args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Executes an MCP tool cleanly through access gate + Privacy Shield translator + outbound leak gate."""
    # SECURITY DO: Route every `@mcp.tool` entry point exclusively through `_invoke_mcp_tool` so
    # `_check_mcp_access`, `translator.scrub/unscrub`, `_assert_wire_leak_free`, and `confirmed_by_user` apply uniformly.
    # SECURITY DON'T: Never call `execute_named_tool` directly inside `@mcp.tool` handlers or return raw unscrubbed errors.
    denied, translator = _check_mcp_access()
    if denied:
        return denied

    norm_args = dict(t_args or {})
    # Support `non_primary_rate_mode` while keeping `uma_rate_mode` as an undocumented fallback (`SA2-22`)
    if "non_primary_rate_mode" in norm_args or "uma_rate_mode" in norm_args:
        eff_np_mode = norm_args.get("non_primary_rate_mode") or norm_args.get("uma_rate_mode") or "ytd_statement"
        norm_args["non_primary_rate_mode"] = eff_np_mode
        norm_args.setdefault("sleeve_rate_mode", eff_np_mode)

    try:
        _raw_args, wire_res = llm_tools._exec_shielded_tool(t_name, norm_args, translator)
        if translator is not None:
            serialized = json.dumps(wire_res, ensure_ascii=False)
            leak_err = _enforce_outbound_leak_gate(serialized, translator, t_name)
            if isinstance(leak_err, dict):
                return leak_err
        return wire_res
    except Exception as exc:
        return {
            "ok": False,
            "error": _sanitize_mcp_error(exc, translator),
            "error_class": type(exc).__name__,
        }


try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    _HAS_FASTMCP = True
except ImportError:
    _HAS_FASTMCP = False

if _HAS_FASTMCP:
    mcp = FastMCP("holdings-tools", instructions=SERVER_INSTRUCTIONS)

    # Behavior hints so MCP clients can distinguish read-only queries from
    # state-changing / destructive tools and prompt the user accordingly (I4).
    _READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    _READ_ONLY_LIVE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    _MUTATING_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
    _MUTATING_LIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    @mcp.tool(annotations=_READ_ONLY_LIVE)
    def holdings__get_overview() -> dict[str, Any]:
        """
        Returns portfolio KPIs, live primary equity price, Reference (`Ref:`) growth rates,
        family member birthdays, simulator asset IDs, and saved configuration files.
        """
        return _invoke_mcp_tool("holdings__get_overview", {})

    @mcp.tool(annotations=_READ_ONLY)
    def holdings__query_positions(
        has_maturity: bool | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """
        Lists portfolio positions across all accounts/rollups.
        - `has_maturity=True`: Returns all holdings with maturity dates sorted chronologically.
        - `account_id`: Optional substring/token filter.
        - `symbol`: Optional ticker/CUSIP/contract or token filter.
        """
        return _invoke_mcp_tool(
            "holdings__query_positions",
            {"has_maturity": has_maturity, "account_id": account_id, "symbol": symbol},
        )

    @mcp.tool(annotations=_READ_ONLY)
    def holdings__run_simulation(
        base_save: str | None = None,
        use_ref_rates: bool = False,
        overrides: dict[str, Any] | None = None,
        rollovers: list[dict[str, Any]] | None = None,
        reoptimize_order: bool = False,
        compare_with_baseline: bool = False,
        apply_non_primary_rate_mode: str = "none",
        non_primary_rate_mode: str = "ytd_statement",
    ) -> dict[str, Any]:
        """
        Runs the 40-year monthly Retirement Simulator (and 100-year solvency check).
        - `base_save`: Optional save config name or token.
        - `use_ref_rates`: If True, sets growth/burn/inflation/SSN sliders to their Reference (`Ref:`) values.
        - `overrides`: Optional dict of setting overrides (`holdingForecasts` including per-holding `vol_pct`, `simAnnualBurn`, `simInflationPct`, `simMemberRetirementAges` (`{"member_0_retirement_age": 65}`), `simInvestor1RetirementAge`, `simInvestor2RetirementAge`, `simMarriedFilingJointly`, `simMinTaxBracketFloor`, etc.).
        - `rollovers`: Optional Day-0 hypothetical trades, e.g. `[{"from_id": "<source_id>", "to_id": "<dest_id>"}]`.
        - `reoptimize_order`: If True, runs Auto-Optimize on the asset liquidation order.
        - `apply_non_primary_rate_mode`: `'none'`, `'shift'`, or `'solved'`.
        - `non_primary_rate_mode`: `'ytd_statement'`, `'matched_window'`, `'full_history'`, or `'excluded'`.
        """
        return _invoke_mcp_tool(
            "holdings__run_simulation",
            {
                "base_save": base_save,
                "use_ref_rates": use_ref_rates,
                "overrides": overrides,
                "rollovers": rollovers,
                "reoptimize_order": reoptimize_order,
                "compare_with_baseline": compare_with_baseline,
                "apply_non_primary_rate_mode": apply_non_primary_rate_mode,
                "non_primary_rate_mode": non_primary_rate_mode,
            },
        )

    @mcp.tool(annotations=_READ_ONLY)
    def holdings__compute_non_primary_growth(
        rollovers: list[dict[str, Any]] | None = None,
        non_primary_rate_mode: str = "ytd_statement",
        muni_rate_mode: str = "price_cagr",
        include_cash: bool = False,
        asset_rate_overrides: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """
        Python solver that computes the market-value-weighted average annualized growth rate across diversified
        (non-primary) holdings BEFORE and AFTER a hypothetical transaction/rollover.
        """
        return _invoke_mcp_tool(
            "holdings__compute_non_primary_growth",
            {
                "rollovers": rollovers,
                "non_primary_rate_mode": non_primary_rate_mode,
                "muni_rate_mode": muni_rate_mode,
                "include_cash": include_cash,
                "asset_rate_overrides": asset_rate_overrides,
            },
        )

    @mcp.tool(annotations=_READ_ONLY)
    def holdings__solve_simulation(
        solve_for: str = "primary_equity",
        target_metric: str = "investor_1_milestone_real",
        target_value: float = 2_000_000.0,
        base_save: str | None = None,
        use_ref_rates: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Goal-seek solver finding the exact model parameter (`primary_equity`, `broad_equity`, `all_holdings`,
        any `<forecast_key>`, `simAnnualBurn`, `simInflationPct`) needed to hit `target_value` on `target_metric`
        (e.g. `'investor_1_milestone_real'`, `'yr_40_real'`, `'p25'`, `'p50'`, `'p75'`),
        honoring `simActiveModel` (`single_path`, `stress`, `range`, `historical`).
        """
        return _invoke_mcp_tool(
            "holdings__solve_simulation",
            {
                "solve_for": solve_for,
                "target_metric": target_metric,
                "target_value": target_value,
                "base_save": base_save,
                "use_ref_rates": use_ref_rates,
                "overrides": overrides,
            },
        )

    @mcp.tool(annotations=_MUTATING_DESTRUCTIVE)
    def holdings__manage_saves(
        action: str = "list",
        name: str | None = None,
        id: str | None = None,
        base_save: str | None = None,
        use_ref_rates: bool = False,
        overrides: dict[str, Any] | None = None,
        notes_md: str | None = None,
        author: dict[str, Any] | None = None,
        model_name: str | None = None,
        reason: str | None = None,
        reoptimize_order: bool = False,
        set_active: bool = True,
        apply_to_live_ui: bool = True,
    ) -> dict[str, Any]:
        """
        Manages canonical Scenarios in `Scenarios/` ('list', 'get', 'create', 'save', 'load', 'upvote')
        and syncs changes to the open browser UI in realtime. Use `action='upvote'` to append a timestamped
        concurrence signature to `notes_md` without overwriting settings or auto-loading.
        """
        return _invoke_mcp_tool(
            "holdings__manage_saves",
            {
                "action": action,
                "name": name,
                "id": id,
                "base_save": base_save,
                "use_ref_rates": use_ref_rates,
                "overrides": overrides,
                "notes_md": notes_md,
                "author": author,
                "model_name": model_name,
                "reason": reason,
                "reoptimize_order": reoptimize_order,
                "set_active": set_active,
                "apply_to_live_ui": apply_to_live_ui,
            },
        )

    @mcp.tool(annotations=_MUTATING_LIVE)
    def holdings__control_live_ui(
        settings_patch: dict[str, Any] | None = None,
        view_patch: dict[str, Any] | None = None,
        switch_screen: str | None = None,
        refresh_action: str | None = None,
        use_ref_rates: bool = False,
        reoptimize_order: bool = False,
    ) -> dict[str, Any]:
        """
        Tunes sliders/toggles/withdrawals, switches screens ('scenarios', 'models', 'portfolio', 'primary', 'grants', 'retirement', 'ssn', 'income'),
        or triggers a live refresh ('primary_quote', 'reload_page') in the open browser window in realtime.
        """
        return _invoke_mcp_tool(
            "holdings__control_live_ui",
            {
                "settings_patch": settings_patch,
                "view_patch": view_patch,
                "switch_screen": switch_screen,
                "refresh_action": refresh_action,
                "use_ref_rates": use_ref_rates,
                "reoptimize_order": reoptimize_order,
            },
        )


def _run_stdlib_jsonrpc_stdio() -> None:
    """Fallback JSON-RPC 2.0 MCP stdio server when the `mcp` package is not installed."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "holdings-tools", "version": "1.0.0"},
                    "instructions": SERVER_INSTRUCTIONS,
                },
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "tools/list":
            _denied, translator = _check_mcp_access()
            decls = llm_tools._shielded_tool_declarations(translator)
            tools_out = []
            for d in decls:
                props = {}
                for k, v in (d["parameters"].get("properties") or {}).items():
                    props[k] = {
                        "type": v["type"].lower(),
                        "description": v.get("description", ""),
                    }
                tools_out.append(
                    {
                        "name": d["name"],
                        "description": d["description"],
                        "inputSchema": {
                            "type": "object",
                            "properties": props,
                            "required": d["parameters"].get("required", []),
                        },
                    }
                )
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_out}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            t_name = params.get("name") or ""
            t_args = params.get("arguments") or {}
            try:
                res = _invoke_mcp_tool(t_name, t_args)
            except Exception as exc:
                _denied, translator = _check_mcp_access()
                res = {
                    "ok": False,
                    "error": _sanitize_mcp_error(exc, translator),
                    "error_class": type(exc).__name__,
                }
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(res, indent=2)}],
                    "isError": not bool(res.get("ok", True)),
                },
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif msg_id is not None:
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    if _HAS_FASTMCP:
        mcp.run()
    else:
        _run_stdlib_jsonrpc_stdio()
