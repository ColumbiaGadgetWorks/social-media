"""The web app: pages and form handlers."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import announcements as ann_mod
from . import batch as batch_mod
from . import calendar_sync, publishers
from . import channels as ch
from . import metrics as metrics_mod
from . import posts as post_svc
from . import public_media as public_media_mod
from . import video as video_mod
from .auth import check_csrf, check_proxy, client_ip, current_user, require
from .config import ip_in
from .db import get_db, settings, utcnow
from .dolibarr import Dolibarr
from .media import MediaError, abs_path, human_size, store_upload
from .models import (
    ROLES,
    STATUS_LABELS,
    Announcement,
    ApiToken,
    AuditLog,
    ChannelVersion,
    Event,
    MediaAsset,
    MusicTrack,
    Post,
    RenderJob,
    User,
)
from .queue import queue_summary
from .scheduler import kick_media
from .security import (
    Actor,
    audit,
    create_api_token,
    csrf_token,
    hash_password,
    verify_password,
)
from .timeutil import fmt_local, input_value, local_now, parse_local, to_local

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["local"] = fmt_local
templates.env.filters["input_dt"] = input_value
templates.env.filters["size"] = human_size
templates.env.globals["CHANNELS"] = ch.CHANNELS
templates.env.globals["mode_of"] = ch.mode
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS

# License name -> does it require a credit line in captions?
MUSIC_LICENSES = {
    "Pixabay Content License": False,
    "CC0 / public domain": False,
    "CC BY 4.0": True,
    "CC BY-SA 4.0": True,
    "Own recording": False,
    "Other (credit required)": True,
}

PILLARS = [
    ("member_projects", "Member projects"),
    ("classes_events", "Classes & events"),
    ("hack_night", "Thursday Open Hack Night"),
    ("tool_spotlight", "Tool spotlight"),
    ("repair_reuse", "Repair & reuse"),
    ("shop_humor", "Shop humor"),
    ("people", "People"),
    ("how_to", "Quick how-tos & safety"),
    ("impact_giving", "Impact & giving"),
    ("community", "Community & partners"),
]
templates.env.globals["PILLARS"] = PILLARS
templates.env.globals["PILLAR_LABELS"] = dict(PILLARS)

_failed_logins: dict[str, list[float]] = defaultdict(list)


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name, {"user": user, "csrf": csrf_token(request.session), "flashes": flashes, **ctx}
    )


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


async def form_with_csrf(request: Request):
    form = await request.form()
    check_csrf(request, form)
    return form


# --- sign in -------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    check_proxy(request)
    if current_user(request, db):
        return back("/")
    return render(request, "login.html", None)


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    proxy_name = check_proxy(request)
    form = await form_with_csrf(request)
    ip = client_ip(request) or "?"
    username = str(form.get("username", "")).strip()
    # Keyed by IP and username: behind the reverse proxy everyone shares one IP.
    key = f"{ip}|{username.lower()}"
    recent = [t for t in _failed_logins[key] if t > time.time() - 900]
    _failed_logins[key] = recent
    if len(recent) >= 10:
        flash(request, "Too many failed sign-ins. Wait 15 minutes and try again.", "error")
        return back("/login")
    user = db.scalar(select(User).where(User.username == username))
    ok = user is not None and user.is_active and verify_password(str(form.get("password", "")), user.password_hash)
    if ok and settings().require_proxy_auth and (proxy_name or "").lower() != (user.proxy_username or user.username).lower():
        ok = False
    if not ok:
        _failed_logins[key].append(time.time())
        flash(request, "That username and password don't match.", "error")
        return back("/login")
    request.session.clear()
    request.session["user_id"] = user.id
    audit(db, Actor("user", user), "signed_in", "user", user.id, ip=ip)
    return back("/")


@router.post("/logout")
async def logout(request: Request):
    await form_with_csrf(request)
    request.session.clear()
    return back("/login")


# --- dashboard -----------------------------------------------------------


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    counts = {status: 0 for status in STATUS_LABELS}
    for status in db.scalars(select(Post.status)):
        counts[status] = counts.get(status, 0) + 1
    upcoming = db.scalars(
        select(ChannelVersion).join(Post).where(
            ChannelVersion.enabled.is_(True), ChannelVersion.publish_state == "pending",
            ChannelVersion.scheduled_at >= utcnow(), Post.status == "approved",
        ).order_by(ChannelVersion.scheduled_at).limit(12)
    ).all()
    failed = db.scalars(
        select(ChannelVersion).join(Post).where(ChannelVersion.publish_state == "failed", Post.status == "approved")
    ).all()
    return render(
        request, "dashboard.html", user, counts=counts, claude=queue_summary(db), upcoming=upcoming,
        failed=failed, batches=batch_mod.overview(db), cycle=batch_mod.cycle_info(),
    )


# --- media ---------------------------------------------------------------


def taken_at_choices(db: Session, asset: MediaAsset) -> list[dict]:
    """Events around when a file was uploaded (5 weeks back to a day after), for "Taken at"."""
    rows = db.scalars(select(Event).where(Event.start >= asset.created_at - timedelta(days=35),
                                          Event.start <= asset.created_at + timedelta(days=1))
                      .order_by(Event.start.desc())).all()
    if asset.event_id and asset.event_id not in {e.id for e in rows} and (current := db.get(Event, asset.event_id)):
        rows.append(current)
    return [{"id": e.id, "title": e.title, "label": f"{to_local(e.start):%a %b} {to_local(e.start).day}"} for e in rows]


def recent_events(db: Session) -> list[dict]:
    """Events from the last 8 days up to tonight, newest first, for the upload form's "Taken at"."""
    now = utcnow()
    rows = db.scalars(select(Event).where(Event.start >= now - timedelta(days=8), Event.start <= now + timedelta(hours=12),
                                          Event.status == "active").order_by(Event.start.desc())).all()
    out = []
    for e in rows:
        start = to_local(e.start)
        is_now = e.start - timedelta(hours=2) <= now <= (e.end or e.start) + timedelta(hours=6)
        out.append({"id": e.id, "title": e.title, "label": "now" if is_now else f"{start:%a %b} {start.day}", "is_now": is_now})
    return out


@router.get("/upload")
def upload_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    return render(request, "upload.html", user, recent_events=recent_events(db))


@router.post("/upload")
async def upload(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    if not files:
        flash(request, "Choose at least one photo or video.", "error")
        return back("/upload")
    note = str(form.get("note", "")).strip()
    taken_at = db.get(Event, int(form["event_id"])) if str(form.get("event_id", "")).isdigit() else None
    actor = Actor("user", user)
    stored = []
    try:
        for f in files:
            asset = store_upload(db, f.file, f.filename, f.content_type, user.id, note)
            if taken_at is not None:
                asset.event_id = taken_at.id
            stored.append(asset)
            audit(db, actor, "media_uploaded", "media", asset.id, name=asset.original_name, size=asset.size_bytes)
    except MediaError as exc:
        db.rollback()
        for asset in stored:  # nothing from a failed batch is kept
            abs_path(asset.path).unlink(missing_ok=True)
        flash(request, str(exc), "error")
        return back("/upload")
    media_ids = [a.id for a in stored]
    mode = form.get("mode", "together")
    pillar = str(form.get("pillar", ""))
    groups = [media_ids] if mode == "together" else [[m] for m in media_ids]
    if mode != "library":
        for group in groups:
            post_svc.create_post(db, actor, group, note=note, pillar=pillar, for_claude=True)
    db.commit()
    kick_media()
    count = len(media_ids)
    if mode == "library":
        flash(request, f"Added {count} file(s) to the media library.")
    else:
        flash(request, f"Uploaded {count} file(s). They're in the queue for the next Claude session.")
    return back("/upload")


@router.get("/media")
def media_list(request: Request, q: str = "", kind: str = "", db: Session = Depends(get_db)):
    user = require(request, db)
    stmt = select(MediaAsset).order_by(MediaAsset.created_at.desc()).limit(200)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(MediaAsset.note.ilike(like), MediaAsset.description.ilike(like),
                              MediaAsset.original_name.ilike(like), MediaAsset.alt_text.ilike(like)))
    if kind in ("image", "video"):
        stmt = stmt.where(MediaAsset.kind == kind)
    return render(request, "media_list.html", user, items=db.scalars(stmt).all(), q=q, kind=kind)


@router.post("/media/new-post")
async def media_new_post(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    ids = [int(x) for x in form.getlist("media_ids")]
    if not ids:
        flash(request, "Select at least one file first.", "error")
        return back("/media")
    post = post_svc.create_post(db, Actor("user", user), ids, note=str(form.get("note", "")),
                                for_claude=form.get("for_claude") == "1", source="library")
    db.commit()
    return back(f"/posts/{post.id}")


@router.get("/media/{media_id}")
def media_detail(request: Request, media_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    asset = db.get(MediaAsset, media_id)
    if asset is None:
        return render(request, "error.html", user, message="That file doesn't exist.")
    used_in = db.scalars(select(Post).join(Post.media_links).where(Post.media_links.any(media_id=media_id))).unique().all()
    jobs = db.scalars(select(RenderJob).where(RenderJob.source_media_id == media_id).order_by(RenderJob.id.desc())).all()
    tracks = db.scalars(select(MusicTrack).order_by(MusicTrack.title)).all()
    post_id = request.query_params.get("post")
    return render(request, "media_detail.html", user, asset=asset, used_in=used_in, jobs=jobs, tracks=tracks,
                  srt=video_mod.to_srt(asset.transcript or []), for_post=int(post_id) if post_id and post_id.isdigit() else None,
                  shapes=list(video_mod.SHAPES), default_end_card=video_mod.DEFAULT_END_CARD,
                  taken_events=taken_at_choices(db, asset))


@router.post("/media/{media_id}/render")
async def media_render(request: Request, media_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    asset = db.get(MediaAsset, media_id)
    post = db.get(Post, int(form["post_id"])) if str(form.get("post_id", "")).isdigit() else None
    raw = {k: form.get(k) for k in ("start", "end", "shape", "fit", "music_track_id", "music_volume", "end_card_text")}
    raw |= {k: form.get(k) == "1" for k in ("subtitles", "keep_audio", "logo", "end_card")}
    try:
        video_mod.request(db, Actor("user", user), asset, raw, post=post)
    except video_mod.RenderError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return back(f"/media/{media_id}" + (f"?post={post.id}" if post else ""))
    db.commit()
    kick_media()
    flash(request, "Render queued. It usually takes a minute or two; reload to see it." +
          (" The result will replace this video in the post." if post else ""))
    return back(f"/media/{media_id}" + (f"?post={post.id}" if post else ""))


@router.post("/media/{media_id}/transcript")
async def media_transcript(request: Request, media_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    asset = db.get(MediaAsset, media_id)
    try:
        asset.transcript = video_mod.from_srt(str(form.get("srt", "")))
    except ValueError:
        flash(request, "That doesn't look like subtitle text. Keep the numbered blocks and the time lines.", "error")
        return back(f"/media/{media_id}")
    asset.transcript_status = "done" if asset.transcript else asset.transcript_status
    audit(db, Actor("user", user), "transcript_edited", "media", asset.id, segments=len(asset.transcript))
    flash(request, "Subtitles saved.")
    return back(f"/media/{media_id}")


@router.get("/music")
def music_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    tracks = db.scalars(select(MusicTrack).order_by(MusicTrack.title)).all()
    return render(request, "music.html", user, tracks=tracks, licenses=MUSIC_LICENSES)


@router.post("/music")
async def music_upload(request: Request, db: Session = Depends(get_db)):
    user = require(request, db, "editor")
    form = await form_with_csrf(request)
    upload = form.get("file")
    license_ = str(form.get("license", ""))
    if not getattr(upload, "filename", "") or license_ not in MUSIC_LICENSES:
        flash(request, "Choose an audio file and its license.", "error")
        return back("/music")
    credit = str(form.get("credit_line", "")).strip()
    if MUSIC_LICENSES[license_] and not credit:
        flash(request, f"{license_} requires a credit line, e.g. \"Music: Title by Artist (CC BY 4.0)\".", "error")
        return back("/music")
    try:
        track = video_mod.store_music(
            db, upload.file, upload.filename, title=str(form.get("title", "")).strip() or upload.filename,
            artist=str(form.get("artist", "")).strip(), license=license_, credit_line=credit,
            source_url=str(form.get("source_url", "")).strip(), mood=str(form.get("mood", "")).strip())
    except video_mod.RenderError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return back("/music")
    audit(db, Actor("user", user), "music_added", "music", track.id, title=track.title, license=license_)
    flash(request, f"Added {track.title}.")
    return back("/music")


@router.get("/music/{track_id}/file")
def music_file(request: Request, track_id: int, db: Session = Depends(get_db)):
    require(request, db)
    track = db.get(MusicTrack, track_id)
    return FileResponse(abs_path(track.path)) if track else Response(status_code=404)


@router.post("/media/{media_id}")
async def media_update(request: Request, media_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    asset = db.get(MediaAsset, media_id)
    asset.description = str(form.get("description", "")).strip()
    asset.alt_text = str(form.get("alt_text", "")).strip()
    asset.tags = [t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()]
    asset.note = str(form.get("note", "")).strip()
    event_id = str(form.get("event_id", ""))
    asset.event_id = int(event_id) if event_id.isdigit() and db.get(Event, int(event_id)) else None
    audit(db, Actor("user", user), "media_edited", "media", asset.id, event_id=asset.event_id)
    flash(request, "Saved.")
    return back(f"/media/{media_id}")


@router.get("/files/{media_id}/{variant}")
def media_file(request: Request, media_id: int, variant: str, db: Session = Depends(get_db)):
    require(request, db)
    asset = db.get(MediaAsset, media_id)
    if asset is None:
        return Response(status_code=404)
    if variant == "export" and asset.kind == "image":
        # Re-encoded without phone metadata (GPS, device), for posting by hand.
        return Response(batch_mod.export_image(asset.path), media_type="image/jpeg")
    if variant in ("original", "export"):
        rel = asset.path
    elif variant in ("thumb", "preview"):
        rel = getattr(asset, f"{variant}_path")
    elif variant.startswith("frame") and variant[5:].isdigit():
        idx = int(variant[5:]) - 1
        rel = asset.frames[idx] if 0 <= idx < len(asset.frames) else None
    else:
        rel = None
    if not rel:
        return Response(status_code=404)
    return FileResponse(abs_path(rel), headers={"Cache-Control": "private, max-age=3600"})


# --- posts ---------------------------------------------------------------


@router.get("/posts")
def posts_list(request: Request, status: str = "", db: Session = Depends(get_db)):
    user = require(request, db)
    stmt = select(Post).order_by(Post.updated_at.desc()).limit(200)
    if status in STATUS_LABELS:
        stmt = stmt.where(Post.status == status)
    return render(request, "posts_list.html", user, posts=db.scalars(stmt).all(), status=status)


@router.get("/review")
def review(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    waiting = db.scalars(select(Post).where(Post.status == "in_review").order_by(Post.submitted_at)).all()
    emails = db.scalars(select(Announcement).where(Announcement.status == "in_review")).all()
    return render(request, "review.html", user, posts=waiting, emails=emails)


def _load_post(db: Session, post_id: int) -> Post | None:
    return db.get(Post, post_id)


@router.get("/posts/{post_id}")
def post_page(request: Request, post_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    post = _load_post(db, post_id)
    if post is None:
        return render(request, "error.html", user, message="That post doesn't exist.")
    if post.status not in ("approved", "done"):
        post_svc.ensure_versions(post)
        db.flush()
    found = post_svc.problems(post)
    history = db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "post", AuditLog.entity_id == post.id).order_by(AuditLog.at.desc())
    ).all()
    compatible = {c.key for c in post_svc.compatible_channels(post)}
    snapshots = {v.id: metrics_mod.latest(db, v.id) for v in post.versions if v.publish_state == "published"}
    return render(
        request, "post_edit.html", user, post=post, problems=found,
        history=history, compatible=compatible, seen_hash=post_svc.approval_hash(post),
        snapshots=snapshots, event_when=calendar_sync.event_when,
    )


@router.post("/posts/{post_id}")
async def post_save(request: Request, post_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    post = _load_post(db, post_id)
    versions = []
    for key in ch.CHANNELS:
        if f"{key}.present" not in form:
            continue
        try:
            when = parse_local(form.get(f"{key}.scheduled_at"))
        except ValueError:
            flash(request, f"{ch.CHANNELS[key].label}: that date and time isn't valid.", "error")
            return back(f"/posts/{post_id}")
        versions.append({
            "channel": key,
            "enabled": form.get(f"{key}.enabled") == "1",
            "title": str(form.get(f"{key}.title", "")),
            "body": str(form.get(f"{key}.body", "")).replace("\r\n", "\n"),
            "hashtags": str(form.get(f"{key}.hashtags", "")),
            "scheduled_at": when,
        })
    was_approved = post.status == "approved"
    try:
        changed = post_svc.update_post(
            db, Actor("user", user), post,
            title=str(form.get("title", post.title)), note=str(form.get("note", post.note)),
            pillar=str(form.get("pillar", post.pillar)), versions=versions,
        )
    except post_svc.PostError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return back(f"/posts/{post_id}")
    if form.get("action") == "submit":
        try:
            post_svc.submit_for_review(db, Actor("user", user), post)
            flash(request, "Saved and sent for review.")
        except post_svc.PostError as exc:
            flash(request, f"Saved, but not sent for review. {exc}", "error")
    elif changed and was_approved:
        flash(request, "Saved. The approval was cleared, so this post needs approving again.")
    elif changed:
        flash(request, "Saved.")
    return back(f"/posts/{post_id}")


def _next(form, default: str) -> str:
    target = str(form.get("next") or "")
    return target if target.startswith("/") and not target.startswith("//") else default


async def _post_action(request: Request, post_id: int, db: Session, role: str, fn) -> RedirectResponse:
    user = require(request, db, role)
    form = await form_with_csrf(request)
    post = _load_post(db, post_id)
    try:
        message = fn(Actor("user", user), post, form)
    except post_svc.PostError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return back(f"/posts/{post_id}")
    flash(request, message)
    return back(_next(form, f"/posts/{post_id}"))


@router.post("/posts/{post_id}/submit")
async def post_submit(request: Request, post_id: int, db: Session = Depends(get_db)):
    def fn(actor, post, form):
        post_svc.submit_for_review(db, actor, post)
        return "Sent for review."
    return await _post_action(request, post_id, db, "contributor", fn)


@router.post("/posts/{post_id}/approve")
async def post_approve(request: Request, post_id: int, db: Session = Depends(get_db)):
    def fn(actor, post, form):
        post_svc.approve(db, actor, post, str(form.get("seen_hash", "")))
        return "Approved. Automatic channels will publish on time; the rest are on the batch pages."
    return await _post_action(request, post_id, db, "approver", fn)


@router.post("/posts/{post_id}/changes")
async def post_changes(request: Request, post_id: int, db: Session = Depends(get_db)):
    def fn(actor, post, form):
        reject = form.get("reject") == "1"
        post_svc.request_changes(db, actor, post, str(form.get("comment", "")).strip(), reject=reject)
        return "Rejected." if reject else "Sent back for changes."
    return await _post_action(request, post_id, db, "approver", fn)


@router.post("/posts/{post_id}/claude")
async def post_to_claude(request: Request, post_id: int, db: Session = Depends(get_db)):
    def fn(actor, post, form):
        post_svc.send_to_claude(db, actor, post)
        return "Queued for the next Claude session."
    return await _post_action(request, post_id, db, "contributor", fn)


async def _version_action(request: Request, version_id: int, db: Session, role: str, fn) -> RedirectResponse:
    user = require(request, db, role)
    form = await form_with_csrf(request)
    version = db.get(ChannelVersion, version_id)
    try:
        message = fn(Actor("user", user), version, form)
    except post_svc.PostError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return back(_next(form, f"/posts/{version.post_id}"))
    flash(request, message)
    return back(_next(form, f"/posts/{version.post_id}"))


@router.post("/versions/{version_id}/posted")
async def version_posted(request: Request, version_id: int, db: Session = Depends(get_db)):
    def fn(actor, version, form):
        post_svc.mark_posted(db, actor, version, str(form.get("url", "")))
        return f"Marked as scheduled on {ch.CHANNELS[version.channel].label}."
    return await _version_action(request, version_id, db, "contributor", fn)


@router.post("/versions/{version_id}/url")
async def version_url(request: Request, version_id: int, db: Session = Depends(get_db)):
    def fn(actor, version, form):
        post_svc.set_external_url(db, actor, version, str(form.get("url", "")))
        return "Link saved."
    return await _version_action(request, version_id, db, "contributor", fn)


@router.post("/versions/{version_id}/skip")
async def version_skip(request: Request, version_id: int, db: Session = Depends(get_db)):
    def fn(actor, version, form):
        post_svc.skip_version(db, actor, version)
        return f"Skipped {ch.CHANNELS[version.channel].label}."
    return await _version_action(request, version_id, db, "editor", fn)


@router.post("/versions/{version_id}/retry")
async def version_retry(request: Request, version_id: int, db: Session = Depends(get_db)):
    def fn(actor, version, form):
        ok, reason = post_svc.is_publishable(version.post)
        if not ok:
            raise post_svc.PostError(f"Can't retry: {reason}.")
        publishers.retry(db, actor, version)
        return "Will retry on the next pass (within a minute)."
    return await _version_action(request, version_id, db, "editor", fn)


# --- email announcements ---------------------------------------------------


@router.get("/announcements")
def announcements_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    rows = db.scalars(select(Announcement).order_by(Announcement.send_at.desc().nulls_last()).limit(60)).all()
    return render(request, "announcements.html", user, rows=rows, dolibarr=settings().dolibarr_configured)


@router.post("/announcements/new")
async def announcement_new(request: Request, db: Session = Depends(get_db)):
    user = require(request, db, "editor")
    form = await form_with_csrf(request)
    try:
        when = parse_local(form.get("send_at"))
        if when is None:
            raise ValueError
        ann = ann_mod.create_special(db, Actor("user", user), str(form.get("subject", "")), when)
    except (ValueError, ann_mod.AnnouncementError) as exc:
        db.rollback()
        flash(request, str(exc) or "Enter a subject and a send time.", "error")
        return back("/announcements")
    return back(f"/announcements/{ann.id}")


def _recipient_count() -> tuple[int | None, str]:
    if not settings().dolibarr_configured:
        return None, "Dolibarr isn't connected, so the list can't be read yet."
    try:
        return len(Dolibarr().recipients()), ""
    except Exception as exc:
        return None, f"Couldn't read the list from Dolibarr: {exc}"


@router.get("/announcements/{ann_id}")
def announcement_page(request: Request, ann_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    ann = db.get(Announcement, ann_id)
    if ann is None:
        return render(request, "error.html", user, message="That email doesn't exist.")
    preview_html, _ = ann_mod.render(ann, "#unsubscribe")
    count, count_note = _recipient_count() if ann.status in ("in_review", "approved") else (None, "")
    history = db.scalars(select(AuditLog).where(AuditLog.entity_type == "announcement", AuditLog.entity_id == ann.id)
                         .order_by(AuditLog.at.desc())).all()
    return render(request, "announcement_edit.html", user, a=ann, preview_html=preview_html,
                  problems=ann_mod.problems(db, ann), seen_hash=ann_mod.content_hash(ann), history=history,
                  count=count, count_note=count_note, same_month=ann_mod.same_month_count(db, ann),
                  editable=ann.status not in ("sent", "cancelled"))


@router.post("/announcements/{ann_id}")
async def announcement_save(request: Request, ann_id: int, db: Session = Depends(get_db)):
    user = require(request, db, "editor")
    form = await form_with_csrf(request)
    ann = db.get(Announcement, ann_id)
    items = []
    for i, item in enumerate(ann.items):
        if form.get(f"item{i}.remove") == "1":
            continue
        items.append({**item, "blurb": str(form.get(f"item{i}.blurb", item.get("blurb", ""))).strip()})
    try:
        when = parse_local(form.get("send_at"))
        ann_mod.update(db, Actor("user", user), ann, subject=str(form.get("subject", "")).strip(),
                       preheader=str(form.get("preheader", "")).strip(),
                       intro=str(form.get("intro", "")).replace("\r\n", "\n").strip(),
                       closing=str(form.get("closing", "")).replace("\r\n", "\n").strip(), items=items, send_at=when)
        if form.get("action") == "submit":
            ann_mod.submit(db, Actor("user", user), ann)
            flash(request, "Saved and sent for review.")
        else:
            flash(request, "Saved.")
    except (ValueError, ann_mod.AnnouncementError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return back(f"/announcements/{ann_id}")


async def _ann_action(request: Request, ann_id: int, db: Session, role: str, fn):
    user = require(request, db, role)
    form = await form_with_csrf(request)
    ann = db.get(Announcement, ann_id)
    try:
        flash(request, fn(Actor("user", user), ann, form))
    except ann_mod.AnnouncementError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return back(f"/announcements/{ann_id}")


@router.post("/announcements/{ann_id}/approve")
async def announcement_approve(request: Request, ann_id: int, db: Session = Depends(get_db)):
    def fn(actor, ann, form):
        ann_mod.approve(db, actor, ann, str(form.get("seen_hash", "")))
        return f"Approved. It sends {fmt_local(ann.send_at)}."
    return await _ann_action(request, ann_id, db, "approver", fn)


@router.post("/announcements/{ann_id}/changes")
async def announcement_changes(request: Request, ann_id: int, db: Session = Depends(get_db)):
    def fn(actor, ann, form):
        cancel = form.get("cancel") == "1"
        ann_mod.send_back(db, actor, ann, str(form.get("comment", "")).strip(), cancel=cancel)
        return "Cancelled; it won't be sent." if cancel else "Sent back for changes."
    return await _ann_action(request, ann_id, db, "approver", fn)


@router.post("/announcements/{ann_id}/override")
async def announcement_override(request: Request, ann_id: int, db: Session = Depends(get_db)):
    def fn(actor, ann, form):
        ann_mod.set_override(db, actor, ann, form.get("on") == "1")
        return "Monthly limit overridden for this email." if ann.override_cap else "Override removed."
    return await _ann_action(request, ann_id, db, "admin", fn)


@router.post("/announcements/{ann_id}/test")
async def announcement_test(request: Request, ann_id: int, db: Session = Depends(get_db)):
    def fn(actor, ann, form):
        if not actor.user.email:
            raise ann_mod.AnnouncementError("Add your email address under Users first.")
        ann_mod.send_test(ann, actor.user.email)
        audit(db, actor, "test_sent", "announcement", ann.id)
        return f"Test sent to {actor.user.email}."
    return await _ann_action(request, ann_id, db, "editor", fn)


# --- unsubscribe (public) --------------------------------------------------


@router.get("/u/{token}")
def unsubscribe_page(request: Request, token: str):
    email = ann_mod.email_from_token(token)
    return templates.TemplateResponse(request, "unsubscribe.html", {"email": email, "done": False, "token": token},
                                      status_code=200 if email else 404)


@router.post("/u/{token}")
def unsubscribe_now(request: Request, token: str, db: Session = Depends(get_db)):
    """The page's button and RFC 8058 one-click requests (List-Unsubscribe=One-Click) both land here."""
    email = ann_mod.email_from_token(token)
    if email is None:
        return Response("That unsubscribe link isn't valid.", status_code=404)
    ann_mod.record_unsubscribe(db, email)
    return templates.TemplateResponse(request, "unsubscribe.html", {"email": email, "done": True, "token": token})


# --- insights --------------------------------------------------------------


def _bars(groups: dict, labels: dict) -> list[dict]:
    rows = sorted(groups.items(), key=lambda kv: kv[1]["avg_engagement"], reverse=True)
    top = max((v["avg_engagement"] for _, v in rows), default=0) or 1
    return [{"label": labels.get(k, k.replace("_", " ").capitalize()), "value": v["avg_engagement"],
             "posts": v["posts"], "pct": round(100 * v["avg_engagement"] / top, 1)} for k, v in rows]


@router.get("/insights")
def insights(request: Request, days: int = 90, db: Session = Depends(get_db)):
    user = require(request, db)
    days = days if days in (30, 90, 365) else 90
    data = metrics_mod.summary(db, days=days)
    channel_labels = {k: c.label for k, c in ch.CHANNELS.items()}
    top = sorted((p for p in data["posts"] if p["metrics"]), key=lambda p: p["engagement"], reverse=True)[:15]
    return render(request, "insights.html", user, days=days, measured=sum(1 for p in data["posts"] if p["metrics"]),
                  published=len(data["posts"]), top=top, channel_labels=channel_labels,
                  charts=[("By pillar", _bars(data["by_pillar"], dict(PILLARS) | {"none": "No pillar"})),
                          ("By channel", _bars(data["by_channel"], channel_labels)),
                          ("By format", _bars(data["by_format"], {}))])


# --- events --------------------------------------------------------------


@router.get("/events")
def events_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    events = calendar_sync.upcoming_events(db, days=settings().calendar_lookahead_days)
    one_off = [e for e in events if not e.recurring or e.posts]
    series: dict[str, list] = {}
    for e in events:
        if e.recurring and not e.posts:
            series.setdefault(e.title, []).append(e)
    return render(request, "events.html", user, events=one_off, series=series, when=calendar_sync.event_when,
                  ics=settings().calendar_ics_url)


# --- batch days ----------------------------------------------------------


@router.get("/batch")
def batch_overview(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    return render(request, "batch_overview.html", user, rows=batch_mod.overview(db), cycle=batch_mod.cycle_info())


@router.get("/batch/{channel}")
def batch_channel(request: Request, channel: str, db: Session = Depends(get_db)):
    user = require(request, db)
    if channel not in ch.CHANNELS or ch.mode(channel) not in ch.MANUAL_MODES:
        return render(request, "error.html", user, message="That channel doesn't have a batch page.")
    return render(request, "batch_channel.html", user, channel=ch.CHANNELS[channel],
                  items=batch_mod.items(db, channel), cycle=batch_mod.cycle_info())


@router.get("/batch/{channel}/download")
def batch_download(request: Request, channel: str, db: Session = Depends(get_db)):
    user = require(request, db)
    if channel not in ch.CHANNELS or ch.mode(channel) not in ch.MANUAL_MODES:
        return Response(status_code=404)
    data, count = batch_mod.build_zip(db, channel)
    audit(db, Actor("user", user), "batch_downloaded", "channel", None, channel=channel, items=count)
    name = f"cgw-{channel}-batch-{local_now():%Y-%m-%d}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --- settings and users --------------------------------------------------


def lan_address(request: Request) -> str | None:
    """The Studio's LAN address for Claude Code and the extension: STUDIO_LAN_URL, or the address in
    the browser bar when this page was opened directly on the LAN (Docker hides the host IP and port)."""
    s = settings()
    if s.lan_url:
        return s.lan_url
    ip = client_ip(request)
    proxied = ip_in(ip, s.trusted_proxies) or "x-forwarded-for" in request.headers or "forwarded" in request.headers
    if proxied or not ip_in(ip, s.mcp_allowed_networks):
        return None
    return f"{request.url.scheme}://{request.url.netloc}"


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    tokens = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())).all()
    new_token = request.session.pop("new_token", None)
    new_scope = request.session.pop("new_token_scope", "mcp")
    s = settings()
    connections = [
        ("Bluesky", bool(s.bluesky_handle and s.bluesky_app_password), "STUDIO_BLUESKY_HANDLE, STUDIO_BLUESKY_APP_PASSWORD"),
        ("Facebook page", s.meta_configured, "STUDIO_META_PAGE_ID, STUDIO_META_PAGE_TOKEN"),
        ("Instagram", s.instagram_configured, "STUDIO_META_IG_USER_ID (plus the page token)"),
        ("Threads", s.threads_configured, "STUDIO_THREADS_USER_ID, STUDIO_THREADS_TOKEN"),
        ("Website news", s.website_configured, f"STUDIO_GITHUB_TOKEN (writes to {s.website_repo})"),
        ("Events calendar", bool(s.calendar_ics_url), s.calendar_ics_url or "STUDIO_CALENDAR_ICS_URL"),
        ("Reminder email", s.mail_backend == "smtp" and bool(s.smtp_user), "STUDIO_SMTP_USER, STUDIO_SMTP_PASSWORD"),
        ("Email list (Dolibarr)", s.dolibarr_configured, "STUDIO_DOLIBARR_URL, STUDIO_DOLIBARR_API_KEY"),
        ("Subtitles (Whisper)", bool(s.whisper_model), f"STUDIO_WHISPER_MODEL={s.whisper_model or '(off)'}"),
    ]
    lan = lan_address(request)
    return render(request, "settings.html", user, tokens=tokens, new_token=new_token, new_scope=new_scope,
                  base_url=s.base_url, lan=lan, lan_configured=bool(s.lan_url),
                  connections=connections, media_base=s.media_base_url, is_admin=user.has_role("admin"))


@router.post("/settings/tokens")
async def token_create(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    scope = "extension" if form.get("scope") == "extension" else "mcp"
    name = str(form.get("name", "")).strip() or ("Browser extension" if scope == "extension" else "Claude Code")
    token = create_api_token(db, user, name, scope)
    audit(db, Actor("user", user), "token_created", "user", user.id, name=name, scope=scope)
    request.session["new_token"] = token
    request.session["new_token_scope"] = scope
    return back("/settings")


@router.post("/settings/tokens/{token_id}/revoke")
async def token_revoke(request: Request, token_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    await form_with_csrf(request)
    token = db.get(ApiToken, token_id)
    if token and (token.user_id == user.id or user.has_role("admin")):
        token.revoked_at = utcnow()
        audit(db, Actor("user", user), "token_revoked", "user", token.user_id, name=token.name)
        flash(request, "Token revoked.")
    return back("/settings")


@router.post("/settings/password")
async def password_change(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    if not verify_password(str(form.get("current", "")), user.password_hash):
        flash(request, "Your current password isn't right.", "error")
    elif len(str(form.get("new", ""))) < 10:
        flash(request, "Use at least 10 characters for the new password.", "error")
    else:
        user.password_hash = hash_password(str(form.get("new")))
        audit(db, Actor("user", user), "password_changed", "user", user.id)
        flash(request, "Password changed.")
    return back("/settings")


@router.get("/admin/users")
def users_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db, "admin")
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.username)).all(), roles=ROLES)


@router.post("/admin/users")
async def users_create(request: Request, db: Session = Depends(get_db)):
    admin = require(request, db, "admin")
    form = await form_with_csrf(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    role = str(form.get("role", "contributor"))
    if not username or len(password) < 10 or role not in ROLES:
        flash(request, "Enter a username, a role, and a password of at least 10 characters.", "error")
        return back("/admin/users")
    if db.scalar(select(User).where(User.username == username)):
        flash(request, "That username is taken.", "error")
        return back("/admin/users")
    new = User(username=username, display_name=str(form.get("display_name", "")).strip(),
               email=str(form.get("email", "")).strip(), role=role, password_hash=hash_password(password),
               proxy_username=str(form.get("proxy_username", "")).strip() or None)
    db.add(new)
    db.flush()
    audit(db, Actor("user", admin), "user_created", "user", new.id, role=role)
    flash(request, f"Added {username}.")
    return back("/admin/users")


@router.post("/admin/users/{user_id}")
async def users_update(request: Request, user_id: int, db: Session = Depends(get_db)):
    admin = require(request, db, "admin")
    form = await form_with_csrf(request)
    target = db.get(User, user_id)
    role = str(form.get("role", target.role))
    if role in ROLES:
        target.role = role
    target.email = str(form.get("email", target.email)).strip()
    target.proxy_username = str(form.get("proxy_username", "")).strip() or None
    target.is_active = form.get("is_active") == "1" or target.id == admin.id
    if form.get("password"):
        if len(str(form.get("password"))) < 10:
            flash(request, "Use at least 10 characters for the password.", "error")
            return back("/admin/users")
        target.password_hash = hash_password(str(form.get("password")))
    audit(db, Actor("user", admin), "user_updated", "user", target.id, role=target.role, active=target.is_active)
    flash(request, f"Saved {target.username}.")
    return back("/admin/users")


@router.get("/activity")
def activity(request: Request, db: Session = Depends(get_db)):
    user = require(request, db, "editor")
    rows = db.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(300)).all()
    return render(request, "activity.html", user, rows=rows)


@router.get("/m/{media_id}/{variant}/{filename}")
def public_media(media_id: int, variant: str, filename: str, db: Session = Depends(get_db)):
    """Unauthenticated, signed links for platforms that fetch media (Meta). Approved posts only."""
    sig = filename.rsplit(".", 1)[0]
    if not public_media_mod.allowed(db, media_id, variant, sig):
        return Response(status_code=404)
    asset = db.get(MediaAsset, media_id)
    if variant == "video":
        if asset.kind != "video":
            return Response(status_code=404)
        return FileResponse(abs_path(asset.path), media_type=asset.mime)
    if asset.kind != "image":
        return Response(status_code=404)
    return Response(public_media_mod.render(asset, variant), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/healthz")
def healthz():
    return {"ok": True}
