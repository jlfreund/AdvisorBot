#!/usr/bin/env python3
"""
Process-level root switching for the test harness.

Every test runs against a temporary AdvisorBot root assembled from committed fixture
portfolios. Nothing here ever reads the user's real `Portfolios/` directory.

`config.HOLDINGS_ROOT` and the paths derived from it are module constants computed at
import time, so switching roots in-process requires `config.reload_root()` (added for
this harness). `pointed_at()` wraps that in a context manager which also restores the
previous environment and clears every root-keyed cache on the way out.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

# Environment variables that influence root/portfolio resolution in `config.py`.
_ROOT_ENV_VARS = (
    "ADVISORBOT_ROOT",
    "HOLDINGS_ROOT",
    "HOLDINGS_DATA_DIR",
    "PORTFOLIOS_DIR",
    "LLM_DIR",
    "PROJECT_DIR",
    "PORTFOLIO_DIR",
    "ADVISORBOT_ALLOW_CUSTOM_PLUGINS",
)


def app_checkout_dir() -> Path:
    """The repository checkout (where `Scripts/` lives), independent of the active data root."""
    return Path(__file__).resolve().parents[3]


def reload_root(root: Path | str | None = None) -> Path:
    """Points the in-process application at `root` (or re-reads the environment)."""
    import config

    return config.reload_root(root)


def clear_caches() -> None:
    """Drops every cache that is keyed to the active root or portfolio."""
    import sys

    for mod_name, fn_name in (
        ("llm_tools", "invalidate_payload_cache"),
        ("settings", "invalidate_settings_cache"),
    ):
        mod = sys.modules.get(mod_name)
        fn = getattr(mod, fn_name, None) if mod is not None else None
        if callable(fn):
            fn()
    from . import pipeline as _pipeline

    _pipeline.clear_payload_cache()


@contextlib.contextmanager
def pointed_at(root: Path | str) -> Iterator[Path]:
    """
    Runs the block with the application pointed at `root`.

    Restores the prior environment and root on exit even if the block raises, so one
    test can never leak its root into the next.
    """
    saved = {k: os.environ.get(k) for k in _ROOT_ENV_VARS}
    try:
        for key in _ROOT_ENV_VARS:
            os.environ.pop(key, None)
        clear_caches()
        resolved = reload_root(Path(root))
        yield resolved
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        clear_caches()
        reload_root()


def current_root() -> Path:
    import config

    return config.HOLDINGS_ROOT
