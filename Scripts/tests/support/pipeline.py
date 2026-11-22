#!/usr/bin/env python3
"""
Runs the real ingest -> SQLite -> view-model -> render pipeline against a fixture root.

Everything happens in-process (no subprocess, no network): statements are ingested with
`ingest.pipeline.ingest_statements`, the payload is built with
`exporters.view_model.build_report_payload` at the fixture's pinned `as_of` date, and the
dashboard is rendered with `exporters.html_report.render_html_report`.

Payloads are cached per (root, slug, today) because building one is the expensive part of
the suite; callers that mutate get an explicit deep copy.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

_PAYLOAD_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


def clear_payload_cache() -> None:
    _PAYLOAD_CACHE.clear()


def payload_copy(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep copy, for tests that mutate a cached payload."""
    return copy.deepcopy(payload)


def active_slug() -> str:
    import config

    return str(config.get_active_portfolio_id() or "")


def portfolio_dir(slug: str | None = None) -> Path:
    import config

    path = config.get_portfolio_dir(slug)
    if path is None:
        raise RuntimeError("No active portfolio in the current root")
    return path


def load_cfg(slug: str | None = None) -> dict[str, Any]:
    import config

    return config.load_config(config.get_default_config_path(slug), portfolio_id=slug)


def open_db(slug: str | None = None):
    """Opens the fixture portfolio's SQLite database (schema ensured)."""
    import config
    from db.database import PortfolioDB

    db_path = config.get_default_db_path(slug)
    if db_path is None:
        return PortfolioDB(":memory:")
    db = PortfolioDB(db_path)
    db.ensure_schema()
    return db


def seed_market_prices(db, slug: str | None = None) -> int:
    """
    Loads the fixture's committed `market_prices.json` into the price cache.

    Fixtures ship their own price history so no test ever needs the network; the shape is
    `{"<SYMBOL>": {"YYYY-MM-DD": close, ...}, ...}`.
    """
    path = portfolio_dir(slug) / "market_prices.json"
    if not path.is_file():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = 0
    for symbol, series in sorted(data.items()):
        for price_date, close in sorted(series.items()):
            db.conn.execute(
                "INSERT OR REPLACE INTO market_prices "
                "(quote_symbol, price_date, close_price, adj_close, fetched_at_iso) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(symbol).upper(), str(price_date), float(close), float(close), "fixture"),
            )
            rows += 1
    db.conn.commit()
    return rows


def ingest_fixture(slug: str | None = None, *, force_all: bool = True) -> Any:
    """Ingests every statement in the fixture portfolio. Returns the `IngestSummary`."""
    import config
    from ingest.pipeline import ingest_statements

    cfg = load_cfg(slug)
    stmt_dir = config.get_statements_dir(slug)
    with open_db(slug) as db:
        seed_market_prices(db, slug)
        run_id = db.start_run("test-ingest")
        summary = ingest_statements(stmt_dir, db, cfg, run_id=run_id, force_all=force_all)
        db.finish_run(
            run_id,
            files_scanned=summary.files_scanned,
            files_ingested=summary.files_ingested,
            positions_changed=summary.positions_changed,
            anchors_updated=summary.anchors_updated,
        )
        return summary


def build_payload(
    slug: str | None = None,
    *,
    today: str | None = None,
    ingest: bool = True,
    fresh: bool = False,
) -> dict[str, Any]:
    """
    Returns the view-model payload for a fixture portfolio.

    Ingests first (unless `ingest=False`), then builds at the fixture's pinned `as_of`
    date so snapshots are stable. The result is cached and returned by reference; pass
    `fresh=True` (or deep-copy it yourself) before mutating.
    """
    import config
    from exporters.view_model import build_report_payload
    from valuation.forecast_enricher import enrich_holding_forecasts

    from . import roots

    slug = slug or active_slug()
    today_iso = today or roots.spec_as_of(slug)
    key = (str(config.HOLDINGS_ROOT), slug, today_iso)
    if not fresh and key in _PAYLOAD_CACHE:
        return _PAYLOAD_CACHE[key]

    if ingest:
        ingest_fixture(slug)
    cfg = load_cfg(slug)
    today_date = date.fromisoformat(today_iso)
    with open_db(slug) as db:
        seed_market_prices(db, slug)
        try:
            enrich_holding_forecasts(db, cfg, today_date, sync_prices=False)
        except Exception:
            # Best-effort: an empty fixture portfolio has nothing to enrich.
            pass
        payload = build_report_payload(db=db, cfg=cfg, today_date=today_date)
    _PAYLOAD_CACHE[key] = payload
    return payload


def render_payload(payload: dict[str, Any], out_path: Path | None = None) -> Path:
    """Renders a payload to `index.html` inside the active root (or `out_path`)."""
    import config
    from exporters.html_report import render_html_report

    target = Path(out_path) if out_path is not None else Path(config.DEFAULT_HTML_REPORT_PATH)
    return render_html_report(payload, target)


def render_fixture(slug: str | None = None, *, today: str | None = None) -> tuple[Path, dict[str, Any]]:
    """Builds and renders a fixture portfolio. Returns `(index_html_path, payload)`."""
    payload = build_payload(slug, today=today)
    return render_payload(payload), payload


def rendered_html(slug: str | None = None, *, today: str | None = None) -> str:
    path, _ = render_fixture(slug, today=today)
    return path.read_text(encoding="utf-8")


def embedded_payload_json(html: str) -> str:
    """Returns the raw JSON text of the payload embedded in a rendered dashboard."""
    marker = "const PAYLOAD = "
    start = html.find(marker)
    if start < 0:
        raise AssertionError("No embedded PAYLOAD found in rendered HTML")
    start += len(marker)
    depth = 0
    for idx in range(start, len(html)):
        ch = html[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : idx + 1]
    raise AssertionError("Unterminated embedded PAYLOAD in rendered HTML")


def embedded_payload(html: str) -> dict[str, Any]:
    """Extracts the JSON payload embedded in a rendered dashboard."""
    marker = "const PAYLOAD = "
    start = html.find(marker)
    if start < 0:
        raise AssertionError("No embedded PAYLOAD found in rendered HTML")
    start += len(marker)
    depth = 0
    for idx in range(start, len(html)):
        ch = html[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = html[start : idx + 1]
                return json.loads(raw.replace("<\\/", "</"))
    raise AssertionError("Unterminated embedded PAYLOAD in rendered HTML")
