"""Database tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

ROLES = ("contributor", "editor", "approver", "admin")

# Post lifecycle. "approved" is reachable only through approval.approve().
POST_STATUSES = ("needs_claude", "draft", "in_review", "approved", "done", "rejected")
STATUS_LABELS = {
    "needs_claude": "Needs Claude",
    "draft": "Draft",
    "in_review": "In review",
    "approved": "Approved",
    "done": "Done",
    "rejected": "Rejected",
}

# Per-channel publishing state.
VERSION_STATES = ("pending", "published", "failed", "skipped")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str] = mapped_column(String(256), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="contributor")
    proxy_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def label(self) -> str:
        return self.display_name or self.username

    def has_role(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


class ApiToken(Base):
    """Personal tokens for the MCP connection. Only the hash is stored."""

    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user: Mapped[User] = relationship()


class MediaAsset(Base):
    __tablename__ = "media"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # image | video
    original_name: Mapped[str] = mapped_column(String(256))
    path: Mapped[str] = mapped_column(String(512))  # relative to media_dir
    mime: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preview_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    frames: Mapped[list[str]] = mapped_column(JSON, default=list)
    uploader_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    alt_text: Mapped[str] = mapped_column(Text, default="")
    processing_status: Mapped[str] = mapped_column(String(16), default="pending")
    processing_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    uploader: Mapped[User | None] = relationship()


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")  # the uploader's one-line brief
    pillar: Mapped[str] = mapped_column(String(48), default="")
    status: Mapped[str] = mapped_column(String(16), default="needs_claude")
    source: Mapped[str] = mapped_column(String(16), default="upload")
    claude_notes: Mapped[str] = mapped_column(Text, default="")
    review_comment: Mapped[str] = mapped_column(Text, default="")
    # Event promos: which calendar event, what kind (announce | reminder), and when to aim for.
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(16), default="")
    target_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    event: Mapped[Event | None] = relationship(back_populates="posts")
    media_links: Mapped[list[PostMedia]] = relationship(
        order_by="PostMedia.position", cascade="all, delete-orphan", back_populates="post"
    )
    versions: Mapped[list[ChannelVersion]] = relationship(
        order_by="ChannelVersion.channel", cascade="all, delete-orphan", back_populates="post"
    )

    @property
    def media(self) -> list[MediaAsset]:
        return [link.media for link in self.media_links]

    def version(self, channel: str) -> ChannelVersion | None:
        return next((v for v in self.versions if v.channel == channel), None)

    @property
    def enabled_versions(self) -> list[ChannelVersion]:
        return [v for v in self.versions if v.enabled]

    @property
    def display_title(self) -> str:
        return self.title or (self.note[:60] if self.note else f"Post {self.id}")


class PostMedia(Base):
    __tablename__ = "post_media"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    post: Mapped[Post] = relationship(back_populates="media_links")
    media: Mapped[MediaAsset] = relationship()


class ChannelVersion(Base):
    __tablename__ = "channel_versions"
    __table_args__ = (UniqueConstraint("post_id", "channel"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[str] = mapped_column(String(500), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # naive UTC
    publish_state: Mapped[str] = mapped_column(String(16), default="pending")
    external_url: Mapped[str] = mapped_column(String(512), default="")
    external_id: Mapped[str] = mapped_column(String(256), default="")  # platform id, for metrics
    last_error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    post: Mapped[Post] = relationship(back_populates="versions")

    @property
    def full_text(self) -> str:
        parts = [self.body.strip(), self.hashtags.strip()]
        return "\n\n".join(p for p in parts if p)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | mcp | system
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_label: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReminderLog(Base):
    """One row per reminder sent, so each reminder goes out once per key."""

    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("kind", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """One occurrence from the events calendar (ICS)."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("uid", "start"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(256))
    start: Mapped[datetime] = mapped_column(DateTime)  # naive UTC
    end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    promote: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | cancelled
    facts_hash: Mapped[str] = mapped_column(String(64), default="")
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    promos_created: Mapped[bool] = mapped_column(Boolean, default=False)
    card_media_id: Mapped[int | None] = mapped_column(ForeignKey("media.id"), nullable=True)
    posts: Mapped[list[Post]] = relationship(back_populates="event")


class MetricSnapshot(Base):
    """Engagement numbers for one published channel version at one point in time."""

    __tablename__ = "metric_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("channel_versions.id", ondelete="CASCADE"))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[ChannelVersion] = relationship()


class Credential(Base):
    """Tokens the Studio refreshes itself (e.g. Threads), overriding the .env value."""

    __tablename__ = "credentials"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
