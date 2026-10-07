// Copy buttons and caption character counters. No framework needed.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const source = document.getElementById(btn.dataset.copy);
  const text = source ? (source.value ?? source.textContent) : btn.dataset.text;
  try {
    await navigator.clipboard.writeText(text);
    const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = old), 1500);
  } catch {
    if (source && source.select) source.select();
  }
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
