"""Posting to the CGW LinkedIn company page with the Community Management API.

The app (client id/secret) is set in the environment; a page admin connects it once in Settings
(OAuth). The access token lasts 60 days and the refresh token about a year; the nightly job
refreshes the access token, and Settings shows when a reconnect is due. Until the app is approved
and connected, LinkedIn stays on batch day.
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy.orm import Session

from .. import public_media
from ..db import session_scope, settings, utcnow
from ..media import abs_path
from ..models import ChannelVersion, Credential
from .bluesky import PublishError

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API = "https://api.linkedin.com/rest"
SCOPES = "w_organization_social r_organization_social"
KEYS = ("linkedin_access", "linkedin_refresh", "linkedin_expires", "linkedin_refresh_expires")
MAX_TEXT = 3000
RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")  # LinkedIn "little text" reserved characters
HASHTAG = re.compile(r"(?<![\w#])#(\w+)")

_connected_cache: tuple[float, bool] = (0.0, False)


# --- connection state -------------------------------------------------------


def _get(db: Session, key: str) -> str:
    row = db.get(Credential, key)
    return row.value if row else ""


def _put(db: Session, key: str, value: str) -> None:
    row = db.get(Credential, key)
    if row is None:
        db.add(Credential(key=key, value=value))
    else:
        row.value, row.updated_at = value, utcnow()


def connected() -> bool:
    """True when the app is set up and a page admin has connected it (cached for a minute)."""
    global _connected_cache
    if not settings().linkedin_app_configured:
        return False
    at, value = _connected_cache
    if time.monotonic() - at < 60:
        return value
    try:
        with session_scope() as db:
            value = bool(_get(db, "linkedin_access"))
    except Exception:
        value = False
    _connected_cache = (time.monotonic(), value)
    return value


def _forget_cache() -> None:
    global _connected_cache
    _connected_cache = (0.0, False)


def status(db: Session) -> dict:
    s = settings()
    expires, refresh_expires = _get(db, "linkedin_expires"), _get(db, "linkedin_refresh_expires")
    return {
        "app": s.linkedin_app_configured,
        "connected": bool(_get(db, "linkedin_access")),
        "expires": expires,
        "refresh_expires": refresh_expires,
        "can_refresh": bool(_get(db, "linkedin_refresh")),
        "org": s.linkedin_org_id,
    }


def redirect_uri() -> str:
    return f"{settings().base_url}/settings/linkedin/callback"


def authorize_url(session: dict) -> str:
    state = secrets.token_urlsafe(24)
    session["linkedin_state"] = state
    return AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": settings().linkedin_client_id,
        "redirect_uri": redirect_uri(), "state": state, "scope": SCOPES,
    })


def _save_tokens(db: Session, body: dict) -> None:
    now = utcnow()
    _put(db, "linkedin_access", body["access_token"])
    _put(db, "linkedin_expires", (now + timedelta(seconds=int(body.get("expires_in", 5184000)))).isoformat(timespec="minutes"))
    if body.get("refresh_token"):
        _put(db, "linkedin_refresh", body["refresh_token"])
        seconds = int(body.get("refresh_token_expires_in", 31536000))
        _put(db, "linkedin_refresh_expires", (now + timedelta(seconds=seconds)).isoformat(timespec="minutes"))
    _forget_cache()


def _token_request(data: dict, http: httpx.Client | None) -> dict:
    s = settings()
    client = http or httpx.Client(timeout=30)
    r = client.post(TOKEN_URL, data={**data, "client_id": s.linkedin_client_id, "client_secret": s.linkedin_client_secret})
    if r.status_code >= 400:
        raise PublishError(f"LinkedIn sign-in failed (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()


def finish_connect(db: Session, session: dict, code: str, state: str, http: httpx.Client | None = None) -> None:
    expected = session.pop("linkedin_state", None)
    if not expected or not secrets.compare_digest(expected, state or ""):
        raise PublishError("That LinkedIn sign-in link expired. Try Connect again.")
    body = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}, http)
    _save_tokens(db, body)


def disconnect(db: Session) -> None:
    for key in KEYS:
        row = db.get(Credential, key)
        if row is not None:
            db.delete(row)
    _forget_cache()


def refresh(db: Session, http: httpx.Client | None = None, within_days: int = 10) -> bool:
    """Refresh the 60-day access token when it has under `within_days` left (needs a refresh token)."""
    if not settings().linkedin_app_configured or not _get(db, "linkedin_refresh"):
        return False
    expires = _get(db, "linkedin_expires")
    if expires and utcnow() + timedelta(days=within_days) < datetime.fromisoformat(expires):
        return False
    body = _token_request({"grant_type": "refresh_token", "refresh_token": _get(db, "linkedin_refresh")}, http)
    _save_tokens(db, body)
    return True


# --- text --------------------------------------------------------------------


def _escape(text: str) -> str:
    return RESERVED.sub(r"\\\1", text)


def little_text(text: str) -> str:
    """LinkedIn's "little text" format: reserved characters escaped, #hashtags as hashtag templates."""
    out, last = [], 0
    for m in HASHTAG.finditer(text):
        out.append(_escape(text[last:m.start()]))
        out.append("{hashtag|\\#|" + m.group(1) + "}")
        last = m.end()
    out.append(_escape(text[last:]))
    return "".join(out)


# --- publishing ----------------------------------------------------------------


class LinkedInClient:
    def __init__(self, db: Session, http: httpx.Client | None = None, sleep=time.sleep):
        s = settings()
        if not s.linkedin_app_configured:
            raise PublishError("LinkedIn isn't configured (STUDIO_LINKEDIN_CLIENT_ID / _CLIENT_SECRET / _ORG_ID)")
        token = _get(db, "linkedin_access")
        if not token:
            raise PublishError("LinkedIn isn't connected: Settings → Connect LinkedIn")
        self.owner = f"urn:li:organization:{s.linkedin_org_id}"
        self.http = http or httpx.Client(timeout=120)
        self.sleep = sleep
        self.headers = {"Authorization": f"Bearer {token}", "LinkedIn-Version": s.linkedin_api_version,
                        "X-Restli-Protocol-Version": "2.0.0"}

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.http.request(method, f"{API}{path}", headers=self.headers, **kw)
        if r.status_code == 401:
            raise PublishError("LinkedIn sign-in expired: Settings → Connect LinkedIn again")
        if r.status_code == 426 or "NONEXISTENT_VERSION" in r.text[:500]:
            raise PublishError(f"LinkedIn API version {settings().linkedin_api_version} isn't accepted any more; "
                               "set STUDIO_LINKEDIN_API_VERSION to a recent month (YYYYMM)")
        if r.status_code >= 400:
            raise PublishError(f"LinkedIn error (HTTP {r.status_code}): {r.text[:300]}")
        return r

    def _upload_image(self, asset) -> str:
        init = self._call("POST", "/images?action=initializeUpload",
                          json={"initializeUploadRequest": {"owner": self.owner}}).json()["value"]
        data = public_media.render(asset, "jpg")
        r = self.http.put(init["uploadUrl"], content=data, headers={"Authorization": self.headers["Authorization"]})
        if r.status_code >= 400:
            raise PublishError(f"LinkedIn image upload failed (HTTP {r.status_code})")
        return init["image"]

    def _upload_video(self, asset) -> str:
        path = abs_path(asset.path)
        size = path.stat().st_size
        init = self._call("POST", "/videos?action=initializeUpload", json={"initializeUploadRequest": {
            "owner": self.owner, "fileSizeBytes": size, "uploadCaptions": False, "uploadThumbnail": False}}).json()["value"]
        etags = []
        with path.open("rb") as f:
            for part in init["uploadInstructions"]:
                f.seek(part["firstByte"])
                chunk = f.read(part["lastByte"] - part["firstByte"] + 1)
                r = self.http.put(part["uploadUrl"], content=chunk, headers={"Content-Type": "application/octet-stream"})
                if r.status_code >= 400:
                    raise PublishError(f"LinkedIn video upload failed (HTTP {r.status_code})")
                etags.append(r.headers.get("etag", "").strip('"'))
        video = init["video"]
        self._call("POST", "/videos?action=finalizeUpload", json={"finalizeUploadRequest": {
            "video": video, "uploadToken": init.get("uploadToken", ""), "uploadedPartIds": etags}})
        for _ in range(60):  # wait until LinkedIn has processed it (up to ~5 minutes)
            state = self._call("GET", f"/videos/{quote(video, safe='')}").json()
            if state.get("status") == "AVAILABLE":
                return video
            if state.get("status") == "PROCESSING_FAILED":
                raise PublishError("LinkedIn couldn't process the video")
            self.sleep(5)
        raise PublishError("LinkedIn is still processing the video; it will be retried")

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        text = version.full_text
        if len(text) > MAX_TEXT:
            raise PublishError(f"LinkedIn posts are limited to {MAX_TEXT} characters ({len(text)} here)")
        post = {
            "author": self.owner, "commentary": little_text(text), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False,
        }
        media = version.post.media
        video = next((m for m in media if m.kind == "video"), None)
        images = [m for m in media if m.kind == "image"][:20]
        if video is not None:
            post["content"] = {"media": {"id": self._upload_video(video), "title": (version.title or "")[:200]}}
        elif len(images) == 1:
            post["content"] = {"media": {"id": self._upload_image(images[0]), "altText": images[0].alt_text[:4086]}}
        elif images:
            post["content"] = {"multiImage": {"images": [{"id": self._upload_image(m), "altText": m.alt_text[:4086]}
                                                         for m in images]}}
        r = self._call("POST", "/posts", json=post)
        urn = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id") or ""
        return f"https://www.linkedin.com/feed/update/{urn}/", urn
