#!/usr/bin/env python3
"""
Scenario and project writes through the sidecar.

Covers the guarantees added after the first review: protected scenarios cannot be
destroyed through the API, deletes are reversible, Demo Mode is read-only, and the SSA
path parameter cannot read outside the portfolio.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.portfolio("p4_trust_household")


def _scenario_names(client) -> list[str]:
    response = client.get("/api/models")
    assert response.status == 200, response.text[:200]
    body = response.json()
    rows = body.get("models") or body.get("saves") or []
    return [str(r.get("name")) for r in rows]


@pytest.mark.feature("scenarios")
def test_scenarios__fixture_set_is_listed(sidecar):
    """The generated scenarios are all visible, including the one with an apostrophe."""
    sidecar.authenticate()
    names = _scenario_names(sidecar)
    assert "Default" in names
    assert "Ilona's Default" in names
    assert len(names) == 5, names


@pytest.mark.feature("scenarios")
def test_scenario_create__then_appears_in_the_index(sidecar):
    """A new scenario is written to disk and indexed."""
    sidecar.authenticate()
    response = sidecar.post(
        "/api/models",
        {"name": "Created By Test", "settings": {"simAnnualBurn": 123456}},
    )
    assert response.status == 200, response.text[:300]
    assert "Created By Test" in _scenario_names(sidecar)


@pytest.mark.feature("scenarios")
@pytest.mark.security
@pytest.mark.regression("I4")
def test_protected_scenario__cannot_be_deleted_through_the_api(sidecar):
    """
    A protected baseline survives a delete request.

    Exercises: the guarantee should be enforced in code rather than requested in a prompt,
    so no scripted call can destroy a golden baseline.
    """
    sidecar.authenticate()
    before = _scenario_names(sidecar)
    response = sidecar.post("/api/models/delete", {"name": "Ilona's Default"})
    body = response.json() if response.body else {}
    assert response.status != 200 or body.get("ok") is False, body
    assert "Ilona's Default" in _scenario_names(sidecar)
    assert set(before) == set(_scenario_names(sidecar))


@pytest.mark.feature("scenarios")
@pytest.mark.security
@pytest.mark.regression("I4")
def test_scenario_delete__is_reversible(sidecar, root, slug):
    """An ordinary scenario is moved to the trash folder rather than unlinked."""
    from tests.support import pipeline as pipeline_support

    sidecar.authenticate()
    sidecar.post("/api/models", {"name": "Throwaway", "settings": {"simAnnualBurn": 1}})
    assert "Throwaway" in _scenario_names(sidecar)

    response = sidecar.post("/api/models/delete", {"name": "Throwaway"})
    assert response.status == 200, response.text[:300]
    assert "Throwaway" not in _scenario_names(sidecar)

    scen_dir = pipeline_support.portfolio_dir(slug) / "Scenarios"
    trashed = list((scen_dir / ".trash").glob("*throwaway*.json")) if (scen_dir / ".trash").is_dir() else []
    assert trashed, f"nothing in {scen_dir / '.trash'}"


@pytest.mark.feature("scenarios")
def test_project_save__round_trips_through_the_api(sidecar, root, slug):
    """A settings change posted to `/api/project` is persisted to `project.json`."""
    from tests.support import pipeline as pipeline_support

    sidecar.authenticate()
    response = sidecar.post("/api/project", {"reference_annual_burn": 123123})
    assert response.status == 200, response.text[:300]
    saved = json.loads((pipeline_support.portfolio_dir(slug) / "project.json").read_text(encoding="utf-8"))
    assert int(saved["reference_annual_burn"]) == 123123


@pytest.mark.feature("demo_mode")
@pytest.mark.security
def test_demo_mode__blocks_mutating_routes(sidecar, root, slug):
    """
    With Demo Mode on, writes are refused so a live demonstration cannot corrupt data.

    Exercises: the read-only guard, which matters because Demo Mode is the state a user is
    in while screen-sharing and clicking around.
    """
    sidecar.authenticate()
    enable = sidecar.post("/api/privacy", {"demo_mode": {"enabled": True}})
    assert enable.status == 200, enable.text[:300]
    try:
        response = sidecar.post("/api/models", {"name": "During Demo", "settings": {}})
        body = response.json() if response.body else {}
        assert response.status == 403 or body.get("demo_blocked") is True, body
    finally:
        sidecar.post("/api/privacy", {"demo_mode": {"enabled": False}})


@pytest.mark.feature("privacy_shield")
@pytest.mark.security
@pytest.mark.regression("SA-13")
def test_ssa_parse__path_outside_the_portfolio__is_refused(sidecar, tmp_path):
    """An authenticated caller still cannot read an arbitrary file through `file_path`."""
    sidecar.authenticate()
    outside = tmp_path / "outside.xml"
    outside.write_text("<x/>", encoding="utf-8")
    response = sidecar.post("/api/ssn/parse", {"file_path": str(outside)})
    body = response.json() if response.body else {}
    assert body.get("ok") is not True, body
    assert "portfolio" in str(body.get("error", "")).lower() or response.status == 403


@pytest.mark.feature("plugins")
@pytest.mark.security
@pytest.mark.regression("SA-15")
@pytest.mark.portfolio("p5_adversarial")
def test_statement_audit__does_not_execute_a_non_opted_in_plugin(sidecar, root, slug):
    """
    Listing statements never imports a custom plugin the portfolio has not allowed.

    Exercises: the adversarial fixture ships a plugin that writes a marker file on import
    and does not opt in. The marker must not exist after the audit endpoint runs.
    """
    from tests.support import pipeline as pipeline_support

    sidecar.authenticate()
    response = sidecar.get("/api/portfolio/statements")
    assert response.status == 200, response.text[:200]
    marker = pipeline_support.portfolio_dir(slug) / "Plugins" / "PLUGIN_EXECUTED.marker"
    assert not marker.exists(), "a non-opted-in custom plugin was imported"
