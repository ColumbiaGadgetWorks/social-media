"""Calendar events as native events on other platforms, not only as promo posts.

A promo post says "come Thursday"; a native event is the thing people RSVP to, get reminded by, and find in the
platform's event search. Which platforms can take one, and how:

  platform   how                                                            mode
  ---------  -------------------------------------------------------------  ------
  bluesky    an event record (community.lexicon.calendar.event, the lexicon  auto
             Smoke Signal and other AT Protocol calendars read) in our own
             Bluesky repo, with the app password we already have
  google     a Google Business Profile event post (localPosts, topicType     auto
             EVENT): shows on Search and Maps. Needs Business Profile API
             access from Google and an OAuth refresh token
  facebook   a Page event. Meta closed event creation to apps in 2018, so    manual
             the Studio gives a copy-ready kit and a person makes it, then
             pastes the link back
  linkedin   a Page event. The Events API is for approved partners only,     manual
             so the same copy kit as Facebook
  discord    already synced to Discord Scheduled Events outside the Studio;  (not here)
             can move here later (POST /guilds/{id}/scheduled-events, the
             bot needs Manage Events) so everything lives in one place
  instagram, threads, x: no event object; the promo posts cover them (Instagram's in-app "Add reminder" on a post is
  app-only).

The approval rule holds: nothing is listed until a human approver approves the event's listing (approve() with the
hash they saw), and the facts listed are fingerprinted (listing_hash). When the calendar changes an event, the hash no
longer matches, so updates wait for a fresh approval. Two things go out without a new approval because they only take
things down: a cancelled event's listings are marked cancelled / removed, and so are listings of events that vanish
from the calendar.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import settings, utcnow
from .models import Event, EventListing
from .security import Actor, audit

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class ListingError(Exception):
    pass


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    mode: str  # auto | manual
    how: str  # one line for the Events page


PLATFORMS: dict[str, Platform] = {p.key: p for p in (
    Platform("bluesky", "Bluesky (Smoke Signal)", "auto", "Created from the Studio as an AT Protocol calendar event."),
    Platform("google", "Google Business Profile", "auto", "An event post on Search and Maps, from the Studio."),
    Platform("facebook", "Facebook Page event", "manual",
             "Facebook doesn't let apps create events. Copy the kit into Create event on the Page, then paste the link."),
    Platform("linkedin", "LinkedIn Page event", "manual",
             "LinkedIn's Events API is partner-only. Copy the kit into Create event on the Page, then paste the link."),
)}


class Adapter(Protocol):
    def create(self, event: Event) -> tuple[str, str]: ...  # (external id, public url)
    def update(self, listing: EventListing) -> None: ...
    def cancel(self, listing: EventListing) -> None: ...


def enabled_platforms() -> list[str]:
    """Manual platforms always show (a kit costs nothing); auto ones only when turned on (STUDIO_EVENT_LISTINGS)."""
    auto = [k for k in settings().event_listing_platforms if k in PLATFORMS and PLATFORMS[k].mode == "auto"]
    return auto + [k for k, p in PLATFORMS.items() if p.mode == "manual"]


def wants_listing(event: Event) -> bool:
    """Which events get listings: the ones the Studio promotes, one-offs only (a weekly series would flood the
    platforms; list the series once by hand)."""
    return event.promote and not event.series and not event.all_day


def listing_facts(event: Event) -> dict:
    """Exactly what gets listed. Everything in here is covered by the approval."""
    from .calendar_sync import is_cancelled

    return {
        "event": event.id,
        "title": event.title.strip(),
        "description": event.description.strip(),
        "start": event.start.isoformat(),
        "end": event.end.isoformat() if event.end else None,
        "location": event.location.strip(),
        "url": event.url.strip(),
        "cancelled": is_cancelled(event),
    }


def listing_hash(event: Event) -> str:
    return hashlib.sha256(json.dumps(listing_facts(event), sort_keys=True).encode()).hexdigest()


def approve(db: Session, actor: Actor, event: Event, seen_hash: str | None) -> None:
    """The only way an event becomes listable. Humans with the Approver role only, for the facts they saw."""
    if not actor.is_human or not actor.user.has_role("approver"):
        raise ListingError("Only a signed-in approver can approve event listings.")
    current = listing_hash(event)
    if seen_hash != current:
        raise ListingError("This event changed in the calendar after you opened the page. Check it and approve again.")
    event.listing_approved_hash = current
    event.listing_approved_by_id = actor.user.id
    event.listing_approved_at = utcnow()
    ensure_rows(db, event)
    audit(db, actor, "event_listing_approved", "event", event.id, hash=current, platforms=enabled_platforms())


def is_listable(event: Event) -> tuple[bool, str]:
    """The check every listing adapter runs right before anything leaves the Studio."""
    if not event.listing_approved_hash or not event.listing_approved_by_id:
        return False, "the listing hasn't been approved"
    if listing_hash(event) != event.listing_approved_hash:
        return False, "the event changed after its listing was approved"
    return True, ""


def ensure_rows(db: Session, event: Event) -> list[EventListing]:
    have = {row.platform for row in event.listings}
    for key in enabled_platforms():
        if key not in have:
            event.listings.append(EventListing(platform=key))
    db.flush()
    return event.listings


def mark_listed_by_hand(db: Session, actor: Actor, listing: EventListing, url: str) -> None:
    """A person made the event on a manual platform (Facebook, LinkedIn) and pasted its link."""
    if not actor.is_human:
        raise ListingError("Only a signed-in person can record a listing made by hand.")
    ok, why = is_listable(listing.event)
    if not ok:
        raise ListingError(f"Approve the listing first ({why}).")
    url = url.strip()
    if not url.startswith("https://"):
        raise ListingError("Paste the event's https:// link.")
    listing.state, listing.external_url = "listed", url[:512]
    listing.listed_hash, listing.updated_at = listing.event.listing_approved_hash, utcnow()
    audit(db, actor, "event_listed", "event", listing.event_id, platform=listing.platform, url=url, by_hand=True)


def manual_kit(event: Event) -> dict:
    """Copy-ready fields for a platform without an API: the same facts the approver approved."""
    from .calendar_sync import event_when, to_local

    day, when = event_when(event)
    start = to_local(event.start)
    end = to_local(event.end) if event.end else None
    return {
        "name": event.title,
        "date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%I:%M %p").lstrip("0"),
        "end_time": end.strftime("%I:%M %p").lstrip("0") if end else "",
        "location": event.location,
        "details": event.description,
        "link": event.url or f"{settings().website_url}/calendar/",
        "summary": f"{day}, {when}",
    }


def _adapters() -> dict[str, Callable[[], Adapter]]:
    def bluesky():
        from .publishers.event_bluesky import BlueskyEvents

        return BlueskyEvents()

    def google():
        from .publishers.event_google import GoogleEvents

        return GoogleEvents()

    return {"bluesky": bluesky, "google": google}


def sync_due(db: Session, factories: dict[str, Callable[[], Adapter]] | None = None) -> dict[str, int]:
    """One pass for the automatic platforms: create approved listings, update ones whose approved facts moved on,
    take down cancelled ones. Returns counts for the log."""
    from .calendar_sync import is_cancelled

    factories = factories or _adapters()
    auto = [k for k in enabled_platforms() if PLATFORMS[k].mode == "auto"]
    counts = {"created": 0, "updated": 0, "cancelled": 0, "failed": 0}
    if not auto:
        return counts
    clients: dict[str, Adapter] = {}
    system = Actor.system()
    rows = db.scalars(select(EventListing).where(EventListing.platform.in_(auto),
                                                 EventListing.state.in_(("pending", "listed")))).all()
    for row in rows:
        event = row.event
        try:
            cancelled = event.status == "cancelled" or is_cancelled(event)
            if cancelled and row.state == "listed":  # taking down needs no new approval
                action = "cancelled"
            elif cancelled:
                row.state = "cancelled"  # never listed: nothing to take down
                continue
            else:
                ok, _ = is_listable(event)
                if not ok:
                    continue  # waiting for (re-)approval
                if row.state == "pending":
                    action = "created"
                elif row.listed_hash != event.listing_approved_hash:
                    action = "updated"
                else:
                    continue
            if row.platform not in clients:
                clients[row.platform] = factories[row.platform]()
            client = clients[row.platform]
            if action == "created":
                row.external_id, row.external_url = client.create(event)
                row.state = "listed"
            elif action == "updated":
                client.update(row)
            else:
                client.cancel(row)
                row.state = "cancelled"
            row.listed_hash = event.listing_approved_hash or ""
            row.attempts, row.last_error, row.updated_at = 0, "", utcnow()
            counts[action] += 1
            audit(db, system, f"event_listing_{action}", "event", event.id, platform=row.platform,
                  url=row.external_url)
        except Exception as exc:  # noqa: BLE001 - one platform failing never stops the others
            log.warning("event listing %s on %s failed: %s", event.id, row.platform, exc)
            clients.pop(row.platform, None)
            row.attempts += 1
            row.last_error = str(exc)[:500]
            if row.attempts >= MAX_ATTEMPTS:
                row.state = "failed"
            counts["failed"] += 1
            audit(db, system, "event_listing_failed", "event", event.id, platform=row.platform, error=row.last_error)
        db.commit()
    return counts
