#!/usr/bin/env python3
"""
Canonical AdvisorBot statement JSON ingestion plugin (`*.statement.json`).

A first-class, institution-neutral statement format that maps one-to-one onto the
`ParsedStatement` / `ParsedPosition` schema. Two audiences:

  1. Users whose institution has no PDF/XLSX parser yet. Export or hand-write a
     `*.statement.json` and it ingests like any other statement: dedupe by SHA-256,
     chronological ordering, position snapshots, valuation anchors and `ingest_events`
     all behave identically.
  2. The test suite, which generates fixture portfolios in this format so no PDF has
     to be forged (see `Scripts/tests/README.md`). Institution PDF/XLSX parsers are
     covered separately by golden page-text fixtures.

Format (`format: "advisorbot.statement/1"`):

    {
      "format": "advisorbot.statement/1",
      "doc_type": "CANONICAL_JSON",          # optional, defaults to CANONICAL_JSON
      "institution": "Harbor Brokerage",
      "account_id": "HB-7001",
      "account_name": "Harbor Individual Brokerage",
      "account_type": "BROKERAGE",
      "tax_status": "TAXABLE",
      "display_order": 10,                   # optional
      "period_start": "2026-09-01",          # optional
      "as_of_date": "2026-09-30",
      "reported_total": 412850.17,           # optional; checksum-verified when present
      "positions": [ {...ParsedPosition fields...} ],
      "adjustments": [ {...ParsedAdjustment fields...} ],   # optional
      "grants": { ...grants.json payload... }               # optional
    }

Unknown top-level and per-position keys are rejected so that a typo in a hand-written
file fails loudly at parse time rather than silently dropping a holding. The reserved
`fixture` key is accepted and ignored (test-generator provenance metadata).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedAdjustment, ParsedPosition, ParsedStatement

FORMAT_ID = "advisorbot.statement/1"
FILE_SUFFIX = ".statement.json"
DEFAULT_DOC_TYPE = "CANONICAL_JSON"
_MAX_CANONICAL_JSON_BYTES = 10 * 1024 * 1024

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RAW_NUMERIC_ACCT_RE = re.compile(r"^(?:\d{4}-\d{4}|[A-Za-z0-9-]*\d{6,}[A-Za-z0-9-]*)$")


def _mask_if_raw_account_number(raw_acct: str | None, cfg: dict[str, Any] | None = None) -> str | None:
    """
    DESIGN INTENT (DP-05): Preserve real custodial account identifiers in local storage and Live View;
    aliasing is performed dynamically by `DemoTranslator` (Demo Mode) and `PrivacyTranslator` (AI Chat Mode B/C).
    """
    _ = cfg
    return raw_acct if raw_acct else None

_STATEMENT_KEYS = {
    "format", "doc_type", "institution", "account_id", "account_name", "account_type",
    "tax_status", "display_order", "period_start", "as_of_date", "reported_total",
    "positions", "adjustments", "grants", "fixture",
}
_REQUIRED_STATEMENT_KEYS = ("institution", "as_of_date")

_POSITION_REQUIRED = ("security_id", "symbol_or_code", "canonical_name", "asset_class", "quantity", "market_value")
_POSITION_KEYS = {
    "security_id", "symbol_or_code", "canonical_name", "asset_class", "stack_bucket",
    "is_scrapeable", "quote_symbol", "quantity", "stmt_price", "market_value", "value_date",
    "risk_class", "accrued_interest", "cost_basis", "est_annual_income", "yield_pct",
    "surrender_value", "issue_date", "initial_premium", "acquisition_date", "maturity_date",
    "expense_ratio_pct", "credited_rate_pct", "reported_return_pct", "reported_return_window",
    "metadata",
}
_POSITION_FLOATS = (
    "stmt_price", "cost_basis", "est_annual_income", "yield_pct", "surrender_value",
    "initial_premium", "expense_ratio_pct", "credited_rate_pct", "reported_return_pct",
)
_POSITION_DATES = ("value_date", "issue_date", "acquisition_date", "maturity_date")

_ADJUSTMENT_KEYS = {"security_id", "effective_date", "adj_type", "pre_quantity", "post_quantity", "notes"}
_ADJUSTMENT_REQUIRED = ("security_id", "effective_date", "adj_type", "pre_quantity", "post_quantity")


def _err(label: str, message: str) -> StatementParseError:
    return StatementParseError(f"{label}: {message}", plugin_id="canonical_statement_json")


def _require_date(label: str, field: str, value: Any, *, allow_none: bool = False) -> str | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise _err(label, f"`{field}` is required and must be YYYY-MM-DD")
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise _err(label, f"`{field}` must be YYYY-MM-DD, got {value!r}")
    return text


def _require_float(label: str, field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(label, f"`{field}` must be a number, got {value!r}")
    return float(value)


def _optional_float(label: str, field: str, value: Any) -> float | None:
    if value is None:
        return None
    return _require_float(label, field, value)


def _reject_unknown(label: str, raw: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(raw.keys()) - allowed)
    if unknown:
        raise _err(label, f"unknown {what} key(s): {', '.join(unknown)}")


def _parse_position(label: str, idx: int, raw: Any, stmt_as_of: str) -> ParsedPosition:
    tag = f"{label} positions[{idx}]"
    if not isinstance(raw, dict):
        raise _err(tag, "must be an object")
    _reject_unknown(tag, raw, _POSITION_KEYS, "position")
    for field in _POSITION_REQUIRED:
        if raw.get(field) is None:
            raise _err(tag, f"`{field}` is required")

    asset_class = str(raw["asset_class"]).strip().upper()
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise _err(tag, "`metadata` must be an object")

    is_scrapeable = bool(raw.get("is_scrapeable", False))
    quote_symbol = raw.get("quote_symbol")
    quote_symbol = str(quote_symbol).strip().upper() if quote_symbol else None
    if is_scrapeable and not quote_symbol:
        raise _err(tag, "`quote_symbol` is required when `is_scrapeable` is true")
    if not is_scrapeable:
        quote_symbol = None

    dates: dict[str, str | None] = {}
    for field in _POSITION_DATES:
        allow_none = field != "value_date"
        value = raw.get(field) if field != "value_date" else raw.get("value_date", stmt_as_of)
        dates[field] = _require_date(tag, field, value, allow_none=allow_none)

    try:
        return ParsedPosition(
            security_id=str(raw["security_id"]).strip(),
            symbol_or_code=str(raw["symbol_or_code"]).strip(),
            canonical_name=str(raw["canonical_name"]).strip(),
            asset_class=asset_class,
            stack_bucket=str(raw.get("stack_bucket") or "").strip(),
            is_scrapeable=is_scrapeable,
            quote_symbol=quote_symbol,
            quantity=_require_float(tag, "quantity", raw["quantity"]),
            stmt_price=_optional_float(tag, "stmt_price", raw.get("stmt_price")),
            market_value=_require_float(tag, "market_value", raw["market_value"]),
            value_date=str(dates["value_date"]),
            risk_class=str(raw.get("risk_class") or "").strip().lower(),
            accrued_interest=_require_float(tag, "accrued_interest", raw.get("accrued_interest", 0.0)),
            cost_basis=_optional_float(tag, "cost_basis", raw.get("cost_basis")),
            est_annual_income=_optional_float(tag, "est_annual_income", raw.get("est_annual_income")),
            yield_pct=_optional_float(tag, "yield_pct", raw.get("yield_pct")),
            surrender_value=_optional_float(tag, "surrender_value", raw.get("surrender_value")),
            issue_date=dates["issue_date"],
            initial_premium=_optional_float(tag, "initial_premium", raw.get("initial_premium")),
            acquisition_date=dates["acquisition_date"],
            maturity_date=dates["maturity_date"],
            expense_ratio_pct=_optional_float(tag, "expense_ratio_pct", raw.get("expense_ratio_pct")),
            credited_rate_pct=_optional_float(tag, "credited_rate_pct", raw.get("credited_rate_pct")),
            reported_return_pct=_optional_float(tag, "reported_return_pct", raw.get("reported_return_pct")),
            reported_return_window=(
                str(raw["reported_return_window"]).strip() if raw.get("reported_return_window") else None
            ),
            metadata=dict(metadata),
        )
    except StatementParseError:
        raise
    except Exception as exc:
        # Includes InvalidHoldingClassificationError from ParsedPosition.__post_init__.
        raise _err(tag, str(exc)) from exc


def _parse_adjustment(label: str, idx: int, raw: Any) -> ParsedAdjustment:
    tag = f"{label} adjustments[{idx}]"
    if not isinstance(raw, dict):
        raise _err(tag, "must be an object")
    _reject_unknown(tag, raw, _ADJUSTMENT_KEYS, "adjustment")
    for field in _ADJUSTMENT_REQUIRED:
        if raw.get(field) is None:
            raise _err(tag, f"`{field}` is required")
    adj_type = str(raw["adj_type"]).strip().upper()
    if adj_type not in ("PRE_SPLIT_SHARES", "HISTORICAL_SHARES"):
        raise _err(tag, f"`adj_type` must be PRE_SPLIT_SHARES or HISTORICAL_SHARES, got {adj_type!r}")
    return ParsedAdjustment(
        security_id=str(raw["security_id"]).strip(),
        effective_date=str(_require_date(tag, "effective_date", raw["effective_date"])),
        adj_type=adj_type,
        pre_quantity=_require_float(tag, "pre_quantity", raw["pre_quantity"]),
        post_quantity=_require_float(tag, "post_quantity", raw["post_quantity"]),
        notes=str(raw.get("notes") or ""),
    )


def parse_canonical_statement_payload(
    payload: dict[str, Any],
    source_label: str = "statement.json",
) -> ParsedStatement:
    """Validates a canonical statement payload and converts it into a `ParsedStatement`."""
    if not isinstance(payload, dict):
        raise _err(source_label, "payload must be a JSON object")
    fmt = str(payload.get("format") or "").strip()
    if fmt != FORMAT_ID:
        raise _err(source_label, f'`format` must be "{FORMAT_ID}", got {fmt!r}')
    _reject_unknown(source_label, payload, _STATEMENT_KEYS, "statement")
    for field in _REQUIRED_STATEMENT_KEYS:
        if not str(payload.get(field) or "").strip():
            raise _err(source_label, f"`{field}` is required")

    as_of = str(_require_date(source_label, "as_of_date", payload.get("as_of_date")))
    period_start = _require_date(source_label, "period_start", payload.get("period_start"), allow_none=True)

    raw_positions = payload.get("positions") or []
    if not isinstance(raw_positions, list):
        raise _err(source_label, "`positions` must be a list")
    raw_adjustments = payload.get("adjustments") or []
    if not isinstance(raw_adjustments, list):
        raise _err(source_label, "`adjustments` must be a list")

    positions = [_parse_position(source_label, i, row, as_of) for i, row in enumerate(raw_positions)]
    seen: set[str] = set()
    for pos in positions:
        if pos.security_id in seen:
            raise _err(source_label, f"duplicate security_id in positions: {pos.security_id}")
        seen.add(pos.security_id)
    adjustments = [_parse_adjustment(source_label, i, row) for i, row in enumerate(raw_adjustments)]

    grants = payload.get("grants")
    tranches: list[Any] = []
    releases: list[Any] = []
    withdrawals: list[Any] = []
    if grants is not None:
        if not isinstance(grants, dict):
            raise _err(source_label, "`grants` must be an object")
        from ingest.plugins.grant_json import parse_grants_payload_to_statement

        grant_stmt = parse_grants_payload_to_statement(grants, source_label=f"{source_label}#grants")
        tranches = grant_stmt.gsu_tranches
        releases = grant_stmt.gsu_releases
        withdrawals = grant_stmt.gsu_withdrawals

    if not positions and not tranches and not releases and not withdrawals:
        raise _err(source_label, "statement contains no positions and no grants")

    account_id = _mask_if_raw_account_number(str(payload.get("account_id") or "").strip() or None)
    reported_total = _optional_float(source_label, "reported_total", payload.get("reported_total"))
    # DESIGN INTENT (DA2-13b — Canonical JSON Accounting Checksum):
    # Verify `sum(positions.market_value)` against `reported_total` when present (allowing optional accrued interest)
    # so truncated/mis-summed `*.statement.json` imports fail loudly at parse time.
    if reported_total is not None and positions:
        pos_sum = sum(float(p.market_value) for p in positions)
        accrued_sum = sum(float(p.accrued_interest or 0.0) for p in positions)
        candidates = (pos_sum, pos_sum + accrued_sum)
        if min(abs(c - reported_total) for c in candidates) > 0.05:
            raise _err(
                source_label,
                f"reported_total checksum mismatch: positions sum {pos_sum:.2f} vs reported_total {reported_total:.2f}",
            )
    return ParsedStatement(
        doc_type=str(payload.get("doc_type") or DEFAULT_DOC_TYPE).strip().upper(),
        institution=str(payload["institution"]).strip(),
        account_id=account_id,
        account_name=str(payload.get("account_name") or "").strip() or None,
        account_type=str(payload.get("account_type") or "").strip().upper() or None,
        tax_status=str(payload.get("tax_status") or "").strip().upper() or None,
        display_order=int(payload.get("display_order", 100)),
        period_start=period_start,
        as_of_date=as_of,
        reported_total=reported_total,
        positions=positions,
        adjustments=adjustments,
        gsu_tranches=tranches,
        gsu_releases=releases,
        gsu_withdrawals=withdrawals,
    )


def is_canonical_statement_file(path: Path) -> bool:
    return path.name.lower().endswith(FILE_SUFFIX)


class CanonicalStatementJsonPlugin(StatementPlugin):
    """Ingests canonical `*.statement.json` files dropped into `Portfolios/<active>/Statements/`."""

    plugin_id = "canonical_statement_json"
    institution_name = "AdvisorBot Canonical Statement JSON"
    supported_extensions = (".json",)

    def detect(self, ctx: IngestContext) -> float:
        if not is_canonical_statement_file(ctx.file_path):
            return 0.0
        try:
            # SECURITY INVARIANT (SA-18 / SA3-09d): Enforce file size limit BEFORE reading into memory.
            if ctx.file_path.stat().st_size > _MAX_CANONICAL_JSON_BYTES:
                return 0.0
            data = json.loads(ctx.file_path.read_text(encoding="utf-8"))
        except Exception:
            return 0.0
        if isinstance(data, dict) and str(data.get("format") or "").strip() == FORMAT_ID:
            return 0.99
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        try:
            if ctx.file_path.stat().st_size > _MAX_CANONICAL_JSON_BYTES:
                raise ValueError(f"JSON statement exceeds {_MAX_CANONICAL_JSON_BYTES // (1024 * 1024)} MB limit")
            data = json.loads(ctx.file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StatementParseError(
                f"{ctx.file_path.name}: invalid JSON ({exc})",
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            ) from exc
        try:
            stmt = parse_canonical_statement_payload(data, source_label=ctx.file_path.name)
        except StatementParseError as exc:
            raise StatementParseError(
                str(exc), plugin_id=self.plugin_id, file_path=ctx.file_path
            ) from exc
        if stmt.account_id:
            stmt.account_id = _mask_if_raw_account_number(stmt.account_id, cfg=ctx.cfg)
            name, acct_type, tax_status, display_order = ctx.resolve_account_metadata(
                stmt.account_id,
                default_name=stmt.account_name,
                default_type=stmt.account_type,
                default_tax_status=stmt.tax_status,
                default_display_order=stmt.display_order,
            )
            stmt.account_name = name
            stmt.account_type = acct_type
            stmt.tax_status = tax_status
            stmt.display_order = display_order
        return stmt
