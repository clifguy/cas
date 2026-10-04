"""Backend-for-frontend OIDC auth router.

GET  /app/auth/login    -- begin interactive sign-in; returns the identity
                           provider's authorization URL for the SPA to navigate
                           the browser to, and sets a short-lived cookie binding
                           the sign-in to this browser. Rate-limited per client.
GET  /app/auth/callback -- the identity provider's redirect target; exchanges
                           the authorization code, opens a server-side session,
                           sets the session cookie, and redirects the browser
                           back to the SPA. A browser-facing redirect mechanism,
                           kept out of the documented JSON API surface.
GET  /app/auth/me       -- report whether the caller has a live session.
POST /app/auth/logout   -- end the session and clear the cookie.

The router resolves no vault (it is cross-vault); service resolution flows
through the ``app.backend.auth.dependencies`` factories, matching the canonical
service-as-load-bearer shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from app.backend.auth.config import (
    LEGACY_SESSION_COOKIE_NAME,
    LOGIN_BINDING_COOKIE_NAME,
    BffAuthSettings,
)
from app.backend.auth.dependencies import (
    get_auth_settings,
    get_login_rate_limiter,
    get_oidc_service,
    get_session_service,
    get_session_store,
)
from app.backend.auth.oidc import AuthError, OidcService
from app.backend.auth.rate_limit import LoginRateLimiter, client_key
from app.backend.auth.session_store import (
    PendingLogin,
    Session,
    SessionService,
    SessionStore,
)
from app.backend.auth.urls import callback_url
from app.backend.models import (
    ErrorResponse,
    LoginChallengeResponse,
    SessionInfoResponse,
    UserClaims,
)
from sage.api.errors import RateLimitedError, SAGEError
from sage.api.wire_route import WireRoute

router = APIRouter(route_class=WireRoute, prefix="/app/auth", tags=["auth"])

# A pre-login flow record is short-lived: it bridges the authorization redirect
# and the callback, nothing more.
_PENDING_TTL_SECONDS = 600


def _state_digest(state: str) -> str:
    """The binding-cookie value for ``state``: its SHA-256 hex digest."""
    return hashlib.sha256(state.encode()).hexdigest()


@router.get(
    "/login",
    response_model=LoginChallengeResponse,
    operation_id="begin_login",
    responses={
        429: {
            "model": ErrorResponse,
            "description": (
                "`rate_limited`: this client has started too many sign-ins recently. "
                "Retry after `detail.retry_after_seconds`, also sent as the "
                "`Retry-After` header."
            ),
        },
    },
)
async def begin_login(
    request: Request,
    response: Response,
    settings: BffAuthSettings = Depends(get_auth_settings),
    oidc: OidcService = Depends(get_oidc_service),
    store: SessionStore = Depends(get_session_store),
    limiter: LoginRateLimiter = Depends(get_login_rate_limiter),
) -> LoginChallengeResponse:
    """Begin interactive sign-in and return the authorization URL."""
    retry_after = limiter.check(client_key(request))
    if retry_after is not None:
        raise RateLimitedError(retry_after)
    redirect_uri = callback_url(request, settings.callback_path)
    challenge = await asyncio.to_thread(oidc.begin_login, redirect_uri)
    await store.put_pending(
        PendingLogin(
            state=challenge.state,
            flow=challenge.flow,
            expires_at=time.time() + _PENDING_TTL_SECONDS,
        )
    )
    response.set_cookie(
        LOGIN_BINDING_COOKIE_NAME,
        _state_digest(challenge.state),
        max_age=_PENDING_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return LoginChallengeResponse(
        authorization_url=challenge.authorization_url, state=challenge.state
    )


@router.get("/callback", include_in_schema=False)
async def auth_callback(
    request: Request,
    settings: BffAuthSettings = Depends(get_auth_settings),
    oidc: OidcService = Depends(get_oidc_service),
    store: SessionStore = Depends(get_session_store),
) -> Response:
    """Exchange the authorization code, open a session, and set the cookie."""
    params = dict(request.query_params)
    if params.get("error"):
        message = params.get("error_description") or params["error"]
        raise SAGEError("auth_failed", message, 400)

    state = params.get("state")
    # The sign-in must complete in the browser that began it: that browser holds
    # the binding cookie for this state. Checked before the pending record is
    # consumed, so a refused callback leaves the genuine sign-in completable.
    binding = request.cookies.get(LOGIN_BINDING_COOKIE_NAME, "")
    if not state or not hmac.compare_digest(binding, _state_digest(state)):
        raise SAGEError("invalid_state", "Missing or unrecognized sign-in state.", 400)
    pending = await store.take_pending(state)
    if pending is None:
        raise SAGEError("invalid_state", "Missing or unrecognized sign-in state.", 400)

    try:
        result = await asyncio.to_thread(oidc.complete_login, pending.flow, params)
    except AuthError as exc:
        raise SAGEError("auth_failed", str(exc), 400) from exc

    session_id = secrets.token_urlsafe(32)
    await store.create_session(
        Session(
            session_id=session_id,
            subject=result.subject,
            claims=result.claims,
            token_cache=result.token_cache,
            expires_at=time.time() + settings.session_ttl_seconds,
        )
    )
    response = RedirectResponse(settings.post_login_redirect, status_code=302)
    response.delete_cookie(
        LOGIN_BINDING_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/me", response_model=SessionInfoResponse, operation_id="get_session")
async def session_info(
    request: Request,
    settings: BffAuthSettings = Depends(get_auth_settings),
    sessions: SessionService = Depends(get_session_service),
) -> SessionInfoResponse:
    """Report whether the caller has a live session."""
    session_id = request.cookies.get(settings.session_cookie_name)
    session = await sessions.read(session_id)
    if session is None:
        return SessionInfoResponse(authenticated=False, user=None)
    claims = session.claims
    return SessionInfoResponse(
        authenticated=True,
        user=UserClaims(
            subject=session.subject,
            name=claims.get("name"),
            email=claims.get("preferred_username") or claims.get("email"),
        ),
    )


@router.post("/logout", status_code=204, operation_id="end_session")
async def logout(
    request: Request,
    settings: BffAuthSettings = Depends(get_auth_settings),
    sessions: SessionService = Depends(get_session_service),
) -> Response:
    """End the session and clear the cookie."""
    session_id = request.cookies.get(settings.session_cookie_name)
    await sessions.terminate(session_id)
    response = Response(status_code=204)
    response.delete_cookie(
        settings.session_cookie_name, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.delete_cookie(LEGACY_SESSION_COOKIE_NAME, path="/")
    return response
