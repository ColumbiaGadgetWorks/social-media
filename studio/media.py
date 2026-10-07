"""Storing uploads and preparing them: thumbnails, Claude-sized previews, video key frames."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import settings
from .models import MediaAsset

log = logging.getLogger(__name__)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/gif"}
VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v", "video/x-matroska"}
THUMB_PX = 400
PREVIEW_PX = 768  # what Claude sees; small keeps Pro sessions cheap
FRAME_COUNT = 5


class MediaError(Exception):
    pass


def kind_for(mime: str) -> str | None:
    if mime in IMAGE_TYPES:
        return "image"
    if mime in VIDEO_TYPES:
        return "video"
    return None


def abs_path(rel: str) -> Path:
    path = (settings().media_dir / rel).resolve()
    if settings().media_dir not in path.parents:
        raise MediaError("path outside media directory")
    return path


def store_upload(db: Session, stream: BinaryIO, filename: str, content_type: str | None, uploader_id: int | None, note: str = "") -> MediaAsset:
    mime = content_type or mimetypes.guess_type(filename)[0] or ""
    if mime in ("application/octet-stream", ""):
        mime = mimetypes.guess_type(filename)[0] or ""
    kind = kind_for(mime)
    if kind is None:
        raise MediaError(f"{filename}: only photos and videos can be uploaded")

    folder = datetime.now().strftime("%Y/%m")
    suffix = Path(filename).suffix.lower() or mimetypes.guess_extension(mime) or ""
    rel = f"originals/{folder}/{uuid.uuid4().hex}{suffix}"
    dest = abs_path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)

    limit = settings().max_upload_mb * 1024 * 1024
    digest, size = hashlib.sha256(), 0
    with dest.open("wb") as out:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise MediaError(f"{filename}: larger than {settings().max_upload_mb} MB")
            digest.update(chunk)
            out.write(chunk)

    asset = MediaAsset(
        kind=kind, original_name=Path(filename).name[:256], path=rel, mime=mime,
        size_bytes=size, sha256=digest.hexdigest(), uploader_id=uploader_id, note=note,
        frames=[], tags=[],
    )
    db.add(asset)
    db.flush()
    return asset


def _derived(asset: MediaAsset, name: str) -> tuple[str, Path]:
    rel = f"derived/{asset.id}/{name}"
    path = abs_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    return rel, path


def _save_jpeg(img: Image.Image, px: int, path: Path) -> None:
    copy = img.copy()
    copy.thumbnail((px, px))
    if copy.mode not in ("RGB", "L"):
        copy = copy.convert("RGB")
    copy.save(path, "JPEG", quality=85, optimize=True)


def _process_image(asset: MediaAsset) -> None:
    with Image.open(abs_path(asset.path)) as raw:
        img = ImageOps.exif_transpose(raw)  # phone photos come in sideways otherwise
        asset.width, asset.height = img.size
        rel, path = _derived(asset, "thumb.jpg")
        _save_jpeg(img, THUMB_PX, path)
        asset.thumb_path = rel
        rel, path = _derived(asset, "preview.jpg")
        _save_jpeg(img, PREVIEW_PX, path)
        asset.preview_path = rel


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


def _grab_frame(src: Path, at: float, px: int, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale='min({px},iw)':-2", "-q:v", "4", str(dest)],
        capture_output=True, timeout=120, check=True,
    )


def _process_video(asset: MediaAsset) -> None:
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg is not installed")
    src = abs_path(asset.path)
    info = _ffprobe(src)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("no video stream found")
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        rotation = int(side.get("rotation", 0) or 0)
    if abs(rotation) in (90, 270):
        width, height = height, width
    asset.width, asset.height, asset.duration_s = width, height, duration

    # Poster/thumbnail from 1s in (or the middle of very short clips), then evenly spaced frames.
    poster_at = min(1.0, duration / 2) if duration else 0
    rel, path = _derived(asset, "thumb.jpg")
    _grab_frame(src, poster_at, THUMB_PX, path)
    asset.thumb_path = rel
    rel, path = _derived(asset, "preview.jpg")
    _grab_frame(src, poster_at, PREVIEW_PX, path)
    asset.preview_path = rel
    frames = []
    for i in range(FRAME_COUNT):
        at = duration * (i + 0.5) / FRAME_COUNT if duration else 0
        rel, path = _derived(asset, f"frame_{i + 1}.jpg")
        _grab_frame(src, at, PREVIEW_PX, path)
        frames.append(rel)
    asset.frames = frames


def process(asset: MediaAsset) -> None:
    try:
        if asset.kind == "image":
            _process_image(asset)
        else:
            _process_video(asset)
        asset.processing_status = "ready"
        asset.processing_error = ""
    except Exception as exc:  # keep the upload; show the error in the library
        log.exception("processing media %s failed", asset.id)
        asset.processing_status = "failed"
        asset.processing_error = str(exc)[:500]


def process_pending(db: Session, limit: int = 5) -> int:
    """Run by the background loop: one heavy job at a time keeps RAM low."""
    pending = db.scalars(
        select(MediaAsset).where(MediaAsset.processing_status == "pending").order_by(MediaAsset.id).limit(limit)
    ).all()
    for asset in pending:
        process(asset)
        db.commit()
    return len(pending)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
