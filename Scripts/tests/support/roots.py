#!/usr/bin/env python3
"""
Fixture-portfolio registry and temporary-root assembly.

A fixture portfolio is a complete AdvisorBot portfolio folder under
`Scripts/tests/fixtures/portfolios/<slug>/`, generated from its spec by
`python3 -m tests.generate` and committed. `assemble_root()` copies one or more of them
into a throwaway root so the real CLI, the sidecar server and the renderer can all open
them unchanged.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = TESTS_DIR / "fixtures"
SPECS_DIR = FIXTURES_DIR / "specs"
PORTFOLIOS_DIR = FIXTURES_DIR / "portfolios"
EXPECTED_DIR = FIXTURES_DIR / "expected"
PARSERS_DIR = FIXTURES_DIR / "parsers"
DENYLIST_DIR = FIXTURES_DIR / "denylist"

# Fixture slugs, in the order the generator and the documentation present them.
FIXTURE_SLUGS: tuple[str, ...] = (
    "p0_empty_household",
    "p1_index_saver",
    "p2_equity_comp_rsu",
    "p3_retiree_annuities",
    "p4_trust_household",
    "p5_adversarial",
)

DEFAULT_SLUG = "p0_empty_household"


def spec_path(slug: str) -> Path:
    return SPECS_DIR / f"{slug}.json"


def load_spec(slug: str) -> dict:
    """Loads a fixture portfolio spec (JSON; see `fixtures/specs/SCHEMA.md`)."""
    path = spec_path(slug)
    if not path.is_file():
        raise FileNotFoundError(f"No spec for fixture portfolio {slug!r} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def all_specs() -> list[dict]:
    return [load_spec(slug) for slug in FIXTURE_SLUGS if spec_path(slug).is_file()]


def fixture_dir(slug: str) -> Path:
    path = PORTFOLIOS_DIR / slug
    if not path.is_dir():
        raise FileNotFoundError(
            f"Fixture portfolio {slug!r} has not been generated. Run:  make fixtures"
        )
    return path


def spec_as_of(slug: str) -> str:
    """The pinned 'today' for a fixture, so payload and simulator output are deterministic."""
    return str(load_spec(slug).get("as_of") or "2026-11-16")


def assemble_root(root: Path, portfolios: list[str] | tuple[str, ...] | None = None) -> Path:
    """
    Builds a temporary AdvisorBot root containing the named fixture portfolios.

    The first slug becomes the active portfolio. `Data/` is created empty so the session
    token, logs and LLM config directory all land inside the throwaway root.
    """
    slugs = list(portfolios or [DEFAULT_SLUG])
    root = Path(root)
    (root / "Portfolios").mkdir(parents=True, exist_ok=True)
    (root / "Data" / "Config" / "LLM").mkdir(parents=True, exist_ok=True)
    from . import env as _env_mod
    cohorts_src = _env_mod.app_checkout_dir() / "Data" / "historical_cohorts.json"
    if cohorts_src.is_file():
        shutil.copyfile(cohorts_src, root / "Data" / "historical_cohorts.json")
    for slug in slugs:
        src = fixture_dir(slug)
        dst = root / "Portfolios" / slug
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        # Statement mtimes decide incremental-scan behaviour; normalise so a freshly
        # copied fixture always looks new to the scanner.
        for path in (dst / "Statements").rglob("*"):
            if path.is_file():
                path.touch()
    (root / "Portfolios" / "_active.json").write_text(
        json.dumps({"active_portfolio_id": slugs[0]}, indent=2) + "\n", encoding="utf-8"
    )
    return root


def is_fixture_root(root: Path) -> bool:
    """True when every portfolio in `root` is a known fixture slug (the isolation guard)."""
    pdir = Path(root) / "Portfolios"
    if not pdir.is_dir():
        return True
    for child in pdir.iterdir():
        if child.is_dir() and not child.name.startswith((".", "_")) and child.name not in FIXTURE_SLUGS:
            return False
    return True


def denylist_terms() -> list[str]:
    """Legacy product/institution vocabulary that must never appear in generated output."""
    path = DENYLIST_DIR / "legacy_terms.txt"
    return _read_term_file(path)


def local_household_terms() -> list[str]:
    """
    Optional machine-local terms (`Data/.household_terms`, gitignored).

    Lets a developer deny their own household's names without committing them. CI has no
    such file, so the committed `legacy_terms.txt` is the baseline everywhere.
    """
    from . import env

    return _read_term_file(env.app_checkout_dir() / "Data" / ".household_terms")


def _read_term_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.split("#", 1)[0].strip()
        if term:
            out.append(term)
    return out
