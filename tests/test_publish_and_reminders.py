import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio import mailer, publishers, reminders, timeutil
from studio import posts as post_svc
from studio.models import Post, User
from studio.publishers.bluesky import BlueskyClient, facets
from studio.security import Actor

from .conftest import jpeg_bytes, make_user


@pytest.fixture
def approved_post(app):
    """A photo post with a Bluesky version, approved, due 1 minute ago."""
    import io

    from studio.media import process, store_upload

    make_user("adam", "admin")
    with db_mod.session_scope() as s:
        adam = s.scalar(select(User).where(User.username == "adam"))
        actor = Actor("user", adam)
        asset = store_upload(s, io.BytesIO(jpeg_bytes()), "a.jpg", "image/jpeg", adam.id)
        process(asset)
        post = post_svc.create_post(s, actor, [asset.id], note="test")
        post_svc.update_post(s, actor, post, versions=[{
            "channel": "bluesky", "enabled": True, "body": "New class! https://columbiagadgetworks.org", "hashtags": "#makerspace",
            "scheduled_at": db_mod.utcnow() + timedelta(minutes=10)}])
        post_svc.submit_for_review(s, actor, post)
        post_svc.approve(s, actor, post, post_svc.approval_hash(post))
        # Make it due without editing content (the time is part of the approval, so set both).
        post.version("bluesky").scheduled_at = db_mod.utcnow() - timedelta(minutes=1)
        post.approved_hash = post_svc.approval_hash(post)
        return post.id


def fake_bluesky(calls):
    def handler(request: httpx.Request):
        path = request.url.path
        calls.append(path)
        if path.endswith("createSession"):
            return httpx.Response(200, json={"did": "did:plc:abc", "handle": "cgw.test", "accessJwt": "jwt"})
        if path.endswith("uploadBlob"):
            assert request.headers["Authorization"] == "Bearer jwt"
            return httpx.Response(200, json={"blob": {"$type": "blob", "ref": {"$link": "bafy"}, "mimeType": "image/jpeg", "size": 10}})
        if path.endswith("createRecord"):
            record = json.loads(request.content)["record"]
            assert record["text"].startswith("New class!")
            assert record["embed"]["images"][0]["image"]["ref"]["$link"] == "bafy"
            return httpx.Response(200, json={"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "c"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_facets_use_byte_offsets():
    text = "Café night #makerspace https://cgw.org/x."
    found = facets(text)
    data = text.encode()
    link = next(f for f in found if "link" in f["features"][0]["$type"])
    tag = next(f for f in found if "tag" in f["features"][0]["$type"])
    assert data[link["index"]["byteStart"]:link["index"]["byteEnd"]] == b"https://cgw.org/x"
    assert data[tag["index"]["byteStart"]:tag["index"]["byteEnd"]] == b"#makerspace"


def test_publishes_due_bluesky_post(approved_post):
    calls = []
    with db_mod.session_scope() as s:
        failed = publishers.publish_due(s, {"bluesky": lambda db: BlueskyClient(http=fake_bluesky(calls))})
        assert failed == []
    assert [c.rsplit(".", 1)[-1] for c in calls] == ["createSession", "uploadBlob", "createRecord"]
    with db_mod.session_scope() as s:
        post = s.get(Post, approved_post)
        v = post.version("bluesky")
        assert v.publish_state == "published"
        assert v.external_url == "https://bsky.app/profile/cgw.test/post/3kxyz"
        assert post.status == "done"


def test_tampered_post_is_blocked_not_sent(approved_post):
    with db_mod.session_scope() as s:
        s.get(Post, approved_post).version("bluesky").body = "Changed without approval"
    calls = []
    with db_mod.session_scope() as s:
        failed = publishers.publish_due(s, {"bluesky": lambda db: BlueskyClient(http=fake_bluesky(calls))})
        assert len(failed) == 1 and failed[0].last_error.startswith("Blocked")
    assert calls == []


def test_failures_retry_then_email(approved_post):
    def broken(db):
        raise RuntimeError("network down")
    for _ in range(publishers.MAX_ATTEMPTS):
        with db_mod.session_scope() as s:
            failed = publishers.publish_due(s, {"bluesky": broken})
            if failed:
                reminders.publish_failures(s, failed)
    with db_mod.session_scope() as s:
        v = s.get(Post, approved_post).version("bluesky")
        assert v.publish_state == "failed" and v.attempts == 3
    assert any("Publishing failed" in m["Subject"] for m in mailer.outbox)


def at_local(monkeypatch, when: datetime):
    utc = when.replace(tzinfo=db_mod.settings().timezone).astimezone(__import__("datetime").UTC).replace(tzinfo=None)
    monkeypatch.setattr(timeutil, "utcnow", lambda: utc)


def test_claude_session_reminder_once_per_day(app, monkeypatch):
    make_user("adam", "admin")
    from studio.models import Post as P
    with db_mod.session_scope() as s:
        for i in range(10):
            s.add(P(note=f"upload {i}", status="needs_claude"))
    at_local(monkeypatch, datetime(2026, 10, 14, 9, 0))
    with db_mod.session_scope() as s:
        assert reminders.claude_session(s) is True
        assert reminders.claude_session(s) is False  # already sent today
    msg = mailer.outbox[-1]
    assert "Claude session needed: 10 items" in msg["Subject"]
    assert "/cgw-session" in msg.get_content()


def test_batch_day_reminder_only_on_cycle_start(approved_post, monkeypatch):
    def schedule_instagram_in_3_days():
        with db_mod.session_scope() as s:
            p = s.get(Post, approved_post)
            v = p.version("instagram")
            v.enabled, v.body, v.scheduled_at = True, "Hi", timeutil.utcnow() + timedelta(days=3)
            p.approved_hash = post_svc.approval_hash(p)  # stands in for re-approval

    monkeypatch.setattr("studio.batch.utcnow", lambda: timeutil.utcnow())
    at_local(monkeypatch, datetime(2026, 10, 13, 9, 0))  # the day after the anchor batch day
    schedule_instagram_in_3_days()
    with db_mod.session_scope() as s:
        assert reminders.batch_day(s) is False

    at_local(monkeypatch, datetime(2026, 10, 26, 9, 0))  # two weeks after the anchor
    schedule_instagram_in_3_days()
    with db_mod.session_scope() as s:
        assert reminders.batch_day(s) is True
        assert reminders.batch_day(s) is False  # once per cycle
    assert "Batch day: Instagram 1" in mailer.outbox[-1]["Subject"]
