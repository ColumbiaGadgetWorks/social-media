"""The channels a post can go to, and what each one accepts.

mode:
  direct  - the Studio publishes through the platform's API at the scheduled time
  batch   - approved posts appear on that platform's batch page; a person schedules
            them in the platform's own scheduler (later with the browser extension)
  on_day  - like batch, but the platform can't schedule ahead, so the person posts
            on the day from an email reminder

Email announcements and the website are deliberately not channels: email is a
separate Announcement type (Phase 3) so social posts can never be emailed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    mode: str
    images: bool
    video: bool
    text_only: bool  # may post with no media at all
    max_chars: int
    max_images: int
    title_chars: int = 0  # >0 means the channel has a title field
    horizon_days: int = 14  # how far ahead the platform's scheduler accepts posts
    link_note: str = ""
    tips: str = ""


CHANNELS: dict[str, Channel] = {
    c.key: c
    for c in [
        Channel(
            "instagram", "Instagram", "batch", images=True, video=True, text_only=False,
            max_chars=2200, max_images=10,
            link_note="Links aren't clickable: say \"link in bio\".",
            tips="Hook in the first line. 3-5 targeted hashtags. Reels for video.",
        ),
        Channel(
            "facebook", "Facebook", "batch", images=True, video=True, text_only=True,
            max_chars=5000, max_images=10,
            link_note="Use the full link (e.g. https://givebutter.com/kxk2FA).",
            tips="Keywords matter more than hashtags; 2-3 hashtags max.",
        ),
        Channel(
            "threads", "Threads", "batch", images=True, video=True, text_only=True,
            max_chars=500, max_images=10, tips="Conversational, short. One hashtag at most.",
        ),
        Channel(
            "bluesky", "Bluesky", "direct", images=True, video=False, text_only=True,
            max_chars=300, max_images=4,
            tips="300 characters including hashtags and links. Video posting comes later.",
        ),
        Channel(
            "youtube_shorts", "YouTube Shorts", "batch", images=False, video=True, text_only=False,
            max_chars=5000, max_images=0, title_chars=100,
            tips="Vertical video up to 3 minutes. Short searchable title; #Shorts in the description.",
        ),
        Channel(
            "tiktok", "TikTok", "batch", images=False, video=True, text_only=False,
            max_chars=2200, max_images=0, horizon_days=10,
            tips="TikTok Studio schedules only 10 days ahead, so schedule TikTok posts within days 1-10 of a batch cycle.",
        ),
        Channel(
            "gbp", "Google Business Profile", "on_day", images=True, video=False, text_only=True,
            max_chars=1500, max_images=1, horizon_days=0,
            tips="About 1 post every 2 weeks: the best upcoming class/event or a standout project. Posted by hand on the day.",
        ),
        Channel(
            "linkedin", "LinkedIn", "batch", images=True, video=True, text_only=True,
            max_chars=3000, max_images=9,
            tips="Professional angle: impact, partners, sponsors, classes.",
        ),
        Channel(
            "x", "X", "batch", images=True, video=True, text_only=True,
            max_chars=280, max_images=4, tips="280 characters including hashtags; a link counts as 23.",
        ),
        Channel(
            "website", "Website news", "batch", images=True, video=False, text_only=True,
            max_chars=20000, max_images=6, title_chars=90,
            link_note="Markdown. Hashtags aren't used on the website.",
            tips="A news post on columbiagadgetworks.org/news/. Title like a headline; body in Markdown, "
                 "a few short paragraphs, link to /tools/... or /membership/ pages where useful. "
                 "Only for posts worth keeping: class recaps, events, standout projects, org news.",
        ),
    ]
}

# Channels that switch from batch day to automatic once their credentials are set.
_DIRECT_WHEN = {
    "instagram": "instagram_configured",
    "facebook": "meta_configured",
    "threads": "threads_configured",
    "website": "website_configured",
}


def mode(channel: Channel | str) -> str:
    """The channel's mode right now: direct when the Studio can publish it, else its default."""
    from .db import settings

    c = CHANNELS[channel] if isinstance(channel, str) else channel
    flag = _DIRECT_WHEN.get(c.key)
    if flag and getattr(settings(), flag):
        return "direct"
    return c.mode


UNSAFE_HTML = ("<script", "<iframe", "<object", "<embed", "javascript:", "onerror=", "onload=")

MANUAL_MODES = ("batch", "on_day")


def compatible(channel: Channel, kinds: set[str]) -> bool:
    """Can this channel take a post whose media are of these kinds?"""
    if not kinds:
        return channel.text_only
    if "video" in kinds and not channel.video:
        return False
    if "image" in kinds and not channel.images:
        return False
    return True


def validate(channel: Channel, *, body: str, hashtags: str, title: str, kinds: set[str], image_count: int) -> list[str]:
    """Problems that block submitting or approving this version."""
    problems = []
    text = "\n\n".join(p for p in (body.strip(), hashtags.strip()) if p)
    if not body.strip():
        problems.append("caption is empty")
    if len(text) > channel.max_chars:
        problems.append(f"caption is {len(text)} characters; the limit is {channel.max_chars}")
    if not compatible(channel, kinds):
        accepts = [k for k, ok in (("images", channel.images), ("video", channel.video)) if ok]
        problems.append(f"{channel.label} accepts {' and '.join(accepts) or 'text only'}")
    if channel.images and image_count > channel.max_images:
        problems.append(f"{image_count} images; {channel.label} takes at most {channel.max_images}")
    if channel.key == "website" and any(bad in body.lower() for bad in UNSAFE_HTML):
        problems.append("the website text contains script or embed HTML, which isn't allowed")
    if channel.title_chars:
        if not title.strip():
            problems.append("title is empty")
        elif len(title) > channel.title_chars:
            problems.append(f"title is {len(title)} characters; the limit is {channel.title_chars}")
    return problems
