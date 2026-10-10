"""Discord uploads channel → media library, against a stand-in Discord API."""

import dataclasses
import json
from datetime import UTC, timedelta

import httpx
import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio import discord_ingest, mailer
from studio.models import AuditLog, Event, MediaAsset, Post

from .conftest import jpeg_bytes, make_user

CHANNEL = "111"


@pytest.fixture
def app_discord(settings):
    from studio.app import create_app

    mailer.outbox.clear()
    app = create_app(dataclasses.replace(settings, discord_bot_token="BOT", discord_channel_ids=[CHANNEL]))
    make_user("adam", "admin")
    return app


def ts(delta=timedelta(0)) -> str:
    return (db_mod.utcnow() + delta).replace(tzinfo=UTC).isoformat()


class FakeDiscord:
    def __init__(self):
        self.messages: list[dict] = []
        self.reactions: list[tuple[str, str]] = []
        self.replies: list[str] = []
        self.files = {"https://cdn.test/a.jpg": jpeg_bytes(), "https://cdn.test/b.jpg": jpeg_bytes((800, 600)),
                      "https://cdn.test/n.txt": b"hello"}

    def add(self, mid, content="", files=(), bot=False, name="Sam"):
        self.messages.append({
            "id": mid, "content": content, "timestamp": ts(),
            "author": {"username": name.lower(), "global_name": name, "bot": bot},
            "attachments": [{"id": f"{mid}{i}", "filename": url.rsplit("/", 1)[1], "url": url,
                             "size": len(self.files[url]),
                             "content_type": "image/jpeg" if url.endswith(".jpg") else "text/plain"}
                            for i, url in enumerate(files)],
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("discord.com", "cdn.test")
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=self.files[str(request.url)])
        assert request.headers["authorization"] == "Bot BOT"
        path = request.url.path
        if request.method == "GET" and path.endswith("/messages"):
            after = request.url.params.get("after")
            found = [m for m in self.messages if after is None or int(m["id"]) > int(after)]
            found = sorted(found, key=lambda m: -int(m["id"]))[: int(request.url.params.get("limit", 50))]
            return httpx.Response(200, json=found)
        if request.method == "PUT" and "/reactions/" in path:
            self.reactions.append((path.split("/messages/")[1].split("/")[0], path.split("/reactions/")[1].split("/")[0]))
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/messages"):
            self.replies.append(json.loads(request.content)["content"])
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def client(self):
        return discord_ingest.Discord("BOT", http=httpx.Client(transport=httpx.MockTransport(self.handler)))


def test_channel_photos_become_uploads(app_discord):
    fake = FakeDiscord()
    fake.add("100", "old message before the bot joined", ["https://cdn.test/a.jpg"])
    api = fake.client()
    with db_mod.session_scope() as s:
        assert discord_ingest.poll(s, api) == 0  # first run starts from now: history isn't imported
    fake.add("101", "Sam's walnut ukulele <@123>, first laser-engraved rosette",
             ["https://cdn.test/a.jpg", "https://cdn.test/b.jpg"])
    fake.add("102", "just chatting")
    fake.add("103", "bot post", ["https://cdn.test/a.jpg"], bot=True)
    fake.add("104", "#library shop overview", ["https://cdn.test/b.jpg"], name="Jo")
    fake.add("105", "notes", ["https://cdn.test/n.txt"])
    with db_mod.session_scope() as s:
        assert discord_ingest.poll(s, api) == 3
        assert discord_ingest.poll(s, api) == 0  # nothing twice
    with db_mod.session_scope() as s:
        media = s.scalars(select(MediaAsset).order_by(MediaAsset.id)).all()
        assert len(media) == 3 and all(m.tags == ["discord"] for m in media)
        assert media[0].note == "Sam's walnut ukulele, first laser-engraved rosette (sent by Sam on Discord)"
        posts = s.scalars(select(Post)).all()
        assert len(posts) == 1 and posts[0].source == "discord" and posts[0].status == "needs_claude"
        assert len(posts[0].media) == 2  # one carousel for the message
        assert "library" not in media[2].note.lower() and media[2].note.startswith("shop overview")
        assert s.scalar(select(AuditLog).where(AuditLog.action == "media_uploaded")).detail["via"] == "discord"
    assert {m for m, _ in fake.reactions} == {"101", "104"}  # ✅; chat and non-media files are left alone
    assert fake.replies == []


def test_photos_during_hack_night_are_taken_at_it(app_discord):
    with db_mod.session_scope() as s:
        s.add(Event(uid="h@site", start=db_mod.utcnow() - timedelta(hours=1), end=db_mod.utcnow() + timedelta(hours=1),
                    title="Open Hack Night", series="open hack night|Thu|18:00"))
    fake = FakeDiscord()
    api = fake.client()
    with db_mod.session_scope() as s:
        discord_ingest.poll(s, api)
    fake.add("200", "first print!", ["https://cdn.test/a.jpg"])
    with db_mod.session_scope() as s:
        discord_ingest.poll(s, api)
        event = s.scalar(select(Event))
        assert s.scalar(select(MediaAsset)).event_id == event.id


def test_too_big_gets_a_warning(app_discord):
    fake = FakeDiscord()
    api = fake.client()
    with db_mod.session_scope() as s:
        discord_ingest.poll(s, api)
    fake.add("300", "huge", ["https://cdn.test/a.jpg"])
    fake.messages[-1]["attachments"][0]["size"] = 10**12
    with db_mod.session_scope() as s:
        discord_ingest.poll(s, api)
        assert s.scalar(select(MediaAsset)) is None
    assert fake.replies and "larger than" in fake.replies[0]
