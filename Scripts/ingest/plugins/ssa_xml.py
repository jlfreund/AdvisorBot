#!/usr/bin/env python3
"""Social Security Administration (SSA) XML Statement Ingestion Plugin."""

from __future__ import annotations

from datetime import date
from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedStatement
from ingest.ssa_xml import parse_ssa_xml_text


_MAX_SSA_XML_BYTES = 10 * 1024 * 1024


class SsaXmlStatementPlugin(StatementPlugin):
    plugin_id = "ssa_statement_xml"
    institution_name = "Social Security Administration"
    supported_extensions = (".xml",)

    def detect(self, ctx: IngestContext) -> float:
        if not self.supports_extension(ctx.file_path):
            return 0.0
        try:
            # SECURITY INVARIANT (SA-18 / SA3-09d): Check byte cap BEFORE reading XML into memory.
            if ctx.file_path.stat().st_size > _MAX_SSA_XML_BYTES:
                return 0.0
            content = ctx.file_path.read_text(encoding="utf-8", errors="ignore")
            if (
                "SocialSecurityStatement" in content
                or "OnlineSocialSecurityStatementData" in content
                or "EarningsRecord" in content
            ):
                return 1.0
        except Exception:
            pass
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        try:
            if ctx.file_path.stat().st_size > _MAX_SSA_XML_BYTES:
                raise StatementParseError("SSA XML exceeds 10 MB size limit", file_path=ctx.file_path)
            content = ctx.file_path.read_text(encoding="utf-8", errors="ignore")
            data = parse_ssa_xml_text(content, file_path=ctx.rel_path, file_name=ctx.file_path.name)
            if not data.get("ok"):
                raise StatementParseError(data.get("error") or "Failed to parse SSA XML", file_path=ctx.file_path)
            as_of = f"{data['max_year']}-12-31" if data.get("max_year") else str(date.today())
            p_start = f"{data['min_year']}-01-01" if data.get("min_year") else None
            return ParsedStatement(
                doc_type="SSA_XML",
                institution="Social Security Administration",
                account_id="SSA",
                account_name=f"SSA Statement ({data.get('name') or 'Social Security'})",
                account_type="SSN",
                tax_status="ORDINARY_INCOME",
                display_order=900,
                period_start=p_start,
                as_of_date=as_of,
                reported_total=None,
            )
        except Exception as exc:
            if isinstance(exc, StatementParseError):
                raise
            raise StatementParseError(f"SSA XML parse failed: {exc}", file_path=ctx.file_path) from exc
