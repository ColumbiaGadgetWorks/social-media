# CGW Content Studio

Self-hosted social media studio for Columbia Gadget Works. Members upload photos and videos,
Claude (through your Claude Pro plan) drafts captions for each channel, a person approves every
post, and the Studio publishes or lines it up for batch day.

The full plan is in [`docs/plan.html`](docs/plan.html). This is **Phase 1**.

## What Phase 1 does

- **Upload** photos and videos from a phone or desktop with a one-line note. The Studio makes
  thumbnails, 768px previews, and 5 key frames per video, one job at a time.
- **Claude sessions over MCP.** Claude Code on your LAN connects with a personal token, reads the
  queue and media previews, and submits per-channel drafts. There's no tool to approve, publish or
  send. The Monday digest tells you when to run `/cgw-session`.
- **Approval rule.** Only a signed-in Approver can approve. The approval is tied to a hash of the
  exact captions, media and times, and any later edit clears it. The approve button also checks the
  version the approver was looking at, and publishers re-check the hash before sending.
- **Bluesky** publishes automatically at the scheduled time (photos and text).
- **Batch pages** for Instagram, Facebook, Threads, TikTok, YouTube Shorts, LinkedIn, X and
  Google Business Profile: numbered files, copy buttons, a zip (photos re-encoded without GPS
  data), and "Mark scheduled". Instagram, Facebook and Threads move to automatic posting in Phase 2.
- **Reminder emails, kept rare**: one Monday digest (posts to approve this week with deadlines, a
  Claude session when work is due, batch day, Google Business Profile, gaps, Hack Night photos), a
  last call when something goes out within 48 hours unapproved, and alerts when something breaks.
- Roles (contributor, editor, approver, admin), reverse-proxy auth plus a Studio login, CSRF
  protection, and an activity log of every change.

## What Phase 2 added

- **Automatic posting to Instagram, Facebook and Threads** once connected (photos, carousels,
  Reels/videos, text). Instagram and Threads fetch media from signed `/m/...` links that only work
  while the post is approved. Setup: [`docs/setup-platforms.md`](docs/setup-platforms.md).
- **Website news posts:** approved `website` versions become one commit to
  `ColumbiaGadgetWorks/website` (`content/news/<slug>.md` + photos in `assets/img/`) at the
  scheduled time; Cloudflare deploys them.
- **Events calendar sync** from the site's ICS feed every 15 minutes. New one-off events get an
  event card and two queued posts (announce two weeks out, remind two days out). Recurring events
  and board/member meetings are skipped unless the description says `Promote: yes`. If an event
  moves, approvals on its posts are cleared; if it's cancelled, its posts are pulled (and a
  cancellation notice is queued if it was already announced). See the **Events** page.
- **Planning:** gap detection (3 main posts a week, 1 Google Business Profile post per cycle), a
  weekly "schedule running dry" email, the old spreadsheet's hooks/CTAs as a hook bank, and
  `/cgw-event`, `/cgw-plan`, `/cgw-review` commands.
- **Nightly metrics** from Bluesky, Instagram, Facebook and Threads, kept permanently and shown
  on each post; Claude reads them with `get_metrics`.

## What Phase 3 added

- **Video editing.** On any video's page (or "Edit video" from a post), or from Claude with
  `request_render`: trim, shape (9:16 / 4:5 / 1:1, blurred background or crop), burned-in subtitles,
  a licensed music bed ducked under speech, a small logo and a 2-second end card. Edits are new media
  items; when requested for a post, the edit replaces the original in it. Subtitles come from a local
  Whisper model (`base.en`, downloaded once to `/data/models`); fix any words on the video's page.
- **Music library** (`/music`): upload licensed tracks with their license. Tracks that need credit
  (CC BY) make the credit line required in every caption of a post that uses them.
- **Monthly "Coming up at CGW" email** to the Dolibarr contacts tagged *Email updates* (the list
  the website signup already builds). The draft appears about 12 days before the first Tuesday with
  that month's one-off events, Claude writes it (`draft_announcement`), you approve it, and it sends
  at 10 am on the first Tuesday. Special announcements are possible; a third email in a month needs
  an admin override. Every email has one-click unsubscribe (RFC 8058), which removes the Dolibarr tag
  and sets the contact's email opt-out; each send is logged on the contact's Dolibarr agenda.
  Social posts can't be emailed.
- **Browser extension** for batch day ([`extension/`](extension/README.md)): fills each platform's
  upload form (file, title, caption) from the approved batch; you press Schedule.
- **Insights** (`/insights`): average engagement by pillar, channel and format, and the top posts.

The roadmap and long-term ideas are in the plan.

## Install on Unraid

The image is published to `ghcr.io/columbiagadgetworks/social-media:latest` on every push. Add the
template and icon:

```
wget -O /boot/config/plugins/dockerMan/templates-user/my-cgw-studio.xml https://raw.githubusercontent.com/ColumbiaGadgetWorks/social-media/HEAD/unraid/cgw-studio.xml
```

Then **Docker → Add Container → Template: cgw-studio**. For a first test on the LAN without the
proxy, set *Require proxy auth* and *Secure cookies* to `false`; turn both back on behind the proxy.

## Run it with docker compose

1. Copy the repo to the server, then `cp .env.example .env` and fill it in. At minimum set
   `STUDIO_BASE_URL`, the admin username and password, `STUDIO_TRUSTED_PROXIES` (your reverse
   proxy's LAN IP), and the SMTP2GO login.
2. `docker compose up -d --build`. The data lives in `/mnt/user/appdata/cgw-studio` and the media
   in `/mnt/user/cgw-media`. The container is capped at 1 GB of RAM; the app measured about 105 MB at idle.
3. Point your reverse proxy at `http://<unraid-ip>:8080`, and have it pass the signed-in user in
   the `Remote-User` header (Authelia and Authentik both do). **Block `/mcp` and `/api/ext/` at
   the proxy** (the Studio also refuses those through the proxy) and **let `/m/*` and `/u/*` through
   without sign-in** so Meta can fetch approved media and email recipients can unsubscribe.
4. Sign in with the admin account. Under **Users**, set each person's proxy username if it differs
   from their Studio username, and add an email address for reminders.

## Connect Claude Code (or Claude Desktop)

1. Set **LAN address** (`STUDIO_LAN_URL`, e.g. `http://192.168.1.50:8095`) in the Unraid template so
   Settings can fill in your address. Opening Settings directly on the LAN works too.
2. In the Studio, open **Settings** and create a token **For Claude Code**. The page shows:
   - a `claude mcp add --scope user ...` command for Claude Code or the **Code tab in Claude
     Desktop**. Run it once in a terminal (PowerShell, Terminal) on your computer.
   - a config block for the **Claude Desktop chat** (Settings → Developer → Edit Config; needs
     Node.js for `mcp-remote`).
   MCP only answers requests from local networks, never through the proxy.
3. Clone this repo (`cd $HOME\Documents; git clone https://github.com/ColumbiaGadgetWorks/social-media.git`)
   and open that folder in the Code tab, so `/cgw-session`, `/cgw-inbox` and the others are
   available. Run `/cgw-session` when the Monday digest says a session is due. In the Desktop chat, ask it to
   "start a CGW Studio session: call get_guidelines, then work through the queue".
4. Sonnet 5.5 is plenty for content sessions; use Opus 5.5 for the monthly review.

### If Claude says the cgw-studio tools aren't available

Claude Desktop and Claude Code connect to the Studio when they start and don't retry. If the
container was restarting (an update) or unreachable at that moment, the tools stay missing. In
Claude Code or the Code tab, run `/mcp`, pick **cgw-studio** and **Reconnect**; or quit and reopen
Claude Desktop. Still failing: open `http://<unraid-ip>:<port>/` in a browser on the same computer.
If that doesn't load either, the container isn't running or the port is wrong; if it does, check
the address and token in the MCP setup (Settings shows the command).

## Day to day

| When | What |
|---|---|
| Any time | Upload media (phone works fine), or drop photos in the Discord uploads channel |
| Monday digest email (only if something needs you) | Approve what goes out this week, run `/cgw-session` if it says so, batch day every 2 weeks, the Google Business Profile post on its day |
| "Last call" email (rare) | Something goes out within 2 days unapproved: approve it or move it |
| Alert email (rare) | A post failed to publish, the monthly email failed, or an announced event changed |

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
STUDIO_ADMIN_USERNAME=adam STUDIO_ADMIN_PASSWORD=dev-password-1 .venv/bin/python -m studio serve --port 8080
```

Other commands: `python -m studio create-user NAME --role approver` and `python -m studio tick`
(one background pass). Settings are environment variables; see `.env.example`.
