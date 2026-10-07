"""Internal email (reminders to the team). SMTP2GO works through plain SMTP."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .db import settings

log = logging.getLogger(__name__)

# Messages sent with the "memory" backend, for tests.
outbox: list[EmailMessage] = []


def send(to: list[str], subject: str, text: str) -> bool:
    s = settings()
    if not to:
        log.warning("No reminder recipients configured; skipped %r", subject)
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text)
    if s.mail_backend == "memory":
        outbox.append(msg)
        return True
    if s.mail_backend == "console":
        log.info("EMAIL to %s: %s\n%s", to, subject, text)
        return True
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        if s.smtp_starttls:
            smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
    return True
