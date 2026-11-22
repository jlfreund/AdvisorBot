#!/usr/bin/env python3
"""
Text scanners shared by the `scans` tier.

Three jobs:

  * **Portability.** No fixture portfolio uses the vocabulary the application was
    originally built against, so any occurrence of a denylisted term in rendered output,
    an LLM wire payload or a generated artefact is a literal leaking out of the code
    rather than data. This is what turns the fixtures into a regression harness for the
    legacy-logic findings (`LL-*` in `.dev/SecurityAudit.html`).
  * **PII shape.** Account-number shapes, cents-precise dollar amounts in test bodies and
    absolute home-directory paths should never be committed.
  * **Fallback audit.** A static check that no new `or "<legacy literal>"` fallback lands.
    Current offenders are listed in an allowlist that shrinks as fixes ship, so the set
    can only get smaller.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from . import roots

# Account-number / contract shapes and absolute home paths.
#
# The 4-4 shape also matches a year range ("2012-2026"), which fixture READMEs legitimately
# print for an earnings history, so year-like pairs are filtered out after matching.
PII_PATTERNS: tuple[tuple[str, str], ...] = (
    ("account_number_4_4", r"\b\d{4}-\d{4}\b"),
    ("account_number_3_6", r"\b\d{3}-\d{6}\b"),
    ("absolute_home_path", r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
)

_YEAR_RANGE_RE = re.compile(r"^(19|20)\d{2}-(19|20)\d{2}$")


def _is_year_range(text: str) -> bool:
    return bool(_YEAR_RANGE_RE.match(text))

# Dollar amounts written to the cent, which in a test body almost always means a real
# statement value was pasted in.
CENTS_PATTERN = r"\$\s?\d{1,3}(?:,\d{3})+\.\d{2}\b"

_WORD_SAFE = re.compile(r"^[A-Za-z0-9 .&'/-]+$")


def _term_regex(term: str) -> re.Pattern[str]:
    """Word-boundary match for alphanumeric terms, plain substring for anything else."""
    if _WORD_SAFE.match(term):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Returns the denylisted terms present in `text`, in denylist order, de-duplicated."""
    hits: list[str] = []
    for term in terms:
        if _term_regex(term).search(text):
            hits.append(term)
    return hits


def legacy_terms() -> list[str]:
    """Committed legacy vocabulary plus any machine-local household terms."""
    return [*roots.denylist_terms(), *roots.local_household_terms()]


def scan_text_for_legacy(text: str) -> list[str]:
    return find_terms(text, legacy_terms())


def find_pii(text: str) -> list[tuple[str, str]]:
    """Returns `(pattern_name, matched_text)` for every PII-shaped match."""
    out: list[tuple[str, str]] = []
    for name, pattern in PII_PATTERNS:
        for match in re.finditer(pattern, text):
            value = match.group(0)
            if name == "account_number_4_4" and _is_year_range(value):
                continue
            out.append((name, value))
    return out


def pii_allowlist() -> set[str]:
    """
    Files allowed to contain account-number-shaped strings, with a recorded reason.

    Entries are repo-relative paths under `Scripts/tests/`. The list exists so ported
    legacy fixtures do not block the scan while they are being reshaped; it should shrink
    to nothing.
    """
    from . import roots

    return set(roots._read_term_file(roots.DENYLIST_DIR / "pii_allowlist.txt"))


def iter_source_files(
    base: Path,
    suffixes: tuple[str, ...] = (".py", ".json", ".txt", ".md", ".mjs", ".js", ".html"),
    *,
    skip_dirs: tuple[str, ...] = ("__pycache__", ".pytest_cache", "node_modules", ".git"),
) -> list[Path]:
    out: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        out.append(path)
    return out


def fallback_patterns() -> list[str]:
    path = roots.DENYLIST_DIR / "fallback_patterns.txt"
    return roots._read_term_file(path)


def fallback_allowlist() -> set[str]:
    """
    Known-open literal fallbacks, as `<relpath>:<line>:<pattern-id>` entries.

    Each line is a finding still to be fixed. Delete the line when the fix lands; the test
    fails if an entry no longer matches, so the allowlist cannot rot.
    """
    path = roots.DENYLIST_DIR / "fallback_allowlist.txt"
    return set(roots._read_term_file(path))


def scan_fallbacks(app_dir: Path) -> list[str]:
    """
    Finds literal-fallback shapes in application source.

    Returns `<relpath>:<line>:<pattern-id>` keys so results can be diffed against the
    allowlist without depending on surrounding code.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for raw in fallback_patterns():
        pattern_id, _, regex = raw.partition("=")
        if not regex:
            continue
        patterns.append((pattern_id.strip(), re.compile(regex.strip())))
    hits: list[str] = []
    scan_root = app_dir / "Scripts"
    for path in iter_source_files(scan_root, suffixes=(".py", ".js", ".html", ".sql")):
        if "tests" in path.relative_to(app_dir).parts:
            continue
        rel = path.relative_to(app_dir).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for pattern_id, regex in patterns:
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}:{pattern_id}")
    return hits
