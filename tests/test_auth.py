# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Regression tests for two bugs that shipped silently to production before
# being caught by hand: a SameSite=Strict session cookie that broke every
# OIDC login, and a CSP header that silently blocked every page's inline
# JavaScript. Both failure modes are invisible to a naive end-to-end test,
# which is exactly why they need direct, targeted assertions — see the
# comments in each test for why.

import re

import pytest
from starlette.testclient import TestClient

from app.auth import current_user, oauth
from app.main import app

FAKE_OIDC_METADATA = {
    "issuer": "https://sso.keekar.au/application/o/test-app/",
    "authorization_endpoint": "https://sso.keekar.au/application/o/authorize/",
    "token_endpoint": "https://sso.keekar.au/application/o/token/",
    "jwks_uri": "https://sso.keekar.au/application/o/test-app/jwks/",
}

PAGE_ROUTES = ["/", "/network", "/routing", "/wireguard"]


@pytest.fixture(autouse=True)
def _no_real_oidc_network_call(monkeypatch):
    # /auth/login triggers authlib's lazy OIDC discovery fetch on first
    # use; stub it so tests don't depend on network access or the real
    # sso.keekar.au being reachable.
    async def fake_load_server_metadata(*args, **kwargs):
        return FAKE_OIDC_METADATA

    monkeypatch.setattr(oauth.sso, "load_server_metadata", fake_load_server_metadata)


@pytest.fixture
def client():
    return TestClient(app)


def test_login_sets_lax_not_strict_samesite_cookie(client):
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code in (302, 307)

    set_cookie = response.headers.get("set-cookie", "")
    assert set_cookie, "expected /auth/login to set a session cookie"
    lowered = set_cookie.lower()

    # Must be a direct header-attribute assertion, not a simulated
    # login->callback round trip: httpx's cookie jar (what TestClient uses
    # under the hood) does not enforce SameSite the way a real browser
    # does, so a full simulated flow would "pass" even with
    # same_site="strict" — that's exactly how this bug reached production
    # unnoticed (every login failed with a mismatching_state CSRF error
    # because the browser silently dropped the cookie on the redirect back
    # from the IdP). Only asserting on the literal attribute catches it.
    assert "samesite=lax" in lowered, f"expected SameSite=Lax, got: {set_cookie!r}"
    assert "samesite=strict" not in lowered
    assert "secure" in lowered
    assert "httponly" in lowered


@pytest.fixture
def authenticated_client():
    app.dependency_overrides[current_user] = lambda: {"sub": "test-user", "name": "Test User"}
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(current_user, None)


@pytest.mark.parametrize("path", PAGE_ROUTES)
def test_pages_have_no_logic_bearing_inline_script(authenticated_client, path):
    response = authenticated_client.get(path)
    assert response.status_code == 200, f"{path} did not render (dependency override may be misconfigured)"

    # A <script> tag with no src attribute and non-whitespace content is
    # exactly the shape that silently broke every page: the CSP header
    # (`default-src 'self'`, no 'unsafe-inline') blocks inline scripts, but
    # a browser fails this *silently* — no visible error, no failed
    # network request, just dead JavaScript. This regex-based check is
    # cheap and catches the regression without needing a real browser to
    # enforce CSP the way Safari/Chrome do.
    inline_scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
        response.text,
        re.IGNORECASE | re.DOTALL,
    )
    logic_bearing = [s for s in inline_scripts if s.strip()]
    assert not logic_bearing, f"{path} has inline <script> content that CSP will silently block: {logic_bearing}"


@pytest.mark.parametrize("path", ["/auth/login"] + PAGE_ROUTES)
def test_csp_header_never_allows_unsafe_inline(authenticated_client, path):
    response = authenticated_client.get(path, follow_redirects=False)
    csp = response.headers.get("content-security-policy", "")
    assert csp, f"{path} is missing a Content-Security-Policy header"
    assert "unsafe-inline" not in csp.lower()
