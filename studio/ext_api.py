"""API for the CGW Studio browser extension (batch day helper).

Same network rule as MCP: LAN only, never through the reverse proxy, and an extension-scoped
token. It reads approved batch items and their media, and records "scheduled" when the person
clicks it in the side panel. It can't change content or approve anything.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import batch as batch_mod
from . import channels as ch
from . import posts as post_svc
from .auth import client_ip
from .config import ip_in
from .db import get_db, settings
from .media import abs_path
from .models import ChannelVersion, MediaAsset, Post, PostMedia, User
from .security import Actor, user_for_token
from .timeutil import to_local

router = APIRouter(prefix="/api/ext")


def ext_user(request: Request, db: Session = Depends(get_db)) -> User:
    s = settings()
    ip = client_ip(request)
    via_proxy = ip_in(ip, s.trusted_proxies) or "x-forwarded-for" in request.headers or "forwarded" in request.headers
    if via_proxy or not ip_in(ip, s.mcp_allowed_networks):
        raise HTTPException(403, "The extension API is only available on the local network.")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    user = user_for_token(db, token, scope="extension") if token else None
    if user is None:
        raise HTTPException(401, "Missing or invalid extension token. Create one under Settings.")
    return user


def _manual(channel: str) -> ch.Channel:
    if channel not in ch.CHANNELS or ch.mode(channel) not in ch.MANUAL_MODES:
        raise HTTPException(404, "No batch for that channel.")
    return ch.CHANNELS[channel]


@router.get("/batch")
def batch_overview(user: User = Depends(ext_user), db: Session = Depends(get_db)):
    return {"channels": [{"key": r["channel"].key, "label": r["channel"].label, "count": r["count"],
                          "mode": ch.mode(r["channel"])} for r in batch_mod.overview(db)]}


@router.get("/batch/{channel}")
def batch_items(channel: str, user: User = Depends(ext_user), db: Session = Depends(get_db)):
    c = _manual(channel)
    out = []
    for item in batch_mod.items(db, channel):
        v = item.version
        local = to_local(v.scheduled_at)
        media = []
        for i, m in enumerate(item.post.media):
            letter = chr(ord("a") + i) if len(item.post.media) > 1 else ""
            ext = ".jpg" if m.kind == "image" else Path(m.path).suffix
            media.append({"media_id": m.id, "kind": m.kind, "filename": f"{item.stem}{letter}{ext}",
                          "mime": "image/jpeg" if m.kind == "image" else m.mime, "url": f"/api/ext/media/{m.id}"})
        out.append({
            "version_id": v.id, "number": item.number, "approved": item.approved, "blocked_reason": item.reason,
            "overdue": item.overdue, "within_horizon": item.within_horizon,
            "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M"),
            "when": local.strftime("%a %b %-d, %-I:%M %p"), "title": v.title, "caption": v.full_text,
            "post_title": item.post.display_title, "post_url": f"{settings().base_url}/posts/{item.post.id}",
            "media": media,
        })
    return {"channel": c.key, "label": c.label, "mode": ch.mode(c), "horizon_days": c.horizon_days, "items": out}


@router.get("/media/{media_id}")
def media_file(media_id: int, user: User = Depends(ext_user), db: Session = Depends(get_db)):
    """Only media of approved posts; photos re-encoded without phone metadata."""
    approved = db.scalar(select(Post.id).join(PostMedia).where(PostMedia.media_id == media_id,
                                                               Post.status == "approved").limit(1))
    asset = db.get(MediaAsset, media_id)
    if approved is None or asset is None:
        raise HTTPException(404, "Not available.")
    if asset.kind == "image":
        return Response(batch_mod.export_image(asset.path), media_type="image/jpeg")
    return FileResponse(abs_path(asset.path), media_type=asset.mime)


class Scheduled(BaseModel):
    url: str = ""


@router.post("/versions/{version_id}/scheduled")
def mark_scheduled(version_id: int, body: Scheduled, user: User = Depends(ext_user), db: Session = Depends(get_db)):
    version = db.get(ChannelVersion, version_id)
    if version is None:
        raise HTTPException(404, "No such item.")
    try:
        post_svc.mark_posted(db, Actor("user", user), version, body.url)
    except post_svc.PostError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "post_status": version.post.status}
