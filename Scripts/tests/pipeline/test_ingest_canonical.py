#!/usr/bin/env python3
"""
Ingest end to end on a fixture portfolio: statements to SQLite to payload.

Replaces the old single pipeline test, which ingested the owner's real statement folder
and asserted their share counts and account values to the cent. Same intent, no real data:
the fixture ships eight monthly statements for one taxable brokerage account.
"""

from __future__ import annotations

import shutil

import pytest

from tests.support import payload as P, pipeline as pipeline_support, roots

pytestmark = pytest.mark.portfolio("p1_index_saver")


@pytest.mark.feature("ingest")
def test_ingest__fixture_statements__all_recognised_and_stored(root, slug):
    """
    Every statement in the fixture is claimed by a plugin and ingested.

    Exercises: the canonical statement format through the real scanner, registry and
    pipeline, including chronological ordering and the run watermark.
    """
    summary = pipeline_support.ingest_fixture(slug)
    assert summary.files_scanned == 8
    assert summary.files_ingested == 8
    assert summary.positions_changed > 0
    assert not [m for m in summary.messages if "Skipped unrecognized" in m]


@pytest.mark.feature("ingest")
def test_ingest__securities_and_snapshots__land_in_the_database(root, slug):
    """Positions become securities plus point-in-time snapshots, one set per statement date."""
    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        securities = {r["security_id"] for r in db.conn.execute("SELECT security_id FROM securities")}
        assert {"TICKER:VTI", "TICKER:BNDX"} <= securities
        dates = [
            r["as_of_date"]
            for r in db.conn.execute("SELECT DISTINCT as_of_date FROM position_snapshots ORDER BY as_of_date")
        ]
        assert dates == [
            "2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30",
            "2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31",
        ]


@pytest.mark.feature("anchors")
def test_ingest__non_scrapeable_cash__gets_a_valuation_anchor(root, slug):
    """The cash sweep is statement-anchored, so it must produce an anchor row."""
    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        rows = db.conn.execute(
            "SELECT security_id, value_date FROM valuation_anchors ORDER BY value_date"
        ).fetchall()
        assert rows, "no valuation anchors were written"
        assert any(str(r["security_id"]).startswith("CASH:") for r in rows)


@pytest.mark.feature("ingest")
def test_ingest__events_log__records_one_row_per_ingested_file(root, slug):
    """`ingest_events` is the audit trail the dashboard's recent-activity panel reads."""
    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM ingest_events WHERE event_type = 'FILE_INGESTED'"
        ).fetchone()["n"]
        assert count == 8


@pytest.mark.feature("dedupe")
def test_ingest__identical_statement_under_a_new_name__is_deduplicated(root, slug):
    """
    Re-dropping the same bytes under a different filename ingests nothing new.

    Exercises: the SHA-256 content check, which is what stops a re-downloaded statement
    from double-counting a position.
    """
    pipeline_support.ingest_fixture(slug)
    stmt_dir = pipeline_support.portfolio_dir(slug) / "Statements" / "harbor_brokerage"
    original = sorted(stmt_dir.glob("*.statement.json"))[0]
    shutil.copyfile(original, stmt_dir / "a_copy_under_another_name.statement.json")

    summary = pipeline_support.ingest_fixture(slug, force_all=False)
    assert summary.files_ingested == 0, summary.messages


@pytest.mark.feature("ingest")
def test_ingest__rerun_without_changes__is_idempotent(root, slug):
    """A second refresh over unchanged files changes no positions."""
    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        before = db.conn.execute("SELECT COUNT(*) AS n FROM position_snapshots").fetchone()["n"]
    second = pipeline_support.ingest_fixture(slug, force_all=False)
    with pipeline_support.open_db(slug) as db:
        after = db.conn.execute("SELECT COUNT(*) AS n FROM position_snapshots").fetchone()["n"]
    assert second.files_ingested == 0
    assert before == after


@pytest.mark.feature("payload")
def test_payload__matches_the_committed_summary_snapshot(root, slug, payload):
    """
    The payload agrees with `fixtures/expected/<slug>/payload_summary.json`.

    Exercises: account totals, bucket ids, institutions and grant symbols in one
    assertion, so a model change surfaces as one reviewable snapshot diff.
    """
    from tests.generate import expected as expected_mod
    from tests.support import snapshots

    spec = roots.load_spec(slug)
    snapshots.assert_flat(expected_mod.payload_summary(spec, payload), slug, "payload_summary")


@pytest.mark.feature("payload")
def test_payload__simulator_buckets__match_the_committed_snapshot(root, slug, payload):
    """Per-bucket values, wrappers and rule titles agree with the recorded snapshot."""
    from tests.generate import expected as expected_mod
    from tests.support import snapshots

    spec = roots.load_spec(slug)
    snapshots.assert_flat(expected_mod.sim_kpis(spec, payload), slug, "sim_kpis")


@pytest.mark.feature("payload")
def test_payload__spec_invariants__hold(root, slug, payload):
    """
    The invariants the fixture's spec declares are true of the built payload.

    Exercises: the spec's own `invariants` block, so the README's claims about a fixture
    are executable rather than decorative.
    """
    from tests.generate import expected as expected_mod
    from tests.support import invariants

    spec = roots.load_spec(slug)
    summary = expected_mod.payload_summary(spec, payload)
    failures, known_gaps, fixed_gaps = invariants.check(spec, payload, summary)
    assert not failures, "\n".join(failures)
    assert not fixed_gaps, (
        "A spec invariant marked as a known gap now holds. Remove its `known_gap` field "
        "and close the finding:\n" + "\n".join(fixed_gaps)
    )
    if known_gaps:
        pytest.xfail("known open findings: " + ", ".join(known_gaps))


@pytest.mark.feature("cash_interest")
def test_cash_sweep__is_a_single_merged_bucket_with_its_configured_rate(root, payload, cfg):
    """Cash across accounts rolls into one simulator bucket carrying the configured APY."""
    cash = P.sim_asset(payload, "cash_sweeps")
    assert cash is not None, f"no cash bucket among {P.sim_asset_ids(payload)}"
    expected_rate = (cfg.get("sim_asset_rates") or {}).get("cash_sweeps", {}).get("annual_rate_pct")
    assert cash.get("annual_rate_pct") == pytest.approx(float(expected_rate))
