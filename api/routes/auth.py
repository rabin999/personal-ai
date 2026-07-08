"""Auth routes — Google sign-in/sign-up, session, current user, logout (brief §1/§3).

Sign-in and sign-up are ONE flow: the callback looks up the Google account and
either signs in (existing) or creates it (new), then establishes a real session
by writing OUR internal ``user_id`` into the signed session cookie. The redirect
URI is built from ``PUBLIC_BASE_URL`` (not the internal request host) so Google
never returns ``redirect_uri_mismatch`` behind the reverse proxy (brief §0).
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from adapters.user_context.accounts import AccountStore, GoogleIdentity
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_USER_KEY = "user_id"


@router.get("/google/login")
async def google_login(request: Request) -> RedirectResponse:
    """Kick off the OAuth flow — redirect to Google's consent screen."""
    settings: Settings = request.app.state.settings
    oauth = request.app.state.oauth
    redirect_uri = settings.google_redirect_uri  # from PUBLIC_BASE_URL (brief §0)
    logger.info("oauth login → google, redirect_uri=%s", redirect_uri)
    redirect: RedirectResponse = await oauth.google.authorize_redirect(request, redirect_uri)
    return redirect


@router.get("/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    """Google redirects here with the code → exchange for tokens, resolve the
    account (sign in or sign up), and establish the session."""
    oauth = request.app.state.oauth
    accounts: AccountStore = request.app.state.accounts
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:  # bad/expired state, user denied, etc.
        logger.warning("oauth callback failed: %s", exc)
        return RedirectResponse(url="/login?error=auth_failed", status_code=303)

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.userinfo(token=token)
    identity = GoogleIdentity(
        sub=str(userinfo["sub"]),
        email=str(userinfo.get("email", "")),
        name=userinfo.get("name"),
        picture=userinfo.get("picture"),
    )
    result = await accounts.upsert_from_google(identity)
    # Establish the real session (signed cookie carries OUR user_id).
    request.session[SESSION_USER_KEY] = result.account.user_id
    logger.info(
        "auth ok: user_id=%s %s",
        result.account.user_id,
        "(new signup)" if result.created else "(returning)",
    )
    return RedirectResponse(url="/", status_code=303)


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    """The current authenticated user (401 if no valid session)."""
    # DEV/TEST bypass (empty in prod): the client's auth guard hits THIS endpoint,
    # so honor DEV_AUTH_USER here too — return a synthetic identity so the SPA
    # renders the real authenticated pages locally over http.
    dev_user = get_settings().dev_auth_user
    if dev_user:
        return JSONResponse(
            {
                "user_id": dev_user,
                "email": f"{dev_user}@dev.local",
                "name": "Dev User",
                "picture": None,
            }
        )
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    accounts: AccountStore = request.app.state.accounts
    account = await accounts.get(user_id)
    if account is None:  # stale session (user deleted) → clear it
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown session")
    return JSONResponse(
        {
            "user_id": account.user_id,
            "email": account.email,
            "name": account.name,
            "picture": account.picture,
        }
    )


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Clear the session (revoke)."""
    request.session.clear()
    return JSONResponse({"ok": True})


# Every Mongo collection that stores data keyed by ``user_id`` — the full set wiped
# on account deletion (GDPR-style). Global/shared collections (provider_config,
# trait_defs, project_types, prompts) are intentionally excluded.
_USER_COLLECTIONS = [
    "user_profile",
    "conversations",
    "conversation_turns",
    "turn_traces",
    "response_feedback",
    "procedural",
    "self_statements",
    "self_model_log",
    "psych_model",
    "psych_correlations",
    "projects",
    "pending_insights",
    "entities",
    "tool_results",
    "cost_ledger",
    "ledger_entries",
    "search_cache",
]


@router.delete("/account")
async def delete_account(request: Request) -> JSONResponse:
    """Delete the signed-in user's account and ALL their data, then end the session.
    Wipes every user-scoped Mongo collection + episodic vectors (Qdrant); best-effort
    so one failing store never blocks the rest. The user is fully logged out after."""
    dev_user = get_settings().dev_auth_user
    user_id = dev_user or request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    pipeline = request.app.state.pipeline
    removed = 0
    for col in _USER_COLLECTIONS:
        try:
            removed += await pipeline.docs.delete_many(col, {"user_id": user_id})
        except Exception:
            logger.warning("delete_account: failed on collection %s", col, exc_info=True)
    try:
        await pipeline.episodic.delete_all(user_id)
    except Exception:
        logger.warning("delete_account: episodic wipe failed", exc_info=True)
    try:  # the account record itself lives in the accounts store (Mongo)
        await pipeline.docs.delete_many("accounts", {"user_id": user_id})
        await pipeline.docs.delete_many("accounts", {"_id": user_id})
    except Exception:
        logger.warning("delete_account: account delete failed", exc_info=True)
    request.session.clear()
    logger.info("account deleted: user_id=%s (%d docs)", user_id, removed)
    return JSONResponse({"ok": True, "deleted_docs": removed})
