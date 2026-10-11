import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio import mailer, publishers, reminders, timeutil
from studio import posts as post_svc
from studio.models import Post, ReminderLog, User
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
    with db_mod.session_scope() as s:  # bundled into one alert email, at most one a day
        started_two_days_ago(s)
        assert reminders.flush_alerts(s) is True
        assert reminders.flush_alerts(s) is False
    assert sum("Publishing failed" in m["Subject"] for m in mailer.outbox) == 1


def at_local(monkeypatch, when: datetime):
    utc = when.replace(tzinfo=db_mod.settings().timezone).astimezone(__import__("datetime").UTC).replace(tzinfo=None)
    monkeypatch.setattr(timeutil, "utcnow", lambda: utc)


def started_two_days_ago(s):
    reminders.started_at(s)
    s.scalar(select(ReminderLog).where(ReminderLog.kind == "installed")).sent_at = timeutil.utcnow() - timedelta(days=2)


def test_quiet_first_day(app, monkeypatch):
    make_user("adam", "admin")
    from studio.models import Post as P
    at_local(monkeypatch, datetime(2026, 10, 12, 9, 0))
    with db_mod.session_scope() as s:
        for i in range(12):
            s.add(P(note=f"upload {i}", status="needs_claude", created_at=timeutil.utcnow() - timedelta(days=5)))
    with db_mod.session_scope() as s:
        reminders.run(s)  # first start: nothing for 24 hours
    assert mailer.outbox == []


def test_running_low_email_once_then_weekly_and_again_after_a_recovery(approved_post, monkeypatch):
    """One email when 1 (or 0) approved posts are left, one a week while it stays that low, and a fresh
    email straight away if it recovers and then dips again."""
    with db_mod.session_scope() as s:
        started_two_days_ago(s)
    left = {"n": 1}
    monkeypatch.setattr(reminders, "runway", lambda db: [
        (db.get(Post, approved_post), timeutil.utcnow() + timedelta(days=3)) for _ in range(left["n"])])

    def run_at(when):
        at_local(monkeypatch, when)
        with db_mod.session_scope() as s:
            reminders.run(s)
        return [m["Subject"] for m in mailer.outbox]

    assert run_at(datetime(2026, 10, 14, 10, 0)) == ["CGW Studio: only 1 post left scheduled"]
    assert len(run_at(datetime(2026, 10, 14, 15, 0))) == 1  # not again the same day
    assert len(run_at(datetime(2026, 10, 20, 10, 0))) == 1  # six days on: still quiet
    assert len(run_at(datetime(2026, 10, 21, 10, 0))) == 2  # a week on: the weekly reminder
    left["n"] = 2
    assert len(run_at(datetime(2026, 10, 22, 10, 0))) == 2  # plenty again: nothing, and the stretch resets
    left["n"] = 0
    subjects = run_at(datetime(2026, 10, 23, 10, 0))  # dipped again: emails at once
    assert len(subjects) == 3 and subjects[-1] == "CGW Studio: no posts left scheduled"
    body = mailer.outbox[-1].get_content()
    assert "/cgw-session" in body and "8 weeks" in body and "/settings" in body


def test_running_low_email_can_be_switched_off(approved_post, monkeypatch):
    at_local(monkeypatch, datetime(2026, 10, 14, 10, 0))
    with db_mod.session_scope() as s:
        started_two_days_ago(s)
        reminders.set_enabled(s, "runway", False)
        reminders.run(s)
    assert mailer.outbox == []


def test_problem_alerts_follow_their_switches(approved_post):
    with db_mod.session_scope() as s:
        reminders.alert(s, "announcement_failed", "1:3", "Monthly email not sent: x", urgent=True)
        reminders.alert(s, "publish_failed", "1:3", "Publishing failed: y", urgent=True)
        s.flush()
        assert [a.kind for a in s.scalars(select(reminders.PendingAlert)).all()] == ["announcement_failed", "publish_failed"]
        reminders.set_enabled(s, "alert_announcement", False)
        reminders.alert(s, "announcement_failed", "2:3", "Monthly email not sent: z", urgent=True)
        s.flush()
        assert sum(a.kind == "announcement_failed" for a in s.scalars(select(reminders.PendingAlert)).all()) == 1


def test_default_emails_are_running_low_and_alerts_only(app):
    with db_mod.session_scope() as s:
        on = {k for k, *_ in reminders.EMAIL_KINDS if reminders.enabled(s, k)}
    assert on == {"runway", "alert_publish", "alert_announcement"}
