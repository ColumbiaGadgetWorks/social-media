---
description: Work through everything the CGW Content Studio has waiting for Claude
---
Use the `cgw-studio` MCP server.

1. Call `get_guidelines` and follow it for the whole session.
2. Call `get_work_queue`. If it's empty, say so, then call `get_schedule` and `search_media`
   with `unused_only: true` and suggest up to 3 posts to fill the emptiest days in the next two
   weeks (create them with `create_post` + `submit_drafts` only if I agree).
3. Otherwise process up to 10 items, oldest first, exactly as the guidelines describe:
   `get_work_item`, then `submit_drafts` with per-channel captions, times, descriptions and alt
   text. Fix any returned problems.
4. End with a short summary: drafted items, open days in the next two weeks, and anything the
   reviewer should double-check. Remind me to review at the Studio's Review page.

$ARGUMENTS
