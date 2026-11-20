"""Calendar events as native events on other platforms: nothing goes out without a human approval of the facts."""

import dataclasses
from datetime import timedelta

import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio import event_listings as el
from studio.models import Event, EventListing, User
from studio.security import Actor

from .conftest import make_user


@pytest.fixture
def app_listings(settings):
    from studio.app import create_app

    app = create_app(dataclasses.replace(settings, event_listing_platforms=["bluesky"]))
    make_user("adam", "admin")
    make_user("eddie", "editor")
    return app


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def create(self, event):
        self.calls.append(("create", event.title))
        return "rkey1", "https://smokesignal.events/did/rkey1"

    def update(self, listing):
        self.calls.append(("update", listing.event.title))

    def cancel(self, listing):
        self.calls.append(("cancel", listing.event.title))


def _event(s, title="Soldering 101"):
    start = db_mod.utcnow() + timedelta(days=10)
    e = Event(uid=title, start=start, end=start + timedelta(hours=2), title=title, location="CGW")
    s.add(e)
    s.flush()
    return e


def _actor(s, name):
    return Actor("user", s.scalar(select(User).where(User.username == name)))


def test_nothing_is_listed_before_approval_and_only_approvers_can_approve(app_listings):
    fake = FakeAdapter()
    with db_mod.session_scope() as s:
        e = _event(s)
        el.ensure_rows(s, e)
        assert el.sync_due(s, {"bluesky": lambda: fake})["created"] == 0 and fake.calls == []
        with pytest.raises(el.ListingError):
            el.approve(s, _actor(s, "eddie"), e, el.listing_hash(e))
        with pytest.raises(el.ListingError):
            el.approve(s, Actor("mcp", _actor(s, "adam").user), e, el.listing_hash(e))  # Claude acting for adam
        with pytest.raises(el.ListingError):
            el.approve(s, _actor(s, "adam"), e, "stale-hash")
        el.approve(s, _actor(s, "adam"), e, el.listing_hash(e))
        assert el.sync_due(s, {"bluesky": lambda: fake})["created"] == 1
        row = s.scalar(select(EventListing).where(EventListing.platform == "bluesky"))
        assert row.state == "listed" and row.external_id == "rkey1"


def test_a_calendar_change_waits_for_a_new_approval_but_a_cancellation_goes_out(app_listings):
    fake = FakeAdapter()
    with db_mod.session_scope() as s:
        e = _event(s)
        el.approve(s, _actor(s, "adam"), e, el.listing_hash(e))
        el.sync_due(s, {"bluesky": lambda: fake})
        e.location = "Somewhere else"
        assert el.is_listable(e)[0] is False
        assert el.sync_due(s, {"bluesky": lambda: fake})["updated"] == 0
        el.approve(s, _actor(s, "adam"), e, el.listing_hash(e))
        assert el.sync_due(s, {"bluesky": lambda: fake})["updated"] == 1
        e.status = "cancelled"
        assert el.sync_due(s, {"bluesky": lambda: fake})["cancelled"] == 1
        assert [c[0] for c in fake.calls] == ["create", "update", "cancel"]


def test_manual_platforms_need_approval_and_a_link(app_listings):
    with db_mod.session_scope() as s:
        e = _event(s)
        el.ensure_rows(s, e)
        fb = next(r for r in e.listings if r.platform == "facebook")
        with pytest.raises(el.ListingError):
            el.mark_listed_by_hand(s, _actor(s, "adam"), fb, "https://facebook.com/events/1")
        el.approve(s, _actor(s, "adam"), e, el.listing_hash(e))
        with pytest.raises(el.ListingError):
            el.mark_listed_by_hand(s, _actor(s, "adam"), fb, "not a link")
        el.mark_listed_by_hand(s, _actor(s, "adam"), fb, "https://facebook.com/events/1")
        assert fb.state == "listed"
        kit = el.manual_kit(e)
        assert kit["name"] == "Soldering 101" and kit["location"] == "CGW"


def test_bluesky_event_record_shape(app_listings):
    from studio.publishers.event_bluesky import record

    with db_mod.session_scope() as s:
        rec = record(_event(s))
        assert rec["$type"] == "community.lexicon.calendar.event"
        assert rec["status"].endswith("#scheduled") and rec["startsAt"].endswith("Z")
