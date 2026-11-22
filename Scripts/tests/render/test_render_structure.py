#!/usr/bin/env python3
"""
The generated dashboard: structure, self-containment and escaping.

Every fixture portfolio must render, including the empty one, and the result must be a
single self-contained page. The escaping checks run against the adversarial fixture, whose
holding and scenario names carry script payloads.
"""

from __future__ import annotations

import html as html_lib
import json
import re

import pytest

from tests.support import pipeline as pipeline_support, roots

ALL_SLUGS = list(roots.FIXTURE_SLUGS)


@pytest.mark.parametrize("slug", ALL_SLUGS)
@pytest.mark.feature("render")
def test_every_fixture__renders_a_page_with_an_embedded_payload(request, slug, tmp_path_factory):
    """
    All six fixtures render, and the payload round-trips out of the HTML.

    Exercises: the empty household is included deliberately, because that is the state a
    new user is in and the one most likely to trip an unguarded default.
    """
    from tests.support import env

    target = tmp_path_factory.mktemp(f"root-{slug}")
    roots.assemble_root(target, [slug])
    with env.pointed_at(target):
        pipeline_support.clear_payload_cache()
        path, payload = pipeline_support.render_fixture(slug)
        html = path.read_text(encoding="utf-8")
        pipeline_support.clear_payload_cache()

    assert path.stat().st_size > 50_000, "page is implausibly small"
    embedded = pipeline_support.embedded_payload(html)
    assert embedded.get("household_name") == payload.get("household_name")
    assert embedded.get("today_iso") == payload.get("today_iso")


@pytest.mark.portfolio("p1_index_saver")
@pytest.mark.feature("render")
@pytest.mark.regression("SA-09")
def test_rendered_page__loads_no_external_resources(rendered):
    """
    The page is self-contained: no third-party script, style, font or image.

    Exercises: the "zero external dependency" claim, and finding SA-09 (a CORS-proxy
    fallback that leaked the ticker list to an unaffiliated host).
    """
    _path, html = rendered
    externals = re.findall(r"""<(?:script|link|img)[^>]+(?:src|href)\s*=\s*["'](https?://[^"']+)""", html)
    allowed_prefixes = ("https://finance.yahoo.com/quote/",)  # user-clickable quote links only
    offenders = [url for url in externals if not url.startswith(allowed_prefixes)]
    assert not offenders, f"external resources referenced: {sorted(set(offenders))}"


@pytest.mark.portfolio("p1_index_saver")
@pytest.mark.feature("render")
@pytest.mark.regression("SA-09")
def test_rendered_page__contacts_no_third_party_host_at_runtime(rendered):
    """No fetch target outside the local sidecar and the quote provider survives in the page."""
    _path, html = rendered
    fetch_urls = re.findall(r"""fetch\(\s*[`'"]([^`'"]+)""", html)
    remote = [u for u in fetch_urls if u.startswith("http")]
    allowed = ("https://query1.finance.yahoo.com/",)
    offenders = [u for u in remote if not u.startswith(allowed)]
    assert not offenders, f"runtime fetches to third parties: {sorted(set(offenders))}"


@pytest.mark.portfolio("p4_trust_household")
@pytest.mark.feature("render")
@pytest.mark.regression("LL-23")
def test_page_title__is_the_household_name_without_a_product_suffix(rendered, cfg):
    """
    The browser tab names the household, not a hard-coded product line.

    Exercises: the page title used to carry a suffix naming the original employer's stock,
    which every user saw regardless of their holdings.
    """
    _path, html = rendered
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match, "no <title> in the rendered page"
    title = html_lib.unescape(match.group(1)).strip()
    assert cfg["household_name"] in title, title
    assert "Google" not in title and "GOOG" not in title, title


@pytest.mark.portfolio("p5_adversarial")
@pytest.mark.feature("render")
@pytest.mark.security
@pytest.mark.regression("SA-11")
def test_hostile_holding_names__are_escaped_in_the_page(rendered):
    """
    A holding whose name contains markup cannot inject an element or a handler.

    Exercises: the adversarial fixture holds a position named with a `<script>` payload and
    another that closes a JavaScript string. Neither may appear unescaped outside the JSON
    payload, which is itself embedded as a quoted string.
    """
    _path, html = rendered
    assert "<script>alert(2)</script>" not in html, "raw script element from a holding name"
    assert "<b>Bold</b>" not in html, "raw markup from a beneficiary name"

    # The payload is JSON inside a <script> block: quotes there are escaped by json.dumps,
    # and `</` is escaped so the block cannot be closed early.
    assert "</script>" not in pipeline_support.embedded_payload(html).get("household_name", "")


@pytest.mark.portfolio("p5_adversarial")
@pytest.mark.feature("render")
@pytest.mark.security
@pytest.mark.regression("SA-11")
def test_embedded_payload__cannot_break_out_of_its_script_block(rendered):
    """
    `</` inside the embedded JSON is escaped, so the script element cannot be closed early.

    Exercises: the renderer rewrites `</` to `<\\/` before embedding. Without that, a
    holding or scenario name containing `</script>` would end the block and everything
    after it would be parsed as markup.
    """
    _path, html = rendered
    raw_json = pipeline_support.embedded_payload_json(html)
    assert "</" not in raw_json, "unescaped `</` inside the embedded payload JSON"
    assert "<\\/" in raw_json or "</" not in raw_json


@pytest.mark.portfolio("p5_adversarial")
@pytest.mark.feature("render")
def test_awkward_household_name__survives_round_trip(rendered, cfg):
    """An apostrophe, an ampersand and a diacritic all survive into the payload."""
    _path, html = rendered
    embedded = pipeline_support.embedded_payload(html)
    assert embedded["household_name"] == cfg["household_name"]
    assert "'" in embedded["household_name"] and "&" in embedded["household_name"]


@pytest.mark.portfolio("p0_empty_household")
@pytest.mark.feature("empty_state")
def test_empty_household__renders_without_inventing_holdings(rendered, payload):
    """
    A portfolio with no statements renders an empty state, not a fabricated one.

    Exercises: the fresh-install path. Any holding, bucket or ticker on this page came
    from a default rather than from data.
    """
    _path, html = rendered
    assert payload.get("retirement_sim_assets") == []
    assert payload.get("holdings_by_account") == []
    assert "const PAYLOAD = " in html


@pytest.mark.parametrize("slug", ALL_SLUGS)
@pytest.mark.feature("render")
def test_rendered_page__payload_is_valid_json_and_matches_the_file(request, slug, tmp_path_factory):
    """The embedded payload parses and carries the fixture's pinned `as_of` date."""
    from tests.support import env

    target = tmp_path_factory.mktemp(f"root-json-{slug}")
    roots.assemble_root(target, [slug])
    with env.pointed_at(target):
        pipeline_support.clear_payload_cache()
        html = pipeline_support.rendered_html(slug)
        pipeline_support.clear_payload_cache()
    embedded = pipeline_support.embedded_payload(html)
    assert embedded["today_iso"] == roots.spec_as_of(slug)
    assert json.dumps(embedded)  # fully serialisable


@pytest.mark.portfolio("p4_trust_household")
@pytest.mark.feature("render")
def test_rendered_page__includes_collapsible_settings_sections(rendered):
    """Portfolio and Family Members settings render uniform collapsible rows and status capsules."""
    _path, html = rendered
    for row_id in (
        "proj-sec-identity",
        "proj-burn-box",
        "proj-tax-box",
        "proj-sec-graphic",
        "proj-statements-section",
        "proj-fam-sec-identity",
        "proj-fam-sec-work",
        "proj-fam-sec-ssn",
    ):
        assert row_id in html, f"missing collapsible settings section id {row_id}"
    assert "data-proj-collapse-toggle" in html
    assert "syncPortfolioCollapsibleHeaders" in html
    assert "syncFamilyCollapsibleHeaders" in html
