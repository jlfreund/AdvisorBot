#!/usr/bin/env python3
"""
The dashboard's JavaScript parses.

The renderer concatenates `Scripts/exporters/templates/*.js` into one inline `<script>`
block. A syntax error anywhere in that block takes the whole page down: no chart renders,
no button works, and the only symptom is a silent console error. Nothing else in the suite
would catch it, because every other tier asserts on the payload or the server rather than
on the script the browser has to parse.

`node --check` is the cheapest possible check and runs in milliseconds. Skipped when Node
is not installed, which is also why it cannot be the only front-end guard.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.support import env

TEMPLATES_DIR = env.app_checkout_dir() / "Scripts" / "exporters" / "templates"
JS_TEMPLATES = sorted(TEMPLATES_DIR.glob("*.js"))

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.mark.parametrize("path", JS_TEMPLATES, ids=[p.name for p in JS_TEMPLATES])
@pytest.mark.feature("render")
def test_js_template__parses(path):
    """
    Each JavaScript template is syntactically valid on its own.

    Exercises: a stray brace in any template breaks every screen in the dashboard. This
    names the file and line, which is the difference between a two-minute fix and an
    afternoon bisecting a 16,000-line inline script.
    """
    result = subprocess.run(
        ["node", "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"{path.name} does not parse:\n{result.stderr.strip()[:600]}"
    )


@pytest.mark.feature("render")
def test_js_templates__are_all_discovered():
    """The glob finds the templates, so a passing run is not a vacuous one."""
    names = {p.name for p in JS_TEMPLATES}
    assert len(names) >= 4, f"expected the app's JS templates, found {sorted(names)}"


@pytest.mark.portfolio("p1_index_saver")
@pytest.mark.feature("render")
def test_rendered_page__script_block_parses(rendered, tmp_path):
    """
    The concatenated script block in a rendered page parses as a whole.

    Exercises: the templates can each be valid while the assembled block is not, if the
    renderer's substitution or ordering introduces a problem. This checks the artefact the
    browser actually receives.
    """
    _path, html = rendered
    blocks = []
    cursor = 0
    while True:
        start = html.find("<script>", cursor)
        if start < 0:
            break
        end = html.find("</script>", start)
        if end < 0:
            break
        blocks.append(html[start + len("<script>") : end])
        cursor = end + 1
    assert blocks, "the rendered page has no inline script block"

    combined = tmp_path / "page_scripts.js"
    # Wrap in an IIFE: the page's code runs inside one, and top-level `return` or
    # duplicate `const` across blocks would otherwise fail for the wrong reason.
    combined.write_text("(function(){\n" + "\n".join(blocks) + "\n})();\n", encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(combined)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        "the rendered page's script block does not parse:\n" + result.stderr.strip()[:600]
    )
