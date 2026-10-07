import io
import re
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from starlette.testclient import TestClient

from studio import db as db_mod
from studio import mailer
from studio.config import PRIVATE_NETWORKS, Settings, _networks
from studio.models import User
from studio.security import hash_password

PASSWORD = "correct horse battery"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        media_dir=tmp_path / "media",
        secret_key="test-secret",
        base_url="http://studio.test",
        timezone=ZoneInfo("America/Chicago"),
        require_proxy_auth=False,
        proxy_user_header="Remote-User",
        trusted_proxies=_networks("10.9.9.9/32"),
        mcp_allowed_networks=_networks(PRIVATE_NETWORKS),
        mail_backend="memory",
        reminder_emails=["adam@example.org"],
        bluesky_handle="cgw.test",
        bluesky_app_password="app-pass",
        batch_anchor=date(2026, 10, 12),
        scheduler_enabled=False,
    )


@pytest.fixture
def app(settings):
    from studio.app import create_app

    mailer.outbox.clear()
    return create_app(settings)


def make_user(username: str, role: str) -> User:
    with db_mod.session_scope() as s:
        user = User(username=username, role=role, email=f"{username}@example.org", password_hash=hash_password(PASSWORD))
        s.add(user)
        s.flush()
        return user


def login(client: TestClient, username: str) -> None:
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"csrf": csrf, "username": username, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def csrf_of(client: TestClient, path: str = "/") -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


@pytest.fixture
def client(app):
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        yield c


@pytest.fixture
def approver_client(app):
    make_user("adam", "admin")
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        yield c


def jpeg_bytes(size=(1600, 1200), color=(200, 120, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()
