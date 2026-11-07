# CGW Content Studio: project summary and notes for continuing

Columbia Gadget Works (nonprofit makerspace, 1404 Grand Ave, Columbia MO) runs this app to turn
member photos and videos into social posts, a monthly newsletter, and website news. Claude drafts;
a person approves everything. This file sums up what was built in the planning and build chat
(Oct 7 – Oct 15, 2026), what other sessions added afterwards (to Nov 2), and what to know before
continuing.

- Repo: `ColumbiaGadgetWorks/social-media`. The only branch, `claude/sleepy-wozniak-cdmb6d`, is
  also the default branch. Pushing to it **is** "merging": CI tests, then publishes
  `ghcr.io/columbiagadgetworks/social-media:latest`.
- Runs on Unraid as one Docker container (template `unraid/cgw-studio.xml`), at
  `https://studio.adman.casa` behind a reverse proxy.
- The full plan and roadmap: `docs/plan.html`. Platform setup: `docs/setup-platforms.md`.
  Single sign-on: `docs/setup-oidc.md`. Code conventions for Claude: `CLAUDE.md`.

---

## 1. Ground rules (from the user; keep them)

- **A human approves every post and every email.** Claude only drafts. The approval is tied to a
  hash of the exact content, and any edit clears it. `is_publishable()` / `is_sendable()` are checked
  right before anything leaves. MCP tools never approve, publish, or send.
- **Free to run.** No Claude API key: AI work happens in Claude Code / Claude Desktop sessions on the
  Pro plan, over a LAN-only MCP server. No paid SaaS.
- **Email is for announcements only** (classes, events, important org news), to ~50 people,
  1–2 a month. Social posts must never be emailed. Dolibarr is the list and the unsubscribe source of
  truth. SMTP2GO sends.
- **Low RAM** (one container, SQLite, no extra services). About 105 MB idle.
- **One approver for now** (the user). Photo releases: a future feature.
- **Few emails to the user.** They pushed back twice on noisy reminders. Emails should only come when
  something truly needs them.
- **Look and feel:** CGW orange from the website (`#bf4d28`), never pink. Captions must not be cringy
  or repetitive; weekly Hack Night posts need variety and real photos.
- The user likes to see a plan and approve it before big changes, then "merge" (push).

---

## 2. Architecture at a glance

- Python 3.12, FastAPI, SQLAlchemy 2 on SQLite (WAL), Jinja templates, plain JS, no front-end build.
- `studio/` modules (main ones):
  - `posts.py`: approval rules (`approve`, `approval_hash`, `is_publishable`, `problems`).
  - `channels.py`: every channel, its limits and mode (`direct` = published by the Studio,
    `batch` = scheduled by hand on batch day, `on_day` = posted by hand on the day).
  - `publishers/`: `bluesky`, `meta` (Facebook, Instagram, Threads), `website` (GitHub commit to the
    Hugo site), `discord` (#shop-news), `linkedin` (company page).
  - `calendar_sync.py`: ICS sync from `https://columbiagadgetworks.org/api/calendar.ics`, event
    cards, promo posts, weekly-series logic.
  - `media.py`, `video.py` (ffmpeg edits, subtitles via Whisper `base.en`, music, end cards),
    `upload_batch.py` (drag-and-drop/zip upload and sorting), `public_media.py` (signed `/m/` links
    that work only for approved posts).
  - `announcements.py` + `dolibarr.py`: monthly newsletter and event announcements, RFC 8058
    one-click unsubscribe.
  - `discord_ingest.py`: photos posted in the Discord uploads channel become uploads.
  - `reminders.py`: emails to the user. `scheduler.py`: the background loop.
  - `mcp_server.py`: MCP tools for Claude. `ext_api.py`: API for the browser extension.
  - `oidc.py`: single sign-on.
- `extension/`: Chrome/Edge Manifest V3 extension that fills platform upload forms on batch day.
- `guidelines/` (workflow, voice, pillars, hook bank) are what Claude reads via `get_guidelines`.
- `.claude/commands/`: `/cgw-session`, `/cgw-inbox`, `/cgw-event`, `/cgw-plan`, `/cgw-review`,
  `/cgw-fix-extension`.

---

## 3. What exists now, by area

### Posting channels
| Channel | How it posts |
|---|---|
| Bluesky | Automatic |
| Facebook, Instagram, Threads | Automatic once Meta credentials are set (media fetched from `/m/`) |
| Website news | Automatic: one commit to `ColumbiaGadgetWorks/website` per post |
| Discord #shop-news | Automatic once `STUDIO_DISCORD_POST_CHANNEL_ID` is set; crossposts in Announcement channels |
| LinkedIn company page | Built; automatic once LinkedIn approves the Community Management API and a page admin clicks **Connect LinkedIn** in Settings. Until then, batch day |
| TikTok, YouTube Shorts, X | Batch day every 2 weeks (anchor Mon Oct 12, 2026) with the browser extension |
| Google Business Profile | By hand on the day, ~1 post per 2 weeks |

### Content flow
- **Upload**: drag in many photos/videos or a zip, then the **sort page** groups them into posts or
  carousels (max 10, one video per post), with a description per post, optional per-photo notes,
  "Taken at" event, kind of post, library-only or discard. A "one at a time" view steps through
  with large previews. The old quick upload form is still there.
- **Discord uploads channel**: the bot reads it every minute; photos/videos with text become uploads
  (✅ reaction). `library` / `separate` keywords. Photos sent during an event are tagged "Taken at".
- **Claude sessions** (`/cgw-session` etc.) read the queue over MCP and submit per-channel drafts.
  The queue is ordered by urgency.
- **Weekly events** (Open Hack Night): the site's feed lists each occurrence separately, so the
  Studio detects weekly series itself (same title, weekday, time). One post per week, created
  10 days ahead, with a rotating angle (project spotlight, photo recap, first-timer, tool spotlight,
  fix-it, question, humor), fresh photos attached, and recent posts shown to Claude so it doesn't
  repeat itself. Claude asks the person for photos/stories when there's nothing good.
- **Review page**: approve, request changes, withdraw approval, cancel posts (also in bulk).
  The post page shows a live preview per platform. After approving, a page lists the by-hand channels.

### Emails to the user (current design, by a later session)
Three kinds only, each switchable in **Settings → Emails**:
1. **Running low**: when only ~1 approved post is left to go out (posts are planned ~8 weeks ahead);
   then weekly while it stays low.
2. **A post failed to publish** (after automatic retries, default 3), bundled, at most one a day.
3. **The monthly email failed to send.**

There is no weekly digest or "last call" any more. Nothing in the first 24 hours after start, nothing
in the evening. The digest/last-call design from Oct 8 was replaced on Oct 10.

### Emails to the mailing list
- Monthly newsletter (draft ~12 days before the first Tuesday, sends 10:00 first Tuesday) from
  `info@`; event announcements from `events@` (both editable in Settings).
- A test send is required before approving. Cap of 2 per month. Weekly Hack Nights appear as one line.

### Other features
- CGW orange theme, favicon; **Settings → Appearance** has colorways (orange, blue, teal, graphite)
  and light/dark per person. Static files are versioned (`?v=`) so browsers don't keep old styles.
- Settings shows ready-made MCP setup for Claude Code / the Desktop Code tab and the Desktop chat
  (`mcp-remote`), filled with the LAN address (`STUDIO_LAN_URL`).
- Single sign-on with OpenID Connect (VoidAuth etc.), optional OIDC-only mode.
- Music library (licensed tracks, zip import, credit rules), video render jobs, insights page.
- Event cards with the CGW logo; stale cards are redrawn automatically.

---

## 4. Deployment and configuration notes

- **Update the container:** Unraid → Docker → cgw-studio → Force Update (pulls `:latest`).
- **Template:** don't re-download the template over `my-cgw-studio.xml` (it wipes your values). Add
  new variables by hand (Add another Path, Port, Variable). A later session added an Unraid script
  for the OIDC variables.
- **Reverse proxy rules:** `/m/*` and `/u/*` must be public (no login): Meta fetches approved media
  from `/m/`, recipients unsubscribe at `/u/`. Block `/mcp` and `/api/ext/*` at the proxy. Raise the
  request size limit for big uploads (e.g. nginx `client_max_body_size 500m;`).
  - If Instagram/Facebook/Threads fail with "Only photo or video can be accepted" or "invalid image
    file", the proxy login is almost certainly covering `/m/*`.
- **Trusted proxy:** with proxy auth on, `STUDIO_TRUSTED_PROXIES` must be the proxy's IP as the
  container sees it (often a Docker network IP).
- **Dolibarr:** `STUDIO_DOLIBARR_URL` is the base address (no `/api`), reachable from the container
  and not behind the proxy login. A dedicated `studio` user with contacts, tags and (optionally)
  agenda permissions; its API key and user ID go in the template.
- **GitHub token (website):** fine-grained, `ColumbiaGadgetWorks/website` only, Contents read/write.
  Fine-grained tokens can't pull from GHCR; the package should be public or use a classic
  `read:packages` token.
- **MCP:** runs only on the LAN. Claude Desktop/Code connect at startup and don't retry; if the
  tools are missing, run `/mcp` → cgw-studio → Reconnect, or restart Claude Desktop.
- **Discord bot:** Message Content Intent on; permissions View Channels, Read Message History, Add
  Reactions, Send Messages, Attach Files, Embed Links. Icon: `docs/img/discord-bot-icon.png`.
- **LinkedIn:** setup and suggested application wording in `docs/setup-platforms.md` section 8.
  Redirect URL: `https://studio.adman.casa/settings/linkedin/callback`. The API version is picked
  automatically (two months back) unless a recent month is pinned. Refresh tokens may only come with
  LinkedIn's standard tier; until then reconnect every 60 days (Settings shows the date).

---

## 5. Status, open items and ideas

### Waiting on others
- **LinkedIn Community Management API approval**, then Connect LinkedIn in Settings.
- YouTube Shorts audit, Google Business Profile API, TikTok Content Posting audit (all optional;
  until then batch day).

### Not verified against the real services (only stand-in APIs in tests)
- First real posts to Discord #shop-news and LinkedIn; the extension's selectors on the live
  TikTok/YouTube/X/LinkedIn pages; Whisper model download in the container.

### Long-term ideas (`docs/plan.html`, "Future & long-term ideas")
- **Channel mix rule**: default channels per kind of post so Claude stops guessing (Hack Night,
  projects, tool spotlights and humor go to Instagram, Facebook, Threads, Bluesky, X and Discord,
  plus TikTok/Shorts for video; classes, events, giving, partners and org news also go to LinkedIn
  and the website).
- Photo-release tracking on Dolibarr contacts, blocking approval for people without a release.
- claude.ai / phone sessions through a public OAuth MCP connector.
- More approvers; a second approval for email.
- Phone push alerts (ntfy).
- Unattended browser robot for batch channels (on hold: platform terms, bot detection).
- Opt-in Discord show-and-tell channel feeding the uploads.
- Discord Scheduled Events from the calendar (RSVP button), suggested Oct 15.
- Board review of the Discord channel's consent wording; real photo releases.

---

## 6. Working on the code

- Read `CLAUDE.md` first (approval rule, token scopes, audit every state change, MCP tools never
  approve/publish/send).
- Set up and check before every push:
  ```
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
  .venv/bin/python -m pytest
  .venv/bin/ruff check studio tests --select F,E9,B,I --ignore B008   # the rules CI enforces
  ```
- Browser tests (extension and sort page) need a full Chromium. In the cloud sandbox use
  `CGW_TEST_CHROMIUM=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`; CI installs its own.
- About 140 tests; CI was green on the latest commit (Nov 2, 2026).
- Database changes: add columns to `models.py`. `db._add_missing_columns()` adds them on start
  (forward-only, nullable or with a default).
- Tests that move the clock: set any "started at" or relative timestamps after patching time, or use
  a fixed date. Two tests broke on real calendar dates before this was fixed.
- The website (`ColumbiaGadgetWorks/website`, Hugo on Cloudflare) defines the brand colors in
  `assets/css/site.css` and `hugo.toml` (`brandColor = "#BF4D28"`), and builds the calendar feed in
  `src/calendar.js` (it flattens recurring events, which is why the Studio detects weekly series).
- Several Claude sessions have worked on this repo in parallel. Pull before starting, and check
  `git log` for what changed.
