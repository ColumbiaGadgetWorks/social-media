"""The real extension, loaded in Chromium, filling a stand-in TikTok upload page from a live Studio.

Skips when Playwright or a Chromium build isn't available. Set CGW_TEST_CHROMIUM to use a
specific browser binary.
"""

import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio import posts as post_svc
from studio.media import process, store_upload
from studio.models import User
from studio.security import Actor, create_api_token

from .conftest import make_user

playwright = pytest.importorskip("playwright.sync_api")
EXTENSION = Path(__file__).resolve().parent.parent / "extension"
MOCK_PAGE = """<!doctype html><html><body><input type="file" accept="video/*" id="f"><div id="host"></div>
<script>
document.getElementById('f').addEventListener('change', e => {
  const f = e.target.files[0];
  window.__got = {name: f.name, size: f.size, type: f.type};
  setTimeout(() => { document.getElementById('host').innerHTML =
    '<div class="public-DraftEditor-content" contenteditable="true">' + f.name + '</div>'; }, 500);
});
</script></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_studio(app, tmp_path):
    import uvicorn

    make_user("adam", "admin")
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=3",
                    "-c:v", "libx264", "-b:v", "3M", "-pix_fmt", "yuv420p", str(clip)], check=True)
    with db_mod.session_scope() as s:
        adam = s.scalar(select(User))
        actor = Actor("user", adam)
        asset = store_upload(s, io.BytesIO(clip.read_bytes()), "clip.mp4", "video/mp4", adam.id)
        process(asset)
        post = post_svc.create_post(s, actor, [asset.id])
        post_svc.update_post(s, actor, post, versions=[{
            "channel": "tiktok", "enabled": True, "body": "The laser has opinions.", "hashtags": "#makerspace",
            "scheduled_at": db_mod.utcnow() + timedelta(days=3)}])
        post_svc.submit_for_review(s, actor, post)
        post_svc.approve(s, actor, post, post_svc.approval_hash(post))
        token, size = create_api_token(s, adam, "ext", "extension"), asset.size_bytes
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", token, size
    server.should_exit = True
    thread.join(timeout=5)


def test_extension_fills_upload_page(live_studio, tmp_path):
    studio, token, size = live_studio
    ext = tmp_path / "ext"
    shutil.copytree(EXTENSION, ext)
    manifest = json.loads((ext / "manifest.json").read_text())
    manifest["host_permissions"].append("http://127.0.0.1/*")  # what Options grants for your Studio address
    (ext / "manifest.json").write_text(json.dumps(manifest))
    with playwright.sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                str(tmp_path / "profile"), executable_path=os.environ.get("CGW_TEST_CHROMIUM") or None, headless=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}", "--headless=new"])
        except Exception as exc:  # no browser installed here
            pytest.skip(f"Chromium not available: {exc}")
        ctx.route("https://www.tiktok.com/**", lambda r: r.fulfill(status=200, content_type="text/html", body=MOCK_PAGE))
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        sw.evaluate("cfg => chrome.storage.local.set(cfg)", {"studioUrl": studio, "token": token})
        page = ctx.new_page()
        page.goto("https://www.tiktok.com/tiktokstudio/upload")
        item = sw.evaluate("async () => (await api('/api/ext/batch/tiktok')).json()")["items"][0]
        report = sw.evaluate("""async (item) => {
            const [tab] = await chrome.tabs.query({url: 'https://www.tiktok.com/*'});
            return fillTab(tab.id, 'tiktok', item);
        }""", item)
        got = page.evaluate("window.__got")
        caption = page.inner_text(".public-DraftEditor-content")
        ctx.close()
    assert report["done"] == ["file", "caption"] and report["missing"] == []
    assert got == {"name": item["media"][0]["filename"], "size": size, "type": "video/mp4"}
    assert caption.startswith("The laser has opinions.") and caption.rstrip().endswith("#makerspace")
