import dataclasses
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from studio import announcements as ann_mod
from studio import db as db_mod
from studio import mailer, timeutil
from studio.dolibarr import Dolibarr
from studio.models import Announcement, AnnouncementRecipient, Event, Unsubscribe, User
from studio.security import Actor

from .conftest import csrf_of, login, make_user

CONTACTS = [
    {"id": "11", "email": "ann@example.org", "firstname": "Ann", "statut": "1", "no_email": "0"},
    {"id": "12", "email": "ANN@example.org", "firstname": "Dup", "statut": "1", "no_email": "0"},  # duplicate address
    {"id": "13", "email": "bob@example.org", "statut": "1", "no_email": "1"},  # opted out in Dolibarr
    {"id": "14", "email": "old@example.org", "statut": "0", "no_email": "0"},  # inactive contact
    {"id": "15", "email": "not-an-email", "statut": "1"},
    {"id": "16", "email": "cy@example.org", "statut": "1", "no_email": "0"},
]


class FakeDolibarr:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request):
        assert request.headers["DOLAPIKEY"] == "DOLKEY"
        path = request.url.path.split("/api/index.php/", 1)[1]
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((request.method, path, q, request.content.decode()))
        if path == "categories":
            assert "Email updates" in q["sqlfilters"]
            return httpx.Response(200, json=[{"id": "7", "label": "Email updates"}])
        if path == "contacts" and "category" in q:
            return httpx.Response(200, json=CONTACTS if q.get("page", "0") == "0" else [])
        if path == "contacts" and "sqlfilters" in q:
            return httpx.Response(200, json=[c for c in CONTACTS if c["email"].lower() in q["sqlfilters"].lower()])
        if request.method in ("PUT", "DELETE", "POST"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def client(self):
        return Dolibarr(http=httpx.Client(transport=httpx.MockTransport(self)))


@pytest.fixture
def app3(settings):
    from studio.app import create_app

    mailer.outbox.clear()
    app = create_app(dataclasses.replace(settings, dolibarr_url="https://crm.example.org", dolibarr_api_key="DOLKEY"))
    make_user("adam", "admin")
    return app


def at_local(monkeypatch, when: datetime):
    utc = when.replace(tzinfo=db_mod.settings().timezone).astimezone(UTC).replace(tzinfo=None)
    monkeypatch.setattr(timeutil, "utcnow", lambda: utc)
    monkeypatch.setattr(ann_mod, "utcnow", lambda: utc)
    return utc


def adam(s):
    return Actor("user", s.scalar(select(User).where(User.username == "adam")))


def approved_email(send_at, subject="Coming up at CGW: November", kind="special"):
    with db_mod.session_scope() as s:
        a = Announcement(kind=kind, subject=subject, intro="Hello.", send_at=send_at, status="draft",
                         items=[{"title": "Soldering", "when": "Thu", "where": "Shop", "link": "https://x.org", "blurb": "Learn."}])
        s.add(a)
        s.flush()
        actor = adam(s)
        ann_mod.submit(s, actor, a)
        ann_mod.record_test(s, a, "adam@example.org")  # approving needs a test of this version
        ann_mod.approve(s, actor, a, ann_mod.content_hash(a))
        return a.id


def test_first_tuesday():
    assert ann_mod.first_tuesday(2026, 11).isoformat() == "2026-11-03"
    assert ann_mod.first_tuesday(2026, 12).isoformat() == "2026-12-01"


def test_monthly_draft_created_12_days_ahead_with_events(app3, monkeypatch):
    with db_mod.session_scope() as s:
        for title, day, recurring, email_ok in (("Soldering class", 10, False, True), ("Hack night", 12, True, True),
                                                ("Private thing", 14, False, False), ("Too early", 2, False, True)):
            start = datetime(2026, 11, day, 0, 0)
            s.add(Event(uid=title, start=start, title=title, recurring=recurring, email_ok=email_ok))
    at_local(monkeypatch, datetime(2026, 10, 20, 9, 0))  # 14 days before Nov 3: too early
    with db_mod.session_scope() as s:
        assert ann_mod.ensure_monthly(s) is None
    at_local(monkeypatch, datetime(2026, 10, 23, 9, 0))  # 11 days before
    with db_mod.session_scope() as s:
        a = ann_mod.ensure_monthly(s)
        assert a.month == "2026-11" and a.status == "needs_claude"
        assert [i["title"] for i in a.items] == ["Soldering class"]
        assert timeutil.to_local(a.send_at).strftime("%Y-%m-%d %H:%M") == "2026-11-03 10:00"
        assert ann_mod.ensure_monthly(s) is None  # only one per month


def test_approval_rules_and_edit_clears(app3):
    send_at = db_mod.utcnow() + timedelta(days=3)
    ann_id = approved_email(send_at)
    with db_mod.session_scope() as s:
        a = s.get(Announcement, ann_id)
        assert ann_mod.is_sendable(a) == (True, "")
        ann_mod.update(s, adam(s), a, intro="Changed.")
        assert a.status == "in_review" and ann_mod.is_sendable(a)[0] is False
        with pytest.raises(ann_mod.AnnouncementError, match="changed after you opened it"):
            ann_mod.approve(s, adam(s), a, "stale")
        with pytest.raises(ann_mod.AnnouncementError, match="Approved emails"):
            a.status = "approved"
            ann_mod.update(s, Actor("mcp", adam(s).user), a, intro="sneaky")


def test_third_email_in_a_month_needs_override(app3):
    base = db_mod.utcnow() + timedelta(days=2)
    approved_email(base, "One")
    approved_email(base + timedelta(hours=1), "Two")
    with db_mod.session_scope() as s:
        a = Announcement(kind="special", subject="Three", intro="Hi", send_at=base + timedelta(hours=2), status="draft")
        s.add(a)
        s.flush()
        assert any("email number 3" in p for p in ann_mod.problems(s, a))
        with pytest.raises(ann_mod.AnnouncementError, match="admin"):
            ann_mod.set_override(s, Actor("user", User(username="x", role="approver", password_hash="")), a, True)
        ann_mod.set_override(s, adam(s), a, True)
        assert not ann_mod.problems(s, a)


def test_send_filters_list_and_logs_to_dolibarr(app3):
    ann_id = approved_email(db_mod.utcnow() - timedelta(minutes=1))
    fake = FakeDolibarr()
    with db_mod.session_scope() as s:
        s.add(Unsubscribe(email="cy@example.org"))  # not yet synced: must be skipped
    with db_mod.session_scope() as s:
        sent = ann_mod.send(s, s.get(Announcement, ann_id), dolibarr=fake.client(), pause=0)
    assert sent == 1
    msg = mailer.outbox[-1]
    assert msg["To"] == "ann@example.org"
    assert msg["List-Unsubscribe"].startswith("<http://studio.test/u/")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    html = msg.get_body(("html",)).get_content()
    assert "Soldering" in html and "1404 Grand Ave" in html and "Unsubscribe" in html
    logged = [c for c in fake.calls if c[1] == "agendaevents"]
    assert len(logged) == 1 and json.loads(logged[0][3])["contact_id"] == 11
    with db_mod.session_scope() as s:
        a = s.get(Announcement, ann_id)
        assert a.status == "sent" and a.recipient_count == 1


def test_send_resumes_without_duplicates(app3):
    ann_id = approved_email(db_mod.utcnow() - timedelta(minutes=1))
    with db_mod.session_scope() as s:
        s.add(AnnouncementRecipient(announcement_id=ann_id, email="ann@example.org", contact_id=11))
    with db_mod.session_scope() as s:
        sent = ann_mod.send(s, s.get(Announcement, ann_id), dolibarr=FakeDolibarr().client(), pause=0)
    assert sent == 1 and [m["To"] for m in mailer.outbox] == ["cy@example.org"]


def test_unsent_if_tampered(app3):
    ann_id = approved_email(db_mod.utcnow() - timedelta(minutes=1))
    with db_mod.session_scope() as s:
        s.get(Announcement, ann_id).intro = "Edited behind the approver's back"
    with db_mod.session_scope() as s:
        with pytest.raises(ann_mod.AnnouncementError, match="changed after"):
            ann_mod.send(s, s.get(Announcement, ann_id), dolibarr=FakeDolibarr().client(), pause=0)
    assert mailer.outbox == []


def test_one_click_unsubscribe_syncs_to_dolibarr(app3, monkeypatch):
    fake = FakeDolibarr()
    monkeypatch.setattr(ann_mod, "Dolibarr", fake.client)
    token = ann_mod.unsubscribe_token("Ann@Example.org")
    with TestClient(app3) as c:
        page = c.get(f"/u/{token}")
        assert page.status_code == 200 and "ann@example.org" in page.text
        assert c.get("/u/bad.token").status_code == 404
        r = c.post(f"/u/{token}", content="List-Unsubscribe=One-Click",
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200 and "unsubscribed" in r.text
    changed = [(m, p) for m, p, _q, _b in fake.calls if m in ("PUT", "DELETE")]
    assert ("PUT", "contacts/11") in changed and ("DELETE", "categories/7/objects/contact/11") in changed
    with db_mod.session_scope() as s:
        row = s.scalar(select(Unsubscribe))
        assert row.email == "ann@example.org" and row.synced_at is not None


def test_email_pages_and_test_send(app3, monkeypatch):
    monkeypatch.setattr("studio.web.Dolibarr", FakeDolibarr().client)
    with db_mod.session_scope() as s:
        a = Announcement(kind="special", subject="Big news", intro="Hello **there**.", status="draft",
                         send_at=db_mod.utcnow() + timedelta(days=3))
        s.add(a)
        s.flush()
        ann_id = a.id
    with TestClient(app3, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        page = c.get(f"/announcements/{ann_id}")
        assert page.status_code == 200 and "Big news" in page.text and "&lt;strong&gt;there" in page.text
        c.post(f"/announcements/{ann_id}/test", data={"csrf": csrf_of(c, f"/announcements/{ann_id}")})
    assert mailer.outbox[-1]["Subject"].startswith("[TEST ") and mailer.outbox[-1]["Subject"].endswith("] Big news")
    assert mailer.outbox[-1]["To"] == "adam@example.org"


def test_claude_drafts_blurbs_but_cannot_change_facts(app3):
    from studio import mcp_server

    with db_mod.session_scope() as s:
        a = Announcement(kind="monthly", month="2026-11", subject="Coming up at CGW: November", status="needs_claude",
                         send_at=db_mod.utcnow() + timedelta(days=5),
                         items=[{"event_id": 1, "title": "Soldering", "when": "Thu, 6 PM", "where": "Shop", "link": "", "blurb": ""}])
        s.add(a)
        s.flush()
        ann_id = a.id
        token = mcp_server._current_user_id.set(adam(s).user.id)
    try:
        out = json.loads(mcp_server.draft_announcement(
            ann_id, intro="Here's what's on in November.",
            blurbs=[mcp_server.BlurbDraft(index=0, blurb="Learn to solder a blinky badge.")]))
    finally:
        mcp_server._current_user_id.reset(token)
    assert out["status"] == "in_review"
    with db_mod.session_scope() as s:
        a = s.get(Announcement, ann_id)
        assert a.items[0]["blurb"] == "Learn to solder a blinky badge." and a.items[0]["title"] == "Soldering"
        assert a.approved_hash is None


def test_newsletter_and_event_announcements_use_different_addresses(app3):
    monthly = Announcement(kind="monthly", subject="Coming up at CGW: November", items=[], status="draft")
    special = Announcement(kind="special", subject="Big news", items=[], status="draft")
    for a, address in ((monthly, "info@columbiagadgetworks.org"), (special, "events@columbiagadgetworks.org")):
        msg = ann_mod.build_message(a, "sam@example.org")
        assert msg["From"] == f"Columbia Gadget Works <{address}>" and msg["Reply-To"] == address



def test_approval_needs_a_test_of_this_exact_version(app3):
    with db_mod.session_scope() as s:
        a = Announcement(kind="special", subject="Big news", intro="Hello.", send_at=db_mod.utcnow() + timedelta(days=3),
                         status="draft", items=[])
        s.add(a)
        s.flush()
        ann_mod.submit(s, adam(s), a)
        with pytest.raises(ann_mod.AnnouncementError, match="test of this exact version"):
            ann_mod.approve(s, adam(s), a, ann_mod.content_hash(a))
        mailer.outbox.clear()
        ann_mod.send_test(a, "adam@example.org", s)
        assert ann_mod.tested_version(s, a)["to"] == "adam@example.org"
        ann_mod.update(s, adam(s), a, intro="Changed after the test.")
        assert ann_mod.tested_version(s, a) is None  # an edit means testing again
        with pytest.raises(ann_mod.AnnouncementError, match="test of this exact version"):
            ann_mod.approve(s, adam(s), a, ann_mod.content_hash(a))
        ann_mod.send_test(a, "adam@example.org", s)
        ann_mod.approve(s, adam(s), a, ann_mod.content_hash(a))
        assert a.status == "approved"


def test_sender_addresses_can_be_set_in_settings(app3):
    from starlette.testclient import TestClient

    from .conftest import csrf_of, login

    def save(c, **fields):
        data = {"csrf": csrf_of(c, "/settings/emails")} | fields
        return c.post("/settings/emails", data=data, follow_redirects=True).text

    with TestClient(app3, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        assert "info@columbiagadgetworks.org" in c.get("/settings/emails").text  # the default shows as a hint
        assert "plain email address" in save(c, **{"sender.newsletter": "Name <x@y.org>"})
        assert "saved" in save(c, **{"sender.newsletter": "news@example.org", "sender.events": "go@example.org",
                                     "sender.name": "CGW Makers"})
    with db_mod.session_scope() as s:
        monthly = Announcement(kind="monthly", subject="S", items=[], status="draft")
        special = Announcement(kind="special", subject="S", items=[], status="draft")
        s.add_all([monthly, special])
        s.flush()
        assert ann_mod.build_message(monthly, "a@example.org")["From"] == "CGW Makers <news@example.org>"
        assert ann_mod.build_message(special, "a@example.org")["Reply-To"] == "go@example.org"



def test_newsletter_can_be_started_early(app3, monkeypatch):
    with db_mod.session_scope() as s:
        s.add(Event(uid="class", start=datetime(2026, 11, 10, 18, 0), title="Soldering class", email_ok=True))
    at_local(monkeypatch, datetime(2026, 10, 12, 9, 0))  # well before the usual 12-day window
    with db_mod.session_scope() as s:
        assert ann_mod.ensure_monthly(s) is None
        a = ann_mod.ensure_monthly(s, early=True)
        assert a.month == "2026-11" and a.status == "needs_claude" and [i["title"] for i in a.items] == ["Soldering class"]
        assert ann_mod.ensure_monthly(s, early=True) is None  # already started



def test_social_links_set_in_settings_appear_in_the_email(app3):
    from starlette.testclient import TestClient

    from .conftest import csrf_of, login

    with TestClient(app3, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        data = {"csrf": csrf_of(c, "/settings/emails"), "social.facebook": "not a link"}
        assert "must start with https://" in c.post("/settings/emails", data=data, follow_redirects=True).text
        data = {"csrf": csrf_of(c, "/settings/emails"), "social.facebook": "https://www.facebook.com/cgw",
                "social.instagram": "https://www.instagram.com/cgw"}
        assert "saved" in c.post("/settings/emails", data=data, follow_redirects=True).text
    with db_mod.session_scope() as s:
        a = Announcement(kind="monthly", subject="S", intro="Hi", closing="Bye", items=[], status="draft")
        s.add(a)
        s.flush()
        html_body, text_body = ann_mod.render(a, "#u")
        assert "Follow us:" in html_body and 'href="https://www.facebook.com/cgw"' in html_body
        assert "Instagram: https://www.instagram.com/cgw" in text_body and "TikTok" not in html_body



def test_weekly_events_are_one_line_and_cancelled_ones_have_no_link(app3, monkeypatch):
    with db_mod.session_scope() as s:
        for day in (6, 13, 20):  # 6 PM Central is midnight UTC the next day
            s.add(Event(uid=f"hn{day}", start=datetime(2026, 11, day, 0, 0), title="Open Hack Night", series="hack|Thu|18:00",
                        location="1404 Grand Ave"))
        s.add(Event(uid="class", start=datetime(2026, 11, 4, 0, 0), title="Free Public CGW Class: CAD"))
        s.add(Event(uid="thanks", start=datetime(2026, 11, 27, 0, 0), title="Cancelled: Open Hack Night (Thanksgiving)"))
    at_local(monkeypatch, datetime(2026, 10, 12, 9, 0))
    with db_mod.session_scope() as s:
        a = ann_mod.ensure_monthly(s, early=True)
        titles = [i["title"] for i in a.items]
        assert titles == ["Free Public CGW Class: CAD", "Open Hack Night", "Cancelled: Open Hack Night (Thanksgiving)"]
        hack = a.items[1]
        assert hack["when"].startswith("Thursdays, ") and hack["when"].endswith("November 5, 12 and 19")
        assert hack["link"] and a.items[0]["link"]
        assert a.items[2]["link"] == ""  # nothing to link for a cancelled event
        html_body, text_body = ann_mod.render(a, "#u")
        assert "Details: https://" in text_body and text_body.count("Details:") == 2
        # an older draft built one line per Hack Night is rebuilt, keeping what was written
        a.items = [{"event_id": 99, "title": "Open Hack Night", "when": "x", "where": "", "link": "l", "blurb": "Free for all."},
                   {"event_id": 98, "title": "Open Hack Night", "when": "y", "where": "", "link": "l", "blurb": "Free for all."}]
        a.status = "draft"
        items = ann_mod.refresh_items(s, adam(s), a)
        assert [i["title"] for i in items][1] == "Open Hack Night" and items[1]["blurb"] == "Free for all."
        assert len(items) == 3



def test_list_check_explains_where_contacts_drop_out(app3, monkeypatch):
    import httpx

    from studio import dolibarr as dol

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json=[{"id": "1", "label": "Email updates"}])
        return httpx.Response(200, json=[
            {"id": "1", "email": "a@example.org", "statut": "1", "no_email": None},
            {"id": "2", "email": "b@example.org", "statut": "1", "no_email": "1"},
            {"id": "3", "email": "not-an-address", "statut": "1"},
            {"id": "4", "email": "d@example.org", "statut": "0"}])

    object.__setattr__(db_mod.settings(), "dolibarr_url", "https://dol.example")
    object.__setattr__(db_mod.settings(), "dolibarr_api_key", "k")
    client = dol.Dolibarr(http=httpx.Client(transport=httpx.MockTransport(handler)))
    d = client.diagnose()
    assert d == {"tag": "Email updates", "tag_id": 1, "tagged": 4, "valid_email": 3, "active": 2, "subscribed": 1, "usable": 1}


def test_a_library_photo_can_go_in_an_email_and_is_public_only_while_it_is_in_one(app3):
    import io

    from studio import public_media
    from studio.media import process, store_upload
    from studio.models import User

    from .conftest import jpeg_bytes

    with db_mod.session_scope() as s:
        photo = store_upload(s, io.BytesIO(jpeg_bytes()), "p.jpg", "image/jpeg", s.scalar(select(User.id)), "Jeff at the mill")
        process(photo)
        pid = photo.id
        a = Announcement(kind="special", subject="S", intro=f"Hi\n\n![Jeff working on the mill](photo:{pid})", closing="Bye",
                         items=[], status="draft", send_at=db_mod.utcnow() + timedelta(days=3))
        s.add(a)
        s.flush()
        html_body, text_body = ann_mod.render(a, "#u")
        sig = public_media.signature(pid, "jpg")
        assert f"/m/{pid}/jpg/{sig}.jpg" in html_body and 'alt="Jeff working on the mill"' in html_body
        assert "[Photo: Jeff working on the mill]" in text_body and "photo:" not in text_body
        assert not public_media.allowed(s, pid, "jpg", sig)  # a draft email doesn't make it public
        a.status = "in_review"
        s.flush()
        assert public_media.allowed(s, pid, "jpg", sig)
        a.closing = "![x](photo:99999)"
        assert any("isn't a photo in the library" in p for p in ann_mod.problems(s, a))
