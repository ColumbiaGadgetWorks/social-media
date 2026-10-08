"""Signed public links to approved media, for platforms that fetch media by URL (Meta).

/m/<media id>/<variant>/<signature>.<ext> is the only unauthenticated media path. A
link works only while the media belongs to an approved post, so drafts never leak.
"""

from __future__ import annotations

import hashlib
import hmac
import io

from PIL import Image, ImageFilter, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import settings
from .media import abs_path
from .models import MediaAsset, Post, PostMedia

VARIANTS = {"jpg": "jpg", "ig": "jpg", "video": "mp4"}
IG_MIN_RATIO, IG_MAX_RATIO = 4 / 5, 1.91
MAX_EDGE = 1440


def signature(media_id: int, variant: str) -> str:
    key = settings().secret_key.encode()
    return hmac.new(key, f"public-media:{media_id}:{variant}".encode(), hashlib.sha256).hexdigest()[:32]


def url(asset: MediaAsset, variant: str) -> str:
    return f"{settings().media_base_url}/m/{asset.id}/{variant}/{signature(asset.id, variant)}.{VARIANTS[variant]}"


def allowed(db: Session, media_id: int, variant: str, sig: str) -> bool:
    if variant not in VARIANTS or not hmac.compare_digest(sig, signature(media_id, variant)):
        return False
    approved = db.scalar(
        select(Post.id).join(PostMedia).where(PostMedia.media_id == media_id, Post.status == "approved").limit(1)
    )
    return approved is not None


def _open(asset: MediaAsset) -> Image.Image:
    with Image.open(abs_path(asset.path)) as raw:
        img = ImageOps.exif_transpose(raw)  # also leaves EXIF/GPS behind on save
        return img.convert("RGB")


def jpeg(img: Image.Image, quality: int = 90) -> bytes:
    img = img.copy()
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def fit_for_instagram(img: Image.Image) -> Image.Image:
    """Instagram feed photos must be between 4:5 and 1.91:1. Pad onto a blurred copy rather than crop."""
    w, h = img.size
    ratio = w / h
    if IG_MIN_RATIO <= ratio <= IG_MAX_RATIO:
        return img
    if ratio < IG_MIN_RATIO:
        canvas_size = (round(h * IG_MIN_RATIO), h)
    else:
        canvas_size = (w, round(w / IG_MAX_RATIO))
    background = ImageOps.fit(img, canvas_size).filter(ImageFilter.GaussianBlur(radius=max(canvas_size) // 30))
    background = Image.blend(background, Image.new("RGB", canvas_size, (0, 0, 0)), 0.25)
    background.paste(img, ((canvas_size[0] - w) // 2, (canvas_size[1] - h) // 2))
    return background


def render(asset: MediaAsset, variant: str) -> bytes | None:
    """Bytes for an image variant; None means serve the original file (video)."""
    if variant == "video":
        return None
    img = _open(asset)
    if variant == "ig":
        img = fit_for_instagram(img)
    return jpeg(img)
