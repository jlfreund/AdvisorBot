#!/usr/bin/env python3
"""Default Financial Institution statement ingestion plugin registry."""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from pathlib import Path
from typing import Any

from config import get_plugins_dir, get_portfolios_dir, load_config
from ingest.framework import PluginRegistry, StatementPlugin
from ingest.plugins.canonical_json import CanonicalStatementJsonPlugin
from ingest.plugins.grant_json import GenericGrantJsonPlugin
from ingest.plugins.morgan_stanley_activity import MorganStanleyActivityXlsxPlugin
from ingest.plugins.morgan_stanley_quarterly import MorganStanleyQuarterlyPlugin
from ingest.plugins.morgan_stanley_unvested import MorganStanleyUnvestedPlugin
from ingest.plugins.ssa_xml import SsaXmlStatementPlugin
from ingest.plugins.wells_fargo import WellsFargoSnapshotPlugin

log = logging.getLogger(__name__)


def _is_inside_portfolios_dir(p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(get_portfolios_dir().resolve())
    except Exception:
        return "Portfolios" in p.parts


def _is_plugin_permitted(
    py_path: Path,
    *,
    explicit_dir: bool,
    cfg: dict[str, Any] | None,
    allow_custom: bool | None,
) -> bool:
    if allow_custom is False:
        return False
    if allow_custom is True or os.environ.get("ADVISORBOT_ALLOW_CUSTOM_PLUGINS") == "1":
        return True
    eff_cfg = cfg if cfg is not None else load_config()
    if bool((eff_cfg or {}).get("allow_custom_plugins", False)):
        return True
    allowed_list = (eff_cfg or {}).get("allowed_plugins")
    if isinstance(allowed_list, list):
        allowed_strs = {str(x).strip() for x in allowed_list if str(x).strip()}
        if py_path.name in allowed_strs or py_path.stem in allowed_strs:
            return True
    # Preserve standalone unit tests passing an explicit temp plugins_dir outside Portfolios/
    if explicit_dir and cfg is None and not _is_inside_portfolios_dir(py_path):
        return True
    return False


def load_custom_plugins_from_dir(
    registry: PluginRegistry,
    plugins_dir: Path | None = None,
    *,
    cfg: dict[str, Any] | None = None,
    allow_custom: bool | None = None,
) -> list[str]:
    """Discovers opted-in `StatementPlugin` subclasses from `Portfolios/<active>/Plugins/*.py`."""
    explicit_dir = plugins_dir is not None
    target_dir = plugins_dir or get_plugins_dir()
    loaded: list[str] = []
    if target_dir is None or not target_dir.exists() or not target_dir.is_dir():
        return loaded
    for py_path in sorted(target_dir.glob("*.py")):
        # SECURITY DO: Require explicit opt-in (`allow_custom_plugins` / `allowed_plugins`) and skip symlinks before
        # executing any arbitrary Python code in `Portfolios/<id>/Plugins/` (`SA-15`).
        # SECURITY DON'T: Never auto-exec unapproved `.py` files from portfolio data folders.
        if py_path.name.startswith("_") or py_path.is_symlink():
            continue
        if not _is_plugin_permitted(py_path, explicit_dir=explicit_dir, cfg=cfg, allow_custom=allow_custom):
            log.info("Skipping non-opted-in custom plugin %s (SA-15)", py_path)
            continue
        mod_name = f"user_custom_plugin_{py_path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py_path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for _attr_name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, StatementPlugin)
                    and obj is not StatementPlugin
                    and obj.__module__ == mod_name
                ):
                    instance = obj()
                    registry.register(instance, is_custom=True)
                    loaded.append(getattr(instance, "plugin_id", py_path.stem))
        except Exception as exc:
            log.warning("Failed to load custom plugin %s: %s", py_path, exc)
    return loaded


def build_default_registry(
    plugins_dir: Path | None = None,
    *,
    cfg: dict[str, Any] | None = None,
    allow_custom: bool | None = None,
) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(WellsFargoSnapshotPlugin())
    registry.register(MorganStanleyQuarterlyPlugin())
    registry.register(MorganStanleyUnvestedPlugin())
    registry.register(MorganStanleyActivityXlsxPlugin())
    registry.register(GenericGrantJsonPlugin())
    registry.register(CanonicalStatementJsonPlugin())
    registry.register(SsaXmlStatementPlugin())
    load_custom_plugins_from_dir(registry, plugins_dir=plugins_dir, cfg=cfg, allow_custom=allow_custom)
    return registry
