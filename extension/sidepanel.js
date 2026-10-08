const $ = (s) => document.querySelector(s);
const channelSel = $("#channel");
let current = null;

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function copyButton(label, text) {
  const b = document.createElement("button");
  b.textContent = label;
  b.onclick = async () => {
    await navigator.clipboard.writeText(text);
    b.textContent = "Copied";
    setTimeout(() => (b.textContent = label), 1200);
  };
  return b;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderItem(item) {
  const box = el("div", "item");
  box.append(el("div", "when", `${String(item.number).padStart(2, "0")} · ${item.when}`));
  if (!item.approved) box.append(el("div", "bad", `Blocked: ${item.blocked_reason}`));
  if (!item.within_horizon) box.append(el("div", "muted", "Past this platform's scheduling limit: wait for the next batch."));
  if (item.overdue) box.append(el("div", "bad", "Overdue"));
  box.append(el("div", "muted", `${item.post_title} · ${item.media.map((m) => m.filename).join(", ") || "no media"}`));
  if (item.title) box.append(el("div", "text", item.title));
  box.append(el("div", "text", item.caption));

  const copies = el("div", "row");
  copies.append(copyButton("Copy date", item.date), copyButton("Copy time", item.time));
  if (item.title) copies.append(copyButton("Copy title", item.title));
  copies.append(copyButton("Copy caption", item.caption));
  box.append(copies);

  const actions = el("div", "row");
  const fill = el("button", "primary", "Fill this page");
  const report = el("div", "report");
  fill.disabled = !item.approved;
  fill.onclick = async () => {
    const tab = await activeTab();
    fill.disabled = true;
    report.textContent = item.media.length ? "Sending media to the page…" : "Filling…";
    const res = await send({ type: "fill", tabId: tab.id, platform: channelSel.value, item });
    fill.disabled = false;
    if (!res || !res.ok) { report.className = "report bad"; report.textContent = res ? res.error : "No answer"; return; }
    const r = res.data || { done: [], missing: [] };
    report.className = "report";
    report.innerHTML = "";
    if (r.done.length) report.append(el("div", "ok", `Filled: ${r.done.join(", ")}`));
    if (r.missing.length) report.append(el("div", "bad", `Not found, paste it yourself: ${r.missing.join(", ")}`));
    if (r.next) report.append(el("div", "muted", `Next: ${r.next} Then press Schedule yourself.`));
  };
  const url = el("input");
  url.placeholder = "Post link (optional)";
  url.size = 18;
  const mark = el("button", "", "Mark scheduled");
  mark.disabled = !item.approved;
  mark.onclick = async () => {
    const res = await send({ type: "scheduled", versionId: item.version_id, url: url.value });
    if (res && res.ok) { box.classList.add("done"); mark.textContent = "Scheduled ✓"; mark.disabled = true; }
    else report.textContent = res ? res.error : "No answer";
  };
  actions.append(fill, url, mark);
  box.append(actions, report);
  return box;
}

async function load() {
  const key = channelSel.value;
  $("#items").innerHTML = "";
  $("#status").textContent = "Loading…";
  const res = await send({ type: "batch", channel: key });
  if (!res || !res.ok) { $("#status").textContent = res ? res.error : "No answer from the extension."; return; }
  const items = res.data.items;
  $("#status").textContent = items.length
    ? `${items.length} approved post(s) for ${res.data.label}. Fill, check, press Schedule, then Mark scheduled.`
    : `Nothing approved for ${res.data.label} in the next two weeks.`;
  for (const item of items) $("#items").append(renderItem(item));
}

async function init() {
  const res = await send({ type: "overview" });
  if (!res || !res.ok) { $("#status").textContent = res ? res.error : "Open Options to connect."; return; }
  for (const c of res.data.channels) {
    const o = el("option", "", `${c.label} (${c.count})`);
    o.value = c.key;
    channelSel.append(o);
  }
  const tab = await activeTab();
  const detected = tab && self.CGW_PLATFORM_FOR(tab.url);
  if (detected && [...channelSel.options].some((o) => o.value === detected)) channelSel.value = detected;
  current = channelSel.value;
  channelSel.onchange = load;
  await load();
}

chrome.tabs.onActivated.addListener(async () => {
  const tab = await activeTab();
  const detected = tab && self.CGW_PLATFORM_FOR(tab.url);
  if (detected && detected !== current && [...channelSel.options].some((o) => o.value === detected)) {
    channelSel.value = current = detected;
    load();
  }
});

init();
