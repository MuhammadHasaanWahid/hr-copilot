"""Token-based authentication (JWT, HS256).

Demo mode: `/api/auth/login` issues a short-lived signed token for a synthetic
employee (optionally protected by DEMO_PASSCODE). Production: tokens come from
the company identity provider (OIDC); only `verify` changes (RS256 + JWKS) -
every downstream check already works on the Principal resolved from the token.
"""
from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from .config import Settings

ISSUER = "hr-copilot"


class AuthError(Exception):
    pass


def check_passcode(settings: Settings, passcode: str | None) -> None:
    if settings.demo_passcode and not hmac.compare_digest(settings.demo_passcode, passcode or ""):
        raise AuthError("Invalid demo passcode.")


def issue(settings: Settings, employee_id: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": employee_id, "iss": ISSUER, "iat": now, "nbf": now,
              "exp": now + timedelta(minutes=settings.jwt_ttl_minutes), "jti": uuid.uuid4().hex}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def verify(settings: Settings, token: str) -> str:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], issuer=ISSUER,
                            options={"require": ["exp", "iat", "sub", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired - please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid token.") from exc
    return claims["sub"]
