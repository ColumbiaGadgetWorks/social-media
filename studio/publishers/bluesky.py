"""Bluesky publishing over the AT Protocol XRPC API, using an app password."""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime

import httpx
from PIL import Image, ImageOps

from ..db import settings
from ..media import abs_path
from ..models import ChannelVersion

MAX_BLOB_BYTES = 950_000  # Bluesky rejects images over ~976 KB
URL_RE = re.compile(rb"https?://[^\s<>\"')\]]+")
TAG_RE = re.compile(rb"(?:^|\s)(#[^\d\s#][^\s#]*)")


class PublishError(Exception):
    pass


def facets(text: str) -> list[dict]:
    """Links and hashtags as rich-text facets (offsets are UTF-8 byte positions)."""
    data = text.encode()
    out = []
    for m in URL_RE.finditer(data):
        url = m.group().rstrip(b".,;:!?")
        out.append({
            "index": {"byteStart": m.start(), "byteEnd": m.start() + len(url)},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url.decode()}],
        })
    for m in TAG_RE.finditer(data):
        tag = m.group(1).rstrip(b".,;:!?")
        out.append({
            "index": {"byteStart": m.start(1), "byteEnd": m.start(1) + len(tag)},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag[1:].decode()}],
        })
    return out


def _compressed_jpeg(rel_path: str) -> tuple[bytes, int, int]:
    with Image.open(abs_path(rel_path)) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")  # also drops EXIF/GPS
    size = 2000
    for quality in (85, 75, 65, 55):
        copy = img.copy()
        copy.thumbnail((size, size))
        buf = io.BytesIO()
        copy.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BLOB_BYTES:
            return buf.getvalue(), copy.width, copy.height
        size = int(size * 0.8)
    raise PublishError("image could not be compressed under Bluesky's size limit")


class BlueskyClient:
    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        if not s.bluesky_handle or not s.bluesky_app_password:
            raise PublishError("Bluesky isn't configured (STUDIO_BLUESKY_HANDLE / STUDIO_BLUESKY_APP_PASSWORD)")
        self.service = s.bluesky_service
        self.handle = s.bluesky_handle
        self.http = http or httpx.Client(timeout=60)
        self._login(s.bluesky_app_password)

    def _login(self, password: str) -> None:
        r = self.http.post(
            f"{self.service}/xrpc/com.atproto.server.createSession",
            json={"identifier": self.handle, "password": password},
        )
        self._check(r, "sign in")
        body = r.json()
        self.did = body["did"]
        self.handle = body.get("handle", self.handle)
        self.headers = {"Authorization": f"Bearer {body['accessJwt']}"}

    @staticmethod
    def _check(r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json().get("message") or r.text
            except ValueError:
                detail = r.text
            raise PublishError(f"Bluesky {what} failed ({r.status_code}): {detail[:300]}")

    def upload_image(self, rel_path: str) -> tuple[dict, int, int]:
        data, w, h = _compressed_jpeg(rel_path)
        r = self.http.post(
            f"{self.service}/xrpc/com.atproto.repo.uploadBlob",
            content=data,
            headers={**self.headers, "Content-Type": "image/jpeg"},
        )
        self._check(r, "image upload")
        return r.json()["blob"], w, h

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        post = version.post
        text = version.full_text
        record: dict = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
        }
        found = facets(text)
        if found:
            record["facets"] = found
        images = [m for m in post.media if m.kind == "image"][:4]
        if images:
            embeds = []
            for m in images:
                blob, w, h = self.upload_image(m.path)
                embeds.append({"alt": m.alt_text or m.description or "", "image": blob, "aspectRatio": {"width": w, "height": h}})
            record["embed"] = {"$type": "app.bsky.embed.images", "images": embeds}
        r = self.http.post(
            f"{self.service}/xrpc/com.atproto.repo.createRecord",
            json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record},
            headers=self.headers,
        )
        self._check(r, "post")
        uri = r.json()["uri"]
        rkey = uri.rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{self.handle}/post/{rkey}", uri
