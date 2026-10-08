"""The MCP server Claude Code connects to (LAN only, personal token).

Claude can read the work queue and media, and write drafts. There is no tool to
approve, publish, schedule on a platform, or send anything: those happen only
in the web app, by a person.
"""

from __future__ import annotations

import contextvars
import json
import logging
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from . import calendar_sync, metrics, queue
from . import channels as ch
from . import posts as post_svc
from .config import ip_in
from .db import session_scope, settings
from .media import abs_path
from .models import MediaAsset, Post, PostMedia, User
from .security import Actor, audit, user_for_token
from .timeutil import fmt_local, input_value, parse_local
from .web import PILLARS

log = logging.getLogger(__name__)
GUIDELINES_DIR = Path(__file__).resolve().parent.parent / "guidelines"
MAX_IMAGES_PER_ITEM = 8

_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("mcp_user_id", default=None)

INSTRUCTIONS = """CGW Content Studio for Columbia Gadget Works, a nonprofit makerspace.
You draft social posts from members' photos and videos. A person approves every post in the
web app; you cannot approve, publish or send anything. Call get_guidelines first in each session."""

server = MCPServer("cgw-studio", instructions=INSTRUCTIONS)


class VersionDraft(BaseModel):
    channel: str = Field(description="Channel key, e.g. instagram, facebook, bluesky, tiktok")
    enabled: bool = Field(default=True, description="false removes this channel from the post")
    body: str = Field(default="", description="Caption without hashtags")
    hashtags: str = Field(default="", description="Hashtags, space separated")
    title: str = Field(default="", description="Only for channels with titles (youtube_shorts)")
    scheduled_at: str = Field(default="", description="Local time, 'YYYY-MM-DDTHH:MM' (America/Chicago)")


class MediaNote(BaseModel):
    media_id: int
    description: str = Field(default="", description="What the photo/video shows: tools, materials, technique")
    alt_text: str = Field(default="", description="Accessible description for screen readers, 1-2 sentences")
    tags: list[str] = Field(default_factory=list)


def _actor(db) -> Actor:
    user_id = _current_user_id.get()
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise PermissionError("Not authenticated")
    return Actor("mcp", user)


def _media_summary(m: MediaAsset) -> dict:
    return {
        "media_id": m.id, "kind": m.kind, "status": m.processing_status,
        "width": m.width, "height": m.height,
        "duration_s": round(m.duration_s, 1) if m.duration_s else None,
        "uploader_note": m.note, "description": m.description, "alt_text": m.alt_text, "tags": m.tags,
    }


def _post_summary(p: Post) -> dict:
    summary = {
        "post_id": p.id, "status": p.status, "title": p.title, "note": p.note, "pillar": p.pillar,
        "review_comment": p.review_comment,
        "media": [_media_summary(m) for m in p.media],
        "channels_available": [c.key for c in post_svc.compatible_channels(p)],
        "versions": [
            {"channel": v.channel, "enabled": v.enabled, "title": v.title, "body": v.body,
             "hashtags": v.hashtags, "scheduled_at": input_value(v.scheduled_at)}
            for v in p.versions if v.enabled or v.body
        ],
    }
    if p.event is not None:
        summary["event"] = calendar_sync.event_facts(p.event)
        summary["purpose"] = p.purpose
        summary["aim_for"] = input_value(p.target_at)
        summary["event_instructions"] = (
            "Facts (date, time, place, price, link) come only from `event`. Schedule close to `aim_for`. "
            "The attached event card shows the facts; swap in or add real photos with attach_media if any fit."
        )
    return summary


@server.tool()
def get_guidelines() -> str:
    """Brand voice, content pillars, channel rules and the session workflow. Read this first."""
    parts = []
    for path in sorted(GUIDELINES_DIR.glob("*.md")):
        parts.append(path.read_text())
    parts.append("## Channel rules (from the Studio)\n" + queue.channel_rules_text())
    parts.append("## Pillar keys\n" + "\n".join(f"- {k}: {label}" for k, label in PILLARS))
    parts.append(f"Timezone for scheduled_at: {settings().timezone.key}.")
    return "\n\n".join(parts)


@server.tool()
def get_work_queue() -> str:
    """Posts waiting for Claude (oldest first) plus a summary. Work through these in order."""
    with session_scope() as db:
        _actor(db)
        items = queue.claude_queue(db)
        return json.dumps({
            "summary": queue.queue_summary(db),
            "items": [
                {"post_id": p.id, "note": p.note, "pillar": p.pillar, "media_count": len(p.media_links),
                 "waiting_since": fmt_local(p.created_at), "review_comment": p.review_comment,
                 "media_ready": all(m.processing_status == "ready" for m in p.media)}
                for p in items
            ],
        }, indent=1)


@server.tool(structured_output=False)
def get_work_item(post_id: int) -> list:
    """One post with its media previews (photos at 768px, 5 frames per video) and current drafts."""
    with session_scope() as db:
        _actor(db)
        post = db.get(Post, post_id)
        if post is None:
            return [f"Post {post_id} doesn't exist."]
        content: list = [json.dumps(_post_summary(post), indent=1)]
        images = 0
        for m in post.media:
            paths = [m.preview_path] if m.kind == "image" else m.frames or [m.preview_path]
            for i, rel in enumerate(p for p in paths if p):
                if images >= MAX_IMAGES_PER_ITEM:
                    break
                label = f"media {m.id} ({m.kind})" + (f", frame {i + 1} of {len(paths)}" if m.kind == "video" else "")
                content.append(label)
                content.append(Image(path=abs_path(rel)))
                images += 1
        return content


@server.tool(structured_output=False)
def view_media(media_ids: list[int]) -> list:
    """Previews of specific library files (up to 6), e.g. to build a post from unused media."""
    with session_scope() as db:
        _actor(db)
        content: list = []
        for media_id in media_ids[:6]:
            m = db.get(MediaAsset, media_id)
            if m is None or not m.preview_path:
                content.append(f"media {media_id}: not available")
                continue
            content.append(json.dumps(_media_summary(m)))
            content.append(Image(path=abs_path(m.preview_path)))
        return content


@server.tool()
def search_media(query: str = "", kind: str = "", unused_only: bool = False, limit: int = 20) -> str:
    """Search the media library by uploader note, description, tags or file name."""
    with session_scope() as db:
        _actor(db)
        if unused_only:
            found = queue.unused_media(db, limit=limit)
            if query:
                q = query.lower()
                found = [m for m in found if q in (m.note + m.description + " ".join(m.tags)).lower()]
        else:
            stmt = select(MediaAsset).where(MediaAsset.processing_status == "ready").order_by(MediaAsset.created_at.desc()).limit(limit)
            if query:
                like = f"%{query}%"
                stmt = stmt.where(or_(MediaAsset.note.ilike(like), MediaAsset.description.ilike(like), MediaAsset.original_name.ilike(like)))
            if kind in ("image", "video"):
                stmt = stmt.where(MediaAsset.kind == kind)
            found = db.scalars(stmt).all()
        return json.dumps([_media_summary(m) for m in found], indent=1)


@server.tool()
def get_schedule(days: int = 21) -> str:
    """What's planned per day, which days are empty, weeks short of 3 main posts, and the GBP post for this cycle."""
    with session_scope() as db:
        _actor(db)
        data = queue.schedule(db, days=min(max(days, 1), 60))
        data["gaps"] = queue.gaps(db, weeks=3)
        return json.dumps(data, indent=1)


@server.tool()
def get_events(days: int = 45) -> str:
    """Upcoming calendar events, with whether the Studio made promo posts for each."""
    with session_scope() as db:
        _actor(db)
        events = calendar_sync.upcoming_events(db, days=min(max(days, 1), 120))
        return json.dumps([
            {**calendar_sync.event_facts(e), "promoted": e.promos_created,
             "promo_posts": [{"post_id": p.id, "purpose": p.purpose, "status": p.status} for p in e.posts]}
            for e in events
        ], indent=1)


@server.tool()
def get_metrics(days: int = 90) -> str:
    """Engagement for posts published in the last `days`, with averages by pillar and by channel."""
    with session_scope() as db:
        _actor(db)
        return json.dumps(metrics.summary(db, days=min(max(days, 7), 365)), indent=1, default=str)


@server.tool()
def attach_media(post_id: int, media_ids: list[int], replace: bool = False) -> str:
    """Add library photos/videos to a post that isn't approved yet (or replace its media with replace=true)."""
    with session_scope() as db:
        actor = _actor(db)
        post = db.get(Post, post_id)
        if post is None:
            return f"Error: post {post_id} doesn't exist."
        if post.status in ("approved", "done"):
            return "Error: approved posts can only be changed in the web app."
        assets = [db.get(MediaAsset, i) for i in media_ids]
        if any(a is None for a in assets):
            return "Error: one of those media ids doesn't exist."
        if replace:
            post.media_links.clear()
            db.flush()
        have = {link.media_id for link in post.media_links}
        for asset in assets:
            if asset.id not in have:
                post.media_links.append(PostMedia(media=asset, position=len(post.media_links)))
        post_svc.ensure_versions(post)
        for v in post.versions:  # drop channels that can no longer take this media
            if v.enabled and v.channel not in {c.key for c in post_svc.compatible_channels(post)}:
                v.enabled = False
        if post.status == "in_review":
            post.status = "draft"
        audit(db, actor, "media_changed", "post", post.id, media=[link.media_id for link in post.media_links])
        return json.dumps({"post_id": post.id, "media": [link.media_id for link in post.media_links],
                           "channels_available": [c.key for c in post_svc.compatible_channels(post)],
                           "status": post.status})


@server.tool()
def create_post(media_ids: list[int], note: str, pillar: str = "") -> str:
    """Start a new post from library media (e.g. unused photos to fill a schedule gap).
    Then fill it with submit_drafts."""
    with session_scope() as db:
        actor = _actor(db)
        try:
            post = post_svc.create_post(db, actor, media_ids, note=note, pillar=pillar, for_claude=False, source="claude")
        except post_svc.PostError as exc:
            return f"Error: {exc}"
        return json.dumps({"post_id": post.id, "channels_available": [c.key for c in post_svc.compatible_channels(post)]})


@server.tool()
def submit_drafts(
    post_id: int,
    versions: list[VersionDraft],
    title: str = "",
    pillar: str = "",
    notes: str = "",
    media: list[MediaNote] | None = None,
    submit_for_review: bool = True,
) -> str:
    """Save captions for each channel, media descriptions/alt text, and send the post for human review.

    Only include channels you want enabled (set enabled=false to drop one). If validation fails the
    drafts are still saved and the problems are returned so you can fix them and call again.
    """
    with session_scope() as db:
        actor = _actor(db)
        post = db.get(Post, post_id)
        if post is None:
            return f"Error: post {post_id} doesn't exist."
        if post.status in ("approved", "done"):
            return "Error: this post is already approved; edits happen in the web app."
        payload = []
        for v in versions:
            if v.channel not in ch.CHANNELS:
                return f"Error: unknown channel {v.channel!r}. Valid: {', '.join(ch.CHANNELS)}"
            try:
                when = parse_local(v.scheduled_at) if v.scheduled_at else None
            except ValueError:
                return f"Error: {v.channel}: scheduled_at must look like 2026-10-14T18:00"
            payload.append({"channel": v.channel, "enabled": v.enabled, "body": v.body, "hashtags": v.hashtags,
                            "title": v.title, "scheduled_at": when})
        for note in media or []:
            asset = db.get(MediaAsset, note.media_id)
            if asset is None or asset.id not in {m.id for m in post.media}:
                continue
            if note.description:
                asset.description = note.description
            if note.alt_text:
                asset.alt_text = note.alt_text
            if note.tags:
                asset.tags = sorted(set(asset.tags) | set(note.tags))
        try:
            post_svc.update_post(db, actor, post, title=title or None, pillar=pillar or None,
                                 claude_notes=notes or None, versions=payload)
        except post_svc.PostError as exc:
            db.rollback()
            return f"Error: {exc}"
        if post.status == "needs_claude":
            post.status = "draft"
        result = {"post_id": post.id, "status": post.status}
        found = post_svc.problems(post)
        if found:
            result["problems"] = found
        elif submit_for_review:
            post_svc.submit_for_review(db, actor, post)
            result["status"] = post.status
        audit(db, actor, "claude_drafted", "post", post.id, channels=[v["channel"] for v in payload])
        return json.dumps(result)


class MCPGate:
    """LAN-only, bearer-token gate in front of the MCP app."""

    def __init__(self):
        self.app = None

    def start(self):
        """Build a fresh MCP app; its session manager can run only once, so one per app start."""
        self.app = server.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            host="0.0.0.0",
        )
        return server.session_manager.run()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.app is None:
            return await _reject(send, 503, "The Studio is still starting.")
        s = settings()
        ip = (scope.get("client") or (None, None))[0]
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        via_proxy = ip_in(ip, s.trusted_proxies) or "x-forwarded-for" in headers or "forwarded" in headers
        if via_proxy or not ip_in(ip, s.mcp_allowed_networks):
            return await _reject(send, 403, "MCP is only available on the local network.")
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        user_id = None
        if token:
            with session_scope() as db:
                user = user_for_token(db, token)
                user_id = user.id if user else None
        if user_id is None:
            return await _reject(send, 401, "Missing or invalid Studio token. Create one under Settings.")
        reset = _current_user_id.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user_id.reset(reset)


async def _reject(send, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
