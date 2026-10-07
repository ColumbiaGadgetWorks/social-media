"""Password hashing, API tokens, CSRF tokens, and the Actor that every change is recorded under."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import ApiToken, AuditLog, User

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(db: Session, user: User, name: str) -> str:
    """Returns the plain token once; only its hash is stored."""
    token = "cgw_" + secrets.token_urlsafe(32)
    db.add(ApiToken(user_id=user.id, name=name, prefix=token[:10], token_hash=_token_hash(token)))
    return token


def user_for_token(db: Session, token: str) -> User | None:
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == _token_hash(token)))
    if row is None or row.revoked_at is not None or not row.user.is_active:
        return None
    row.last_used_at = utcnow()
    return row.user


def csrf_token(session: dict) -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def csrf_ok(session: dict, submitted: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


@dataclass(frozen=True)
class Actor:
    """Who is making a change. MCP actors act for a user but can never approve."""

    type: str  # user | mcp | system
    user: User | None = None

    @property
    def label(self) -> str:
        if self.type == "system":
            return "Studio"
        name = self.user.label if self.user else "unknown"
        return f"Claude (via {name})" if self.type == "mcp" else name

    @property
    def is_human(self) -> bool:
        return self.type == "user" and self.user is not None

    @classmethod
    def system(cls) -> Actor:
        return cls("system")


def audit(db: Session, actor: Actor, action: str, entity_type: str = "", entity_id: int | None = None, **detail) -> None:
    db.add(
        AuditLog(
            actor_type=actor.type,
            actor_id=actor.user.id if actor.user else None,
            actor_label=actor.label,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
        )
    )
