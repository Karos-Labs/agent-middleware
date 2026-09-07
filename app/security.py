"""Service-to-service authentication for the control plane.

Every non-health route sits behind :func:`require_service_identity`. Two ways in:

* **Google OIDC (production).** A Cloud Run caller asks its metadata server for
  an identity token whose ``aud`` is *this* service's URL, and sends it as
  ``Authorization: Bearer <jwt>``. We verify the signature against Google's
  public certificates, then check ``aud`` and the caller's service-account
  email.
* **A static bearer token (development).** Convenient for curl, the test suite
  and a local portal. Refused outright when ``environment=production``, so a
  stray ``AUTH_DEV_TOKEN`` on a production deploy cannot become a bypass.

Why the ``aud`` check is not optional
-------------------------------------
Verifying only the signature proves nothing useful: Google will happily issue a
valid, correctly-signed identity token to *any* account on *any* project. The
audience claim is what ties a token to this specific service, so a token minted
for some other service can't be replayed here. ``verify_oauth2_token`` requires
an audience argument for exactly this reason, and this module refuses to start
a verification without one rather than passing ``None``.

The service-account allowlist is a second, coarser gate on top. Left empty it
permits any identity Google vouches for, which is only safe when Cloud Run IAM
(``roles/run.invoker``) is already restricting who can reach the service at all
— the normal deployment. Populate it for defence in depth.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, Request, status
from fastapi.exceptions import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.core.roles import Role
from app.dependencies import get_settings_dep

logger = logging.getLogger(__name__)

# auto_error=False so a missing header reaches our own handler and produces a
# consistent problem detail, instead of FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False, description="Google OIDC identity token")


class CallerIdentity:
    """Who made the request, once authenticated, and what they may do."""

    def __init__(
        self,
        *,
        subject: str,
        email: str | None,
        method: str,
        role: Role = Role.VIEWER,
    ) -> None:
        self.subject = subject
        self.email = email
        self.method = method
        self.role = role

    @property
    def actor(self) -> str:
        """The audit-trail name for this caller.

        Read off the verified token, never off the request body or query
        string. Before this existed, ``created_by`` and ``?actor=`` were free
        text supplied by the caller and compared against nothing -- so the
        audit log for a prompt edit recorded whatever the editor typed, which
        is a log that cannot be wrong and therefore cannot be evidence.
        """

        return self.email or self.subject

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CallerIdentity(subject={self.subject!r}, method={self.method!r}, "
            f"role={self.role.value!r})"
        )


#: The identity used when ``auth_enabled`` is false. It holds ``admin`` on
#: purpose: with authentication off there is no principal to bind a role to,
#: and enforcing roles against an unauthenticated caller would be a check that
#: looks like security and is not. Turning authentication ON is what turns
#: authorization on, in one step, which is also what makes this change inert
#: until then.
ANONYMOUS = CallerIdentity(
    subject="anonymous", email=None, method="disabled", role=Role.ADMIN
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _verify_oidc_token(token: str, audience: str) -> dict[str, Any]:
    """Verify a Google-issued OIDC token. Blocking; call it off the event loop.

    Imported lazily so the rest of the service — and the whole test suite —
    does not need ``google-auth`` resolved at import time.
    """

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return dict(
        google_id_token.verify_oauth2_token(
            token, google_requests.Request(), audience=audience
        )
    )


async def require_service_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings_dep),
) -> CallerIdentity:
    """Authenticate the caller, or raise 401/403.

    Attaches the resolved identity to ``request.state.caller`` so handlers and
    log records can name who acted without re-parsing the header.
    """

    if not settings.auth_enabled:
        request.state.caller = ANONYMOUS
        return ANONYMOUS

    if credentials is None or not credentials.credentials:
        raise _unauthorized("missing bearer token")
    token = credentials.credentials

    # --- Development shortcut -------------------------------------------
    if settings.dev_token_permitted:
        # compare_digest, not ==: token comparison should not leak length or a
        # common prefix through timing.
        assert settings.auth_dev_token is not None  # narrowed by dev_token_permitted
        if hmac.compare_digest(token, settings.auth_dev_token):
            identity = CallerIdentity(
                subject="dev-token",
                email=None,
                method="dev_token",
                role=settings.role_for("dev-token"),
            )
            request.state.caller = identity
            return identity
        # Fall through: a non-matching token may still be a real OIDC token.

    # --- Google OIDC ------------------------------------------------------
    if not settings.auth_audience:
        # Refusing is the safe direction. Verifying without an audience would
        # accept any Google-signed token in existence (see module docstring).
        logger.error(
            "AUTH_ENABLED is set but AUTH_AUDIENCE is not configured; refusing to "
            "verify tokens without an audience to bind them to this service"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Service authentication is misconfigured.",
        )

    try:
        claims = await asyncio.to_thread(_verify_oidc_token, token, settings.auth_audience)
    except Exception as exc:  # noqa: BLE001 - every verification failure is a 401
        logger.warning("Rejected an identity token: %s", exc)
        raise _unauthorized("invalid identity token") from exc

    email = claims.get("email")
    allowed = settings.auth_allowed_service_accounts
    if allowed and email not in allowed:
        logger.warning("Caller %s is not in the service-account allowlist", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Caller is not authorized to use this service.",
        )

    subject = str(claims.get("sub", "unknown"))
    identity = CallerIdentity(
        subject=subject,
        email=email,
        method="oidc",
        role=settings.role_for(email or subject),
    )
    request.state.caller = identity
    return identity


def require_role(minimum: Role) -> Callable[..., Awaitable[CallerIdentity]]:
    """Dependency factory: refuse a caller whose role is below ``minimum``.

    A route names the minimum it needs and nothing else. FastAPI resolves
    ``require_service_identity`` once per request and caches it, so the token
    is verified once however many of these a request passes through.

    The 403 names the principal and both roles. A missing binding is the most
    likely cause of a refusal here and it is invisible from the client side --
    "forbidden" with no principal sends someone reading the wrong config file.
    """

    async def dependency(
        identity: CallerIdentity = Depends(require_service_identity),
    ) -> CallerIdentity:
        if not identity.role.satisfies(minimum):
            logger.warning(
                "Refused %s (role %s) on a route requiring %s",
                identity.actor,
                identity.role.value,
                minimum.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"caller '{identity.actor}' holds role '{identity.role.value}'; "
                    f"this operation requires '{minimum.value}' or higher"
                ),
            )
        return identity

    return dependency
