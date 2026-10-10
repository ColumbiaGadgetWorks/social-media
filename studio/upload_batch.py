"""Batch uploads: drop many files (or a zip), then sort them into posts on one page.

Files land in the media library straight away, marked as unsorted in their batch. The sort page
groups them into carousels, takes a description per post (and optionally per photo), and on save
turns each group into a post for Claude, or files it in the library. Anything not saved stays in
the batch, so you can come back to it.
"""

from __future__ import annotations

import mimetypes
import secrets
import zipfile
from pathlib import PurePosixPath
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import posts as post_svc
from .db import settings
from .media import MediaError, abs_path, kind_for, process, store_upload
from .models import Event, MediaAsset, PostMedia
from .security import Actor, audit

MAX_ZIP_FILES = 300
MAX_CAROUSEL = 10  # Instagram's limit
ZIP_TYPES = ("application/zip", "application/x-zip-compressed", "application/x-zip")


class BatchError(Exception):
    pass


def new_batch_id() -> str:
    return secrets.token_hex(6)


def _valid_batch(batch: str) -> str:
    if not batch or len(batch) > 24 or not batch.isalnum():
        raise BatchError("That upload batch isn't valid. Reload the upload page.")
    return batch


def _store(db: Session, user_id: int, batch: str, stream: BinaryIO, filename: str, mime: str | None) -> MediaAsset:
    asset = store_upload(db, stream, filename, mime, user_id)
    asset.upload_batch, asset.unsorted = batch, True
    db.flush()
    if asset.kind == "image":
        process(asset)  # quick: thumbnails for the sort page right away (videos go to the background)
    duplicate = db.scalar(select(MediaAsset.id).where(MediaAsset.sha256 == asset.sha256, MediaAsset.id != asset.id).limit(1))
    if duplicate:
        asset.tags = sorted(set(asset.tags or []) | {"duplicate"})
    return asset


def _is_zip(filename: str, mime: str | None) -> bool:
    return filename.lower().endswith(".zip") or (mime or "") in ZIP_TYPES


def add_file(db: Session, actor: Actor, batch: str, stream: BinaryIO, filename: str, mime: str | None) -> tuple[list[MediaAsset], list[str]]:
    """Store one dropped file; a zip is unpacked (photos and videos only). Returns (stored, problems)."""
    batch = _valid_batch(batch)
    stored, problems = [], []
    if _is_zip(filename, mime):
        try:
            archive = zipfile.ZipFile(stream)
        except zipfile.BadZipFile:
            return [], [f"{filename}: not a readable zip file"]
        limit = settings().max_upload_mb * 1024 * 1024
        members = [m for m in archive.infolist() if not m.is_dir()]
        wanted = []
        for m in members:
            name = PurePosixPath(m.filename).name
            if not name or name.startswith(".") or "__MACOSX" in m.filename:
                continue
            if kind_for(mimetypes.guess_type(name)[0] or "") is None:
                continue
            wanted.append((m, name))
        if len(wanted) > MAX_ZIP_FILES:
            return [], [f"{filename}: {len(wanted)} files; split it into zips of {MAX_ZIP_FILES} or fewer"]
        if not wanted:
            problems.append(f"{filename}: no photos or videos inside")
        for m, name in wanted:
            if m.file_size > limit:
                problems.append(f"{name}: larger than {settings().max_upload_mb} MB")
                continue
            try:
                with archive.open(m) as f:  # store_upload stops at the size limit, whatever the header claims
                    stored.append(_store(db, actor.user.id, batch, f, name, None))
            except (MediaError, zipfile.BadZipFile, RuntimeError) as exc:
                problems.append(f"{name}: {exc}")
    else:
        try:
            stored.append(_store(db, actor.user.id, batch, stream, filename, mime))
        except MediaError as exc:
            problems.append(str(exc))
    for asset in stored:
        audit(db, actor, "media_uploaded", "media", asset.id, name=asset.original_name, size=asset.size_bytes,
              batch=batch, via="batch")
    return stored, problems


def item(asset: MediaAsset) -> dict:
    return {
        "id": asset.id, "kind": asset.kind, "name": asset.original_name, "note": asset.note,
        "status": asset.processing_status, "error": asset.processing_error,
        "thumb": f"/files/{asset.id}/thumb" if asset.thumb_path else "",
        "preview": f"/files/{asset.id}/preview" if asset.preview_path else "",
        "original": f"/files/{asset.id}/original",
        "duration": round(asset.duration_s or 0, 1), "width": asset.width, "height": asset.height,
        "duplicate": "duplicate" in (asset.tags or []), "event_id": asset.event_id,
    }


def unsorted(db: Session, batch: str) -> list[MediaAsset]:
    return db.scalars(select(MediaAsset).where(MediaAsset.upload_batch == batch, MediaAsset.unsorted.is_(True))
                      .order_by(MediaAsset.id)).all()


def open_batches(db: Session) -> list[dict]:
    """Batches with files still waiting to be sorted, newest first."""
    rows = db.scalars(select(MediaAsset).where(MediaAsset.unsorted.is_(True)).order_by(MediaAsset.created_at.desc())).all()
    found: dict[str, dict] = {}
    for a in rows:
        b = found.setdefault(a.upload_batch, {"batch": a.upload_batch, "count": 0, "started": a.created_at, "thumb": ""})
        b["count"] += 1
        b["started"] = min(b["started"], a.created_at)
        if not b["thumb"] and a.thumb_path:
            b["thumb"] = f"/files/{a.id}/thumb"
    return list(found.values())


def save(db: Session, actor: Actor, batch: str, groups: list[dict], discard: list[int]) -> dict:
    """Turn the sorted groups into posts (or library entries). Everything is checked before anything changes."""
    batch = _valid_batch(batch)
    waiting = {a.id: a for a in unsorted(db, batch)}
    seen: set[int] = set()
    clean = []
    for n, g in enumerate(groups, 1):
        ids = [int(i) for i in g.get("media", [])]
        if not ids:
            continue
        for i in ids:
            if i not in waiting:
                raise BatchError("Some files were already saved or removed. Reload the page.")
            if i in seen:
                raise BatchError("A file is in two posts. Reload the page.")
            seen.add(i)
        library = bool(g.get("library"))
        videos = sum(1 for i in ids if waiting[i].kind == "video")
        if not library and videos > 1:
            raise BatchError(f"Post {n}: a post can have only one video. Split the videos into their own posts.")
        if not library and len(ids) > MAX_CAROUSEL:
            raise BatchError(f"Post {n}: a carousel can have at most {MAX_CAROUSEL} photos (Instagram's limit).")
        event_id = g.get("event_id")
        event_id = int(event_id) if str(event_id or "").isdigit() and db.get(Event, int(event_id)) else None
        notes = {int(k): str(v).strip()[:2000] for k, v in (g.get("notes") or {}).items() if str(k).isdigit()}
        clean.append({"ids": ids, "library": library, "note": str(g.get("note", "")).strip()[:2000],
                      "pillar": str(g.get("pillar", ""))[:48], "event_id": event_id, "notes": notes})
    gone = [int(i) for i in discard if int(i) in waiting and int(i) not in seen]

    made, filed = [], 0
    for g in clean:
        for i in g["ids"]:
            asset = waiting[i]
            asset.note = g["notes"].get(i) or g["note"]
            asset.event_id = g["event_id"] or asset.event_id
            asset.unsorted = False
        if g["library"]:
            filed += len(g["ids"])
            continue
        note = g["note"] or "; ".join(n for n in (g["notes"].get(i, "") for i in g["ids"]) if n)
        post = post_svc.create_post(db, actor, g["ids"], note=note, pillar=g["pillar"], for_claude=True)
        made.append(post.id)
    for i in gone:
        asset = waiting[i]
        if db.scalar(select(PostMedia.id).where(PostMedia.media_id == i).limit(1)):
            continue
        for rel in [asset.path, asset.thumb_path, asset.preview_path, *(asset.frames or [])]:
            if rel:
                abs_path(rel).unlink(missing_ok=True)
        audit(db, actor, "media_discarded", "media", i, name=asset.original_name, batch=batch)
        db.delete(asset)
    audit(db, actor, "batch_sorted", "media", None, batch=batch, posts=made, library=filed, discarded=len(gone))
    return {"posts": made, "library": filed, "discarded": len(gone), "left": len(waiting) - len(seen) - len(gone)}
