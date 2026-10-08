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
    assert mailer.outbox[-1]["Subject"] == "[TEST] Big news"
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
