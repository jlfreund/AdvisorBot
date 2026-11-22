#!/usr/bin/env python3
"""
Security audit regression tests covering SA-02 through SA-26 remediations.
All test fixtures use strictly synthetic identifiers.
"""
from __future__ import annotations

import getpass
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = ROOT / "Scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import holdings  # noqa: E402
import llm_tools  # noqa: E402
import mcp_server  # noqa: E402
import advisorbot_server  # noqa: E402
from db.database import PortfolioDB  # noqa: E402
from exporters import html_report  # noqa: E402
from exporters import view_model  # noqa: E402
from ingest import account_masking, ms_activity_xlsx, plugins, scanner, ssa_xml  # noqa: E402
from privacy.demo_projector import DemoTranslator, apply_demo_projection_to_payload  # noqa: E402
from privacy.registry import _discover_entities, assign_demo_personas, default_privacy_config  # noqa: E402
from privacy.translator import build_translator  # noqa: E402


def _synthetic_privacy_cfg(mode: str = "identity_and_values", scale_factor: float = 0.5) -> dict:
    cfg = default_privacy_config()
    cfg["enabled"] = True
    cfg["ai_shield"] = {
        "mode": mode,
        "scale_factor": scale_factor,
        "block_inapp_agy_cli": True,
    }
    cfg["demo_mode"] = {
        "enabled": False,
        "household_label": "Demo Family",
        "scale_factor": 1.0,
    }
    sources = {
        "cfg": {
            "household_name": "Monroe",
            "investors": [{"name": "Alex Monroe", "birth_date": "1979-08-14"}],
            "beneficiaries": [{"name": "Rowan Monroe", "birth_date": "2011-04-12"}],
        },
        "accounts": {"6420-8114": {"name": "Alex Primary Brokerage", "owner": "Alex", "type": "brokerage"}},
        "securities": [],
        "sim_assets": [],
        "scenarios": [],
    }
    cfg["entities"] = assign_demo_personas(_discover_entities(sources))
    return cfg


class TestShieldFailClosedAndLeakControls(unittest.TestCase):
    """SA-02, SA-03, SA-04, SA-05, SA-10, SA-12, SA-14, SA-16, SA-17, SA-19."""

    def test_sa02_inapp_shield_and_mcp_fail_closed(self) -> None:
        with patch("llm_tools.load_privacy_config", side_effect=RuntimeError("corrupt json")):
            err, tr, mode = llm_tools._resolve_inapp_privacy_shield(False, {"provider": "gemini"})
        self.assertIsNotNone(err)
        self.assertIsNone(tr)
        self.assertEqual(mode, "error_blocked")
        self.assertEqual(err.get("privacy_shield"), "error_blocked")

        with patch("mcp_server.load_privacy_config", side_effect=OSError("missing")):
            mcp_err, mcp_tr = mcp_server._check_mcp_access()
        self.assertIsNotNone(mcp_err)
        self.assertIsNone(mcp_tr)
        self.assertFalse(mcp_err.get("ok"))
        self.assertFalse(mcp_err.get("mcp_enabled"))

        fake_cfgs = {
            "gemini_cfg": {
                "id": "gemini_cfg",
                "provider": "gemini",
                "api_key": "test-key-123",
                "models": ["gemini-2.5-flash"],
                "model": "gemini-2.5-flash",
            }
        }
        with (
            patch("llm_tools.load_all_llm_configs", return_value=fake_cfgs),
            patch("llm_tools.load_privacy_config", side_effect=ValueError("bad cfg")),
        ):
            models = llm_tools.list_available_models(probe=False)
        self.assertEqual(len(models), 1)
        self.assertTrue(models[0]["shield_blocked"])
        self.assertIn("failed to load privacy configuration", models[0]["shield_reason"])

    def test_sa03_primary_symbol_scrub_and_tool_names(self) -> None:
        raw = {
            "goog_value": 12345.0,
            "non_goog_total": 50000.0,
            "nested": {"googPrice": 180.5, "ok_field": 42},
        }
        scrubbed = llm_tools._scrub_primary_symbol_keys(raw)
        self.assertIn("primary_value", scrubbed)
        self.assertIn("non_primary_total", scrubbed)
        self.assertIn("primaryPrice", scrubbed["nested"])
        self.assertNotIn("goog", json.dumps(scrubbed).lower())

        decl_names = {d["name"] for d in llm_tools.GEMINI_TOOL_DECLARATIONS}
        self.assertIn("holdings__compute_non_primary_growth", decl_names)
        self.assertNotIn("holdings__compute_non_goog_growth", decl_names)
        run_sim_decl = next(d for d in llm_tools.GEMINI_TOOL_DECLARATIONS if d["name"] == "holdings__run_simulation")
        self.assertIn("apply_non_primary_rate_mode", run_sim_decl["parameters"]["properties"])
        self.assertNotIn("apply_non_goog_rate_mode", run_sim_decl["parameters"]["properties"])
        mcp_src = (SCRIPTS_DIR / "mcp_server.py").read_text(encoding="utf-8")
        self.assertIn("def holdings__compute_non_primary_growth", mcp_src)
        self.assertIn("apply_non_primary_rate_mode", mcp_src)

    def test_sa04_mode_c_scales_all_monetary_keys_and_preserves_rates(self) -> None:
        cfg = _synthetic_privacy_cfg(mode="identity_and_values", scale_factor=0.5)
        tr = build_translator(cfg)
        payload = {
            "current_value": 2000.0,
            "total_value_usd": 4000.0,
            "price": 100.0,
            "quantity": 10.0,
            "annual_rate_pct": 7.5,
            "year": 2026,
            "age": 48,
            "kpi_summary": {"peak_net_worth": 5000000.0, "retire_age": 60},
            "note": "Alex Monroe has 6420-8114 with ending in 8114 and gross_value=8000",
        }
        aliased = tr.to_alias(payload)
        self.assertAlmostEqual(aliased["current_value"], 1000.0)
        self.assertAlmostEqual(aliased["total_value_usd"], 2000.0)
        # Per DP-03: market per-share price stays unscaled while share quantity scales by k,
        # preserving (k * quantity) * price == k * current_value (5.0 * 100.0 == 500.0).
        self.assertAlmostEqual(aliased["price"], 100.0)
        self.assertAlmostEqual(aliased["quantity"], 5.0)
        self.assertEqual(aliased["annual_rate_pct"], 7.5)
        self.assertEqual(aliased["year"], 2026)
        self.assertEqual(aliased["age"], 48)
        self.assertAlmostEqual(aliased["kpi_summary"]["peak_net_worth"], 2500000.0)
        self.assertEqual(aliased["kpi_summary"]["retire_age"], 60)
        self.assertNotIn("Alex Monroe", aliased["note"])
        self.assertNotIn("6420-8114", aliased["note"])
        self.assertNotIn("8114", aliased["note"])
        self.assertIn("gross_value=4000", aliased["note"])

    def test_sa05_no_dangerously_skip_permissions(self) -> None:
        src = (SCRIPTS_DIR / "llm_tools.py").read_text(encoding="utf-8")
        self.assertNotIn("--dangerously-skip-permissions", src)

    def test_sa10_generate_text_with_model_respects_shield(self) -> None:
        ext_cfg = {
            "id": "ext_openai",
            "provider": "openai",
            "api_key": "sk-test",
            "has_key": True,
            "api_base_url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-4o",
            "models": ["gpt-4o"],
        }
        cfg_map = {"ext_openai": ext_cfg}
        with (
            patch("llm_tools.load_all_llm_configs", return_value=cfg_map),
            patch("llm_tools.load_llm_config", return_value=ext_cfg),
            patch("llm_tools.load_privacy_config", return_value=_synthetic_privacy_cfg(mode="local_only")),
            patch("llm_tools._call_provider_single_prompt") as mock_call,
        ):
            text, lbl = llm_tools.generate_text_with_model("Hello Alex Monroe", model_id="ext_openai")
        self.assertIsNone(text)
        self.assertIsNone(lbl)
        mock_call.assert_not_called()

        captured_prompts: list[str] = []

        def _fake_provider(prov: str, prompt: str, cfg: dict):
            captured_prompts.append(prompt)
            return "Summary for INVESTOR_1 regarding ACCT_1", "gpt-4o"

        with (
            patch("llm_tools.load_all_llm_configs", return_value=cfg_map),
            patch("llm_tools.load_llm_config", return_value=ext_cfg),
            patch("llm_tools.load_privacy_config", return_value=_synthetic_privacy_cfg(mode="identity_aliased")),
            patch("llm_tools._call_provider_single_prompt", side_effect=_fake_provider),
        ):
            out_text, out_lbl = llm_tools.generate_text_with_model(
                "Review Alex Monroe account 6420-8114",
                model_id="ext_openai",
            )
        self.assertEqual(out_lbl, "gpt-4o")
        self.assertTrue(captured_prompts)
        self.assertNotIn("Alex Monroe", captured_prompts[0])
        self.assertNotIn("6420-8114", captured_prompts[0])
        self.assertIn("Alex", out_text or "")

    def test_sa12_mcp_honors_privacy_shield_mode(self) -> None:
        cfg_disabled = _synthetic_privacy_cfg(mode="identity_aliased")
        cfg_disabled["mcp_server"] = {"enabled": False}
        with patch("mcp_server.load_privacy_config", return_value=cfg_disabled):
            err, _ = mcp_server._check_mcp_access()
        self.assertIsNotNone(err)
        self.assertFalse(err["ok"])

        cfg_enabled = _synthetic_privacy_cfg(mode="identity_aliased")
        cfg_enabled["mcp_server"] = {"enabled": True}
        with (
            patch("mcp_server.load_privacy_config", return_value=cfg_enabled),
            patch("llm_tools.execute_named_tool", return_value={"ok": True, "owner": "Alex Monroe", "acct": "6420-8114"}),
        ):
            res = mcp_server._invoke_mcp_tool("holdings__get_overview", {})
        self.assertTrue(res["ok"])
        self.assertNotIn("Alex Monroe", json.dumps(res))
        self.assertNotIn("6420-8114", json.dumps(res))

    def test_sa14_destructive_tools_blocked_without_confirmation(self) -> None:
        saves_decl = next(d for d in llm_tools.GEMINI_TOOL_DECLARATIONS if d["name"] == "holdings__manage_saves")
        self.assertNotIn("overwrite_existing", saves_decl["parameters"]["properties"])
        ctrl_decl = next(d for d in llm_tools.GEMINI_TOOL_DECLARATIONS if d["name"] == "holdings__control_live_ui")
        self.assertNotIn("rebuild_site", ctrl_decl["parameters"]["properties"]["refresh_action"].get("enum", []))

        _, del_res = llm_tools._exec_shielded_tool("holdings__manage_saves", {"action": "delete", "save_name": "base"}, None)
        self.assertFalse(del_res["ok"])
        self.assertTrue(del_res.get("confirmation_required"))

        _, ovw_res = llm_tools._exec_shielded_tool(
            "holdings__manage_saves",
            {"action": "create", "save_name": "base", "overwrite_existing": True},
            None,
        )
        self.assertFalse(ovw_res["ok"])
        self.assertTrue(ovw_res.get("confirmation_required"))

        _, reb_res = llm_tools._exec_shielded_tool(
            "holdings__control_live_ui",
            {"refresh_action": "rebuild_site"},
            None,
        )
        self.assertFalse(reb_res["ok"])
        self.assertTrue(reb_res.get("confirmation_required"))

    def test_sa16_gemini_uses_header_not_query_param(self) -> None:
        captured: list[tuple[str, dict]] = []

        def _fake_post(url: str, headers: dict, payload: dict, timeout: int = 150) -> dict:
            captured.append((url, headers))
            return {"candidates": [{"content": {"parts": [{"text": "All clear."}]}}]}

        with patch("llm_tools._http_post_json", side_effect=_fake_post):
            res = llm_tools._run_chat_gemini_api(
                "hi",
                {"provider": "gemini", "api_key": "gem-secret-999", "model": "gemini-2.5-flash"},
                "",
                0,
                translator=None,
            )
        self.assertTrue(res["ok"])
        self.assertEqual(len(captured), 1)
        url, headers = captured[0]
        self.assertNotIn("key=", url)
        self.assertEqual(headers.get("x-goog-api-key"), "gem-secret-999")

    def test_sa17_portfolio_llm_precedence_and_endpoint_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            global_dir = tpath / "global_llm"
            pdir = tpath / "Portfolios" / "p1"
            port_dir = pdir / "LLM"
            global_dir.mkdir(parents=True)
            port_dir.mkdir(parents=True)
            (global_dir / "shared.json").write_text(
                json.dumps({"id": "shared", "provider": "openai", "api_key": "global-k", "model": "gpt-4o"}),
                encoding="utf-8",
            )
            (port_dir / "shared.json").write_text(
                json.dumps({"id": "shared", "provider": "openai", "api_key": "port-k", "model": "gpt-4o-mini"}),
                encoding="utf-8",
            )
            with (
                patch("llm_tools.get_portfolio_dir", return_value=pdir),
                patch("llm_tools.get_llm_dir", return_value=global_dir),
            ):
                cfgs = llm_tools.load_all_llm_configs()
            self.assertEqual(cfgs["shared"]["api_key"], "port-k")
            self.assertEqual(cfgs["shared"]["model"], "gpt-4o-mini")

        warn = llm_tools._check_base_url_trust("openai", "https://evil.example.com/v1")
        self.assertIsNotNone(warn)
        self.assertIsNone(llm_tools._check_base_url_trust("openai", "https://api.openai.com/v1/chat/completions"))

    def test_sa19_os_user_path_redaction_and_error_sanitization(self) -> None:
        msg = llm_tools._sanitize_error_for_llm("IOError at /Users/realperson/Portfolios/secret/db.sqlite failed")
        self.assertNotIn("realperson", msg)
        self.assertIn("[LOCAL_PATH]", msg)

        tr = build_translator(_synthetic_privacy_cfg(mode="identity_aliased"))
        os_user = getpass.getuser()
        aliased = tr.to_alias_text(f"Error in /Users/{os_user}/Desktop/report.txt")
        self.assertNotIn(os_user, aliased)


class TestStaticAndServerSecurity(unittest.TestCase):
    """SA-06, SA-20, SA-21, SA-22."""

    def test_sa20_persistent_secret_and_explicit_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with (
                patch.object(advisorbot_server, "DATA_DIR", tmp_p),
                patch.object(advisorbot_server, "TOKEN_FILE", tmp_p / ".session_token"),
            ):
                t1 = advisorbot_server._init_session_token()
                t2 = advisorbot_server._init_session_token()
                # Per DP-01: local 0600 .session_secret persists across restarts for seamless reconnect
                self.assertEqual(t1, t2)
                self.assertGreaterEqual(len(t1), 32)
                mode = (tmp_p / ".session_secret").stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)
                # Explicit --rotate-token rotates the secret
                t3 = advisorbot_server._init_session_token(rotate=True)
                self.assertNotEqual(t2, t3)

    def test_sa22_svg_sanitizer_strips_xss(self) -> None:
        dirty = (
            '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
            '<script>fetch("http://evil")</script>'
            "<foreignObject><div>x</div></foreignObject>"
            '<a href="javascript:alert(2)"><rect width="10" height="10"/></a>'
            "</svg>"
        )
        cleaned = advisorbot_server._sanitize_svg_text(dirty)
        self.assertNotIn("<script", cleaned.lower())
        self.assertNotIn("onload=", cleaned.lower())
        self.assertNotIn("foreignobject", cleaned.lower())
        self.assertNotIn("javascript:", cleaned.lower())

    def test_sa06_sa21_http_auth_gate_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html><body>Secret Dashboard</body></html>", encoding="utf-8")
            (root / "Statements").mkdir()
            (root / "Statements" / "stmt.pdf").write_bytes(b"%PDF-1.4 secret")
            (root / "Docs").mkdir()
            (root / "Docs" / "help.html").write_text("<html>Help</html>", encoding="utf-8")
            (root / "Scenarios" / "snapshots").mkdir(parents=True)
            (root / "Scenarios" / "snapshots" / "priv.jpg").write_bytes(b"JPG")

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            with (
                patch.object(advisorbot_server, "PROJECT_ROOT", root),
                patch.object(advisorbot_server, "SESSION_TOKEN", "test-ephemeral-token-xyz"),
                patch.object(advisorbot_server, "get_portfolio_dir", return_value=None),
            ):
                srv = ThreadingHTTPServer(("127.0.0.1", 0), advisorbot_server.SavesRequestHandler)
                port = srv.server_address[1]
                th = threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()
                try:
                    base = f"http://127.0.0.1:{port}"
                    for path in (
                        "/",
                        "/index.html",
                        "/api/llm/models",
                        "/api/quote?symbol=GOOG",
                        "/Statements/stmt.pdf",
                        "/Scenarios/snapshots/priv.jpg",
                        "/Docs/help.html",
                    ):
                        with self.assertRaises(urllib.error.HTTPError) as cm:
                            urllib.request.urlopen(base + path, timeout=5)
                        self.assertEqual(cm.exception.code, 403, f"Expected 403 for unauthed {path}")
                        # SA2-01: 403 HTML must NEVER disclose SESSION_TOKEN
                        err_body = cm.exception.read().decode("utf-8", errors="replace")
                        self.assertNotIn("test-ephemeral-token-xyz", err_body)

                    # Traversal out of Docs/ into Scenarios/ must fail closed
                    with self.assertRaises(urllib.error.HTTPError) as cm_trav:
                        urllib.request.urlopen(base + "/Docs/../Scenarios/snapshots/priv.jpg", timeout=5)
                    self.assertIn(cm_trav.exception.code, (403, 404))

                    # Health stays public
                    with urllib.request.urlopen(base + "/api/health", timeout=5) as resp:
                        self.assertEqual(resp.status, 200)

                    # SA2-01 / DP-01: Unauthenticated silent reconnect MUST be rejected (403, no Set-Cookie)
                    req_unauth_rec = urllib.request.Request(base + "/api/auth/reconnect", method="POST", data=b"{}")
                    req_unauth_rec.add_header("Origin", base)
                    req_unauth_rec.add_header("Sec-Fetch-Site", "same-origin")
                    req_unauth_rec.add_header("Content-Type", "application/json")
                    req_unauth_rec.add_header("X-Requested-With", "AdvisorBot")
                    with self.assertRaises(urllib.error.HTTPError) as cm_unrec:
                        urllib.request.urlopen(req_unauth_rec, timeout=5)
                    self.assertEqual(cm_unrec.exception.code, 403)
                    self.assertEqual(cm_unrec.exception.headers.get("Set-Cookie", ""), "")

                    # One-time ?token= handshake sets HttpOnly; SameSite=Strict cookie and redirects to /
                    opener = urllib.request.build_opener(_NoRedirect)
                    with self.assertRaises(urllib.error.HTTPError) as cm_auth:
                        opener.open(base + "/?token=test-ephemeral-token-xyz", timeout=5)
                    self.assertEqual(cm_auth.exception.code, 302)
                    set_cookie = cm_auth.exception.headers.get("Set-Cookie", "")
                    self.assertIn("HttpOnly", set_cookie)
                    self.assertIn("SameSite=Strict", set_cookie)
                    self.assertIn(advisorbot_server.COOKIE_NAME, set_cookie)

                    # Authenticated cookie reconnect succeeds for same-origin loopback
                    req_rec = urllib.request.Request(base + "/api/auth/reconnect", method="POST", data=b"{}")
                    req_rec.add_header("Origin", base)
                    req_rec.add_header("Sec-Fetch-Site", "same-origin")
                    req_rec.add_header("Content-Type", "application/json")
                    req_rec.add_header("X-Requested-With", "AdvisorBot")
                    req_rec.add_header("Cookie", set_cookie.split(";", 1)[0])
                    with urllib.request.urlopen(req_rec, timeout=5) as resp_rec:
                        self.assertEqual(resp_rec.status, 200)

                    # Cross-site reconnect attempt must be blocked (403) even with cookie
                    req_bad = urllib.request.Request(base + "/api/auth/reconnect", method="POST", data=b"{}")
                    req_bad.add_header("Origin", "https://malicious.site")
                    req_bad.add_header("Sec-Fetch-Site", "cross-site")
                    req_bad.add_header("Cookie", f"{advisorbot_server.COOKIE_NAME}=test-ephemeral-token-xyz")
                    with self.assertRaises(urllib.error.HTTPError) as cm_bad:
                        urllib.request.urlopen(req_bad, timeout=5)
                    self.assertEqual(cm_bad.exception.code, 403)
                finally:
                    srv.shutdown()
                    srv.server_close()


class TestDataMinimizationAndIngestHardening(unittest.TestCase):
    """SA-07, SA-08, SA-13, SA-15, SA-18, SA-25, SA-26."""

    def test_sa07_demo_projection_masks_dob_paths_and_saves_dir(self) -> None:
        cfg = _synthetic_privacy_cfg()
        cfg["demo_mode"] = {
            "enabled": True,
            "household_label": "Sample Household",
            "scale_factor": 1.0,
        }
        payload = {
            "household_name": "Monroe",
            "project": {
                "investors": [{"name": "Alex Monroe", "birth_date": "1979-08-14"}],
                "family_members": [{"name": "Alex Monroe", "birth_date": "1979-08-14"}],
            },
            "ssn_model": {
                "person": {"name": "Alex Monroe", "birth_iso": "1979-08-14", "birth_day": 14, "external_file": "/Users/owner/Docs/ssa.xml"},
                "family_birthdays": [{"name": "Alex Monroe", "birth_date": "1979-08-14"}],
            },
            "saves_bundle": {"saves_dir": "/Users/owner/Portfolios/monroe/Scenarios", "active_save": "base", "saves": []},
        }
        out = apply_demo_projection_to_payload(payload, cfg)
        self.assertEqual(out["project"]["investors"][0]["birth_date"], "1979-08-01")
        self.assertEqual(out["project"]["family_members"][0]["birth_date"], "1979-08-01")
        self.assertEqual(out["ssn_model"]["person"]["birth_iso"], "1979-08-01")
        self.assertEqual(out["ssn_model"]["person"]["birth_day"], 1)
        self.assertEqual(out["ssn_model"]["person"]["external_file"], "ssa.xml")
        self.assertEqual(out["ssn_model"]["family_birthdays"][0]["birth_date"], "1979-08-01")
        self.assertEqual(out["saves_bundle"]["saves_dir"], "")

        tr = DemoTranslator(cfg)
        red = tr.map_text("Saved at /" + "Users/alexm/Secret/file.txt and C:\\Users\\alexm\\doc.pdf")
        self.assertNotIn("alexm", red)

    def test_sa08_account_masking_and_report_minimization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "account_map.json"
            m1 = account_masking.mask_account_id("6420-8114", map_path=map_path)
            m2 = account_masking.mask_account_id("6190-2205", map_path=map_path)
            c1 = account_masking.mask_contract_id("C99T4012881", map_path=map_path)
            self.assertEqual(m1, "ACCT-01")
            self.assertEqual(m2, "ACCT-02")
            self.assertEqual(c1, "ANN-01")
            self.assertEqual(account_masking.mask_account_id("6420-8114", map_path=map_path), "ACCT-01")
            mode = stat.S_IMODE(os.stat(map_path).st_mode)
            self.assertEqual(mode, 0o600)

            with patch("ingest.account_masking.resolve_account_map_path", return_value=map_path):
                self.assertEqual(
                    account_masking.resolve_target_account_id("6420-8114", ["ACCT-01", "ACCT-02"]),
                    "ACCT-01",
                )

    def test_sa08_view_model_dob_and_ssn_minimization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "portfolio.sqlite"
            db = PortfolioDB(db_path)
            try:
                cfg = {
                    "household_name": "Monroe Family",
                    "investors": [
                        {
                            "id": "inv_1",
                            "name": "Alex Monroe",
                            "birth_date": "1979-08-14",
                            "ssn": {"external_file": "/Users/owner/Private/ssa.xml", "annual_earnings": {"2024": 150000}},
                        }
                    ],
                    "beneficiaries": [{"id": "ben_1", "name": "Rowan Monroe", "birth_date": "2011-04-12"}],
                    "accounts": {},
                }
                payload = view_model.build_report_payload(db, cfg, date(2026, 1, 15))
            finally:
                db.close()
        inv = payload["project"]["investors"][0]
        self.assertEqual(inv["birth_date"], "1979-08")
        self.assertEqual(inv["ssn"]["external_file"], "ssa.xml")
        self.assertEqual(inv["ssn"]["annual_earnings"], {})
        self.assertEqual(payload["ssn_model"]["investors"][0]["birth_iso"], "1979-08")
        self.assertEqual(payload["ssn_model"]["investors"][0]["birth_day"], 1)
        self.assertEqual(payload["ssn_model"]["investors"][0]["external_file"], "ssa.xml")
        self.assertEqual(payload["ssn_model"]["investors"][0]["history"], [])
        self.assertEqual(payload["saves_bundle"]["saves_dir"], "")

    def test_sa13_sa18_xml_xxe_and_path_confinement(self) -> None:
        with self.assertRaises(ValueError):
            ssa_xml._safe_parse_xml('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root/>')

        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "p1"
            pdir.mkdir()
            outside = Path(tmp) / "outside.xml"
            outside.write_text("<osss:OnlineSocialSecurityStatementData/>", encoding="utf-8")
            with patch("ingest.ssa_xml.get_portfolio_dir", return_value=pdir):
                res = ssa_xml.parse_ssa_xml_path(str(outside), portfolio_id="p1")
            self.assertFalse(res.get("ok"))

    def test_sa18_zip_bomb_and_symlink_guards(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("../escape.xml", b"<x/>")
            zf.writestr("ok.xml", b"A" * 2048)
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            with self.assertRaises(ValueError):
                ms_activity_xlsx._read_zip_member_limited(zf, "../escape.xml", max_bytes=4096)
            with self.assertRaises(ValueError):
                ms_activity_xlsx._read_zip_member_limited(zf, "ok.xml", max_bytes=1024)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdir = root / "Statements"
            sdir.mkdir()
            secret = root / "outside.txt"
            secret.write_text("secret", encoding="utf-8")
            (sdir / "real.txt").write_text("ok", encoding="utf-8")
            link = sdir / "link.txt"
            os.symlink(secret, link)
            with PortfolioDB(root / "portfolio.sqlite") as db:
                _, found = scanner.scan_statement_files(sdir, db)
            rels = [f.rel_path for f in found]
            self.assertEqual(rels, [])

            # Also verify symlinked supported extension (.pdf) is skipped
            pdf_secret = root / "outside.pdf"
            pdf_secret.write_bytes(b"%PDF-1.4")
            (sdir / "real.pdf").write_bytes(b"%PDF-1.4")
            os.symlink(pdf_secret, sdir / "link.pdf")
            with PortfolioDB(root / "portfolio2.sqlite") as db2:
                _, found_pdfs = scanner.scan_statement_files(sdir, db2)
            self.assertEqual([f.rel_path for f in found_pdfs], ["real.pdf"])

    def test_sa15_custom_plugin_opt_in_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "Portfolios" / "demo" / "Plugins"
            pdir.mkdir(parents=True)
            (pdir / "evil.py").write_text("raise RuntimeError('should not execute')\n", encoding="utf-8")
            with patch("ingest.plugins.get_portfolios_dir", return_value=Path(tmp) / "Portfolios"):
                loaded = plugins.load_custom_plugins_from_dir(pdir, cfg={})
            self.assertEqual(loaded, [])

    def test_sa25_html_report_escaping_and_0600_perms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "report.html"
            html_report.render_html_report(
                {
                    "household_name": '<script>alert("xss")</script>',
                    "generated_at": '2026-01-01" onmouseover="alert(1)',
                    "badge_image": "javascript:alert(3)",
                    "has_portfolio": True,
                },
                out_path,
            )
            txt = out_path.read_text(encoding="utf-8")
            head_section = txt.split("<style>", 1)[0]
            self.assertIn('<link rel="icon" href="" />', head_section)
            self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", head_section)
            self.assertNotIn("javascript:alert(3)", txt.split("<script>", 1)[0])
            self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)

    def test_sa26_cli_statement_redaction(self) -> None:
        sample = "Acct 6420-8114 SSN 123-45-6789 ID MSX904C112 Contract C99T4012881 Val $1250000 Ref 987654321"
        red = holdings._redact_cli_statement_text(sample)
        self.assertNotIn("6420-8114", red)
        self.assertNotIn("123-45-6789", red)
        self.assertNotIn("MSX904C112", red)
        self.assertNotIn("C99T4012881", red)
        self.assertNotIn("987654321", red)
        self.assertIn("$1250000", red)


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
