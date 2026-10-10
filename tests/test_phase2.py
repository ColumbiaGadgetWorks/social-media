import dataclasses
import io
import json
import sqlite3
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from studio import calendar_sync, mailer, public_media, publishers, reminders
from studio import db as db_mod
from studio import posts as post_svc
from studio.media import process, store_upload
from studio.models import Event, MediaAsset, PendingAlert, Post, User
from studio.publishers.meta import FacebookClient, InstagramClient, ThreadsClient
from studio.publishers.website import WebsiteClient, build_files, description_from
from studio.security import Actor
from studio.timeutil import to_local

from .conftest import jpeg_bytes, make_user


@pytest.fixture
def app2(settings):
    from studio.app import create_app

    mailer.outbox.clear()
    s = dataclasses.replace(
        settings, public_url="https://studio.example.org", meta_page_id="PAGE", meta_page_token="PTOKEN",
        meta_ig_user_id="IGUSER", threads_user_id="THUSER", threads_token="THTOKEN", github_token="GHTOKEN",
        event_skip_keywords=["board meeting"],
    )
    app = create_app(s)
    make_user("adam", "admin")
    return app


def adam(s):
    return Actor("user", s.scalar(select(User).where(User.username == "adam")))


def approved_post(channels: dict[str, str], images=1, size=(1600, 1200), title="", video=False):
    """A post with `images` photos (or one video file), approved for the given channels."""
    with db_mod.session_scope() as s:
        actor = adam(s)
        ids = []
        for i in range(images):
            asset = store_upload(s, io.BytesIO(jpeg_bytes(size, (40 * i, 90, 120))), f"p{i}.jpg", "image/jpeg", actor.user.id)
            process(asset)
            ids.append(asset.id)
        if video:
            asset = store_upload(s, io.BytesIO(b"\x00" * 1000), "v.mp4", "video/mp4", actor.user.id)
            asset.processing_status = "ready"
            ids.append(asset.id)
        post = post_svc.create_post(s, actor, ids, note="test")
        when = db_mod.utcnow() + timedelta(minutes=10)
        post_svc.update_post(s, actor, post, versions=[
            {"channel": c, "enabled": True, "body": body, "title": title, "scheduled_at": when} for c, body in channels.items()])
        post_svc.submit_for_review(s, actor, post)
        post_svc.approve(s, actor, post, post_svc.approval_hash(post))
        return post.id


def version_of(post_id, channel):
    s = db_mod.state.SessionLocal()
    return s, s.get(Post, post_id).version(channel)


# --- channels and public media ---------------------------------------------------


def test_connected_channels_become_automatic(app2):
    from studio import channels as ch

    assert ch.mode("instagram") == "direct" and ch.mode("website") == "direct"
    assert ch.mode("tiktok") == "batch" and ch.mode("gbp") == "on_day"


def test_public_media_only_for_approved_posts(app2):
    from starlette.testclient import TestClient

    post_id = approved_post({"instagram": "Hello"}, size=(900, 1600))  # tall phone photo
    with db_mod.session_scope() as s:
        asset = s.get(Post, post_id).media[0]
        url = public_media.url(asset, "ig")
        bad = url.replace(url.rsplit("/", 1)[1], "0" * 32 + ".jpg")
    assert url.startswith("https://studio.example.org/m/")
    with TestClient(app2) as c:
        path = url.replace("https://studio.example.org", "")
        r = c.get(path)
        assert r.status_code == 200
        w, h = Image.open(io.BytesIO(r.content)).size
        assert abs(w / h - 0.8) < 0.01  # padded to Instagram's 4:5 minimum
        assert c.get(bad.replace("https://studio.example.org", "")).status_code == 404
        with db_mod.session_scope() as s:  # back to review: the link stops working
            post = s.get(Post, post_id)
            post_svc.request_changes(s, adam(s), post, "redo")
        assert c.get(path).status_code == 404


def test_post_with_video_must_be_alone(app2):
    with pytest.raises(post_svc.PostError, match="video"):
        approved_post({"facebook": "x"}, images=1, video=True)


# --- Meta publishers ----------------------------------------------------------------


class FakeGraph:
    def __init__(self, statuses=("IN_PROGRESS", "FINISHED")):
        self.calls, self.statuses, self.n = [], list(statuses), 0

    def __call__(self, request: httpx.Request):
        params = parse_qs(request.content.decode()) if request.method == "POST" else parse_qs(request.url.query.decode())
        params = {k: v[0] for k, v in params.items()}
        path = request.url.path.split("/", 2)[-1]  # drop the version / v1.0 prefix
        self.calls.append((request.method, path, params))
        self.n += 1
        if path.endswith("media_publish") or path.endswith("threads_publish"):
            return httpx.Response(200, json={"id": "PUBLISHED1"})
        if request.method == "GET" and params.get("fields") in ("status_code", "status"):
            return httpx.Response(200, json={params["fields"]: self.statuses.pop(0)})
        if request.method == "GET" and params.get("fields") == "permalink":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/abc/"})
        if path.endswith("/photos"):
            return httpx.Response(200, json={"id": f"PHOTO{self.n}", "post_id": f"PAGE_{self.n}"})
        if path.endswith("/feed"):
            return httpx.Response(200, json={"id": "PAGE_FEED1"})
        return httpx.Response(200, json={"id": f"C{self.n}"})


def test_instagram_single_photo(app2):
    post_id = approved_post({"instagram": "Single photo"})
    fake = FakeGraph()
    s, v = version_of(post_id, "instagram")
    url, media_id = InstagramClient(http=httpx.Client(transport=httpx.MockTransport(fake))).publish(v)
    s.close()
    assert (url, media_id) == ("https://www.instagram.com/p/abc/", "PUBLISHED1")
    method, path, params = fake.calls[0]
    assert path == "IGUSER/media" and params["caption"] == "Single photo" and "/m/" in params["image_url"]
    assert params["access_token"] == "PTOKEN"


def test_instagram_carousel_and_facebook_album(app2):
    post_id = approved_post({"instagram": "Three photos", "facebook": "Three photos"}, images=3)
    fake = FakeGraph()
    s, v = version_of(post_id, "instagram")
    InstagramClient(http=httpx.Client(transport=httpx.MockTransport(fake))).publish(v)
    children = [c for c in fake.calls if c[2].get("is_carousel_item") == "true"]
    carousel = next(c for c in fake.calls if c[2].get("media_type") == "CAROUSEL")
    assert len(children) == 3 and carousel[2]["children"].count(",") == 2

    fb = FakeGraph()
    v = s.get(Post, post_id).version("facebook")
    url, post_ref = FacebookClient(http=httpx.Client(transport=httpx.MockTransport(fb))).publish(v)
    s.close()
    unpublished = [c for c in fb.calls if c[2].get("published") == "false"]
    feed = next(c for c in fb.calls if c[1].endswith("/feed"))
    assert len(unpublished) == 3 and "attached_media[2]" in feed[2]
    assert url == "https://www.facebook.com/PAGE_FEED1"


def test_instagram_reel_waits_for_processing(app2):
    post_id = approved_post({"instagram": "A reel"}, images=0, video=True)
    fake = FakeGraph(statuses=("IN_PROGRESS", "IN_PROGRESS", "FINISHED"))
    s, v = version_of(post_id, "instagram")
    InstagramClient(http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda _: None).publish(v)
    s.close()
    assert fake.calls[0][2]["media_type"] == "REELS"
    assert sum(1 for c in fake.calls if c[2].get("fields") == "status_code") == 3


def test_threads_text_post(app2):
    post_id = approved_post({"threads": "Just words"}, images=0)
    fake = FakeGraph()
    s, v = version_of(post_id, "threads")
    ThreadsClient(http=httpx.Client(transport=httpx.MockTransport(fake))).publish(v)
    s.close()
    assert fake.calls[0][1] == "THUSER/threads" and fake.calls[0][2]["media_type"] == "TEXT"
    assert fake.calls[0][2]["access_token"] == "THTOKEN"


# --- website -----------------------------------------------------------------------


def test_website_files_follow_site_conventions(app2):
    body = "Members cast **bronze** parts. See the [metal shop](/tools/metal-shop/).\n\nMore text here."
    post_id = approved_post({"website": body}, images=2, title='Casting class: "recap"')
    s, v = version_of(post_id, "website")
    files = build_files(v, "casting-class-recap")
    s.close()
    md = files["content/news/casting-class-recap.md"].decode()
    assert set(files) == {"content/news/casting-class-recap.md", "assets/img/casting-class-recap.jpg", "assets/img/casting-class-recap-2.jpg"}
    assert 'title: "Casting class: \\"recap\\""' in md
    assert "image: casting-class-recap.jpg" in md and '{{< img src="casting-class-recap-2.jpg"' in md
    assert "description: \"Members cast bronze parts. See the metal shop." in md
    img = Image.open(io.BytesIO(files["assets/img/casting-class-recap.jpg"]))
    assert max(img.size) <= 1600 and not img.getexif()


def test_website_publish_makes_one_commit(app2):
    seen = []

    def gh(request: httpx.Request):
        path = request.url.path.replace("/repos/ColumbiaGadgetWorks/website/", "")
        seen.append((request.method, path))
        assert request.headers["Authorization"] == "Bearer GHTOKEN"
        if path.startswith("contents/"):
            return httpx.Response(200 if path.endswith("open-house.md") else 404, json={})
        if path == "git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "HEAD1"}})
        if path == "git/commits/HEAD1":
            return httpx.Response(200, json={"tree": {"sha": "TREE1"}})
        if path == "git/blobs":
            return httpx.Response(201, json={"sha": f"BLOB{len(seen)}"})
        if path == "git/trees":
            assert json.loads(request.content)["base_tree"] == "TREE1"
            return httpx.Response(201, json={"sha": "TREE2"})
        if path == "git/commits":
            assert json.loads(request.content)["parents"] == ["HEAD1"]
            return httpx.Response(201, json={"sha": "COMMIT2"})
        if path == "git/refs/heads/main":
            return httpx.Response(200, json={})
        return httpx.Response(500)

    post_id = approved_post({"website": "Come see the shop."}, title="Open house")
    s, v = version_of(post_id, "website")
    url, sha = WebsiteClient(http=httpx.Client(transport=httpx.MockTransport(gh))).publish(v)
    s.close()
    assert url == "https://columbiagadgetworks.org/news/open-house-2/"  # open-house.md already existed
    assert sha == "COMMIT2" and ("PATCH", "git/refs/heads/main") in seen


def test_website_blocks_script_html(app2):
    with pytest.raises(post_svc.PostError, match="script"):
        approved_post({"website": "Hi <script>alert(1)</script>"}, title="Bad")


def test_description_strips_markdown():
    assert description_from("Hello **world**, see [this](/x/).\n\n{{< img src=\"a.jpg\" >}}") == "Hello world, see this."


# --- publishing loop ---------------------------------------------------------------


def test_publish_due_records_platform_ids(app2):
    post_id = approved_post({"threads": "Due now"}, images=0)
    with db_mod.session_scope() as s:
        post = s.get(Post, post_id)
        post.version("threads").scheduled_at = db_mod.utcnow() - timedelta(minutes=1)
        post.approved_hash = post_svc.approval_hash(post)

    class Fake:
        def publish(self, version):
            return "https://www.threads.net/@cgw/post/x", "TH123"

    with db_mod.session_scope() as s:
        publishers.publish_due(s, {"threads": lambda db: Fake()})
    with db_mod.session_scope() as s:
        v = s.get(Post, post_id).version("threads")
        assert v.publish_state == "published" and v.external_id == "TH123"


# --- calendar ------------------------------------------------------------------------


def ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def vevent(uid, start, end=None, summary="Event", description="", rrule="", status="", location="The shop"):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART;TZID=America/Chicago:{start}", f"SUMMARY:{summary}",
             f"LOCATION:{location}"]
    if end:
        lines.append(f"DTEND;TZID=America/Chicago:{end}")
    if description:
        lines.append("DESCRIPTION:" + description.replace("\n", "\\n"))
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if status:
        lines.append(f"STATUS:{status}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


def day(offset: int, hhmm="1800") -> str:
    d = to_local(db_mod.utcnow()).date() + timedelta(days=offset)
    return f"{d:%Y%m%d}T{hhmm}00"


def calendar_text(class_offset=30, class_status="", class_time="1800", include_class=True):
    events = [
        vevent("hack@cgw", day(1), day(1, "2000"), "Open Hack Night", rrule="FREQ=WEEKLY;BYDAY=TH"),
        vevent("board@cgw", day(20, "2000"), summary="Quarterly board meeting"),
    ]
    if include_class:
        events.append(vevent("solder@cgw", day(class_offset, class_time), day(class_offset, "2000"),
                             "Intro to Soldering class", "Learn to solder.\nSignup: https://givebutter.com/solder\nPrice: $20",
                             status=class_status))
    return ics(*events)


def test_sync_creates_promos_for_one_off_and_weekly_events(app2):
    with db_mod.session_scope() as s:
        result = calendar_sync.sync(s, calendar_text())
    # The class, plus one post per weekly Hack Night inside the 10-day window (how many depends on today).
    assert result["promoted"] >= 2
    with db_mod.session_scope() as s:
        event = s.scalar(select(Event).where(Event.uid == "solder@cgw"))
        assert event.url == "https://givebutter.com/solder" and "Signup" not in event.description
        assert [p.purpose for p in event.posts] == ["announce", "reminder"]
        post = event.posts[0]
        assert post.status == "needs_claude" and post.media[0].tags[0] == "event-card"
        assert post.media[0].tags[1].startswith("card:")
        assert post.media[0].processing_status == "ready"
        assert (to_local(event.start) - to_local(post.target_at)).days == 14
        hack = s.scalars(select(Event).where(Event.uid == "hack@cgw", Event.series != "").order_by(Event.start)).all()
        horizon = db_mod.utcnow() + timedelta(days=10)
        assert hack and all([p.purpose for p in e.posts] == (["weekly"] if e.start < horizon else []) for e in hack)
        assert result["promoted"] == 1 + sum(1 for e in hack if e.start < horizon)
        assert not s.scalar(select(Event).where(Event.uid == "board@cgw")).posts
    with db_mod.session_scope() as s:  # a second sync changes nothing
        assert calendar_sync.sync(s, calendar_text()) == {"new": 0, "promoted": 0, "changed": 0, "cancelled": 0}


def test_event_change_clears_approval_and_moves_targets(app2):
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, calendar_text())
    with db_mod.session_scope() as s:
        event = s.scalar(select(Event).where(Event.uid == "solder@cgw"))
        post = event.posts[0]
        actor = adam(s)
        post_svc.update_post(s, actor, post, versions=[{"channel": "facebook", "enabled": True, "body": "Solder!",
                                                         "scheduled_at": db_mod.utcnow() + timedelta(days=5)}])
        post_svc.submit_for_review(s, actor, post)
        post_svc.approve(s, actor, post, post_svc.approval_hash(post))
        post_id, old_target = post.id, post.target_at
    with db_mod.session_scope() as s:  # moved two days later and an hour earlier
        result = calendar_sync.sync(s, calendar_text(class_offset=32, class_time="1700"))
    assert result["changed"] == 1 and result["new"] == 0
    with db_mod.session_scope() as s:
        post = s.get(Post, post_id)
        assert post.status == "in_review" and post.approved_hash is None
        assert "changed" in post.review_comment and post.target_at - old_target == timedelta(days=2)
    with db_mod.session_scope() as s:  # posts go out in 5 days: it waits for the weekly digest
        alert = s.scalar(select(PendingAlert).where(PendingAlert.kind == "event_changed"))
        assert alert and not alert.urgent and "Intro to Soldering" in alert.line
        assert "CALENDAR CHANGES" in reminders.digest(s)[1]
    assert mailer.outbox == []


def test_cancelled_event_pulls_posts(app2):
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, calendar_text())
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, calendar_text(class_status="CANCELLED"))
    with db_mod.session_scope() as s:
        event = s.scalar(select(Event).where(Event.uid == "solder@cgw"))
        assert event.status == "cancelled"
        assert {p.status for p in event.posts} == {"rejected"}
        alert = s.scalar(select(PendingAlert).where(PendingAlert.kind == "event_cancelled"))
        assert alert and not alert.urgent  # nothing was announced or approved yet


def test_sync_redraws_a_stale_event_card_and_reopens_approved_posts(app2):
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, calendar_text())
    with db_mod.session_scope() as s:
        event = s.scalar(select(Event).where(Event.uid == "solder@cgw"))
        post, actor = event.posts[0], adam(s)
        post_svc.update_post(s, actor, post, versions=[{"channel": "facebook", "enabled": True, "body": "Solder!",
                                                         "scheduled_at": db_mod.utcnow() + timedelta(days=5)}])
        post_svc.submit_for_review(s, actor, post)
        post_svc.approve(s, actor, post, post_svc.approval_hash(post))
        post_id, old_card = post.id, event.card_media_id
        s.get(MediaAsset, old_card).tags = ["event-card"]  # a card drawn before the tag existed, i.e. an older design
    with db_mod.session_scope() as s:
        assert calendar_sync.sync(s, calendar_text()).get("refreshed") == 1
    with db_mod.session_scope() as s:
        event = s.scalar(select(Event).where(Event.uid == "solder@cgw"))
        assert event.card_media_id != old_card
        post = s.get(Post, post_id)
        assert [m.id for m in post.media] == [event.card_media_id]
        assert post.status == "in_review" and post.approved_hash is None and "redrawn" in post.review_comment
        assert all([m.id for m in p.media] == [event.card_media_id] for p in event.posts)
    with db_mod.session_scope() as s:  # the new card is current, so nothing more happens
        assert "refreshed" not in calendar_sync.sync(s, calendar_text())


def test_deleted_event_is_cancelled_after_two_missing_syncs(app2):
    with db_mod.session_scope() as s:
        calendar_sync.sync(s, calendar_text())
    for expected in ("active", "cancelled"):
        with db_mod.session_scope() as s:
            calendar_sync.sync(s, calendar_text(include_class=False))
        with db_mod.session_scope() as s:
            assert s.scalar(select(Event).where(Event.uid == "solder@cgw")).status == expected


def test_event_card_renders(app2, tmp_path):
    start = db_mod.utcnow() + timedelta(days=10)
    event = Event(title="Intro to Soldering: make a blinky badge you can wear home the same night",
                  start=start, end=start + timedelta(hours=2), location="Columbia Gadget Works, 1103 E Broadway",
                  description="A beginner class.")
    img = Image.open(io.BytesIO(calendar_sync.render_card(event, "$20")))
    assert img.size == (1080, 1350)
    corner = img.crop((860, 60, 990, 225))  # the logo's white gear sits here on the orange
    assert max(sum(px) for px in corner.getdata()) > 720
    img.save(tmp_path / "card.jpg")


def test_cancelled_event_card_drops_time_and_kicker(app2, monkeypatch):
    start = db_mod.utcnow() + timedelta(days=10)
    event = Event(title="Cancelled: Open Hack Night (Thanksgiving)", start=start, end=start + timedelta(hours=2),
                  location="Columbia Gadget Works", description="Closed for Thanksgiving.")
    assert calendar_sync.is_cancelled(event)
    assert not calendar_sync.is_cancelled(Event(title="Open Hack Night", start=start, status="active"))
    texts = []
    real_text = calendar_sync.ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        texts.append(text)
        return real_text(self, xy, text, *a, **kw)

    monkeypatch.setattr(calendar_sync.ImageDraw.ImageDraw, "text", spy)
    calendar_sync.render_card(event)
    assert "COLUMBIA GADGET WORKS" in texts and not any("EVENT" in t for t in texts)
    assert not any(" to " in t and "M" in t for t in texts)


# --- planning and reminders ------------------------------------------------------------


def test_gaps_and_schedule_dry_reminder(app2, monkeypatch):
    from studio import queue

    with db_mod.session_scope() as s:
        found = queue.gaps(s)
        assert found["weeks"][1]["missing"] == 3 and found["gbp_this_cycle"] == 0
        subject, body = reminders.digest(s)
    assert subject and "SCHEDULE GAPS" in body and "/cgw-plan" in body


# --- migration ---------------------------------------------------------------------------


def test_old_database_gets_new_columns(settings):
    settings.data_dir.mkdir(parents=True)
    con = sqlite3.connect(settings.data_dir / "studio.db")
    con.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, title VARCHAR(200), note TEXT, pillar VARCHAR(48), "
                "status VARCHAR(16), source VARCHAR(16), claude_notes TEXT, review_comment TEXT, created_by_id INTEGER, "
                "created_at DATETIME, updated_at DATETIME, submitted_at DATETIME, approved_hash VARCHAR(64), "
                "approved_by_id INTEGER, approved_at DATETIME)")
    con.execute("INSERT INTO posts (id, note, status) VALUES (1, 'old post', 'draft')")
    con.commit()
    con.close()
    db_mod.init(settings)
    with db_mod.session_scope() as s:
        post = s.get(Post, 1)
        assert post.note == "old post" and post.purpose == "" and post.event_id is None
