// Platform previews: an approximation of how each channel's post will look, built from the form as you type.
// Photos and videos flip with the arrows. Everything is drawn with textContent, so captions can't inject HTML.
(function () {
  const dataEl = document.getElementById("preview-data");
  const form = document.getElementById("post-form");
  if (!dataEl || !form) return;
  const data = JSON.parse(dataEl.textContent);
  const NAME = data.name;
  const slides = {}; // channel key -> the slide the viewer is on

  const h = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const add = (parent, ...kids) => { kids.forEach((k) => k && parent.append(k)); return parent; };
  const field = (key, name) => form.querySelector(`[name="${key}.${name}"]`)?.value ?? "";
  const svg = (path) => {
    const s = h("span", "sm-ico");
    s.innerHTML = `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
    return s;
  };
  const ICON = {
    heart: "<path d='M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.5A4 4 0 0 1 19 10c0 5.6-7 10-7 10z'/>",
    comment: "<path d='M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z'/>",
    share: "<path d='M22 3 11 14M22 3l-7 18-4-7-7-4 18-7z'/>",
    repost: "<path d='M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3'/>",
  };

  // Links, #tags and @names get the platform's link colour.
  function rich(text) {
    const frag = document.createDocumentFragment();
    text.split(/(https?:\/\/\S+|#\w+|@\w[\w.]*)/g).forEach((part, i) => {
      frag.append(i % 2 ? h("span", "lnk", part) : part);
    });
    return frag;
  }

  // Text cut off after `limit` characters with a "more" link, as feeds do.
  function clipped(text, limit, label) {
    const box = h("span");
    if (text.length <= limit) { box.append(rich(text)); return box; }
    const more = h("button", "sm-more", label);
    more.type = "button";
    box.append(rich(text.slice(0, limit).trimEnd() + "… "), more);
    more.addEventListener("click", () => { box.replaceChildren(rich(text)); });
    return box;
  }

  // Text with anything past the platform's limit marked in red.
  function limited(text, max) {
    const box = h("span");
    box.append(rich(text.slice(0, max)));
    if (text.length > max) box.append(h("span", "sm-over", text.slice(max)));
    return box;
  }

  function avatar() {
    const a = h("span", "sm-avatar");
    const img = h("img");
    img.src = data.logo; img.alt = "";
    a.append(img);
    return a;
  }

  function head(sub) {
    const row = h("div", "sm-head");
    const who = h("div", "sm-who");
    add(who, h("b", null, NAME), sub ? h("span", "sm-sub", sub) : null);
    return add(row, avatar(), who);
  }

  // One photo at a time with arrows, dots and a count, cropped the way the platform crops.
  function carousel(key, items, ratio, fit) {
    if (!items.length) return null;
    const wrap = h("div", "sm-media");
    wrap.style.aspectRatio = ratio;
    const stage = h("div", "sm-stage");
    const count = h("span", "sm-count");
    const dots = h("div", "sm-dots");
    const prev = h("button", "sm-arrow sm-prev", "‹");
    const next = h("button", "sm-arrow sm-next", "›");
    prev.type = next.type = "button";
    prev.setAttribute("aria-label", "Previous photo");
    next.setAttribute("aria-label", "Next photo");

    const draw = () => {
      const i = Math.min(Math.max(slides[key] ?? 0, 0), items.length - 1);
      slides[key] = i;
      const item = items[i];
      let node;
      if (item.kind === "video" && item.video) {
        node = h("video");
        node.controls = true; node.muted = true; node.playsInline = true; node.preload = "metadata";
        node.src = item.video;
        if (item.src) node.poster = item.src;
      } else {
        node = h("img");
        node.src = item.src; node.alt = item.alt || "";
        if (item.alt) node.title = item.alt;
      }
      node.style.objectFit = fit;
      stage.replaceChildren(node);
      count.textContent = `${i + 1}/${items.length}`;
      dots.replaceChildren(...items.map((_, j) => h("span", j === i ? "on" : "")));
      prev.hidden = i === 0;
      next.hidden = i === items.length - 1;
    };
    const go = (d) => { slides[key] = (slides[key] ?? 0) + d; draw(); };
    prev.addEventListener("click", () => go(-1));
    next.addEventListener("click", () => go(1));
    add(wrap, stage);
    if (items.length > 1) add(wrap, prev, next, count);
    draw();
    return items.length > 1 ? add(h("div"), wrap, dots) : wrap;
  }

  const actions = (icons) => add(h("div", "sm-actions"), ...icons.map((i) => svg(ICON[i])));
  const textRow = (...labels) => add(h("div", "sm-actions sm-labels"), ...labels.map((l) => h("span", null, l)));

  // ---- one builder per platform ----------------------------------------------------------------

  const builders = {
    instagram(p) {
      const root = h("div", "sm sm-instagram");
      const cap = add(h("div", "sm-text"), h("b", null, NAME + " "), clipped(p.full, 125, "more"));
      return add(root, head(), carousel("instagram", p.items, "4 / 5", "cover"), actions(["heart", "comment", "share"]), cap);
    },
    facebook(p) {
      const root = h("div", "sm sm-facebook");
      const cap = add(h("div", "sm-text"), clipped(p.full, 480, "See more"));
      return add(root, head("Just now · Public"), cap, carousel("facebook", p.items, "1 / 1", "contain"), textRow("Like", "Comment", "Share"));
    },
    threads(p) {
      const root = h("div", "sm sm-threads");
      const cap = add(h("div", "sm-text"), limited(p.full, p.max));
      return add(root, head("now"), cap, carousel("threads", p.items, "4 / 5", "cover"), actions(["heart", "comment", "repost", "share"]));
    },
    bluesky(p) {
      const root = h("div", "sm sm-bluesky");
      const cap = add(h("div", "sm-text"), limited(p.full, p.max));
      return add(root, head("now"), cap, carousel("bluesky", p.items, "16 / 10", "cover"), actions(["comment", "repost", "heart", "share"]));
    },
    x(p) {
      const root = h("div", "sm sm-x");
      const cap = add(h("div", "sm-text"), limited(p.full, p.max));
      return add(root, head("now"), cap, carousel("x", p.items, "16 / 9", "cover"), actions(["comment", "repost", "heart", "share"]));
    },
    linkedin(p) {
      const root = h("div", "sm sm-linkedin");
      const cap = add(h("div", "sm-text"), clipped(p.full, 210, "…more"));
      return add(root, head("Nonprofit · now"), cap, carousel("linkedin", p.items, "1 / 1", "contain"), textRow("Like", "Comment", "Repost", "Send"));
    },
    gbp(p) {
      const root = h("div", "sm sm-gbp");
      const cap = add(h("div", "sm-text"), clipped(p.full, 140, "More"));
      return add(root, carousel("gbp", p.items, "4 / 3", "cover"), head("Update · now"), cap);
    },
    website(p) {
      const root = h("div", "sm sm-website");
      const article = h("div", "sm-article");
      add(article, h("h3", null, p.title || "(no title)"), h("div", "sm-sub", "Columbia Gadget Works news"));
      p.body.split(/\n{2,}/).filter((t) => t.trim()).forEach((para) => article.append(markdown(para)));
      return add(root, carousel("website", p.items, "16 / 9", "cover"), article);
    },
  };

  // Short-form video: a phone-shaped frame with the caption laid over the bottom.
  function shortVideo(key, label) {
    return (p) => {
      const root = h("div", `sm sm-reel sm-${key}`);
      const phone = h("div", "sm-phone");
      const first = p.items.find((m) => m.kind === "video") || p.items[0];
      const stage = h("div", "sm-stage");
      if (first) {
        let node;
        if (first.kind === "video" && first.video) {
          node = h("video");
          node.controls = true; node.muted = true; node.playsInline = true; node.preload = "metadata";
          node.src = first.video;
          if (first.src) node.poster = first.src;
        } else { node = h("img"); node.src = first.src; node.alt = first.alt || ""; }
        node.style.objectFit = "cover";
        stage.append(node);
      }
      const over = h("div", "sm-over-text");
      add(over, h("b", null, "@" + NAME.replace(/\s+/g, "").toLowerCase()),
        p.title ? h("div", "sm-reel-title", p.title) : null,
        add(h("div", "sm-reel-cap"), clipped(p.full, 110, "more")));
      const rail = add(h("div", "sm-rail"), svg(ICON.heart), svg(ICON.comment), svg(ICON.share));
      add(phone, stage, rail, over, h("span", "sm-pill", label));
      return add(root, phone);
    };
  }
  builders.youtube_shorts = shortVideo("youtube_shorts", "Shorts");
  builders.tiktok = shortVideo("tiktok", "TikTok");

  // The small subset of Markdown the website channel allows: **bold**, [links](/url) and line breaks.
  function markdown(para) {
    const p = h("p");
    para.split("\n").forEach((line, li) => {
      if (li) p.append(h("br"));
      line.split(/(\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g).forEach((part, i) => {
        if (i % 2 === 0) { p.append(part); return; }
        if (part.startsWith("**")) { p.append(h("b", null, part.slice(2, -2))); return; }
        const m = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        p.append(h("span", "lnk", m ? m[1] : part));
      });
    });
    return p;
  }

  // ---- render ---------------------------------------------------------------------------------

  function render(slot) {
    const key = slot.dataset.channel;
    const cfg = data.channels[key];
    const build = builders[key];
    if (!cfg || !build) return;
    const body = field(key, "body").trim();
    const tags = field(key, "hashtags").trim();
    const items = (cfg.max_images ? data.media.slice(0, cfg.max_images) : data.media).filter((m) => m.src || m.video);
    const p = {
      body, tags, title: field(key, "title").trim(), max: cfg.max_chars, items,
      full: [body, tags].filter(Boolean).join("\n\n"),
    };
    const notes = h("div", "sm-note small muted");
    const extra = cfg.max_images && data.media.length > cfg.max_images ? data.media.length - cfg.max_images : 0;
    notes.textContent = [
      "Approximation of how it will look.",
      extra ? `Only the first ${cfg.max_images} of ${data.media.length} photos will post.` : "",
      p.full.length > cfg.max_chars ? `Over the limit by ${p.full.length - cfg.max_chars} characters.` : "",
    ].filter(Boolean).join(" ");
    slot.replaceChildren(build(p), notes);
  }

  let queued = false;
  function renderAll() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      document.querySelectorAll(".sm-slot").forEach(render);
    });
  }
  form.addEventListener("input", renderAll);
  renderAll();
})();
