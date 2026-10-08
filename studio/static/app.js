// Copy buttons and caption character counters. No framework needed.
// navigator.clipboard only exists on https:// and localhost, and the Studio is often opened at
// http://<lan-ip>, so fall back to the older execCommand copy, then to selecting the text.
function copyFallback(text) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

function selectText(el) {
  if (el.select) { el.focus(); el.select(); return; }
  const range = document.createRange(); range.selectNodeContents(el);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
}

function flashLabel(btn, label) {
  const old = btn.dataset.label || btn.textContent;
  btn.dataset.label = old; btn.textContent = label;
  setTimeout(() => (btn.textContent = old), 1800);
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const source = document.getElementById(btn.dataset.copy);
  const text = source ? (source.value ?? source.textContent) : btn.dataset.text;
  let ok = false;
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); ok = true; } catch { ok = false; }
  }
  if (!ok) ok = copyFallback(text);
  if (ok) { flashLabel(btn, "Copied"); return; }
  if (source) selectText(source);
  flashLabel(btn, "Press Ctrl+C");
});

function updateCount(el) {
  const out = document.getElementById(el.dataset.counter);
  if (!out) return;
  const key = el.dataset.channel;
  const body = document.querySelector(`[name="${key}.body"]`)?.value.trim() ?? "";
  const tags = document.querySelector(`[name="${key}.hashtags"]`)?.value.trim() ?? "";
  const n = [body, tags].filter(Boolean).join("\n\n").length;
  const max = Number(out.dataset.max);
  out.textContent = `${n} / ${max}`;
  out.classList.toggle("over", n > max);
}
document.querySelectorAll("[data-counter]").forEach((el) => {
  el.addEventListener("input", () => updateCount(el));
  updateCount(el);
});

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    if (!window.confirm(form.dataset.confirm)) e.preventDefault();
  });
});
