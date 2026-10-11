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
    theme: Mapped[str] = mapped_column(String(16), default="orange")  # colorway (Settings → Appearance)
    color_mode: Mapped[str] = mapped_column(String(8), default="auto")  # auto | light | dark
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
    # "mcp" for Claude Code, "extension" for the batch-day browser extension; never interchangeable
    scope: Mapped[str] = mapped_column(String(16), default="mcp")
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
    # Videos: speech transcript as [{"start": s, "end": s, "text": ...}], filled by Whisper when enabled.
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    transcript_status: Mapped[str] = mapped_column(String(16), default="")  # "" | done | failed | off
    # Renders: where it came from, and any credit line its music license requires.
    source_media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credit: Mapped[str] = mapped_column(String(300), default="")
    # The calendar event it was taken at ("Taken at" on upload), so weekly promos can find fresh photos.
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Batch uploads: files wait in their batch, unsorted, until they're grouped into posts on the sort page.
    upload_batch: Mapped[str] = mapped_column(String(24), default="")
    unsorted: Mapped[bool] = mapped_column(Boolean, default=False)
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
    angle: Mapped[str] = mapped_column(String(32), default="")  # weekly event promos rotate angles
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


class AppSetting(Base):
    """Settings changed in the app (for now: which emails to get), as opposed to the container's environment."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")


class ReminderLog(Base):
    """One row per reminder sent, so each reminder goes out once per key."""

    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("kind", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PendingAlert(Base):
    """Something to tell people about: urgent ones go out in at most one email a day, the rest in the
    weekly digest."""

    __tablename__ = "pending_alerts"
    __table_args__ = (UniqueConstraint("kind", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(96))
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    line: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    # Weekly series key (title|weekday|time). The site's feed lists each Hack Night separately,
    # so the Studio spots the pattern itself.
    series: Mapped[str] = mapped_column(String(320), default="")
    promote: Mapped[bool] = mapped_column(Boolean, default=True)
    email_ok: Mapped[bool] = mapped_column(Boolean, default=True)  # "Email: no" in the description turns it off
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


class MusicTrack(Base):
    """A track in the licensed music library. Only tracks with a recorded license are used."""

    __tablename__ = "music_tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    artist: Mapped[str] = mapped_column(String(200), default="")
    license: Mapped[str] = mapped_column(String(100))
    credit_line: Mapped[str] = mapped_column(String(300), default="")  # must appear in captions when set
    source_url: Mapped[str] = mapped_column(String(500), default="")
    mood: Mapped[str] = mapped_column(String(100), default="")
    path: Mapped[str] = mapped_column(String(512))
    duration_s: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RenderJob(Base):
    """A video edit: trim, fit, subtitles, music, logo, end card. Output is a new media item."""

    __tablename__ = "render_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), nullable=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | failed
    output_media_id: Mapped[int | None] = mapped_column(ForeignKey("media.id"), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[MediaAsset] = relationship(foreign_keys=[source_media_id])
    output: Mapped[MediaAsset | None] = relationship(foreign_keys=[output_media_id])


class Announcement(Base):
    """An email to the "Email updates" list: classes, events and important news only.

    Deliberately separate from Post: there is no way to turn a social post into an email.
    """

    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="monthly")  # monthly | special
    month: Mapped[str] = mapped_column(String(7), default="")  # YYYY-MM for monthly ones
    subject: Mapped[str] = mapped_column(String(200), default="")
    preheader: Mapped[str] = mapped_column(String(200), default="")
    intro: Mapped[str] = mapped_column(Text, default="")
    # [{"event_id", "title", "when", "where", "link", "blurb"}]; facts come from the calendar
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    closing: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="needs_claude")
    send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # naive UTC
    override_cap: Mapped[bool] = mapped_column(Boolean, default=False)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    approved_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_by: Mapped[User | None] = relationship()


class AnnouncementRecipient(Base):
    __tablename__ = "announcement_recipients"
    __table_args__ = (UniqueConstraint("announcement_id", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("announcements.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(256))
    contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | failed
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Unsubscribe(Base):
    """An unsubscribe request. Dolibarr is the source of truth; this row holds it until synced."""

    __tablename__ = "unsubscribes"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(256), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
