#!/usr/bin/env python3
"""
Readers for the view-model payload.

Tests assert against the payload a lot, and the payload is wide (40+ top-level keys, with
the simulator buckets under `retirement_sim_assets` and account rollups under
`holdings_by_account`). These accessors keep the test bodies about behaviour rather than
about dictionary spelunking, and give one place to update if a key is renamed.
"""

from __future__ import annotations

from typing import Any, Iterator

SIM_ASSETS_KEY = "retirement_sim_assets"


def sim_assets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload.get(SIM_ASSETS_KEY) or [])


def sim_asset(payload: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
    return next((a for a in sim_assets(payload) if str(a.get("id")) == asset_id), None)


def sim_asset_ids(payload: dict[str, Any]) -> list[str]:
    return [str(a.get("id")) for a in sim_assets(payload)]


def accounts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload.get("holdings_by_account") or [])


def account(payload: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    return next((a for a in accounts(payload) if str(a.get("account_id")) == account_id), None)


def account_children(payload: dict[str, Any], account_id: str) -> list[dict[str, Any]]:
    acct = account(payload, account_id) or {}
    return list(acct.get("children") or [])


def institutions(payload: dict[str, Any]) -> set[str]:
    """Every institution string the dashboard would display."""
    found: set[str] = set()
    for acct in accounts(payload):
        if acct.get("institution"):
            found.add(str(acct["institution"]))
        for child in acct.get("children") or []:
            if child.get("institution"):
                found.add(str(child["institution"]))
    return found


def account_labels(payload: dict[str, Any]) -> set[str]:
    labels = {str(a.get("account_label")) for a in accounts(payload) if a.get("account_label")}
    labels |= {str(a.get("account_label")) for a in sim_assets(payload) if a.get("account_label")}
    return labels


def holdings(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Flattens account rollups and their children into one stream of holdings."""
    for acct in accounts(payload):
        yield acct
        for child in acct.get("children") or []:
            yield child


def investors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(((payload.get("ssn_model") or {}).get("investors")) or [])


def investor(payload: dict[str, Any], investor_id: str) -> dict[str, Any] | None:
    return next((i for i in investors(payload) if str(i.get("id")) == investor_id), None)


def tax_rule_texts(payload: dict[str, Any]) -> list[str]:
    """Every liquidation tax-rule string shown in the simulator bucket table."""
    out: list[str] = []
    for asset in sim_assets(payload):
        for key in ("tax_rule_title", "tax_rule_detail"):
            if asset.get(key):
                out.append(str(asset[key]))
    return out


def tooltips(payload: dict[str, Any]) -> list[str]:
    """Every tooltip / provenance string attached to a holding."""
    keys = (
        "pct_tooltip",
        "principal_tooltip",
        "growth_tooltip",
        "source_tooltip",
        "recent_change_tip",
    )
    out: list[str] = []
    for holding in holdings(payload):
        for key in keys:
            if holding.get(key):
                out.append(str(holding[key]))
    return out


def asset_class_labels(payload: dict[str, Any]) -> list[str]:
    """Chart legend labels, from `schemas.asset`."""
    schemas = payload.get("schemas") or {}
    return [str(row.get("label")) for row in (schemas.get("asset") or []) if row.get("label")]


def all_strings(payload: Any) -> Iterator[str]:
    """Every string value and dict key anywhere in the payload (for leak scans)."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from all_strings(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from all_strings(item)
    elif isinstance(payload, str):
        yield payload


def text_blob(payload: dict[str, Any]) -> str:
    """The payload's strings and keys as one searchable blob."""
    return "\n".join(all_strings(payload))


def stock_plan_account(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    The account holding employee equity awards.

    Resolved from the grant metadata the payload itself carries, never from an
    institution name (the mistake `LL-03` records).
    """
    grants = payload.get("stock_grants") or {}
    by_symbol = grants.get("by_symbol") or {}
    for entry in by_symbol.values():
        acct_id = str((entry or {}).get("account_id") or "")
        if acct_id:
            match = account(payload, acct_id)
            if match is not None:
                return match
    for acct in accounts(payload):
        if str(acct.get("investment_type") or "").upper() in ("GSU", "RSU", "STOCK_PLAN"):
            return acct
    return None


def masked_account_id(spec_account_id: str, slug: str | None = None) -> str:
    """
    Translates a spec account id into the synthetic id the pipeline assigned it.

    Account and contract numbers are masked on ingest (finding SA-08), with the mapping
    kept in a 0600 `account_map.json` beside the database. Tests therefore assert on
    institutions and configured ids, and use this when they must name a masked id.
    """
    import json

    from . import pipeline as pipeline_support

    path = pipeline_support.portfolio_dir(slug) / "account_map.json"
    if not path.is_file():
        return spec_account_id
    mapping = (json.loads(path.read_text(encoding="utf-8")) or {}).get("accounts") or {}
    return str(mapping.get(spec_account_id) or spec_account_id)


def grant_symbols(payload: dict[str, Any]) -> list[str]:
    grants = payload.get("stock_grants") or {}
    return sorted(str(k).upper() for k in (grants.get("by_symbol") or {}))
