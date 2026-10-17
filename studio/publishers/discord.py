"""Posting approved posts to a Discord channel (e.g. #shop-news) with the uploads bot.

One message per post: the caption plus up to 10 photos (re-encoded, so phone metadata never
leaves), or the video if it fits under the server's upload limit, otherwise a link to the
approved public copy. In an Announcement channel the message is also published to servers that
follow it.
"""

from __future__ import annotations

import io
import json

import httpx
from PIL import Image

from .. import public_media
from ..db import settings
from ..media import abs_path
from ..models import ChannelVersion
from .bluesky import PublishError

API = "https://discord.com/api/v10"
ANNOUNCEMENT = 5  # Discord channel type for announcement (news) channels
MAX_TEXT = 2000


class DiscordClient:
    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        if not s.discord_post_configured:
            raise PublishError("Discord posting isn't configured (STUDIO_DISCORD_BOT_TOKEN / STUDIO_DISCORD_POST_CHANNEL_ID)")
        self.channel = s.discord_post_channel_id
        self.limit = s.discord_upload_mb * 1024 * 1024
        self.http = http or httpx.Client(timeout=120)
        self.headers = {"Authorization": f"Bot {s.discord_bot_token}",
                        "User-Agent": "DiscordBot (https://github.com/ColumbiaGadgetWorks/social-media, 1)"}
        info = self._call("GET", f"/channels/{self.channel}")
        self.guild, self.kind = info.get("guild_id", ""), info.get("type")

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, f"{API}{path}", headers=self.headers, **kw)
        if r.status_code == 429:
            raise PublishError("Discord rate limit; trying again shortly")
        if r.status_code in (401, 403):
            raise PublishError(f"Discord refused ({r.status_code}): {_why(r)}. The bot needs View Channel, Send Messages "
                               "and Attach Files in the posting channel (photos fail without Attach Files)")
        if r.status_code == 413:
            raise PublishError(f"Discord says the message is too large ({_why(r)}); the photos and video together must "
                               f"fit under {self.limit // (1024 * 1024)} MB (STUDIO_DISCORD_UPLOAD_MB)")
        if r.status_code >= 400:
            raise PublishError(f"Discord error (HTTP {r.status_code}): {_why(r)}")
        return r.json() if r.content else {}

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        text = version.full_text
        files: list[tuple[str, bytes, str]] = []
        media = version.post.media
        video = next((m for m in media if m.kind == "video"), None)
        if video is not None:
            path = abs_path(video.path)
            if path.stat().st_size <= self.limit:
                files.append((f"cgw-{video.id}{path.suffix.lower() or '.mp4'}", path.read_bytes(), video.mime or "video/mp4"))
            else:  # too big for Discord: link the approved public copy, which Discord plays inline
                text = f"{text}\n\n{public_media.url(video, 'video')}".strip()
        else:
            photos = [m for m in media if m.kind == "image"][:10]
            for m, data in zip(photos, fit_images([public_media._open(m) for m in photos], self.limit), strict=True):
                files.append((f"cgw-{m.id}.jpg", data, "image/jpeg"))
        if len(text) > MAX_TEXT:
            raise PublishError(f"Discord messages are limited to {MAX_TEXT} characters ({len(text)} here)")
        payload = {"content": text, "allowed_mentions": {"parse": []},
                   "attachments": [{"id": i, "filename": name, "description": _alt(media, name)[:1024]}
                                   for i, (name, _, _) in enumerate(files)]}
        if files:
            parts = {f"files[{i}]": (name, data, mime) for i, (name, data, mime) in enumerate(files)}
            msg = self._call("POST", f"/channels/{self.channel}/messages",
                             data={"payload_json": json.dumps(payload)}, files=parts)
        else:
            msg = self._call("POST", f"/channels/{self.channel}/messages", json=payload)
        if self.kind == ANNOUNCEMENT:  # also reach servers that follow #shop-news
            try:
                self._call("POST", f"/channels/{self.channel}/messages/{msg['id']}/crosspost")
            except PublishError:
                pass  # the message is up in our own channel either way
        url = f"https://discord.com/channels/{self.guild or '@me'}/{self.channel}/{msg['id']}"
        return url, msg["id"]


def _why(r: httpx.Response) -> str:
    """Discord's own explanation (its message and error code), not just the HTTP status."""
    try:
        body = r.json()
        return f"{body.get('message', r.text[:200])} (code {body.get('code', '?')})"
    except ValueError:
        return r.text[:200] or "no details"


def fit_images(images: list[Image.Image], limit: int) -> list[bytes]:
    """JPEGs for one message. Discord caps the whole upload (not each photo), so ten large photos can be refused
    even when every one is small enough alone; shrink them together until the set fits."""
    out: list[bytes] = []
    for edge, quality in ((public_media.MAX_EDGE, 90), (2048, 85), (1600, 80), (1280, 72), (1024, 65), (800, 60)):
        out = []
        for img in images:
            copy = img.copy()
            copy.thumbnail((edge, edge))
            buf = io.BytesIO()
            copy.save(buf, "JPEG", quality=quality, optimize=True)
            out.append(buf.getvalue())
        if sum(len(b) for b in out) <= limit * 0.95:  # leave room for the caption and multipart overhead
            return out
    raise PublishError(f"These photos can't be made to fit under Discord's {limit // (1024 * 1024)} MB limit; use fewer")


def _alt(media, filename: str) -> str:
    """Alt text for an attachment (Discord shows it to screen readers)."""
    for m in media:
        if filename.startswith(f"cgw-{m.id}."):
            return m.alt_text or ""
    return ""
