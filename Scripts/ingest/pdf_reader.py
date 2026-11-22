#!/usr/bin/env python3
"""PDF text extraction using macOS native PDFKit (via Swift) with pypdf fallback."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

SWIFT_EXTRACT_SCRIPT = r"""
import Foundation
import PDFKit

let args = Array(CommandLine.arguments.dropFirst())
var result: [String: [String]] = [:]

for path in args {
    let url = URL(fileURLWithPath: path)
    if let doc = PDFDocument(url: url) {
        var pages: [String] = []
        for i in 0..<doc.pageCount {
            pages.append(doc.page(at: i)?.string ?? "")
        }
        result[path] = pages
    } else {
        result[path] = []
    }
}

let data = try! JSONSerialization.data(withJSONObject: result, options: [])
FileHandle.standardOutput.write(data)
"""


def extract_pdf_pages_batch(pdf_paths: list[Path]) -> dict[Path, list[str]]:
    """Extracts page-by-page UTF-8 text for one or more PDF files in a single Swift PDFKit call."""
    if not pdf_paths:
        return {}

    str_paths = [str(p.resolve()) for p in pdf_paths]
    try:
        proc = subprocess.run(
            ["swift", "-e", SWIFT_EXTRACT_SCRIPT, *str_paths],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        raw_map: dict[str, list[str]] = json.loads(proc.stdout)
        return {p: raw_map.get(str(p.resolve()), []) for p in pdf_paths}
    except Exception:
        # Fallback to pypdf if swift is unavailable
        from pypdf import PdfReader

        # SECURITY DO: Keep bounded page limit (`max_pages`) and `timeout=60` on Swift subprocess (`SA-08`).
        # SECURITY DON'T: Never invoke `swift` with `shell=True` or iterate unbounded pages on malformed PDFs.
        max_pages = 200
        out: dict[Path, list[str]] = {}
        for p in pdf_paths:
            reader = PdfReader(str(p))
            out[p] = [(page.extract_text() or "") for page in reader.pages[:max_pages]]
        return out
