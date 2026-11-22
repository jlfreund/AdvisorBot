#!/usr/bin/env python3
"""Wells Fargo Advisors SNAPSHOT Statement Ingestion Plugin."""

from __future__ import annotations

from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedStatement
from ingest.wells_fargo import parse_wfa_snapshot


class WellsFargoSnapshotPlugin(StatementPlugin):
    plugin_id = "wells_fargo_snapshot"
    institution_name = "Wells Fargo Advisors"
    supported_extensions = (".pdf",)

    def detect(self, ctx: IngestContext) -> float:
        pages = ctx.pages or []
        if not pages:
            return 0.0
        head = "\n".join(pages[:3])
        if (
            "Wells Fargo Advisors" in head
            or "WELLS FARGO" in head
            or "WFCS CUSTODIAN" in head
        ) and "ACCOUNT NUMBER:" in head:
            return 0.98
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        pages = ctx.pages or []
        if not pages:
            raise StatementParseError(
                "Cannot parse empty PDF pages for Wells Fargo Advisors statement.",
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            )
        try:
            return parse_wfa_snapshot(pages, ctx.cfg)
        except Exception as exc:
            raise StatementParseError(
                str(exc),
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            ) from exc
