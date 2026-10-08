"""Facebook page, Instagram business account and Threads publishing (Meta Graph APIs).

Instagram and Threads fetch media from a URL, so images and videos are handed over as
signed /m/... links (see public_media.py), which only work while the post is approved.
"""

from __future__ import annotations

import json
import time

import httpx
from sqlalchemy.orm import Session

from .. import public_media
from ..db import settings, utcnow
from ..models import ChannelVersion, Credential, MediaAsset
from .bluesky import PublishError

THREADS_BASE = "https://graph.threads.net/v1.0"
POLL_SECONDS = 5
POLL_LIMIT = 60  # five minutes for video processing


def _graph_base() -> str:
    return f"https://graph.facebook.com/{settings().meta_graph_version}"


class _Graph:
    def __init__(self, base: str, token: str, http: httpx.Client | None, sleep=time.sleep):
        self.base, self.token = base, token
        self.http = http or httpx.Client(timeout=120)
        self.sleep = sleep

    def call(self, method: str, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params["access_token"] = self.token
        if method == "GET":
            r = self.http.get(f"{self.base}/{path}", params=params)
        else:
            r = self.http.post(f"{self.base}/{path}", data=params)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            message = err.get("message") or r.text[:300]
            raise PublishError(f"{path.split('/')[-1] or path}: {message} (HTTP {r.status_code})")
        return body

    def wait_ready(self, container_id: str, field: str) -> None:
        """Video containers process asynchronously; poll until they're ready to publish."""
        for _ in range(POLL_LIMIT):
            status = self.call("GET", container_id, fields=field).get(field, "")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"the platform couldn't process this media (status {status})")
            self.sleep(POLL_SECONDS)
        raise PublishError("media processing took longer than 5 minutes; retry later")


def _media(version: ChannelVersion) -> tuple[list[MediaAsset], MediaAsset | None]:
    media = version.post.media
    images = [m for m in media if m.kind == "image"]
    video = next((m for m in media if m.kind == "video"), None)
    return images, video


class FacebookClient:
    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        if not s.meta_configured:
            raise PublishError("Facebook isn't configured (STUDIO_META_PAGE_ID / STUDIO_META_PAGE_TOKEN)")
        self.page = s.meta_page_id
        self.g = _Graph(_graph_base(), s.meta_page_token, http)

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        text = version.full_text
        images, video = _media(version)
        if video:
            res = self.g.call("POST", f"{self.page}/videos", file_url=public_media.url(video, "video"), description=text)
            return f"https://www.facebook.com/{self.page}/videos/{res['id']}", res["id"]
        if len(images) == 1:
            res = self.g.call("POST", f"{self.page}/photos", url=public_media.url(images[0], "jpg"), message=text)
            post_id = res.get("post_id") or res["id"]
            return f"https://www.facebook.com/{post_id}", post_id
        attached = {}
        for i, img in enumerate(images):
            res = self.g.call("POST", f"{self.page}/photos", url=public_media.url(img, "jpg"), published="false")
            attached[f"attached_media[{i}]"] = json.dumps({"media_fbid": res["id"]})
        res = self.g.call("POST", f"{self.page}/feed", message=text, **attached)
        return f"https://www.facebook.com/{res['id']}", res["id"]


class InstagramClient:
    def __init__(self, http: httpx.Client | None = None, sleep=time.sleep):
        s = settings()
        if not s.instagram_configured:
            raise PublishError("Instagram isn't configured (STUDIO_META_IG_USER_ID and the page token)")
        self.user = s.meta_ig_user_id
        self.g = _Graph(_graph_base(), s.meta_page_token, http, sleep)

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        caption = version.full_text
        images, video = _media(version)
        if video:
            container = self.g.call("POST", f"{self.user}/media", media_type="REELS",
                                    video_url=public_media.url(video, "video"), caption=caption, share_to_feed="true")["id"]
            self.g.wait_ready(container, "status_code")
        elif len(images) == 1:
            container = self.g.call("POST", f"{self.user}/media", image_url=public_media.url(images[0], "ig"), caption=caption)["id"]
        elif images:
            children = [
                self.g.call("POST", f"{self.user}/media", image_url=public_media.url(img, "ig"), is_carousel_item="true")["id"]
                for img in images[:10]
            ]
            container = self.g.call("POST", f"{self.user}/media", media_type="CAROUSEL",
                                    children=",".join(children), caption=caption)["id"]
        else:
            raise PublishError("Instagram posts need a photo or video")
        media_id = self.g.call("POST", f"{self.user}/media_publish", creation_id=container)["id"]
        permalink = self.g.call("GET", media_id, fields="permalink").get("permalink", "")
        return permalink, media_id


def threads_token(db: Session | None = None) -> str:
    """The refreshed token from the database if there is one, else the .env value."""
    if db is not None:
        row = db.get(Credential, "threads_token")
        if row:
            return row.value
    return settings().threads_token


class ThreadsClient:
    def __init__(self, http: httpx.Client | None = None, sleep=time.sleep, token: str | None = None):
        s = settings()
        if not s.threads_configured:
            raise PublishError("Threads isn't configured (STUDIO_THREADS_USER_ID / STUDIO_THREADS_TOKEN)")
        self.user = s.threads_user_id
        self.g = _Graph(THREADS_BASE, token or s.threads_token, http, sleep)

    def _container(self, **params) -> str:
        return self.g.call("POST", f"{self.user}/threads", **params)["id"]

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        text = version.full_text
        images, video = _media(version)
        if video:
            container = self._container(media_type="VIDEO", video_url=public_media.url(video, "video"), text=text)
            self.g.wait_ready(container, "status")
        elif len(images) == 1:
            container = self._container(media_type="IMAGE", image_url=public_media.url(images[0], "jpg"), text=text)
        elif images:
            children = [self._container(media_type="IMAGE", image_url=public_media.url(i, "jpg"), is_carousel_item="true")
                        for i in images[:10]]
            container = self._container(media_type="CAROUSEL", children=",".join(children), text=text)
        else:
            container = self._container(media_type="TEXT", text=text)
        media_id = self.g.call("POST", f"{self.user}/threads_publish", creation_id=container)["id"]
        permalink = self.g.call("GET", media_id, fields="permalink").get("permalink", "")
        return permalink, media_id


def refresh_threads_token(db: Session, http: httpx.Client | None = None, max_age_days: int = 20) -> bool:
    """Threads tokens last 60 days; refresh well before that and keep the new one in the database."""
    if not settings().threads_configured:
        return False
    row = db.get(Credential, "threads_token")
    if row and (utcnow() - row.updated_at).days < max_age_days:
        return False
    client = http or httpx.Client(timeout=30)
    r = client.get(f"{THREADS_BASE.rsplit('/', 1)[0]}/refresh_access_token",
                   params={"grant_type": "th_refresh_token", "access_token": threads_token(db)})
    if r.status_code >= 400:
        raise PublishError(f"Threads token refresh failed (HTTP {r.status_code}): {r.text[:200]}")
    token = r.json()["access_token"]
    if row:
        row.value, row.updated_at = token, utcnow()
    else:
        db.add(Credential(key="threads_token", value=token))
    return True
