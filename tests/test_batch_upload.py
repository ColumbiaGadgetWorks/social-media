"""Batch upload: drop files or a zip, sort them into posts and carousels, save."""

import io
import os
import re
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio.media import abs_path
from studio.models import AuditLog, MediaAsset, Post

from .conftest import csrf_of, jpeg_bytes, make_user


def zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def send(c, batch, name, data, mime):
    return c.post("/upload/files", data={"csrf": csrf_of(c, "/upload"), "batch": batch},
                  files={"file": (name, data, mime)})


def batch_of(page: str) -> str:
    return re.search(r'"batch": "([0-9a-f]+)"', page).group(1)


def test_drop_zip_sort_and_save(approver_client):
    c = approver_client
    page = c.get("/upload").text
    assert 'id="drop"' in page
    batch = batch_of(page)
    r = send(c, batch, "a.jpg", jpeg_bytes((1200, 900), (10, 20, 30)), "image/jpeg")
    assert r.status_code == 200 and r.json()["media"][0]["thumb"]  # photos get thumbnails right away
    zipped = zip_of({"shop/b.jpg": jpeg_bytes((800, 800), (200, 10, 10)), "shop/c.jpg": jpeg_bytes((900, 600), (5, 200, 5)),
                     "shop/d.jpg": jpeg_bytes((640, 480), (1, 2, 250)), "__MACOSX/shop/._b.jpg": b"junk",
                     "notes.txt": b"hello", ".DS_Store": b"x"})
    r = send(c, batch, "photos.zip", zipped, "application/zip")
    data = r.json()
    assert [m["name"] for m in data["media"]] == ["b.jpg", "c.jpg", "d.jpg"] and data["problems"] == []

    sort = c.get(f"/upload/batch/{batch}").text
    assert "Sort uploads" in sort and sort.count('"kind": "image"') == 4
    assert batch in c.get("/").text  # dashboard points at the unsorted batch

    with db_mod.session_scope() as s:
        a, b, cc, d = [m.id for m in s.scalars(select(MediaAsset).order_by(MediaAsset.id))]
        assert s.get(MediaAsset, a).unsorted
    csrf = csrf_of(c, f"/upload/batch/{batch}")
    r = c.post(f"/upload/batch/{batch}/save", json={"csrf": csrf, "discard": [d], "groups": [
        {"media": [cc, a], "note": "Sam's ukulele build", "notes": {str(cc): "the rosette close-up"}, "pillar": "member_projects"},
        {"media": [b], "note": "shop overview", "library": True},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["redirect"] == "/posts?status=needs_claude"
    with db_mod.session_scope() as s:
        post = s.scalar(select(Post))
        assert [m.id for m in post.media] == [cc, a]  # carousel order as arranged
        assert post.note == "Sam's ukulele build" and post.pillar == "member_projects" and post.status == "needs_claude"
        assert s.get(MediaAsset, cc).note == "the rosette close-up" and s.get(MediaAsset, a).note == "Sam's ukulele build"
        lib = s.get(MediaAsset, b)
        assert lib.note == "shop overview" and not lib.unsorted
        assert s.get(MediaAsset, d) is None
        assert s.scalar(select(AuditLog).where(AuditLog.action == "media_discarded")) is not None
    assert "Nothing left to sort" in c.get(f"/upload/batch/{batch}").text


def test_save_rules(approver_client, tmp_path):
    import subprocess

    c = approver_client
    batch = batch_of(c.get("/upload").text)
    clips = []
    for i in range(2):
        path = tmp_path / f"v{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1",
                        "-pix_fmt", "yuv420p", str(path)], check=True)
        clips.append(send(c, batch, path.name, path.read_bytes(), "video/mp4").json()["media"][0]["id"])
    photos = [send(c, batch, f"p{i}.jpg", jpeg_bytes((300, 300), (i * 20, 0, 0)), "image/jpeg").json()["media"][0]["id"]
              for i in range(11)]
    csrf = csrf_of(c, f"/upload/batch/{batch}")
    r = c.post(f"/upload/batch/{batch}/save", json={"csrf": csrf, "groups": [{"media": clips, "note": "two clips"}]})
    assert r.status_code == 400 and "only one video" in r.json()["error"]
    r = c.post(f"/upload/batch/{batch}/save", json={"csrf": csrf, "groups": [{"media": photos, "note": "too many"}]})
    assert r.status_code == 400 and "at most 10" in r.json()["error"]
    r = c.post(f"/upload/batch/{batch}/save", json={"csrf": "wrong", "groups": []})
    assert r.status_code == 403
    # Saving part of a batch leaves the rest for later.
    r = c.post(f"/upload/batch/{batch}/save", json={"csrf": csrf, "groups": [{"media": [clips[0]], "note": "a clip"}]})
    assert r.status_code == 200 and r.json()["left"] == 12 and r.json()["redirect"] == f"/upload/batch/{batch}"
    with db_mod.session_scope() as s:
        assert len(s.scalars(select(Post)).all()) == 1
    r = c.post(f"/upload/batch/{batch}/save", json={"csrf": csrf, "groups": [{"media": [clips[0]], "note": "again"}]})
    assert r.status_code == 400  # already saved


def test_bad_zip_and_wrong_type(approver_client):
    c = approver_client
    batch = batch_of(c.get("/upload").text)
    assert send(c, batch, "x.zip", b"not a zip", "application/zip").json()["problems"] == ["x.zip: not a readable zip file"]
    assert "only photos and videos" in send(c, batch, "x.pdf", b"%PDF", "application/pdf").json()["problems"][0]
    assert send(c, "../etc", "a.jpg", jpeg_bytes(), "image/jpeg").status_code == 400


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sort_page_in_a_browser(app, tmp_path):
    """Drop files with the real page, group two into a carousel, describe, save."""
    playwright = pytest.importorskip("playwright.sync_api")
    import uvicorn

    make_user("adam", "admin")
    files = []
    for i, color in enumerate([(200, 60, 30), (30, 120, 200), (40, 160, 60)]):
        p = tmp_path / f"photo{i}.jpg"
        p.write_bytes(jpeg_bytes((900, 700), color))
        files.append(str(p))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    base = f"http://127.0.0.1:{port}"
    try:
        with playwright.sync_playwright() as p:
            try:
                exe = os.environ.get("CGW_TEST_CHROMIUM")
                browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
            except Exception as exc:
                pytest.skip(f"Chromium not available: {exc}")
            page = browser.new_page()
            page.goto(f"{base}/login")
            page.fill("#username", "adam")
            page.fill("#password", "correct horse battery")
            page.click("button[type=submit]")
            page.goto(f"{base}/upload")
            page.set_input_files("#pick", files)
            page.wait_for_selector("#sort-link", state="visible", timeout=15000)
            page.click("#sort-link")
            page.wait_for_selector(".gcard")
            assert page.locator(".gcard").count() == 3
            page.locator(".gcard .tile").nth(0).click()
            page.locator(".gcard .tile").nth(1).click()
            page.click("#b-group")
            assert page.locator(".gcard").count() == 2
            assert "carousel · 2" in page.locator(".gcard").nth(0).inner_text().lower()
            page.click("#view-step")
            page.fill("#f-note", "Sam's ukulele, start to finish")
            page.fill("#f-slide", "rough cut")
            page.click("#s-next")
            page.check("#f-library")
            page.click("#s-next")  # last one: back to the grid
            page.once("dialog", lambda d: d.accept())
            page.click("#b-save")
            page.wait_for_url("**/posts?status=needs_claude", timeout=10000)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    with db_mod.session_scope() as s:
        posts = s.scalars(select(Post)).all()
        assert len(posts) == 1 and len(posts[0].media) == 2 and posts[0].note == "Sam's ukulele, start to finish"
        assert posts[0].media[0].note == "rough cut"
        assert not s.scalars(select(MediaAsset).where(MediaAsset.unsorted.is_(True))).all()
        assert all(Path(abs_path(m.path)).exists() for m in s.scalars(select(MediaAsset)))
