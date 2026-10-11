"""Emails from the Studio, kept to three reasons, each switchable in Settings (see EMAIL_KINDS).

- Running low: posts are planned about eight weeks ahead at a time, so it says nothing until only one
  approved post is left to go out. Then one email, and after that one a week for as long as there are
  zero or one left. When the runway recovers, the next dip emails right away again.
- A post failed to publish (after its retries), bundled into at most one alert email a day.
- The monthly email to the mailing list failed (after its retries).
- Nothing in the first 24 hours after the Studio starts on a new database, and nothing in the evening.

There's no weekly digest and no "last call" any more. A changed or cancelled event just takes its queued
posts out of the queue (see calendar_sync), without an email.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import channels as ch
from . import mailer, timeutil
from .db import settings
from .models import AppSetting, ChannelVersion, PendingAlert, Post, ReminderLog, User
from .queue import claude_queue, runway
from .timeutil import local_now, to_local


def utcnow():
    return timeutil.utcnow()  # looked up each call, so tests can move the clock

log = logging.getLogger(__name__)

QUIET_START = timedelta(hours=24)
ALERT_GAP = timedelta(hours=23)
EVENING_HOUR = 21  # no email after this local hour

# Every reason the Studio emails the team: (key, name, what it is, on unless changed in Settings).
EMAIL_KINDS = [
    ("runway", "Running low on posts",
     "Only one approved post (or none) is left to go out. One email, then one a week while it stays that low.", True),
    ("alert_publish", "A post failed to publish",
     "A post couldn't be sent to its platform after retrying. At most one alert email a day.", True),
    ("alert_announcement", "The monthly email failed to send",
     "The monthly email to the mailing list couldn't be sent after three tries.", True),
]
KIND_DEFAULTS = {key: default for key, _, _, default in EMAIL_KINDS}
ALERT_SETTING = {"publish_failed": "alert_publish", "announcement_failed": "alert_announcement"}
RUNWAY_LOW = 1  # email when this many approved posts (or fewer) are left
RUNWAY_REPEAT = timedelta(days=7)
PLAN_WEEKS = 8  # how far ahead Claude sessions plan


def enabled(db: Session, kind: str) -> bool:
    """Is this email on? A choice made in Settings wins; otherwise the default."""
    row = db.get(AppSetting, f"email.{kind}")
    return (row.value == "1") if row is not None else KIND_DEFAULTS[kind]


def set_enabled(db: Session, kind: str, on: bool) -> None:
    row = db.get(AppSetting, f"email.{kind}")
    if row is None:
        db.add(AppSetting(key=f"email.{kind}", value="1" if on else "0"))
    else:
        row.value = "1" if on else "0"
    db.flush()


def recipients(db: Session) -> list[str]:
    configured = settings().reminder_emails
    if configured:
        return configured
    users = db.scalars(select(User).where(User.is_active.is_(True), User.role.in_(("approver", "admin")))).all()
    return sorted({u.email for u in users if u.email})


def _once(db: Session, kind: str, key: str) -> bool:
    """Record a reminder; False if this one was already sent."""
    if db.scalar(select(ReminderLog).where(ReminderLog.kind == kind, ReminderLog.key == key)):
        return False
    db.add(ReminderLog(kind=kind, key=key, sent_at=utcnow()))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    return True


def _last_sent(db: Session, kind: str) -> datetime | None:
    return db.scalar(select(ReminderLog.sent_at).where(ReminderLog.kind == kind).order_by(ReminderLog.sent_at.desc()).limit(1))


def _send(db: Session, kind: str, key: str, subject: str, body: str, to: list[str] | None = None) -> bool:
    if not _once(db, kind, key):
        return False
    try:
        mailer.send(to or recipients(db), subject, body + _footer())
    except Exception:
        log.exception("sending %s reminder failed", kind)
        db.rollback()  # try again next loop
        return False
    db.commit()
    return True


def _footer() -> str:
    return f"\n\n-- \nCGW Content Studio\nChoose which emails you get: {settings().base_url}/settings"


def started_at(db: Session) -> datetime:
    """When this database first ran the reminder loop (a fresh install, or the upgrade to these emails)."""
    row = db.scalar(select(ReminderLog).where(ReminderLog.kind == "installed", ReminderLog.key == "first_start"))
    if row is None:
        row = ReminderLog(kind="installed", key="first_start", sent_at=utcnow())
        db.add(row)
        db.commit()
    return row.sent_at


def quiet(db: Session) -> bool:
    return utcnow() - started_at(db) < QUIET_START


def _when(dt: datetime) -> str:
    local = to_local(dt)
    return f"{local:%a %b} {local.day}, {local:%I:%M %p}".replace(", 0", ", ")


# --- problem alerts ------------------------------------------------------


def alert(db: Session, kind: str, key: str, line: str, urgent: bool = True) -> None:
    """Queue something to tell people: it goes out in the next alert email (at most one a day)."""
    if kind in ALERT_SETTING and not enabled(db, ALERT_SETTING[kind]):
        return
    if db.scalar(select(PendingAlert).where(PendingAlert.kind == kind, PendingAlert.key == key)):
        return
    db.add(PendingAlert(kind=kind, key=key[:96], line=line, urgent=urgent))
    db.flush()


def flush_alerts(db: Session) -> bool:
    pending = db.scalars(select(PendingAlert).where(PendingAlert.urgent.is_(True), PendingAlert.sent_at.is_(None))
                         .order_by(PendingAlert.created_at)).all()
    if not pending:
        return False
    last = _last_sent(db, "alerts")
    if last and utcnow() - last < ALERT_GAP:
        return False
    body = "Something needs a look:\n\n" + "\n\n".join(a.line for a in pending)
    subject = pending[0].line.split("\n")[0][:90] if len(pending) == 1 else f"{len(pending)} problems need a look"
    if not _send(db, "alerts", utcnow().isoformat(timespec="minutes"), f"CGW Studio: {subject}", body):
        return False
    for a in pending:
        a.sent_at = utcnow()
    db.commit()
    return True


def publish_failures(db: Session, failed: list[ChannelVersion]) -> None:
    for v in failed:
        line = (f"Publishing failed: {v.post.display_title} on {ch.CHANNELS[v.channel].label}\n"
                f"  Error: {v.last_error}\n  Retry: {settings().base_url}/posts/{v.post_id}")
        alert(db, "publish_failed", f"{v.id}:{v.attempts}", line, urgent=True)
    db.commit()


def announcement_sent(db: Session, ann) -> bool:
    """Shown in the app; no email."""
    return False


def announcement_failed(db: Session, ann) -> bool:
    line = (f"Monthly email not sent: \"{ann.subject}\" (attempt {ann.attempts} of 3)\n  Error: {ann.last_error}\n"
            f"  {settings().base_url}/announcements/{ann.id}")
    alert(db, "announcement_failed", f"{ann.id}:{ann.attempts}", line, urgent=True)
    db.commit()
    return True


# --- running low ---------------------------------------------------------


def runway_message(db: Session) -> tuple[str, str]:
    left = runway(db)
    s = settings()
    if not left:
        subject, first = "CGW Studio: no posts left scheduled", "Nothing approved is waiting to go out."
    else:
        post, when = left[0]
        subject = "CGW Studio: only 1 post left scheduled"
        first = f"The last approved post is \"{post.display_title}\", going out {_when(when)}."
    waiting = db.scalars(select(Post).where(Post.status.in_(("in_review", "draft")))).all()
    queued = claude_queue(db)
    lines = [first]
    if waiting:
        lines.append(f"{len(waiting)} more are drafted but not approved yet: {s.base_url}/review")
    if queued:
        lines.append(f"{len(queued)} items are waiting for Claude to draft them.")
    lines.append(f"To plan the next {PLAN_WEEKS} weeks in one go, open the social-media folder in Claude Code "
                 "(or the Code tab in Claude Desktop) and run /cgw-session, then approve what it drafts on the "
                 "Review page.")
    lines.append(f"This is the only email about it: one now, then one a week while {RUNWAY_LOW} or fewer posts "
                 f"are left. It stops once there are more than {RUNWAY_LOW} again.")
    return subject, "\n\n".join(lines)


def runway_email(db: Session, force_to: list[str] | None = None) -> bool:
    """The running-low email: once when the runway drops to RUNWAY_LOW approved posts or fewer, then once a
    week while it stays that low. A row in the reminder log remembers that we're in a low stretch."""
    if force_to:
        subject, body = runway_message(db)
        mailer.send(force_to, "[TEST] " + subject, body + _footer())
        return True
    if not enabled(db, "runway"):
        return False
    state = db.scalar(select(ReminderLog).where(ReminderLog.kind == "runway_state", ReminderLog.key == "low"))
    if len(runway(db)) > RUNWAY_LOW:
        if state is not None:  # recovered: the next dip emails right away
            db.delete(state)
            db.commit()
        return False
    now = utcnow()
    if state is not None and now - state.sent_at < RUNWAY_REPEAT:
        return False
    subject, body = runway_message(db)
    try:
        mailer.send(recipients(db), subject, body + _footer())
    except Exception:
        log.exception("sending the running-low email failed")
        db.rollback()
        return False
    if state is None:
        db.add(ReminderLog(kind="runway_state", key="low", sent_at=now))
    else:
        state.sent_at = now
    db.commit()
    return True


# --- the loop ------------------------------------------------------------


def run(db: Session) -> None:
    """Called every scheduler loop."""
    if quiet(db):
        return
    hour = local_now().hour
    if not (settings().reminder_hour <= hour < EVENING_HOUR):
        return
    flush_alerts(db)
    runway_email(db)


run_daily = run  # older name
