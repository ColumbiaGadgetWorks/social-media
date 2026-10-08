"""Events calendar (ICS) sync: event records, promo posts, and change/cancel handling.

Optional lines in an event's description steer the Studio:
  Promote: no | yes   skip promos, or force them for a recurring event
  Email: no           keep it out of the monthly email (Phase 3)
  Signup: https://…   the link posts should use
  Price: Free         shown on the event card
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta

import httpx
import icalendar
import recurring_ical_events
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import media as media_mod
from .db import settings, utcnow
from .models import Event, Post
from .posts import create_post
from .security import Actor, audit
from .timeutil import to_local

log = logging.getLogger(__name__)
DIRECTIVE_RE = re.compile(r"^\s*(promote|email|signup|price)\s*:\s*(.+?)\s*$", re.I | re.M)
URL_RE = re.compile(r"https?://[^\s<>\"')]+")
SYSTEM = Actor.system()


# --- parsing -------------------------------------------------------------


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _to_utc(value) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=settings().timezone)
        return value.astimezone(UTC).replace(tzinfo=None), False
    local = datetime.combine(value, time.min, tzinfo=settings().timezone)
    return local.astimezone(UTC).replace(tzinfo=None), True


def parse(text: str, start: datetime, end: datetime) -> list[dict]:
    """Occurrences between start and end (naive UTC), recurring events expanded."""
    cal = icalendar.Calendar.from_ical(text)
    recurring_uids = {str(c.get("UID")) for c in cal.walk("VEVENT") if c.get("RRULE")}
    tz = settings().timezone
    window = (start.replace(tzinfo=UTC).astimezone(tz), end.replace(tzinfo=UTC).astimezone(tz))
    out = []
    for ev in recurring_ical_events.of(cal).between(*window):
        uid = str(ev.get("UID", ""))
        begin, all_day = _to_utc(ev.decoded("DTSTART"))
        finish = _to_utc(ev.decoded("DTEND"))[0] if ev.get("DTEND") else None
        raw_desc = _clean(str(ev.get("DESCRIPTION", "")))
        directives = {k.lower(): v for k, v in DIRECTIVE_RE.findall(raw_desc)}
        description = DIRECTIVE_RE.sub("", raw_desc).strip()
        url = directives.get("signup") or str(ev.get("URL", "") or "")
        if not url:
            found = URL_RE.search(description)
            url = found.group() if found else ""
        out.append({
            "uid": uid, "start": begin, "end": finish, "all_day": all_day,
            "title": str(ev.get("SUMMARY", "")).strip(), "description": description,
            "location": str(ev.get("LOCATION", "") or "").strip(), "url": url,
            "recurring": uid in recurring_uids,
            "cancelled": str(ev.get("STATUS", "")).upper() == "CANCELLED",
            "directives": directives,
        })
    return out


def facts_hash(o: dict) -> str:
    keys = ("title", "start", "end", "location", "description", "url")  # what promos depend on
    return hashlib.sha256(json.dumps({k: str(o[k]) for k in keys}, sort_keys=True).encode()).hexdigest()


def should_promote(o: dict) -> bool:
    choice = o["directives"].get("promote", "").lower()
    if choice in ("no", "false", "off"):
        return False
    if choice in ("yes", "true", "on"):
        return True
    if o["recurring"]:
        return False
    title = o["title"].lower()
    return not any(word.lower() in title for word in settings().event_skip_keywords)


# --- event cards ---------------------------------------------------------

CARD_SIZE = (1080, 1350)
ACCENT, INK, PAPER = (164, 89, 111), (35, 32, 31), (255, 255, 255)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def event_when(event: Event) -> tuple[str, str]:
    local = to_local(event.start)
    day = local.strftime("%A, %B %-d")
    if event.all_day:
        return day, "All day"
    t = local.strftime("%-I:%M %p").replace(":00 ", " ")
    if event.end:
        t += " to " + to_local(event.end).strftime("%-I:%M %p").replace(":00 ", " ")
    return day, t


def render_card(event: Event, price: str = "") -> bytes:
    """A plain, on-brand event graphic: facts only, straight from the calendar."""
    img = Image.new("RGB", CARD_SIZE, ACCENT)
    d = ImageDraw.Draw(img)
    margin, width = 90, CARD_SIZE[0] - 180
    label_font = ImageFont.load_default(size=40)
    kind = "CLASS" if "class" in (event.title + event.description).lower() else "EVENT"
    d.text((margin, 110), f"{kind} AT COLUMBIA GADGET WORKS", font=label_font, fill=PAPER)
    d.rectangle((margin, 172, margin + 120, 180), fill=PAPER)

    size = 104
    while size > 56:
        title_font = ImageFont.load_default(size=size)
        lines = _wrap(d, event.title, title_font, width)
        if len(lines) <= 4:
            break
        size -= 8
    y = 240
    for line in lines[:5]:
        d.text((margin, y), line, font=title_font, fill=PAPER)
        y += int(size * 1.15)

    panel_top = max(y + 60, 760)
    d.rounded_rectangle((60, panel_top, CARD_SIZE[0] - 60, CARD_SIZE[1] - 170), radius=28, fill=PAPER)
    info_font, small_font = ImageFont.load_default(size=56), ImageFont.load_default(size=42)
    day, when = event_when(event)
    iy = panel_top + 50
    for text, font in ((day, info_font), (when, info_font), (event.location or "The shop, on The Loop", small_font)):
        for line in _wrap(d, text, font, width - 40)[:2]:
            d.text((margin + 20, iy), line, font=font, fill=INK)
            iy += int(font.size * 1.3)
    if price:
        d.text((margin + 20, iy + 10), price, font=info_font, fill=ACCENT)
    d.text((margin, CARD_SIZE[1] - 120), "columbiagadgetworks.org", font=small_font, fill=PAPER)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _make_card(db: Session, event: Event, price: str):
    data = render_card(event, price)
    asset = media_mod.store_upload(
        db, io.BytesIO(data), f"event-{event.id}.jpg", "image/jpeg", None,
        note=f"Event card: {event.title}",
    )
    asset.tags = ["event-card"]
    media_mod.process(asset)
    event.card_media_id = asset.id
    return asset


# --- promos and changes --------------------------------------------------


def _at_local(day: date, hour: int, minute: int) -> datetime:
    local = datetime.combine(day, time(hour, minute), tzinfo=settings().timezone)
    return local.astimezone(UTC).replace(tzinfo=None)


def _note(event: Event, purpose: str) -> str:
    day, when = event_when(event)
    parts = [f"{purpose.capitalize()} for: {event.title}", f"{day}, {when}"]
    if event.location:
        parts.append(event.location)
    if event.url:
        parts.append(f"Link: {event.url}")
    return " · ".join(parts)


def promo_targets(event: Event) -> dict[str, datetime]:
    """When to aim each promo: announce two weeks out (or tomorrow), remind two days out."""
    now = utcnow()
    local_start = to_local(event.start).date()
    announce = _at_local(local_start - timedelta(days=14), 11, 30)
    if announce < now + timedelta(days=1):
        announce = _at_local(to_local(now).date() + timedelta(days=1), 11, 30)
    targets = {"announce": announce}
    reminder = _at_local(local_start - timedelta(days=2), 11, 30)
    if reminder - announce >= timedelta(days=3) and reminder > now:
        targets["reminder"] = reminder
    return targets


def create_promos(db: Session, event: Event, price: str = "") -> list[Post]:
    card = _make_card(db, event, price)
    posts = []
    for purpose, target in promo_targets(event).items():
        post = create_post(db, SYSTEM, [card.id], note=_note(event, purpose), pillar="classes_events",
                           for_claude=True, source="event")
        post.event_id, post.purpose, post.target_at = event.id, purpose, target
        post.title = f"{purpose.capitalize()}: {event.title}"[:200]
        posts.append(post)
    event.promos_created = True
    audit(db, SYSTEM, "event_promos_created", "event", event.id, posts=[p.id for p in posts], title=event.title)
    return posts


def _open_posts(event: Event) -> list[Post]:
    return [p for p in event.posts if p.status not in ("done", "rejected")
            and not any(v.publish_state == "published" for v in p.versions)]


def _clear_approval(post: Post) -> None:
    post.approved_hash = None
    post.approved_by_id = None
    post.approved_at = None


def handle_change(db: Session, event: Event, price: str, changes: list[str]) -> list[Post]:
    old_card = event.card_media_id
    new_card = _make_card(db, event, price) if event.promos_created else None
    touched = []
    targets = promo_targets(event)
    for post in _open_posts(event):
        if post.purpose in targets:
            post.target_at = targets[post.purpose]
        if new_card is not None:
            for link in post.media_links:
                if link.media_id == old_card:
                    link.media = new_card
        post.note = _note(event, post.purpose or "promo")
        post.review_comment = f"The event changed ({', '.join(changes)}). Check the dates and details."
        if post.status == "approved":
            post.status = "in_review"
            _clear_approval(post)
            audit(db, SYSTEM, "approval_cleared", "post", post.id, reason="event changed", changes=changes)
        touched.append(post)
    audit(db, SYSTEM, "event_changed", "event", event.id, changes=changes, posts=[p.id for p in touched])
    return touched


def handle_cancel(db: Session, event: Event) -> tuple[list[Post], bool]:
    event.status = "cancelled"
    pulled = []
    for post in _open_posts(event):
        post.status = "rejected"
        post.review_comment = "The event was cancelled."
        _clear_approval(post)
        audit(db, SYSTEM, "rejected", "post", post.id, reason="event cancelled")
        pulled.append(post)
    announced = any(v.publish_state == "published" for p in event.posts for v in p.versions)
    if announced:
        post = create_post(db, SYSTEM, [], note=f"Cancellation notice for: {event.title} ({event_when(event)[0]}). "
                           "It was already announced; tell people it's off.", pillar="classes_events",
                           for_claude=True, source="event")
        post.event_id, post.purpose, post.target_at = event.id, "cancellation", utcnow() + timedelta(hours=2)
        post.title = f"Cancelled: {event.title}"[:200]
    audit(db, SYSTEM, "event_cancelled", "event", event.id, pulled=[p.id for p in pulled], announced=announced)
    return pulled, announced


def sync(db: Session, text: str) -> dict:
    """Bring Event rows in line with the feed. Returns what happened, for reminders and logs."""
    from . import reminders

    now = utcnow()
    horizon = now + timedelta(days=settings().calendar_lookahead_days)
    occurrences = parse(text, now, horizon)
    seen: set[int] = set()
    result = {"new": 0, "promoted": 0, "changed": 0, "cancelled": 0}
    for o in occurrences:
        event = db.scalar(select(Event).where(Event.uid == o["uid"], Event.start == o["start"]))
        if event is None and not o["recurring"]:
            # A one-off event that moved keeps its UID: that's a change, not a new event.
            event = db.scalar(select(Event).where(Event.uid == o["uid"], Event.status == "active",
                                                  Event.recurring.is_(False)))
        digest = facts_hash(o)
        price = o["directives"].get("price", "")
        if event is None:
            event = Event(uid=o["uid"], start=o["start"], end=o["end"], all_day=o["all_day"], title=o["title"],
                          description=o["description"], location=o["location"], url=o["url"],
                          recurring=o["recurring"], promote=should_promote(o), facts_hash=digest,
                          email_ok=o["directives"].get("email", "").lower() not in ("no", "false", "off"))
            db.add(event)
            db.flush()
            result["new"] += 1
            if o["cancelled"]:
                event.status = "cancelled"
            elif event.promote and event.start > now + timedelta(days=1):
                create_promos(db, event, price)
                result["promoted"] += 1
        else:
            event.missing_count = 0
            if o["cancelled"] and event.status != "cancelled":
                pulled, announced = handle_cancel(db, event)
                result["cancelled"] += 1
                reminders.event_cancelled(db, event, pulled, announced)
            elif event.facts_hash != digest and event.status != "cancelled":
                fields = ("title", "start", "end", "location", "description", "url")
                changes = [k for k in fields if str(getattr(event, k)) != str(o[k])]
                for k in fields:
                    setattr(event, k, o[k])
                event.facts_hash, event.changed_at = digest, now
                event.promote = should_promote(o)
                event.email_ok = o["directives"].get("email", "").lower() not in ("no", "false", "off")
                touched = handle_change(db, event, price, changes or ["details"])
                result["changed"] += 1
                reminders.event_changed(db, event, touched, changes)
        seen.add(event.id)

    # An occurrence that vanished from the feed twice in a row was deleted (or moved).
    upcoming = db.scalars(select(Event).where(Event.start > now, Event.start < horizon, Event.status == "active")).all()
    for event in upcoming:
        if event.id in seen:
            continue
        event.missing_count += 1
        if event.missing_count >= 2:
            pulled, announced = handle_cancel(db, event)
            result["cancelled"] += 1
            reminders.event_cancelled(db, event, pulled, announced)
    return result


def fetch(http: httpx.Client | None = None) -> str:
    url = settings().calendar_ics_url
    client = http or httpx.Client(timeout=30, follow_redirects=True)
    r = client.get(url, headers={"User-Agent": "CGW Content Studio"})
    r.raise_for_status()
    return r.text


def upcoming_events(db: Session, days: int = 30) -> list[Event]:
    now = utcnow()
    return db.scalars(
        select(Event).where(Event.start >= now, Event.start < now + timedelta(days=days)).order_by(Event.start)
    ).all()


def event_facts(event: Event) -> dict:
    day, when = event_when(event)
    return {"event_id": event.id, "title": event.title, "date": day, "time": when, "location": event.location,
            "link": event.url, "description": event.description[:1500], "status": event.status,
            "recurring": event.recurring}

