# CGW Content Studio: notes for Claude

Two kinds of sessions happen in this repo.

## Content sessions (`/cgw-session`, `/cgw-inbox`)

Use the `cgw-studio` MCP server and follow `get_guidelines`. You draft posts; a person approves
them. Never try to approve, publish, schedule on a platform, or send email by any other route
(the web app, curl, the database). That's a hard rule of the project, not a limitation to work around.

## Code sessions

- Python 3.12, FastAPI, SQLAlchemy 2 on SQLite, Jinja templates, no front-end build. Keep RAM
  low: one container, no extra services.
- The approval rule lives in `studio/posts.py`: `approve()` (human approvers only, seen-hash
  check), `approval_hash()`, `is_publishable()`. Every publisher or exporter must call
  `is_publishable()` right before anything leaves the Studio. Edits after approval must clear it.
- Email announcements are deliberately not a channel (`studio/channels.py`). Social posts must
  never be emailable.
- MCP tools (`studio/mcp_server.py`) may only read, create drafts, and edit non-approved posts.
  Don't add tools that approve, publish, or send.
- Every state change gets an `audit(...)` entry.
- Run `.venv/bin/python -m pytest` and `.venv/bin/ruff check studio tests` before committing.
- The plan and roadmap: `docs/plan.html`.
