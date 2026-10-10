"""Reminder emails, kept rare on purpose.

- One weekly digest (Monday morning by default), only when something needs a person: posts to
  approve this week with their deadlines, a Claude session when work is due soon, batch day, the
  Google Business Profile post, empty days, the monthly email, Hack Night photos, event changes.
- A "last call" when an unapproved post goes out within 48 hours: at most one every 3 days, and
  each post gets at most one.
- Problem alerts (a post failed to publish, the monthly email failed, an announced event was
  cancelled or changed), bundled into at most one email a day.
- Nothing in the first 24 hours after the Studio starts on a new database.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import batch, mailer, timeutil
from . import channels as ch
from .db import settings
from .models import ChannelVersion, PendingAlert, Post, ReminderLog, User
from .queue import approved_in_next, claude_queue, gaps
from .timeutil import local_now, to_local


def utcnow():
    return timeutil.utcnow()  # looked up each call, so tests can move the clock

log = logging.getLogger(__name__)

QUIET_START = timedelta(hours=24)
LAST_CALL_WINDOW = timedelta(hours=48)
LAST_CALL_GAP = timedelta(days=3)
ALERT_GAP = timedelta(hours=23)
DIGEST_DAYS = 8  # what the digest looks at
EVENING_HOUR = 21  # no reminder email after this local hour


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
    return (f"\n\n-- \nCGW Content Studio\n{settings().base_url}\n"
            "You get one digest a week, a last call when something goes out within two days unapproved, "
            "and an alert if something breaks.")


def started_at(db: Session) -> datetime:
    """When this database first ran the reminder loop (a fresh install, or the upgrade to digests)."""
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


def _day(dt: datetime) -> str:
    local = to_local(dt)
    return f"{local:%a %b} {local.day}"


# --- problem alerts ------------------------------------------------------


def alert(db: Session, kind: str, key: str, line: str, urgent: bool) -> None:
    """Queue something to tell people. Urgent: next alert email (max one a day). Otherwise: the digest."""
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


def event_changed(db: Session, event, posts, changes: list[str]) -> bool:
    if not posts:
        return False
    soon = utcnow() + timedelta(days=3)
    urgent = any(v.publish_state == "published" or (v.enabled and v.scheduled_at and v.scheduled_at < soon)
                 for p in posts for v in p.versions)
    lines = "\n".join(f"  - {p.display_title} ({p.status.replace('_', ' ')}): {settings().base_url}/posts/{p.id}"
                      for p in posts)
    line = (f"Event changed: \"{event.title}\" ({', '.join(changes)}). Posts about it need another look; "
            f"any approval was cleared:\n{lines}")
    alert(db, "event_changed", f"{event.id}:{event.facts_hash[:12]}", line, urgent=urgent)
    return True


def event_cancelled(db: Session, event, pulled, announced: bool) -> bool:
    line = f"Event cancelled: \"{event.title}\" was cancelled or removed from the calendar."
    if pulled:
        line += " These posts were pulled and won't publish:\n" + "\n".join(
            f"  - {p.display_title}: {settings().base_url}/posts/{p.id}" for p in pulled)
    if announced:
        line += ("\n  It was already announced, so a cancellation notice is queued for the next Claude session. "
                 "Also delete any copies already scheduled on batch-day platforms.")
    urgent = announced or any(p.approved_at for p in pulled)
    alert(db, "event_cancelled", str(event.id), line, urgent=urgent)
    return True


def announcement_sent(db: Session, ann) -> bool:
    """Shown in the app; no email."""
    return False


def announcement_failed(db: Session, ann) -> bool:
    line = (f"Monthly email not sent: \"{ann.subject}\" (attempt {ann.attempts} of 3)\n  Error: {ann.last_error}\n"
            f"  {settings().base_url}/announcements/{ann.id}")
    alert(db, "announcement_failed", f"{ann.id}:{ann.attempts}", line, urgent=True)
    db.commit()
    return True


# --- weekly digest -------------------------------------------------------


def _first_slot(post: Post) -> datetime | None:
    times = [v.scheduled_at for v in post.versions if v.enabled and v.scheduled_at and v.publish_state == "pending"]
    return min(times) if times else None


def digest(db: Session) -> tuple[str, str]:
    """(subject, body) of the weekly email; an empty subject means there's nothing to say."""
    from .models import Announcement, Event

    s = settings()
    now = utcnow()
    horizon = now + timedelta(days=DIGEST_DAYS)
    sections: list[str] = []
    subject_bits: list[str] = []

    # Posts to approve this week, with a deadline the day before each goes out.
    waiting = db.scalars(select(Post).where(Post.status.in_(("in_review", "draft")))).all()
    dated = sorted(((p, _first_slot(p)) for p in waiting), key=lambda x: x[1] or horizon + timedelta(days=999))
    soon = [(p, t) for p, t in dated if t and t < horizon]
    later = len(dated) - len(soon)
    if soon:
        lines = [f"- Approve by {_day(t - timedelta(days=1)) if t - now > timedelta(days=1) else 'today'}: "
                 f"{p.display_title} ({', '.join(ch.CHANNELS[v.channel].label for v in p.enabled_versions)}), "
                 f"goes out {_when(t)}" for p, t in soon]
        more = f"\n{later} more in review for later dates." if later else ""
        sections.append("TO APPROVE THIS WEEK\n" + "\n".join(lines) + more + f"\nReview: {s.base_url}/review")
        subject_bits.append(f"{len(soon)} to approve")
    emails = db.scalars(select(Announcement).where(Announcement.status == "in_review")).all()
    if emails:
        sections.append("MONTHLY EMAIL\n" + "\n".join(
            f"- Approve \"{a.subject}\" before it sends {_when(a.send_at)}: {s.base_url}/announcements/{a.id}" for a in emails))
        subject_bits.append("monthly email to approve")

    # A Claude session, only when the work is due soon or uploads have waited a while.
    queued = claude_queue(db)
    due = [p for p in queued if (p.target_at and p.target_at < now + timedelta(days=10))
           or (not p.target_at and now - p.created_at >= timedelta(days=s.claude_max_age_days))]
    drafts = db.scalars(select(Announcement).where(Announcement.status == "needs_claude")).all()
    if due or drafts:
        minutes = max(5, round(len(queued) * 1.5) + 5 * len(drafts))
        what = f"{len(queued)} item(s) waiting" + (", including the monthly email draft" if drafts else "")
        sections.append(f"CLAUDE SESSION (~{minutes} min)\n{what}; {len(due)} due in the next 10 days.\n"
                        "In Claude Code (or the Code tab in Claude Desktop), open the social-media folder and run "
                        "/cgw-session.")
        subject_bits.append(f"Claude session ~{minutes} min")

    # Batch day this week.
    info = batch.cycle_info()
    today = local_now().date()
    next_batch = info["start"] if info["start"] >= today else info["next"]
    if (next_batch - today).days < 7:
        rows = [r for r in batch.overview(db) if r["count"]]
        if rows:
            lines = [f"- {r['channel'].label}: {r['count']} post(s): {s.base_url}/batch/{r['channel'].key}" for r in rows]
            sections.append(f"BATCH DAY {next_batch:%a %b} {next_batch.day}\nSchedule these in each platform's own "
                            "scheduler (the browser extension fills the forms):\n" + "\n".join(lines))
            subject_bits.append("batch day")

    # Posts that have to go up by hand on the day (Google Business Profile).
    on_day = [c.key for c in ch.CHANNELS.values() if ch.mode(c) == "on_day"]
    by_hand = db.scalars(select(ChannelVersion).join(Post).where(
        ChannelVersion.channel.in_(on_day), ChannelVersion.enabled.is_(True), ChannelVersion.publish_state == "pending",
        Post.status == "approved", ChannelVersion.scheduled_at >= now, ChannelVersion.scheduled_at < horizon)).all()
    if by_hand:
        sections.append("POST BY HAND\n" + "\n".join(
            f"- {_day(v.scheduled_at)}: {ch.CHANNELS[v.channel].label}: {v.post.display_title}: {s.base_url}/batch/{v.channel}"
            for v in by_hand))

    # Empty days, only when the next week is thin.
    if approved_in_next(db, 7) < 2:
        found = gaps(db, weeks=2)
        lines = [f"- Week of {w['week_of']}: {w['planned']} of {w['target']} posts planned" for w in found["weeks"] if w["missing"]]
        if found["gbp_this_cycle"] < found["gbp_target"]:
            lines.append("- Google Business Profile: no post yet this cycle")
        if lines:
            sections.append("SCHEDULE GAPS\n" + "\n".join(lines) + "\nRun /cgw-plan for ideas from unused photos.")

    # Photos at this week's weekly events.
    if s.photo_nudges:
        weekly = db.scalars(select(Event).where(Event.series != "", Event.promote.is_(True), Event.status == "active",
                                                Event.start > now, Event.start < now + timedelta(days=7))
                            .order_by(Event.start)).all()
        if weekly:
            names = ", ".join(f"{e.title} {_day(e.start)}" for e in weekly)
            sections.append(f"PHOTOS\n{names}: grab 3-5 photos or a short clip (projects, people at the tools, "
                            "anything funny; ask before photographing faces). Upload with \"Taken at\" set, or drop "
                            "them in the Discord uploads channel. Next week's post uses them.")

    # Event changes and cancellations that aren't urgent.
    notes = db.scalars(select(PendingAlert).where(PendingAlert.urgent.is_(False), PendingAlert.sent_at.is_(None))
                       .order_by(PendingAlert.created_at)).all()
    if notes:
        sections.append("CALENDAR CHANGES\n" + "\n\n".join(n.line for n in notes))

    needs_you = bool(subject_bits) or bool(notes) or any(x.startswith(("SCHEDULE GAPS", "POST BY HAND")) for x in sections)
    if not needs_you:
        return "", ""
    subject = "CGW Studio this week: " + (", ".join(subject_bits) if subject_bits else "a few things to check")
    return subject, "\n\n".join(sections)


def weekly_digest(db: Session, force_to: list[str] | None = None) -> bool:
    subject, body = digest(db)
    if not subject:
        return False
    if force_to:
        mailer.send(force_to, "[TEST] " + subject, body + _footer())
        return True
    if not _send(db, "digest", local_now().date().isoformat(), subject, body):
        return False
    for n in db.scalars(select(PendingAlert).where(PendingAlert.urgent.is_(False), PendingAlert.sent_at.is_(None))):
        n.sent_at = utcnow()
    db.commit()
    return True


def next_digest_date():
    s = settings()
    now = local_now()
    days = (s.digest_day - now.weekday()) % 7
    if days == 0 and now.hour >= s.reminder_hour:
        days = 7
    return (now + timedelta(days=days)).date()


# --- last call -----------------------------------------------------------


def last_call(db: Session) -> bool:
    """Unapproved posts that go out (or should) within 48 hours. Max one email every 3 days; one per post."""
    if not settings().last_call_alerts:
        return False
    last = _last_sent(db, "last_call")
    if last and utcnow() - last < LAST_CALL_GAP:
        return False
    now = utcnow()
    cutoff = now + LAST_CALL_WINDOW
    found = []
    for p in db.scalars(select(Post).where(Post.status.in_(("needs_claude", "draft", "in_review")))).all():
        when = _first_slot(p) or p.target_at
        if when and now < when < cutoff:
            seen = db.scalar(select(ReminderLog).where(ReminderLog.kind == "last_call_post", ReminderLog.key == str(p.id)))
            if not seen:
                found.append((p, when))
    if not found:
        return False
    s = settings()
    lines = []
    for p, when in sorted(found, key=lambda x: x[1]):
        state = "still needs drafting (run /cgw-session)" if p.status == "needs_claude" else "not approved yet"
        lines.append(f"- {p.display_title}: goes out {_when(when)}, {state}: {s.base_url}/posts/{p.id}")
    body = ("These go out within two days but aren't approved, so they won't publish:\n\n" + "\n".join(lines)
            + f"\n\nReview: {s.base_url}/review")
    if not _send(db, "last_call", now.isoformat(timespec="minutes"), f"Last call: {len(found)} post(s) go out within 2 days", body):
        return False
    for p, _ in found:
        _once(db, "last_call_post", str(p.id))
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
    if local_now().weekday() == settings().digest_day:
        weekly_digest(db)
    last_call(db)


run_daily = run  # older name
