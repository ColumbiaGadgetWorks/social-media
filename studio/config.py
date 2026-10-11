"""Settings, read once from environment variables (see .env.example)."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PRIVATE_NETWORKS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128,fc00::/7"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]


def _list(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    data_dir: Path
    media_dir: Path
    secret_key: str
    base_url: str
    timezone: ZoneInfo
    # Login
    require_proxy_auth: bool
    proxy_user_header: str
    trusted_proxies: list = field(default_factory=list)
    secure_cookies: bool = False
    # MCP
    mcp_allowed_networks: list = field(default_factory=list)
    # Email
    mail_backend: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    reminder_emails: list = field(default_factory=list)
    # Bluesky
    bluesky_service: str = "https://bsky.social"
    bluesky_handle: str = ""
    bluesky_app_password: str = ""
    # Batch days and reminders
    batch_anchor: date = date(2026, 10, 12)
    batch_cycle_days: int = 14
    reminder_hour: int = 8
    claude_queue_threshold: int = 10
    # Background loop
    scheduler_enabled: bool = True
    scheduler_interval_s: int = 60
    max_upload_mb: int = 500
    # Public address for media that Meta fetches (/m/...). Defaults to base_url.
    public_url: str = ""
    # LAN address of the Studio (http://<unraid-ip>:<host port>) for Claude Code and the extension.
    lan_url: str = ""
    # Discord: photos/videos posted in these channels come into the media library
    discord_bot_token: str = ""
    discord_channel_ids: list = field(default_factory=list)
    # Calendar
    calendar_ics_url: str = ""
    calendar_poll_minutes: int = 15
    calendar_lookahead_days: int = 60
    weekly_promo_days: int = 10  # weekly events get their post this many days ahead, so fresh photos can be used
    event_skip_keywords: list = field(default_factory=list)
    # Meta (Facebook page, Instagram business account, Threads)
    meta_graph_version: str = "v24.0"
    meta_page_id: str = ""
    meta_page_token: str = ""
    meta_ig_user_id: str = ""
    threads_user_id: str = ""
    threads_token: str = ""
    # Website (Hugo repo on GitHub)
    github_token: str = ""
    website_repo: str = "ColumbiaGadgetWorks/website"
    website_branch: str = "main"
    website_url: str = "https://columbiagadgetworks.org"
    # Email announcements: Dolibarr is the list, SMTP2GO (SMTP) sends
    dolibarr_url: str = ""
    dolibarr_api_key: str = ""
    dolibarr_tag: str = "Email updates"
    dolibarr_user_id: int = 1
    announce_from: str = "Columbia Gadget Works <mail@columbiagadgetworks.org>"
    announce_reply_to: str = "mail@columbiagadgetworks.org"
    announce_hour: int = 10
    org_address: str = "Columbia Gadget Works, 1404 Grand Ave, Columbia, MO 65203"
    # Video: Whisper model for subtitles ("" turns transcription off), ffmpeg threads per job
    whisper_model: str = "base.en"
    ffmpeg_threads: int = 2

    @property
    def media_base_url(self) -> str:
        return (self.public_url or self.base_url).rstrip("/")

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_page_id and self.meta_page_token)

    @property
    def instagram_configured(self) -> bool:
        return bool(self.meta_configured and self.meta_ig_user_id)

    @property
    def threads_configured(self) -> bool:
        return bool(self.threads_user_id and self.threads_token)

    @property
    def dolibarr_configured(self) -> bool:
        return bool(self.dolibarr_url and self.dolibarr_api_key)

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_bot_token and self.discord_channel_ids)

    @property
    def website_configured(self) -> bool:
        return bool(self.github_token and self.website_repo)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'studio.db'}"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("STUDIO_DATA_DIR", "./data")).resolve()
    media_dir = Path(os.environ.get("STUDIO_MEDIA_DIR", "./media")).resolve()
    secret = os.environ.get("STUDIO_SECRET_KEY", "")
    if not secret:
        # Persist a generated key so sessions survive restarts without manual setup.
        data_dir.mkdir(parents=True, exist_ok=True)
        key_file = data_dir / "secret_key"
        if not key_file.exists():
            key_file.write_text(os.urandom(32).hex())
            key_file.chmod(0o600)
        secret = key_file.read_text().strip()
    anchor = os.environ.get("STUDIO_BATCH_ANCHOR", "2026-10-12")
    return Settings(
        data_dir=data_dir,
        media_dir=media_dir,
        secret_key=secret,
        base_url=os.environ.get("STUDIO_BASE_URL", "http://localhost:8080").rstrip("/"),
        timezone=ZoneInfo(os.environ.get("STUDIO_TIMEZONE", "America/Chicago")),
        require_proxy_auth=_bool("STUDIO_REQUIRE_PROXY_AUTH", False),
        proxy_user_header=os.environ.get("STUDIO_PROXY_USER_HEADER", "Remote-User"),
        trusted_proxies=_networks(os.environ.get("STUDIO_TRUSTED_PROXIES", "")),
        secure_cookies=_bool("STUDIO_SECURE_COOKIES", False),
        mcp_allowed_networks=_networks(os.environ.get("STUDIO_MCP_ALLOWED_NETWORKS", PRIVATE_NETWORKS)),
        mail_backend=os.environ.get("STUDIO_MAIL_BACKEND", "console"),
        smtp_host=os.environ.get("STUDIO_SMTP_HOST", "mail.smtp2go.com"),
        smtp_port=_int("STUDIO_SMTP_PORT", 587),
        smtp_user=os.environ.get("STUDIO_SMTP_USER", ""),
        smtp_password=os.environ.get("STUDIO_SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("STUDIO_SMTP_FROM", "studio@columbiagadgetworks.org"),
        smtp_starttls=_bool("STUDIO_SMTP_STARTTLS", True),
        reminder_emails=_list(os.environ.get("STUDIO_REMINDER_EMAILS", "")),
        bluesky_service=os.environ.get("STUDIO_BLUESKY_SERVICE", "https://bsky.social").rstrip("/"),
        bluesky_handle=os.environ.get("STUDIO_BLUESKY_HANDLE", ""),
        bluesky_app_password=os.environ.get("STUDIO_BLUESKY_APP_PASSWORD", ""),
        batch_anchor=date.fromisoformat(anchor),
        batch_cycle_days=_int("STUDIO_BATCH_CYCLE_DAYS", 14),
        reminder_hour=_int("STUDIO_REMINDER_HOUR", 8),
        claude_queue_threshold=_int("STUDIO_CLAUDE_QUEUE_THRESHOLD", 10),
        scheduler_enabled=_bool("STUDIO_SCHEDULER_ENABLED", True),
        scheduler_interval_s=_int("STUDIO_SCHEDULER_INTERVAL", 60),
        max_upload_mb=_int("STUDIO_MAX_UPLOAD_MB", 500),
        public_url=os.environ.get("STUDIO_PUBLIC_URL", ""),
        lan_url=os.environ.get("STUDIO_LAN_URL", "").rstrip("/"),
        discord_bot_token=os.environ.get("STUDIO_DISCORD_BOT_TOKEN", "").strip(),
        discord_channel_ids=[c for c in re.split(r"[\s,]+", os.environ.get("STUDIO_DISCORD_CHANNEL_IDS", "")) if c.isdigit()],
        calendar_ics_url=os.environ.get(
            "STUDIO_CALENDAR_ICS_URL", "https://columbiagadgetworks.org/api/calendar.ics"
        ),
        calendar_poll_minutes=_int("STUDIO_CALENDAR_POLL_MINUTES", 15),
        calendar_lookahead_days=_int("STUDIO_CALENDAR_LOOKAHEAD_DAYS", 60),
        weekly_promo_days=_int("STUDIO_WEEKLY_PROMO_DAYS", 10),
        event_skip_keywords=_list(os.environ.get("STUDIO_EVENT_SKIP_KEYWORDS", "board meeting,member meeting")),
        meta_graph_version=os.environ.get("STUDIO_META_GRAPH_VERSION", "v24.0"),
        meta_page_id=os.environ.get("STUDIO_META_PAGE_ID", ""),
        meta_page_token=os.environ.get("STUDIO_META_PAGE_TOKEN", ""),
        meta_ig_user_id=os.environ.get("STUDIO_META_IG_USER_ID", ""),
        threads_user_id=os.environ.get("STUDIO_THREADS_USER_ID", ""),
        threads_token=os.environ.get("STUDIO_THREADS_TOKEN", ""),
        github_token=os.environ.get("STUDIO_GITHUB_TOKEN", ""),
        website_repo=os.environ.get("STUDIO_WEBSITE_REPO", "ColumbiaGadgetWorks/website"),
        website_branch=os.environ.get("STUDIO_WEBSITE_BRANCH", "main"),
        website_url=os.environ.get("STUDIO_WEBSITE_URL", "https://columbiagadgetworks.org").rstrip("/"),
        dolibarr_url=os.environ.get("STUDIO_DOLIBARR_URL", "").rstrip("/"),
        dolibarr_api_key=os.environ.get("STUDIO_DOLIBARR_API_KEY", ""),
        dolibarr_tag=os.environ.get("STUDIO_DOLIBARR_TAG", "Email updates"),
        dolibarr_user_id=_int("STUDIO_DOLIBARR_USER_ID", 1),
        announce_from=os.environ.get("STUDIO_ANNOUNCE_FROM", "Columbia Gadget Works <mail@columbiagadgetworks.org>"),
        announce_reply_to=os.environ.get("STUDIO_ANNOUNCE_REPLY_TO", "mail@columbiagadgetworks.org"),
        announce_hour=_int("STUDIO_ANNOUNCE_HOUR", 10),
        org_address=os.environ.get("STUDIO_ORG_ADDRESS", "Columbia Gadget Works, 1404 Grand Ave, Columbia, MO 65203"),
        whisper_model=os.environ.get("STUDIO_WHISPER_MODEL", "base.en"),
        ffmpeg_threads=_int("STUDIO_FFMPEG_THREADS", 2),
    )


def ip_in(address: str | None, networks: list) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)
