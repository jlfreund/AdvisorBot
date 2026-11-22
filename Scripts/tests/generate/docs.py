#!/usr/bin/env python3
"""
Per-fixture README generation.

A reviewer should be able to understand what a fixture household is, and what it is for,
without opening a single JSON file. The README is generated from the same spec that
generates the data, so the description cannot drift from the fixture.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from . import builder


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _account_table(spec: dict[str, Any]) -> list[str]:
    rows = [
        "| Account | Institution | Type | Tax status | Statements |",
        "| --- | --- | --- | --- | --- |",
    ]
    for acct in spec.get("accounts") or []:
        periods = acct.get("periods") or {}
        span = f"{periods.get('from')} to {periods.get('to')}" if periods else "none"
        rows.append(
            f"| `{acct['id']}` | {acct['institution']} | {acct['account_type']} "
            f"| {acct['tax_status']} | {span} |"
        )
    if len(rows) == 2:
        rows.append("| _none_ | | | | |")
    return rows


def _position_table(spec: dict[str, Any]) -> list[str]:
    rows = ["| Account | Holding | Kind | Detail |", "| --- | --- | --- | --- |"]
    for acct in spec.get("accounts") or []:
        for pos in acct.get("positions") or []:
            kind = str(pos.get("kind") or "ticker")
            if kind == "ticker":
                lots = pos.get("lots") or []
                qty = sum(float(lot["qty"]) for lot in lots)
                detail = f"{qty:,.0f} sh across {len(lots)} lot(s), symbol `{pos.get('symbol')}`"
            elif kind == "cash":
                detail = f"opens at {_fmt_money(float(pos.get('value') or 0))}, {pos.get('yield_pct', 0)}% APY"
            elif kind == "annuity":
                detail = (
                    f"{_fmt_money(float(pos.get('premium') or 0))} premium, issued {pos.get('issue_date')}, "
                    f"surrender-free {pos.get('maturity_date')}"
                )
            elif kind == "muni":
                detail = f"{_fmt_money(float(pos.get('par') or 0))} par, {pos.get('coupon_pct')}% coupon, {pos.get('state')} issuer"
            else:
                detail = _fmt_money(float(pos.get("value") or 0))
            rows.append(f"| `{acct['id']}` | {pos['name']} | {kind} | {detail} |")
    if len(rows) == 2:
        rows.append("| _none_ | | | |")
    return rows


def _grant_section(spec: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for acct in spec.get("accounts") or []:
        grants = acct.get("grants")
        if not grants:
            continue
        lines.append(f"### Equity awards at `{acct['id']}` ({acct['institution']})")
        lines.append("")
        lines.append(
            f"- **Symbol / company**: `{grants['symbol']}` ({grants['company']}), "
            f"award type **{grants.get('award_type', 'RSU')}**"
        )
        lines.append(f"- **Withholding at release**: {grants.get('withholding_pct', 38)}%")
        for grant in grants.get("grants") or []:
            schedule = builder._vest_schedule(grant)
            lines.append(
                f"- **`{grant['id']}`**: {float(grant['total_shares']):,.0f} shares granted "
                f"{grant['grant_date']}, {grant.get('cadence', 'quarterly')} from {grant['first_vest']} "
                f"({len(schedule)} vests, last {schedule[-1].isoformat()})"
            )
        transfer = grants.get("in_kind_transfer")
        if transfer:
            lines.append(
                f"- **In-kind transfer**: {float(transfer['shares']):,.0f} shares to `{transfer['to_account']}` "
                f"on {transfer.get('exec_date') or transfer['vest_date']} (carryover basis)"
            )
        lines.append("")
    return lines


def _investor_section(spec: dict[str, Any]) -> list[str]:
    as_of = date.fromisoformat(str(spec["as_of"]))
    lines = ["| Person | Role | Born | Age at as_of | Social Security |", "| --- | --- | --- | --- | --- |"]
    for inv in spec.get("investors") or []:
        born = date.fromisoformat(str(inv["birth"]))
        age = as_of.year - born.year - ((as_of.month, as_of.day) < (born.month, born.day))
        ssa = inv.get("ssa") or {}
        earnings = ssa.get("earnings") or {}
        if earnings:
            ssa_text = (
                f"{earnings.get('profile')} {earnings.get('start')}-{earnings.get('end')}, "
                f"claim at {ssa.get('claim_age', 67)}"
                + (", from XML" if ssa.get("emit_xml") else ", from summary")
            )
        else:
            ssa_text = "none"
        role = "investor" + (", RMD owner" if inv.get("rmd_owner") else "")
        lines.append(f"| {inv['name']} | {role} | {inv['birth']} | {age} | {ssa_text} |")
    for ben in spec.get("beneficiaries") or []:
        born = date.fromisoformat(str(ben["birth"]))
        age = as_of.year - born.year - ((as_of.month, as_of.day) < (born.month, born.day))
        lines.append(
            f"| {ben['name']} | beneficiary | {ben['birth']} | {age} | milestone age {ben.get('milestone_age', 25)} |"
        )
    if len(lines) == 2:
        lines.append("| _none_ | | | | |")
    return lines


def _invariant_table(spec: dict[str, Any]) -> list[str]:
    rows = ["| Path | Expectation | Why |", "| --- | --- | --- |"]
    for inv in spec.get("invariants") or []:
        if "equals" in inv:
            expectation = f"equals `{inv['equals']!r}`"
        elif "contains" in inv:
            expectation = f"contains `{inv['contains']!r}`"
        elif inv.get("absent"):
            expectation = "absent"
        else:
            expectation = "present"
        rows.append(f"| `{inv['path']}` | {expectation} | {inv.get('note', '')} |")
    if len(rows) == 2:
        rows.append("| _none_ | | |")
    return rows


def render_readme(spec: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Builds the fixture's README.md text."""
    household = spec["household"]
    rates = household.get("tax_rates") or {}
    lines: list[str] = [
        f"# Fixture portfolio: `{spec['name']}`",
        "",
        "<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->",
        f"<!-- spec: fixtures/specs/{spec['name']}.json  sha: {manifest['spec_sha']} -->",
        "",
        f"**{spec['title']}**",
        "",
        str(spec.get("purpose") or ""),
        "",
        "## At a glance",
        "",
        f"- **Household**: {household['name']} ({household.get('state_label') or household['state']})",
        f"- **Pinned `as_of`**: {spec['as_of']} (payloads and snapshots are built at this date)",
        f"- **Primary equity symbol**: "
        + (f"`{household['primary_equity_symbol']}`" if household.get("primary_equity_symbol") else "_none configured_"),
        f"- **Tax rates**: LTCG {rates.get('ltcg_top', 0):.3f}, ordinary {rates.get('ordinary_top', 0):.3f}, "
        f"state {rates.get('state_rate', 0):.3f}",
        f"- **Statements**: {len(manifest['statements'])} canonical JSON"
        + (f", {len(manifest['grants'])} grant file(s)" if manifest["grants"] else "")
        + (f", {len(manifest['ssa'])} SSA XML" if manifest["ssa"] else "")
        + (f", {len(manifest['extras'])} raw extra file(s)" if manifest["extras"] else ""),
        f"- **Scenarios**: {len([s for s in manifest['scenarios'] if not s.endswith('_index.json')])}",
        f"- **Priced symbols**: {', '.join(f'`{s}`' for s in manifest['symbols']) or '_none_'}",
        "",
        "## Household",
        "",
        *_investor_section(spec),
        "",
        "## Accounts",
        "",
        *_account_table(spec),
        "",
        "## Positions",
        "",
        *_position_table(spec),
        "",
    ]
    grant_lines = _grant_section(spec)
    if grant_lines:
        lines.extend(grant_lines)

    scenarios = spec.get("scenarios") or []
    if scenarios:
        lines.extend(["## Scenarios", ""])
        for scenario in scenarios:
            flags = " (protected)" if scenario.get("protected") else ""
            delta = scenario.get("settings_delta") or scenario.get("settings") or {}
            keys = ", ".join(f"`{k}`" for k in sorted(delta)) or "base settings"
            lines.append(f"- **{scenario['name']}**{flags}: {keys}")
        lines.append("")

    privacy = spec.get("privacy") or {}
    if privacy:
        demo = privacy.get("demo_mode") or {}
        lines.extend(
            [
                "## Privacy settings",
                "",
                f"- **AI shield mode**: `{privacy.get('ai_shield', 'identity_aliased')}`",
                f"- **Demo mode**: {'enabled' if demo.get('enabled') else 'disabled'}"
                + (f", scale {demo.get('scale')}" if demo.get("scale") else ""),
                f"- **MCP server**: {'enabled' if (privacy.get('mcp_server') or {}).get('enabled') else 'disabled'}",
                f"- **Extra synonyms**: {len(privacy.get('synonyms') or {})}",
                "",
            ]
        )

    exercises = spec.get("exercises") or []
    if exercises:
        lines.extend(["## What this fixture exercises", ""])
        lines.extend(f"- `{item}`" for item in exercises)
        lines.append("")

    lines.extend(["## Invariants asserted by tests", "", *_invariant_table(spec), ""])

    if spec.get("notes"):
        lines.extend(["## Notes", "", str(spec["notes"]), ""])

    lines.extend(
        [
            "## Using it by hand",
            "",
            "```bash",
            "# Open this fixture in the real app (no effect on your own portfolios)",
            "ROOT=$(mktemp -d)",
            f"PYTHONPATH=Scripts python3 -m tests.generate --root \"$ROOT\" --only {spec['name']}",
            'ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh',
            'ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799',
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_readme(spec: dict[str, Any], manifest: dict[str, Any], out_dir: Path) -> Path:
    path = Path(out_dir) / "README.md"
    path.write_text(render_readme(spec, manifest), encoding="utf-8")
    return path
