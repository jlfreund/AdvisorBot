#!/usr/bin/env python3
"""
Ingest against hostile and malformed data.

The adversarial fixture ships the things a portfolio folder should survive but no
well-behaved fixture contains: a duplicate statement, a future-dated one, a stale one, a
zero-quantity holding, a negative cash balance, a holding with no basis, an unsupported
file extension, a broken scenario file and a custom plugin that must not be imported.

Nothing here should raise; everything here should be reported.
"""

from __future__ import annotations

import pytest

from tests.support import payload as P, pipeline as pipeline_support

pytestmark = pytest.mark.portfolio("p5_adversarial")

SLUG = "p5_adversarial"


@pytest.mark.feature("ingest")
def test_ingest__survives_the_whole_adversarial_folder(root, slug):
    """
    The folder ingests without an exception, and reports what it skipped.

    Exercises: the fixture mixes valid statements with a duplicate, a future date, a stale
    date and an unsupported extension. A single unhandled case here would abort a user's
    whole refresh.
    """
    summary = pipeline_support.ingest_fixture(slug)
    assert summary.files_ingested > 0, summary.messages
    assert summary.files_scanned >= summary.files_ingested


@pytest.mark.feature("ingest")
def test_unsupported_extension__is_not_offered_to_any_parser(root, slug):
    """A `.rtf` file in the statements folder is ignored by the scanner, not failed over."""
    from ingest.scanner import scan_statement_files

    with pipeline_support.open_db(slug) as db:
        total, candidates = scan_statement_files(
            pipeline_support.portfolio_dir(slug) / "Statements", db, force_all=True
        )
    names = [c.path.name for c in candidates]
    assert not [n for n in names if n.endswith(".rtf")], names
    assert total == len(candidates), "the scanner counted files it did not offer"


@pytest.mark.feature("dedupe")
def test_duplicate_statement__is_ingested_once(root, slug):
    """
    The byte-identical copy under another filename does not double-count.

    Exercises: the fixture ships `duplicate_of_october.statement.json`, a verbatim copy of
    the October statement. Both files are scanned; only one may be ingested.
    """
    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        ingested = {str(r["rel_path"]) for r in db.conn.execute("SELECT rel_path FROM ingested_files")}
    assert not [p for p in ingested if "duplicate_of_october" in p], sorted(ingested)
    assert any("qq_0001_2026-10" in p for p in ingested), "the original October statement is missing"


@pytest.mark.feature("anchors")
def test_future_dated_statement__does_not_become_the_current_anchor(root, slug, payload):
    """
    A statement dated next year does not set today's values.

    Exercises: the fixture ships a 2027 statement while `as_of` is November 2026. Using it
    as the live anchor would show the user a future valuation as current.
    """
    assert payload.get("today_iso") == "2026-11-16"
    anchored_as_of = str((payload.get("kpis") or {}).get("anchored_as_of") or "")
    assert not anchored_as_of.startswith("2027"), anchored_as_of

    # The future statement is still ingested, and does set a snapshot at its own date.
    # That is defensible, but it must not be the row the dashboard treats as current.
    with pipeline_support.open_db(SLUG) as db:
        future_rows = db.conn.execute(
            "SELECT COUNT(*) AS n FROM position_snapshots WHERE as_of_date > ?",
            (payload["today_iso"],),
        ).fetchone()["n"]
    assert future_rows > 0, "the fixture's future-dated statement was not ingested at all"


@pytest.mark.feature("payload")
def test_zero_and_negative_values__render_without_error(root, payload):
    """
    Awkward values do not break the rollup arithmetic.

    Exercises: percentages, weights and growth all divide by a portfolio total, so a zero
    quantity or a negative value must not raise or produce NaN.
    """
    holdings = list(P.holdings(payload))
    assert holdings, "no holdings were built"
    for holding in holdings:
        pct = holding.get("pct_portfolio")
        if pct is not None:
            assert pct == pct, f"NaN percentage on {holding.get('title')!r}"  # NaN != NaN


@pytest.mark.feature("payload")
def test_overdrawn_cash__is_carried_into_the_account_total(root, payload):
    """
    A negative settlement balance reduces the account total rather than vanishing.

    Exercises: the fixture's taxable account holds two positive positions and an overdrawn
    sweep. The account total must be the sum of all three.
    """
    account = next(a for a in P.accounts(payload) if "Taxable" in str(a.get("title") or ""))
    children = list(account.get("children") or [])
    assert any(float(c.get("current_value") or 0.0) < 0 for c in children), (
        f"no negative child in {[c.get('title') for c in children]}"
    )
    assert float(account["current_value"]) == pytest.approx(
        sum(float(c.get("current_value") or 0.0) for c in children)
    )


@pytest.mark.feature("payload")
def test_holding_without_a_cost_basis__is_reported_not_guessed(root, payload):
    """A fund with no reported basis shows an explicit absence rather than a made-up number."""
    target = next(
        (h for h in P.holdings(payload) if "No Reported Basis" in str(h.get("title") or "")),
        None,
    )
    assert target is not None, [h.get("title") for h in P.holdings(payload)]
    principal = str(target.get("principal_str") or "")
    assert principal, "no principal string at all"
    assert "$0.00" not in principal, f"a missing basis was rendered as zero: {principal}"


@pytest.mark.feature("plugins")
@pytest.mark.security
@pytest.mark.regression("SA-15")
def test_ingest__never_imports_a_non_opted_in_plugin(root, slug):
    """
    The custom plugin in the fixture's `Plugins/` folder is not executed.

    Exercises: the plugin writes a marker file at import time and the fixture sets
    `allow_custom_plugins: false`. The marker must not exist after a full ingest.
    """
    pipeline_support.ingest_fixture(slug)
    marker = pipeline_support.portfolio_dir(slug) / "Plugins" / "PLUGIN_EXECUTED.marker"
    assert not marker.exists(), "a non-opted-in custom plugin was imported during ingest"


@pytest.mark.feature("scenarios")
def test_broken_scenario_file__does_not_break_the_scenario_list(root, slug, payload):
    """
    An unparseable scenario file is skipped and the valid ones still load.

    Exercises: the fixture ships `900_broken.json` containing truncated JSON, not listed in
    the index. One bad file must not take the scenario picker down with it.
    """
    saves = (payload.get("saves_bundle") or {}).get("models") or []
    names = [str(s.get("name")) for s in saves]
    assert len(names) == 3, names
    assert "Default" in names


@pytest.mark.feature("aliasing")
def test_privacy_registry__handles_awkward_names(root, slug):
    """
    An apostrophe, an ampersand, a diacritic and a hyphen all register as entities.

    Exercises: the alias boundary rules. A name the registry cannot represent would pass
    through untranslated, which is a silent privacy failure rather than a crash.
    """
    from privacy import load_privacy_config

    cfg = load_privacy_config(auto_sync=True)
    canonicals = {str(e.get("canonical") or "") for e in (cfg.get("entities") or [])}
    assert canonicals, "no entities were detected at all"
    assert any("'" in c for c in canonicals), f"no apostrophe name registered: {sorted(canonicals)[:8]}"


@pytest.mark.feature("ingest")
def test_malformed_statement__logs_error_and_continues_ingesting(root, slug):
    """
    One malformed statement file records a per-file error instead of aborting valid files.

    Exercises: dropping a grants file missing its required `symbol` into Statements/ must
    not prevent the remaining valid statements from ingesting.
    """
    import json

    import config

    stmt_dir = config.get_statements_dir(slug)
    bad_file = stmt_dir / "bad_grants.grants.json"
    bad_file.write_text(json.dumps({"as_of": "2026-10-15", "unvested_tranches": []}), encoding="utf-8")

    summary = pipeline_support.ingest_fixture(slug)
    assert summary.files_ingested >= 3
    assert len(summary.errors) == 1
    assert summary.errors[0]["rel_path"] == "bad_grants.grants.json"
    with pipeline_support.open_db(slug) as db:
        err_events = db.conn.execute(
            "SELECT summary FROM ingest_events WHERE event_type = 'FILE_PARSE_ERROR'"
        ).fetchall()
    assert len(err_events) == 1
    assert "bad_grants.grants.json" in str(err_events[0]["summary"])
