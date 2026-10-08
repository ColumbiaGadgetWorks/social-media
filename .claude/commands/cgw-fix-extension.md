---
description: Update the browser extension's selectors after a platform changes its upload page
---
This is a code session in this repo (not a content session).

The CGW Studio Batch Helper extension couldn't find some fields on a platform's upload page.
Selectors live in `extension/content/sites.js` (one entry per platform: `file`, `title`,
`caption`, tried in order). The filling logic is `extension/content/fill.js`.

1. Ask me which platform and which fields were reported missing, and for the page's HTML around
   the upload form (Chrome: right-click the field → Inspect → right-click the element → Copy →
   Copy outerHTML), unless I already gave it below.
2. Add robust selectors to the front of that platform's lists (prefer stable attributes such as
   `data-testid`, `aria-label`, `name`, roles; avoid generated class names). Keep the old ones as
   fallbacks.
3. Run `.venv/bin/python -m pytest tests/test_extension_e2e.py` and the rest of the suite.
4. Tell me to reload the extension at `chrome://extensions`.

$ARGUMENTS
