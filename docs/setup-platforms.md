# Connecting platforms

Each connection is optional. Until a platform is connected, its posts go to batch day, so you can
connect them one at a time. **Settings → Connections** in the Studio shows what's connected.
Meta changes its developer dashboard often; if a screen below looks different, the permission
names are what matter.

## 1. Reverse proxy rules (do this first)

| Path | Rule |
|---|---|
| `/m/*` | **Public, no sign-in.** Meta's servers fetch approved photos and videos here. Links are signed and stop working when a post isn't approved. |
| `/u/*` | **Public, no sign-in.** Unsubscribe links in emails (including one-click unsubscribe from Gmail and Yahoo). |
| `/mcp` | **Blocked.** Claude Code connects on the LAN directly to port 8080. |
| `/api/ext/*` | **Blocked.** The browser extension connects on the LAN directly to port 8080. |
| everything else | Behind proxy auth, as now. |

If `/m/` should be served from a different hostname than the one people use, set
`STUDIO_PUBLIC_URL`.

## 2. Facebook page and Instagram

Requirements: the Instagram account is a Business account (it is) and it's linked to the CGW
Facebook page (Instagram app → Settings → Business tools → Connect a Facebook page).

1. At <https://developers.facebook.com/apps>, create an app of the **Business** type, connected
   to CGW's business portfolio. Add the use cases for managing a Facebook Page and for the
   Instagram API with Facebook Login.
2. Grant it these permissions: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
   `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`, `business_management`.
   Only people with a role on the app use it, so it can usually stay in development mode
   without App Review. If Meta asks for review or business verification, it's free.
3. In the **Graph API Explorer**, pick the app, choose *Get User Access Token* with those
   permissions, and sign in as a page admin.
4. Exchange that for a long-lived user token:
   ```
   curl "https://graph.facebook.com/v24.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN"
   ```
5. Get the page token and ID (a page token made from a long-lived user token doesn't expire):
   ```
   curl "https://graph.facebook.com/v24.0/me/accounts?access_token=LONG_USER_TOKEN"
   ```
   Use the CGW entry's `id` as **STUDIO_META_PAGE_ID** and its `access_token` as
   **STUDIO_META_PAGE_TOKEN**.
6. Get the Instagram account ID:
   ```
   curl "https://graph.facebook.com/v24.0/PAGE_ID?fields=instagram_business_account&access_token=PAGE_TOKEN"
   ```
   That `id` is **STUDIO_META_IG_USER_ID**.
7. Restart the container. Approve a test post scheduled a few minutes out, and check that it
   appears and the Studio records its link.

Instagram feed photos must be between 4:5 and 1.91:1; the Studio pads taller or wider photos
onto a blurred background instead of cropping them. Videos post as Reels.

## 3. Threads

1. In the same Meta app (or a new one), add the **Threads API** use case with `threads_basic`,
   `threads_content_publish` and `threads_manage_insights`. Add the CGW Threads account as a
   Threads tester under app roles, and accept the invite in Threads (Settings → Account →
   Website permissions).
2. Authorize in a browser (redirect URI must match one set in the app):
   `https://threads.net/oauth/authorize?client_id=APP_ID&redirect_uri=REDIRECT&scope=threads_basic,threads_content_publish,threads_manage_insights&response_type=code`
3. Exchange the code, then make it long-lived:
   ```
   curl -X POST https://graph.threads.net/oauth/access_token -d client_id=APP_ID -d client_secret=APP_SECRET -d grant_type=authorization_code -d redirect_uri=REDIRECT -d code=CODE
   curl "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=APP_SECRET&access_token=SHORT_TOKEN"
   curl "https://graph.threads.net/v1.0/me?fields=id,username&access_token=LONG_TOKEN"
   ```
4. Set **STUDIO_THREADS_USER_ID** and **STUDIO_THREADS_TOKEN**. Long-lived tokens last 60 days;
   the Studio refreshes it every 20 days and keeps the new one in its database.

## 4. Website news posts

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate. Resource owner:
   ColumbiaGadgetWorks. Repository access: only `website`. Permissions: **Contents: Read and write**.
   (An org admin may need to approve it.) Note the expiry date; renew before it.
2. Set **STUDIO_GITHUB_TOKEN** and restart.
3. Each approved website post becomes one commit to `main` at its scheduled time:
   `content/news/<slug>.md` plus photos in `assets/img/`. Cloudflare deploys it within minutes.
   The commit message names the post and its approver.

## 5. Dolibarr (monthly email)

1. In Dolibarr, create a user for the Studio (or use an existing one) with permission to read and
   create/modify **contacts**, read and modify **tags/categories**, and create **agenda events**.
   Under that user's card, generate an **API key**. The REST API module must be enabled
   (Setup → Modules → API REST).
2. Set **STUDIO_DOLIBARR_URL** (Dolibarr's address, reachable from the Studio container; no
   trailing slash), **STUDIO_DOLIBARR_API_KEY**, and **STUDIO_DOLIBARR_USER_ID** (that user's id,
   used as the owner of the "email sent" agenda events).
3. The list is the contacts tagged **Email updates** (change with STUDIO_DOLIBARR_TAG if the
   onboarding module uses a different name). Settings → Connections shows whether it's reachable;
   an email's page shows how many people are on the list.
4. Sending goes through the same SMTP2GO login as reminders. Make sure the From address
   (STUDIO_ANNOUNCE_FROM, default `mail@columbiagadgetworks.org`) is a verified sender in SMTP2GO.

## 6. Discord uploads (optional)

Anyone in the uploads channel can drop photos or videos there; they land in the media library and,
unless the message says `library`, a post is queued for the next Claude session. The message text
becomes the note. The bot only reads the channel (it checks every minute), so nothing new has to be
reachable from the internet.

1. https://discord.com/developers/applications → **New Application** (e.g. "CGW Studio").
2. **Bot** tab: **Reset Token**, copy it (that's `STUDIO_DISCORD_BOT_TOKEN`). Turn on **Message
   Content Intent** (free for bots in fewer than 100 servers; without it the bot sees empty
   messages). Leave "Public Bot" off.
3. **OAuth2 → URL Generator**: scope **bot**; permissions **View Channels**, **Read Message
   History**, **Add Reactions**, and **Send Messages** (only used to say why a file couldn't be
   taken). Open the generated link and add the bot to the CGW server.
4. Create a channel such as `#studio-uploads` and make sure the bot's role can see it. Who can post
   there is up to the channel's permissions.
5. Discord settings → Advanced → **Developer Mode** on. Right-click the channel → **Copy Channel
   ID** (that's `STUDIO_DISCORD_CHANNEL_IDS`; several channels: separate with commas).
6. Set both in the Unraid template and apply. Settings → Connections shows "Discord uploads".
   The bot starts with messages posted after it first runs; it doesn't import the channel's history.

In the channel:

- Photos/videos with a line of text → one post (a carousel for several photos; each video on its
  own). The bot reacts ✅ when they're in.
- Add `library` to the message to file the media without a post; `separate` for one post per file.
- Photos sent during an event (from two hours before to six after) are marked "Taken at" that
  event, so Hack Night photos feed next week's Hack Night post.
- Too big or not a photo/video: the bot reacts ⚠️ and replies with the reason. Discord's own size
  limit (10 MB without Nitro) applies before ours.
- The sender's Discord name is recorded in the note ("sent by Sam on Discord"). That's who shared
  it, not necessarily who made it; captions credit makers only when the text says so.

## 7. Apply for the remaining APIs (free, takes weeks)

These move channels from batch day to automatic when approved:

- **YouTube Shorts:** create a Google Cloud project with the YouTube Data API, then submit the
  YouTube API Services audit (Google's "YouTube API Services - Audit and Quota Extension Form").
  Until the audit passes, API uploads are locked to private.
- **Google Business Profile:** request Business Profile API access through Google's GBP API
  contact form for the same Cloud project. (CGW's profile can't schedule posts, so this would
  let the Studio post on the day instead of you.)
- **TikTok:** create an app at <https://developers.tiktok.com>, add the Content Posting API, and
  apply for the audit. Unaudited apps can only post privately.

Until then, batch day covers them, and the browser extension speeds it up.
