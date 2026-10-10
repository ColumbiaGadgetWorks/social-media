---
description: Draft promo posts for upcoming calendar events
---
Use the `cgw-studio` MCP server. If its tools aren't available, stop and tell me: "The Studio
isn't connected. Run /mcp, pick cgw-studio and Reconnect (or quit and reopen Claude Desktop). If it
still fails, check the Studio opens in a browser at its LAN address." Don't draft anything without
the tools. Call `get_guidelines`, then `get_events` and `get_work_queue`.

For each queued post that has an `event` (announce, reminder, weekly, or cancellation):
1. `get_work_item` to see the event facts, the attached event card, and `aim_for`.
2. If a real photo would be better than (or alongside) the card, `search_media` for the tool or
   topic and `attach_media`. Keep the card when the date and time need to be seen at a glance.
3. `submit_drafts` with captions per channel scheduled near `aim_for`. Use only the event's facts
   for dates, times, places, prices and links. Add a `website` version for announcements of
   classes and public events (headline title, short Markdown body, the link).

For `weekly` posts (Open Hack Night), follow "Weekly events" in the guidelines: use the suggested
angle, real photos from `weekly.candidate_media`, nothing repeated from `weekly.recent_posts`, and
ask me for photos or a story first when there's nothing good to show.

If an upcoming event has no promo posts and seems worth promoting, say so in the summary instead
of creating posts for it.

$ARGUMENTS
