"""Real authentication edge — Google SSO + sessions (design §18/§26)."""

from api.auth.oauth import build_oauth

__all__ = ["build_oauth"]
