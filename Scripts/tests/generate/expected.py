#!/usr/bin/env python3
"""
Expected-snapshot generation.

After a fixture portfolio is written, the generator runs the real pipeline against it in a
throwaway root and records a narrow, allowlisted set of values under
`fixtures/expected/<slug>/`. Tests compare against these rather than embedding magic
numbers, so a deliberate model change shows up as one reviewable diff.

Four files per fixture:

    payload_summary.json   account totals, bucket ids, grant totals, institutions
    sim_kpis.json          per-bucket values and tax rules the simulator produced
    alias_table.json       privacy entity/alias counts and the demo persona mapping
    rendered_scan.json     portability scan result for the rendered dashboard
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from . import spec as spec_mod


def _meta(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "_generated_by": "tests.generate",
        "_spec_sha": spec_mod.spec_sha(spec),
        "_as_of": str(spec["as_of"]),
    }


def _payload_summary(spec: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from tests.support import payload as payload_support

    accounts = payload_support.accounts(payload)
    data: dict[str, Any] = {
        **_meta(spec),
        "has_portfolio": bool(payload.get("has_portfolio")),
        "household_name": str(payload.get("household_name") or ""),
        "today_iso": str(payload.get("today_iso") or ""),
        "account_count": len(accounts),
        "sim_asset_count": len(payload_support.sim_assets(payload)),
        "sim_asset_ids": payload_support.sim_asset_ids(payload),
        "institutions": sorted(payload_support.institutions(payload)),
        "grant_symbols": payload_support.grant_symbols(payload),
        "investor_count": len(payload_support.investors(payload)),
    }
    for acct in accounts:
        acct_id = str(acct.get("account_id"))
        data[f"account.{acct_id}.institution"] = str(acct.get("institution") or "")
        data[f"account.{acct_id}.current_value"] = round(float(acct.get("current_value") or 0.0), 2)
        data[f"account.{acct_id}.child_count"] = len(acct.get("children") or [])
    return data


def _sim_kpis(spec: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from tests.support import payload as payload_support

    data: dict[str, Any] = {**_meta(spec)}
    for asset in payload_support.sim_assets(payload):
        asset_id = str(asset.get("id"))
        data[f"asset.{asset_id}.current_value"] = round(float(asset.get("current_value") or 0.0), 2)
        data[f"asset.{asset_id}.tax_wrapper"] = str(asset.get("tax_wrapper") or "")
        data[f"asset.{asset_id}.tax_model"] = str(asset.get("tax_model") or "")
        data[f"asset.{asset_id}.tax_rule_title"] = str(asset.get("tax_rule_title") or "")
        data[f"asset.{asset_id}.default_risk_class"] = str(asset.get("default_risk_class") or "")
    kpis = payload.get("kpis") or {}
    for key in sorted(kpis):
        value = kpis[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            data[f"kpis.{key}"] = round(float(value), 2)
    return data


def _alias_table(spec: dict[str, Any]) -> dict[str, Any]:
    """Privacy registry summary: how many entities of each category were auto-detected."""
    data: dict[str, Any] = {**_meta(spec)}
    try:
        from privacy import load_privacy_config

        cfg = load_privacy_config(auto_sync=True)
    except Exception as exc:  # pragma: no cover - surfaced in the generator log
        data["error"] = f"{type(exc).__name__}: {exc}"
        return data
    entities = cfg.get("entities") or []
    data["entity_count"] = len(entities)
    by_category: dict[str, int] = {}
    for entity in entities:
        category = str(entity.get("category") or "unknown")
        by_category[category] = by_category.get(category, 0) + 1
    for category in sorted(by_category):
        data[f"entities.{category}"] = by_category[category]
    data["shield_mode"] = str((cfg.get("ai_shield") or {}).get("mode") or "")
    data["demo_enabled"] = bool((cfg.get("demo_mode") or {}).get("enabled"))
    return data


def _rendered_scan(spec: dict[str, Any], html: str) -> dict[str, Any]:
    from tests.support import scans

    hits = scans.scan_text_for_legacy(html)
    return {
        **_meta(spec),
        "legacy_term_hits": sorted(hits),
        "legacy_term_count": len(hits),
        "html_has_payload": "const PAYLOAD = " in html,
        "external_script_srcs": sorted(
            {
                line.split('src="', 1)[1].split('"', 1)[0]
                for line in html.splitlines()
                if "<script" in line and 'src="http' in line
            }
        ),
    }


# Public names so tests build the *same* summaries the generator recorded, rather than a
# parallel implementation that could drift from it.
payload_summary = _payload_summary
sim_kpis = _sim_kpis
rendered_scan = _rendered_scan
alias_table = _alias_table


def build_expected(spec: dict[str, Any], fixture_dir: Path, out_dir: Path) -> list[Path]:
    """
    Runs the pipeline on a copy of the fixture and writes its expected snapshots.

    Uses a throwaway root so generation never touches the developer's own portfolios and
    never leaves an `index.html` behind in the checkout.
    """
    from tests.support import env, pipeline as pipeline_support, roots

    slug = str(spec["name"])
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=f"advisorbot-gen-{slug}-") as tmp:
        root = Path(tmp) / "root"
        roots.assemble_root(root, [slug])
        with env.pointed_at(root):
            pipeline_support.clear_payload_cache()
            payload = pipeline_support.build_payload(slug, today=str(spec["as_of"]))
            html_path = pipeline_support.render_payload(payload)
            html = html_path.read_text(encoding="utf-8")
            files = {
                "payload_summary": _payload_summary(spec, payload),
                "sim_kpis": _sim_kpis(spec, payload),
                "alias_table": _alias_table(spec),
                "rendered_scan": _rendered_scan(spec, html),
            }
            pipeline_support.clear_payload_cache()
    target = Path(out_dir) / slug
    target.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = target / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
