// Drag-and-drop batch upload: files go up one at a time (zips are unpacked on the server),
// then the sort page groups them into posts.
(() => {
  const cfg = JSON.parse(document.getElementById("upload-config").textContent);
  const drop = document.getElementById("drop");
  const pick = document.getElementById("pick");
  const queue = document.getElementById("queue");
  const doneRow = document.getElementById("done-row");
  const doneNote = document.getElementById("done-note");
  const pending = [];
  let running = 0, stored = 0, problems = 0;
  const PARALLEL = 2;

  function row(file) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="name"></span><progress max="100" value="0"></progress><span class="state small muted">waiting</span>';
    li.querySelector(".name").textContent = file.name;
    queue.appendChild(li);
    return li;
  }

  function add(files) {
    for (const file of files) pending.push({ file, li: row(file) });
    doneRow.hidden = true;
    pump();
  }

  function pump() {
    while (running < PARALLEL && pending.length) send(pending.shift());
    if (!running && !pending.length && (stored || problems)) {
      doneRow.hidden = !stored;
      doneNote.textContent = `${stored} file(s) in${problems ? `, ${problems} couldn't be added` : ""}.`;
    }
  }

  function send({ file, li }) {
    running++;
    const state = li.querySelector(".state"), bar = li.querySelector("progress");
    const body = new FormData();
    body.append("csrf", cfg.csrf);
    body.append("batch", cfg.batch);
    body.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/upload/files");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.value = (e.loaded / e.total) * 100; state.textContent = "uploading"; } };
    xhr.upload.onload = () => { state.textContent = /\.zip$/i.test(file.name) ? "unpacking" : "processing"; };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { data = { error: xhr.status === 413 ? "too big for the proxy" : `error ${xhr.status}` }; }
      if (xhr.status >= 400 || data.error) {
        li.classList.add("bad"); state.textContent = data.error || `error ${xhr.status}`; problems++;
      } else {
        bar.value = 100;
        const n = data.media.length;
        stored += n;
        problems += data.problems.length;
        state.textContent = (n === 1 && !/\.zip$/i.test(file.name)) ? "done" : `${n} file(s)`;
        if (data.problems.length) { li.classList.add("warn"); state.title = data.problems.join("\n"); state.textContent += `, ${data.problems.length} skipped (hover)`; }
      }
      running--; pump();
    };
    xhr.onerror = () => { li.classList.add("bad"); state.textContent = "network error"; problems++; running--; pump(); };
    xhr.send(body);
  }

  drop.addEventListener("click", () => pick.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick.click(); } });
  pick.addEventListener("change", () => { add(pick.files); pick.value = ""; });
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); add(e.dataTransfer.files); });
  // Dropping anywhere on the page shouldn't open the file in the browser.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => { if (!drop.contains(e.target)) { e.preventDefault(); add(e.dataTransfer.files); } });
})();
