# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import logging
import ssl

import truststore
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.config import settings

logger = logging.getLogger("pi_config_ui.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Use the OS-native certificate trust store (macOS Keychain / Windows cert
# store / Linux system store) instead of the bundled `certifi` list.
# Verified against sso.keekar.au: its cert chains to a Let's Encrypt root
# ("ISRG Root YR") that curl/the OS trust store already accepts but that a
# pinned certifi release doesn't carry yet — same lag can happen against
# Raspberry Pi OS's system ca-certificates package, so this avoids the
# integration silently breaking whenever a client CA bundle is stale.
_ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

oauth = OAuth()
oauth.register(
    name="sso",
    client_id=settings.sso_client_id,
    client_secret=settings.sso_client_secret,
    server_metadata_url=(
        f"{settings.sso_issuer}/application/o/{settings.sso_application_slug}"
        "/.well-known/openid-configuration"
    ),
    client_kwargs={
        "scope": "openid profile email",
        "verify": _ssl_context,
        "code_challenge_method": "S256",
        # A Pi Zero W's single ARMv6 core makes the TLS handshake itself
        # take several seconds; httpx's 5s default timeout isn't enough.
        "timeout": 20,
    },
)


@router.get("/login")
async def login(request: Request):
    response = await oauth.sso.authorize_redirect(request, settings.sso_redirect_uri)
    logger.info(
        "sso_login_initiated had_session_cookie=%r new_state=%r",
        "session" in request.cookies,
        response.headers.get("location", "").split("state=")[-1].split("&")[0],
    )
    return response


@router.get("/callback")
async def callback(request: Request):
    try:
        token = await oauth.sso.authorize_access_token(request)
    except Exception as exc:
        # `extra={...}` fields don't appear in the configured log format
        # (main.py's formatter only renders %(message)s), so the diagnostic
        # detail is embedded directly in the message text instead.
        # Deliberately omit the `code` query param itself — single-use
        # authorization code, shouldn't be persisted to logs.
        logger.exception(
            "sso_callback_failed error=%r has_session_cookie=%r state_param=%r session_keys=%r",
            str(exc),
            "session" in request.cookies,
            request.query_params.get("state"),
            list(request.session.keys()),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login failed")

    claims = token.get("userinfo")
    if not claims or not claims.get("sub"):
        logger.warning("sso_callback_missing_claims", extra={"event": "auth.callback.invalid_token"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login failed")

    # authlib verifies the ID token signature/iss/aud/exp/nonce during
    # authorize_access_token(); only the minimal claims needed are kept
    # server-side in the signed session cookie.
    request.session["user"] = {
        "sub": claims["sub"],
        "email": claims.get("email"),
        "name": claims.get("name"),
    }
    logger.info("sso_login_succeeded", extra={"event": "auth.login.success", "sub": claims["sub"]})
    return RedirectResponse(url="/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    metadata = await oauth.sso.load_server_metadata()
    end_session_endpoint = metadata.get("end_session_endpoint")
    if end_session_endpoint:
        return RedirectResponse(url=end_session_endpoint)
    return RedirectResponse(url="/auth/login")


def current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/auth/login"},
        )
    return user
