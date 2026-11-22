#!/usr/bin/env python3
"""
In-process stand-in for the cloud LLM providers.

`llm_tools` funnels every provider call through one helper, `_http_post_json(url, headers,
payload)`, so patching that gives the suite a complete record of what would have left the
machine: URL, headers (including any API key) and the full request body.

That record is what the privacy tests assert against. Checking the shield by inspecting
the translator alone would miss leaks that happen after translation (dict keys, tool
names, error strings baked into a prompt), so the tests scan the serialised wire body.

Usage:

    with fake_llm.recording() as wire:
        result = llm_tools.run_chat_via_agy_cli("What is my balance?", model="llm:claude:...")
    assert wire.requests, "no provider call captured"
    assert not wire.leaked_terms(registry_terms)
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


@dataclass
class WireRequest:
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]

    @property
    def provider(self) -> str:
        if "anthropic" in self.url:
            return "claude"
        if "googleapis" in self.url or "generativelanguage" in self.url:
            return "gemini"
        if "openai" in self.url or "/chat/completions" in self.url:
            return "openai"
        return "unknown"

    def serialized(self) -> str:
        """Everything that would cross the wire, as one string (URL + headers + body)."""
        return json.dumps(
            {"url": self.url, "headers": self.headers, "payload": self.payload},
            sort_keys=True,
            default=str,
        )


@dataclass
class WireLog:
    requests: list[WireRequest] = field(default_factory=list)
    responder: Callable[[WireRequest], dict[str, Any]] | None = None

    def serialized(self) -> str:
        return json.dumps([json.loads(r.serialized()) for r in self.requests], sort_keys=True)

    def body_text(self) -> str:
        """Just the request bodies, for leak scans that should ignore URLs and keys."""
        return json.dumps([r.payload for r in self.requests], sort_keys=True, default=str)

    def keys(self) -> set[str]:
        """Every dict key appearing anywhere in any request body."""
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    found.add(str(key))
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        for req in self.requests:
            walk(req.payload)
        return found

    def leaked_terms(self, terms: list[str]) -> list[str]:
        from . import scans

        return scans.find_terms(self.serialized(), terms)


def text_response(provider: str, text: str = "Synthetic reply.") -> dict[str, Any]:
    """A minimal, tool-free provider response in each vendor's own shape."""
    if provider == "gemini":
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}
    if provider == "claude":
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if provider == "openai":
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}
    return {"ok": True, "text": text}


def tool_call_response(provider: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """A provider response that asks for one tool call, in each vendor's own shape."""
    if provider == "gemini":
        return {
            "candidates": [
                {"content": {"role": "model", "parts": [{"functionCall": {"name": tool_name, "args": args}}]}}
            ]
        }
    if provider == "claude":
        return {
            "content": [{"type": "tool_use", "id": "toolu_fake_1", "name": tool_name, "input": args}],
            "stop_reason": "tool_use",
        }
    if provider == "openai":
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_fake_1",
                                "type": "function",
                                "function": {"name": tool_name, "arguments": json.dumps(args)},
                            }
                        ],
                    }
                }
            ]
        }
    return {"ok": True}


def scripted(*responses: dict[str, Any]) -> Callable[[WireRequest], dict[str, Any]]:
    """Returns each response in turn, then a plain text reply for the provider in use."""
    queue = list(responses)

    def respond(req: WireRequest) -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        return text_response(req.provider)

    return respond


@contextlib.contextmanager
def recording(
    responder: Callable[[WireRequest], dict[str, Any]] | None = None,
) -> Iterator[WireLog]:
    """Captures every provider request and answers with `responder` (default: a text reply)."""
    import llm_tools

    log = WireLog(responder=responder)
    original = llm_tools._http_post_json

    def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 150):
        req = WireRequest(url=str(url), headers=dict(headers or {}), payload=json.loads(json.dumps(payload, default=str)))
        log.requests.append(req)
        if log.responder is not None:
            return log.responder(req)
        return text_response(req.provider)

    llm_tools._http_post_json = fake_post  # type: ignore[assignment]
    try:
        yield log
    finally:
        llm_tools._http_post_json = original  # type: ignore[assignment]


def install_config(root, provider: str = "claude", *, model: str = "test-model", api_key: str = "sk-test-not-a-real-key") -> dict:
    """
    Writes a provider config into a fixture root's `Data/Config/LLM/` so the model
    selector has something to resolve, without any real credential.
    """
    import json as _json
    from pathlib import Path

    cfg = {
        "id": provider,
        "provider": provider,
        "display_name": f"Fake {provider.title()}",
        "api_key": api_key,
        "model": model,
        "models": [
            {
                "id": f"llm:{provider}:{model}",
                "raw_id": provider,
                "provider": provider,
                "model": model,
                "display_name": f"Fake {provider.title()} {model}",
                "is_default": True,
            }
        ],
        "max_tool_rounds": 2,
    }
    target = Path(root) / "Data" / "Config" / "LLM" / f"{provider}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg
