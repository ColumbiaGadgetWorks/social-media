"""Post lifecycle: editing, review, approval, and the checks every publisher runs.

The approval rule lives here. A post is publishable only when its status is
"approved" AND the hash of its current content equals the hash recorded when a
human approver approved it. Any edit after approval clears the approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from . import channels as ch
from .db import utcnow
from .models import ChannelVersion, Post
from .security import Actor, audit

EDITABLE_FIELDS = ("title", "body", "hashtags", "enabled", "scheduled_at")


class PostError(Exception):
    pass


def media_kinds(post: Post) -> set[str]:
    return {m.kind for m in post.media}


def image_count(post: Post) -> int:
    return sum(1 for m in post.media if m.kind == "image")


def compatible_channels(post: Post) -> list[ch.Channel]:
    kinds = media_kinds(post)
    return [c for c in ch.CHANNELS.values() if ch.compatible(c, kinds)]


def ensure_versions(post: Post) -> None:
    """Give the post a (disabled) version for every channel its media fits."""
    have = {v.channel for v in post.versions}
    for channel in compatible_channels(post):
        if channel.key not in have:
            post.versions.append(ChannelVersion(channel=channel.key, enabled=False))


def approval_hash(post: Post) -> str:
    """Fingerprint of everything that gets published."""
    payload = {
        "post": post.id,
        "media": [m.sha256 for m in post.media],
        "versions": [
            {
                "channel": v.channel,
                "title": v.title.strip(),
                "body": v.body.strip(),
                "hashtags": v.hashtags.strip(),
                "scheduled_at": v.scheduled_at.isoformat() if v.scheduled_at else None,
            }
            for v in sorted(post.enabled_versions, key=lambda v: v.channel)
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def problems(post: Post) -> dict[str, list[str]]:
    """Everything that blocks review or approval, keyed by channel ('' for the post)."""
    out: dict[str, list[str]] = {}
    enabled = post.enabled_versions
    if not enabled:
        out[""] = ["no channels are selected"]
    if any(m.processing_status != "ready" for m in post.media):
        out.setdefault("", []).append("media is still processing")
    if any(m.kind == "video" for m in post.media) and len(post.media) > 1:
        out.setdefault("", []).append("a post with a video can't have other photos or videos; split it into separate posts")
    kinds, images = media_kinds(post), image_count(post)
    credits = [m.credit for m in post.media if m.credit]
    for v in enabled:
        channel = ch.CHANNELS.get(v.channel)
        if channel is None:
            out[v.channel] = ["unknown channel"]
            continue
        found = ch.validate(channel, body=v.body, hashtags=v.hashtags, title=v.title, kinds=kinds, image_count=images)
        for credit in credits:  # music licenses like CC BY need the credit in every caption
            if credit.lower() not in v.full_text.lower():
                found.append(f"add the music credit: {credit}")
        if v.scheduled_at is None:
            found.append("no date and time set")
        elif v.publish_state == "pending" and v.scheduled_at < utcnow() - timedelta(minutes=5):
            found.append("the date and time is in the past")
        if found:
            out[v.channel] = found
    return out


def is_publishable(post: Post) -> tuple[bool, str]:
    """The check every publisher and exporter runs before anything leaves the Studio."""
    if post.status != "approved":
        return False, f"post status is {post.status}, not approved"
    if not post.approved_hash or not post.approved_by_id:
        return False, "post has no recorded approval"
    if approval_hash(post) != post.approved_hash:
        return False, "post changed after it was approved"
    return True, ""


def _has_published(post: Post) -> bool:
    return any(v.publish_state == "published" for v in post.versions)


def update_post(
    db: Session,
    actor: Actor,
    post: Post,
    *,
    title: str | None = None,
    note: str | None = None,
    pillar: str | None = None,
    claude_notes: str | None = None,
    versions: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Apply edits. Returns the list of fields that changed."""
    if post.status == "done":
        raise PostError("This post is finished and can't be edited. Create a new post instead.")
    if actor.type == "mcp" and post.status == "approved":
        raise PostError("Approved posts can only be edited in the web app.")
    if _has_published(post):
        raise PostError("Part of this post is already published. Create a new post instead.")

    changed: list[str] = []
    for name, value in (("title", title), ("note", note), ("pillar", pillar), ("claude_notes", claude_notes)):
        if value is not None and getattr(post, name) != value:
            setattr(post, name, value)
            changed.append(name)

    for data in versions or []:
        key = data.get("channel")
        if key not in ch.CHANNELS:
            raise PostError(f"Unknown channel: {key}")
        version = post.version(key)
        if version is None:
            version = ChannelVersion(channel=key, enabled=False)
            post.versions.append(version)
        for field in EDITABLE_FIELDS:
            if field not in data:
                continue
            value = data[field]
            if value is None and field != "scheduled_at":  # None clears only the time
                continue
            if getattr(version, field) != value:
                setattr(version, field, value)
                changed.append(f"{key}.{field}")
        if version.enabled and not ch.compatible(ch.CHANNELS[key], media_kinds(post)):
            raise PostError(f"{ch.CHANNELS[key].label} can't take this post's media.")

    if not changed:
        return changed
    post.updated_at = utcnow()
    if post.status == "approved":
        post.status = "in_review"
        post.approved_hash = None
        post.approved_by_id = None
        post.approved_at = None
        audit(db, actor, "approval_cleared", "post", post.id, reason="edited after approval", fields=changed)
    elif post.status in ("needs_claude", "rejected"):
        post.status = "draft"
    audit(db, actor, "post_edited", "post", post.id, fields=changed)
    return changed


def submit_for_review(db: Session, actor: Actor, post: Post) -> None:
    if post.status not in ("needs_claude", "draft", "rejected"):
        raise PostError(f"A post that is {post.status} can't be submitted.")
    found = problems(post)
    if found:
        raise PostError(_describe(found))
    post.status = "in_review"
    post.submitted_at = utcnow()
    post.review_comment = ""
    audit(db, actor, "submitted", "post", post.id)


def approve(db: Session, actor: Actor, post: Post, seen_hash: str | None) -> None:
    """The only way a post becomes publishable. Humans with the Approver role only.

    `seen_hash` is the approval_hash of the version the approver was looking at, so a
    change made after they opened the page (by Claude or another person) can't slip through.
    """
    if not actor.is_human or not actor.user.has_role("approver"):
        raise PostError("Only a signed-in approver can approve posts.")
    if post.status != "in_review":
        raise PostError("Only posts in review can be approved.")
    found = problems(post)
    if found:
        raise PostError(_describe(found))
    current = approval_hash(post)
    if seen_hash != current:
        raise PostError("This post changed after you opened it. Review the new version and approve again.")
    post.approved_hash = current
    post.approved_by_id = actor.user.id
    post.approved_at = utcnow()
    post.status = "approved"
    audit(db, actor, "approved", "post", post.id, hash=post.approved_hash,
          channels=[v.channel for v in post.enabled_versions])


def request_changes(db: Session, actor: Actor, post: Post, comment: str, *, reject: bool = False) -> None:
    if not actor.is_human or not actor.user.has_role("approver"):
        raise PostError("Only an approver can send a post back.")
    if post.status not in ("in_review", "approved"):
        raise PostError("Only posts in review or approved can be sent back.")
    if _has_published(post):
        raise PostError("Part of this post is already published.")
    post.status = "rejected" if reject else "draft"
    post.review_comment = comment
    post.approved_hash = None
    post.approved_by_id = None
    post.approved_at = None
    audit(db, actor, "rejected" if reject else "changes_requested", "post", post.id, comment=comment)


def mark_posted(db: Session, actor: Actor, version: ChannelVersion, url: str = "") -> None:
    """A person scheduled or posted this version by hand (batch / on-day channels)."""
    if not actor.is_human:
        raise PostError("Only a person can mark a post as scheduled.")
    channel = ch.CHANNELS[version.channel]
    if ch.mode(channel) not in ch.MANUAL_MODES:
        raise PostError(f"{channel.label} is published automatically.")
    ok, reason = is_publishable(version.post)
    if not ok:
        raise PostError(f"Not approved: {reason}.")
    version.publish_state = "published"
    version.external_url = url.strip()
    version.published_at = utcnow()
    audit(db, actor, "marked_posted", "post", version.post_id, channel=version.channel, url=version.external_url)
    finish_if_complete(db, version.post)


def set_external_url(db: Session, actor: Actor, version: ChannelVersion, url: str) -> None:
    version.external_url = url.strip()
    audit(db, actor, "post_url_set", "post", version.post_id, channel=version.channel, url=version.external_url)


def skip_version(db: Session, actor: Actor, version: ChannelVersion) -> None:
    if not actor.is_human or not actor.user.has_role("editor"):
        raise PostError("Only an editor can skip a channel.")
    version.publish_state = "skipped"
    audit(db, actor, "channel_skipped", "post", version.post_id, channel=version.channel)
    finish_if_complete(db, version.post)


def finish_if_complete(db: Session, post: Post) -> None:
    if post.status == "approved" and all(v.publish_state in ("published", "skipped") for v in post.enabled_versions):
        post.status = "done"
        audit(db, Actor.system(), "post_done", "post", post.id)


def _describe(found: dict[str, list[str]]) -> str:
    parts = []
    for key, items in found.items():
        label = ch.CHANNELS[key].label if key in ch.CHANNELS else "Post"
        parts.append(f"{label}: {', '.join(items)}")
    return "Can't continue yet. " + "; ".join(parts) + "."


def create_post(
    db: Session,
    actor: Actor,
    media_ids: list[int],
    *,
    note: str = "",
    title: str = "",
    pillar: str = "",
    for_claude: bool = True,
    source: str = "upload",
) -> Post:
    from .models import MediaAsset, PostMedia

    post = Post(
        note=note.strip(), title=title.strip(), pillar=pillar,
        status="needs_claude" if for_claude else "draft", source=source,
        created_by_id=actor.user.id if actor.user else None,
    )
    for position, media_id in enumerate(media_ids):
        asset = db.get(MediaAsset, media_id)
        if asset is None:
            raise PostError(f"Media {media_id} doesn't exist.")
        post.media_links.append(PostMedia(media=asset, position=position))
    db.add(post)
    ensure_versions(post)
    db.flush()
    audit(db, actor, "post_created", "post", post.id, media=media_ids, status=post.status)
    return post


def send_to_claude(db: Session, actor: Actor, post: Post) -> None:
    if post.status not in ("draft", "rejected"):
        raise PostError("Only drafts can be sent to Claude.")
    post.status = "needs_claude"
    audit(db, actor, "sent_to_claude", "post", post.id)
