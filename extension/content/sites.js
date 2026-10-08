// Where each platform's upload form keeps its fields. Platforms change their pages, so every
// field has several candidate selectors, tried in order. When none match, the side panel says
// so and you paste from its copy buttons. Fix selectors here (or ask Claude Code to).
self.CGW_SITES = {
  youtube_shorts: {
    hosts: ["studio.youtube.com"],
    file: ['input[type="file"][name="Filedata"]', "ytcp-uploads-file-picker input[type=file]", "input[type=file]"],
    title: ["#title-textarea #textbox", "ytcp-social-suggestions-textbox#title-textarea [contenteditable=true]"],
    caption: ["#description-textarea #textbox", "ytcp-social-suggestions-textbox#description-textarea [contenteditable=true]"],
    waitAfterFile: true,
    next: "Choose \"No, it's not made for kids\", then Visibility > Schedule and set the date and time shown.",
  },
  tiktok: {
    hosts: ["www.tiktok.com"],
    file: ['input[type="file"][accept*="video"]', "input[type=file]"],
    caption: ['.public-DraftEditor-content[contenteditable="true"]', 'div[contenteditable="true"][role="combobox"]',
              'div[contenteditable="true"]'],
    waitAfterFile: true,
    next: "Turn on Schedule and set the date and time shown (TikTok allows up to 10 days ahead).",
  },
  x: {
    hosts: ["x.com", "twitter.com"],
    file: ['input[data-testid="fileInput"]', "input[type=file]"],
    caption: ['[data-testid="tweetTextarea_0"]', 'div[role="textbox"][contenteditable="true"]'],
    next: "Click the calendar (Schedule) icon and set the date and time shown.",
  },
  linkedin: {
    hosts: ["www.linkedin.com"],
    file: ["input[type=file]"],
    caption: ['.ql-editor[contenteditable="true"]', 'div[role="textbox"][contenteditable="true"]'],
    next: "Open the post box as the CGW page first. Use the clock icon to schedule.",
  },
  gbp: {
    hosts: ["business.google.com", "www.google.com"],
    file: ["input[type=file]"],
    caption: ["textarea", 'div[contenteditable="true"]'],
    next: "Google Business Profile can't schedule: post it now, then Mark posted.",
  },
  instagram: {
    hosts: ["www.instagram.com"],
    file: ["input[type=file]"],
    caption: ['div[aria-label][contenteditable="true"]', 'div[contenteditable="true"]', "textarea"],
    waitAfterFile: true,
    next: "Instagram's web uploader can't schedule; use Meta Business Suite to schedule instead.",
  },
  facebook: {
    hosts: ["business.facebook.com", "www.facebook.com"],
    file: ["input[type=file]"],
    caption: ['div[role="textbox"][contenteditable="true"]', "textarea"],
    next: "In Meta Business Suite, choose Schedule and set the date and time shown.",
  },
  threads: {
    hosts: ["www.threads.net", "www.threads.com"],
    file: ["input[type=file]"],
    caption: ['div[role="textbox"][contenteditable="true"]', 'div[contenteditable="true"]'],
    next: "Use the ... menu > Schedule to pick the date and time shown.",
  },
};

self.CGW_PLATFORM_FOR = function (url) {
  let host;
  try { host = new URL(url).host; } catch { return null; }
  for (const [key, site] of Object.entries(self.CGW_SITES)) {
    if (site.hosts.includes(host)) return key;
  }
  return null;
};
