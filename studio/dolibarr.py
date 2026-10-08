"""Dolibarr REST API: the "Email updates" list, unsubscribes, and the email record.

The list is the same one the website signup and the onboarding module maintain: Dolibarr
contacts carrying the "Email updates" tag. Unsubscribing removes the tag and sets the
contact's email opt-out, so Dolibarr's own EMailing respects it too. Signing up again on
the website puts the tag back, as it does today.

Needs a Dolibarr user API key with read/write on contacts, categories and agenda.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from .db import settings

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class DolibarrError(Exception):
    pass


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or ""))


class Dolibarr:
    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        if not s.dolibarr_configured:
            raise DolibarrError("Dolibarr isn't configured (STUDIO_DOLIBARR_URL / STUDIO_DOLIBARR_API_KEY)")
        self.base = f"{s.dolibarr_url}/api/index.php"
        self.tag = s.dolibarr_tag
        self.user_id = s.dolibarr_user_id
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"DOLAPIKEY": s.dolibarr_api_key, "Accept": "application/json"}
        self._category_id: int | None = None

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, f"{self.base}/{path}", headers=self.headers, **kw)
        if r.status_code == 404:
            return None  # Dolibarr answers "not found" for empty lists
        if r.status_code >= 400:
            raise DolibarrError(f"Dolibarr {path.split('?')[0]} failed (HTTP {r.status_code}): {r.text[:200]}")
        return r.json() if r.content else None

    def category_id(self) -> int:
        if self._category_id is None:
            found = self._call("GET", "categories", params={
                "type": "contact", "sqlfilters": f"(t.label:=:'{self.tag.replace(chr(39), '')}')", "limit": 5})
            if not found:
                raise DolibarrError(f'No contact tag named "{self.tag}" in Dolibarr')
            self._category_id = int(found[0]["id"])
        return self._category_id

    def recipients(self) -> list[dict]:
        """Active contacts with the tag, an email address, and no opt-out. One entry per address."""
        out: dict[str, dict] = {}
        page = 0
        while True:
            rows = self._call("GET", "contacts", params={"category": self.category_id(), "limit": 200, "page": page}) or []
            for c in rows:
                email = (c.get("email") or "").strip()
                if not valid_email(email) or str(c.get("statut", c.get("status", "1"))) != "1":
                    continue
                if str(c.get("no_email") or "0") not in ("0", "", "None"):
                    continue
                name = " ".join(x for x in (c.get("firstname"), c.get("lastname")) if x).strip()
                out.setdefault(email.lower(), {"email": email, "contact_id": int(c["id"]), "name": name})
            if len(rows) < 200:
                break
            page += 1
        return list(out.values())

    def contacts_with_email(self, email: str) -> list[dict]:
        if not valid_email(email):
            return []
        return self._call("GET", "contacts", params={"sqlfilters": f"(t.email:=:'{email}')", "limit": 20}) or []

    def unsubscribe(self, email: str) -> int:
        """Take every contact with this address off the list. Returns how many were changed."""
        changed = 0
        cat = self.category_id()
        for c in self.contacts_with_email(email):
            cid = int(c["id"])
            self._call("PUT", f"contacts/{cid}", json={"no_email": 1})
            self._call("DELETE", f"categories/{cat}/objects/contact/{cid}")
            changed += 1
        return changed

    def log_email(self, contact_id: int, subject: str, note: str) -> None:
        """Record the send on the contact's agenda so Dolibarr keeps the email history."""
        now = int(datetime.now(UTC).timestamp())
        self._call("POST", "agendaevents", json={
            "type_code": "AC_EMAIL", "label": f"Email sent: {subject}"[:128], "note_private": note,
            "datep": now, "datef": now, "percentage": -1, "userownerid": self.user_id,
            "contact_id": contact_id, "socpeopleassigned": {str(contact_id): {"id": contact_id}},
        })
