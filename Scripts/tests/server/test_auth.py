#!/usr/bin/env python3
"""
Sidecar authentication and transport hardening.

Regression cover for the web-layer controls established in the Oct 31 to Nov 11 review
(S1) and re-tested in the Nov 15 audit: loopback-only binding, Host validation, a session
token on every privileged route, strict cookie flags, and no CORS of any kind.

The server runs in a background thread inside the test process against a fixture root, so
these are real HTTP requests to the real handler.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.portfolio("p1_index_saver"), pytest.mark.security]


@pytest.mark.feature("server_auth")
def test_health__is_reachable_without_a_token(sidecar):
    """The liveness probe stays public so a launcher can wait for the port."""
    response = sidecar.get("/api/health", authenticated=False)
    assert response.status == 200
    assert response.json().get("ok") is True


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA-06")
def test_dashboard__requires_a_token(sidecar):
    """
    The dashboard itself is gated.

    Exercises: the page embeds the whole portfolio payload, so serving it unauthenticated
    made the token protect only the API and not the data (finding SA-06).
    """
    response = sidecar.get("/", authenticated=False)
    assert response.status == 403, response.text[:200]


@pytest.mark.feature("server_auth")
@pytest.mark.regression("S1")
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/llm/overview"),
        ("GET", "/api/llm/positions"),
        ("GET", "/api/models"),
        ("GET", "/api/privacy"),
        ("POST", "/api/models"),
        ("POST", "/api/models/delete"),
        ("POST", "/api/portfolio/refresh"),
        ("POST", "/api/chat"),
    ],
)
def test_privileged_routes__reject_requests_without_a_token(sidecar, method, path):
    """Every reading and writing route answers 403 without the session token."""
    response = sidecar.request(method, path, body={} if method == "POST" else None, authenticated=False)
    assert response.status == 403, f"{method} {path} returned {response.status}"
    assert "nauthorized" in response.text or "orbidden" in response.text


@pytest.mark.feature("server_auth")
def test_privileged_route__accepts_the_session_token_header(sidecar):
    """A local tool authenticates with the token from the 0600 file."""
    sidecar.authenticate()
    response = sidecar.get("/api/privacy")
    assert response.status == 200, response.text[:200]
    assert isinstance(response.json(), dict)


@pytest.mark.feature("server_auth")
def test_session_token__is_long_and_random_enough(sidecar):
    """The token is a 256-bit URL-safe value, not a predictable string."""
    import config

    token = config.get_session_token_file().read_text(encoding="utf-8").strip()
    assert len(token) >= 40
    assert len(set(token)) > 15, "token looks structured rather than random"


@pytest.mark.feature("server_auth")
def test_session_token_file__is_owner_only(sidecar):
    """The token file is 0600, because anything that can read it is authenticated."""
    import config

    mode = config.get_session_token_file().stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


@pytest.mark.feature("server_auth")
@pytest.mark.regression("SA-20")
def test_bad_token__is_rejected(sidecar):
    """A wrong token is not a valid token, however well formed."""
    response = sidecar.get(
        "/api/privacy",
        headers={"X-Session-Token": "x" * 43},
        authenticated=False,
    )
    assert response.status == 403


@pytest.mark.feature("server_auth")
@pytest.mark.regression("S1")
def test_host_header__non_loopback__is_refused(sidecar):
    """
    A request claiming a non-loopback Host is refused before anything else happens.

    Exercises: the DNS-rebinding guard. Without it, a public domain that resolves to
    127.0.0.1 could reach the sidecar from a browser tab.
    """
    sidecar.authenticate()
    response = sidecar.get("/api/health", host_header="portfolio.example.com", authenticated=False)
    assert response.status == 403
    assert "host" in response.text.lower()


@pytest.mark.feature("server_auth")
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_host_header__loopback_names__are_accepted(sidecar, host):
    """Both loopback spellings work, since the browser may use either."""
    response = sidecar.get("/api/health", host_header=host, authenticated=False)
    assert response.status == 200


@pytest.mark.feature("server_auth")
@pytest.mark.regression("S1")
def test_responses__carry_no_cors_headers(sidecar):
    """
    No response grants cross-origin access, even when Origin: null or cross-port localhost is sent.

    Exercises: S1 and SA2-01. Sending adversarial Origin headers verifies the server never
    echoes Access-Control-Allow-Origin or Access-Control-Allow-Credentials.
    """
    sidecar.authenticate()
    for origin in (None, "null", "http://127.0.0.1:9999", "http://localhost:3000", "https://evil.example"):
        hdrs = {"Origin": origin} if origin else None
        for path in ("/api/health", "/api/privacy"):
            resp = sidecar.get(path, headers=hdrs)
            headers = {k.lower() for k in resp.headers}
            assert not {h for h in headers if h.startswith("access-control-")}, (origin, headers)


@pytest.mark.feature("server_auth")
def test_responses__carry_the_hardening_headers(sidecar):
    """Sniffing, referrer leakage and caching are all disabled."""
    response = sidecar.get("/api/health", authenticated=False)
    lowered = {k.lower(): v for k, v in response.headers.items()}
    assert lowered.get("x-content-type-options") == "nosniff"
    assert lowered.get("referrer-policy") == "no-referrer"
    assert "no-store" in str(lowered.get("cache-control", ""))


@pytest.mark.feature("server_auth")
def test_dashboard_bootstrap__hands_the_browser_a_strict_httponly_cookie(sidecar):
    """
    Opening the dashboard with the launch token sets the session cookie, once.

    Exercises: the browser cannot read a 0600 file, so the launcher passes the token in
    the URL exactly once; the server redirects and stores it as an `HttpOnly;
    SameSite=Strict` cookie. `HttpOnly` keeps page JavaScript away from the token, and
    `SameSite=Strict` is what makes CSRF against the mutating routes impossible.
    """
    token = sidecar.authenticate()
    response = sidecar.get(f"/?token={token}", authenticated=False)
    assert response.status == 302, response.text[:200]
    assert response.headers.get("Location") == "/", "the token must not stay in the URL"
    cookie = response.headers.get("Set-Cookie") or ""
    assert "HttpOnly" in cookie, f"no HttpOnly cookie in {dict(response.headers)}"
    assert "SameSite=Strict" in cookie
    assert token not in cookie


@pytest.mark.feature("server_auth")
def test_dashboard_bootstrap__wrong_token_in_the_url__sets_no_cookie(sidecar):
    """A guessed token in the query string neither authenticates nor seeds a cookie."""
    response = sidecar.get("/?token=" + "z" * 43, authenticated=False)
    assert response.status == 403
    assert not (response.headers.get("Set-Cookie") or "")


@pytest.mark.feature("server_auth")
def test_dashboard__with_a_valid_token_header__serves_the_page(sidecar):
    """An authenticated request gets the rendered dashboard, not a redirect."""
    sidecar.authenticate()
    response = sidecar.get("/")
    assert response.status == 200
    assert "const PAYLOAD = " in response.text


@pytest.mark.feature("server_auth")
def test_options_preflight__returns_no_cors_allowances(sidecar):
    """A preflight gets no permission, even for Origin: null or cross-port localhost (SA2-01)."""
    for origin in ("null", "http://127.0.0.1:9999", "https://evil.example"):
        response = sidecar.request(
            "OPTIONS",
            "/api/models",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
            authenticated=False,
        )
        assert response.status in (204, 403)
        assert not {k for k in response.headers if k.lower().startswith("access-control-")}


@pytest.mark.feature("server_auth")
def test_root_docs_and_sync_localhost__work_case_insensitively_and_seed_both_hosts(sidecar):
    """Top-level repo docs (README.md, readme.md) are reachable without auth, and sync_localhost hops cleanly."""
    for doc_path in ("/README.md", "/readme.md", "/DISCLAIMER.md", "/disclaimer.md", "/LICENSE", "/license"):
        resp = sidecar.get(doc_path, headers={"Accept": "text/html"}, authenticated=False)
        assert resp.status == 200, f"{doc_path} returned {resp.status}"

    token = sidecar.authenticate()
    hop = sidecar.get(f"/?token={token}&sync_localhost=1", authenticated=False)
    assert hop.status == 302
    location = hop.headers["Location"]
    assert location.startswith(f"http://localhost:{sidecar.port}/?token=")
    assert token not in location
    assert token not in hop.headers["Set-Cookie"]
    ticket = location.split("?token=", 1)[1]
    assert sidecar.get(f"/?token={ticket}", authenticated=False).status == 302
    assert sidecar.get(f"/?token={ticket}", authenticated=False).status == 403


@pytest.mark.regression("SA4-01")
def test_occupied_port_does_not_open_a_browser(root, monkeypatch):
    """An unrelated occupied port cannot cause browser navigation or publication of a session secret."""
    import errno
    import os
    import sys
    import advisorbot_server as server

    def occupied(*args, **kwargs):
        raise OSError(errno.EADDRINUSE, "Port already bound")

    monkeypatch.setattr(sys, "argv", ["advisorbot_server.py", "--open"])
    monkeypatch.setattr(server.ThreadingHTTPServer, "__init__", occupied)
    monkeypatch.setattr(server, "_ticket_from_running_server", lambda *_: None)
    monkeypatch.setattr(server, "_open_authenticated_browser", lambda *args: pytest.fail("opened browser"))
    monkeypatch.setattr(server, "_init_session_token", lambda *args, **kwargs: pytest.fail("issued token"))
    prior_umask = os.umask(0o077)
    try:
        assert server.main() == 2
    finally:
        monkeypatch.undo()
        os.umask(prior_umask)


@pytest.mark.regression("SA4-05")
def test_browser_page_rejects_a_different_active_household(root, sidecar):
    """Switching between two synthetic households rejects an old tab's state and renders a fresh page for the new household."""
    import re
    import shutil
    import config
    from tests.support import roots

    pid = config.get_active_portfolio_id()
    other = "p2_equity_comp_rsu" if pid != "p2_equity_comp_rsu" else "p1_index_saver"
    shutil.copytree(roots.fixture_dir(other), root / "Portfolios" / other, dirs_exist_ok=True)
    scope = re.compile(r'<meta name="advisorbot-portfolio" content="([0-9a-f]+)">')
    sidecar.authenticate()
    old = scope.search(sidecar.get("/").text)[1]
    try:
        assert config.set_active_portfolio(other)["ok"]
        reply = sidecar.post("/api/ui/poll", {"browser_state": {"sentinel": "old"}}, headers={"X-AdvisorBot-Portfolio": old})
        assert reply.status == 409
        new = scope.search(sidecar.get("/").text)[1]
        assert new != old
        assert sidecar.get("/api/privacy", headers={"X-AdvisorBot-Portfolio": new}).status == 200
    finally:
        config.set_active_portfolio(pid)


@pytest.mark.regression("SA4-01")
def test_running_instance_issues_a_one_use_link_without_a_restart(sidecar):
    """A second launcher can sign in a fresh browser and the resulting cookie works on ordinary requests."""
    import advisorbot_server as server

    ticket = server._ticket_from_running_server("127.0.0.1", sidecar.port)
    assert ticket and ticket != server.SESSION_TOKEN
    first = sidecar.get(f"/?token={ticket}", authenticated=False)
    assert first.status == 302
    cookie = first.headers["Set-Cookie"].split(";", 1)[0]
    assert sidecar.get("/api/privacy", headers={"Cookie": cookie}, authenticated=False).status == 200
    assert sidecar.get(f"/?token={ticket}", authenticated=False).status == 403
    assert sidecar.post("/api/auth/launch", {}, authenticated=False).status == 403


def test_legacy_browser_cookie_upgrades_on_a_normal_visit(sidecar):
    """A returning browser exchanges its old cookie silently and reuses the signed replacement."""
    import advisorbot_server as server
    legacy = sidecar.authenticate()
    reply = sidecar.get("/api/privacy", authenticated=False, headers={"Cookie": f"{server.COOKIE_NAME}={legacy}"})
    assert reply.status == 200
    replacement = reply.headers["Set-Cookie"].split(";", 1)[0]
    assert legacy not in replacement
    second = sidecar.get("/api/privacy", authenticated=False, headers={"Cookie": replacement})
    assert second.status == 200 and "Set-Cookie" not in second.headers
