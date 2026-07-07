"""Google OAuth2/OIDC client via Authlib (brief §1) — no hand-rolled OAuth.

Registered from ``server_metadata_url`` so Authlib discovers Google's authorize/
token/userinfo/JWKS endpoints; client_id/secret come from env, never hard-coded.
"""

from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]

from config.settings import Settings

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth
