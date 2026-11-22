#!/usr/bin/env python3
"""
Generates `CASES.md`, the catalogue of every test in the suite.

    PYTHONPATH=Scripts python3 -m tests.catalog           # write CASES.md
    PYTHONPATH=Scripts python3 -m tests.catalog --check   # fail if it is stale

The catalogue is built by static analysis rather than by collecting with pytest, so it can
run without importing the application, and so a syntax error in one test file does not
silently drop a whole tier from the documentation.

Per test it records: tier, name, the first line of the docstring, the fixture portfolios it
declares, and any finding or feature it is tagged with. The scan test in
`scans/test_catalogue.py` keeps it in step with the code.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
for _p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.support import registry  # noqa: E402

CASES_PATH = TESTS_DIR / "CASES.md"


def _marker_args(decorator: ast.expr) -> tuple[str | None, list[str]]:
    """Returns `(marker_name, string_args)` for a `@pytest.mark.<name>(...)` decorator."""
    node = decorator
    args: list[str] = []
    if isinstance(node, ast.Call):
        args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not parts:
        return None, args
    return parts[0], args


def _module_markers(tree: ast.Module) -> dict[str, list[str]]:
    """Collects `pytestmark = ...` markers applied to a whole module."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        values = node.value.elts if isinstance(node.value, ast.List) else [node.value]
        for value in values:
            name, args = _marker_args(value)
            if name:
                out.setdefault(name, []).extend(args)
    return out


def _collect_file(path: Path) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - reported by the scan test
        return [{"error": f"{path.name}: {exc}"}]

    module_markers = _module_markers(tree)
    rel = path.relative_to(TESTS_DIR)
    tier = rel.parts[0] if rel.parts[0] in registry.TIERS else "-"
    out: list[dict] = []

    def visit(node: ast.AST, class_name: str | None = None) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef) and child.name.startswith(("Test", "test")) or (
                isinstance(child, ast.ClassDef) and child.name.endswith("Test") or
                isinstance(child, ast.ClassDef) and child.name.endswith("Tests")
            ):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                markers: dict[str, list[str]] = {k: list(v) for k, v in module_markers.items()}
                for decorator in child.decorator_list:
                    name, args = _marker_args(decorator)
                    if name:
                        markers.setdefault(name, []).extend(args)
                doc = (ast.get_docstring(child) or "").strip().splitlines()
                out.append(
                    {
                        "tier": tier,
                        "file": rel.as_posix(),
                        "class": class_name,
                        "name": child.name,
                        "summary": doc[0].strip() if doc else "",
                        "portfolios": markers.get("portfolio", []),
                        "regressions": markers.get("regression", []),
                        "features": markers.get("feature", []),
                        "legacy": "legacy" in markers,
                        "xfail": "xfail" in markers,
                        "security": "security" in markers,
                    }
                )
    visit(tree)
    return out


def collect() -> list[dict]:
    cases: list[dict] = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        cases.extend(_collect_file(path))
    return cases


def render(cases: list[dict]) -> str:
    errors = [c["error"] for c in cases if "error" in c]
    cases = [c for c in cases if "error" not in c]
    tiers = [t for t in registry.TIERS if any(c["tier"] == t for c in cases)]

    counts = {
        "total": len(cases),
        "legacy": sum(1 for c in cases if c["legacy"]),
        "xfail": sum(1 for c in cases if c["xfail"]),
        "security": sum(1 for c in cases if c["security"]),
    }
    findings = sorted({f for c in cases for f in c["regressions"]})

    lines = [
        "# Test case catalogue",
        "",
        "<!-- GENERATED FILE. Run `make fixtures` (or `python3 -m tests.catalog`) to refresh. -->",
        "",
        f"**{counts['total']} tests** across {len(tiers)} tiers. "
        f"{counts['security']} are tagged as security regressions, "
        f"{counts['xfail']} are expected failures recording an open finding, and "
        f"{counts['legacy']} are ported from the pre-fixture suite.",
        "",
        "Tagged findings: " + (", ".join(f"`{f}`" for f in findings) if findings else "_none_") + ".",
        "",
        "| Tier | Tests |",
        "| --- | --- |",
    ]
    for tier in tiers:
        lines.append(f"| `{tier}` | {sum(1 for c in cases if c['tier'] == tier)} |")
    lines.append("")

    for tier in tiers:
        tier_cases = [c for c in cases if c["tier"] == tier]
        lines.extend([f"## `{tier}`", ""])
        by_file: dict[str, list[dict]] = {}
        for case in tier_cases:
            by_file.setdefault(case["file"], []).append(case)
        for file_name in sorted(by_file):
            lines.extend([f"### `{file_name}`", "", "| Case | Portfolio | Covers | Summary |", "| --- | --- | --- | --- |"])
            for case in by_file[file_name]:
                name = case["name"]
                if case["class"]:
                    name = f"{case['class']}::{name}"
                tags: list[str] = [f"`{r}`" for r in case["regressions"]]
                tags += [f"_{f}_" for f in case["features"]]
                if case["xfail"]:
                    tags.append("**xfail**")
                if case["legacy"]:
                    tags.append("_legacy_")
                portfolios = ", ".join(f"`{p}`" for p in case["portfolios"]) or "—"
                summary = case["summary"].replace("|", "\\|") or "_(no docstring)_"
                lines.append(f"| `{name}` | {portfolios} | {' '.join(tags) or '—'} | {summary} |")
            lines.append("")

    if errors:
        lines.extend(["## Files that could not be parsed", ""])
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.catalog", description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if CASES.md is stale")
    args = parser.parse_args(argv)

    text = render(collect())
    if args.check:
        current = CASES_PATH.read_text(encoding="utf-8") if CASES_PATH.is_file() else ""
        if current != text:
            print("CASES.md is stale. Run: make fixtures", file=sys.stderr)
            return 1
        print("CASES.md is up to date.")
        return 0
    CASES_PATH.write_text(text, encoding="utf-8")
    print(f"  [catalog] wrote {CASES_PATH.relative_to(SCRIPTS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
