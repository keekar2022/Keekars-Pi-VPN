# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Sets required env vars before any `app.*` module is imported, since
# app.config.Settings() is instantiated at import time and fails fast if
# these are missing. Values are dummy/test-only — never real credentials.

import os

os.environ.setdefault("SSO_APPLICATION_SLUG", "test-app")
os.environ.setdefault("SSO_CLIENT_ID", "test-client-id")
os.environ.setdefault("SSO_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("SSO_REDIRECT_URI", "https://testserver/auth/callback")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-0123456789")
