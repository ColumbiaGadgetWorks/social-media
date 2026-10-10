"""Weekly events (Open Hack Night): one varied post a week with real photos, plus the Settings and
queue changes that came with them."""

import dataclasses
import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from studio import calendar_sync, mailer, queue, reminders
from studio import db as db_mod
from studio.media import process, store_upload
from studio.models import Event, MediaAsset, Post, User
from studio.posts import create_post
from studio.security import Actor, create_api_token
from studio.timeutil import to_local

from .conftest import csrf_of, jpeg_bytes, login, make_user
from .test_mcp import MCP
from .test_phase2 import day, ics, vevent


@pytest.fixture
def app3(settings):
    from studio.app import create_app

    mailer.outbox.clear()
    app = create_app(dataclasses.replace(settings, event_skip_keywords=["board meeting"]))
    make_user("adam", "admin")
    return app


def flattened_feed(offsets=(2, 9, 16, 23)) -> str:
    """Like the website's feed: every Hack Night is its own event with its own UID, no RRULE."""
    events = [vevent(f"hack{o}@site", day(o), day(o, "2000"), "Open Hack Night", "Free and open to everyone.")
              for o in offsets]
    events.append(vevent("class@site", day(5), day(5, "2000"), "Free public class"))
    events.append(vevent("class2@site", day(33), day(33, "2000"), "Free public class"))  # monthly, not weekly
    return ics(*events)


def photo(s, note="", event_id=None, created=None) -> int:
    user = s.scalar(select(User))
    asset = store_upload(s, io.BytesIO(jpeg_bytes()), "p.jpg", "image/jpeg", user.id, note)
    process(asset)
    asset.event_id = event_id
    if created:
        asset.created_at = created
    return asset.id


def test_flattened_weekly_feed_gets_one_post_a_week(app3):
    with db_mod.session_scope() as s:
        result = calendar_sync.sync(s, flattened_feed())
    assert result["promoted"] == 4  # two Hack Nights inside the 10-day window, plus two one-off classes
    with db_mod.session_scope() as s:
        hack = s.scalars(select(Event).where(Event.title == "Open Hack Night").order_by(Event.start)).all()
        assert len({e.series for e in hack}) == 1 and hack[0].series.startswith("open hack night|")
        posts = [p for e in hack for p in e.posts]
        assert [p.purpose for p in posts] == ["weekly", "weekly"]
        assert posts[0].angle != posts[1].angle  # rotated
        assert all(p.pillar == "hack_night" and p.status == "needs_claude" for p in posts)
        # The post goes out the day before, late morning.
        assert (to_local(hack[0].start).date() - to_local(posts[0].target_at).date()).days == 1
        classes = s.scalars(select(Event).where(Event.title == "Free public class")).all()
        assert all(not e.series for e in classes)
        assert sorted(p.purpose for e in classes for p in e.posts) == ["announce", "announce", "reminder"]
    with db_mod.session_scope() as s:  # nothing new on a second sync
        assert calendar_sync.sync(s, flattened_feed())["promoted"] == 0


def test_weekly_post_uses_fresh_photos_and_shows_history(app3):
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, flattened_feed(offsets=(11, 18)))  # nothing inside the window yet
        series = s.scalar(select(Event).where(Event.title == "Open Hack Night")).series
        last_week = Event(uid="past@site", start=db_mod.utcnow() - timedelta(days=5), title="Open Hack Night",
                          series=series, promote=True, promos_created=True)
        s.add(last_week)
        s.flush()
        tagged = [photo(s, "Sam's walnut ukulele", event_id=last_week.id) for _ in range(2)]
        during = photo(s, "", created=last_week.start + timedelta(hours=1))  # uploaded during the session
        old_post = create_post(s, Actor.system(), [], note="old")
        old_post.event_id, old_post.purpose, old_post.angle = last_week.id, "weekly", "first_timer"
        old_post.status = "done"
        unrelated = photo(s, "board photo", created=db_mod.utcnow() - timedelta(days=60))
    with db_mod.session_scope() as s:
        settings = db_mod.settings()
        object.__setattr__(settings, "weekly_promo_days", 12)
        try:
            calendar_sync.sync(s, flattened_feed(offsets=(11, 18)))
        finally:
            object.__setattr__(settings, "weekly_promo_days", 10)
    with db_mod.session_scope() as s:
        post = s.scalar(select(Post).where(Post.purpose == "weekly", Post.status == "needs_claude"))
        assert post.angle in ("project_spotlight", "photo_recap")
        media = [m.id for m in post.media]
        assert set(media) <= set(tagged + [during]) and media[0] in tagged  # tagged photos rank first
        context = calendar_sync.series_context(s, post)
        assert context["needs_photos"] is False
        assert context["recent_posts"][0]["angle"] == "first_timer"
        assert unrelated not in [c["media_id"] for c in context["candidate_media"]]
        post_id = post.id
    # Claude sees the weekly block and candidate previews through MCP, and can record its angle.
    with TestClient(app3, client=("192.168.1.20", 50000)) as web:
        with db_mod.session_scope() as s:
            token = create_api_token(s, s.scalar(select(User)), "t")
        mcp = MCP(web, token).init()
        result = mcp.call("get_work_item", post_id=post_id)
        summary = json.loads(result["content"][0]["text"])
        assert summary["weekly"]["angle"] == post.angle and "recent_posts" in summary["weekly"]
        assert "must read differently" in summary["event_instructions"]
        mcp.call("submit_drafts", post_id=post_id, angle="humor", submit_for_review=False,
                 versions=[{"channel": "facebook", "body": "The laser has opinions. Thursday 6 pm, free."}])
    with db_mod.session_scope() as s:
        assert s.get(Post, post_id).angle == "humor"


def test_no_photos_means_card_and_a_non_photo_angle(app3):
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, flattened_feed(offsets=(2, 9)))
    with db_mod.session_scope() as s:
        post = s.scalars(select(Post).where(Post.purpose == "weekly")).first()
        assert post.angle in ("first_timer", "tool_spotlight", "fix_it", "question", "humor")
        assert [m.tags[0] for m in post.media] == ["event-card"]
        assert calendar_sync.series_context(s, post)["needs_photos"] is True


def test_old_announce_reminder_pairs_are_replaced(app3):
    feed = flattened_feed(offsets=(2, 9))
    with db_mod.session_scope() as s:  # as the previous version saw it: two one-off events
        for o in calendar_sync.parse(feed, db_mod.utcnow(), db_mod.utcnow() + timedelta(days=60)):
            if o["title"] != "Open Hack Night":
                continue
            event = Event(uid=o["uid"], start=o["start"], end=o["end"], title=o["title"], facts_hash=calendar_sync.facts_hash(o))
            s.add(event)
            s.flush()
            calendar_sync.create_promos(s, event)
        assert len(s.scalars(select(Post)).all()) >= 2
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, feed)
    with db_mod.session_scope() as s:
        old = s.scalars(select(Post).join(Event, Post.event_id == Event.id).where(
            Post.purpose.in_(("announce", "reminder")), Event.title == "Open Hack Night")).all()
        assert old and all(p.status == "rejected" and "Replaced" in p.review_comment for p in old)
        weekly = s.scalars(select(Post).where(Post.purpose == "weekly")).all()
        assert len(weekly) == 2 and all(p.status == "needs_claude" for p in weekly)


def test_queue_puts_uploads_before_far_off_event_posts(app3):
    with db_mod.session_scope() as s:
        far = create_post(s, Actor.system(), [], note="event", source="event")
        far.target_at = db_mod.utcnow() + timedelta(days=12)
        s.flush()
        upload = create_post(s, Actor.system(), [], note="upload")
        upload.created_at = db_mod.utcnow() + timedelta(minutes=1)  # uploaded after the event post was made
        soon = create_post(s, Actor.system(), [], note="soon", source="event")
        soon.target_at = db_mod.utcnow() + timedelta(days=1)
        s.flush()
        assert [p.note for p in queue.claude_queue(s)] == ["soon", "upload", "event"]


def test_digest_reminds_about_weekly_event_photos(app3):
    with db_mod.session_scope() as s:
        s.add(Event(uid="t@site", start=db_mod.utcnow() + timedelta(days=2), title="Open Hack Night",
                    series="open hack night|Thu|18:00", promote=True))
        s.flush()
        assert "Taken at" in reminders.digest(s)[1]


def test_upload_taken_at_and_settings_snippets(app3):
    with db_mod.session_scope() as s:
        event = Event(uid="now@site", start=db_mod.utcnow() - timedelta(minutes=30), title="Open Hack Night",
                      series="open hack night|Thu|18:00")
        s.add(event)
        s.flush()
        event_id = event.id
    with TestClient(app3, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        page = c.get("/upload")
        assert f'value="{event_id}" selected' in page.text
        c.post("/upload", data={"csrf": csrf_of(c, "/upload"), "note": "laser", "mode": "together",
                                "event_id": str(event_id)}, files=[("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))])
        with db_mod.session_scope() as s:
            assert s.scalar(select(MediaAsset)).event_id == event_id
        page = c.post("/settings/tokens", data={"csrf": csrf_of(c, "/settings"), "name": "pc", "scope": "mcp"}).text
        assert "http://testserver/mcp" in page and "--scope user" in page and "mcp-remote" in page
        assert "(from this browser)" in page
    with TestClient(app3, client=("10.9.9.9", 50000)) as c:  # through the proxy: no address to guess
        login(c, "adam")
        page = c.post("/settings/tokens", data={"csrf": csrf_of(c, "/settings"), "name": "pc2", "scope": "mcp"}).text
        assert "&lt;unraid-lan-ip&gt;:&lt;port&gt;/mcp" in page
    object.__setattr__(db_mod.settings(), "lan_url", "http://192.168.1.50:8095")
    with TestClient(app3, client=("10.9.9.9", 50000)) as c:
        login(c, "adam")
        page = c.post("/settings/tokens", data={"csrf": csrf_of(c, "/settings"), "name": "ext", "scope": "extension"}).text
        assert 'id="ext-addr">http://192.168.1.50:8095' in page


def test_favicon_and_theme(app3):
    with TestClient(app3, client=("192.168.1.20", 50000)) as c:
        r = c.get("/favicon.ico", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/static/favicon.ico"
        assert c.get("/static/favicon.ico").status_code == 200
        assert "--accent: #bf4d28" in c.get("/static/app.css").text
        assert 'rel="icon"' in c.get("/login").text


def test_colorway_setting_and_asset_versions(app3):
    from .conftest import csrf_of, login

    with TestClient(app3, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        page = c.get("/").text
        assert 'data-theme="orange"' in page and "data-mode" not in page.split("<head>")[0]
        assert "/static/app.css?v=" in page
        c.post("/settings/appearance", data={"csrf": csrf_of(c, "/settings"), "theme": "teal", "color_mode": "dark"})
        page = c.get("/").text
        assert 'data-theme="teal" data-mode="dark"' in page
        c.post("/settings/appearance", data={"csrf": csrf_of(c, "/settings"), "theme": "pink", "color_mode": "auto"})
        page = c.get("/").text
        assert 'data-theme="teal"' in page and 'data-mode=' not in page.split("<head>")[0]
        assert ':root[data-theme="teal"][data-mode="dark"]' in c.get("/static/app.css").text
