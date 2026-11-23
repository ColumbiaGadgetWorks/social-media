"""Turning channels on and off in Settings."""

import pytest
from sqlalchemy import select

from studio import batch, publishers
from studio import channels as ch
from studio import db as db_mod
from studio import posts as post_svc
from studio.models import Post, User
from studio.security import Actor

from .conftest import csrf_of
from .test_publish_and_reminders import approved_post  # noqa: F401  (fixture)


def save_channels(client, keep_on):
    data = {"csrf": csrf_of(client, "/settings"), "on": list(keep_on)}
    return client.post("/settings/channels", data=data, follow_redirects=True).text


def test_everything_is_on_by_default(app):
    with db_mod.session_scope() as s:
        assert ch.disabled_keys(s) == set()


def test_settings_turns_a_channel_off_and_back_on(approver_client):
    others = [k for k in ch.CHANNELS if k != "youtube_shorts"]
    assert "YouTube Shorts off" in save_channels(approver_client, others)
    with db_mod.session_scope() as s:
        assert ch.disabled_keys(s) == {"youtube_shorts"}
    assert "No changes" in save_channels(approver_client, others)
    assert "YouTube Shorts on" in save_channels(approver_client, list(ch.CHANNELS))
    with db_mod.session_scope() as s:
        assert ch.disabled_keys(s) == set()


def test_off_channel_is_not_offered_or_enabled_on_new_drafts(approved_post):  # noqa: F811
    with db_mod.session_scope() as s:
        ch.set_enabled(s, "bluesky", False)
        post = s.get(Post, approved_post)
        assert "bluesky" not in {c.key for c in post_svc.compatible_channels(post)}
        actor = Actor("user", s.scalar(select(User).where(User.username == "adam")))
        post.version("bluesky").enabled = False
        with pytest.raises(post_svc.PostError, match="turned off"):
            post_svc.update_post(s, actor, post, versions=[{"channel": "bluesky", "enabled": True}])


def test_off_channel_blocks_review_until_unticked_and_never_publishes(approved_post):  # noqa: F811
    with db_mod.session_scope() as s:
        assert [v.channel for v in publishers.due_versions(s)] == ["bluesky"]
        ch.set_enabled(s, "bluesky", False)
        assert publishers.due_versions(s) == []
        post = s.get(Post, approved_post)
        assert "bluesky" in post_svc.problems(post)
        assert "turned off" in post_svc.problems(post)["bluesky"][0]


def test_off_channel_has_no_batch_items_and_isnt_in_the_rules(approved_post):  # noqa: F811
    from studio import queue

    with db_mod.session_scope() as s:
        assert "youtube_shorts" in queue.channel_rules_text(s)
        ch.set_enabled(s, "youtube_shorts", False)
        assert "youtube_shorts" not in queue.channel_rules_text(s)
        assert batch.items(s, "youtube_shorts") == []
        assert "youtube_shorts" not in {r["channel"].key for r in batch.overview(s)}
