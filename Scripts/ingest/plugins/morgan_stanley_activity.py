#!/usr/bin/env python3
"""Morgan Stanley at Work Activity Report (.xlsx) Ingestion Plugin."""

from __future__ import annotations

from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedStatement
from ingest.ms_activity_xlsx import is_ms_activity_xlsx, parse_ms_activity_xlsx


class MorganStanleyActivityXlsxPlugin(StatementPlugin):
    plugin_id = "morgan_stanley_activity_xlsx"
    institution_name = "Morgan Stanley"
    supported_extensions = (".xlsx",)

    def detect(self, ctx: IngestContext) -> float:
        if is_ms_activity_xlsx(ctx.file_path):
            return 1.0
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        try:
            return parse_ms_activity_xlsx(ctx.file_path, ctx.cfg)
        except Exception as exc:
            raise StatementParseError(
                str(exc),
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            ) from exc
