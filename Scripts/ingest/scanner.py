#!/usr/bin/env python3
"""Incremental statement scanner checking file creation/modification/move timestamps & SHA-256."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from db.database import PortfolioDB


@dataclass
class CandidateFile:
    path: Path
    rel_path: str
    size: int
    birthtime: float
    mtime: float
    ctime: float
    sha256: str
    reason: str


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_statement_files(
    statements_dir: Path,
    db: PortfolioDB,
    force_all: bool = False,
) -> tuple[int, list[CandidateFile]]:
    """
    Scans `statements_dir` recursively for statement files (`*.pdf`, `*.xlsx`, `*.grants.json`,
    `*.statement.json`) that are either:
      1) Unseen file paths,
      2) Created, modified, or moved into `Statements/` after `last_completed_run_epoch`, or
      3) Changed in size or mtime relative to the previously recorded file at that path.
    """
    if not statements_dir.exists():
        return 0, []

    last_run_ts = 0.0 if force_all else db.get_last_completed_run_epoch()
    known_by_hash = {} if force_all else db.get_ingested_hashes()
    known_by_path = {} if force_all else db.get_ingested_by_path()

    stmts_resolved = statements_dir.resolve()
    # SECURITY DO: Skip symlinks and enforce `is_relative_to(stmts_resolved)` so Statements/ links cannot read arbitrary host files.
    # SECURITY DON'T: Never follow external symlinks during recursive statement discovery.
    all_pdfs = sorted(
        p for p in statements_dir.rglob("*")
        if not p.is_symlink()
        and p.is_file()
        and p.resolve().is_relative_to(stmts_resolved)
        and not p.name.startswith(".")
        and (
            p.suffix.lower() in (".pdf", ".xlsx")
            or p.name.lower().endswith((".grants.json", ".statement.json"))
        )
    )

    candidates: list[CandidateFile] = []
    for pdf in all_pdfs:
        st = pdf.stat()
        birthtime = getattr(st, "st_birthtime", st.st_ctime)
        mtime = st.st_mtime
        ctime = st.st_ctime
        newest_ts = max(birthtime, mtime, ctime)
        abs_str = str(pdf.resolve())
        rel_str = str(pdf.relative_to(statements_dir))

        prev_rec = known_by_path.get(abs_str)
        needs_check = False
        reason = ""

        if force_all:
            needs_check = True
            reason = "force_all"
        elif prev_rec is None:
            needs_check = True
            reason = "new_file_path"
        elif newest_ts > last_run_ts:
            needs_check = True
            reason = "timestamp_newer_than_last_run"
        elif st.st_size != prev_rec["file_size"] or abs(mtime - prev_rec["file_mtime"]) > 1e-3:
            needs_check = True
            reason = "file_size_or_mtime_changed"

        if not needs_check:
            continue

        digest = compute_sha256(pdf)
        if not force_all and digest in known_by_hash:
            # Exact identical PDF bytes already ingested (e.g. file was touched/moved without content change)
            continue

        candidates.append(
            CandidateFile(
                path=pdf.resolve(),
                rel_path=rel_str,
                size=st.st_size,
                birthtime=birthtime,
                mtime=mtime,
                ctime=ctime,
                sha256=digest,
                reason=reason,
            )
        )

    return len(all_pdfs), candidates
