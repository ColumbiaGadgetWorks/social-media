from datetime import timedelta

from sqlalchemy import select

from studio import db as db_mod
from studio import posts as post_svc
from studio.models import MetricSnapshot, User
from studio.security import Actor


def seed_published():
    with db_mod.session_scope() as s:
        actor = Actor("user", s.scalar(select(User)))
        for i, (pillar, channel, likes) in enumerate((("member_projects", "x", 40), ("shop_humor", "bluesky", 12),
                                                      ("member_projects", "facebook", 20), ("classes_events", "threads", 5))):
            post = post_svc.create_post(s, actor, [], note=f"post {i}", pillar=pillar)
            post.status = "done"
            v = post.version(channel)
            v.enabled, v.body, v.publish_state, v.external_id = True, "x", "published", f"id{i}"
            v.published_at = db_mod.utcnow() - timedelta(days=i + 1)
            s.flush()
            s.add(MetricSnapshot(version_id=v.id, data={"likes": likes, "comments": 2}))


def test_insights_page(approver_client):
    seed_published()
    page = approver_client.get("/insights")
    assert page.status_code == 200
    text = page.text
    assert "Member projects" in text and "32.0" in text  # (42 + 22) / 2
    assert text.index("Member projects") < text.index("Shop humor") < text.index("Classes &amp; events")
    assert "4 of 4 published posts have numbers" in text
