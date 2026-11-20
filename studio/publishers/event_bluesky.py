"""Calendar events as AT Protocol event records in our Bluesky repo.

Lexicon: community.lexicon.calendar.event (the shared calendar lexicon Smoke Signal and other AT Protocol calendar
apps read). The record lives in the Studio's own Bluesky account, so the existing app password is enough. It shows
up at https://smokesignal.events/<handle>/<rkey> and in apps that index the lexicon; RSVPs are other people's
community.lexicon.calendar.rsvp records pointing at it.

SCAFFOLD: check the field names against the current lexicon before turning it on (STUDIO_EVENT_LISTINGS=bluesky).
"""

from __future__ import annotations

from ..calendar_sync import is_cancelled
from ..db import settings
from ..models import Event, EventListing
from .bluesky import BlueskyClient

COLLECTION = "community.lexicon.calendar.event"
NS = COLLECTION + "#"


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds") + "Z"  # naive UTC in the database


def record(event: Event) -> dict:
    from ..db import utcnow

    rec: dict = {
        "$type": COLLECTION,
        "name": event.title[:300],
        "description": (event.description or "")[:3000],
        "createdAt": _iso(utcnow()),
        "startsAt": _iso(event.start),
        "mode": NS + "inperson",
        "status": NS + ("cancelled" if is_cancelled(event) else "scheduled"),
        "uris": [{"uri": event.url or f"{settings().website_url}/calendar/", "name": "Details"}],
    }
    if event.end:
        rec["endsAt"] = _iso(event.end)
    if event.location:
        rec["locations"] = [{"$type": "community.lexicon.location.address", "name": event.location[:300],
                             "country": "US"}]
    return rec


class BlueskyEvents:
    def __init__(self, client: BlueskyClient | None = None):
        self.bsky = client or BlueskyClient()

    def _put(self, rkey: str | None, rec: dict) -> dict:
        body = {"repo": self.bsky.did, "collection": COLLECTION, "record": rec}
        method = "com.atproto.repo.createRecord"
        if rkey:
            body["rkey"], method = rkey, "com.atproto.repo.putRecord"
        r = self.bsky.http.post(f"{self.bsky.service}/xrpc/{method}", json=body, headers=self.bsky.headers)
        self.bsky._check(r, "event record")
        return r.json()

    def create(self, event: Event) -> tuple[str, str]:
        out = self._put(None, record(event))
        rkey = out["uri"].rsplit("/", 1)[-1]
        return rkey, f"https://smokesignal.events/{self.bsky.did}/{rkey}"

    def update(self, listing: EventListing) -> None:
        self._put(listing.external_id, record(listing.event))

    def cancel(self, listing: EventListing) -> None:
        rec = record(listing.event)
        rec["status"] = NS + "cancelled"  # keep the record so people who RSVPed see it was called off
        self._put(listing.external_id, rec)
