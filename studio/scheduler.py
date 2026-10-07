"""The background loop: process uploads, publish due posts, send reminders."""

from __future__ import annotations

import asyncio
import logging
import threading

from . import media, publishers, reminders
from .db import session_scope, settings

log = logging.getLogger(__name__)
_media_lock = threading.Lock()  # one heavy media job at a time keeps RAM low


def tick() -> None:
    """One pass. Each step is isolated so one failure doesn't stop the others."""
    for name, step in (("media", _media), ("publish", _publish), ("reminders", _reminders)):
        try:
            step()
        except Exception:
            log.exception("background step %s failed", name)


def _media() -> None:
    if not _media_lock.acquire(blocking=False):
        return  # already running; the running pass picks up new uploads
    try:
        with session_scope() as db:
            while media.process_pending(db):
                pass
    finally:
        _media_lock.release()


def _publish() -> None:
    with session_scope() as db:
        failed = publishers.publish_due(db)
        if failed:
            reminders.publish_failures(db, failed)


def _reminders() -> None:
    with session_scope() as db:
        reminders.run_daily(db)


async def run_forever() -> None:
    interval = settings().scheduler_interval_s
    while True:
        await asyncio.to_thread(tick)
        await asyncio.sleep(interval)


def kick_media() -> None:
    """Process new uploads right away instead of waiting for the next loop."""
    if not settings().scheduler_enabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.run_in_executor(None, _media_safe)


def _media_safe() -> None:
    try:
        _media()
    except Exception:
        log.exception("media processing failed")
