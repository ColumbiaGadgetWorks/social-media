import io
import zipfile
from datetime import timedelta

from PIL import Image
from sqlalchemy import select
from starlette.testclient import TestClient

from studio import db as db_mod
from studio import posts as post_svc
from studio import scheduler
from studio.models import AuditLog, Post
from studio.timeutil import input_value

from .conftest import csrf_of, jpeg_bytes, login, make_user


def upload(client, files, note="Sam's walnut ukulele", mode="together"):
    csrf = csrf_of(client, "/upload")
    r = client.post("/upload", data={"csrf": csrf, "note": note, "mode": mode},
                    files=[("files", (name, data, mime)) for name, data, mime in files], follow_redirects=False)
    assert r.status_code == 303
    scheduler._media()  # process synchronously in tests
    with db_mod.session_scope() as s:
        return s.scalars(select(Post).order_by(Post.id.desc())).first().id


def future(days=3):
    return input_value(db_mod.utcnow() + timedelta(days=days))


def fill_post(client, post_id, **channels):
    data = {"csrf": csrf_of(client, f"/posts/{post_id}"), "title": "Ukulele", "note": "Sam's walnut ukulele", "pillar": "member_projects"}
    for key, body in channels.items():
        data |= {f"{key}.present": "1", f"{key}.enabled": "1", f"{key}.body": body,
                 f"{key}.hashtags": "#makerspace", f"{key}.scheduled_at": future()}
    return client.post(f"/posts/{post_id}", data=data, follow_redirects=False)


def approve(client, post_id):
    with db_mod.session_scope() as s:
        seen = post_svc.approval_hash(s.get(Post, post_id))
    return client.post(f"/posts/{post_id}/approve", data={"csrf": csrf_of(client, f"/posts/{post_id}"), "seen_hash": seen},
                       follow_redirects=False)


def status_of(post_id):
    with db_mod.session_scope() as s:
        return s.get(Post, post_id).status


def test_pages_need_sign_in(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password_is_rejected(client):
    make_user("sam", "contributor")
    csrf = csrf_of(client, "/login")
    r = client.post("/login", data={"csrf": csrf, "username": "sam", "password": "nope"})
    assert "password don&#39;t match" in r.text


def test_post_without_csrf_is_refused(approver_client):
    r = approver_client.post("/upload", data={"note": "x"}, follow_redirects=False)
    assert r.status_code == 403


def test_upload_creates_post_waiting_for_claude(approver_client):
    post_id = upload(approver_client, [("a.jpg", jpeg_bytes(), "image/jpeg"), ("b.jpg", jpeg_bytes(), "image/jpeg")])
    with db_mod.session_scope() as s:
        post = s.get(Post, post_id)
        assert post.status == "needs_claude"
        assert len(post.media) == 2
        assert all(m.processing_status == "ready" and m.preview_path for m in post.media)
        assert {v.channel for v in post.versions} >= {"instagram", "bluesky", "facebook"}
        assert "tiktok" not in {v.channel for v in post.versions}  # photos only
        media_id = post.media[0].id
    assert approver_client.get(f"/files/{media_id}/thumb").status_code == 200
    assert "Needs Claude" in approver_client.get("/").text


def test_rejects_non_media_upload(approver_client):
    csrf = csrf_of(approver_client, "/upload")
    r = approver_client.post("/upload", data={"csrf": csrf, "mode": "together"},
                             files=[("files", ("notes.txt", b"hello", "text/plain"))])
    assert "only photos and videos" in r.text


def test_full_approval_flow_and_edit_clears_approval(approver_client):
    post_id = upload(approver_client, [("a.jpg", jpeg_bytes(), "image/jpeg")])
    fill_post(approver_client, post_id, bluesky="Sam built a walnut ukulele.", instagram="Sam built a walnut ukulele.")
    r = approver_client.post(f"/posts/{post_id}/submit", data={"csrf": csrf_of(approver_client, f"/posts/{post_id}")})
    assert status_of(post_id) == "in_review", r.text

    assert approve(approver_client, post_id).status_code == 303
    assert status_of(post_id) == "approved"
    with db_mod.session_scope() as s:
        assert post_svc.is_publishable(s.get(Post, post_id)) == (True, "")

    # Any edit after approval sends it back to review.
    fill_post(approver_client, post_id, bluesky="Sam built a walnut ukulele. Changed.", instagram="Sam built a walnut ukulele.")
    assert status_of(post_id) == "in_review"
    with db_mod.session_scope() as s:
        post = s.get(Post, post_id)
        assert post.approved_hash is None
        assert post_svc.is_publishable(post)[0] is False
        actions = s.scalars(select(AuditLog.action).where(AuditLog.entity_id == post_id)).all()
        assert "approval_cleared" in actions


def test_approval_fails_if_post_changed_since_page_load(approver_client):
    post_id = upload(approver_client, [("a.jpg", jpeg_bytes(), "image/jpeg")])
    fill_post(approver_client, post_id, bluesky="Original caption.")
    approver_client.post(f"/posts/{post_id}/submit", data={"csrf": csrf_of(approver_client, f"/posts/{post_id}")})
    with db_mod.session_scope() as s:
        stale = post_svc.approval_hash(s.get(Post, post_id))
        s.get(Post, post_id).version("bluesky").body = "Changed behind the approver's back."
    r = approver_client.post(f"/posts/{post_id}/approve", data={"csrf": csrf_of(approver_client, f"/posts/{post_id}"), "seen_hash": stale})
    assert "changed after you opened it" in r.text
    assert status_of(post_id) == "in_review"


def test_contributor_cannot_approve(app):
    make_user("adam", "admin")
    make_user("sam", "contributor")
    with TestClient(app, client=("192.168.1.20", 50000)) as admin:
        login(admin, "adam")
        post_id = upload(admin, [("a.jpg", jpeg_bytes(), "image/jpeg")])
        fill_post(admin, post_id, bluesky="Caption.")
        admin.post(f"/posts/{post_id}/submit", data={"csrf": csrf_of(admin, f"/posts/{post_id}")})
    with TestClient(app, client=("192.168.1.21", 50000)) as sam:
        login(sam, "sam")
        assert approve(sam, post_id).status_code == 403
    assert status_of(post_id) == "in_review"


def test_validation_blocks_overlong_caption(approver_client):
    post_id = upload(approver_client, [("a.jpg", jpeg_bytes(), "image/jpeg")])
    fill_post(approver_client, post_id, x="word " * 80)
    r = approver_client.post(f"/posts/{post_id}/submit", data={"csrf": csrf_of(approver_client, f"/posts/{post_id}")})
    assert "the limit is 280" in r.text
    assert status_of(post_id) == "needs_claude" or status_of(post_id) == "draft"


def test_batch_page_zip_and_mark_posted(approver_client):
    gps_exif = Image.Exif()
    gps_exif[0x8825] = {1: "N", 2: (38.0, 57.0, 0.0)}  # GPSInfo
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (10, 20, 30)).save(buf, "JPEG", exif=gps_exif)
    post_id = upload(approver_client, [("gps.jpg", buf.getvalue(), "image/jpeg")])
    fill_post(approver_client, post_id, instagram="Look at this build.")
    approver_client.post(f"/posts/{post_id}/submit", data={"csrf": csrf_of(approver_client, f"/posts/{post_id}")})
    approve(approver_client, post_id)

    page = approver_client.get("/batch/instagram")
    assert "Look at this build." in page.text
    z = zipfile.ZipFile(io.BytesIO(approver_client.get("/batch/instagram/download").content))
    names = z.namelist()
    assert "captions.txt" in names and any(n.endswith(".jpg") for n in names)
    img_name = next(n for n in names if n.endswith(".jpg"))
    assert not Image.open(io.BytesIO(z.read(img_name))).getexif().get(0x8825), "GPS must be stripped"
    assert "Look at this build." in z.read("captions.txt").decode()

    with db_mod.session_scope() as s:
        version_id = s.get(Post, post_id).version("instagram").id
    r = approver_client.post(f"/versions/{version_id}/posted",
                             data={"csrf": csrf_of(approver_client, "/batch/instagram"), "url": "https://instagram.com/p/x", "next": "/batch/instagram"},
                             follow_redirects=False)
    assert r.headers["location"] == "/batch/instagram"
    assert status_of(post_id) == "done"


def test_open_redirect_is_ignored(approver_client):
    post_id = upload(approver_client, [("a.jpg", jpeg_bytes(), "image/jpeg")])
    r = approver_client.post(f"/posts/{post_id}/submit",
                             data={"csrf": csrf_of(approver_client, f"/posts/{post_id}"), "next": "//evil.example"},
                             follow_redirects=False)
    assert r.headers["location"] == f"/posts/{post_id}"


def test_token_page_shows_new_token_once(approver_client):
    approver_client.post("/settings/tokens", data={"csrf": csrf_of(approver_client, "/settings"), "name": "desk"},
                        follow_redirects=False)
    first = approver_client.get("/settings").text
    assert "cgw_" in first and "claude mcp add" in first and "be shown again" in first
    assert "be shown again" not in approver_client.get("/settings").text
