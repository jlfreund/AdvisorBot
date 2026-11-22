#!/usr/bin/env python3
"""Structured error handling and diagnostics protocol for the statement ingestion framework."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class IngestDiagnostic:
    severity: str  # 'ERROR' | 'WARNING' | 'INFO'
    code: str
    message: str
    plugin_id: str | None = None
    file_path: str | None = None
    page_number: int | None = None


class IngestError(Exception):
    """Base exception for all statement detection, parsing, and verification errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INGEST_ERROR",
        plugin_id: str | None = None,
        file_path: Path | str | None = None,
        page_number: int | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = IngestDiagnostic(
            severity="ERROR",
            code=code,
            message=message,
            plugin_id=plugin_id,
            file_path=str(file_path) if file_path is not None else None,
            page_number=page_number,
        )


class StatementDetectError(IngestError):
    """Raised when no registered FI plugin matches a statement file."""

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message, code=kwargs.pop("code", "DETECT_FAILED"), **kwargs)


class StatementParseError(IngestError):
    """Raised when an FI plugin encounters malformed or missing required fields."""

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message, code=kwargs.pop("code", "PARSE_FAILED"), **kwargs)


class StatementChecksumError(IngestError):
    """Raised when a statement fails strict accounting reconciliation and strict mode is enabled."""

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message, code=kwargs.pop("code", "CHECKSUM_MISMATCH"), **kwargs)


class InvalidHoldingClassificationError(IngestError):
    """Raised when a parsed holding omits `risk_class`, uses an invalid value, or conflicts with `asset_class`."""

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message, code=kwargs.pop("code", "INVALID_RISK_CLASS"), **kwargs)

