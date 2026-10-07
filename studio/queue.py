"""The work only Claude can do, and the schedule overview Claude plans against."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import channels as ch
from .db import utcnow
from .models import ChannelVersion, MediaAsset, Post
from .timeutil import local_now, to_local


def claude_queue(db: Session) -> list[Post]:
    """Posts waiting for a Claude session, oldest first."""
    return db.scalars(select(Post).where(Post.status == "needs_claude").order_by(Post.created_at)).all()


def queue_summary(db: Session) -> dict:
    items = claude_queue(db)
    oldest = items[0].created_at if items else None
    return {
        "count": len(items),
        "oldest_days": (utcnow() - oldest).days if oldest else 0,
        "media_count": sum(len(p.media_links) for p in items),
        "minutes": max(5, round(len(items) * 1.5)) if items else 0,
    }


def schedule(db: Session, days: int = 21) -> dict:
    """Enabled versions of in-review/approved posts over the next `days`, grouped by local date."""
    now = utcnow()
    end = now + timedelta(days=days)
    rows = db.execute(
        select(ChannelVersion, Post)
        .join(Post)
        .where(
            ChannelVersion.enabled.is_(True),
            ChannelVersion.scheduled_at.is_not(None),
            ChannelVersion.scheduled_at >= now - timedelta(days=1),
            ChannelVersion.scheduled_at < end,
            Post.status.in_(("draft", "in_review", "approved", "done")),
        )
        .order_by(ChannelVersion.scheduled_at)
    ).all()
    by_day: dict[str, list[dict]] = {}
    for version, post in rows:
        local = to_local(version.scheduled_at)
        by_day.setdefault(local.date().isoformat(), []).append(
            {
                "post_id": post.id,
                "title": post.display_title,
                "pillar": post.pillar,
                "status": post.status,
                "channel": version.channel,
                "time": local.strftime("%H:%M"),
            }
        )
    today = local_now().date()
    all_days = [(today + timedelta(days=i)).isoformat() for i in range(days)]
    empty = [d for d in all_days if d not in by_day]
    return {"days": by_day, "empty_days": empty}


def unused_media(db: Session, limit: int = 30) -> list[MediaAsset]:
    """Ready media that isn't attached to any post that went out or is on its way."""
    from .models import PostMedia

    used = select(PostMedia.media_id).join(Post).where(Post.status.in_(("in_review", "approved", "done")))
    return db.scalars(
        select(MediaAsset)
        .where(MediaAsset.processing_status == "ready", MediaAsset.id.not_in(used))
        .order_by(MediaAsset.created_at.desc())
        .limit(limit)
    ).all()


def channel_rules_text() -> str:
    lines = []
    for c in ch.CHANNELS.values():
        accepts = ", ".join(k for k, ok in (("images", c.images), ("video", c.video), ("text only", c.text_only)) if ok)
        how = {"direct": "published automatically", "batch": "scheduled by hand on batch day", "on_day": "posted by hand on the day"}[c.mode]
        extra = f" Title up to {c.title_chars} chars." if c.title_chars else ""
        lines.append(
            f"- {c.key} ({c.label}): {how}; accepts {accepts}; caption up to {c.max_chars} chars incl. hashtags"
            f"{'; up to ' + str(c.max_images) + ' images' if c.images else ''}.{extra} {c.link_note} {c.tips}".rstrip()
        )
    return "\n".join(lines)
