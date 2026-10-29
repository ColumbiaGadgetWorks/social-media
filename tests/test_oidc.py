"""Sign in with OpenID Connect against a pretend identity provider."""

import base64
import dataclasses
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from studio import db as db_mod
from studio import oidc
from studio.models import AuditLog, User

from .conftest import PASSWORD, make_user

ISSUER = "https://id.test"


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


@pytest.fixture
def idp(monkeypatch):
    """The pretend provider's knobs: the claims it puts in the ID token and in userinfo."""
    state = {"nonce": "", "claims": {"preferred_username": "sam"}, "userinfo": {}, "token_status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
                "userinfo_endpoint": f"{ISSUER}/userinfo"})
        if path.endswith("/token"):
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"] and form["code_verifier"] and form["code"] == ["abc"]
            if state["token_status"] != 200:
                return httpx.Response(state["token_status"], json={"error": "invalid_grant"})
            claims = {"iss": ISSUER, "aud": "cgw", "sub": "u1", "exp": time.time() + 300, "nonce": state["nonce"],
                      **state["claims"]}
            return httpx.Response(200, json={"id_token": f"{b64({'alg': 'none'})}.{b64(claims)}.x", "access_token": "at"})
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "u1", **state["userinfo"]})
        return httpx.Response(404)

    monkeypatch.setattr(oidc, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    oidc._discovery_cache.clear()
    return state


def make_app(settings, **overrides):
    from studio.app import create_app

    return create_app(dataclasses.replace(
        settings, oidc_issuer=ISSUER, oidc_client_id="cgw", oidc_client_secret="shh", **overrides))


def sign_in(client: TestClient, idp: dict, **callback):
    r = client.get("/login/oidc", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISSUER}/authorize?")
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["cgw"]
    assert query["redirect_uri"] == ["http://studio.test/login/oidc/callback"]
    idp["nonce"] = query["nonce"][0]
    params = {"code": "abc", "state": query["state"][0]} | callback
    return client.get("/login/oidc/callback", params=params, follow_redirects=False)


def test_login_page_offers_single_sign_on_and_oidc_only_hides_the_password_form(settings):
    app = make_app(settings, oidc_button_label="Sign in with VoidAuth")
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        page = c.get("/login").text
        assert "Sign in with VoidAuth" in page and 'name="password"' in page
    app = make_app(settings, oidc_only=True)
    make_user("sam", "editor")
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        page = c.get("/login").text
        assert "Sign in with SSO" in page and 'name="password"' not in page
        r = c.post("/login", data={"username": "sam", "password": PASSWORD}, follow_redirects=False)  # direct post
        assert r.headers["location"] == "/login" and "turned off" in c.get("/login").text
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"


def test_oidc_only_without_a_provider_lets_nobody_in(settings):
    from studio.app import create_app

    app = create_app(dataclasses.replace(settings, oidc_only=True))
    make_user("sam", "editor")
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        page = c.get("/login").text
        assert "isn't set up" in page and 'name="password"' not in page


def test_sign_in_matches_an_existing_account(settings, idp):
    make_app(settings)
    make_user("sam", "editor")
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        r = sign_in(c, idp)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/login", follow_redirects=False).headers["location"] == "/"  # signed in now
    with db_mod.session_scope() as s:
        entry = s.scalars(select(AuditLog).where(AuditLog.action == "signed_in")).one()
        assert "oidc" in json.dumps(entry.detail)


def test_proxy_username_and_verified_email_also_match(settings, idp):
    make_app(settings)
    with db_mod.session_scope() as s:
        make_user("studio-sam", "editor")
        s.get(User, 1).proxy_username = "sam"
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/"
    idp["claims"] = {"preferred_username": "someone-else", "email": "studio-sam@example.org", "email_verified": False}
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/login"  # an unverified email isn't enough
    idp["claims"]["email_verified"] = True
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/"


def test_strangers_are_refused_unless_accounts_are_created_on_first_sign_in(settings, idp):
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        r = sign_in(c, idp)
        assert r.headers["location"] == "/login"
        assert "No Studio account matches" in c.get("/login").text
    idp["claims"] = {"preferred_username": "newbie", "name": "New Person", "email": "n@example.org", "groups": ["cgw-approvers"]}
    app = make_app(settings, oidc_auto_create=True, oidc_approver_groups=["cgw-approvers"], oidc_default_role="contributor")
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/"
    with db_mod.session_scope() as s:
        user = s.scalar(select(User).where(User.username == "newbie"))
        assert user.role == "approver" and user.display_name == "New Person"
    idp["claims"]["groups"] = ["cgw-admins"]  # groups keep roles in step on later sign-ins
    app = make_app(settings, oidc_approver_groups=["cgw-approvers"], oidc_admin_groups=["cgw-admins"])
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/"
    with db_mod.session_scope() as s:
        assert s.scalar(select(User).where(User.username == "newbie")).role == "admin"


def test_only_allowed_groups_may_sign_in(settings, idp):
    make_app(settings)
    make_user("sam", "editor")
    idp["claims"] = {"preferred_username": "sam", "groups": ["family"]}
    app = make_app(settings, oidc_allowed_groups=["cgw"])
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/login"
        assert "in a group that may use" in c.get("/login").text
    idp["claims"]["groups"] = ["CGW"]
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/"


def test_bad_state_nonce_and_inactive_accounts_are_refused(settings, idp):
    make_app(settings)
    make_user("sam", "editor")
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp, state="forged").headers["location"] == "/login"
        assert "expired" in c.get("/login").text
        c.get("/login/oidc", follow_redirects=False)  # a callback with no matching start
        assert c.get("/login/oidc/callback", params={"code": "abc", "state": "x"}, follow_redirects=False
                     ).headers["location"] == "/login"
    idp["claims"] = {"preferred_username": "sam", "nonce": "replayed"}
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/login"
        assert "expired" in c.get("/login").text
    idp["claims"] = {"preferred_username": "sam"}
    with db_mod.session_scope() as s:
        s.scalar(select(User).where(User.username == "sam")).is_active = False
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/login"
        assert "turned off" in c.get("/login").text
    idp["token_status"] = 400
    with TestClient(make_app(settings), client=("192.168.1.20", 50000)) as c:
        sign_in(c, idp)
        assert "refused" in c.get("/login").text


def test_userinfo_can_supply_the_username_and_groups(settings, idp):
    make_app(settings)
    make_user("sam", "contributor")
    idp["claims"] = {}
    idp["userinfo"] = {"preferred_username": "sam", "groups": ["cgw-editors"]}
    app = make_app(settings, oidc_editor_groups=["cgw-editors"])
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert sign_in(c, idp).headers["location"] == "/"
    with db_mod.session_scope() as s:
        assert s.scalar(select(User).where(User.username == "sam")).role == "editor"
