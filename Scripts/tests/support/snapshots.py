#!/usr/bin/env python3
"""
Allowlist-based snapshot assertions.

Numeric expectations live in `fixtures/expected/<slug>/*.json` rather than in test
bodies, so a deliberate model change is a reviewable diff instead of a scatter of edited
literals. Only paths an expectation file actually lists are compared, which keeps churn
out of unrelated changes.

Refresh with `pytest --update-snapshots` (or `make fixtures`, which regenerates them from
the specs) and review the diff like code.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from . import roots

UPDATE_ENV = "ADVISORBOT_UPDATE_SNAPSHOTS"
DEFAULT_REL_TOLERANCE = 1e-6


def updating() -> bool:
    return os.environ.get(UPDATE_ENV) == "1"


def snapshot_path(slug: str, name: str) -> Path:
    return roots.EXPECTED_DIR / slug / f"{name}.json"


def load(slug: str, name: str) -> dict[str, Any]:
    path = snapshot_path(slug, name)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing snapshot {slug}/{name}.json. Generate fixtures with:  make fixtures"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def save(slug: str, name: str, data: dict[str, Any]) -> Path:
    path = snapshot_path(slug, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def dig(obj: Any, path: str) -> Any:
    """
    Reads a dotted path out of nested dicts/lists.

    Segments are dict keys, list indices (`items.0.title`) or `key=value` list selectors
    (`accounts.id=HB-7001.institution`), which keep expectations readable when ordering is
    not the thing under test.
    """
    current = obj
    for raw_seg in path.split("."):
        if current is None:
            return None
        seg = raw_seg.strip()
        if isinstance(current, list):
            if seg.isdigit():
                idx = int(seg)
                current = current[idx] if 0 <= idx < len(current) else None
                continue
            if "=" in seg:
                key, _, want = seg.partition("=")
                match = next(
                    (row for row in current if isinstance(row, dict) and str(row.get(key)) == want),
                    None,
                )
                current = match
                continue
            return None
        if isinstance(current, dict):
            current = current.get(seg)
            continue
        return None
    return current


def _close(actual: Any, expected: Any, rel: float) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if isinstance(expected, bool) or isinstance(actual, bool):
            return bool(actual) == bool(expected)
        return math.isclose(float(actual), float(expected), rel_tol=rel, abs_tol=1e-9)
    return actual == expected


def compare(
    subject: Any,
    expected: dict[str, Any],
    *,
    rel: float = DEFAULT_REL_TOLERANCE,
    label: str = "snapshot",
) -> list[str]:
    """Returns a list of human-readable mismatches (empty when the snapshot holds)."""
    failures: list[str] = []
    for path, want in sorted(expected.items()):
        if path.startswith("_"):
            continue  # metadata (e.g. _generated_by, _spec_sha)
        got = dig(subject, path)
        if not _close(got, want, rel):
            failures.append(f"{label}: {path} expected {want!r}, got {got!r}")
    return failures


def assert_flat(
    computed: dict[str, Any],
    slug: str,
    name: str,
    *,
    rel: float = DEFAULT_REL_TOLERANCE,
) -> None:
    """
    Compares a freshly computed flat summary against the committed snapshot.

    Used for `payload_summary` / `sim_kpis`, whose keys are already dotted strings built by
    `tests.generate.expected`. Both sides come from the same builder, so a difference is a
    model change rather than a test/generator mismatch.
    """
    if updating():
        save(slug, name, computed)
        return
    expected = load(slug, name)
    failures: list[str] = []
    for key in sorted(set(expected) | set(computed)):
        if key.startswith("_"):
            continue
        if key not in expected:
            failures.append(f"{slug}/{name}: new key {key} = {computed[key]!r} (snapshot needs refresh)")
            continue
        if key not in computed:
            failures.append(f"{slug}/{name}: key {key} disappeared (was {expected[key]!r})")
            continue
        if not _close(computed[key], expected[key], rel):
            failures.append(f"{slug}/{name}: {key} expected {expected[key]!r}, got {computed[key]!r}")
    if failures:
        raise AssertionError(
            "\n".join(failures) + "\n\nIf this change is intended: make fixtures (or pytest --update-snapshots)"
        )


def assert_snapshot(
    subject: Any,
    slug: str,
    name: str,
    *,
    rel: float = DEFAULT_REL_TOLERANCE,
    capture: dict[str, Any] | None = None,
) -> None:
    """
    Asserts `subject` matches the committed snapshot.

    Under `--update-snapshots`, writes `capture` (or the current values at the snapshot's
    existing paths) instead of asserting.
    """
    if updating():
        if capture is not None:
            save(slug, name, capture)
            return
        try:
            existing = load(slug, name)
        except FileNotFoundError:
            return
        refreshed = {
            path: (existing[path] if path.startswith("_") else dig(subject, path))
            for path in existing
        }
        save(slug, name, refreshed)
        return
    failures = compare(subject, load(slug, name), rel=rel, label=f"{slug}/{name}")
    if failures:
        raise AssertionError("\n".join(failures))
