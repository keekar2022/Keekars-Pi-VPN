# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # OIDC / SSO — populated at deploy time via /etc/pi-config-ui/sso.env
    # (see deploy/sso.env.example). Never hardcode real values here.
    # sso.keekar.au is an Authentik instance: discovery lives per-application
    # at /application/o/<slug>/.well-known/openid-configuration, not at the
    # issuer root, so the application slug is required separately from the
    # client ID even though they happen to share the same value today.
    sso_issuer: str = "https://sso.keekar.au"
    sso_application_slug: str
    sso_client_id: str
    sso_client_secret: str
    sso_redirect_uri: str

    # Session cookie signing key — generate a random 32+ byte secret per
    # deployment (e.g. `openssl rand -hex 32`) and store it alongside the
    # SSO credentials, not in source control.
    session_secret: str


settings = Settings()
