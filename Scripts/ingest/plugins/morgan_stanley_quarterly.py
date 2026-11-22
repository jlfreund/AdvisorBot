#!/usr/bin/env python3
"""Morgan Stanley at Work Quarterly Stock Plan Statement Ingestion Plugin."""

from __future__ import annotations

from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedStatement
from ingest.morgan_stanley import parse_ms_quarterly


class MorganStanleyQuarterlyPlugin(StatementPlugin):
    plugin_id = "morgan_stanley_quarterly"
    institution_name = "Morgan Stanley"
    supported_extensions = (".pdf",)

    def detect(self, ctx: IngestContext) -> float:
        pages = ctx.pages or []
        if not pages:
            return 0.0
        head = "\n".join(pages[:3])
        if "Morgan Stanley" in head and "Share Purchase and Holdings Summary" in head:
            return 0.99
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        pages = ctx.pages or []
        if not pages:
            raise StatementParseError(
                "Cannot parse empty PDF pages for Morgan Stanley quarterly statement.",
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            )
        try:
            return parse_ms_quarterly(pages, ctx.cfg)
        except Exception as exc:
            raise StatementParseError(
                str(exc),
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            ) from exc
