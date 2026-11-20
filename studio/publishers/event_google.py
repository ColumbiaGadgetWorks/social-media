"""Calendar events as Google Business Profile event posts (Search and Maps).

API: My Business v4 localPosts with topicType EVENT. Google gates the Business Profile APIs: request access for the
Google Cloud project first (Business Profile API access form), then enable the "My Business" APIs, create an OAuth
client and get a refresh token for an owner/manager of the profile (scope business.manage).

Settings: STUDIO_GBP_ACCOUNT_ID, STUDIO_GBP_LOCATION_ID, STUDIO_GBP_CLIENT_ID, STUDIO_GBP_CLIENT_SECRET,
STUDIO_GBP_REFRESH_TOKEN; turn it on with STUDIO_EVENT_LISTINGS=google.

SCAFFOLD: no photo yet (Google wants a public image URL; the event card's public copy would do), and untested
against the live API.
"""

from __future__ import annotations

import httpx

from ..calendar_sync import to_local
from ..db import settings
from ..models import Event, EventListing
from .bluesky import PublishError

API = "https://mybusiness.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _date(dt) -> dict:
    return {"year": dt.year, "month": dt.month, "day": dt.day}


def _time(dt) -> dict:
    return {"hours": dt.hour, "minutes": dt.minute}


def body(event: Event) -> dict:
    start = to_local(event.start)
    end = to_local(event.end) if event.end else start
    return {
        "languageCode": "en-US",
        "topicType": "EVENT",
        "summary": (event.description or event.title)[:1500],
        "event": {"title": event.title[:58],  # Google's limit for event titles
                  "schedule": {"startDate": _date(start), "startTime": _time(start),
                               "endDate": _date(end), "endTime": _time(end)}},
        "callToAction": {"actionType": "LEARN_MORE", "url": event.url or f"{settings().website_url}/calendar/"},
    }


class GoogleEvents:
    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        if not (s.gbp_account_id and s.gbp_location_id and s.gbp_client_id and s.gbp_client_secret
                and s.gbp_refresh_token):
            raise PublishError("Google Business Profile isn't configured (STUDIO_GBP_ACCOUNT_ID, _LOCATION_ID, "
                               "_CLIENT_ID, _CLIENT_SECRET, _REFRESH_TOKEN)")
        self.http = http or httpx.Client(timeout=60)
        self.base = f"{API}/accounts/{s.gbp_account_id}/locations/{s.gbp_location_id}/localPosts"
        r = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": s.gbp_refresh_token,
                                            "client_id": s.gbp_client_id, "client_secret": s.gbp_client_secret})
        self._check(r, "sign in")
        self.headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    @staticmethod
    def _check(r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            raise PublishError(f"Google Business Profile {what} failed ({r.status_code}): {r.text[:300]}")

    def create(self, event: Event) -> tuple[str, str]:
        r = self.http.post(self.base, json=body(event), headers=self.headers)
        self._check(r, "event post")
        out = r.json()
        return out["name"], out.get("searchUrl", "")

    def update(self, listing: EventListing) -> None:
        r = self.http.patch(f"{API}/{listing.external_id}", json=body(listing.event), headers=self.headers,
                            params={"updateMask": "summary,event,callToAction"})
        self._check(r, "event update")

    def cancel(self, listing: EventListing) -> None:
        r = self.http.delete(f"{API}/{listing.external_id}", headers=self.headers)
        if r.status_code != 404:  # already gone is fine
            self._check(r, "event removal")
