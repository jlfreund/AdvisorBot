#!/usr/bin/env python3
"""
Validated vocabularies for test markers.

`@pytest.mark.regression("SA-11")` and `@pytest.mark.feature("rmd")` are free text, which
rots silently. These registries are checked in `conftest.py` at collection time so a typo
fails loudly, and `CASES.md` can group by a known set.

Finding ids match the sections of `.dev/SecurityAudit.html`:
  * `S*` / `I*`  the Oct 31 - Nov 11 engineering review
  * `SA-*` / `DOC-*`  the Nov 15 privacy & security audit
  * `LL-*`  the Nov 16 legacy portfolio-specific logic review
"""

from __future__ import annotations

# Security / engineering review findings that tests may claim to cover.
KNOWN_FINDINGS: frozenset[str] = frozenset(
    {
        # Oct 31 - Nov 11 engineering review (security subset)
        "S1", "S2", "S3", "S4", "S5", "S6", "I1", "I4",
        # Nov 15 privacy & security audit
        *(f"SA-{n:02d}" for n in range(1, 28)),
        # Nov 15 documentation accuracy
        *(f"DOC-{n:02d}" for n in range(1, 13)),
        # Nov 16 legacy portfolio-specific logic review
        *(f"LL-{n:02d}" for n in range(0, 34)),
        # Nov 16 (Pass 2) / Nov 17 full security, privacy, Demo Mode & portability audit
        *(f"SA2-{n:02d}" for n in range(1, 25)),
        *(f"DP-{n:02d}" for n in range(1, 6)),
        *(f"DA3-{n:02d}" for n in range(1, 7)),
        *(f"SA4-{n:02d}" for n in range(1, 7)),
        # Nov 17 Pass-5 security audit & Pass-2 simulation/design audit
        *(f"SA3-{n:02d}" for n in range(1, 15)),
        *(f"DA2-{n:02d}" for n in range(1, 20)),
        # Nov 20 Pass-6 security audit & Pass-3 post-V1.0 simulation/UI audit
        *(f"SA5-{n:02d}" for n in range(1, 11)),
        *(f"DA4-{n:02d}" for n in range(1, 15)),
    }
)

# Feature keys, used to group the generated case catalogue by product area.
KNOWN_FEATURES: frozenset[str] = frozenset(
    {
        "aliasing",
        "annuities",
        "anchors",
        "asset_classes",
        "cash_interest",
        "cohorts",
        "demo_mode",
        "dedupe",
        "empty_state",
        "grants",
        "ingest",
        "monte_carlo",
        "munis",
        "payload",
        "plugins",
        "privacy_shield",
        "render",
        "rmd",
        "scenarios",
        "server_auth",
        "simulator",
        "social_security",
        "solver",
        "tax_models",
        "wrappers",
    }
)

# Tier markers. Every test file lives in the directory matching its tier.
TIERS: tuple[str, ...] = (
    "unit",
    "parsers",
    "pipeline",
    "engine",
    "parity",
    "render",
    "server",
    "llm",
    "scans",
)

# Cross-cutting markers that combine with a tier.
CROSS_MARKERS: tuple[str, ...] = ("security", "slow", "network", "legacy")


def validate_finding(value: str) -> None:
    if value not in KNOWN_FINDINGS:
        raise ValueError(
            f"Unknown regression finding id {value!r}. "
            f"Add it to tests/support/registry.py KNOWN_FINDINGS if it is a real finding."
        )


def validate_feature(value: str) -> None:
    if value not in KNOWN_FEATURES:
        raise ValueError(
            f"Unknown feature key {value!r}. "
            f"Add it to tests/support/registry.py KNOWN_FEATURES if it is a real feature area."
        )
