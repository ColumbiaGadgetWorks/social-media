"""Website news posts: one commit to the Hugo repo (Markdown + photos); Cloudflare deploys it.

Files follow the site's own conventions (see the website repo's README):
  content/news/<slug>.md        front matter: title, date, description, image, imageAlt
  assets/img/<slug>[-n].jpg     long edge 1600 px or less; Hugo makes WebP sizes
Extra photos go below the text with the site's {{< img >}} shortcode. The site doesn't
build future-dated posts, so the Studio commits at the scheduled time.
"""

from __future__ import annotations

import base64
import io
import json
import re

import httpx
from PIL import Image, ImageOps

from ..db import settings
from ..media import abs_path
from ..models import ChannelVersion
from ..timeutil import to_local
from .bluesky import PublishError

API = "https://api.github.com"
IMAGE_EDGE = 1600


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].rstrip("-") or "news"


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid double-quoted YAML string


def description_from(body: str, limit: int = 155) -> str:
    text = re.sub(r"\{\{<.*?>\}\}", "", body)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`#>]", "", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def _image_bytes(rel: str) -> bytes:
    with Image.open(abs_path(rel)) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
    img.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue()


def build_files(version: ChannelVersion, slug: str) -> dict[str, bytes]:
    """Path -> content for the commit."""
    post = version.post
    images = [m for m in post.media if m.kind == "image"][:6]
    files: dict[str, bytes] = {}
    names = []
    for i, m in enumerate(images):
        name = f"{slug}.jpg" if i == 0 else f"{slug}-{i + 1}.jpg"
        files[f"assets/img/{name}"] = _image_bytes(m.path)
        names.append((name, m.alt_text or m.description or ""))
    date = to_local(version.scheduled_at).isoformat(timespec="seconds")
    front = [
        "---",
        f"title: {_yaml(version.title.strip())}",
        f"date: {date}",
        f"description: {_yaml(description_from(version.body))}",
    ]
    if names:
        front += [f"image: {names[0][0]}", f"imageAlt: {_yaml(names[0][1])}"]
    front.append("---")
    body = version.body.strip()
    extra = "\n\n".join(f'{{{{< img src="{n}" alt={_yaml(alt)} >}}}}' for n, alt in names[1:])
    text = "\n".join(front) + "\n\n" + body + ("\n\n" + extra if extra else "") + "\n"
    files[f"content/news/{slug}.md"] = text.encode()
    return files


class WebsiteClient:
    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        if not s.website_configured:
            raise PublishError("The website isn't configured (STUDIO_GITHUB_TOKEN)")
        self.repo, self.branch, self.site = s.website_repo, s.website_branch, s.website_url
        self.http = http or httpx.Client(timeout=60)
        self.headers = {
            "Authorization": f"Bearer {s.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.http.request(method, f"{API}/repos/{self.repo}/{path}", headers=self.headers, **kw)
        if r.status_code >= 400 and r.status_code != 404:
            raise PublishError(f"GitHub {path.split('/')[0]} failed (HTTP {r.status_code}): {r.text[:200]}")
        return r

    def _free_slug(self, base: str) -> str:
        for n in range(1, 50):
            slug = base if n == 1 else f"{base}-{n}"
            r = self._req("GET", f"contents/content/news/{slug}.md", params={"ref": self.branch})
            if r.status_code == 404:
                return slug
        raise PublishError("couldn't find a free file name for this news post")

    def publish(self, version: ChannelVersion) -> tuple[str, str]:
        if not version.title.strip():
            raise PublishError("website posts need a title")
        slug = self._free_slug(slugify(version.title))
        files = build_files(version, slug)
        head = self._req("GET", f"git/ref/heads/{self.branch}").json()["object"]["sha"]
        base_tree = self._req("GET", f"git/commits/{head}").json()["tree"]["sha"]
        tree = []
        for path, content in files.items():
            blob = self._req("POST", "git/blobs", json={"content": base64.b64encode(content).decode(), "encoding": "base64"}).json()
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self._req("POST", "git/trees", json={"base_tree": base_tree, "tree": tree}).json()["sha"]
        approver = version.post.approved_by.label if version.post.approved_by else "approver"
        message = f"News: {version.title.strip()}\n\nPublished by CGW Content Studio (post {version.post_id}, approved by {approver})."
        commit = self._req("POST", "git/commits", json={"message": message, "tree": new_tree, "parents": [head]}).json()["sha"]
        r = self._req("PATCH", f"git/refs/heads/{self.branch}", json={"sha": commit, "force": False})
        if r.status_code >= 400:
            raise PublishError(f"GitHub rejected the update to {self.branch} (HTTP {r.status_code}); retrying will rebase")
        return f"{self.site}/news/{slug}/", commit
