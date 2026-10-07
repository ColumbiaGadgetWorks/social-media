"""Sign-in: the reverse proxy's auth in front, then a Studio account and role."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from .config import ip_in
from .db import settings
from .models import User
from .security import csrf_ok


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class ProxyAuthMissing(Exception):
    pass


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def proxy_user(request: Request) -> str | None:
    """The proxy's user header, trusted only when the request comes from a trusted proxy."""
    s = settings()
    if not ip_in(client_ip(request), s.trusted_proxies):
        return None
    value = request.headers.get(s.proxy_user_header, "").strip()
    return value or None


def check_proxy(request: Request) -> str | None:
    if not settings().require_proxy_auth:
        return proxy_user(request)
    who = proxy_user(request)
    if not who:
        raise ProxyAuthMissing()
    return who


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    if settings().require_proxy_auth:
        who = check_proxy(request)
        expected = user.proxy_username or user.username
        if who.lower() != expected.lower():
            request.session.clear()
            return None
    return user


def require(request: Request, db: Session, role: str = "contributor") -> User:
    check_proxy(request)
    user = current_user(request, db)
    if user is None:
        raise LoginRequired()
    if not user.has_role(role):
        raise Forbidden()
    return user


def check_csrf(request: Request, form) -> None:
    if not csrf_ok(request.session, form.get("csrf")):
        raise Forbidden()
