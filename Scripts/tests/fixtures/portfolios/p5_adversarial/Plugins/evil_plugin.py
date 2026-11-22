#!/usr/bin/env python3
"""
A custom plugin that must never be imported.

Ingest loads `Portfolios/<slug>/Plugins/*.py` only when the portfolio opts in
(`allow_custom_plugins` / `allowed_plugins`, finding SA-15). This fixture does not opt in,
so the marker file below must not exist after any ingest, audit or render.
"""
from pathlib import Path

(Path(__file__).resolve().parent / "PLUGIN_EXECUTED.marker").write_text("executed\n", encoding="utf-8")

from ingest.framework import IngestContext, StatementPlugin  # noqa: E402
from ingest.models import ParsedStatement  # noqa: E402


class EvilPlugin(StatementPlugin):
    plugin_id = "evil_plugin"
    institution_name = "Should Never Load"
    supported_extensions = (".pdf",)

    def detect(self, ctx: IngestContext) -> float:
        return 1.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        raise RuntimeError("evil plugin should never run")
