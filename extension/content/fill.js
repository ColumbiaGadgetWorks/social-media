// Injected into the platform's page on demand. Receives media in chunks from the background
// worker, then attaches files and fills text fields. It never presses Post or Schedule.
(() => {
  if (self.__cgwFillLoaded) return;
  self.__cgwFillLoaded = true;

  const incoming = {}; // media id -> {name, mime, parts: []}

  function decode(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  function find(selectors) {
    for (const s of selectors || []) {
      const el = document.querySelector(s);
      if (el) return el;
    }
    return null;
  }

  async function waitFor(selectors, ms) {
    const until = Date.now() + ms;
    while (Date.now() < until) {
      const el = find(selectors);
      if (el) return el;
      await new Promise((r) => setTimeout(r, 300));
    }
    return null;
  }

  function mark(el, ok) {
    el.style.outline = ok ? "3px solid #2f7a4d" : "3px solid #a63a2b";
    el.style.outlineOffset = "2px";
  }

  function setValue(el, text) {
    if (el.isContentEditable) {
      el.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      // Typing-style edits keep rich editors (Draft.js, Quill, YouTube's textbox) in sync with their
      // own state; line breaks go in as paragraphs, the way a person pressing Enter would.
      document.execCommand("delete", false);
      let ok = true;
      text.split("\n").forEach((line, i) => {
        if (i > 0) ok = document.execCommand("insertParagraph", false) && ok;
        if (line) ok = document.execCommand("insertText", false, line) && ok;
      });
      const squash = (t) => t.replace(/\s+/g, " ").trim();
      if (!ok || squash(el.innerText) !== squash(text)) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
      }
    } else {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    mark(el, true);
  }

  function attach(input, files) {
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    input.files = dt.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function fill(platform, item, opts = {}) {
    const site = self.CGW_SITES[platform];
    const report = { done: [], missing: [], next: site ? site.next : "" };
    if (!site) {
      report.missing.push("this page isn't a known upload page");
      return report;
    }
    const files = (item.media || []).map((m) => {
      const got = incoming[m.media_id];
      return got ? new File(got.parts, m.filename, { type: m.mime }) : null;
    }).filter(Boolean);
    if (files.length) {
      const input = find(site.file);
      if (input) {
        attach(input, input.multiple ? files : files.slice(0, 1));
        report.done.push(files.length > 1 && !input.multiple ? "file (first only)" : "file");
      } else {
        report.missing.push("file upload");
      }
    }
    const wait = site.waitAfterFile && files.length ? opts.waitMs ?? 20000 : 1500;
    if (site.title && item.title) {
      const el = await waitFor(site.title, wait);
      if (el) { setValue(el, item.title); report.done.push("title"); } else report.missing.push("title");
    }
    if (site.caption && item.caption) {
      const el = await waitFor(site.caption, wait);
      if (el) { setValue(el, item.caption); report.done.push("caption"); } else report.missing.push("caption");
    }
    for (const id of Object.keys(incoming)) delete incoming[id];
    return report;
  }

  self.cgwFill = fill;
  self.cgwReceive = function (msg) {
    const f = (incoming[msg.mediaId] ||= { parts: [] });
    f.parts[msg.index] = decode(msg.data);
  };

  if (self.chrome && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
      if (msg.type === "cgw-chunk") {
        self.cgwReceive(msg);
        reply({ ok: true });
      } else if (msg.type === "cgw-fill") {
        fill(msg.platform, msg.item).then(reply);
        return true; // async reply
      }
      return false;
    });
  }
})();
