# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# app/system.py's reboot/shutdown endpoints call the real `systemctl`
# binary, which doesn't make sense to actually invoke in a test run (and
# wouldn't be authorized without the polkit rule only present on the real
# Pi anyway) — _run_systemctl is mocked here instead, same as the
# subprocess boundary would be mocked for app/network.py's nmcli calls.

import pytest
from starlette.testclient import TestClient

from app import system
from app.auth import current_user
from app.main import app


@pytest.fixture
def authenticated_client():
    app.dependency_overrides[current_user] = lambda: {"sub": "test-user", "name": "Test User"}
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(current_user, None)


@pytest.mark.parametrize("endpoint", ["/api/system/reboot", "/api/system/shutdown"])
def test_power_endpoints_require_auth(endpoint):
    # No dependency override here — a real, unauthenticated client.
    response = TestClient(app).post(endpoint, follow_redirects=False)
    assert response.status_code in (302, 303, 401, 403)


@pytest.mark.parametrize(
    "endpoint,expected_arg",
    [("/api/system/reboot", "reboot"), ("/api/system/shutdown", "poweroff")],
)
def test_power_endpoints_call_correct_systemctl_verb(authenticated_client, monkeypatch, endpoint, expected_arg):
    calls = []

    async def fake_run_systemctl(*args):
        calls.append(args)

    monkeypatch.setattr(system, "_run_systemctl", fake_run_systemctl)

    response = authenticated_client.post(endpoint)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == [(expected_arg,)]


@pytest.mark.parametrize("endpoint", ["/api/system/reboot", "/api/system/shutdown"])
def test_power_endpoint_surfaces_systemctl_failure(authenticated_client, monkeypatch, endpoint):
    from fastapi import HTTPException

    async def fake_run_systemctl_fails(*args):
        raise HTTPException(status_code=502, detail="System action failed")

    monkeypatch.setattr(system, "_run_systemctl", fake_run_systemctl_fails)

    response = authenticated_client.post(endpoint)
    assert response.status_code == 502
