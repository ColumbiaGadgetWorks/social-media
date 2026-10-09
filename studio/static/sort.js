// Sort page: group a batch of uploads into posts (carousels), describe each, then save.
(() => {
  const cfg = JSON.parse(document.getElementById("sort-config").textContent);
  const $ = (id) => document.getElementById(id);
  const KEY = `cgw-sort-${cfg.batch}`;
  const MAX_CAROUSEL = 10;
  const items = new Map(cfg.items.map((i) => [i.id, i]));
  let groups = [], discarded = [], selected = new Set(), view = "grid", pos = 0, slide = 0, nextGid = 1;

  // --- state -------------------------------------------------------------
  const newGroup = (media, extra = {}) => ({ gid: nextGid++, media, note: "", notes: {}, pillar: "", event_id: "", library: false, ...extra });

  function load() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch { saved = null; }
    const placed = new Set();
    if (saved && Array.isArray(saved.groups)) {
      for (const g of saved.groups) {
        const media = (g.media || []).filter((id) => items.has(id) && !placed.has(id));
        media.forEach((id) => placed.add(id));
        if (media.length) groups.push(newGroup(media, { note: g.note || "", notes: g.notes || {}, pillar: g.pillar || "", event_id: g.event_id || "", library: !!g.library }));
      }
      discarded = (saved.discarded || []).filter((id) => items.has(id) && !placed.has(id));
      discarded.forEach((id) => placed.add(id));
    }
    for (const it of cfg.items) {
      if (!placed.has(it.id)) groups.push(newGroup([it.id], { note: it.note || "", event_id: it.event_id ? String(it.event_id) : "" }));
    }
  }

  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify({ groups, discarded })); } catch { /* private window: fine */ }
  }

  const isVideo = (id) => items.get(id)?.kind === "video";
  const videos = (g) => g.media.filter(isVideo).length;

  // --- rendering ---------------------------------------------------------
  function thumbOf(it) {
    if (it.thumb) return it.thumb;
    return it.kind === "image" ? it.original : "";
  }

  function mediaTile(id, cls = "") {
    const it = items.get(id);
    const src = thumbOf(it);
    const wrap = document.createElement("div");
    wrap.className = `tile ${cls}`;
    if (src) {
      const img = document.createElement("img");
      img.src = src; img.alt = it.name; img.loading = "lazy";
      wrap.appendChild(img);
    } else {
      wrap.innerHTML = '<span class="small muted">processing…</span>';
    }
    if (it.kind === "video") wrap.insertAdjacentHTML("beforeend", `<span class="tag badge">▶ ${it.duration ? it.duration + "s" : "video"}</span>`);
    if (it.duplicate) wrap.insertAdjacentHTML("beforeend", '<span class="tag warn badge2" title="The same file is already in the library">duplicate</span>');
    return wrap;
  }

  function renderGrid() {
    const grid = $("grid");
    grid.innerHTML = "";
    groups.forEach((g, i) => {
      const card = document.createElement("div");
      card.className = "gcard" + (g.media.some((id) => selected.has(id)) ? " picked" : "") + (g.library ? " library" : "");
      const tiles = document.createElement("div");
      tiles.className = g.media.length > 1 ? "tiles multi" : "tiles";
      g.media.slice(0, 4).forEach((id, n) => {
        const t = mediaTile(id, selected.has(id) ? "sel" : "");
        t.tabIndex = 0;
        t.setAttribute("role", "checkbox");
        t.setAttribute("aria-checked", selected.has(id));
        t.title = "Click to select";
        t.onclick = (e) => { e.stopPropagation(); toggle(id); };
        t.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(id); } };
        if (n === 3 && g.media.length > 4) t.insertAdjacentHTML("beforeend", `<span class="more">+${g.media.length - 4}</span>`);
        tiles.appendChild(t);
      });
      card.appendChild(tiles);
      const meta = document.createElement("div");
      meta.className = "gmeta";
      const kind = g.library ? '<span class="tag">Library only</span>'
        : g.media.length > 1 ? `<span class="tag accent">Carousel · ${g.media.length}</span>` : '<span class="tag ok">Post</span>';
      const problem = !g.library && (videos(g) > 1 ? "Only one video per post" : g.media.length > MAX_CAROUSEL ? `Max ${MAX_CAROUSEL} per carousel` : "");
      meta.innerHTML = `<div class="row spread">${kind}${problem ? `<span class="tag stop">${problem}</span>` : ""}</div>`;
      const note = document.createElement("p");
      note.className = "small" + (g.note ? "" : " muted");
      note.textContent = g.note || "No description yet";
      meta.appendChild(note);
      const edit = document.createElement("button");
      edit.type = "button"; edit.className = "link"; edit.textContent = g.note ? "Edit" : "Describe";
      edit.onclick = () => openStep(i);
      meta.appendChild(edit);
      card.appendChild(meta);
      grid.appendChild(card);
    });
    const d = $("discarded");
    d.innerHTML = "";
    if (discarded.length) {
      d.append(`${discarded.length} file(s) will be discarded on save. `);
      const undo = document.createElement("button");
      undo.type = "button"; undo.className = "link"; undo.textContent = "Undo";
      undo.onclick = () => { discarded.forEach((id) => groups.push(newGroup([id]))); discarded = []; changed(); };
      d.appendChild(undo);
    }
  }

  function renderBar() {
    const n = selected.size;
    $("sel-count").textContent = n ? `${n} selected` : "Click files to select them";
    const owners = new Set([...selected].map((id) => groupOf(id)));
    $("b-group").disabled = n < 2;
    $("b-split").disabled = ![...owners].some((g) => g.media.length > 1);
    $("b-library").disabled = !n;
    $("b-post").disabled = !n;
    $("b-discard").disabled = !n;
    const posts = groups.filter((g) => !g.library).length;
    const lib = groups.filter((g) => g.library).reduce((a, g) => a + g.media.length, 0);
    const missing = groups.filter((g) => !g.library && !g.note && !Object.values(g.notes).some(Boolean)).length;
    $("summary").textContent = `${posts} post(s)` + (lib ? `, ${lib} to library` : "") + (discarded.length ? `, ${discarded.length} discard` : "")
      + (missing ? ` · ${missing} without a description` : "");
  }

  function renderStep() {
    if (!groups.length) { setView("grid"); return; }
    pos = Math.max(0, Math.min(pos, groups.length - 1));
    const g = groups[pos];
    slide = Math.max(0, Math.min(slide, g.media.length - 1));
    const id = g.media[slide], it = items.get(id);
    const stage = $("stage");
    stage.innerHTML = "";
    if (it.kind === "video") {
      const v = document.createElement("video");
      v.controls = true; v.preload = "metadata"; v.src = it.original;
      if (it.preview) v.poster = it.preview;
      stage.appendChild(v);
    } else {
      const img = document.createElement("img");
      img.src = it.preview || it.original; img.alt = it.name;
      stage.appendChild(img);
    }
    stage.insertAdjacentHTML("beforeend", `<span class="small muted cap"></span>`);
    stage.querySelector(".cap").textContent = `${it.name}${g.media.length > 1 ? ` · ${slide + 1} of ${g.media.length}` : ""}`;

    const strip = $("strip");
    strip.innerHTML = "";
    g.media.forEach((mid, n) => {
      const t = mediaTile(mid, n === slide ? "current" : "");
      t.tabIndex = 0;
      t.title = items.get(mid).name;
      t.onclick = () => { slide = n; renderStep(); };
      strip.appendChild(t);
    });
    strip.hidden = g.media.length < 2;
    const tools = $("slide-tools");
    tools.innerHTML = "";
    if (g.media.length > 1) {
      tools.append(btn("◀ Move earlier", () => moveSlide(-1), slide === 0));
      tools.append(btn("Move later ▶", () => moveSlide(1), slide === g.media.length - 1));
      tools.append(btn("Take out of carousel", takeOut));
    }

    $("step-pos").textContent = `Post ${pos + 1} of ${groups.length}`;
    $("step-kind").textContent = g.library ? "Library only" : g.media.length > 1 ? `Carousel · ${g.media.length}` : (it.kind === "video" ? "Video post" : "Photo post");
    $("f-note").value = g.note;
    $("f-slide").value = g.notes[id] || "";
    const single = g.media.length < 2;  // a per-photo note only makes sense in a carousel
    $("f-slide").hidden = single;
    document.querySelector('label[for="f-slide"]').hidden = single;
    $("f-event").value = g.event_id || "";
    $("f-pillar").value = g.pillar || "";
    $("f-library").checked = g.library;
    $("s-prev").disabled = pos === 0;
    $("s-next").textContent = pos === groups.length - 1 ? "Done: back to all" : "Next ›";
  }

  function btn(label, fn, disabled = false) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = label; b.disabled = disabled; b.onclick = fn;
    return b;
  }

  function render() {
    if (view === "grid") renderGrid(); else renderStep();
    renderBar();
  }

  function changed() { persist(); render(); }

  // --- actions -----------------------------------------------------------
  const groupOf = (id) => groups.find((g) => g.media.includes(id));

  function toggle(id) {
    if (selected.has(id)) selected.delete(id); else selected.add(id);
    render();
  }

  function selectedInOrder() {
    return groups.flatMap((g) => g.media).filter((id) => selected.has(id));
  }

  function groupSelected() {
    const ids = selectedInOrder();
    if (ids.length < 2) return;
    const first = groupOf(ids[0]);
    const at = groups.indexOf(first);
    const merged = newGroup(ids, {
      note: groups.filter((g) => g.media.some((id) => selected.has(id))).map((g) => g.note).filter(Boolean).join(" "),
      event_id: first.event_id, pillar: first.pillar,
    });
    for (const g of groups) {
      for (const id of g.media) if (selected.has(id) && g.notes[id]) merged.notes[id] = g.notes[id];
      g.media = g.media.filter((id) => !selected.has(id));
    }
    groups.splice(at, 0, merged);
    groups = groups.filter((g) => g.media.length);
    selected.clear();
    changed();
    if (videos(merged) > 1) showError("A post can have only one video; split the videos out before saving.");
  }

  function splitSelected() {
    const out = [];
    for (const g of groups) {
      if (g.media.length > 1 && g.media.some((id) => selected.has(id))) {
        out.push(g, ...g.media.slice(1).map((id) => newGroup([id], { event_id: g.event_id, pillar: g.pillar, note: g.notes[id] || "" })));
        g.media = g.media.slice(0, 1);
      } else out.push(g);
    }
    groups = out;
    selected.clear();
    changed();
  }

  function setLibrary(flag) {
    for (const g of groups) if (g.media.some((id) => selected.has(id))) g.library = flag;
    selected.clear();
    changed();
  }

  function discardSelected() {
    if (!confirm(`Discard ${selected.size} file(s)? They're deleted when you save.`)) return;
    for (const g of groups) g.media = g.media.filter((id) => { if (selected.has(id)) { discarded.push(id); return false; } return true; });
    groups = groups.filter((g) => g.media.length);
    selected.clear();
    changed();
  }

  function moveSlide(dir) {
    const g = groups[pos], to = slide + dir;
    if (to < 0 || to >= g.media.length) return;
    [g.media[slide], g.media[to]] = [g.media[to], g.media[slide]];
    slide = to;
    changed();
  }

  function takeOut() {
    const g = groups[pos];
    const [id] = g.media.splice(slide, 1);
    groups.splice(pos + 1, 0, newGroup([id], { event_id: g.event_id, pillar: g.pillar, note: g.notes[id] || "" }));
    delete g.notes[id];
    slide = Math.max(0, slide - 1);
    changed();
  }

  function openStep(i) { pos = i; slide = 0; setView("step"); }

  function setView(v) {
    view = v;
    $("grid").hidden = v !== "grid";
    $("discarded").hidden = v !== "grid";
    $("step").hidden = v !== "step";
    $("view-grid").classList.toggle("on", v === "grid");
    $("view-step").classList.toggle("on", v === "step");
    for (const b of ["b-group", "b-split", "b-library", "b-post", "b-discard", "b-clear", "sel-count"]) $(b).hidden = v !== "grid";
    render();
  }

  function showError(msg) {
    const e = $("sort-error");
    e.textContent = msg; e.hidden = !msg;
    if (msg) e.scrollIntoView({ block: "nearest" });
  }

  async function save() {
    const bad = groups.findIndex((g) => !g.library && (videos(g) > 1 || g.media.length > MAX_CAROUSEL));
    if (bad >= 0) { showError(`Post ${bad + 1}: ${videos(groups[bad]) > 1 ? "a post can have only one video" : `a carousel can have at most ${MAX_CAROUSEL} files`}.`); return; }
    const missing = groups.filter((g) => !g.library && !g.note && !Object.values(g.notes).some(Boolean)).length;
    if (missing && !confirm(`${missing} post(s) have no description. Claude will only have the photos to go on. Save anyway?`)) return;
    $("b-save").disabled = true;
    showError("");
    try {
      const r = await fetch(`/upload/batch/${cfg.batch}/save`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf: cfg.csrf, discard: discarded,
          groups: groups.map((g) => ({ media: g.media, note: g.note, notes: g.notes, pillar: g.pillar, event_id: g.event_id, library: g.library })) }),
      });
      const data = await r.json();
      if (!r.ok) { showError(data.error || `Couldn't save (error ${r.status}).`); return; }
      try { localStorage.removeItem(KEY); } catch { /* fine */ }
      location.href = data.redirect;
    } catch {
      showError("Couldn't reach the Studio. Your notes are kept in this browser; try again.");
    } finally {
      $("b-save").disabled = false;
    }
  }

  // --- wiring ------------------------------------------------------------
  $("b-group").onclick = groupSelected;
  $("b-split").onclick = splitSelected;
  $("b-library").onclick = () => setLibrary(true);
  $("b-post").onclick = () => setLibrary(false);
  $("b-discard").onclick = discardSelected;
  $("b-clear").onclick = () => { selected.clear(); render(); };
  $("b-save").onclick = save;
  $("view-grid").onclick = () => setView("grid");
  $("view-step").onclick = () => openStep(0);
  $("s-prev").onclick = () => { if (pos > 0) { pos--; slide = 0; render(); } };
  $("s-next").onclick = () => { if (pos < groups.length - 1) { pos++; slide = 0; render(); } else setView("grid"); };
  $("s-discard").onclick = () => {
    const g = groups[pos];
    if (!confirm(`Discard ${g.media.length} file(s)? They're deleted when you save.`)) return;
    discarded.push(...g.media);
    groups.splice(pos, 1);
    slide = 0;
    changed();
  };
  $("f-note").oninput = (e) => { groups[pos].note = e.target.value; persist(); renderBar(); };
  $("f-slide").oninput = (e) => { const g = groups[pos]; g.notes[g.media[slide]] = e.target.value; persist(); renderBar(); };
  $("f-event").onchange = (e) => { groups[pos].event_id = e.target.value; persist(); };
  $("f-pillar").onchange = (e) => { groups[pos].pillar = e.target.value; persist(); };
  $("f-library").onchange = (e) => { groups[pos].library = e.target.checked; changed(); };
  document.addEventListener("keydown", (e) => {
    if (view !== "step" || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")) return;
    if (e.key === "ArrowRight") $("s-next").click();
    if (e.key === "ArrowLeft") $("s-prev").click();
  });
  window.addEventListener("beforeunload", persist);

  // Videos get thumbnails in the background; check back until they're ready.
  async function poll() {
    if (![...items.values()].some((i) => i.status === "pending")) return;
    try {
      const r = await fetch(`/upload/batch/${cfg.batch}/items`);
      const data = await r.json();
      for (const it of data.items) if (items.has(it.id)) items.set(it.id, it);
      render();
    } catch { /* try again */ }
    setTimeout(poll, 4000);
  }

  load();
  setView("grid");
  setTimeout(poll, 3000);
})();
