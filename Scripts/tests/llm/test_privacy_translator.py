#!/usr/bin/env python3
"""
Tests for Privacy Shield (Case B) and Demo Mode (Case A) using synthetic fixture entities:
  - Entity auto-discovery & longest-match-first bidirectional translation (`PrivacyTranslator`)
  - D1 Full product/carrier/equity/fund aliasing vs mechanics preservation
  - D2 Composite sim_asset ID & forecast_key 1-to-1 reversible round-trip
  - D3 In-app Chat Shield vs unrestricted terminal CLI, Local-Only loopback check, & mocked Cloud API loop
  - D4 + D5 Demo Mode deterministic personas, value scaling `k`, hidden `k` badge, & HTTP write guard
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
import urllib.error
import urllib.request

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm_tools  # noqa: E402
from privacy import (  # noqa: E402
    apply_demo_projection_to_payload,
    assign_demo_personas,
    build_translator,
    default_privacy_config,
)
import advisorbot_server  # noqa: E402


def _sample_registry_cfg() -> dict:
    cfg = default_privacy_config()
    cfg["entities"] = [
        {
            "token": "HOUSEHOLD_1",
            "category": "household",
            "canonical": "Rivera Portfolio",
            "synonyms": ["Rivera Portfolio", "Rivera Household", "Rivera"],
            "enabled": True,
            "meta": {"surname": "Rivera"},
        },
        {
            "token": "TRUST_1",
            "category": "trust",
            "canonical": "Rivera Living Trust",
            "synonyms": ["Rivera Living Trust", "Living Trust"],
            "enabled": True,
        },
        {
            "token": "INVESTOR_1",
            "category": "investor",
            "canonical": "Jordan",
            "synonyms": ["Jordan P. Rivera", "Jordan Rivera", "Jordans", "Jordan", "jordan"],
            "enabled": True,
            "meta": {"first": "Jordan", "birth_date": "1975-03-10"},
        },
        {
            "token": "INVESTOR_2",
            "category": "investor",
            "canonical": "Taylor",
            "synonyms": ["Taylor Rivera", "Taylor"],
            "enabled": True,
            "meta": {"first": "Taylor", "birth_date": "1976-08-20"},
        },
        {
            "token": "BENEFICIARY_1",
            "category": "beneficiary",
            "canonical": "Casey",
            "synonyms": ["Casey Rivera", "Casey"],
            "enabled": True,
            "meta": {"first": "Casey", "birth_date": "2006-02-12"},
        },
        {
            "token": "BANK_1",
            "category": "custodian",
            "canonical": "Harbor Brokerage",
            "synonyms": ["Harbor Brokerage Services", "Harbor Brokerage", "HBS", "HB"],
            "enabled": True,
        },
        {
            "token": "BANK_2",
            "category": "custodian",
            "canonical": "Milestone Securities",
            "synonyms": ["Milestone Stock Plan", "Milestone Securities", "MS"],
            "enabled": True,
        },
        {
            "token": "ACCT_1",
            "category": "account",
            "canonical": "MSP100A900",
            "synonyms": ["MSP100A900"],
            "enabled": True,
            "meta": {"bank_token": "BANK_2"},
        },
        {
            "token": "ACCT_2",
            "category": "account",
            "canonical": "7100-1001",
            "synonyms": ["7100-1001", "7100_1001"],
            "enabled": True,
            "meta": {"bank_token": "BANK_1"},
        },
        {
            "token": "ACCT_3",
            "category": "account",
            "canonical": "7100-2002",
            "synonyms": ["7100-2002", "7100_2002"],
            "enabled": True,
            "meta": {"bank_token": "BANK_1"},
        },
        {
            "token": "STOCK_1",
            "category": "equity",
            "canonical": "ACME",
            "synonyms": ["Acme Corp Class A", "Acme Equity Plan", "Acme Corp", "ACME", "RSU"],
            "enabled": True,
        },
        {
            "token": "STOCK_2",
            "category": "equity",
            "canonical": "WDGT",
            "synonyms": ["Widget Labs", "WDGT"],
            "enabled": True,
        },
        {
            "token": "FUND_1",
            "category": "fund",
            "canonical": "VTSAX",
            "synonyms": ["Vanguard Total Stock", "VTSAX"],
            "enabled": True,
        },
        {
            "token": "MGR_1",
            "category": "manager",
            "canonical": "ApexDirect",
            "synonyms": ["ApexDirect Core", "ApexDirect"],
            "enabled": True,
        },
        {
            "token": "CARRIER_1",
            "category": "carrier",
            "canonical": "SLI",
            "synonyms": ["Summit Life Horizon", "Summit Life", "SLI"],
            "enabled": True,
        },
        {
            "token": "CONTRACT_1",
            "category": "contract",
            "canonical": "SL9001001",
            "synonyms": ["CONTRACT:SL9001001", "SL9001001"],
            "enabled": True,
        },
        {
            "token": "SCENARIO_1",
            "category": "scenario",
            "canonical": "Jordans Baseline",
            "synonyms": ["001_jordans_baseline.json", "Jordans Baseline"],
            "enabled": True,
        },
    ]
    cfg["entities"] = assign_demo_personas(cfg["entities"], demo_seed="demo-1001", portfolio_slug="test_pf")
    return cfg


class PrivacyTranslatorTests(unittest.TestCase):
    def test_longest_match_boundaries_and_short_code_casing(self):
        tr = build_translator(_sample_registry_cfg())
        text = (
            "Consolidate Harbor Brokerage Services and HBS ACME in 7100-1001 with MS MSP100A900. "
            "Check 5 ms network latency and ApexDirect Core 200-stock SMA. Also check Taylor and Jordan's Roth."
        )
        wire = tr.to_alias_text(text)
        self.assertIn("BANK_1 and BANK_1 STOCK_1 in ACCT_2 with BANK_2 ACCT_1.", wire)
        self.assertIn("5 ms network latency", wire)  # lowercase "ms" MUST NOT become BANK_2
        self.assertIn("MGR_1 200-stock SMA", wire)
        self.assertIn("INVESTOR_2 and INVESTOR_1's Roth", wire)
        self.assertEqual(tr.find_leaked_terms(wire), [])

        restored = tr.from_alias_text(wire)
        self.assertIn("Harbor Brokerage", restored)
        self.assertIn("Milestone Securities", restored)
        self.assertIn("Jordan's Roth", restored)
        self.assertIn("ApexDirect", restored)

    def test_d1_d2_composite_ids_dob_and_prompt2_roundtrip(self):
        tr = build_translator(_sample_registry_cfg())
        payload = {
            "family_birthdays": [
                {"name": "Jordan", "y": 1975, "m": 3, "d": 10},
                {"name": "Casey", "y": 2006, "m": 2, "d": 12},
            ],
            "simulator_assets": [
                {"id": "hbs_acme_7100_1001", "forecast_key": "single_7100-1001_ACME"},
                {"id": "sma_7100_2002", "forecast_key": "sma_7100_2002"},
                {"id": "ann_sli_SL9001001", "forecast_key": "CONTRACT:SL9001001"},
            ],
            "settings": {
                "simAnnualBurn": 120000,  # True unscaled USD (Case B k=1.0)
                "simAssetOrder": ["sma_7100_2002", "hbs_acme_7100_1001"],
                "holdingForecasts": {
                    "single_7100-1001_ACME": {"growth_pct": 8.0, "risk_class": "single_stock"},
                    "sma_7100_2002": {"growth_pct": 7.5, "risk_class": "broad_equity"},
                },
            },
            "models": [
                {
                    "id": "001",
                    "name": "Jordans Baseline",
                    "filename": "001_jordans_baseline.json",
                    "notes_md": "## Upvotes & Concurrences\n- SLI Horizon 5 FIA contract SL9001001 protects Casey.",
                }
            ],
        }
        wire = tr.to_alias(payload)
        self.assertEqual(tr.find_leaked_terms(wire), [])
        self.assertEqual(wire["family_birthdays"][0], {"name": "INVESTOR_1", "y": 1975, "m": 3, "d": 1})
        self.assertEqual(wire["settings"]["simAnnualBurn"], 120000)
        self.assertIn("BANK_1_STOCK_1_ACCT_2", wire["settings"]["simAssetOrder"])
        self.assertIn("sma_ACCT_3", wire["settings"]["simAssetOrder"])
        self.assertIn("ann_CARRIER_1_CONTRACT_1", json.dumps(wire))
        self.assertIn("FIA", json.dumps(wire))

        restored = tr.from_alias(wire)
        self.assertEqual(restored["settings"]["simAssetOrder"], ["sma_7100_2002", "hbs_acme_7100_1001"])
        self.assertIn("single_7100-1001_ACME", restored["settings"]["holdingForecasts"])
        self.assertIn("ann_sli_SL9001001", json.dumps(restored))

    def test_dob_age_only_and_notes_strip_policies(self):
        cfg = _sample_registry_cfg()
        cfg["ai_shield"]["dob_granularity"] = "age_only"
        cfg["ai_shield"]["notes_policy"] = "strip"
        cfg["ai_shield"]["alias_products_and_tickers"] = False
        tr = build_translator(cfg)

        wire = tr.to_alias(
            {
                "investors": [{"name": "Jordan", "birth_date": "1975-03-10"}],
                "notes_md": "Confidential estate note for HBS ACME",
            }
        )
        self.assertIsInstance(wire["investors"][0]["birth_date"], int)
        self.assertEqual(wire["notes_md"], "[Redacted by Privacy Shield]")

    def test_identity_and_values_mode_scales_usd_and_shifts_birthdays(self):
        cfg = _sample_registry_cfg()
        cfg["ai_shield"]["mode"] = "identity_and_values"
        cfg["ai_shield"]["dob_granularity"] = "shift_years"
        cfg["ai_shield"]["scale_factor"] = 0.25
        cfg["ai_shield"]["birthday_shift_years"] = 5
        tr = build_translator(cfg)

        text = "Jordan (1975-03-10) holds $800,000 of ACME at Harbor Brokerage."
        wire_text = tr.to_alias_text(text)
        self.assertIn("INVESTOR_1", wire_text)
        self.assertIn("1980-03-01", wire_text)
        self.assertIn("$200,000", wire_text)
        self.assertNotIn("$800,000", wire_text)
        self.assertNotIn("1975-03-10", wire_text)
        self.assertEqual(tr.find_leaked_terms(wire_text), [])

        # Per DP-03: from_alias_text restores entity & DOB tokens while keeping free-form '$' prose
        # untouched (so statutory tax thresholds like $250,000 NIIT are never multiplied by 1/k).
        restored_text = tr.from_alias_text(wire_text)
        self.assertEqual(
            restored_text,
            "Jordan (1975-03-10) holds $200,000 of ACME at Harbor Brokerage.",
        )

        wire_payload = tr.to_alias({
            "family_birthdays": [{"name": "Jordan", "y": 1975, "m": 3, "d": 10}],
            "settings": {"simAnnualBurn": 120000},
        })
        self.assertEqual(wire_payload["family_birthdays"][0], {"name": "INVESTOR_1", "y": 1980, "m": 3, "d": 1})
        self.assertEqual(wire_payload["settings"]["simAnnualBurn"], 30000)

        restored_payload = tr.from_alias(wire_payload)
        self.assertEqual(restored_payload["family_birthdays"][0]["y"], 1975)
        self.assertEqual(restored_payload["settings"]["simAnnualBurn"], 120000)

        with mock.patch("llm_tools.load_privacy_config", return_value=cfg):
            err, shield_tr, mode = llm_tools._resolve_inapp_privacy_shield(False, {"provider": "gemini"})
            self.assertIsNone(err)
            self.assertIsNotNone(shield_tr)
            self.assertEqual(mode, "identity_and_values")


class ShieldChatBoundaryTests(unittest.TestCase):
    def test_d3_blocks_inapp_cli_and_remote_under_local_only(self):
        cfg = _sample_registry_cfg()
        with mock.patch("llm_tools.load_privacy_config", return_value=cfg):
            err, tr, mode = llm_tools._resolve_inapp_privacy_shield(True, {})
            self.assertIsNotNone(err)
            self.assertFalse(err["ok"])
            self.assertIn("Privacy Shield", err["error"])
            self.assertIsNone(tr)
            self.assertEqual(mode, "identity_aliased")

        cfg["ai_shield"]["mode"] = "local_only"
        with mock.patch("llm_tools.load_privacy_config", return_value=cfg):
            err_remote, _, _ = llm_tools._resolve_inapp_privacy_shield(
                False, {"provider": "openai", "api_base_url": "https://api.openai.com/v1/chat/completions"}
            )
            self.assertIsNotNone(err_remote)
            self.assertIn("Local-Only", err_remote["error"])

            err_local, tr_local, mode_local = llm_tools._resolve_inapp_privacy_shield(
                False, {"provider": "openai", "api_base_url": "http://127.0.0.1:11434/v1/chat/completions"}
            )
            self.assertIsNone(err_local)
            self.assertIsNone(tr_local)
            self.assertEqual(mode_local, "local_only")

    def test_no_agy_entries_and_multi_model_catalog_expansion(self):
        repo_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmpdir:
            llm_dir = Path(tmpdir) / "LLM"
            llm_dir.mkdir(parents=True)
            for prov in ("gemini", "claude", "openai"):
                raw = json.loads((repo_root / "Examples" / f"llm_config.{prov}.json").read_text(encoding="utf-8"))
                raw["api_key"] = f"test-{prov}-key"
                (llm_dir / f"{prov}.json").write_text(json.dumps(raw), encoding="utf-8")

            with (
                mock.patch("llm_tools.get_llm_dir", return_value=llm_dir),
                mock.patch("llm_tools.get_portfolio_dir", return_value=None),
                mock.patch("llm_tools.load_privacy_config", return_value=_sample_registry_cfg()),
            ):
                models = llm_tools.list_available_models(probe=False)
                self.assertFalse(any(m.get("provider") == "agy" or m.get("type") == "cli" for m in models))
                ids = {m["id"] for m in models}
                self.assertIn("llm:gemini:gemini-2.5-flash", ids)
                self.assertIn("llm:gemini:gemini-2.5-pro", ids)
                self.assertIn("llm:claude:claude-opus-4-6", ids)
                self.assertIn("llm:openai:gpt-4.1", ids)
                self.assertIn("llm:openai:o3", ids)

                chosen = llm_tools.load_llm_config("llm:gemini:gemini-2.5-flash")
                self.assertEqual(chosen["provider"], "gemini")
                self.assertEqual(chosen["model"], "gemini-2.5-flash")
                self.assertIn("Gemini 2.5 Flash", chosen["display_name"])

    def test_gemini_api_roundtrip_aliases_egress_and_restores_tool_args_and_reply(self):
        cfg = _sample_registry_cfg()
        seen_wire_requests = []
        captured_tool_calls = []

        def fake_http_post(_url, _headers, body):
            seen_wire_requests.append(json.loads(json.dumps(body)))
            if len(seen_wire_requests) == 1:
                return {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "holdings__manage_saves",
                                            "args": {
                                                "action": "upvote",
                                                "id": "SCENARIO_1",
                                                "reason": "Concur keeping INVESTOR_1's BANK_1 STOCK_1 in ACCT_2.",
                                            },
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "Upvoted SCENARIO_1 for INVESTOR_1's STOCK_1 at BANK_1 (ACCT_2)."
                                }
                            ]
                        }
                    }
                ]
            }

        def fake_exec_tool(name, args):
            captured_tool_calls.append((name, args))
            return {"ok": True, "upvoted_id": "001", "upvoted_save": "Jordans Baseline"}

        tr = build_translator(cfg)
        with (
            mock.patch("llm_tools._http_post_json", side_effect=fake_http_post),
            mock.patch("llm_tools.execute_named_tool", side_effect=fake_exec_tool),
        ):
            res = llm_tools._run_chat_gemini_api(
                "Please upvote Jordans Baseline for Jordan's ACME at Harbor Brokerage 7100-1001.",
                {"provider": "gemini", "model": "gemini-2.5-pro", "api_key": "dummy"},
                "",
                0,
                translator=tr,
            )

        self.assertTrue(res["ok"])
        wire_dump = json.dumps(seen_wire_requests)
        self.assertEqual(tr.find_leaked_terms(wire_dump), [])
        self.assertNotIn("Jordans Baseline", wire_dump)
        self.assertNotIn("ACME", wire_dump)
        self.assertIn("INVESTOR_1", wire_dump)
        self.assertIn("BANK_1", wire_dump)
        # De-aliased BEFORE touching execute_named_tool
        self.assertEqual(captured_tool_calls[0][1]["id"], "Jordans Baseline")
        self.assertIn("Jordan's Harbor Brokerage ACME in 7100-1001", captured_tool_calls[0][1]["reason"])
        # UI Reply restored cleanly
        self.assertIn("Jordan's ACME at Harbor Brokerage (7100-1001)", res["reply"])


class DemoProjectorTests(unittest.TestCase):
    def test_demo_personas_never_collide_with_real_names_and_hide_k(self):
        cfg = _sample_registry_cfg()
        cfg["demo_mode"]["enabled"] = True
        cfg["demo_mode"]["scale_factor"] = 0.25
        cfg["demo_mode"]["round_to"] = 1000

        real_terms = {
            str(s).lower()
            for ent in cfg["entities"]
            for s in ([ent["canonical"]] + list(ent.get("synonyms") or []))
            if len(str(s)) >= 3
        }
        for ent in cfg["entities"]:
            dv = str(ent.get("demo_value") or "").lower()
            self.assertTrue(dv)
            self.assertNotIn(dv, real_terms)

        # Shared BANK_1 clustering: ACCT_2 and ACCT_3 share the same custodian prefix
        accts = {e["token"]: e for e in cfg["entities"] if e["category"] == "account"}
        self.assertEqual(accts["ACCT_2"]["demo_meta"]["bank_code"], accts["ACCT_3"]["demo_meta"]["bank_code"])
        self.assertNotEqual(accts["ACCT_1"]["demo_meta"]["bank_code"], accts["ACCT_2"]["demo_meta"]["bank_code"])

        payload = {
            "household_name": "Rivera Portfolio",
            "badge_image": "Portfolios/x/Assets/custom.png",
            "primary_symbol": "ACME",
            "symbols": ["ACME", "WDGT"],
            "kpis": {"total_vested": 2_000_000.0, "goog_vested_shares": 1000.0},
            "timeline": [{"iso": "2026-11-01", "vestedTotal": 2_000_000.0, "accounts": {"7100-1001": 500_000.0}}],
            "holdings_by_account": [
                {
                    "account_id": "7100-1001",
                    "institution": "Harbor Brokerage",
                    "title": "Rivera Living Trust — Brokerage (7100-1001)",
                    "current_value": 500_000.0,
                    "principal_str": "$400,000",
                    "pct_portfolio": 25.0,
                }
            ],
            "retirement_sim_assets": [
                {"id": "hbs_acme_7100_1001", "forecast_key": "single_7100-1001_ACME", "current_value": 500_000.0, "default_growth_pct": 8.0}
            ],
        }
        demo = apply_demo_projection_to_payload(payload, cfg)
        self.assertEqual(demo["badge_image"], "Assets/default_badge.svg")
        self.assertAlmostEqual(demo["kpis"]["total_vested"], 500_000.0)
        self.assertAlmostEqual(demo["kpis"]["goog_vested_shares"], 250.0)
        self.assertEqual(demo["holdings_by_account"][0]["pct_portfolio"], 25.0)
        self.assertEqual(demo["holdings_by_account"][0]["principal_str"], "$100,000")
        self.assertEqual(demo["retirement_sim_assets"][0]["default_growth_pct"], 8.0)
        eq_map = {e["token"]: e["demo_value"] for e in cfg["entities"] if e["category"] == "equity"}
        self.assertNotIn(eq_map["STOCK_1"], ("ACME", "WDGT"))
        self.assertNotIn(eq_map["STOCK_2"], ("ACME", "WDGT"))
        self.assertEqual(demo["symbols"], [eq_map["STOCK_1"], eq_map["STOCK_2"]])
        # D4: k must not leak in privacy_status
        self.assertEqual(demo["privacy_status"], {"demo_enabled": True, "shield_mode": "identity_aliased"})
        self.assertNotIn("0.25", json.dumps(demo["privacy_status"]))


class PrivacyServerIntegrationTests(unittest.TestCase):
    def test_privacy_endpoints_and_demo_write_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdir = root / "Portfolios" / "alpha"
            (pdir / "Scenarios").mkdir(parents=True)
            (root / "Portfolios" / "_active.json").write_text(json.dumps({"active_portfolio_id": "alpha"}), encoding="utf-8")
            (pdir / "project.json").write_text(
                json.dumps(
                    {
                        "household_name": "Alpha Test Portfolio",
                        "primary_equity_symbol": "ACME",
                        "symbols": ["ACME", "WDGT"],
                        "investors": [{"id": "jordan_real", "name": "MorganReal", "birth_date": "1975-05-15"}],
                        "beneficiaries": [{"id": "kid_real", "name": "TinyKid", "birth_date": "2010-09-01"}],
                    }
                ),
                encoding="utf-8",
            )
            (pdir / "Scenarios" / "001_default.json").write_text(
                json.dumps({"id": "001", "name": "Default", "settings": {"simAnnualBurn": 120000}}),
                encoding="utf-8",
            )
            (pdir / "Scenarios" / "_index.json").write_text(
                json.dumps({"version": 1, "active_id": "001", "models": [{"id": "001", "name": "Default", "file": "001_default.json"}]}),
                encoding="utf-8",
            )

            with (
                mock.patch.object(advisorbot_server, "PROJECT_ROOT", root),
                mock.patch("config.HOLDINGS_ROOT", root),
                mock.patch("config.ADVISORBOT_ROOT", root),
                mock.patch.dict("os.environ", {"ADVISORBOT_ROOT": str(root)}),
            ):
                advisorbot_server.invalidate_portfolio_cache() if hasattr(advisorbot_server, "invalidate_portfolio_cache") else None
                server = ThreadingHTTPServer(("127.0.0.1", 0), advisorbot_server.SavesRequestHandler)
                port = server.server_address[1]
                th = threading.Thread(target=server.serve_forever, daemon=True)
                th.start()
                try:
                    tok = advisorbot_server.SESSION_TOKEN
                    base = f"http://127.0.0.1:{port}"

                    def _req(path, method="GET", payload=None):
                        data = json.dumps(payload).encode("utf-8") if payload is not None else None
                        headers = {"X-Session-Token": tok}
                        if data is not None:
                            headers["Content-Type"] = "application/json"
                        r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
                        try:
                            with urllib.request.urlopen(r, timeout=5) as resp:
                                return resp.status, json.loads(resp.read().decode("utf-8"))
                        except urllib.error.HTTPError as e:
                            with e:
                                return e.code, json.loads(e.read().decode("utf-8"))

                    st, prv = _req("/api/privacy?portfolio_id=alpha")
                    self.assertEqual(st, 200)
                    self.assertTrue(prv["ok"])
                    tokens = {e["token"] for e in prv["privacy"]["entities"]}
                    self.assertIn("INVESTOR_1", tokens)
                    self.assertIn("BENEFICIARY_1", tokens)

                    st, prev_res = _req(
                        "/api/privacy/preview",
                        method="POST",
                        payload={"portfolio_id": "alpha", "sample_text": "Ask MorganReal about ACME and TinyKid."},
                    )
                    self.assertEqual(st, 200)
                    self.assertTrue(prev_res["leak_free"])
                    self.assertIn("INVESTOR_1", prev_res["wire_text"])
                    self.assertIn("STOCK_1", prev_res["wire_text"])
                    self.assertEqual(prev_res["restored_text"], "Ask MorganReal about ACME and TinyKid.")

                    # Enable Demo Mode -> saves endpoint scales & POST /api/saves is write-locked (403)
                    st, upd = _req(
                        "/api/privacy",
                        method="POST",
                        payload={"portfolio_id": "alpha", "demo_mode": {"enabled": True, "scale_factor": 0.25}},
                    )
                    self.assertEqual(st, 200)
                    self.assertTrue(upd["privacy"]["demo_mode"]["enabled"])

                    st, saves_res = _req("/api/saves")
                    self.assertEqual(st, 200)
                    self.assertTrue(saves_res.get("demo_mode"))
                    self.assertEqual(saves_res["models"][0]["settings"]["simAnnualBurn"], 30000)

                    st_block, blk_res = _req(
                        "/api/saves",
                        method="POST",
                        payload={"name": "Bad Save", "settings": {"simAnnualBurn": 50000}},
                    )
                    self.assertEqual(st_block, 403)
                    self.assertTrue(blk_res.get("demo_blocked"))

                    # Turning Demo Mode back OFF via /api/privacy must always be allowed
                    st_off, off_res = _req(
                        "/api/privacy",
                        method="POST",
                        payload={"portfolio_id": "alpha", "demo_mode": {"enabled": False}},
                    )
                    self.assertEqual(st_off, 200)
                    self.assertFalse(off_res["privacy"]["demo_mode"]["enabled"])
                finally:
                    server.shutdown()
                    server.server_close()

    def test_demo_mode_retirement_goal_and_grant_id_masking(self):
        reg = _sample_registry_cfg()
        reg["demo_mode"] = {"enabled": True, "scale_factor": 0.25, "round_to": 1000}
        reg["entities"].append({
            "token": "GRANT_1",
            "category": "grant",
            "canonical": "R9001001",
            "synonyms": ["R9001001"],
            "demo_value": "GR-4411",
            "demo_meta": {"grant_id": "GR-4411"},
            "enabled": True,
        })
        reg["entities"].append({
            "token": "GRANT_2",
            "category": "grant",
            "canonical": "PRIOR-LOTS",
            "synonyms": ["PRIOR-LOTS"],
            "demo_value": "RET-PRIOR",
            "demo_meta": {"grant_id": "RET-PRIOR"},
            "enabled": True,
        })

        payload = {
            "retirement_annual_burn": 120000.0,
            "retirement_goal_years": 30,
            "retirement_goal_val": 3600000.0,
            "gsu_schedule": {
                "by_grant": [
                    {"grant_id": "R9001001", "label": "R9001001 (Ends Jun '28)", "gross_val": 40000.0},
                    {"grant_id": "R9002002", "label": "R9002002 (Ends Jun '28)", "gross_val": 20000.0},
                ],
            },
            "google_stock": {
                "grant_series": [
                    {"id": "PRIOR-LOTS", "label": "PRIOR-LOTS (Prior History)"},
                    {"id": "R9001001", "label": "R9001001 (Ends Jun '28)"},
                ],
                "release_bars": [
                    {
                        "vestDate": "2025-06-15",
                        "byGrant": {
                            "PRIOR-LOTS": {"val_today_gross": 10000.0, "val_today_net": 5000.0},
                            "R9001001": {"val_today_gross": 20000.0, "val_today_net": 10000.0},
                            "R9002002": {"val_today_gross": 8000.0, "val_today_net": 4000.0},
                        },
                    }
                ],
                "by_grant": [
                    {"grant_id": "R9001001", "label": "R9001001 (Ends Jun '28)"},
                ],
                "retired_grant_summary": {
                    "grant_id": "PRIOR-LOTS",
                    "gross_shares": 500.0,
                },
            },
            "stock_grants": {
                "by_symbol": {
                    "ACME": {
                        "grant_series": [
                            {"id": "R9001001", "label": "R9001001 (Ends Jun '28)"},
                        ],
                        "release_bars": [
                            {
                                "vestDate": "2025-06-15",
                                "byGrant": {
                                    "R9001001": {"val_today_gross": 20000.0},
                                }
                            }
                        ],
                        "by_grant": [
                            {"grant_id": "R9001001", "label": "R9001001 (Ends Jun '28)", "cadence_note": "Tranche #1 of R9001001"},
                        ],
                        "retired_grant_summary": {
                            "grant_id": "PRIOR-LOTS",
                        },
                    }
                }
            },
            "retirement_sim_assets": [
                {
                    "id": "asset_acme",
                    "tranches": [
                        {"grant_id": "R9001001", "vest_date": "2025-06-15"},
                        {"grant_id": "PRIOR-LOTS", "vest_date": "2024-06-15"},
                    ]
                }
            ]
        }

        demo_payload = apply_demo_projection_to_payload(payload, reg)

        # 1. Goal & Burn scaling
        self.assertEqual(demo_payload["retirement_annual_burn"], 30000.0)
        self.assertEqual(demo_payload["retirement_goal_val"], 900000.0)
        self.assertEqual(demo_payload["retirement_goal_years"], 30)

        # 2. Grant ID masking in gsu_schedule
        gsu_bg = demo_payload["gsu_schedule"]["by_grant"]
        self.assertEqual(gsu_bg[0]["grant_id"], "GR-4411")
        self.assertEqual(gsu_bg[0]["label"], "GR-4411 (Ends Jun '28)")
        # R9002002 was not in entities, so fallback mask applies (GR-xxxx)
        self.assertTrue(gsu_bg[1]["grant_id"].startswith("GR-"))
        self.assertNotIn("R9002002", gsu_bg[1]["grant_id"])

        # 3. Grant ID masking in google_stock
        gs = demo_payload["google_stock"]["grant_series"]
        self.assertEqual(gs[0]["id"], "RET-PRIOR")
        self.assertEqual(gs[1]["id"], "GR-4411")
        self.assertEqual(gs[1]["label"], "GR-4411 (Ends Jun '28)")

        rb_bg = demo_payload["google_stock"]["release_bars"][0]["byGrant"]
        self.assertIn("RET-PRIOR", rb_bg)
        self.assertIn("GR-4411", rb_bg)
        self.assertNotIn("PRIOR-LOTS", rb_bg)
        self.assertNotIn("R9001001", rb_bg)
        self.assertNotIn("R9002002", rb_bg)

        ret = demo_payload["google_stock"]["retired_grant_summary"]
        self.assertEqual(ret["grant_id"], "RET-PRIOR")

        # 4. stock_grants
        eq_map = {e["token"]: e["demo_value"] for e in reg["entities"] if e["category"] == "equity"}
        demo_sym = eq_map["STOCK_1"]
        sg_bg = demo_payload["stock_grants"]["by_symbol"][demo_sym]["by_grant"][0]
        self.assertEqual(sg_bg["grant_id"], "GR-4411")
        self.assertEqual(sg_bg["label"], "GR-4411 (Ends Jun '28)")
        self.assertIn("GR-4411", sg_bg["cadence_note"])

        # 5. tranches in retirement_sim_assets
        tranches = demo_payload["retirement_sim_assets"][0]["tranches"]
        self.assertEqual(tranches[0]["grant_id"], "GR-4411")
        self.assertEqual(tranches[1]["grant_id"], "RET-PRIOR")


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Ported from the pre-fixture suite (see Scripts/tests/README.md, "Porting notes").
# Marked `legacy`: kept for the coverage it provides, not as an example of the current
# conventions. Rewrite it in the tier style when it next needs changing.
import pytest as _pytest_legacy  # noqa: E402

pytestmark = _pytest_legacy.mark.legacy
