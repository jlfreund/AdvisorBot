#!/usr/bin/env python3
"""
Fixture portfolio generator CLI.

    PYTHONPATH=Scripts python3 -m tests.generate --all          # regenerate everything
    PYTHONPATH=Scripts python3 -m tests.generate --only p2_equity_comp_rsu
    PYTHONPATH=Scripts python3 -m tests.generate --check        # drift check, writes nothing
    PYTHONPATH=Scripts python3 -m tests.generate --root DIR --only p1_index_saver
                                                               # build a throwaway root to explore

`make fixtures` runs `--all` and then rebuilds `CASES.md`.

Output per fixture: the portfolio folder under `fixtures/portfolios/<slug>/` (data +
README) and the snapshots under `fixtures/expected/<slug>/`. Generation is deterministic,
so `--check` is a real drift test: it regenerates into a temp directory and diffs.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = TESTS_DIR.parent
for _path in (str(SCRIPTS_DIR), str(SCRIPTS_DIR.parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tests.generate import builder, docs, expected as expected_mod, spec as spec_mod  # noqa: E402
from tests.support import roots  # noqa: E402


def generate_one(slug: str, *, portfolios_dir: Path, expected_dir: Path | None) -> dict:
    denylist = roots.denylist_terms() + roots.local_household_terms()
    spec = spec_mod.load(roots.spec_path(slug), denylist=denylist)
    out_dir = Path(portfolios_dir) / slug
    manifest = builder.build_portfolio(spec, out_dir)
    docs.write_readme(spec, manifest, out_dir)
    if expected_dir is not None:
        expected_mod.build_expected(spec, out_dir, Path(expected_dir))
    return manifest


def _tree_diff(left: Path, right: Path) -> list[str]:
    """Recursive file-level diff of two generated trees."""
    diffs: list[str] = []

    def walk(rel: Path) -> None:
        lhs, rhs = left / rel, right / rel
        left_names = {p.name for p in lhs.iterdir()} if lhs.is_dir() else set()
        right_names = {p.name for p in rhs.iterdir()} if rhs.is_dir() else set()
        for name in sorted(left_names - right_names):
            diffs.append(f"only in committed: {(rel / name).as_posix()}")
        for name in sorted(right_names - left_names):
            diffs.append(f"only in regenerated: {(rel / name).as_posix()}")
        for name in sorted(left_names & right_names):
            sub = rel / name
            if (lhs / name).is_dir():
                walk(sub)
            elif not filecmp.cmp(lhs / name, rhs / name, shallow=False):
                diffs.append(f"differs: {sub.as_posix()}")

    walk(Path("."))
    return diffs


def check_drift(slugs: list[str]) -> list[str]:
    """Regenerates into a temp tree and reports any difference from the committed fixtures."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="advisorbot-drift-") as tmp:
        tmp_portfolios = Path(tmp) / "portfolios"
        tmp_portfolios.mkdir(parents=True)
        for slug in slugs:
            committed = roots.PORTFOLIOS_DIR / slug
            if not committed.is_dir():
                problems.append(f"{slug}: not generated yet (run `make fixtures`)")
                continue
            generate_one(slug, portfolios_dir=tmp_portfolios, expected_dir=None)
            for entry in _tree_diff(committed, tmp_portfolios / slug):
                problems.append(f"{slug}: {entry}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.generate", description=__doc__)
    parser.add_argument("--all", action="store_true", help="generate every spec")
    parser.add_argument("--only", action="append", default=[], metavar="SLUG", help="generate one spec (repeatable)")
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--root", metavar="DIR", help="also assemble a runnable AdvisorBot root here")
    parser.add_argument("--no-expected", action="store_true", help="skip expected-snapshot generation")
    args = parser.parse_args(argv)

    slugs = list(args.only) or ([*roots.FIXTURE_SLUGS] if (args.all or args.check or args.root) else [])
    if not slugs:
        parser.error("pass --all, --only SLUG, or --check")
    unknown = [s for s in slugs if not roots.spec_path(s).is_file()]
    if unknown:
        parser.error(f"no spec file for: {', '.join(unknown)}")

    if args.check:
        problems = check_drift(slugs)
        if problems:
            print("Fixture drift detected:", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"No fixture drift across {len(slugs)} portfolio(s).")
        return 0

    expected_dir = None if args.no_expected else roots.EXPECTED_DIR
    for slug in slugs:
        manifest = generate_one(slug, portfolios_dir=roots.PORTFOLIOS_DIR, expected_dir=expected_dir)
        print(
            f"  [fixture] {slug}: {len(manifest['statements'])} statements, "
            f"{len(manifest['grants'])} grant file(s), {len(manifest['ssa'])} SSA XML, "
            f"{len(manifest['scenarios']) - 1} scenario(s)"
        )

    if args.root:
        root = Path(args.root)
        if root.exists():
            shutil.rmtree(root)
        roots.assemble_root(root, slugs)
        print(f"  [root] assembled {root} (active: {slugs[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
