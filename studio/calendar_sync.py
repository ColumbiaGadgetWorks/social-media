"""Events calendar (ICS) sync: event records, promo posts, and change/cancel handling.

Optional lines in an event's description steer the Studio:
  Promote: no | yes   skip promos, or force them for a recurring event
  Email: no           keep it out of the monthly email (Phase 3)
  Signup: https://…   the link posts should use
  Price: Free         shown on the event card

Weekly events (the same title, weekday and time every week, like Open Hack Night) get one post per
week instead of an announcement and a reminder, each with a different angle and real photos.
"""

from __future__ import annotations

import functools
import hashlib
import html
import io
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import icalendar
import recurring_ical_events
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import media as media_mod
from .db import settings, utcnow
from .models import Event, MediaAsset, Post, PostMedia
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
    _mark_series(out)
    return out


def _norm_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", title.lower()).split())


def _mark_series(occurrences: list[dict]) -> None:
    """Tag occurrences that repeat weekly (same title, weekday and local time, a week or so apart).

    The website's feed flattens recurring events into separate events with separate UIDs, so an
    RRULE alone can't be relied on.
    """
    tz = settings().timezone
    groups: dict[str, list[dict]] = defaultdict(list)
    for o in occurrences:
        o["series"] = ""
        if o["all_day"] or not o["title"]:
            continue
        local = o["start"].replace(tzinfo=UTC).astimezone(tz)
        groups[f"{_norm_title(o['title'])}|{local:%a}|{local:%H:%M}"].append(o)
    for key, items in groups.items():
        if len(items) < 2:
            continue
        starts = sorted(o["start"] for o in items)
        gaps = [b - a for a, b in pairwise(starts)]
        if min(gaps) <= timedelta(days=8) and max(gaps) <= timedelta(days=22):  # weekly, allowing a skipped week or two
            for o in items:
                o["series"] = key


def facts_hash(o: dict) -> str:
    keys = ("title", "start", "end", "location", "description", "url")  # what promos depend on
    return hashlib.sha256(json.dumps({k: str(o[k]) for k in keys}, sort_keys=True).encode()).hexdigest()


def should_promote(o: dict) -> bool:
    choice = o["directives"].get("promote", "").lower()
    if choice in ("no", "false", "off"):
        return False
    if choice in ("yes", "true", "on"):
        return True
    title = o["title"].lower()
    skipped = any(word.lower() in title for word in settings().event_skip_keywords)
    if o.get("series"):
        return not skipped  # weekly events get a weekly post
    if o["recurring"]:
        return False
    return not skipped


# --- event cards ---------------------------------------------------------

CARD_SIZE = (1080, 1350)
ACCENT, INK, PAPER = (191, 77, 40), (31, 31, 35), (255, 255, 255)


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


LOGO_PATH = Path(__file__).parent / "static" / "cgw-logo.png"  # the CGW logo: orange artwork, transparent background
LOGO_HEIGHT = 150


@functools.lru_cache(maxsize=1)
def _logo_mark() -> Image.Image | None:
    """The CGW logo as a white silhouette. The artwork is the same orange as the card, so it is
    drawn in white (its shape comes from the file's transparency) to stand out."""
    if not LOGO_PATH.exists():
        return None
    with Image.open(LOGO_PATH) as src:
        alpha = src.convert("RGBA").getchannel("A")
    alpha = alpha.crop(alpha.getbbox())
    width = round(alpha.width * LOGO_HEIGHT / alpha.height)
    alpha = alpha.resize((width, LOGO_HEIGHT), Image.Resampling.LANCZOS)
    mark = Image.new("RGBA", alpha.size, (*PAPER, 0))
    mark.putalpha(alpha)
    return mark


def is_cancelled(event: Event) -> bool:
    return event.status == "cancelled" or event.title.strip().lower().startswith(("cancelled", "canceled"))


def render_card(event: Event, price: str = "") -> bytes:
    """A plain, on-brand event graphic: facts only, straight from the calendar."""
    img = Image.new("RGB", CARD_SIZE, ACCENT)
    d = ImageDraw.Draw(img)
    margin, width = 90, CARD_SIZE[0] - 180
    label_font = ImageFont.load_default(size=40)
    cancelled = is_cancelled(event)
    kind = "CLASS" if "class" in (event.title + event.description).lower() else "EVENT"
    label = "COLUMBIA GADGET WORKS" if cancelled else f"{kind} AT COLUMBIA GADGET WORKS"
    d.text((margin, 110), label, font=label_font, fill=PAPER)
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
    d.rounded_rectangle((60, panel_top, CARD_SIZE[0] - 60, CARD_SIZE[1] - 200), radius=28, fill=PAPER)
    info_font, small_font = ImageFont.load_default(size=56), ImageFont.load_default(size=42)
    day, when = event_when(event)
    iy = panel_top + 50
    rows = [(day, info_font)] + ([] if cancelled else [(when, info_font)])
    rows.append((event.location or "The shop, on The Loop", small_font))
    for text, font in rows:
        for line in _wrap(d, text, font, width - 40)[:2]:
            d.text((margin + 20, iy), line, font=font, fill=INK)
            iy += int(font.size * 1.3)
    if price:
        d.text((margin + 20, iy + 10), price, font=info_font, fill=ACCENT)
    d.text((margin, CARD_SIZE[1] - 120), "columbiagadgetworks.org", font=small_font, fill=PAPER)
    if (mark := _logo_mark()) is not None:  # footer, opposite the web address
        img.paste(mark, (CARD_SIZE[0] - margin - mark.width, CARD_SIZE[1] - 20 - mark.height), mark)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _make_card(db: Session, event: Event, price: str):
    data = render_card(event, price)
    asset = media_mod.store_upload(
        db, io.BytesIO(data), f"event-{event.id}.jpg", "image/jpeg", None,
        note=f"Event card: {event.title}",
    )
    asset.tags = ["event-card", _card_tag(data)]
    media_mod.process(asset)
    event.card_media_id = asset.id
    return asset


def _card_tag(data: bytes) -> str:
    """Fingerprint of a rendered card, kept in the media tags so a design change can be spotted later."""
    return "card:" + hashlib.sha256(data).hexdigest()[:16]


# --- promos and changes --------------------------------------------------


def _at_local(day: date, hour: int, minute: int) -> datetime:
    local = datetime.combine(day, time(hour, minute), tzinfo=settings().timezone)
    return local.astimezone(UTC).replace(tzinfo=None)


def _note(event: Event, purpose: str, angle: str = "") -> str:
    day, when = event_when(event)
    label = "Weekly post" if purpose == "weekly" else purpose.capitalize()
    parts = [f"{label} for: {event.title}", f"{day}, {when}"]
    if event.location:
        parts.append(event.location)
    if event.url:
        parts.append(f"Link: {event.url}")
    if angle in ANGLES:
        parts.append(f"Angle: {ANGLES[angle][0]}")
    return " · ".join(parts)


def promo_targets(event: Event) -> dict[str, datetime]:
    """When to aim each promo: announce two weeks out (or tomorrow), remind two days out.
    A weekly event's one post goes out the day before."""
    now = utcnow()
    local_start = to_local(event.start).date()
    if event.series:
        return {"weekly": max(_at_local(local_start - timedelta(days=1), 11, 30), now + timedelta(hours=2))}
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
        post.note = _note(event, post.purpose or "promo", post.angle)
        post.review_comment = f"The event changed ({', '.join(changes)}). Check the dates and details."
        if post.status == "approved":
            post.status = "in_review"
            _clear_approval(post)
            audit(db, SYSTEM, "approval_cleared", "post", post.id, reason="event changed", changes=changes)
        touched.append(post)
    audit(db, SYSTEM, "event_changed", "event", event.id, changes=changes, posts=[p.id for p in touched])
    return touched


def refresh_card(db: Session, event: Event, price: str = "") -> list[Post]:
    """Rebuild an event's card when the card design (or the rules for drawing it) changed since it was made,
    and swap the new one into the event's open posts. A post that was approved goes back to review."""
    if not event.promos_created or not event.card_media_id or event.status != "active" or event.start <= utcnow():
        return []
    old = db.get(MediaAsset, event.card_media_id)
    if old is None:
        return []
    posts = [p for p in _open_posts(event) if any(link.media_id == old.id for link in p.media_links)]
    if not posts or _card_tag(render_card(event, price)) in (old.tags or []):
        return []
    new_card = _make_card(db, event, price)
    for post in posts:
        for link in post.media_links:
            if link.media_id == old.id:
                link.media = new_card
        if post.status == "approved":
            post.status = "in_review"
            post.review_comment = "The event card was redrawn with the current design. Check it before approving again."
            _clear_approval(post)
            audit(db, SYSTEM, "approval_cleared", "post", post.id, reason="event card refreshed")
        audit(db, SYSTEM, "card_refreshed", "post", post.id, event=event.id, media=new_card.id)
    return posts


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


# --- weekly events -------------------------------------------------------

# Angles for weekly posts, so Open Hack Night doesn't read the same every week.
# key: (label, brief, needs real photos)
ANGLES: dict[str, tuple[str, str, bool]] = {
    "project_spotlight": ("Project spotlight", "One thing a member or visitor made or fixed recently, shown in a real "
                          "photo: what it is, the tool or trick behind it. Invite people to bring theirs Thursday.", True),
    "photo_recap": ("Photo recap", "A carousel from last week's session: what people were building, fixing and "
                    "trying. \"This week could be you.\"", True),
    "first_timer": ("First-timer guide", "What actually happens when you walk in the first time: who says hi, what "
                    "to bring, what you can try. No experience or sign-up needed.", False),
    "tool_spotlight": ("Tool spotlight", "One tool people can try at the next session (laser cutter, 3D printers, "
                       "soldering, sewing, wood shop), ideally in a real photo of it in use.", False),
    "fix_it": ("Bring something broken", "Repair angle: lamps, toys, small electronics, a wobbly chair. Someone "
               "here can probably help you fix it.", False),
    "question": ("Question for followers", "Ask something people want to answer (what would you make with X, the "
                 "weirdest thing you've fixed) and tie it back to the next session.", False),
    "humor": ("Shop humor", "A light, real moment from the shop in the CGW voice: a funny sign, a glorious fail, a "
              "very serious robot. Not cringy, no forced memes.", False),
}
PHOTO_WINDOW_DAYS = 28


def series_posts(db: Session, series: str, limit: int = 8) -> list[Post]:
    """Earlier weekly posts for the same series, newest event first (rejected ones left out)."""
    return db.scalars(
        select(Post).join(Event, Post.event_id == Event.id)
        .where(Event.series == series, Post.purpose == "weekly", Post.status != "rejected")
        .order_by(Event.start.desc()).limit(limit)
    ).all()


def pick_angle(recent: list[str], has_photos: bool) -> str:
    """The angle used least recently (never-used first). Photo angles need fresh photos."""
    allowed = [k for k, (_, _, photos) in ANGLES.items() if has_photos or not photos]

    def age(key: str) -> int:
        return recent.index(key) if key in recent else len(recent) + 1

    return max(allowed, key=lambda k: (age(k), -allowed.index(k)))


def candidate_media(db: Session, event: Event, limit: int = 8) -> list[MediaAsset]:
    """Fresh photos for a weekly post, best first: ones marked as taken at an earlier session of
    this series, then recent uploads that mention it or were uploaded during a session.
    Media already in another post (other than a rejected one) is left out."""
    now = utcnow()
    since = now - timedelta(days=PHOTO_WINDOW_DAYS)
    used = select(PostMedia.media_id).join(Post).where(Post.status != "rejected")
    past = db.scalars(select(Event).where(Event.series == event.series, Event.start < now, Event.start >= since)).all()
    base = (select(MediaAsset).where(MediaAsset.processing_status == "ready", MediaAsset.id.not_in(used))
            .order_by(MediaAsset.created_at.desc()))
    found: list[MediaAsset] = []
    if past:
        found += db.scalars(base.where(MediaAsset.event_id.in_([e.id for e in past]))).all()
    words = event.title.strip()
    windows = [MediaAsset.created_at.between(e.start - timedelta(hours=1), (e.end or e.start) + timedelta(hours=4))
               for e in past]
    recent = db.scalars(base.where(MediaAsset.created_at >= since, or_(
        MediaAsset.note.ilike(f"%{words}%"), MediaAsset.description.ilike(f"%{words}%"), *windows))).all()
    seen = {m.id for m in found}
    found += [m for m in recent if m.id not in seen and "event-card" not in (m.tags or [])]
    return [m for m in found if "event-card" not in (m.tags or [])][:limit]


def create_weekly_promo(db: Session, event: Event, price: str = "") -> Post:
    card = _make_card(db, event, price)
    candidates = candidate_media(db, event)
    recent = [p.angle for p in series_posts(db, event.series)]
    angle = pick_angle(recent, has_photos=bool(candidates))
    images = [m for m in candidates if m.kind == "image"]
    if angle == "photo_recap" and len(images) >= 2:
        media = images[:5]
    elif candidates:
        media = candidates[:1]
    else:
        media = [card]  # nothing fresh yet: the card holds the spot, Claude asks for photos
    pillar = "hack_night" if "hack night" in event.title.lower() else "classes_events"
    post = create_post(db, SYSTEM, [m.id for m in media], note=_note(event, "weekly", angle), pillar=pillar,
                       for_claude=True, source="event")
    post.event_id, post.purpose, post.angle = event.id, "weekly", angle
    post.target_at = promo_targets(event)["weekly"]
    post.title = f"{event.title}: {ANGLES[angle][0]}"[:200]
    event.promos_created = True
    audit(db, SYSTEM, "event_promos_created", "event", event.id, posts=[post.id], title=event.title, angle=angle,
          media=[m.id for m in media])
    return post


def _retire_old_promos(db: Session, event: Event) -> None:
    """Announcement/reminder pairs made before weekly posts existed: pull the ones still showing only
    the event card and not approved, so a weekly post replaces them."""
    old = [p for p in _open_posts(event) if p.purpose in ("announce", "reminder")]
    if not old:
        return
    keep = [p for p in old if p.status == "approved" or any(m.id != event.card_media_id for m in p.media)]
    for post in old:
        if post in keep:
            continue
        post.status = "rejected"
        post.review_comment = "Replaced: weekly events now get one post a week with a fresh angle and real photos."
        _clear_approval(post)
        audit(db, SYSTEM, "rejected", "post", post.id, reason="weekly event: replaced by a weekly post")
    if not keep:
        event.promos_created = False


def retire_past_posts(db: Session) -> int:
    """An event post nobody drafted before the event started is no use any more: take it out of Claude's
    queue. Drafts, posts in review and approved posts are left to the people who own them."""
    posts = db.scalars(select(Post).join(Event, Post.event_id == Event.id)
                       .where(Post.status == "needs_claude", Event.start < utcnow())).all()
    for post in posts:
        post.status = "rejected"
        post.review_comment = "The event already happened before this was drafted."
        _clear_approval(post)
        audit(db, SYSTEM, "rejected", "post", post.id, reason="event already happened")
    return len(posts)


def weekly_promos(db: Session) -> int:
    """Create each weekly event's post once it's within weekly_promo_days, so it can use the latest photos."""
    now = utcnow()
    horizon = now + timedelta(days=settings().weekly_promo_days)
    made = 0
    events = db.scalars(select(Event).where(Event.series != "", Event.status == "active", Event.start > now)
                        .order_by(Event.start)).all()
    for event in events:
        _retire_old_promos(db, event)
        if event.promote and not event.promos_created and now + timedelta(hours=12) < event.start < horizon:
            create_weekly_promo(db, event)
            made += 1
    return made


def series_context(db: Session, post: Post) -> dict:
    """What Claude needs to make this week's post different from the last few."""
    event = post.event
    history = []
    for p in series_posts(db, event.series):
        if p.id == post.id:
            continue
        first_lines = sorted({(v.body.strip().splitlines() or [""])[0][:140] for v in p.versions if v.enabled and v.body})
        history.append({"date": event_when(p.event)[0], "angle": p.angle, "status": p.status,
                        "opening_lines": first_lines, "media": [m.id for m in p.media]})
    candidates = candidate_media(db, event)
    in_post = {m.id for m in post.media}
    label, brief, _ = ANGLES.get(post.angle, ("", "", False))
    only_card = bool(post.media) and all(m.id == event.card_media_id for m in post.media)
    return {
        "angle": post.angle, "angle_label": label, "angle_brief": brief,
        "other_angles": {k: v[0] for k, v in ANGLES.items() if k != post.angle},
        "recent_posts": history,
        "candidate_media": [{"media_id": m.id, "kind": m.kind, "note": m.note, "description": m.description,
                             "uploaded": to_local(m.created_at).strftime("%a %b %-d"), "taken_at_event": bool(m.event_id)}
                            for m in candidates if m.id not in in_post],
        "card_media_id": event.card_media_id,
        "needs_photos": only_card,
    }


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
                          recurring=o["recurring"], series=o["series"], promote=should_promote(o), facts_hash=digest,
                          email_ok=o["directives"].get("email", "").lower() not in ("no", "false", "off"))
            db.add(event)
            db.flush()
            result["new"] += 1
            if o["cancelled"]:
                event.status = "cancelled"
            elif event.promote and not event.series and event.start > now + timedelta(days=1):
                create_promos(db, event, price)
                result["promoted"] += 1
        else:
            event.missing_count = 0
            if o["series"] and event.series != o["series"]:  # spotted as weekly (also after an upgrade)
                event.series, event.promote = o["series"], should_promote(o)
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
            if refresh_card(db, event, price):
                result["refreshed"] = result.get("refreshed", 0) + 1
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
    result["promoted"] += weekly_promos(db)
    if retired := retire_past_posts(db):
        result["retired"] = retired
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
            "recurring": event.recurring, "weekly": bool(event.series)}

