"""Email announcements: the monthly "Coming up at CGW" email and the occasional special one.

Rules (see the plan):
- Only classes, events and important organizational news. This is its own record type;
  nothing turns a social post into an email.
- 1-2 a month. A third in the same month needs an admin override, which is logged.
- Same approval rule as posts: a signed-in approver approves the exact content (hash),
  any edit clears the approval, and the sender re-checks it right before sending.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import smtplib
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mailer
from .db import settings, utcnow
from .dolibarr import Dolibarr, valid_email
from .models import Announcement, AnnouncementRecipient, Event, Unsubscribe
from .security import Actor, audit
from .timeutil import local_now, to_local

log = logging.getLogger(__name__)
MONTHLY_CAP = 2
DRAFT_DAYS_AHEAD = 12
EDITABLE = ("subject", "preheader", "intro", "closing", "items", "send_at")
DEFAULT_CLOSING = "Open Hack Night is every Thursday at 6 PM. It's free and open to everyone, no sign-up needed."

_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates" / "email")),
                   autoescape=select_autoescape(["html"]))


class AnnouncementError(Exception):
    pass


# --- dates --------------------------------------------------------------------


def first_tuesday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(1 - d.weekday()) % 7)


def monthly_send_at(year: int, month: int) -> datetime:
    from datetime import UTC
    from datetime import time as dtime

    local = datetime.combine(first_tuesday(year, month), dtime(settings().announce_hour), tzinfo=settings().timezone)
    return local.astimezone(UTC).replace(tzinfo=None)


def _month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    from datetime import UTC
    from datetime import time as dtime

    tz = settings().timezone
    start = datetime.combine(date(year, month, 1), dtime(0), tzinfo=tz)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    end = datetime.combine(nxt, dtime(0), tzinfo=tz)
    return start.astimezone(UTC).replace(tzinfo=None), end.astimezone(UTC).replace(tzinfo=None)


# --- creating drafts -----------------------------------------------------------


def event_item(event: Event) -> dict:
    from .calendar_sync import event_when

    day, when = event_when(event)
    return {"event_id": event.id, "title": event.title, "when": f"{day}, {when}", "where": event.location,
            "link": event.url or f"{settings().website_url}/calendar/", "blurb": ""}


def ensure_monthly(db: Session) -> Announcement | None:
    """Create next month's draft about 12 days before its send date (the first Tuesday)."""
    now = utcnow()
    today = local_now().date()
    for year, month in ((today.year, today.month), (today.year + (today.month == 12), today.month % 12 + 1)):
        key = f"{year:04d}-{month:02d}"
        send_at = monthly_send_at(year, month)
        if send_at <= now or send_at - now > timedelta(days=DRAFT_DAYS_AHEAD):
            continue
        if db.scalar(select(Announcement).where(Announcement.kind == "monthly", Announcement.month == key)):
            continue
        _, month_end = _month_bounds_utc(year, month)
        events = db.scalars(
            select(Event).where(Event.start >= send_at, Event.start < month_end, Event.status == "active",
                                Event.recurring.is_(False), Event.email_ok.is_(True)).order_by(Event.start)
        ).all()
        name = date(year, month, 1).strftime("%B")
        ann = Announcement(kind="monthly", month=key, subject=f"Coming up at CGW: {name}",
                           items=[event_item(e) for e in events], closing=DEFAULT_CLOSING, send_at=send_at,
                           status="needs_claude")
        db.add(ann)
        db.flush()
        audit(db, Actor.system(), "announcement_created", "announcement", ann.id, month=key, items=len(events))
        return ann
    return None


def create_special(db: Session, actor: Actor, subject: str, send_at: datetime) -> Announcement:
    if not actor.is_human or not actor.user.has_role("editor"):
        raise AnnouncementError("Only editors can start a special announcement.")
    ann = Announcement(kind="special", subject=subject.strip()[:200], send_at=send_at, items=[], status="draft",
                       closing=DEFAULT_CLOSING)
    db.add(ann)
    db.flush()
    audit(db, actor, "announcement_created", "announcement", ann.id, kind="special")
    return ann


# --- approval ------------------------------------------------------------------


def content_hash(ann: Announcement) -> str:
    payload = {"id": ann.id, **{k: getattr(ann, k) for k in ("subject", "preheader", "intro", "closing", "items")},
               "send_at": ann.send_at.isoformat() if ann.send_at else None}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def same_month_count(db: Session, ann: Announcement) -> int:
    if ann.send_at is None:
        return 0
    local = to_local(ann.send_at)
    start, end = _month_bounds_utc(local.year, local.month)
    return len(db.scalars(select(Announcement.id).where(
        Announcement.id != ann.id, Announcement.status.in_(("approved", "sent")),
        Announcement.send_at >= start, Announcement.send_at < end)).all())


def problems(db: Session, ann: Announcement) -> list[str]:
    found = []
    if not ann.subject.strip():
        found.append("the subject is empty")
    if not ann.intro.strip():
        found.append("the opening paragraph is empty")
    if ann.kind == "monthly" and not ann.items:
        found.append("there are no classes or events in it; cancel it or add news to the opening")
    if any(not str(i.get("blurb", "")).strip() for i in ann.items):
        found.append("some items have no description")
    if ann.send_at is None:
        found.append("no send time")
    elif ann.send_at < utcnow() - timedelta(minutes=5) and ann.status != "sent":
        found.append("the send time is in the past")
    if same_month_count(db, ann) >= MONTHLY_CAP and not ann.override_cap:
        found.append(f"this would be email number {MONTHLY_CAP + 1} this month; the limit is {MONTHLY_CAP} "
                     "unless an admin overrides it")
    return found


def is_sendable(ann: Announcement) -> tuple[bool, str]:
    if ann.status != "approved":
        return False, f"status is {ann.status}"
    if not ann.approved_hash or not ann.approved_by_id:
        return False, "no recorded approval"
    if content_hash(ann) != ann.approved_hash:
        return False, "changed after it was approved"
    return True, ""


def update(db: Session, actor: Actor, ann: Announcement, **fields) -> list[str]:
    if ann.status in ("sent", "cancelled"):
        raise AnnouncementError("This email was already sent or cancelled.")
    if actor.type == "mcp" and ann.status == "approved":
        raise AnnouncementError("Approved emails can only be edited in the web app.")
    changed = []
    for key, value in fields.items():
        if key in EDITABLE and value is not None and getattr(ann, key) != value:
            setattr(ann, key, value)
            changed.append(key)
    if not changed:
        return changed
    ann.updated_at = utcnow()
    if ann.status == "approved":
        ann.status = "in_review"
        ann.approved_hash = ann.approved_by_id = ann.approved_at = None
        audit(db, actor, "approval_cleared", "announcement", ann.id, fields=changed)
    elif ann.status == "needs_claude":
        ann.status = "draft"
    audit(db, actor, "announcement_edited", "announcement", ann.id, fields=changed)
    return changed


def submit(db: Session, actor: Actor, ann: Announcement) -> None:
    if ann.status not in ("needs_claude", "draft"):
        raise AnnouncementError(f"An email that is {ann.status.replace('_', ' ')} can't be submitted.")
    found = problems(db, ann)
    if found:
        raise AnnouncementError("Can't send for review yet: " + "; ".join(found) + ".")
    ann.status = "in_review"
    ann.review_comment = ""
    audit(db, actor, "submitted", "announcement", ann.id)


def approve(db: Session, actor: Actor, ann: Announcement, seen_hash: str | None) -> None:
    if not actor.is_human or not actor.user.has_role("approver"):
        raise AnnouncementError("Only a signed-in approver can approve emails.")
    if ann.status != "in_review":
        raise AnnouncementError("Only emails in review can be approved.")
    found = problems(db, ann)
    if found:
        raise AnnouncementError("Can't approve yet: " + "; ".join(found) + ".")
    current = content_hash(ann)
    if seen_hash != current:
        raise AnnouncementError("This email changed after you opened it. Review the new version and approve again.")
    ann.approved_hash, ann.approved_by_id, ann.approved_at, ann.status = current, actor.user.id, utcnow(), "approved"
    audit(db, actor, "approved", "announcement", ann.id, hash=current, send_at=ann.send_at.isoformat())


def send_back(db: Session, actor: Actor, ann: Announcement, comment: str, cancel: bool = False) -> None:
    if not actor.is_human or not actor.user.has_role("approver"):
        raise AnnouncementError("Only an approver can do that.")
    if ann.status in ("sent", "cancelled"):
        raise AnnouncementError("This email was already sent or cancelled.")
    ann.status = "cancelled" if cancel else "draft"
    ann.review_comment = comment
    ann.approved_hash = ann.approved_by_id = ann.approved_at = None
    audit(db, actor, "cancelled" if cancel else "changes_requested", "announcement", ann.id, comment=comment)


def set_override(db: Session, actor: Actor, ann: Announcement, on: bool) -> None:
    if not actor.is_human or not actor.user.has_role("admin"):
        raise AnnouncementError("Only an admin can override the monthly limit.")
    ann.override_cap = on
    audit(db, actor, "cap_override", "announcement", ann.id, on=on)


# --- rendering -------------------------------------------------------------------


def _inline(text: str) -> str:
    """Escape, then allow **bold** and [links](https://...) only."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2" style="color:#a4596f">\1</a>', out)


def paragraphs(text: str) -> list[str]:
    return [_inline(p.strip()).replace("\n", "<br>") for p in (text or "").split("\n\n") if p.strip()]


def plain(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", (text or "").replace("**", ""))


def render(ann: Announcement, unsubscribe_url: str) -> tuple[str, str]:
    ctx = {"a": ann, "intro": paragraphs(ann.intro), "closing": paragraphs(ann.closing),
           "items": [{**i, "blurb_html": _inline(i.get("blurb", ""))} for i in ann.items],
           "unsubscribe_url": unsubscribe_url, "address": settings().org_address,
           "site": settings().website_url, "plain": plain}
    return _env.get_template("announcement.html").render(ctx), _env.get_template("announcement.txt").render(ctx)


# --- unsubscribe -----------------------------------------------------------------


def _sig(email: str) -> str:
    return hmac.new(settings().secret_key.encode(), f"unsub:{email.lower()}".encode(), hashlib.sha256).hexdigest()[:24]


def unsubscribe_token(email: str) -> str:
    return base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=") + "." + _sig(email)


def email_from_token(token: str) -> str | None:
    try:
        encoded, sig = token.rsplit(".", 1)
        email = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return email if valid_email(email) and hmac.compare_digest(sig, _sig(email)) else None


def unsubscribe_url(email: str) -> str:
    return f"{settings().media_base_url}/u/{unsubscribe_token(email)}"


def record_unsubscribe(db: Session, email: str, dolibarr: Dolibarr | None = None) -> Unsubscribe:
    row = Unsubscribe(email=email.lower())
    db.add(row)
    db.flush()
    audit(db, Actor.system(), "unsubscribed", "unsubscribe", row.id)
    sync_unsubscribe(row, dolibarr)
    return row


def sync_unsubscribe(row: Unsubscribe, dolibarr: Dolibarr | None = None) -> None:
    if not settings().dolibarr_configured:
        row.error = "Dolibarr isn't configured; kept locally"
        return
    try:
        (dolibarr or Dolibarr()).unsubscribe(row.email)
        row.synced_at, row.error = utcnow(), ""
    except Exception as exc:
        row.error = str(exc)[:300]


def sync_pending_unsubscribes(db: Session, dolibarr: Dolibarr | None = None) -> int:
    rows = db.scalars(select(Unsubscribe).where(Unsubscribe.synced_at.is_(None))).all()
    for row in rows:
        sync_unsubscribe(row, dolibarr)
    return len(rows)


# --- sending ------------------------------------------------------------------------


def build_message(ann: Announcement, to: str, test: bool = False) -> EmailMessage:
    s = settings()
    unsub = unsubscribe_url(to)
    html_body, text_body = render(ann, unsub)
    msg = EmailMessage()
    msg["From"] = s.announce_from
    msg["To"] = to
    msg["Reply-To"] = s.announce_reply_to
    msg["Subject"] = ("[TEST] " if test else "") + ann.subject
    msg["Message-ID"] = make_msgid(domain=s.announce_reply_to.split("@")[-1] or None)
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"  # RFC 8058 one-click
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


class _Transport:
    """One SMTP connection for a whole send (or the in-memory outbox in tests)."""

    def __enter__(self):
        s = settings()
        self.smtp = None
        if s.mail_backend == "smtp":
            self.smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
            if s.smtp_starttls:
                self.smtp.starttls()
            if s.smtp_user:
                self.smtp.login(s.smtp_user, s.smtp_password)
        return self

    def send(self, msg: EmailMessage) -> None:
        if self.smtp is not None:
            self.smtp.send_message(msg)
        elif settings().mail_backend == "memory":
            mailer.outbox.append(msg)
        else:
            log.info("EMAIL to %s: %s", msg["To"], msg["Subject"])

    def __exit__(self, *exc):
        if self.smtp is not None:
            self.smtp.quit()


def send_test(ann: Announcement, to: str) -> None:
    with _Transport() as t:
        t.send(build_message(ann, to, test=True))


def recipients(db: Session, dolibarr: Dolibarr | None = None) -> list[dict]:
    pending = {u.email for u in db.scalars(select(Unsubscribe).where(Unsubscribe.synced_at.is_(None)))}
    return [r for r in (dolibarr or Dolibarr()).recipients() if r["email"].lower() not in pending]


def send(db: Session, ann: Announcement, dolibarr: Dolibarr | None = None, pause: float = 0.2) -> int:
    """Send to the list. Safe to resume: addresses already sent to are skipped."""
    ok, reason = is_sendable(ann)
    if not ok:
        raise AnnouncementError(f"Not sending: {reason}.")
    dol = dolibarr or Dolibarr()
    already = {r.email.lower() for r in db.scalars(
        select(AnnouncementRecipient).where(AnnouncementRecipient.announcement_id == ann.id,
                                            AnnouncementRecipient.status == "sent"))}
    sent = 0
    with _Transport() as t:
        for r in recipients(db, dol):
            if r["email"].lower() in already:
                continue
            row = AnnouncementRecipient(announcement_id=ann.id, email=r["email"], contact_id=r["contact_id"])
            try:
                t.send(build_message(ann, r["email"]))
                sent += 1
            except Exception as exc:
                row.status, row.error = "failed", str(exc)[:300]
            db.add(row)
            db.commit()
            if pause:
                time.sleep(pause)
    rows = db.scalars(select(AnnouncementRecipient).where(AnnouncementRecipient.announcement_id == ann.id)).all()
    ann.status, ann.sent_at = "sent", utcnow()
    ann.recipient_count = sum(1 for r in rows if r.status == "sent")
    audit(db, Actor.system(), "announcement_sent", "announcement", ann.id, sent=ann.recipient_count,
          failed=sum(1 for r in rows if r.status == "failed"))
    db.commit()
    for r in rows:  # Dolibarr keeps the email history; best effort
        if r.status == "sent" and r.contact_id:
            try:
                dol.log_email(r.contact_id, ann.subject, f"CGW Content Studio announcement {ann.id}")
            except Exception as exc:
                log.warning("logging email to Dolibarr contact %s failed: %s", r.contact_id, exc)
    return sent


def due(db: Session) -> list[Announcement]:
    return db.scalars(select(Announcement).where(Announcement.status == "approved",
                                                 Announcement.send_at <= utcnow())).all()
