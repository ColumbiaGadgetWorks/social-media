---
description: Work through everything the CGW Content Studio has waiting for Claude
---
Use the `cgw-studio` MCP server. If its tools aren't available, stop and tell me: "The Studio
isn't connected. Run /mcp, pick cgw-studio and Reconnect (or quit and reopen Claude Desktop). If it
still fails, check the Studio opens in a browser at its LAN address." Don't draft anything without
the tools.

1. Call `get_guidelines` and follow it for the whole session.
2. Call `get_work_queue`. If it's empty, say so, then call `get_schedule` and `search_media`
   with `unused_only: true` and suggest up to 3 posts to fill the emptiest days in the next two
   weeks (create them with `create_post` + `submit_drafts` only if I agree).
3. Otherwise process up to 10 items in queue order (most urgent first), exactly as the guidelines describe:
   `get_work_item`, then `submit_drafts` with per-channel captions, times, descriptions and alt
   text. Fix any returned problems. For weekly event posts (Open Hack Night), follow "Weekly events"
   in the guidelines and ask me for photos or a story when there's nothing good to show; it's fine
   to collect my answers for several items at once before drafting them.
4. End with a short summary: drafted items, open days in the next two weeks, and anything the
   reviewer should double-check. Remind me to review at the Studio's Review page.

$ARGUMENTS
