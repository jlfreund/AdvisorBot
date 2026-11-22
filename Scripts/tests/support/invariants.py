#!/usr/bin/env python3
"""
Executable fixture invariants.

Each spec declares what must be true of its portfolio once the pipeline has run, so a
fixture README's claims are checked rather than asserted in prose. An invariant may be
marked with a `known_gap` finding id, which means: this is the behaviour the application
*should* have, and the named finding is why it does not yet.

Known gaps are reported as expected failures, and the moment one starts passing the test
fails with instructions to delete the marker. That makes the gap list a backlog that can
only shrink, in the same spirit as the portability leak baseline.
"""

from __future__ import annotations

from typing import Any

from . import snapshots


def _resolve(invariant: dict[str, Any], payload: dict[str, Any], summary: dict[str, Any]) -> Any:
    source = str(invariant.get("source") or "payload")
    path = str(invariant["path"])
    if source == "payload_summary":
        # Summary keys are already flat dotted strings.
        return summary.get(path)
    return snapshots.dig(payload, path)


def _holds(invariant: dict[str, Any], got: Any) -> bool:
    if "equals" in invariant:
        want = invariant["equals"]
        if isinstance(want, float) and isinstance(got, (int, float)):
            return abs(float(got) - want) <= max(1e-9, abs(want) * 1e-6)
        return got == want
    if "contains" in invariant:
        return str(invariant["contains"]) in str(got or "")
    if invariant.get("absent"):
        return got in (None, [], {}, "")
    return got not in (None, "")


def _describe(invariant: dict[str, Any], got: Any) -> str:
    path = invariant["path"]
    if "equals" in invariant:
        return f"{path}: expected {invariant['equals']!r}, got {got!r}"
    if "contains" in invariant:
        return f"{path}: expected to contain {invariant['contains']!r}, got {got!r}"
    if invariant.get("absent"):
        return f"{path}: expected absent, got {got!r}"
    return f"{path}: expected a value, got {got!r}"


def check(
    spec: dict[str, Any],
    payload: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """
    Evaluates every invariant in `spec`.

    Returns `(failures, known_gaps, fixed_gaps)`:
      * `failures`    invariants that should hold and do not (a real regression)
      * `known_gaps`  invariants that fail for a recorded open finding
      * `fixed_gaps`  invariants marked as gaps that now hold, so the marker is stale
    """
    failures: list[str] = []
    known_gaps: list[str] = []
    fixed_gaps: list[str] = []
    for invariant in spec.get("invariants") or []:
        got = _resolve(invariant, payload, summary)
        ok = _holds(invariant, got)
        gap = invariant.get("known_gap")
        if ok and gap:
            fixed_gaps.append(f"  {invariant['path']} (marked {gap}) now holds: {got!r}")
        elif not ok and gap:
            known_gaps.append(f"{gap} ({invariant['path']})")
        elif not ok:
            failures.append(_describe(invariant, got))
    return failures, known_gaps, fixed_gaps
