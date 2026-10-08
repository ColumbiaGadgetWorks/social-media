import io
from datetime import timedelta

from PIL import Image
from sqlalchemy import select
from starlette.testclient import TestClient

from studio import db as db_mod
from studio import posts as post_svc
from studio.media import process, store_upload
from studio.models import Post, User
from studio.security import Actor, create_api_token

from .conftest import make_user


def setup_post(channel="x"):
    make_user("adam", "admin")
    with db_mod.session_scope() as s:
        adam = s.scalar(select(User))
        actor = Actor("user", adam)
        exif = Image.Exif()
        exif[0x8825] = {1: "N"}
        buf = io.BytesIO()
        Image.new("RGB", (400, 300)).save(buf, "JPEG", exif=exif)
        asset = store_upload(s, io.BytesIO(buf.getvalue()), "a.jpg", "image/jpeg", adam.id)
        process(asset)
        post = post_svc.create_post(s, actor, [asset.id], note="Ukulele")
        post_svc.update_post(s, actor, post, versions=[{"channel": channel, "enabled": True, "body": "Sam's ukulele",
                                                        "scheduled_at": db_mod.utcnow() + timedelta(days=2)}])
        post_svc.submit_for_review(s, actor, post)
        post_svc.approve(s, actor, post, post_svc.approval_hash(post))
        return post.id, asset.id, create_api_token(s, adam, "ext", "extension"), create_api_token(s, adam, "mcp", "mcp")


def test_extension_api_flow(app):
    post_id, media_id, ext_token, mcp_token = setup_post()
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        auth = {"Authorization": f"Bearer {ext_token}"}
        assert c.get("/api/ext/batch", headers={"Authorization": f"Bearer {mcp_token}"}).status_code == 401
        overview = c.get("/api/ext/batch", headers=auth).json()
        assert {"key": "x", "label": "X", "count": 1, "mode": "batch"} in overview["channels"]
        batch = c.get("/api/ext/batch/x", headers=auth).json()
        item = batch["items"][0]
        assert item["caption"] == "Sam's ukulele" and item["media"][0]["filename"].endswith(".jpg")
        img = c.get(item["media"][0]["url"], headers=auth)
        assert img.status_code == 200 and not Image.open(io.BytesIO(img.content)).getexif().get(0x8825)
        assert c.get("/api/ext/batch/bluesky", headers=auth).status_code == 404  # automatic channel
        r = c.post(f"/api/ext/versions/{item['version_id']}/scheduled", headers=auth, json={"url": "https://x.com/cgw/1"})
        assert r.json() == {"ok": True, "post_status": "done"}
    with db_mod.session_scope() as s:
        assert s.get(Post, post_id).version("x").external_url == "https://x.com/cgw/1"


def test_extension_api_is_lan_only(app):
    _post_id, _media, ext_token, _mcp = setup_post()
    auth = {"Authorization": f"Bearer {ext_token}"}
    with TestClient(app, client=("203.0.113.9", 50000)) as c:
        assert c.get("/api/ext/batch", headers=auth).status_code == 403
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert c.get("/api/ext/batch", headers={**auth, "X-Forwarded-For": "1.2.3.4"}).status_code == 403


def test_extension_token_cannot_use_mcp(app):
    _post_id, _media, ext_token, _mcp = setup_post()
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        r = c.post("/mcp", headers={"Authorization": f"Bearer {ext_token}", "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
