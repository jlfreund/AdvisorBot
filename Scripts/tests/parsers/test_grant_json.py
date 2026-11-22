#!/usr/bin/env python3
"""
Canonical grant JSON parsing (`*.grants.json`).

The equity-award format is where the award type, symbol and company name enter the
pipeline. Findings LL-06 and LL-14 are about those values having legacy defaults, so these
tests pin that an award type and symbol supplied by the file survive unchanged, and that a
missing symbol is rejected rather than defaulted.
"""

from __future__ import annotations

import json

import pytest

from ingest.errors import StatementParseError
from ingest.framework import IngestContext
from ingest.plugins.grant_json import GenericGrantJsonPlugin, parse_grants_payload_to_statement
from tests.support import roots

FIXTURE = roots.PORTFOLIOS_DIR / "p2_equity_comp_rsu" / "Statements" / "meridian_equity_services" / "vntg_awards.grants.json"


@pytest.mark.feature("grants")
def test_fixture_grants_file__parses_into_tranches_releases_and_withdrawals():
    """The generated award file carries unvested tranches, vested releases and a transfer."""
    ctx = IngestContext(file_path=FIXTURE, rel_path=FIXTURE.name, cfg={})
    plugin = GenericGrantJsonPlugin()
    assert plugin.detect(ctx) > 0.9
    stmt = plugin.parse(ctx)
    assert stmt.gsu_tranches, "expected unvested tranches"
    assert stmt.gsu_releases, "expected a vested release history"
    assert stmt.gsu_withdrawals, "expected the in-kind transfer"


@pytest.mark.feature("grants")
@pytest.mark.regression("LL-06")
def test_award_type_and_symbol__come_from_the_file_not_from_a_default():
    """
    Every parsed row carries the fixture's own ticker and award type.

    Exercises: the fixture grants RSUs of a ticker the application was not built around.
    A legacy default would show up as a different symbol or award type on some row.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stmt = parse_grants_payload_to_statement(payload, source_label=FIXTURE.name)
    rows = [*stmt.gsu_tranches, *stmt.gsu_releases]
    assert rows, "fixture has no award rows"
    assert {row.symbol for row in rows} == {"VNTG"}
    assert {row.award_type for row in rows} == {"RSU"}
    assert {row.company_name for row in rows} == {"Vantage Robotics, Inc."}


@pytest.mark.feature("grants")
def test_releases__net_shares_reflect_the_specified_withholding():
    """Net shares are gross minus withholding, which is what retention is computed from."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stmt = parse_grants_payload_to_statement(payload, source_label=FIXTURE.name)
    for release in stmt.gsu_releases:
        assert release.net_shares < release.gross_shares
        retention = release.net_shares / release.gross_shares
        assert retention == pytest.approx(0.62, abs=0.005)


@pytest.mark.feature("grants")
@pytest.mark.regression("LL-14")
def test_missing_symbol__is_rejected_rather_than_defaulted():
    """
    A tranche with no symbol fails loudly.

    Exercises: the adversarial fixture ships such a file deliberately. Defaulting it to a
    ticker would attribute someone else's awards to the wrong company.
    """
    path = roots.PORTFOLIOS_DIR / "p5_adversarial" / "Malformed" / "missing_symbol.grants.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises((StatementParseError, ValueError)) as exc:
        parse_grants_payload_to_statement(payload, source_label=path.name)
    assert "symbol" in str(exc.value)


@pytest.mark.feature("grants")
def test_empty_payload__is_rejected():
    """A grants file with no rows is a mistake, not an empty success."""
    with pytest.raises((StatementParseError, ValueError)):
        parse_grants_payload_to_statement(
            {"unvested_tranches": [], "releases_history": [], "withdrawals": []},
            source_label="empty.grants.json",
        )
