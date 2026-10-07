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
from .queue import queue_summary
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
        f"{summary['count']} post(s) are waiting for Claude ({summary['media_count']} photo/video files). "
        f"The oldest has waited {summary['oldest_days']} day(s).\n"
        f"Estimated time: about {summary['minutes']} minutes.\n\n"
        "On your computer, open Claude Code in the social-media repo and run:\n\n"
        "    /cgw-session\n\n"
        f"Then review the drafts at {s.base_url}/review"
    )
    return _send(db, "claude_session", today, f"Claude session needed: {summary['count']} items, ~{summary['minutes']} min", body)


def approvals(db: Session) -> bool:
    waiting = db.scalars(select(Post).where(Post.status == "in_review").order_by(Post.submitted_at)).all()
    if not waiting:
        return False
    lines = [f"- {p.display_title} ({', '.join(ch.CHANNELS[v.channel].label for v in p.enabled_versions)})" for p in waiting[:20]]
    body = f"{len(waiting)} post(s) are waiting for your approval:\n\n" + "\n".join(lines) + f"\n\nReview: {settings().base_url}/review"
    return _send(db, "approvals", local_now().date().isoformat(), f"{len(waiting)} post(s) waiting for approval", body)


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
    keys = [c.key for c in ch.CHANNELS.values() if c.mode == "on_day"]
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


def run_daily(db: Session) -> None:
    if local_now().hour < settings().reminder_hour:
        return
    claude_session(db)
    approvals(db)
    batch_day(db)
    on_day_posts(db)

