"""Application factory: the web app, the MCP endpoint, and the background loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import db as db_mod
from .auth import Forbidden, LoginRequired, ProxyAuthMissing
from .config import Settings, load_settings
from .models import User
from .security import hash_password

log = logging.getLogger(__name__)


def _bootstrap_admin() -> None:
    """Create the first admin from STUDIO_ADMIN_USERNAME / STUDIO_ADMIN_PASSWORD if there are no users."""
    username = os.environ.get("STUDIO_ADMIN_USERNAME")
    password = os.environ.get("STUDIO_ADMIN_PASSWORD")
    if not username or not password:
        return
    with db_mod.session_scope() as db:
        if db.scalar(select(User).limit(1)):
            return
        db.add(User(username=username, role="admin", password_hash=hash_password(password),
                    email=os.environ.get("STUDIO_ADMIN_EMAIL", ""),
                    proxy_username=os.environ.get("STUDIO_ADMIN_PROXY_USERNAME") or None))
        log.info("Created admin user %s", username)


def create_app(settings: Settings | None = None):
    settings = settings or load_settings()
    db_mod.init(settings)
    _bootstrap_admin()

    from . import ext_api, mcp_server, scheduler
    from .web import router

    mcp_app = mcp_server.MCPGate()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp_app.start():
            task = asyncio.create_task(scheduler.run_forever()) if settings.scheduler_enabled else None
            try:
                yield
            finally:
                if task:
                    task.cancel()

    web = FastAPI(title="CGW Content Studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    web.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, session_cookie="cgw_studio",
        same_site="lax", https_only=settings.secure_cookies, max_age=14 * 24 * 3600,
    )
    web.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    web.include_router(router)
    web.include_router(ext_api.router)

    @web.get("/favicon.ico", include_in_schema=False)
    async def _favicon():
        return RedirectResponse("/static/favicon.ico", status_code=301)

    @web.exception_handler(LoginRequired)
    async def _login(request: Request, _exc):
        return RedirectResponse("/login", status_code=303)

    @web.exception_handler(Forbidden)
    async def _forbidden(request: Request, _exc):
        return HTMLResponse("<h1>Not allowed</h1><p>Your role can't do that, or the form expired. "
                            "Go back, reload the page and try again.</p>", status_code=403)

    @web.exception_handler(ProxyAuthMissing)
    async def _proxy(request: Request, _exc):
        return HTMLResponse("<h1>Sign in through the proxy</h1><p>Open the Studio through its normal address "
                            "so the proxy can sign you in.</p>", status_code=401)

    async def app(scope, receive, send):
        # /mcp goes to the MCP server (with its own LAN + token gate); everything else to the web app.
        if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            return await mcp_app(scope, receive, send)
        return await web(scope, receive, send)

    app.web = web  # for tests
    return app
