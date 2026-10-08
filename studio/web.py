"""The web app: pages and form handlers."""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import batch as batch_mod
from . import calendar_sync, publishers
from . import channels as ch
from . import metrics as metrics_mod
from . import posts as post_svc
from . import public_media as public_media_mod
from .auth import check_csrf, check_proxy, client_ip, current_user, require
from .db import get_db, settings, utcnow
from .media import MediaError, abs_path, human_size, store_upload
from .models import (
    ROLES,
    STATUS_LABELS,
    ApiToken,
    AuditLog,
    ChannelVersion,
    MediaAsset,
    Post,
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
from .timeutil import fmt_local, input_value, local_now, parse_local

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["local"] = fmt_local
templates.env.filters["input_dt"] = input_value
templates.env.filters["size"] = human_size
templates.env.globals["CHANNELS"] = ch.CHANNELS
templates.env.globals["mode_of"] = ch.mode
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS

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


@router.get("/upload")
def upload_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    return render(request, "upload.html", user)


@router.post("/upload")
async def upload(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    if not files:
        flash(request, "Choose at least one photo or video.", "error")
        return back("/upload")
    note = str(form.get("note", "")).strip()
    actor = Actor("user", user)
    stored = []
    try:
        for f in files:
            asset = store_upload(db, f.file, f.filename, f.content_type, user.id, note)
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
    return render(request, "media_detail.html", user, asset=asset, used_in=used_in)


@router.post("/media/{media_id}")
async def media_update(request: Request, media_id: int, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    asset = db.get(MediaAsset, media_id)
    asset.description = str(form.get("description", "")).strip()
    asset.alt_text = str(form.get("alt_text", "")).strip()
    asset.tags = [t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()]
    asset.note = str(form.get("note", "")).strip()
    audit(db, Actor("user", user), "media_edited", "media", asset.id)
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
    return render(request, "review.html", user, posts=waiting)


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


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    tokens = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())).all()
    new_token = request.session.pop("new_token", None)
    s = settings()
    connections = [
        ("Bluesky", bool(s.bluesky_handle and s.bluesky_app_password), "STUDIO_BLUESKY_HANDLE, STUDIO_BLUESKY_APP_PASSWORD"),
        ("Facebook page", s.meta_configured, "STUDIO_META_PAGE_ID, STUDIO_META_PAGE_TOKEN"),
        ("Instagram", s.instagram_configured, "STUDIO_META_IG_USER_ID (plus the page token)"),
        ("Threads", s.threads_configured, "STUDIO_THREADS_USER_ID, STUDIO_THREADS_TOKEN"),
        ("Website news", s.website_configured, f"STUDIO_GITHUB_TOKEN (writes to {s.website_repo})"),
        ("Events calendar", bool(s.calendar_ics_url), s.calendar_ics_url or "STUDIO_CALENDAR_ICS_URL"),
        ("Reminder email", s.mail_backend == "smtp" and bool(s.smtp_user), "STUDIO_SMTP_USER, STUDIO_SMTP_PASSWORD"),
    ]
    return render(request, "settings.html", user, tokens=tokens, new_token=new_token, base_url=s.base_url,
                  connections=connections, media_base=s.media_base_url, is_admin=user.has_role("admin"))


@router.post("/settings/tokens")
async def token_create(request: Request, db: Session = Depends(get_db)):
    user = require(request, db)
    form = await form_with_csrf(request)
    name = str(form.get("name", "")).strip() or "Claude Code"
    token = create_api_token(db, user, name)
    audit(db, Actor("user", user), "token_created", "user", user.id, name=name)
    request.session["new_token"] = token
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
