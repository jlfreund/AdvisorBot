#!/usr/bin/env python3
"""
Root isolation, guards and shared fixtures for the AdvisorBot test suite.

Three guarantees, in order of importance:

  1. **No test reads the user's portfolio.** `pytest_configure` points
     `ADVISORBOT_ROOT` at a throwaway scratch root before any application module is
     imported, and an autouse guard fails any test whose active portfolio is
     not a committed fixture slug. A stray `ADVISORBOT_ROOT` in a developer's shell
     cannot aim the suite at real data.
  2. **No test reaches the network.** Market-data fetches and the provider HTTP helper
     raise unless the test is marked `network`. Fixture portfolios ship their own price
     history.
  3. **Determinism.** Payloads are built at each fixture's pinned `as_of` date, so
     snapshots do not drift with the wall clock.

See `Scripts/tests/README.md` for the tier table and the marker vocabulary.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
CHECKOUT_DIR = SCRIPTS_DIR.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(CHECKOUT_DIR) not in sys.path:
    sys.path.insert(0, str(CHECKOUT_DIR))

from tests.support import env, pipeline as pipeline_support, registry, roots  # noqa: E402

_SESSION_SCRATCH: Path | None = None


# --------------------------------------------------------------------------- options


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Rewrite fixtures/expected/ snapshots from the current code instead of asserting.",
    )
    parser.addoption(
        "--keep-roots",
        action="store_true",
        default=False,
        help="Keep temporary fixture roots on disk after the run (for debugging).",
    )


# --------------------------------------------------------------------------- configure


def pytest_configure(config: Any) -> None:
    """Points the process at a scratch root *before* any application import happens."""
    global _SESSION_SCRATCH
    _SESSION_SCRATCH = Path(tempfile.mkdtemp(prefix="advisorbot-tests-"))
    bootstrap = _SESSION_SCRATCH / "bootstrap-root"
    (bootstrap / "Portfolios").mkdir(parents=True, exist_ok=True)
    (bootstrap / "Data").mkdir(parents=True, exist_ok=True)
    (bootstrap / "Portfolios" / "_active.json").write_text(
        json.dumps({"active_portfolio_id": None}, indent=2) + "\n", encoding="utf-8"
    )
    os.environ["ADVISORBOT_ROOT"] = str(bootstrap)
    for stale in ("HOLDINGS_ROOT", "PROJECT_DIR", "PORTFOLIO_DIR", "PORTFOLIOS_DIR", "HOLDINGS_DATA_DIR", "LLM_DIR"):
        os.environ.pop(stale, None)
    if config.getoption("--update-snapshots"):
        os.environ["ADVISORBOT_UPDATE_SNAPSHOTS"] = "1"

    for tier in registry.TIERS:
        config.addinivalue_line("markers", f"{tier}: {tier} tier")
    for name, desc in (
        ("security", "regression test for a security/privacy finding"),
        ("slow", "takes more than a few seconds"),
        ("network", "needs real outbound network access"),
        ("legacy", "ported from the pre-fixture suite, kept for coverage"),
        ("portfolio", "portfolio(*slugs): fixture portfolios this test needs"),
        ("regression", "regression(finding_id): links the test to a review finding"),
        ("feature", "feature(key): product area this test covers"),
    ):
        config.addinivalue_line("markers", f"{name}: {desc}")


def pytest_unconfigure(config: Any) -> None:
    if _SESSION_SCRATCH and _SESSION_SCRATCH.exists() and not config.getoption("--keep-roots"):
        shutil.rmtree(_SESSION_SCRATCH, ignore_errors=True)


# Tests ported from the pre-fixture suite whose assertions encode the *original*
# household's data (its solvency, its carriers' prose, its account shapes) and therefore
# cannot pass against a synthetic fixture without being rewritten.
#
# Each entry names the node and why it is parked. The equivalent coverage, where it
# matters, is provided by the tier tests listed in the reason. This registry is the
# rewrite backlog: a parked test that starts passing fails the audit below, and the entry
# should then be deleted.
# All legacy tests ported from the pre-fixture suite have been re-wired against synthetic
# fixture portfolios (`p0_empty_household`–`p5_adversarial`) and isolated temp roots (TS-08).
LEGACY_PENDING: dict[str, str] = {}


def _pending_reason(nodeid: str) -> str | None:
    """Matches a node id against `LEGACY_PENDING`, by exact node or by module."""
    rel = nodeid.replace("Scripts/tests/", "", 1)
    if rel in LEGACY_PENDING:
        return LEGACY_PENDING[rel]
    module = rel.split("::", 1)[0]
    return LEGACY_PENDING.get(module)


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Auto-marks each test with its tier directory and validates marker vocabularies."""
    errors: list[str] = []
    for item in items:
        rel = Path(str(item.fspath)).resolve()
        try:
            parts = rel.relative_to(TESTS_DIR).parts
        except ValueError:
            parts = ()
        if parts and parts[0] in registry.TIERS:
            item.add_marker(getattr(pytest.mark, parts[0]))
        reason = _pending_reason(item.nodeid)
        if reason is not None:
            item.add_marker(pytest.mark.legacy)
            item.add_marker(
                pytest.mark.skip(reason=f"legacy test pending rewrite: {reason}")
            )
        for marker in item.iter_markers(name="regression"):
            for value in marker.args:
                try:
                    registry.validate_finding(str(value))
                except ValueError as exc:
                    errors.append(f"{item.nodeid}: {exc}")
        for marker in item.iter_markers(name="feature"):
            for value in marker.args:
                try:
                    registry.validate_feature(str(value))
                except ValueError as exc:
                    errors.append(f"{item.nodeid}: {exc}")
        for marker in item.iter_markers(name="portfolio"):
            for value in marker.args:
                if str(value) not in roots.FIXTURE_SLUGS:
                    errors.append(f"{item.nodeid}: unknown fixture portfolio {value!r}")
    if errors:
        raise pytest.UsageError("\n".join(errors))


# --------------------------------------------------------------------------- guards


@pytest.fixture(autouse=True)
def _no_live_portfolio(request: Any) -> Iterator[None]:
    """
    Refuses to run any test against anything but fixture portfolios.

    This is the backstop for requirement (A): even if a root is mis-assembled or an
    environment variable leaks in, a test cannot silently read the owner's data.
    """
    yield
    import config

    root = Path(config.HOLDINGS_ROOT)
    if not roots.is_fixture_root(root):
        pytest.fail(
            f"Refusing to run test against non-fixture root {root}. "
            f"Portfolios present are not all known fixture slugs.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _no_network(request: Any, monkeypatch: Any) -> None:
    """Blocks outbound HTTP unless the test is marked `network`."""
    if request.node.get_closest_marker("network"):
        return

    def _blocked(*args: Any, **kwargs: Any):
        raise AssertionError(
            "Outbound network access attempted in a test. Mark the test `@pytest.mark.network` "
            "or seed the fixture's market_prices.json instead."
        )

    import urllib.request

    real_urlopen = urllib.request.urlopen

    def _loopback_only(req: Any, *args: Any, **kwargs: Any):
        """Allows the sidecar's own loopback traffic; blocks everything external."""
        url = req if isinstance(req, str) else getattr(req, "full_url", "")
        host = str(url).split("//", 1)[-1].split("/", 1)[0].split("@")[-1]
        if host.startswith(("127.0.0.1", "localhost", "[::1]")):
            return real_urlopen(req, *args, **kwargs)
        return _blocked(req)

    monkeypatch.setattr(urllib.request, "urlopen", _loopback_only)
    for mod_name, attr in (
        ("valuation.market_data", "_fetch_yahoo_chart"),
        ("valuation.market_data", "fetch_quote_snapshot"),
        ("llm_tools", "_http_post_json"),
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, _blocked)


# --------------------------------------------------------------------------- roots


def _requested_portfolios(request: Any) -> list[str]:
    slugs: list[str] = []
    for marker in request.node.iter_markers(name="portfolio"):
        for value in marker.args:
            if str(value) not in slugs:
                slugs.append(str(value))
    return slugs or [roots.DEFAULT_SLUG]


@pytest.fixture
def root(request: Any, tmp_path_factory: Any) -> Iterator[Path]:
    """
    A temporary AdvisorBot root containing the fixture portfolios this test asked for.

    Select them with `@pytest.mark.portfolio("p2_equity_comp_rsu")`; the first is active.
    The application is pointed at the root for the duration of the test and restored
    afterwards, caches cleared both ways.
    """
    slugs = _requested_portfolios(request)
    target = tmp_path_factory.mktemp("root")
    roots.assemble_root(target, slugs)
    with env.pointed_at(target) as resolved:
        pipeline_support.clear_payload_cache()
        yield resolved
    pipeline_support.clear_payload_cache()


@pytest.fixture
def slug(request: Any) -> str:
    """The active fixture slug for this test."""
    return _requested_portfolios(request)[0]


@pytest.fixture
def cfg(root: Path, slug: str) -> dict[str, Any]:
    """The active fixture portfolio's `project.json`, as the application loads it."""
    return pipeline_support.load_cfg(slug)


@pytest.fixture
def payload(root: Path, slug: str) -> dict[str, Any]:
    """
    The view-model payload for the active fixture portfolio.

    Ingests the fixture's statements, then builds at the spec's pinned `as_of` date.
    Shared by reference within a test; deep-copy via `support.pipeline.payload_copy`
    before mutating.
    """
    return pipeline_support.build_payload(slug)


@pytest.fixture
def rendered(root: Path, slug: str) -> tuple[Path, str]:
    """`(path, html)` for the rendered dashboard of the active fixture portfolio."""
    path, _ = pipeline_support.render_fixture(slug)
    return path, path.read_text(encoding="utf-8")


@pytest.fixture
def db(root: Path, slug: str) -> Iterator[Any]:
    """An open `PortfolioDB` for the active fixture, after ingest."""
    pipeline_support.ingest_fixture(slug)
    handle = pipeline_support.open_db(slug)
    try:
        yield handle
    finally:
        handle.conn.close()


@pytest.fixture
def sidecar(root: Path) -> Iterator[Any]:
    """A running sidecar server bound to a free loopback port, plus an HTTP client."""
    from tests.support import server as server_support

    with server_support.running(root) as client:
        yield client
