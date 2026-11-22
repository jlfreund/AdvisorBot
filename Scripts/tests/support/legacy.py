#!/usr/bin/env python3
"""
Bridge for tests ported from the pre-fixture suite.

Those tests call `load_live_payload()`, which resolves whatever portfolio is active. Under
the new harness that is a throwaway root, so they need a payload handed to them instead.
`fixture_payload()` builds one from a committed fixture and caches it for the session.

New tests should take the `payload` fixture instead; this module exists only so the ported
coverage keeps running while it is rewritten tier by tier.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def fixture_payload(slug: str = "p2_equity_comp_rsu", *, today: str | None = None) -> dict[str, Any]:
    """
    Returns the view-model payload for a fixture portfolio, built in a throwaway root.

    The root is torn down before returning: the payload is a plain dict, and everything a
    ported engine test does with it afterwards is pure computation.
    """
    from . import env, pipeline as pipeline_support, roots

    key = (slug, today or roots.spec_as_of(slug))
    if key in _CACHE:
        return _CACHE[key]
    with tempfile.TemporaryDirectory(prefix=f"advisorbot-legacy-{slug}-") as tmp:
        root = Path(tmp) / "root"
        roots.assemble_root(root, [slug])
        with env.pointed_at(root):
            pipeline_support.clear_payload_cache()
            payload = pipeline_support.build_payload(slug, today=key[1])
            pipeline_support.clear_payload_cache()
    _CACHE[key] = payload
    return payload
