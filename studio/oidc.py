"""Sign in with OpenID Connect (VoidAuth, Authentik, Authelia, Keycloak, Google...): authorization code flow with PKCE.

The browser is sent to the identity provider and comes back to /login/oidc/callback with a one-time code. The Studio
then trades that code for tokens directly with the provider over HTTPS (never through the browser), checks the
ID token's issuer, audience, expiry and nonce, and matches the person to a Studio account. Because the token comes
straight from the provider's own address over TLS, its signature isn't re-checked (OpenID Connect Core, 3.1.3.7).

Roles stay in the Studio. Optionally, provider groups can set them (STUDIO_OIDC_*_GROUPS)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import settings
from .models import ROLES, User
from .security import hash_password


class OIDCError(Exception):
    """Something that stops a sign-in, worded for the person at the login page."""


_discovery_cache: dict[str, tuple[float, dict]] = {}


def configured() -> bool:
    return settings().oidc_configured


def redirect_uri() -> str:
    return f"{settings().base_url}/login/oidc/callback"


def _http() -> httpx.Client:
    return httpx.Client(timeout=15, follow_redirects=False)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def discovery() -> dict:
    s = settings()
    issuer = s.oidc_issuer.rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        with _http() as http:
            r = http.get(f"{issuer}/.well-known/openid-configuration")
        r.raise_for_status()
        doc = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError(f"Couldn't reach the sign-in provider ({exc.__class__.__name__}). Try again in a minute.") from exc
    if str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise OIDCError("The sign-in provider's address doesn't match STUDIO_OIDC_ISSUER.")
    if not doc.get("authorization_endpoint") or not doc.get("token_endpoint"):
        raise OIDCError("The sign-in provider's configuration is incomplete.")
    _discovery_cache[issuer] = (time.time() + 3600, doc)
    return doc


def authorize_url(session: dict) -> str:
    doc = discovery()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session["oidc"] = {"state": state, "nonce": nonce, "verifier": verifier}
    query = urlencode({
        "response_type": "code", "client_id": settings().oidc_client_id, "redirect_uri": redirect_uri(),
        "scope": settings().oidc_scopes, "state": state, "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
    })
    base = doc["authorization_endpoint"]
    return f"{base}{'&' if '?' in base else '?'}{query}"


def _claims_of(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise OIDCError("The sign-in provider sent an ID token that couldn't be read.") from exc


def _exchange(doc: dict, code: str, verifier: str) -> dict:
    s = settings()
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "client_id": s.oidc_client_id, "code_verifier": verifier}
    auth = None
    if s.oidc_client_secret:
        methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in methods:
            auth = (s.oidc_client_id, s.oidc_client_secret)
        else:
            data["client_secret"] = s.oidc_client_secret
    try:
        with _http() as http:
            r = http.post(doc["token_endpoint"], data=data, auth=auth)
    except httpx.HTTPError as exc:
        raise OIDCError(f"Couldn't reach the sign-in provider ({exc.__class__.__name__}).") from exc
    if r.status_code >= 400:
        try:
            why = r.json().get("error_description") or r.json().get("error") or ""
        except ValueError:
            why = ""
        raise OIDCError(f"The sign-in provider refused the sign-in (HTTP {r.status_code}). {why}".strip())
    return r.json()


def _checked_claims(doc: dict, tokens: dict, nonce: str) -> dict:
    if not tokens.get("id_token"):
        raise OIDCError("The sign-in provider didn't send an ID token. Check that the scope includes openid.")
    claims = _claims_of(tokens["id_token"])
    s = settings()
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if str(claims.get("iss", "")).rstrip("/") != str(doc.get("issuer", "")).rstrip("/"):
        raise OIDCError("The ID token came from a different issuer.")
    if s.oidc_client_id not in audiences:
        raise OIDCError("The ID token wasn't issued for this app.")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise OIDCError("The ID token has expired. Try signing in again.")
    if not claims.get("nonce") or not secrets.compare_digest(str(claims["nonce"]), nonce):
        raise OIDCError("That sign-in link expired. Try again.")
    if not claims.get("sub"):
        raise OIDCError("The ID token has no subject.")
    info_url = doc.get("userinfo_endpoint")
    if info_url and tokens.get("access_token"):  # groups and email are often only here
        try:
            with _http() as http:
                r = http.get(info_url, headers={"Authorization": f"Bearer {tokens['access_token']}"})
            info = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            info = {}
        if isinstance(info, dict) and info.get("sub") == claims["sub"]:
            claims = {**claims, **info}
    return claims


def _groups(claims: dict) -> set[str]:
    raw = claims.get(settings().oidc_groups_claim) or []
    if isinstance(raw, str):
        raw = [part for part in raw.replace(",", " ").split()]
    return {str(g).strip().lower() for g in raw if str(g).strip()}


def _mapped_role(groups: set[str]) -> str | None:
    """The highest Studio role the person's provider groups give, if any group lists are set."""
    s = settings()
    best = None
    for role, wanted in (("editor", s.oidc_editor_groups), ("approver", s.oidc_approver_groups),
                         ("admin", s.oidc_admin_groups)):
        if groups & {g.lower() for g in wanted}:
            best = role
    return best


def _find_user(db: Session, ident: str, claims: dict) -> User | None:
    low = ident.lower()
    user = (db.scalar(select(User).where(func.lower(User.proxy_username) == low))
            or db.scalar(select(User).where(func.lower(User.username) == low)))
    email = str(claims.get("email") or "").strip().lower()
    if user is None and email and claims.get("email_verified") in (True, "true"):
        user = db.scalar(select(User).where(func.lower(User.email) == email))
    return user


def finish(db: Session, session: dict, params: dict) -> tuple[User, dict]:
    """Complete a sign-in from the callback's query. Returns the Studio user and what changed (for the audit log)."""
    saved = session.pop("oidc", None)
    if params.get("error"):
        raise OIDCError(f"The sign-in provider said no: {params.get('error_description') or params['error']}")
    if not saved or not secrets.compare_digest(saved["state"], str(params.get("state", ""))):
        raise OIDCError("That sign-in link expired. Try again.")
    if not params.get("code"):
        raise OIDCError("The sign-in provider didn't send a code.")
    doc = discovery()
    claims = _checked_claims(doc, _exchange(doc, str(params["code"]), saved["verifier"]), saved["nonce"])
    s = settings()
    ident = str(claims.get(s.oidc_username_claim) or "").strip()
    if not ident:
        raise OIDCError(f"The sign-in provider didn't send a '{s.oidc_username_claim}' claim.")
    groups = _groups(claims)
    if s.oidc_allowed_groups and not groups & {g.lower() for g in s.oidc_allowed_groups}:
        raise OIDCError("Your account isn't in a group that may use the Studio.")
    changes: dict = {}
    user = _find_user(db, ident, claims)
    role = _mapped_role(groups)
    if user is None:
        if not s.oidc_auto_create:
            raise OIDCError(f"No Studio account matches '{ident}'. Ask an admin to add you, then sign in again.")
        default = s.oidc_default_role if s.oidc_default_role in ROLES else "contributor"
        user = User(username=ident[:64], display_name=str(claims.get("name") or "")[:128],
                    email=str(claims.get("email") or "")[:256], role=role or default,
                    password_hash=hash_password(secrets.token_urlsafe(32)))  # unusable: sign-in is through the provider
        db.add(user)
        db.flush()
        changes["created"] = True
    elif role and role != user.role:
        changes["role"] = f"{user.role} -> {role}"
        user.role = role
    if not user.is_active:
        raise OIDCError("That Studio account is turned off.")
    return user, changes
