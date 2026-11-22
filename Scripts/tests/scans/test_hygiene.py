#!/usr/bin/env python3
"""
Hygiene scans over the test tree and the shipped artefacts.

Three things that should never be committed, and one that should never be stale:
PII-shaped strings, absolute home-directory paths, cents-precise dollar amounts pasted
into a test body, and a fixture tree that no longer matches its specs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support import env, roots, scans

TEST_TREE = roots.TESTS_DIR

# `fixtures/parsers/` holds institution statement *formats*, which necessarily contain the
# institution's own name and a plausible account number: that is what the parser detects
# on. It is the one directory exempt from the vocabulary and account-number scans.
PARSER_FIXTURE_DIR = roots.PARSERS_DIR

LEGACY_MARKER = "pytestmark = _pytest_legacy.mark.legacy"


def _is_legacy_file(path) -> bool:
    """
    True for a test file ported from the pre-fixture suite.

    These scans enforce the conventions on *new* test code. Ported files carry synthetic
    but PII-shaped inputs (they test redaction), inline dollar literals and unittest
    methods without docstrings; they are tracked for rewrite by `LEGACY_PENDING` in
    `conftest.py` rather than by these scans.
    """
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return False
    try:
        return LEGACY_MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _test_tree_files():
    return [
        path
        for path in scans.iter_source_files(TEST_TREE)
        if PARSER_FIXTURE_DIR not in path.parents and not _is_legacy_file(path)
    ]


def _new_test_py_files():
    return [
        path
        for path in sorted(TEST_TREE.rglob("*.py"))
        if "__pycache__" not in path.parts and not _is_legacy_file(path)
    ]


@pytest.mark.feature("payload")
def test_test_tree__contains_no_account_number_shapes():
    """
    No committed test file carries a real-looking account or contract number.

    Exercises: the old suite asserted the owner's account suffixes directly, which put
    them in version control. Fixture account ids are deliberately shaped differently
    (`HB-7001`), so this pattern should find nothing.
    """
    allowlist = scans.pii_allowlist()
    offenders: list[str] = []
    stale_allowlist = set(allowlist)
    for path in _test_tree_files():
        rel = path.relative_to(TEST_TREE).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [m for name, m in scans.find_pii(text) if name.startswith("account_number")]
        if not hits:
            continue
        if rel in allowlist:
            stale_allowlist.discard(rel)
            continue
        offenders.extend(f"{rel}: {hit}" for hit in sorted(set(hits)))
    assert not offenders, (
        "account-number shapes in the test tree:\n"
        + "\n".join(offenders[:20])
        + "\n\nReshape them to the fixture convention (e.g. HB-7001) or record the file in "
        "fixtures/denylist/pii_allowlist.txt with a reason."
    )
    assert not stale_allowlist, (
        "pii_allowlist.txt lists files that are now clean; delete these entries: "
        + ", ".join(sorted(stale_allowlist))
    )


@pytest.mark.feature("payload")
def test_test_tree__contains_no_absolute_home_paths():
    """A committed path under /Users or /home names a machine and usually a person."""
    offenders: list[str] = []
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, match in scans.find_pii(text):
            if name == "absolute_home_path":
                offenders.append(f"{path.relative_to(TEST_TREE)}: {match}")
    assert not offenders, "absolute home paths in the test tree:\n" + "\n".join(offenders[:20])


@pytest.mark.feature("payload")
def test_test_code__has_no_cents_precise_dollar_literals():
    """
    A dollar amount written to the cent in a test body is almost always pasted real data.

    Exercises: expectations belong in `fixtures/expected/`, which is regenerated from the
    specs; a literal in a test body cannot be regenerated and tends to be copied from
    whatever portfolio the author had open.
    """
    offenders: list[str] = []
    for path in _new_test_py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(scans.CENTS_PATTERN, line):
                offenders.append(f"{path.relative_to(TEST_TREE)}:{lineno}: {line.strip()[:90]}")
    assert not offenders, "cents-precise dollar literals in test code:\n" + "\n".join(offenders[:20])


@pytest.mark.feature("payload")
def test_fixture_tree__matches_its_specs():
    """
    Regenerating every fixture reproduces exactly what is committed.

    Exercises: determinism of the generator, and that nobody has hand-edited a generated
    fixture. A failure lists the files that differ.
    """
    from tests.generate import __main__ as generate_main

    problems = generate_main.check_drift(list(roots.FIXTURE_SLUGS))
    assert not problems, (
        "Committed fixtures differ from their specs:\n"
        + "\n".join(f"  {p}" for p in problems[:30])
        + "\n\nRun `make fixtures` and review the diff."
    )


@pytest.mark.feature("payload")
def test_every_fixture__has_a_generated_readme_that_names_its_spec():
    """Each fixture README is generated and stamped with the spec hash it came from."""
    from tests.generate import spec as spec_mod

    for slug in roots.FIXTURE_SLUGS:
        readme = (roots.PORTFOLIOS_DIR / slug / "README.md").read_text(encoding="utf-8")
        assert "GENERATED FILE" in readme, f"{slug}: README is not marked generated"
        sha = spec_mod.spec_sha(roots.load_spec(slug))
        assert sha in readme, f"{slug}: README records a different spec hash (run `make fixtures`)"


@pytest.mark.feature("payload")
def test_every_test__has_a_first_line_docstring(request):
    """
    Every test documents itself in one sentence.

    Exercises: the docstring contract. `CASES.md` is generated from these, so a test with
    no docstring would appear in the catalogue as a blank row.
    """
    legacy_files = {p.resolve() for p in TEST_TREE.rglob("*.py") if _is_legacy_file(p)}
    missing: list[str] = []
    for item in request.session.items:
        func = getattr(item, "function", None)
        if func is None:
            continue
        if Path(str(item.fspath)).resolve() in legacy_files:
            continue
        doc = (func.__doc__ or "").strip()
        if not doc:
            missing.append(item.nodeid)
    assert not missing, "tests without a docstring:\n" + "\n".join(missing[:20])


@pytest.mark.feature("payload")
def test_spec_schema_doc__documents_every_validated_key():
    """
    `fixtures/specs/SCHEMA.md` lists every key the validator accepts.

    Exercises: keeps the human-facing schema reference honest. A key added to the
    validator without a line in the document fails here.
    """
    from tests.generate import spec as spec_mod

    doc_path = roots.SPECS_DIR / "SCHEMA.md"
    if not doc_path.is_file():
        pytest.skip("SCHEMA.md has not been written yet")
    doc = doc_path.read_text(encoding="utf-8")
    documented_sets = (
        spec_mod.TOP_KEYS,
        spec_mod.HOUSEHOLD_KEYS,
        spec_mod.INVESTOR_KEYS,
        spec_mod.ACCOUNT_KEYS,
        spec_mod.POSITION_KEYS,
        spec_mod.GRANTS_KEYS,
        spec_mod.SCENARIO_KEYS,
        spec_mod.PRIVACY_KEYS,
        spec_mod.INVARIANT_KEYS,
    )
    missing = sorted({key for keys in documented_sets for key in keys if f"`{key}`" not in doc})
    assert not missing, f"undocumented spec keys in SCHEMA.md: {missing}"


@pytest.mark.feature("payload")
def test_no_test_reads_the_real_portfolios_directory():
    """
    No test file refers to the user's portfolio folder.

    Exercises: requirement (A) at the source level, as a complement to the runtime guard.
    A test that hard-coded a path into `Portfolios/` would bypass the fixture roots.
    """
    offenders: list[str] = []
    for path in _new_test_py_files():
        rel = path.relative_to(TEST_TREE)
        if rel.parts[0] in ("support", "generate"):
            continue  # the harness itself resolves roots deliberately
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["'][^"']*Portfolios/""", line):
                offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")
    assert not offenders, "tests referring to Portfolios/ directly:\n" + "\n".join(offenders[:20])


@pytest.mark.feature("payload")
def test_application_and_docs__contain_no_pii_or_home_paths():
    """
    No application source file under `Scripts/` (outside `tests/`) or `Docs/` carries
    an account-number shape or an absolute `/Users/...` or `/home/...` path (SA2-24d).
    """
    app_dir = env.app_checkout_dir()
    offenders: list[str] = []
    for sub in ("Scripts", "Docs"):
        base = app_dir / sub
        if not base.is_dir():
            continue
        for path in scans.iter_source_files(base):
            rel = path.relative_to(app_dir).as_posix()
            if rel.startswith("Scripts/tests/"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for kind, match in scans.find_pii(text):
                offenders.append(f"{rel} ({kind}): {match}")
    assert not offenders, "PII or home-directory paths in application/docs:\n" + "\n".join(offenders[:20])


@pytest.mark.feature("payload")
def test_checkout__has_no_index_html_written_by_the_suite():
    """
    Running the suite leaves no rendered dashboard in the checkout.

    Exercises: renders go to the fixture root's own `index.html`. If this fails, something
    resolved the output path against the real root and may have overwritten the user's page.
    """
    import config

    assert config.HOLDINGS_ROOT != env.app_checkout_dir(), (
        "the suite is pointed at the checkout rather than a fixture root"
    )
