#!/usr/bin/env python3
"""
Refreshes the shrink-only baselines after a round of fixes.

    PYTHONPATH=Scripts python3 -m tests.refresh_baselines

Rewrites `fixtures/denylist/expected_leaks.json` and
`fixtures/denylist/fallback_allowlist.txt` from the current state of the code.

Run this *after* fixing a finding, and read the diff: every line it removes is a leak that
is now closed. It should only ever shrink the files. If it adds a line, something
introduced a new literal and the diff is the review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
for _p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.support import env, roots, scans  # noqa: E402

LEAKS_PATH = roots.DENYLIST_DIR / "expected_leaks.json"
FALLBACKS_PATH = roots.DENYLIST_DIR / "fallback_allowlist.txt"


def refresh_leaks() -> tuple[int, int]:
    """Rewrites the per-fixture rendered-leak baseline from the generated snapshots."""
    data = json.loads(LEAKS_PATH.read_text(encoding="utf-8"))
    before = sum(len(v) for v in (data.get("portfolios") or {}).values())
    portfolios: dict[str, list[str]] = {}
    for slug in roots.FIXTURE_SLUGS:
        scan_path = roots.EXPECTED_DIR / slug / "rendered_scan.json"
        if not scan_path.is_file():
            raise SystemExit(f"{slug}: no rendered_scan.json; run `make fixtures` first")
        portfolios[slug] = sorted(json.loads(scan_path.read_text(encoding="utf-8"))["legacy_term_hits"])
    data["portfolios"] = portfolios
    LEAKS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return before, sum(len(v) for v in portfolios.values())


def refresh_fallbacks() -> tuple[int, int]:
    """Rewrites the literal-fallback allowlist, preserving the header comment."""
    existing = FALLBACKS_PATH.read_text(encoding="utf-8").splitlines() if FALLBACKS_PATH.is_file() else []
    header = [line for line in existing if line.startswith("#") or not line.strip()]
    before = len([line for line in existing if line.strip() and not line.startswith("#")])
    hits = sorted(scans.scan_fallbacks(env.app_checkout_dir()))
    FALLBACKS_PATH.write_text("\n".join([*header, *hits]) + "\n", encoding="utf-8")
    return before, len(hits)


def main() -> int:
    leak_before, leak_after = refresh_leaks()
    fb_before, fb_after = refresh_fallbacks()
    print(f"  [baseline] rendered legacy-term leaks: {leak_before} -> {leak_after}")
    print(f"  [baseline] literal fallbacks:          {fb_before} -> {fb_after}")
    if leak_after > leak_before or fb_after > fb_before:
        print("  [baseline] WARNING: a baseline grew. Review the diff before committing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
