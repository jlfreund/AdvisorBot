#!/usr/bin/env python3
"""
Spawns the real sidecar server against a fixture root and talks to it over HTTP.

The server is started in a background thread inside the test process (not a subprocess),
so it shares the root switched in by `env.pointed_at` and needs no port negotiation with
a child. A free port is picked by binding port 0.

The client deliberately starts *unauthenticated* so auth tests can assert 403s; call
`client.authenticate()` to pick up the session token the way a local tool would.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surfaces 3xx responses instead of following them, so tests can inspect the hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


class Client:
    """
    Minimal HTTP client for the sidecar, with explicit control over auth and Host.

    Redirects are *not* followed: the cookie bootstrap answers 302 with a `Set-Cookie`,
    and a client that followed the hop would discard the header and report the final 403.
    """

    def __init__(self, port: int, *, host_header: str = "127.0.0.1") -> None:
        self.port = port
        self.host_header = host_header
        self.token: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def authenticate(self) -> str:
        """Reads the session token from the root's 0600 token file, like a local CLI tool."""
        import config

        token_path = config.get_session_token_file()
        self.token = token_path.read_text(encoding="utf-8").strip()
        return self.token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        authenticated: bool | None = None,
        host_header: str | None = None,
    ) -> Response:
        use_auth = self.token is not None if authenticated is None else authenticated
        data = None
        hdrs = {"Host": f"{host_header or self.host_header}:{self.port}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if use_auth:
            if self.token is None:
                self.authenticate()
            hdrs["X-Session-Token"] = str(self.token)
        hdrs.update(headers or {})
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=30) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as exc:
            # 3xx arrives here because redirects are deliberately not followed.
            return Response(exc.code, exc.read(), dict(exc.headers or {}))

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Response:
        return self.request("POST", path, body=body if body is not None else {}, **kw)


@contextlib.contextmanager
def running(root: Path) -> Iterator[Client]:
    """
    Runs the sidecar against `root` for the duration of the block.

    Assumes the caller is already inside `env.pointed_at(root)`, so the server's
    module-level paths resolve into the fixture root.
    """
    import advisorbot_server

    port = free_port()
    advisorbot_server.write_token_file()
    httpd = advisorbot_server.ThreadingHTTPServer(("127.0.0.1", port), advisorbot_server.SavesRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield Client(port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
