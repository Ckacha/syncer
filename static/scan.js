const PAUSE_MS = 2000; 
const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));

const controls = document.querySelector(".scan-controls");
if (controls) {
  const form = controls.closest("form");
  const url = controls.dataset.scan;
  const batch = Number(controls.dataset.batch);
  const quota = Number(controls.dataset.quota);
  const countInput = controls.querySelector("[name=count]");
  const stopBtn = controls.querySelector("[data-scan-stop]");
  const status = document.getElementById("scan-status");
  const progress = document.getElementById("scan-progress");
  const scannedText = document.getElementById("scanned");
  let stopping = false;

  async function scan(target) {
    if (quota && !confirm(`This can use up to ${(target * quota).toLocaleString("en-US")} YouTube quota `
                          + "(10,000 a day). Scanning stops by itself if the quota runs out. Go?")) return;
    controls.querySelectorAll("button, input").forEach((el) => { el.disabled = true; });
    stopBtn.hidden = false;
    stopBtn.disabled = false;
    status.hidden = false;
    let done = 0;
    let first = true;

    while (done < target && !stopping) {
      status.textContent = `Scanning… ${done} / ${target}`;
      const body = first ? new FormData(form) : new FormData();
      body.set("count", Math.min(batch, target - done));
      first = false;
      let result;
      try {
        const r = await fetch(url, { method: "POST", body, headers: { "X-Scan": "1" }, credentials: "same-origin" });
        result = await r.json();
      } catch {
        status.textContent = "Lost the connection. Reloading with what was scanned…";
        break;
      }
      done += result.done;
      progress.value = Number(progress.value) + result.done;
      scannedText.textContent = progress.value;
      if (result.stopped || !result.left || !result.done) break;
      if (done < target) await wait(PAUSE_MS);
    }
    status.textContent = `Scanned ${done}. Loading the results…`;
    location.reload();
  }

  controls.querySelector("[data-scan-next]").addEventListener("click", (e) => {
    e.preventDefault();
    if (!countInput.reportValidity()) return;
    scan(Number(countInput.value));
  });
  controls.querySelector("[data-scan-all]").addEventListener("click", () => scan(Number(controls.dataset.left)));
  stopBtn.addEventListener("click", () => {
    stopping = true;
    stopBtn.disabled = true;
    status.textContent = "Stopping after this batch…";
  });
}

const table = document.getElementById("songs");
const tabs = document.querySelectorAll(".tab[data-show]");
const memoryKey = "show:" + location.pathname;

function show(list) {
  if (!document.querySelector(`.tab[data-show="${list}"]`)) list = "all";
  table.dataset.show = list;
  tabs.forEach((t) => t.setAttribute("aria-selected", String(t.dataset.show === list)));
  const rows = table.querySelectorAll("tr[id^=row-]");
  table.querySelectorAll("tr.just-rescanned").forEach((r) => r.classList.remove("just-rescanned"));
  const any = list === "all" || [...rows].some((r) => r.classList.contains(list));
  table.querySelector(".empty-list").hidden = any;
  try { sessionStorage.setItem(memoryKey, list); } catch {}
}

table.addEventListener("change", async (e) => {
  const box = e.target;
  if (!box.matches('input[name="confirm"]')) return;
  const cell = box.closest("td");
  cell.querySelector(".tick-error")?.remove();
  const body = new FormData();
  body.set("key", box.value);
  body.set("on", box.checked ? "1" : "0");
  let ok = false;
  try {
    const r = await fetch(table.dataset.tick, { method: "POST", body, credentials: "same-origin" });
    ok = r.ok;
  } catch {}
  if (!ok) {
    const note = document.createElement("small");
    note.className = "tick-error";
    note.textContent = "not saved";
    note.title = "Couldn't save this tick. Check your connection and tick it again.";
    cell.append(note);
  }
});

function recount() {
  for (const el of document.querySelectorAll("[data-count]"))
    el.textContent = table.querySelectorAll(`tr[id^=row-].${el.dataset.count}`).length;
  document.dispatchEvent(new Event("change"));
}

async function rescan(btn) {
  const row = btn.closest("tr");
  const form = btn.closest("form");
  btn.disabled = true;
  btn.classList.add("spinning");
  row.querySelector(".rescan-error")?.remove();
  let result;
  try {
    const r = await fetch(btn.getAttribute("formaction"), {
      method: "POST", body: new FormData(form), headers: { "X-Scan": "1" }, credentials: "same-origin",
    });
    result = await r.json();
  } catch {
    result = { error: "Couldn't reach Syncer. Check your connection and try again." };
  }
  if (result.html) {
    const fresh = document.createElement("tbody");
    fresh.innerHTML = result.html.trim();
    const newRow = fresh.querySelector("tr");
    newRow.classList.add("just-rescanned");
    row.replaceWith(newRow);
    recount();
  }
  if (result.error) {
    const target = (document.getElementById(row.id) || row).querySelector("td.score") || row.cells[1];
    const note = document.createElement("small");
    note.className = "rescan-error";
    note.textContent = result.error;
    target.append(note);
  }
  btn.disabled = false;
  btn.classList.remove("spinning");
}

if (table) {
  table.addEventListener("click", (e) => {
    const btn = e.target.closest("button.rescan");
    if (!btn) return;
    e.preventDefault();
    rescan(btn);
  });
  tabs.forEach((t) => t.addEventListener("click", () => show(t.dataset.show)));
  let saved = "all";
  try { saved = sessionStorage.getItem(memoryKey) || "all"; } catch {}
  show(location.hash.startsWith("#row-") ? "all" : saved);
}
