"""Privacy Shield (Case B) & Demo Mode (Case A) package exports."""

from __future__ import annotations

from privacy.demo_projector import (
    DemoTranslator,
    apply_demo_projection_to_payload,
    apply_demo_projection_to_saves,
)
from privacy.registry import (
    PrivacyConfigError,
    assign_demo_personas,
    default_privacy_config,
    load_privacy_config,
    save_privacy_config,
    sync_privacy_registry,
)
from privacy.translator import PrivacyTranslator, build_translator

__all__ = [
    "DemoTranslator",
    "PrivacyTranslator",
    "apply_demo_projection_to_payload",
    "apply_demo_projection_to_saves",
    "assign_demo_personas",
    "build_translator",
    "default_privacy_config",
    "load_privacy_config",
    "save_privacy_config",
    "sync_privacy_registry",
]
