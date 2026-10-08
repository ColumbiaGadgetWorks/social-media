"""Publishing direct channels at their scheduled time."""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import channels as ch
from ..db import utcnow
from ..models import ChannelVersion, Post
from ..posts import finish_if_complete, is_publishable
from ..security import Actor, audit

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def _bluesky(db):
    from .bluesky import BlueskyClient

    return BlueskyClient()


def _facebook(db):
    from .meta import FacebookClient

    return FacebookClient()


def _instagram(db):
    from .meta import InstagramClient

    return InstagramClient()


def _threads(db):
    from .meta import ThreadsClient, threads_token

    return ThreadsClient(token=threads_token(db))


def _website(db):
    from .website import WebsiteClient

    return WebsiteClient()


# Each factory takes the db session and returns a client whose publish(version)
# returns (public url, platform id).
PUBLISHERS: dict[str, Callable] = {
    "bluesky": _bluesky, "facebook": _facebook, "instagram": _instagram,
    "threads": _threads, "website": _website,
}


def due_versions(db: Session) -> list[ChannelVersion]:
    direct = [c.key for c in ch.CHANNELS.values() if ch.mode(c) == "direct"]
    return db.scalars(
        select(ChannelVersion)
        .join(Post)
        .where(
            ChannelVersion.channel.in_(direct),
            ChannelVersion.enabled.is_(True),
            ChannelVersion.publish_state == "pending",
            ChannelVersion.scheduled_at <= utcnow(),
            Post.status == "approved",
        )
        .order_by(ChannelVersion.scheduled_at)
    ).all()


def publish_due(db: Session, factories: dict[str, Callable] | None = None) -> list[ChannelVersion]:
    """Publish every due direct version. Returns versions that just failed for good."""
    factories = factories or PUBLISHERS
    clients: dict[str, object] = {}
    failed: list[ChannelVersion] = []
    system = Actor.system()
    for version in due_versions(db):
        ok, reason = is_publishable(version.post)
        if not ok:
            # Defense in depth: never send content that doesn't match its approval.
            version.publish_state = "failed"
            version.last_error = f"Blocked: {reason}"
            audit(db, system, "publish_blocked", "post", version.post_id, channel=version.channel, reason=reason)
            failed.append(version)
            db.commit()
            continue
        try:
            if version.channel not in clients:
                clients[version.channel] = factories[version.channel](db)
            url, external_id = clients[version.channel].publish(version)
        except Exception as exc:
            log.warning("publishing post %s to %s failed: %s", version.post_id, version.channel, exc)
            version.attempts += 1
            version.last_error = str(exc)[:500]
            clients.pop(version.channel, None)
            if version.attempts >= MAX_ATTEMPTS:
                version.publish_state = "failed"
                failed.append(version)
            audit(db, system, "publish_failed", "post", version.post_id, channel=version.channel,
                  error=version.last_error, attempt=version.attempts)
            db.commit()
            continue
        version.publish_state = "published"
        version.external_url = url
        version.external_id = external_id
        version.published_at = utcnow()
        version.last_error = ""
        audit(db, system, "published", "post", version.post_id, channel=version.channel, url=url)
        finish_if_complete(db, version.post)
        db.commit()
    return failed


def retry(db: Session, actor: Actor, version: ChannelVersion) -> None:
    version.publish_state = "pending"
    version.attempts = 0
    version.last_error = ""
    audit(db, actor, "publish_retry", "post", version.post_id, channel=version.channel)
