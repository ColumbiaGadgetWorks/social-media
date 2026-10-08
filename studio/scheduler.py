"""The background loop: process uploads, publish due posts, send reminders."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from . import announcements, calendar_sync, media, metrics, publishers, reminders, video
from .db import session_scope, settings
from .timeutil import local_now

log = logging.getLogger(__name__)
_media_lock = threading.Lock()  # one heavy media job at a time keeps RAM low


def tick() -> None:
    """One pass. Each step is isolated so one failure doesn't stop the others."""
    steps = (("media", _media), ("calendar", _calendar), ("publish", _publish), ("email", _email),
             ("reminders", _reminders), ("nightly", _nightly))
    for name, step in steps:
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
            while video.run_pending(db):
                pass
    finally:
        _media_lock.release()


def _publish() -> None:
    with session_scope() as db:
        failed = publishers.publish_due(db)
        if failed:
            reminders.publish_failures(db, failed)


_last_calendar_sync = 0.0


def _calendar() -> None:
    global _last_calendar_sync
    if time.monotonic() - _last_calendar_sync < settings().calendar_poll_minutes * 60 and _last_calendar_sync:
        return
    _last_calendar_sync = time.monotonic()
    if not settings().calendar_ics_url:
        return
    text = calendar_sync.fetch()
    with session_scope() as db:
        result = calendar_sync.sync(db, text)
    if any(result.values()):
        log.info("calendar sync: %s", result)


def _nightly() -> None:
    """Once a day after 3am: metrics and token upkeep."""
    from .publishers.meta import refresh_threads_token
    from .reminders import _once

    now = local_now()
    if now.hour < 3:
        return
    with session_scope() as db:
        if not _once(db, "nightly", now.date().isoformat()):
            return
        db.commit()
        try:
            refresh_threads_token(db)
            db.commit()
        except Exception:
            log.exception("Threads token refresh failed")
            db.rollback()
        log.info("collected %s metric snapshots", metrics.collect(db))


def _email() -> None:
    """Monthly drafts, due announcements, and unsubscribes waiting to reach Dolibarr."""
    with session_scope() as db:
        announcements.ensure_monthly(db)
        db.commit()
        if settings().dolibarr_configured:
            announcements.sync_pending_unsubscribes(db)
            db.commit()
        for ann in announcements.due(db):
            try:
                announcements.send(db, ann)
                reminders.announcement_sent(db, ann)
            except Exception as exc:
                log.exception("sending announcement %s failed", ann.id)
                db.rollback()
                ann.attempts += 1
                ann.last_error = str(exc)[:500]
                if ann.attempts >= 3:
                    ann.status = "failed"
                db.commit()
                reminders.announcement_failed(db, ann)


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
