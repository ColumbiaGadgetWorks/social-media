---
description: Draft promo posts for upcoming calendar events
---
Use the `cgw-studio` MCP server. Call `get_guidelines`, then `get_events` and `get_work_queue`.

For each queued post that has an `event` (announce, reminder, or cancellation):
1. `get_work_item` to see the event facts, the attached event card, and `aim_for`.
2. If a real photo would be better than (or alongside) the card, `search_media` for the tool or
   topic and `attach_media`. Keep the card when the date and time need to be seen at a glance.
3. `submit_drafts` with captions per channel scheduled near `aim_for`. Use only the event's facts
   for dates, times, places, prices and links. Add a `website` version for announcements of
   classes and public events (headline title, short Markdown body, the link).

If an upcoming event has no promo posts and seems worth promoting, say so in the summary instead
of creating posts for it.

$ARGUMENTS
