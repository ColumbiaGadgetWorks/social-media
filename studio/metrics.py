"""Nightly engagement numbers for published posts, kept permanently (Metricool free keeps ~30 days)."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import settings, utcnow
from .models import ChannelVersion, MetricSnapshot, Post

log = logging.getLogger(__name__)
WINDOW_DAYS = 30
BSKY_PUBLIC = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"


def _graph(http: httpx.Client, base: str, path: str, token: str, **params) -> dict:
    r = http.get(f"{base}/{path}", params={**params, "access_token": token})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or "error" in body:
        raise RuntimeError((body.get("error") or {}).get("message") or f"HTTP {r.status_code}")
    return body


def _insights(body: dict) -> dict:
    out = {}
    for item in body.get("data", []):
        values = item.get("values") or [{}]
        value = item.get("total_value", {}).get("value", values[-1].get("value"))
        if isinstance(value, (int, float)):
            out[item["name"]] = value
    return out


def collect_one(http: httpx.Client, version: ChannelVersion, db: Session | None = None) -> dict:
    s = settings()
    graph = f"https://graph.facebook.com/{s.meta_graph_version}"
    if version.channel == "bluesky":
        r = http.get(BSKY_PUBLIC, params={"uris": version.external_id})
        post = (r.json().get("posts") or [{}])[0]
        return {k: post.get(f"{k}Count", 0) for k in ("like", "repost", "reply", "quote")}
    if version.channel == "instagram":
        data = _graph(http, graph, version.external_id, s.meta_page_token, fields="like_count,comments_count")
        out = {"likes": data.get("like_count", 0), "comments": data.get("comments_count", 0)}
        try:
            out |= _insights(_graph(http, graph, f"{version.external_id}/insights", s.meta_page_token,
                                    metric="reach,saved,shares,views"))
        except RuntimeError as exc:  # metric names change between API versions; keep what we have
            out["insights_error"] = str(exc)[:120]
        return out
    if version.channel == "facebook":
        data = _graph(http, graph, version.external_id, s.meta_page_token,
                      fields="shares,reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)")
        return {
            "reactions": data.get("reactions", {}).get("summary", {}).get("total_count", 0),
            "comments": data.get("comments", {}).get("summary", {}).get("total_count", 0),
            "shares": data.get("shares", {}).get("count", 0),
        }
    if version.channel == "threads":
        from .publishers.meta import THREADS_BASE, threads_token

        return _insights(_graph(http, THREADS_BASE, f"{version.external_id}/insights", threads_token(db),
                                metric="views,likes,replies,reposts,quotes"))
    return {}


def collect(db: Session, http: httpx.Client | None = None) -> int:
    http = http or httpx.Client(timeout=30)
    since = utcnow() - timedelta(days=WINDOW_DAYS)
    versions = db.scalars(
        select(ChannelVersion).where(
            ChannelVersion.publish_state == "published", ChannelVersion.external_id != "",
            ChannelVersion.published_at >= since,
            ChannelVersion.channel.in_(("bluesky", "instagram", "facebook", "threads")),
        )
    ).all()
    count = 0
    for v in versions:
        try:
            data = collect_one(http, v, db)
        except Exception as exc:
            log.warning("metrics for %s %s failed: %s", v.channel, v.external_id, exc)
            continue
        db.add(MetricSnapshot(version_id=v.id, data=data))
        count += 1
    return count


def latest(db: Session, version_id: int) -> MetricSnapshot | None:
    return db.scalar(
        select(MetricSnapshot).where(MetricSnapshot.version_id == version_id).order_by(MetricSnapshot.collected_at.desc()).limit(1)
    )


ENGAGEMENT_KEYS = ("likes", "like", "reactions", "comments", "reply", "replies", "shares", "repost", "reposts",
                   "saved", "quote", "quotes")


def engagement(data: dict) -> int:
    return int(sum(v for k, v in data.items() if k in ENGAGEMENT_KEYS and isinstance(v, (int, float))))


def summary(db: Session, days: int = 90) -> dict:
    """Latest numbers per published version, plus totals by pillar and by channel, for Claude's reviews."""
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(ChannelVersion, Post).join(Post).where(
            ChannelVersion.publish_state == "published", ChannelVersion.published_at >= since)
    ).all()
    posts, by_pillar, by_channel, by_format = [], defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for version, post in rows:
        snap = latest(db, version.id)
        data = snap.data if snap else {}
        score = engagement(data)
        posts.append({
            "post_id": post.id, "title": post.display_title, "pillar": post.pillar, "channel": version.channel,
            "published": version.published_at.isoformat() if version.published_at else None,
            "media": sorted({m.kind for m in post.media}) or ["text"], "url": version.external_url,
            "metrics": data, "engagement": score,
        })
        kinds = [m.kind for m in post.media]
        fmt = "video" if "video" in kinds else "carousel" if len(kinds) > 1 else "photo" if kinds else "text"
        posts[-1]["format"] = fmt
        if snap:
            for bucket, key in ((by_pillar, post.pillar or "none"), (by_channel, version.channel), (by_format, fmt)):
                bucket[key][0] += 1
                bucket[key][1] += score
    avg = lambda d: {k: {"posts": n, "avg_engagement": round(t / n, 1)} for k, (n, t) in d.items()}  # noqa: E731
    return {"days": days, "posts": posts, "by_pillar": avg(by_pillar), "by_channel": avg(by_channel),
            "by_format": avg(by_format),
            "note": "Engagement = likes + comments/replies + shares/reposts + saves. Batch-day channels have links only."}
