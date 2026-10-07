import dataclasses

import pytest
from starlette.testclient import TestClient

from studio.app import create_app

from .conftest import PASSWORD, make_user


@pytest.fixture
def proxied_app(settings):
    return create_app(dataclasses.replace(settings, require_proxy_auth=True))


def sign_in(client, headers):
    import re
    page = client.get("/login", headers=headers)
    if page.status_code != 200:
        return page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return client.post("/login", headers=headers, follow_redirects=False,
                       data={"csrf": csrf, "username": "adam", "password": PASSWORD})


def test_direct_access_without_proxy_is_refused(proxied_app):
    make_user("adam", "admin")
    with TestClient(proxied_app, client=("192.168.1.20", 50000)) as c:
        assert c.get("/login").status_code == 401


def test_header_from_untrusted_address_is_ignored(proxied_app):
    make_user("adam", "admin")
    with TestClient(proxied_app, client=("192.168.1.20", 50000)) as c:
        assert c.get("/login", headers={"Remote-User": "adam"}).status_code == 401


def test_trusted_proxy_with_matching_user_can_sign_in(proxied_app):
    make_user("adam", "admin")
    with TestClient(proxied_app, client=("10.9.9.9", 50000)) as c:
        r = sign_in(c, {"Remote-User": "adam"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/", headers={"Remote-User": "adam"}).status_code == 200
        # Same session, different proxy user: signed out.
        assert c.get("/", headers={"Remote-User": "someone-else"}, follow_redirects=False).headers["location"] == "/login"


def test_proxy_user_must_match_studio_account(proxied_app):
    make_user("adam", "admin")
    with TestClient(proxied_app, client=("10.9.9.9", 50000)) as c:
        r = sign_in(c, {"Remote-User": "sam"})
        assert r.headers["location"] == "/login"
