# CGW Studio Batch Helper (browser extension)

Speeds up batch day for the platforms the Studio can't post to directly (TikTok, YouTube Shorts,
X, LinkedIn, Google Business Profile, and Instagram/Facebook/Threads until they're connected).
It fills each platform's own upload form from the approved batch. **You still check it and press
Schedule yourself.**

## Install (Chrome, Edge, Brave)

1. Copy this `extension` folder to your computer (or clone the repo).
2. Go to `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick the folder.
3. In the Studio: **Settings → Create token**, type **For the browser extension**. Copy it.
4. Click the extension's icon → **Options**. Enter the Studio's **LAN** address (for example
   `http://192.168.1.50:8080`) and the token, then **Save and test**. Allow access when asked.

The extension only talks to the Studio on your local network; the public address blocks it.

## Batch day

1. Open the platform's upload page (TikTok Studio, YouTube Studio's upload dialog, X's composer…).
2. Click the extension icon to open the side panel. It picks the platform from the open tab.
3. For each post: **Fill this page** attaches the photo or video and fills the title and caption.
   Anything it can't find is listed; use the copy buttons for those.
4. Set the date and time shown (use **Copy date** / **Copy time**), check everything, press the
   platform's **Schedule** button, then **Mark scheduled** (paste the post link if you have it).

## When a platform changes its page

Fields that can't be found are listed instead of guessed. The selectors for each site live in
`content/sites.js`; run `/cgw-fix-extension` in Claude Code with a description (or saved HTML) of
the new page, then reload the extension in `chrome://extensions`.
