"""Settings, read once from environment variables (see .env.example)."""

from __future__ import annotations

import ipaddress
import os
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
    claude_max_age_days: int = 3
    # Background loop
    scheduler_enabled: bool = True
    scheduler_interval_s: int = 60
    max_upload_mb: int = 500
    calendar_ics_url: str = ""

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
        claude_max_age_days=_int("STUDIO_CLAUDE_MAX_AGE_DAYS", 3),
        scheduler_enabled=_bool("STUDIO_SCHEDULER_ENABLED", True),
        scheduler_interval_s=_int("STUDIO_SCHEDULER_INTERVAL", 60),
        max_upload_mb=_int("STUDIO_MAX_UPLOAD_MB", 500),
        calendar_ics_url=os.environ.get(
            "STUDIO_CALENDAR_ICS_URL", "https://columbiagadgetworks.org/api/calendar.ics"
        ),
    )


def ip_in(address: str | None, networks: list) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)
