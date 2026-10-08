// Talks to the Studio (LAN address + extension token from Options) and feeds the page script.
const CHUNK = 512 * 1024;

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

async function config() {
  const { studioUrl = "", token = "" } = await chrome.storage.local.get(["studioUrl", "token"]);
  return { studioUrl: studioUrl.replace(/\/+$/, ""), token };
}

async function api(path, init = {}) {
  const { studioUrl, token } = await config();
  if (!studioUrl || !token) throw new Error("Set the Studio address and token in the extension's Options.");
  const res = await fetch(studioUrl + path, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, ...(init.headers || {}) },
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch {}
    throw new Error(`Studio said ${res.status}${detail ? ": " + detail : ""}`);
  }
  return res;
}

function toBase64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

async function fillTab(tabId, platform, item) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content/sites.js", "content/fill.js"] });
  for (const m of item.media || []) {
    const bytes = new Uint8Array(await (await api(m.url)).arrayBuffer());
    for (let i = 0, n = 0; i < bytes.length; i += CHUNK, n++) {
      await chrome.tabs.sendMessage(tabId, { type: "cgw-chunk", mediaId: m.media_id, index: n,
                                             data: toBase64(bytes.subarray(i, i + CHUNK)) });
    }
  }
  return chrome.tabs.sendMessage(tabId, { type: "cgw-fill", platform, item });
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    try {
      if (msg.type === "batch") reply({ ok: true, data: await (await api(`/api/ext/batch/${msg.channel}`)).json() });
      else if (msg.type === "overview") reply({ ok: true, data: await (await api("/api/ext/batch")).json() });
      else if (msg.type === "fill") reply({ ok: true, data: await fillTab(msg.tabId, msg.platform, msg.item) });
      else if (msg.type === "scheduled") {
        const res = await api(`/api/ext/versions/${msg.versionId}/scheduled`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: msg.url || "" }),
        });
        reply({ ok: true, data: await res.json() });
      } else reply({ ok: false, error: "unknown request" });
    } catch (err) {
      reply({ ok: false, error: String(err.message || err) });
    }
  })();
  return true;
});
