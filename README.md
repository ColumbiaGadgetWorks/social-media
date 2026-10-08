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
  send. A "Claude session needed" email tells you when to run `/cgw-session`.
- **Approval rule.** Only a signed-in Approver can approve. The approval is tied to a hash of the
  exact captions, media and times, and any later edit clears it. The approve button also checks the
  version the approver was looking at, and publishers re-check the hash before sending.
- **Bluesky** publishes automatically at the scheduled time (photos and text).
- **Batch pages** for Instagram, Facebook, Threads, TikTok, YouTube Shorts, LinkedIn, X and
  Google Business Profile: numbered files, copy buttons, a zip (photos re-encoded without GPS
  data), and "Mark scheduled". Instagram, Facebook and Threads move to automatic posting in Phase 2.
- **Reminder emails**: Claude session due, approvals waiting, batch day (every 14 days), Google
  Business Profile posts due today, and publishing failures.
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

## Connect Claude Code

1. In the Studio, open **Settings**, create a token, and copy the command it shows:
   ```
   claude mcp add --transport http cgw-studio http://<unraid-ip>:8080/mcp \
     --header "Authorization: Bearer cgw_..."
   ```
   Use the LAN address (not the public one). MCP only answers requests from local networks.
2. Open Claude Code in a checkout of this repo, so the `/cgw-session` and `/cgw-inbox` commands are
   available, and run `/cgw-session` whenever the Studio emails you.
3. Sonnet 5.5 is plenty for content sessions; use Opus 5.5 for the monthly review.

## Day to day

| When | What |
|---|---|
| Any time | Upload media (phone works fine) |
| When emailed "Claude session needed" | Run `/cgw-session` in Claude Code (~15 min) |
| When emailed "waiting for approval" | Review and approve on the Review page |
| Every 2 weeks (batch day email) | Schedule the batch for each platform, mark each scheduled |
| When emailed "Post today" | Post the Google Business Profile item by hand |

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
STUDIO_ADMIN_USERNAME=adam STUDIO_ADMIN_PASSWORD=dev-password-1 .venv/bin/python -m studio serve --port 8080
```

Other commands: `python -m studio create-user NAME --role approver` and `python -m studio tick`
(one background pass). Settings are environment variables; see `.env.example`.
