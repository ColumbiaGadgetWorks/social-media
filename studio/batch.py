"""Batch days: approved posts for channels without a usable API, ready to schedule by hand."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import channels as ch
from .db import utcnow
from .media import abs_path
from .models import ChannelVersion, Post
from .posts import is_publishable
from .timeutil import cycle_start, local_now, to_local

CYCLE_WINDOW_DAYS = 14


@dataclass
class BatchItem:
    number: int
    version: ChannelVersion
    post: Post
    approved: bool
    reason: str
    overdue: bool
    within_horizon: bool

    @property
    def stem(self) -> str:
        local = to_local(self.version.scheduled_at)
        slug = re.sub(r"[^a-z0-9]+", "-", self.post.display_title.lower()).strip("-")[:40] or f"post-{self.post.id}"
        return f"{self.number:02d}_{local:%Y-%m-%d_%H%M}_{slug}"


def items(db: Session, channel_key: str) -> list[BatchItem]:
    channel = ch.CHANNELS[channel_key]
    now = utcnow()
    end = now + timedelta(days=CYCLE_WINDOW_DAYS)
    horizon_end = now + timedelta(days=channel.horizon_days) if channel.horizon_days else None
    versions = db.scalars(
        select(ChannelVersion)
        .join(Post)
        .where(
            ChannelVersion.channel == channel_key,
            ChannelVersion.enabled.is_(True),
            ChannelVersion.publish_state == "pending",
            ChannelVersion.scheduled_at < end,
            Post.status == "approved",
        )
        .order_by(ChannelVersion.scheduled_at)
    ).all()
    out = []
    for i, v in enumerate(versions, 1):
        ok, reason = is_publishable(v.post)
        out.append(
            BatchItem(
                number=i, version=v, post=v.post, approved=ok, reason=reason,
                overdue=v.scheduled_at < now,
                within_horizon=horizon_end is None or v.scheduled_at <= horizon_end,
            )
        )
    return out


def overview(db: Session) -> list[dict]:
    rows = []
    for c in ch.CHANNELS.values():
        if c.mode not in ch.MANUAL_MODES:
            continue
        found = items(db, c.key)
        rows.append({"channel": c, "count": len(found), "overdue": sum(1 for i in found if i.overdue)})
    return rows


def cycle_info() -> dict:
    today = local_now().date()
    start = cycle_start(today)
    return {"start": start, "next": start + timedelta(days=14), "is_batch_day": start == today}


def export_image(rel: str) -> bytes:
    """Re-encode photos so phone metadata (GPS location, device) never leaves the Studio."""
    with Image.open(abs_path(rel)) as raw:
        img = ImageOps.exif_transpose(raw)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def build_zip(db: Session, channel_key: str) -> tuple[bytes, int]:
    """Zip of the approved items on a channel's batch page. Unapproved items are left out."""
    channel = ch.CHANNELS[channel_key]
    ready = [i for i in items(db, channel_key) if i.approved]
    buf = io.BytesIO()
    lines = [f"{channel.label} batch, exported {local_now():%Y-%m-%d %H:%M}", ""]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for item in ready:
            v = item.version
            media = item.post.media
            for idx, m in enumerate(media):
                letter = chr(ord("a") + idx) if len(media) > 1 else ""
                name = f"{item.stem}{letter}"
                if m.kind == "image":
                    zf.writestr(f"{name}.jpg", export_image(m.path))
                else:
                    zf.write(abs_path(m.path), f"{name}{Path(m.path).suffix}")
            lines += [
                f"=== {item.number:02d}  {to_local(v.scheduled_at):%a %b %d, %I:%M %p}  ({item.post.display_title})",
            ]
            if v.title:
                lines += [f"TITLE: {v.title}"]
            lines += [v.full_text, ""]
        zf.writestr("captions.txt", "\n".join(lines))
    return buf.getvalue(), len(ready)
