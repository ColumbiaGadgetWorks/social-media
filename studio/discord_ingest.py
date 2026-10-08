"""Photos and videos posted in a Discord channel go into the media library, like an upload.

The bot only reads: every scheduler loop it asks Discord for new messages in the configured
channel(s) (no inbound connection, no extra service, nothing to expose). Each message with photos
or videos becomes an upload: the message text is the note, the sender is recorded, and a post is
queued for Claude unless the message says "library". The bot reacts ✅ when it's in, ⚠️ when
something couldn't be taken (with a short reply saying why, if it may send messages).

Setup: docs/setup-platforms.md, "Discord uploads".
"""

from __future__ import annotations

import logging
import mimetypes
import re
import tempfile
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import posts as post_svc
from .db import settings, utcnow
from .media import MediaError, kind_for, store_upload
from .models import Credential, Event, ReminderLog
from .security import Actor, audit

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
OK, WARN = "✅", "⚠️"
MENTION_RE = re.compile(r"<(@[!&]?|#)\d+>|<a?:\w+:\d+>")
LIBRARY_RE = re.compile(r"(^|\s)#?library\b", re.I)
SEPARATE_RE = re.compile(r"(^|\s)#?separate\b", re.I)
ACTOR = Actor.system()


class DiscordError(Exception):
    pass


class Discord:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.headers = {"Authorization": f"Bot {token}",
                        "User-Agent": "DiscordBot (https://github.com/ColumbiaGadgetWorks/social-media, 1)"}

    def _get(self, path: str, **params):
        r = self.http.get(f"{API}{path}", headers=self.headers, params=params)
        if r.status_code == 429:
            raise DiscordError("rate limited")
        if r.status_code in (401, 403):
            raise DiscordError(f"Discord refused ({r.status_code}): check the bot token, that the bot is in the "
                               "server, can see the channel, and has Message Content Intent on")
        r.raise_for_status()
        return r.json()

    def messages(self, channel_id: str, after: str | None = None) -> list[dict]:
        """New messages, oldest first."""
        params = {"limit": 50}
        if after:
            params["after"] = after
        return sorted(self._get(f"/channels/{channel_id}/messages", **params), key=lambda m: int(m["id"]))

    def latest_id(self, channel_id: str) -> str | None:
        found = self._get(f"/channels/{channel_id}/messages", limit=1)
        return found[0]["id"] if found else None

    def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        try:
            self.http.put(f"{API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
                          headers=self.headers)
        except httpx.HTTPError:
            log.warning("couldn't react to Discord message %s", message_id)

    def reply(self, channel_id: str, message_id: str, text: str) -> None:
        try:
            self.http.post(f"{API}/channels/{channel_id}/messages", headers=self.headers,
                           json={"content": text[:1900], "message_reference": {"message_id": message_id},
                                 "allowed_mentions": {"parse": []}})
        except httpx.HTTPError:
            log.warning("couldn't reply to Discord message %s", message_id)

    def download(self, url: str, limit: int):
        """The attachment as a temporary file (Discord's signed CDN link)."""
        out = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        size = 0
        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close()
                    raise MediaError("larger than the upload limit")
                out.write(chunk)
        out.seek(0)
        return out


def _cursor_key(channel_id: str) -> str:
    return f"discord_cursor:{channel_id}"


def _get_cursor(db: Session, channel_id: str) -> str | None:
    row = db.get(Credential, _cursor_key(channel_id))
    return row.value if row else None


def _set_cursor(db: Session, channel_id: str, message_id: str) -> None:
    row = db.get(Credential, _cursor_key(channel_id))
    if row is None:
        db.add(Credential(key=_cursor_key(channel_id), value=message_id))
    else:
        row.value, row.updated_at = message_id, utcnow()


def _sent_at(message: dict) -> datetime:
    """Discord's ISO timestamp as naive UTC, like everything else in the database."""
    return datetime.fromisoformat(message["timestamp"]).astimezone(UTC).replace(tzinfo=None)


def event_at(db: Session, when: datetime) -> Event | None:
    """The event happening when the photo was sent (2 hours before start to 6 hours after the end)."""
    rows = db.scalars(select(Event).where(
        Event.status == "active", Event.start <= when + timedelta(hours=2), Event.start >= when - timedelta(hours=14),
    ).order_by(Event.start.desc())).all()
    for e in sorted(rows, key=lambda e: (not e.series, -e.start.timestamp())):
        end = e.end or e.start + timedelta(hours=2)
        if e.start - timedelta(hours=2) <= when <= end + timedelta(hours=6):
            return e
    return None


def _clean_text(content: str) -> str:
    text = MENTION_RE.sub("", content or "")
    text = LIBRARY_RE.sub(" ", text)
    text = SEPARATE_RE.sub(" ", text)
    return re.sub(r"\s+([,.!?;:])", r"\1", " ".join(text.split()))


def _media_attachments(message: dict) -> list[dict]:
    found = []
    for a in message.get("attachments", []):
        mime = (a.get("content_type") or "").split(";")[0]
        if kind_for(mime) or kind_for(mimetypes.guess_type(a.get("filename", ""))[0] or ""):
            found.append(a)
    return found


def ingest_message(db: Session, api: Discord, channel_id: str, message: dict) -> dict:
    """One message → media (+ a post). Returns what happened, for logs and tests."""
    if message.get("author", {}).get("bot"):
        return {"skipped": "bot"}
    attachments = _media_attachments(message)
    if not attachments:
        return {"skipped": "no media"}
    key = message["id"]
    if db.scalar(select(ReminderLog).where(ReminderLog.kind == "discord_msg", ReminderLog.key == key)):
        return {"skipped": "already in"}
    author = message.get("author", {})
    name = author.get("global_name") or author.get("username") or "someone"
    text = _clean_text(message.get("content", ""))
    note = f"{text} (sent by {name} on Discord)" if text else f"Sent by {name} on Discord"
    when = _sent_at(message)
    taken_at = event_at(db, when)
    limit = settings().max_upload_mb * 1024 * 1024
    stored, problems = [], []
    for a in attachments:
        try:
            if a.get("size", 0) > limit:
                raise MediaError(f"larger than {settings().max_upload_mb} MB")
            with api.download(a["url"], limit) as f:
                asset = store_upload(db, f, a.get("filename", "discord-file"), a.get("content_type"), None, note[:2000])
            asset.tags = ["discord"]
            asset.event_id = taken_at.id if taken_at else None
            stored.append(asset)
            audit(db, ACTOR, "media_uploaded", "media", asset.id, name=asset.original_name, size=asset.size_bytes,
                  via="discord", sender=name, message=key)
        except (MediaError, httpx.HTTPError) as exc:
            problems.append(f"{a.get('filename', 'a file')}: {exc}")
    content = message.get("content", "")
    mode = "library" if LIBRARY_RE.search(content) else "separate" if SEPARATE_RE.search(content) else "together"
    made = []
    if stored and mode != "library":
        images = [a.id for a in stored if a.kind == "image"]
        videos = [a.id for a in stored if a.kind == "video"]
        groups = [[i] for i in images + videos] if mode == "separate" else ([images] if images else []) + [[v] for v in videos]
        for group in groups:
            post = post_svc.create_post(db, ACTOR, group, note=note[:2000], for_claude=True, source="discord")
            made.append(post.id)
    db.add(ReminderLog(kind="discord_msg", key=key))
    db.commit()
    if stored:
        api.react(channel_id, key, OK)
    if problems:
        api.react(channel_id, key, WARN)
        api.reply(channel_id, key, "Couldn't add: " + "; ".join(problems))
    return {"media": [a.id for a in stored], "posts": made, "problems": problems,
            "taken_at": taken_at.id if taken_at else None}


def poll(db: Session, api: Discord | None = None) -> int:
    """Take in new messages from each configured channel. Returns how many files came in."""
    s = settings()
    if not s.discord_configured:
        return 0
    api = api or Discord(s.discord_bot_token)
    count = 0
    for channel_id in s.discord_channel_ids:
        cursor = _get_cursor(db, channel_id)
        if cursor is None:  # first run: start from now, don't import the channel's history
            latest = api.latest_id(channel_id)
            _set_cursor(db, channel_id, latest or "0")
            db.commit()
            log.info("Discord channel %s: starting after message %s", channel_id, latest)
            continue
        for message in api.messages(channel_id, after=cursor):
            try:
                result = ingest_message(db, api, channel_id, message)
                count += len(result.get("media", []))
            except Exception:
                log.exception("Discord message %s couldn't be taken in", message.get("id"))
                db.rollback()
            _set_cursor(db, channel_id, message["id"])
            db.commit()
    return count
