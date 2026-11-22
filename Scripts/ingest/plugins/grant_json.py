#!/usr/bin/env python3
"""Canonical Multi-FI Stock Grant JSON Ingestion Plugin & Shared Payload Validator."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import (
    ParsedGSURelease,
    ParsedGSUTranche,
    ParsedGSUWithdrawal,
    ParsedStatement,
)

_MAX_GRANT_JSON_BYTES = 10 * 1024 * 1024
_RAW_NUMERIC_ACCT_RE = re.compile(r"^(?:\d{4}-\d{4}|[A-Za-z0-9-]*\d{6,}[A-Za-z0-9-]*)$")


def _mask_grant_acct(raw_acct: str, cfg: dict[str, Any] | None = None) -> str:
    _ = cfg
    return str(raw_acct or "").strip()


def parse_grants_payload_to_statement(
    payload: dict[str, Any],
    source_label: str = "grants.json",
    cfg: dict[str, Any] | None = None,
) -> ParsedStatement:
    """Validates a canonical grant JSON payload and converts it into a `ParsedStatement`."""
    if not isinstance(payload, dict):
        raise ValueError(f"{source_label}: payload must be a JSON object")

    raw_tranches = payload.get("unvested_tranches") or []
    raw_releases = payload.get("releases_history") or []
    raw_withdrawals = payload.get("withdrawals") or []
    if not isinstance(raw_tranches, list) or not isinstance(raw_releases, list) or not isinstance(raw_withdrawals, list):
        raise ValueError(f"{source_label}: unvested_tranches, releases_history, and withdrawals must be lists")

    if not raw_tranches and not raw_releases and not raw_withdrawals:
        raise ValueError(f"{source_label}: payload contains no unvested_tranches, releases_history, or withdrawals")

    institution = str(payload.get("institution") or "Custom Grant API").strip()
    top_acct = _mask_grant_acct(str(payload.get("account_id") or "").strip(), cfg=cfg)
    top_sym = str(payload.get("symbol") or "").strip().upper()
    top_comp = str(payload.get("company_name") or "").strip()
    top_award = str(payload.get("award_type") or "RSU").strip()
    as_of_date = str(payload.get("as_of_date") or "").strip()

    tranches: list[ParsedGSUTranche] = []
    for idx, row in enumerate(raw_tranches):
        if not isinstance(row, dict):
            raise ValueError(f"{source_label}: unvested_tranches[{idx}] must be an object")
        sym = str(row.get("symbol") or top_sym).strip().upper()
        if not sym:
            raise ValueError(f"{source_label}: unvested_tranches[{idx}] missing symbol")
        gid = str(row.get("grant_id") or "").strip()
        vdate = str(row.get("vest_date") or "").strip()
        if not gid or not vdate:
            raise ValueError(f"{source_label}: unvested_tranches[{idx}] requires grant_id and vest_date")
        gdate = str(row.get("grant_date") or vdate).strip()
        orig_raw = row.get("orig_qty")
        tot_raw = row.get("total_qty")
        deus_raw = row.get("deus")
        if orig_raw is None and tot_raw is None:
            raise ValueError(f"{source_label}: unvested_tranches[{idx}] requires orig_qty or total_qty")
        orig_q = float(orig_raw if orig_raw is not None else tot_raw)
        if tot_raw is not None:
            tot_q = float(tot_raw)
            deus_q = float(deus_raw) if deus_raw is not None else round(tot_q - orig_q, 6)
        else:
            deus_q = float(deus_raw or 0.0)
            tot_q = round(orig_q + deus_q, 6)
        tranches.append(
            ParsedGSUTranche(
                grant_id=gid,
                grant_date=gdate,
                vest_date=vdate,
                orig_qty=orig_q,
                deus=deus_q,
                total_qty=tot_q,
                symbol=sym,
                company_name=str(row.get("company_name") or top_comp or sym).strip(),
                award_type=str(row.get("award_type") or top_award or "RSU").strip(),
                account_id=_mask_grant_acct(str(row.get("account_id") or top_acct).strip(), cfg=cfg),
            )
        )

    releases: list[ParsedGSURelease] = []
    for idx, row in enumerate(raw_releases):
        if not isinstance(row, dict):
            raise ValueError(f"{source_label}: releases_history[{idx}] must be an object")
        sym = str(row.get("symbol") or top_sym).strip().upper()
        if not sym:
            raise ValueError(f"{source_label}: releases_history[{idx}] missing symbol")
        onum = str(row.get("order_number") or "").strip()
        vdate = str(row.get("vest_date") or "").strip()
        if not onum or not vdate:
            raise ValueError(f"{source_label}: releases_history[{idx}] requires order_number and vest_date")
        releases.append(
            ParsedGSURelease(
                order_number=onum,
                vest_date=vdate,
                release_price=float(row.get("release_price") or 0.0),
                gross_shares=float(row.get("gross_shares") or 0.0),
                net_shares=float(row.get("net_shares") or 0.0),
                tax_method=str(row.get("tax_method") or "").strip(),
                grant_id=str(row.get("grant_id") or "UNRESOLVED").strip() or "UNRESOLVED",
                symbol=sym,
                company_name=str(row.get("company_name") or top_comp or sym).strip(),
                award_type=str(row.get("award_type") or top_award or "RSU").strip(),
                account_id=_mask_grant_acct(str(row.get("account_id") or top_acct).strip(), cfg=cfg),
            )
        )

    withdrawals: list[ParsedGSUWithdrawal] = []
    for idx, row in enumerate(raw_withdrawals):
        if not isinstance(row, dict):
            raise ValueError(f"{source_label}: withdrawals[{idx}] must be an object")
        sym = str(row.get("symbol") or top_sym).strip().upper()
        if not sym:
            raise ValueError(f"{source_label}: withdrawals[{idx}] missing symbol")
        onum = str(row.get("order_number") or "").strip()
        edate = str(row.get("exec_date") or "").strip()
        if not onum or not edate:
            raise ValueError(f"{source_label}: withdrawals[{idx}] requires order_number and exec_date")
        net_amt_raw = row.get("net_amount")
        withdrawals.append(
            ParsedGSUWithdrawal(
                order_number=onum,
                exec_date=edate,
                plan_name=str(row.get("plan_name") or f"{sym} {top_award}").strip(),
                tx_type=str(row.get("tx_type") or "Sale").strip(),
                price=float(row.get("price") or 0.0),
                quantity=float(row.get("quantity") or 0.0),
                net_amount=float(net_amt_raw) if net_amt_raw is not None else None,
                symbol=sym,
                account_id=_mask_grant_acct(str(row.get("account_id") or top_acct).strip(), cfg=cfg),
            )
        )

    if not as_of_date:
        dates = [r.vest_date for r in releases] + [w.exec_date for w in withdrawals]
        as_of_date = max(dates) if (not tranches and dates) else datetime.now().strftime("%Y-%m-%d")

    return ParsedStatement(
        doc_type="GENERIC_GRANTS_JSON",
        institution=institution,
        account_id=top_acct or f"{institution.upper().replace(' ', '_')}_STOCK_PLAN",
        account_name=None,
        account_type=None,
        tax_status=None,
        display_order=10,
        period_start=None,
        as_of_date=as_of_date,
        reported_total=None,
        gsu_tranches=tranches,
        gsu_releases=releases,
        gsu_withdrawals=withdrawals,
    )


def ingest_grant_payload_dict(
    db: Any,
    cfg: dict[str, Any],
    payload: dict[str, Any],
    source_hash: str | None = None,
    reconcile: bool = True,
    source_label: str = "grants_api",
) -> dict[str, Any]:
    """Validates, ingests, and reconciles a grant JSON payload directly into `PortfolioDB`."""
    from ingest.pipeline import _reconcile_gsu_release_grants_and_ledger, apply_parsed_statement

    stmt = parse_grants_payload_to_statement(payload, source_label=source_label, cfg=cfg)
    canonical_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    fhash = source_hash or hashlib.sha256(canonical_bytes).hexdigest()

    run_id = db.start_run("GRANTS_API", notes=source_label)
    pos_changes, anchors_updated = apply_parsed_statement(db, stmt, source_hash=fhash, run_id=run_id)
    if reconcile:
        _reconcile_gsu_release_grants_and_ledger(db, cfg=cfg)
    db.finish_run(
        run_id,
        files_scanned=1,
        files_ingested=1,
        positions_changed=pos_changes,
        anchors_updated=anchors_updated,
    )
    db.conn.commit()

    syms = sorted(
        {
            *(t.symbol for t in stmt.gsu_tranches),
            *(r.symbol for r in stmt.gsu_releases),
            *(w.symbol for w in stmt.gsu_withdrawals),
        }
    )
    return {
        "ok": True,
        "as_of_date": stmt.as_of_date,
        "institution": stmt.institution,
        "symbols": syms,
        "tranches_loaded": len(stmt.gsu_tranches),
        "releases_loaded": len(stmt.gsu_releases),
        "withdrawals_loaded": len(stmt.gsu_withdrawals),
        "positions_changed": pos_changes,
    }


class GenericGrantJsonPlugin(StatementPlugin):
    """Ingests canonical `*.grants.json` files dropped into `Project/Statements/`."""

    plugin_id = "generic_grant_json"
    institution_name = "Generic Equity Grants JSON"
    supported_extensions = (".json",)

    def detect(self, ctx: IngestContext) -> float:
        name = ctx.file_path.name.lower()
        if not name.endswith(".grants.json"):
            return 0.0
        try:
            # SECURITY INVARIANT (SA-18 / SA3-09d): Check byte cap BEFORE loading file into memory.
            if ctx.file_path.stat().st_size > _MAX_GRANT_JSON_BYTES:
                return 0.0
            data = json.loads(ctx.file_path.read_text(encoding="utf-8"))
        except Exception:
            return 0.0
        if isinstance(data, dict) and any(
            k in data for k in ("unvested_tranches", "releases_history", "withdrawals")
        ):
            return 0.98
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        try:
            if ctx.file_path.stat().st_size > _MAX_GRANT_JSON_BYTES:
                raise ValueError(f"Grant JSON exceeds {_MAX_GRANT_JSON_BYTES // (1024 * 1024)} MB limit")
            data = json.loads(ctx.file_path.read_text(encoding="utf-8"))
            return parse_grants_payload_to_statement(data, source_label=ctx.file_path.name, cfg=ctx.cfg)
        except Exception as exc:
            raise StatementParseError(
                str(exc),
                plugin_id=self.plugin_id,
                file_path=ctx.file_path,
            ) from exc
