"""Reminder emails: when a Claude session is due, approvals are waiting, or it's batch day."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import batch, mailer
from . import channels as ch
from .db import settings
from .models import ChannelVersion, Post, ReminderLog, User
from .queue import approved_in_next, gaps, queue_summary
from .timeutil import local_day_bounds_utc, local_now, to_local

log = logging.getLogger(__name__)


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
    db.add(ReminderLog(kind=kind, key=key))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    return True


def _send(db: Session, kind: str, key: str, subject: str, body: str) -> bool:
    if not _once(db, kind, key):
        return False
    to = recipients(db)
    try:
        mailer.send(to, subject, body + _footer())
    except Exception:
        log.exception("sending %s reminder failed", kind)
        db.rollback()  # try again next loop
        return False
    db.commit()
    return True


def _footer() -> str:
    return f"\n\n-- \nCGW Content Studio\n{settings().base_url}\n"


def claude_session(db: Session, force: bool = False) -> bool:
    s = settings()
    summary = queue_summary(db)
    due = summary["count"] >= s.claude_queue_threshold or (
        summary["count"] > 0 and summary["oldest_days"] >= s.claude_max_age_days
    )
    if not (due or (force and summary["count"])):
        return False
    today = local_now().date().isoformat()
    body = (
        f"{summary['count']} item(s) are waiting for Claude ({summary['media_count']} photo/video files"
        f"{', ' + str(summary['emails']) + ' email draft(s)' if summary.get('emails') else ''}). "
        f"The oldest has waited {summary['oldest_days']} day(s).\n"
        f"Estimated time: about {summary['minutes']} minutes.\n\n"
        "On your computer, open Claude Code in the social-media repo and run:\n\n"
        "    /cgw-session\n\n"
        f"Then review the drafts at {s.base_url}/review"
    )
    return _send(db, "claude_session", today, f"Claude session needed: {summary['count']} items, ~{summary['minutes']} min", body)


def approvals(db: Session) -> bool:
    from .models import Announcement

    waiting = db.scalars(select(Post).where(Post.status == "in_review").order_by(Post.submitted_at)).all()
    emails = db.scalars(select(Announcement).where(Announcement.status == "in_review")).all()
    if not waiting and not emails:
        return False
    lines = [f"- {p.display_title} ({', '.join(ch.CHANNELS[v.channel].label for v in p.enabled_versions)})" for p in waiting[:20]]
    lines += [f"- EMAIL: {a.subject} (sends {to_local(a.send_at):%a %b %-d, %-I:%M %p}) -> {settings().base_url}/announcements/{a.id}"
              for a in emails]
    total = len(waiting) + len(emails)
    body = f"{total} item(s) are waiting for your approval:\n\n" + "\n".join(lines) + f"\n\nReview: {settings().base_url}/review"
    return _send(db, "approvals", local_now().date().isoformat(), f"{total} item(s) waiting for approval", body)


def batch_day(db: Session) -> bool:
    info = batch.cycle_info()
    if not info["is_batch_day"]:
        return False
    rows = [r for r in batch.overview(db) if r["count"]]
    if not rows:
        return False
    counts = ", ".join(f"{r['channel'].label} {r['count']}" for r in rows)
    total = sum(r["count"] for r in rows)
    lines = [f"- {r['channel'].label}: {r['count']} post(s) -> {settings().base_url}/batch/{r['channel'].key}" for r in rows]
    body = (
        "It's batch day. Schedule the next two weeks in each platform's own scheduler.\n\n"
        + "\n".join(lines)
        + f"\n\nAbout {total * 2} minutes by hand. TikTok only accepts posts up to 10 days ahead; "
        "the rest wait for the next batch."
    )
    return _send(db, "batch_day", info["start"].isoformat(), f"Batch day: {counts}", body)


def on_day_posts(db: Session) -> bool:
    """Channels that can't schedule ahead (Google Business Profile): remind on the day."""
    start, end = local_day_bounds_utc(local_now().date())
    keys = [c.key for c in ch.CHANNELS.values() if ch.mode(c) == "on_day"]
    due = db.scalars(
        select(ChannelVersion).join(Post).where(
            ChannelVersion.channel.in_(keys), ChannelVersion.enabled.is_(True),
            ChannelVersion.publish_state == "pending", Post.status == "approved",
            ChannelVersion.scheduled_at >= start, ChannelVersion.scheduled_at < end,
        )
    ).all()
    if not due:
        return False
    lines = [f"- {ch.CHANNELS[v.channel].label} at {to_local(v.scheduled_at):%I:%M %p}: {v.post.display_title} -> {settings().base_url}/batch/{v.channel}" for v in due]
    body = "Post these by hand today, then tap \"Mark posted\":\n\n" + "\n".join(lines)
    return _send(db, "on_day", local_now().date().isoformat(), f"Post today: {len(due)} item(s)", body)


def publish_failures(db: Session, failed: list[ChannelVersion]) -> None:
    for v in failed:
        body = (
            f"{ch.CHANNELS[v.channel].label} didn't publish \"{v.post.display_title}\".\n\n"
            f"Error: {v.last_error}\n\nOpen the post to retry: {settings().base_url}/posts/{v.post_id}"
        )
        _send(db, "publish_failed", f"{v.id}:{v.attempts}:{v.last_error[:20]}", f"Publishing failed: {v.post.display_title}", body)


def schedule_dry(db: Session) -> bool:
    """Fewer than two posts lined up for the next week: ask for a planning session."""
    if approved_in_next(db, 7) >= 2:
        return False
    found = gaps(db, weeks=2)
    lines = [f"- Week of {w['week_of']}: {w['planned']} of {w['target']} posts planned" for w in found["weeks"]]
    if found["gbp_this_cycle"] < found["gbp_target"]:
        lines.append("- Google Business Profile: no post yet this cycle")
    body = (
        "The schedule is running dry: fewer than two posts are approved or in review for the next 7 days.\n\n"
        + "\n".join(lines)
        + "\n\nIn Claude Code, run:\n\n    /cgw-plan\n\nClaude will suggest posts from unused media and upcoming events."
    )
    year, week, _ = local_now().isocalendar()
    return _send(db, "schedule_dry", f"{year}-W{week}", "Schedule running dry: run /cgw-plan", body)


def photo_nudge(db: Session) -> bool:
    """On a weekly event's day: ask for a few photos, so next week's post has something real."""
    from datetime import timedelta

    from .db import utcnow
    from .models import Event

    if not settings().photo_nudges:
        return False
    now = utcnow()
    today_end = local_day_bounds_utc(local_now().date())[1]
    events = db.scalars(select(Event).where(Event.series != "", Event.promote.is_(True), Event.status == "active",
                                            Event.start > now, Event.start < min(today_end, now + timedelta(hours=12)))).all()
    sent = False
    for e in events:
        start = to_local(e.start)
        body = (
            f"{e.title} is tonight at {start:%I:%M %p}".replace(" at 0", " at ") + ".\n\n"
            "Grab 3-5 photos or a short clip while you're there: a project in progress, something that works for "
            "the first time, people at the tools, anything funny. Ask before photographing someone's face.\n\n"
            f"Upload them at {settings().base_url}/upload and choose \"Taken at: {e.title}\". Next week's "
            "post will use them instead of the plain event card. A one-line note helps (\"Sam's first laser "
            "cut\", \"fixed a 1970s lamp\")."
        )
        sent = _send(db, "photo_nudge", str(e.id), f"{e.title} tonight: grab a few photos", body) or sent
    return sent


def run_daily(db: Session) -> None:
    if local_now().hour < settings().reminder_hour:
        return
    photo_nudge(db)
    claude_session(db)
    schedule_dry(db)
    approvals(db)
    batch_day(db)
    on_day_posts(db)



def event_changed(db: Session, event, posts, changes: list[str]) -> bool:
    if not posts:
        return False
    lines = [f"- {p.display_title} ({p.status.replace('_', ' ')}): {settings().base_url}/posts/{p.id}" for p in posts]
    body = (f"\"{event.title}\" changed in the calendar ({', '.join(changes)}). Posts about it need another look; "
            "any approval was cleared:\n\n" + "\n".join(lines))
    return _send(db, "event_changed", f"{event.id}:{event.facts_hash[:12]}", f"Event changed: {event.title}", body)


def event_cancelled(db: Session, event, pulled, announced: bool) -> bool:
    lines = [f"- {p.display_title}: {settings().base_url}/posts/{p.id}" for p in pulled]
    body = f"\"{event.title}\" was cancelled or removed from the calendar."
    if lines:
        body += " These posts were pulled and won't publish:\n\n" + "\n".join(lines)
    if announced:
        body += ("\n\nIt had already been announced, so a cancellation notice is queued for the next Claude "
                 "session. Also delete any copies already scheduled on batch-day platforms.")
    return _send(db, "event_cancelled", str(event.id), f"Event cancelled: {event.title}", body)


def announcement_sent(db: Session, ann) -> bool:
    body = f"\"{ann.subject}\" went to {ann.recipient_count} people.\n\n{settings().base_url}/announcements/{ann.id}"
    return _send(db, "announcement_sent", str(ann.id), f"Email sent: {ann.subject}", body)


def announcement_failed(db: Session, ann) -> bool:
    body = (f"\"{ann.subject}\" couldn't be sent (attempt {ann.attempts} of 3).\n\nError: {ann.last_error}\n\n"
            f"{settings().base_url}/announcements/{ann.id}")
    return _send(db, "announcement_failed", f"{ann.id}:{ann.attempts}", f"Email not sent: {ann.subject}", body)
