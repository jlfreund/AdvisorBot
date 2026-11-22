#!/usr/bin/env python3
"""
Financial Institution (FI) Statement Ingestion Framework.

Defines the contracts, context, error-handling protocol, and plugin registry for detecting,
parsing, verifying, and reconciling financial statements from diverse institutions without
coupling institution parsers to private account numbers or household PII.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db.database import PortfolioDB
from ingest.errors import IngestDiagnostic, StatementChecksumError, StatementDetectError
from ingest.models import ParsedStatement


@dataclass
class IngestContext:
    """Contextual state passed to FI plugins during detection, parsing, and verification."""

    file_path: Path
    rel_path: str
    sha256: str = ""
    pages: list[str] | None = None
    cfg: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[IngestDiagnostic] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        """Convenience text accessor across extracted PDF pages or plain-text/CSV/JSON/XML files."""
        if self.pages is not None:
            return "\n".join(self.pages)
        if self.file_path and self.file_path.exists() and self.file_path.is_file():
            if self.file_path.suffix.lower() in (".txt", ".csv", ".tsv", ".json", ".xml", ".ofx", ".qfx", ".html", ".htm"):
                try:
                    return self.file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    return ""
        return ""

    def add_diagnostic(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        plugin_id: str | None = None,
        page_number: int | None = None,
    ) -> None:
        self.diagnostics.append(
            IngestDiagnostic(
                severity=severity,
                code=code,
                message=message,
                plugin_id=plugin_id,
                file_path=self.rel_path or str(self.file_path),
                page_number=page_number,
            )
        )

    def resolve_account_metadata(
        self,
        account_id: str,
        *,
        default_name: str | None = None,
        default_type: str | None = None,
        default_tax_status: str | None = None,
        default_display_order: int = 100,
    ) -> tuple[str | None, str | None, str | None, int]:
        """
        Resolves account metadata (`account_name`, `account_type`, `tax_status`, `display_order`)
        by merging any user-defined overrides in `Portfolios/<active>/project.json -> accounts[account_id]`
        over generic parsed defaults from the statement text.
        """
        acct_cfg = (self.cfg.get("accounts") or {}).get(account_id) or {}
        name = acct_cfg.get("account_name") or default_name
        acct_type = acct_cfg.get("account_type") or default_type
        tax_status = acct_cfg.get("tax_status") or default_tax_status
        display_order = int(acct_cfg.get("display_order", default_display_order))
        return name, acct_type, tax_status, display_order


class StatementPlugin(ABC):
    """Abstract base class for a Financial Institution statement ingestion plugin."""

    plugin_id: str = "base_plugin"
    institution_name: str = "Unknown Institution"
    supported_extensions: tuple[str, ...] = (".pdf",)

    def supports_extension(self, path: Path) -> bool:
        return path.suffix.lower() in self.supported_extensions

    @abstractmethod
    def detect(self, ctx: IngestContext) -> float:
        """
        Inspect `ctx.file_path` and/or `ctx.pages` and return a confidence score in `[0.0, 1.0]`.
        Return `0.0` if the file does not match this plugin's statement format.
        """

    @abstractmethod
    def parse(self, ctx: IngestContext) -> ParsedStatement:
        """
        Parse the statement into the canonical `ParsedStatement` schema.
        Raise `StatementParseError` on unrecoverable format violations.
        """

    def verify_checksum(
        self,
        stmt: ParsedStatement,
        ctx: IngestContext,
        *,
        strict: bool = False,
        tolerance: float | None = None,
    ) -> tuple[bool, float, float]:
        """
        Reconcile `stmt.reported_total` against summed `stmt.positions` market value (with or without accrued interest).
        Returns `(reconciled_ok, computed_total, discrepancy)`. Raises `StatementChecksumError` when `strict=True`.
        """
        tol = float(tolerance) if tolerance is not None else (0.05 if strict else 1.0)
        mkt_sum = round(sum(float(p.market_value or 0.0) for p in stmt.positions), 2)
        mkt_plus_ai = round(sum(float(p.market_value or 0.0) + float(p.accrued_interest or 0.0) for p in stmt.positions), 2)
        if stmt.reported_total is None:
            return True, mkt_sum, 0.0
        if not stmt.positions and (stmt.gsu_tranches or stmt.gsu_releases or stmt.gsu_withdrawals):
            return True, round(float(stmt.reported_total), 2), 0.0
        rep = float(stmt.reported_total)
        computed_total = mkt_plus_ai if abs(rep - mkt_plus_ai) < abs(rep - mkt_sum) else mkt_sum
        discrepancy = round(rep - computed_total, 2)
        reconciled_ok = abs(discrepancy) <= tol
        if not reconciled_ok:
            msg = (
                f"Reported total ${rep:,.2f} differs from "
                f"summed positions ${computed_total:,.2f} by ${discrepancy:+,.2f} "
                f"(tolerance ±${tol:.2f})."
            )
            ctx.add_diagnostic(
                "ERROR" if strict else "WARNING",
                "CHECKSUM_DISCREPANCY",
                msg,
                plugin_id=self.plugin_id,
            )
            if strict:
                raise StatementChecksumError(msg, plugin_id=self.plugin_id, file_path=ctx.file_path)
        return reconciled_ok, computed_total, discrepancy

    def post_ingest_reconcile(self, db: PortfolioDB, cfg: dict[str, Any]) -> None:
        """Optional hook run after batch statement ingestion to reconcile cross-statement state."""
        return None

    def dry_run(self, ctx: IngestContext) -> tuple[bool, ParsedStatement | None, str | None]:
        """
        Executes a dry-run detection and parse of ctx.file_path without altering DB or state.
        Emits a pass/fail signal:
          - Returns (True, parsed_stmt, None) on pass.
          - Returns (False, None, error_message) on fail.
        """
        try:
            score = self.detect(ctx)
            if score <= 0.0:
                return False, None, f"Plugin '{self.plugin_id}' detection score 0.0"
            stmt = self.parse(ctx)
            if stmt is None:
                return False, None, f"Plugin '{self.plugin_id}' parse returned None"
            return True, stmt, None
        except Exception as exc:
            return False, None, f"Plugin '{self.plugin_id}' parse error: {exc}"


class PluginRegistry:
    """Registry of Financial Institution statement plugins (`Portfolios/<active>/Plugins/` wins score ties)."""

    def __init__(self) -> None:
        self._plugins: list[StatementPlugin] = []

    def register(self, plugin: StatementPlugin, *, is_custom: bool = False) -> None:
        setattr(plugin, "_is_custom_plugin", bool(is_custom))
        self._plugins = [p for p in self._plugins if p.plugin_id != plugin.plugin_id]
        self._plugins.append(plugin)

    @property
    def plugins(self) -> list[StatementPlugin]:
        return list(self._plugins)

    def detect_all(self, ctx: IngestContext) -> list[tuple[StatementPlugin, float]]:
        out: list[tuple[StatementPlugin, float]] = []
        for plugin in self._plugins:
            if not plugin.supports_extension(ctx.file_path):
                out.append((plugin, 0.0))
                continue
            out.append((plugin, float(plugin.detect(ctx))))
        return out

    def detect_plugin(self, ctx: IngestContext) -> tuple[StatementPlugin | None, float]:
        best_plugin: StatementPlugin | None = None
        best_score = 0.0
        best_custom = False
        for plugin in self._plugins:
            if not plugin.supports_extension(ctx.file_path):
                continue
            score = float(plugin.detect(ctx))
            if score <= 0.0:
                continue
            is_cust = bool(getattr(plugin, "_is_custom_plugin", False))
            if score > best_score + 1e-9 or (abs(score - best_score) <= 1e-9 and is_cust and not best_custom):
                best_score = score
                best_plugin = plugin
                best_custom = is_cust
        return best_plugin, best_score

    def parse_with_best_plugin(self, ctx: IngestContext) -> tuple[ParsedStatement, StatementPlugin]:
        plugin, score = self.detect_plugin(ctx)
        if plugin is None or score <= 0.0:
            raise StatementDetectError(
                f"No registered FI plugin matched file: {ctx.rel_path or ctx.file_path.name}",
                file_path=ctx.file_path,
            )
        stmt = plugin.parse(ctx)
        return stmt, plugin

    def dry_run_file(self, ctx: IngestContext) -> tuple[bool, StatementPlugin | None, ParsedStatement | None, str | None]:
        """
        Attempts detection across registered plugins and executes dry_run() with the best match.
        Emits pass/fail signal:
          - Returns (True, plugin, stmt, None) on pass.
          - Returns (False, plugin, None, error_msg) on fail.
        """
        plugin, score = self.detect_plugin(ctx)
        if plugin is None or score <= 0.0:
            return False, None, None, f"No registered parser recognized this statement format: {ctx.rel_path or ctx.file_path.name}"
        passed, stmt, err = plugin.dry_run(ctx)
        return passed, plugin, stmt, err
