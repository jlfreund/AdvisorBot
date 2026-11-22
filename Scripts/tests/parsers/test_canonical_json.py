#!/usr/bin/env python3
"""
The canonical statement JSON plugin (`*.statement.json`).

This format is how fixture portfolios are authored, so its validation has to be strict:
a typo in a hand-written statement must fail at parse time rather than silently drop a
holding and quietly change every downstream number.

It is also a user-facing feature for institutions with no PDF parser yet, which is why the
plugin participates in detection, dedupe and account-metadata resolution like any other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest.errors import StatementParseError
from ingest.framework import IngestContext
from ingest.plugins import build_default_registry
from ingest.plugins.canonical_json import (
    FORMAT_ID,
    CanonicalStatementJsonPlugin,
    parse_canonical_statement_payload,
)
from tests.support import roots

MINIMAL = {
    "format": FORMAT_ID,
    "institution": "Harbor Brokerage",
    "account_id": "HB-7001",
    "account_name": "Harbor Individual Brokerage",
    "account_type": "BROKERAGE",
    "tax_status": "TAXABLE",
    "as_of_date": "2026-09-30",
    "reported_total": 10000.0,
    "positions": [
        {
            "security_id": "TICKER:VTI",
            "symbol_or_code": "VTI",
            "canonical_name": "Vanguard Total Stock Market ETF",
            "asset_class": "ETF",
            "stack_bucket": "stocks",
            "risk_class": "broad_equity",
            "is_scrapeable": True,
            "quote_symbol": "VTI",
            "quantity": 50.0,
            "stmt_price": 200.0,
            "market_value": 10000.0,
            "value_date": "2026-09-30",
        }
    ],
}


def _write(tmp_path: Path, payload: dict, name: str = "harbor_2026-09.statement.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ctx(path: Path, cfg: dict | None = None) -> IngestContext:
    return IngestContext(file_path=path, rel_path=path.name, cfg=cfg or {})


@pytest.mark.feature("ingest")
def test_detection__canonical_file__wins_over_every_other_plugin(tmp_path):
    """A `*.statement.json` file is claimed by the canonical plugin, not by the grants plugin."""
    path = _write(tmp_path, MINIMAL)
    plugin, score = build_default_registry(allow_custom=False).detect_plugin(_ctx(path))
    assert plugin is not None and plugin.plugin_id == "canonical_statement_json"
    assert score > 0.9


@pytest.mark.feature("ingest")
def test_detection__json_without_the_format_header__is_not_claimed(tmp_path):
    """An arbitrary JSON file named `.statement.json` is ignored without the format marker."""
    path = _write(tmp_path, {"institution": "Harbor Brokerage", "positions": []})
    assert CanonicalStatementJsonPlugin().detect(_ctx(path)) == 0.0


@pytest.mark.feature("ingest")
def test_parse__minimal_statement__maps_onto_the_parsed_schema(tmp_path):
    """Fields land on `ParsedStatement`/`ParsedPosition` without reinterpretation."""
    stmt = CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, MINIMAL)))
    assert stmt.doc_type == "CANONICAL_JSON"
    assert stmt.institution == "Harbor Brokerage"
    assert stmt.account_id == "HB-7001"
    assert stmt.as_of_date == "2026-09-30"
    assert len(stmt.positions) == 1
    position = stmt.positions[0]
    assert position.security_id == "TICKER:VTI"
    assert position.quantity == 50.0
    assert position.market_value == 10000.0
    assert position.risk_class == "broad_equity"


@pytest.mark.feature("ingest")
def test_parse__reported_total__reconciles_against_summed_positions(tmp_path):
    """The checksum hook sees a consistent statement, so fixtures never warn."""
    plugin = CanonicalStatementJsonPlugin()
    ctx = _ctx(_write(tmp_path, MINIMAL))
    stmt = plugin.parse(ctx)
    ok, computed, discrepancy = plugin.verify_checksum(stmt, ctx, strict=True)
    assert ok and discrepancy == 0.0 and computed == 10000.0


@pytest.mark.feature("ingest")
def test_parse__account_overrides_in_config__win_over_statement_metadata(tmp_path):
    """`project.json -> accounts[id]` renames an account without touching the statement."""
    cfg = {"accounts": {"HB-7001": {"account_name": "Renamed By Config", "tax_status": "ROTH_IRA"}}}
    stmt = CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, MINIMAL), cfg))
    assert stmt.account_name == "Renamed By Config"
    assert stmt.tax_status == "ROTH_IRA"


@pytest.mark.feature("ingest")
@pytest.mark.parametrize(
    "mutate,expected_fragment",
    [
        (lambda p: p.update(format="advisorbot.statement/99"), "`format` must be"),
        (lambda p: p.update(nonsense=1), "unknown statement key"),
        (lambda p: p.pop("institution"), "`institution` is required"),
        (lambda p: p.update(as_of_date="30-09-2026"), "YYYY-MM-DD"),
        (lambda p: p["positions"][0].update(surprise=1), "unknown position key"),
        (lambda p: p["positions"][0].pop("quantity"), "`quantity` is required"),
        (lambda p: p["positions"][0].update(market_value="lots"), "must be a number"),
        (lambda p: p["positions"][0].update(risk_class=""), "risk_class"),
        (lambda p: p["positions"][0].update(risk_class="single_stock", asset_class="CASH"), "Conflicting"),
        (lambda p: p["positions"][0].update(is_scrapeable=True, quote_symbol=None), "`quote_symbol` is required"),
        (lambda p: p.update(positions=[]), "no positions and no grants"),
    ],
)
def test_parse__malformed_input__raises_with_a_pointer_to_the_problem(tmp_path, mutate, expected_fragment):
    """
    Every validation failure names the offending field.

    Exercises: a hand-written statement is a user-facing format, so the error has to be
    actionable rather than a stack trace from deep in the pipeline.
    """
    payload = json.loads(json.dumps(MINIMAL))
    mutate(payload)
    with pytest.raises(StatementParseError) as exc:
        CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, payload)))
    assert expected_fragment in str(exc.value)


@pytest.mark.feature("ingest")
def test_parse__duplicate_security_ids__are_rejected(tmp_path):
    """Two rows for one security would double-count silently, so it is an error."""
    payload = json.loads(json.dumps(MINIMAL))
    payload["positions"].append(json.loads(json.dumps(payload["positions"][0])))
    payload["reported_total"] = 20000.0
    with pytest.raises(StatementParseError) as exc:
        CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, payload)))
    assert "duplicate security_id" in str(exc.value)


@pytest.mark.feature("ingest")
def test_parse__embedded_grants__are_accepted_from_one_file(tmp_path):
    """A single-file fixture can carry positions and awards together."""
    payload = json.loads(json.dumps(MINIMAL))
    payload["grants"] = {
        "institution": "Meridian Equity Services",
        "account_id": "MES-3301",
        "symbol": "VNTG",
        "company_name": "Vantage Robotics, Inc.",
        "award_type": "RSU",
        "as_of_date": "2026-09-30",
        "unvested_tranches": [
            {
                "grant_id": "G-1",
                "grant_date": "2025-01-15",
                "vest_date": "2027-01-15",
                "orig_qty": 100,
                "total_qty": 100,
                "symbol": "VNTG",
            }
        ],
        "releases_history": [],
        "withdrawals": [],
    }
    stmt = CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, payload)))
    assert len(stmt.gsu_tranches) == 1
    assert stmt.gsu_tranches[0].symbol == "VNTG"
    assert stmt.gsu_tranches[0].award_type == "RSU"


@pytest.mark.feature("ingest")
def test_parse__adjustment_rows__carry_split_history(tmp_path):
    """Share-count adjustments ride along with the statement that reports them."""
    payload = json.loads(json.dumps(MINIMAL))
    payload["adjustments"] = [
        {
            "security_id": "TICKER:VTI",
            "effective_date": "2026-09-15",
            "adj_type": "PRE_SPLIT_SHARES",
            "pre_quantity": 200.0,
            "post_quantity": 50.0,
            "notes": "1-for-4 reverse split",
        }
    ]
    stmt = CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, payload)))
    assert len(stmt.adjustments) == 1
    assert stmt.adjustments[0].adj_type == "PRE_SPLIT_SHARES"


@pytest.mark.feature("ingest")
def test_parse__unknown_adjustment_type__is_rejected(tmp_path):
    """Only the two adjustment types the pipeline understands are accepted."""
    payload = json.loads(json.dumps(MINIMAL))
    payload["adjustments"] = [
        {
            "security_id": "TICKER:VTI",
            "effective_date": "2026-09-15",
            "adj_type": "MADE_UP",
            "pre_quantity": 1.0,
            "post_quantity": 1.0,
        }
    ]
    with pytest.raises(StatementParseError) as exc:
        CanonicalStatementJsonPlugin().parse(_ctx(_write(tmp_path, payload)))
    assert "PRE_SPLIT_SHARES" in str(exc.value)


@pytest.mark.feature("ingest")
def test_committed_fixture_statements__all_parse(tmp_path):
    """
    Every statement the generator has written is parseable right now.

    Exercises: catches a generator change that produces data the plugin would reject,
    before it reaches the pipeline tier where the failure is harder to read.
    """
    plugin = CanonicalStatementJsonPlugin()
    checked = 0
    for slug in roots.FIXTURE_SLUGS:
        for path in sorted((roots.PORTFOLIOS_DIR / slug).rglob("*.statement.json")):
            stmt = plugin.parse(_ctx(path))
            assert stmt.as_of_date, f"{path}: no as_of_date"
            checked += 1
    assert checked >= 100, f"expected the fixture corpus, parsed only {checked}"


@pytest.mark.feature("ingest")
def test_payload_helper__is_reusable_without_a_file():
    """The validator is importable on its own, for callers building statements in memory."""
    stmt = parse_canonical_statement_payload(MINIMAL, source_label="in-memory")
    assert stmt.institution == "Harbor Brokerage"
