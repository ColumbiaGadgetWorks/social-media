"""Posting to Discord #shop-news and to the LinkedIn company page, against stand-in APIs."""

import dataclasses
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from studio import channels as ch
from studio import db as db_mod
from studio import mailer
from studio.models import Credential, Post
from studio.publishers import discord as discord_pub
from studio.publishers import linkedin

from .conftest import csrf_of, login, make_user
from .test_phase2 import approved_post, version_of


@pytest.fixture
def app_pub(settings):
    from studio.app import create_app

    mailer.outbox.clear()
    app = create_app(dataclasses.replace(
        settings, discord_bot_token="BOT", discord_post_channel_id="555",
        linkedin_client_id="CID", linkedin_client_secret="SECRET", linkedin_org_id="777"))
    make_user("adam", "admin")
    linkedin._forget_cache()
    return app


class FakeDiscord:
    def __init__(self, channel_type=5):
        self.sent, self.crossposted, self.channel_type = [], [], channel_type

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bot BOT"
        path = request.url.path
        if request.method == "GET" and path.endswith("/channels/555"):
            return httpx.Response(200, json={"id": "555", "guild_id": "999", "type": self.channel_type})
        if request.method == "POST" and path.endswith("/crosspost"):
            self.crossposted.append(path)
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/channels/555/messages"):
            body = request.content
            if request.headers["content-type"].startswith("multipart/"):
                payload = json.loads(body.split(b'name="payload_json"\r\n\r\n')[1].split(b"\r\n--")[0])
                payload["files"] = body.count(b'name="files[')
            else:
                payload = json.loads(body)
                payload["files"] = 0
            self.sent.append(payload)
            return httpx.Response(200, json={"id": "4242"})
        return httpx.Response(404)


def test_discord_posts_photos_and_crossposts(app_pub):
    assert ch.mode("discord") == "direct"
    post_id = approved_post({"discord": "New in the shop: the laser has opinions. Come see it Thursday."}, images=3)
    fake = FakeDiscord()
    client = discord_pub.DiscordClient(http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    s, v = version_of(post_id, "discord")
    url, msg_id = client.publish(v)
    s.close()
    assert url == "https://discord.com/channels/999/555/4242" and msg_id == "4242"
    sent = fake.sent[0]
    assert sent["files"] == 3 and sent["content"].startswith("New in the shop") and sent["allowed_mentions"] == {"parse": []}
    assert fake.crossposted  # an Announcement channel also reaches servers that follow it


def test_discord_text_only_in_a_normal_channel(app_pub):
    post_id = approved_post({"discord": "Hack Night tonight at 6, free."}, images=0)
    fake = FakeDiscord(channel_type=0)
    client = discord_pub.DiscordClient(http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    s, v = version_of(post_id, "discord")
    client.publish(v)
    s.close()
    assert fake.sent[0]["files"] == 0 and fake.crossposted == []


def test_little_text_escapes_and_tags():
    assert linkedin.little_text("Class (Nov 3) @ CGW: 3D_printing #makerspace") == \
        "Class \\(Nov 3\\) \\@ CGW: 3D\\_printing {hashtag|\\#|makerspace}"


class FakeLinkedIn:
    def __init__(self):
        self.posts, self.uploads, self.token_calls = [], [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(linkedin.TOKEN_URL):
            form = dict(httpx.QueryParams(request.content.decode()))
            self.token_calls.append(form)
            assert form["client_id"] == "CID" and form["client_secret"] == "SECRET"
            return httpx.Response(200, json={"access_token": f"AT-{len(self.token_calls)}", "expires_in": 5184000,
                                             "refresh_token": "RT", "refresh_token_expires_in": 31536000})
        if url.startswith("https://upload.test/"):
            self.uploads.append(len(request.content))
            return httpx.Response(201)
        assert request.headers["authorization"].startswith("Bearer AT-")
        assert request.headers["linkedin-version"] and request.headers["x-restli-protocol-version"] == "2.0.0"
        if request.url.path == "/rest/images" and request.url.params.get("action") == "initializeUpload":
            n = len(self.uploads)
            return httpx.Response(200, json={"value": {"uploadUrl": f"https://upload.test/{n}", "image": f"urn:li:image:{n}"}})
        if request.url.path == "/rest/posts":
            self.posts.append(json.loads(request.content))
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:123"})
        return httpx.Response(404, text=url)


def test_linkedin_connect_publish_and_refresh(app_pub, monkeypatch):
    fake = FakeLinkedIn()
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.Client
    monkeypatch.setattr(linkedin.httpx, "Client", lambda **kw: real_client(transport=transport))
    assert ch.mode("linkedin") == "batch"  # set up but not connected yet

    with TestClient(app_pub, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        assert "Connect LinkedIn" in c.get("/settings").text
        r = c.post("/settings/linkedin/connect", data={"csrf": csrf_of(c, "/settings")}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(linkedin.AUTH_URL)
        assert "w_organization_social" in r.headers["location"]
        state = dict(httpx.URL(r.headers["location"]).params)["state"]
        bad = c.get("/settings/linkedin/callback", params={"code": "X", "state": "wrong"})
        assert "expired" in bad.text
        r = c.post("/settings/linkedin/connect", data={"csrf": csrf_of(c, "/settings")}, follow_redirects=False)
        state = dict(httpx.URL(r.headers["location"]).params)["state"]
        page = c.get("/settings/linkedin/callback", params={"code": "CODE", "state": state})
        assert "LinkedIn connected" in page.text and "Disconnect" in page.text
    assert fake.token_calls[-1]["grant_type"] == "authorization_code"
    linkedin._forget_cache()
    assert ch.mode("linkedin") == "direct"

    post_id = approved_post({"linkedin": "Free CAD class (Nov 3) for #makerspace newcomers"}, images=2)
    with db_mod.session_scope() as s:
        client = linkedin.LinkedInClient(s)
        url, urn = client.publish(s.get(Post, post_id).version("linkedin"))
    assert url == "https://www.linkedin.com/feed/update/urn:li:share:123/" and urn == "urn:li:share:123"
    body = fake.posts[0]
    assert body["author"] == "urn:li:organization:777" and body["commentary"].endswith("{hashtag|\\#|makerspace} newcomers")
    assert [i["id"] for i in body["content"]["multiImage"]["images"]] == ["urn:li:image:0", "urn:li:image:1"]
    assert len(fake.uploads) == 2

    with db_mod.session_scope() as s:  # refreshed only when under 10 days are left
        assert linkedin.refresh(s) is False
        s.get(Credential, "linkedin_expires").value = (db_mod.utcnow() + timedelta(days=3)).isoformat()
        assert linkedin.refresh(s) is True
        assert s.get(Credential, "linkedin_access").value == "AT-2"  # the bad-state try never reached LinkedIn
    assert fake.token_calls[-1]["grant_type"] == "refresh_token"

    with TestClient(app_pub, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        c.post("/settings/linkedin/disconnect", data={"csrf": csrf_of(c, "/settings")})
    with db_mod.session_scope() as s:
        assert s.scalars(select(Credential).where(Credential.key.like("linkedin%"))).all() == []
    linkedin._forget_cache()
    assert ch.mode("linkedin") == "batch"
