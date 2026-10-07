import json
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from studio import db as db_mod
from studio import posts as post_svc
from studio.models import Post
from studio.security import Actor, create_api_token
from studio.timeutil import input_value

from .conftest import jpeg_bytes, login, make_user
from .test_web_flow import upload

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class MCP:
    def __init__(self, client: TestClient, token: str):
        self.client, self.headers, self.n = client, {**ACCEPT, "Authorization": f"Bearer {token}"}, 0

    def rpc(self, method, params=None, headers=None):
        self.n += 1
        r = self.client.post("/mcp", headers={**self.headers, **(headers or {})},
                             json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        return r

    def init(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})
        assert r.status_code == 200, r.text
        version = r.json()["result"]["protocolVersion"]
        self.headers["MCP-Protocol-Version"] = version
        return self

    def call(self, name, **arguments):
        r = self.rpc("tools/call", {"name": name, "arguments": arguments})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "result" in body, body
        return body["result"]


@pytest.fixture
def setup(app):
    make_user("adam", "admin")
    with TestClient(app, client=("192.168.1.20", 50000)) as web:
        login(web, "adam")
        post_id = upload(web, [("a.jpg", jpeg_bytes(), "image/jpeg")])
        with db_mod.session_scope() as s:
            from sqlalchemy import select

            from studio.models import User
            token = create_api_token(s, s.scalar(select(User).where(User.username == "adam")), "test")
        yield web, token, post_id


def test_rejects_missing_token(setup):
    web, _token, _ = setup
    r = web.post("/mcp", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_rejects_requests_from_outside_the_lan(app, setup):
    _web, token, _ = setup
    with TestClient(app, client=("203.0.113.5", 50000)) as outside:
        r = outside.post("/mcp", headers={**ACCEPT, "Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403


def test_rejects_requests_through_the_reverse_proxy(app, setup):
    _web, token, _ = setup
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        r = c.post("/mcp", headers={**ACCEPT, "Authorization": f"Bearer {token}", "X-Forwarded-For": "1.2.3.4"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403
    with TestClient(app, client=("10.9.9.9", 50000)) as proxy:  # the trusted proxy's own address
        r = proxy.post("/mcp", headers={**ACCEPT, "Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403


def test_no_approve_or_publish_tools(setup):
    web, token, _ = setup
    mcp = MCP(web, token).init()
    r = mcp.rpc("tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"get_work_queue", "get_work_item", "submit_drafts", "get_guidelines"} <= names
    assert not any(word in name for name in names for word in ("approve", "publish", "send", "schedule_post"))


def test_session_drafts_a_post_into_review_but_never_approves(setup):
    web, token, post_id = setup
    mcp = MCP(web, token).init()
    guide = mcp.call("get_guidelines")["content"][0]["text"]
    assert "givebutter.com/kxk2FA" in guide and "bluesky" in guide

    queue = json.loads(mcp.call("get_work_queue")["content"][0]["text"])
    assert [i["post_id"] for i in queue["items"]] == [post_id]

    item = mcp.call("get_work_item", post_id=post_id)["content"]
    assert any(c["type"] == "image" for c in item)
    media_id = json.loads(item[0]["text"])["media"][0]["media_id"]

    when = input_value(db_mod.utcnow() + timedelta(days=4))
    result = json.loads(mcp.call(
        "submit_drafts", post_id=post_id, pillar="member_projects", notes="Picked Thursday evening.",
        versions=[{"channel": "bluesky", "body": "Sam built a walnut ukulele at the shop.", "hashtags": "#makerspace", "scheduled_at": when},
                  {"channel": "instagram", "body": "Sam's walnut ukulele.", "hashtags": "#makerspace #columbiamo", "scheduled_at": when}],
        media=[{"media_id": media_id, "description": "A walnut ukulele on a bench", "alt_text": "Walnut ukulele with an engraved rosette."}],
    )["content"][0]["text"])
    assert result["status"] == "in_review", result

    with db_mod.session_scope() as s:
        post = s.get(Post, post_id)
        assert post.status == "in_review" and post.approved_hash is None
        assert post.media[0].alt_text.startswith("Walnut ukulele")
        assert post.version("bluesky").enabled
    queue = json.loads(mcp.call("get_work_queue")["content"][0]["text"])
    assert queue["items"] == []


def test_submit_returns_problems_instead_of_submitting(setup):
    web, token, post_id = setup
    mcp = MCP(web, token).init()
    result = json.loads(mcp.call("submit_drafts", post_id=post_id,
                                 versions=[{"channel": "x", "body": "word " * 80}])["content"][0]["text"])
    assert result["status"] == "draft"
    assert "x" in result["problems"]


def test_claude_cannot_edit_an_approved_post(setup):
    web, token, post_id = setup
    with db_mod.session_scope() as s:
        from sqlalchemy import select

        from studio.models import User
        adam = s.scalar(select(User).where(User.username == "adam"))
        post = s.get(Post, post_id)
        when = db_mod.utcnow() + timedelta(days=2)
        post_svc.update_post(s, Actor("user", adam), post, versions=[{"channel": "bluesky", "enabled": True, "body": "Hi", "scheduled_at": when}])
        post_svc.submit_for_review(s, Actor("user", adam), post)
        post_svc.approve(s, Actor("user", adam), post, post_svc.approval_hash(post))
    mcp = MCP(web, token).init()
    text = mcp.call("submit_drafts", post_id=post_id, versions=[{"channel": "bluesky", "body": "Sneaky edit"}])["content"][0]["text"]
    assert "already approved" in text
    with db_mod.session_scope() as s:
        post = s.get(Post, post_id)
        assert post.status == "approved" and post.version("bluesky").body == "Hi"
