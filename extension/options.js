const $ = (s) => document.querySelector(s);
chrome.storage.local.get(["studioUrl", "token"]).then(({ studioUrl = "", token = "" }) => {
  $("#url").value = studioUrl;
  $("#token").value = token;
});
$("#save").onclick = async () => {
  const studioUrl = $("#url").value.trim().replace(/\/+$/, "");
  const token = $("#token").value.trim();
  let origin;
  try { origin = new URL(studioUrl).origin; } catch { $("#msg").textContent = "That address isn't valid."; return; }
  // The Studio's address is your own, so the extension asks for access to just that origin.
  const granted = await chrome.permissions.request({ origins: [origin + "/*"] });
  if (!granted) { $("#msg").textContent = "The extension needs permission to reach the Studio."; return; }
  await chrome.storage.local.set({ studioUrl, token });
  chrome.runtime.sendMessage({ type: "overview" }, (res) => {
    $("#msg").textContent = res && res.ok
      ? `Connected. ${res.data.channels.map((c) => `${c.label}: ${c.count}`).join(", ")}`
      : `Couldn't connect: ${res ? res.error : "no answer"}`;
  });
};
