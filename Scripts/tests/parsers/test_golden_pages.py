#!/usr/bin/env python3
"""
Institution parsers against golden page text.

A parser case is a `.pages.txt` file holding synthetic statement text in the
institution's own layout (pages separated by a form feed) and an `.expected.json` holding
the `ParsedStatement` the parser must return. Tests build an `IngestContext` with
`pages=[...]`, so no PDF is read and the suite needs no PDF toolchain.

The page text necessarily contains the institution's own name, because that is what the
parser detects on. `fixtures/parsers/` is therefore the one place excluded from the
portability denylist scan; see `fixtures/parsers/README.md`.

Real statement account numbers are preserved in local storage and Live View (and aliased
dynamically in Demo Mode and AI Chat Privacy Shield).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ingest.framework import IngestContext
from ingest.plugins import build_default_registry
from tests.support import roots

PAGE_SEPARATOR = "\f\n"


def _cases() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    if not roots.PARSERS_DIR.is_dir():
        return out
    for pages_file in sorted(roots.PARSERS_DIR.rglob("*.pages.txt")):
        out.append((f"{pages_file.parent.name}/{pages_file.name[: -len('.pages.txt')]}", pages_file))
    return out


def _load_case(pages_file: Path) -> tuple[list[str], dict[str, Any]]:
    pages = pages_file.read_text(encoding="utf-8").split(PAGE_SEPARATOR)
    expected_file = pages_file.with_name(pages_file.name.replace(".pages.txt", ".expected.json"))
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    return pages, expected


def _ctx(pages: list[str], name: str, tmp_path: Path) -> IngestContext:
    return IngestContext(
        file_path=tmp_path / f"{name}.pdf",
        rel_path=f"{name}.pdf",
        pages=pages,
        cfg={"_account_map_path": str(tmp_path / "account_map.json")},
    )


@pytest.mark.parametrize("case_id,pages_file", _cases(), ids=[c[0] for c in _cases()])
@pytest.mark.feature("ingest")
def test_golden_case__parses_to_the_expected_statement(case_id, pages_file, tmp_path):
    """
    Each golden page set parses to its recorded `ParsedStatement`.

    Exercises: the real institution parser, driven by text rather than by a PDF, so a
    regression in the layout handling is a readable diff rather than a binary mystery.
    """
    pages, expected = _load_case(pages_file)
    ctx = _ctx(pages, pages_file.stem, tmp_path)
    plugin, score = build_default_registry(allow_custom=False).detect_plugin(ctx)
    assert plugin is not None and score > 0.0, f"{case_id}: no plugin claimed the page text"

    stmt = plugin.parse(ctx)
    for field in (
        "doc_type", "institution", "account_id", "account_name", "account_type",
        "tax_status", "display_order", "period_start", "as_of_date", "reported_total",
    ):
        if field in expected:
            assert getattr(stmt, field) == expected[field], f"{case_id}: {field}"

    want_positions = expected.get("positions") or []
    assert len(stmt.positions) == len(want_positions), (
        f"{case_id}: expected {len(want_positions)} positions, got "
        f"{[p.security_id for p in stmt.positions]}"
    )
    by_id = {p.security_id: p for p in stmt.positions}
    for want in want_positions:
        got = by_id.get(want["security_id"])
        assert got is not None, f"{case_id}: missing position {want['security_id']}"
        for key, value in want.items():
            if key.startswith("_"):
                continue
            actual = getattr(got, key)
            if isinstance(value, float):
                assert actual == pytest.approx(value), f"{case_id}: {want['security_id']}.{key}"
            else:
                assert actual == value, f"{case_id}: {want['security_id']}.{key}"


@pytest.mark.parametrize("case_id,pages_file", _cases(), ids=[c[0] for c in _cases()])
@pytest.mark.feature("ingest")
def test_golden_case__reported_total_reconciles(case_id, pages_file, tmp_path):
    """A golden case is internally consistent, so the checksum hook stays silent."""
    pages, _ = _load_case(pages_file)
    ctx = _ctx(pages, pages_file.stem, tmp_path)
    plugin, _ = build_default_registry(allow_custom=False).detect_plugin(ctx)
    stmt = plugin.parse(ctx)
    if stmt.reported_total is None:
        pytest.skip("case reports no total")
    ok, _computed, discrepancy = plugin.verify_checksum(stmt, ctx)
    assert ok, f"{case_id}: checksum off by {discrepancy}"


@pytest.mark.feature("ingest")
def test_unrecognised_layout__is_claimed_by_nobody(tmp_path):
    """
    Text from an institution with no parser scores zero everywhere.

    Exercises: the audit table's "No Parser Plugin" state depends on detection returning
    0.0 rather than a weak false positive from an over-eager regex.
    """
    pages = [
        "SOUTHMOOR CREDIT UNION",
        "Member Statement for the period ending 2026-02-28",
        "Share Savings .......... 1,204.55",
        "Checking ............... 3,980.12",
    ]
    ctx = _ctx(pages, "southmoor_feb", tmp_path)
    plugin, score = build_default_registry(allow_custom=False).detect_plugin(ctx)
    assert plugin is None and score == 0.0, f"unexpectedly claimed by {plugin and plugin.plugin_id}"


# Plugins with no golden case yet. Each entry needs a reason; the list is asserted to be
# exactly the set of uncovered plugins, so adding a parser without a case fails here.
GOLDEN_WAIVERS: dict[str, str] = {
    "morgan_stanley_quarterly": "needs a multi-page quarterly layout; not yet authored",
    "morgan_stanley_unvested": "covered end to end by the p2 grants fixture via canonical JSON",
    "morgan_stanley_activity_xlsx": "needs an openpyxl-written workbook fixture; not yet authored",
    "ssa_statement_xml": "covered by tests/parsers/test_ssa_xml.py against generated XML",
    "generic_grant_json": "covered by tests/parsers/test_grant_json.py",
    "canonical_statement_json": "covered by tests/parsers/test_canonical_json.py",
}


@pytest.mark.feature("ingest")
def test_plugin_coverage__every_parser_has_a_case_or_a_recorded_waiver():
    """
    No parser is silently untested.

    Exercises: registering a new institution parser without a golden case fails here with
    the plugin id, which is the prompt to author one (or to record why not).
    """
    registered = {p.plugin_id for p in build_default_registry(allow_custom=False).plugins}
    with_cases = {
        path.parent.name for path in roots.PARSERS_DIR.rglob("*.pages.txt")
    } if roots.PARSERS_DIR.is_dir() else set()
    # Golden directories are named after the institution, not the plugin id.
    covered_plugins = {"wells_fargo_snapshot"} if "wells_fargo" in with_cases else set()
    uncovered = sorted(registered - covered_plugins)
    assert uncovered == sorted(GOLDEN_WAIVERS), (
        "Parser coverage drifted.\n"
        f"  uncovered now: {uncovered}\n"
        f"  waivers:       {sorted(GOLDEN_WAIVERS)}\n"
        "Author a golden case under fixtures/parsers/<institution>/ or record a waiver."
    )
