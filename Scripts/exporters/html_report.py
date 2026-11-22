#!/usr/bin/env python3
"""Self-contained multi-screen interactive HTML5/SVG report renderer (`~/Development/AdvisorBot/index.html`)."""

from __future__ import annotations

import html as html_lib
import json
import os
from pathlib import Path
from typing import Any

from valuation import federal_tax_2026

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _read_template_part(filename: str) -> str:
    return (_TEMPLATE_DIR / filename).read_text(encoding="utf-8").rstrip("\n")


def _load_html_template() -> str:
    """Assemble the standalone single-file HTML template from `templates/` parts."""
    styles = _read_template_part("styles.css")
    body = _read_template_part("body.html")
    scripts = "\n".join(
        _read_template_part(name)
        for name in ("app_core.js", "app_simulator.js", "app_charts.js", "app_workspace.js")
    )
    federal_tables = {
        "FED_STD_DED_2026": federal_tax_2026.STD_DEDUCTION,
        "FED_ORD_TOPS_MFJ": federal_tax_2026.ORDINARY_JOINT,
        "FED_ORD_TOPS_SINGLE": federal_tax_2026.ORDINARY_SINGLE,
        "FED_LTCG_TOPS_MFJ": federal_tax_2026.GAINS_JOINT,
        "FED_LTCG_TOPS_SINGLE": federal_tax_2026.GAINS_SINGLE,
        "FED_ORD_LOW_RATES": federal_tax_2026.LOW_RATES,
        "FLOOR_TAXABLE_STARTS_MFJ": federal_tax_2026.FLOORS_JOINT,
        "FLOOR_TAXABLE_STARTS_SINGLE": federal_tax_2026.FLOORS_SINGLE,
    }
    scripts = scripts.replace("__FEDERAL_TAX_2026__", json.dumps(federal_tables))
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '  <link rel="icon" href="__BADGE_IMAGE__" />\n'
        '  <title>__PAGE_TITLE__ — Portfolio &amp; Retirement Simulator</title>\n'
        '  <style>\n'
        f'{styles}\n'
        '  </style>\n'
        '</head>\n'
        f'{body}\n\n'
        '  <script>\n'
        f'{scripts}\n'
        '  </script>\n'
        '</body>\n'
        '</html>\n'
    )


HTML_TEMPLATE = _load_html_template()


def get_default_badge_prompt(portfolio_name: str) -> str:
    name = (portfolio_name or "Household Portfolio").strip()
    return (
        f'Create an opulent, gilded typographic header graphic for "{name}". '
        'Feature an ornate gold filigree border framing the title, rich champagne and polished gold accents, '
        'emerald jewel highlights, and bespoke luxury serif typography on midnight navy.'
    )


def build_html_string(payload: dict[str, Any]) -> str:
    """Embeds `ReportPayload` into the standalone interactive HTML template."""
    tmpl = _load_html_template()
    safe_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    title = str(payload.get("household_name", "My Portfolio"))
    badge_img = str(payload.get("badge_image") or (payload.get("project") or {}).get("badge_image") or "Assets/default_badge.svg")
    gen_at = str(payload.get("generated_at", ""))
    has_portfolio = bool(payload.get("has_portfolio"))
    active_portfolio_name = str(
        payload.get("household_name")
        or (payload.get("project") or {}).get("household_name")
        or "Portfolio Settings"
    )
    if has_portfolio:
        nav_lbl = active_portfolio_name
        nav_sub = "Portfolio Settings"
    else:
        nav_lbl = "Portfolio Settings"
        nav_sub = "Select or create portfolio"

    badge_prompt = get_default_badge_prompt(active_portfolio_name if has_portfolio else "Household Portfolio")

    # SECURITY DO: Escape all interpolated title/label/attribute tokens with `html_lib.escape`, strip `</` in inline JSON,
    # block dangerous badge URI schemes (`javascript:`, `data:text/html`, `data:image/svg+xml`), and write `index.html` as `0o600` (`SA-22`).
    # SECURITY DON'T: Never interpolate raw config strings unescaped into `index.html` or relax file mode beyond `0o600`.
    safe_badge = html_lib.escape(badge_img, quote=True)
    if badge_img:
        lower_badge = badge_img.strip().lower()
        if lower_badge.startswith(("javascript:", "vbscript:", "data:text/html", "data:image/svg+xml")):
            safe_badge = ""
    html = (
        tmpl
        .replace("__PAGE_TITLE__", html_lib.escape(title, quote=True))
        .replace("__BADGE_IMAGE__", safe_badge)
        .replace("__GENERATED_AT__", html_lib.escape(gen_at, quote=True))
        .replace("__PORTFOLIO_NAV_LBL__", html_lib.escape(nav_lbl))
        .replace("__PORTFOLIO_NAV_SUB__", html_lib.escape(nav_sub))
        .replace("__DEFAULT_BADGE_PROMPT__", html_lib.escape(badge_prompt))
        .replace("__REPORT_PAYLOAD_JSON__", safe_json)
    )
    return html.replace("\r\n", "\n")


def render_html_report(payload: dict[str, Any], output_path: Path) -> Path:
    """Writes `index.html` with strict Unix LF (`\\n`) line endings and owner-only permissions."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_html_string(payload)
    output_path.write_bytes(html.encode("utf-8"))
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass
    return output_path
