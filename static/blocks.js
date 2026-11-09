const MAX_TAKES = 10;
const DRAG_START_PX = 5;

function snap(block) {
  block.classList.remove("snap");
  void block.offsetWidth; 
  block.classList.add("snap");
}

document.querySelectorAll("form.builder").forEach((form) => {
  const drop = form.querySelector("[data-drop]");
  const palette = form.querySelector(".palette");
  const template = form.querySelector("[data-take-template]");
  const error = form.querySelector(".build-error");
  const copyBlock = form.querySelector('[data-block="copy"]');
  const takeTool = palette.querySelector("[data-add-take]");
  const takes = () => drop.querySelectorAll("[data-take]");

  function refresh() {
    const n = takes().length;
    takeTool.classList.toggle("used", n >= MAX_TAKES);
    takeTool.title = n >= MAX_TAKES ? `Up to ${MAX_TAKES} playlists` : "";
    palette.querySelectorAll("[data-add]").forEach((tool) => {
      const block = form.querySelector(`[data-block="${tool.dataset.add}"]`);
      tool.classList.toggle("used", !block.hidden);
      tool.title = block.hidden ? "" : "Already in your automation";
    });
    if (error) error.hidden = true;
  }

  const destMode = copyBlock.querySelector("[data-dest-mode]");
  function syncDest() {
    const existing = destMode.value === "existing";
    const on = !copyBlock.hidden;
    copyBlock.querySelector(".dest-new").hidden = existing;
    copyBlock.querySelector(".dest-existing").hidden = !existing;
    copyBlock.querySelectorAll(".dest-new input, .dest-new select").forEach((el) => { el.disabled = !on || existing; });
    copyBlock.querySelectorAll(".dest-existing input").forEach((el) => { el.disabled = !on || !existing; el.required = on && existing; });
  }
  destMode.addEventListener("change", () => {
    syncDest();
    if (destMode.value === "existing") copyBlock.querySelector(".dest-existing input").focus();
  });

  function set(block, on) {
    block.hidden = !on;
    block.querySelectorAll("input, select").forEach((el) => { el.disabled = !on; });
    if (block === copyBlock) syncDest();
    refresh();
  }

  function wireTake(take) {
    take.querySelector("[data-remove-take]").addEventListener("click", () => { take.remove(); refresh(); });
    take.querySelectorAll("[data-picker]").forEach(picker);
  }

  function remove(block) {
    if (block.matches("[data-take]")) { block.remove(); refresh(); } else set(block, false);
  }

  function blockFor(tool) {
    if (tool.classList.contains("used")) return null;
    if (tool.matches("[data-add-take]")) {
      const take = template.content.firstElementChild.cloneNode(true);
      wireTake(take);
      return take;
    }
    return form.querySelector(`[data-block="${tool.dataset.add}"]`);
  }

  function place(block, before) {
    drop.insertBefore(block, before || drop.querySelector(".drop-hint"));
    if (block.dataset.block) set(block, true);
    refresh();
    snap(block);
    const first = block.querySelector("input:not([type=hidden]), select");
    if (first) first.focus({ preventScroll: true });
  }

  form.querySelectorAll("[data-block]").forEach((block) => {
    set(block, !block.hidden);
    block.querySelector(".blk-x").addEventListener("click", () => set(block, false));
  });
  takes().forEach(wireTake);
  form.querySelectorAll("[data-picker]").forEach((input) => { if (!input.closest("[data-take]")) picker(input); });
  refresh();

  let drag = null; 

  function visibleBlocks() {
    return [...drop.children].filter((el) => el.classList.contains("blk") && !el.hidden && el !== drag?.block);
  }

  function overDrop(x, y) {
    const r = drop.getBoundingClientRect();
    return x >= r.left - 40 && x <= r.right + 40 && y >= r.top - 30 && y <= r.bottom + 30;
  }

  function overPalette(x, y) {
    const r = palette.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }

  function begin(e, source) {
    if (e.button !== 0) return;
    const r = source.getBoundingClientRect();
    drag = { source, startX: e.clientX, startY: e.clientY, dx: e.clientX - r.left, dy: e.clientY - r.top, started: false };
    if (source.classList.contains("blk")) drag.block = source;
    else drag.tool = source;
    try { source.setPointerCapture(e.pointerId); } catch {}
  }

  function start() {
    drag.started = true;
    drag.ghost = drag.source.cloneNode(true);
    drag.ghost.classList.add("drag-ghost");
    drag.ghost.querySelectorAll(".picker").forEach((p) => p.remove());
    drag.ghost.style.width = drag.source.getBoundingClientRect().width + "px";
    document.body.append(drag.ghost);
    drag.marker = document.createElement("div");
    drag.marker.className = "drop-marker";
    if (drag.block) drag.block.classList.add("lifted");
    form.classList.add("dragging");
  }

  function move(e) {
    if (!drag) return;
    if (!drag.started) {
      if (Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) < DRAG_START_PX) return;
      start();
    }

    drag.ghost.style.left = e.clientX - drag.dx + "px";
    drag.ghost.style.top = e.clientY - drag.dy + "px";

    if (overDrop(e.clientX, e.clientY)) {
      const before = visibleBlocks().find((b) => {
        const r = b.getBoundingClientRect();
        return e.clientY < r.top + r.height / 2;
      });
      drop.insertBefore(drag.marker, before || drop.querySelector(".drop-hint"));
      drop.classList.add("over");
    } else {
      drag.marker.remove();
      drop.classList.remove("over");
    }
    palette.classList.toggle("bin", Boolean(drag.block) && overPalette(e.clientX, e.clientY));
  }

  function end(e) {
    if (!drag) return;
    const d = drag; 
    drag = null;
    if (!d.started) return;
    const inDrop = d.marker.isConnected;
    if (d.block) {
      d.block.classList.remove("lifted");
      if (inDrop) { drop.insertBefore(d.block, d.marker); snap(d.block); }
      else if (e.type === "pointerup" && overPalette(e.clientX, e.clientY)) remove(d.block);
    } else if (inDrop) {
      const block = blockFor(d.tool);
      if (block) place(block, d.marker);
    }
    d.ghost.remove();
    d.marker.remove();
    drop.classList.remove("over");
    palette.classList.remove("bin");
    form.classList.remove("dragging");
  }

  palette.querySelectorAll(".blk-add").forEach((tool) => {
    tool.addEventListener("pointerdown", (e) => { if (!tool.classList.contains("used")) begin(e, tool); });
    tool.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      const block = blockFor(tool);
      if (block) place(block, null);
    });
    tool.addEventListener("click", (e) => e.preventDefault());
  });
  drop.addEventListener("pointerdown", (e) => {
    const block = e.target.closest(".blk");
    if (!block || e.target.closest("input, select, textarea, button, a, .picker")) return;
    begin(e, block);
  });
  form.addEventListener("pointermove", move);
  form.addEventListener("pointerup", end);
  form.addEventListener("pointercancel", end);

  const unit = form.querySelector("[name=every_unit]");
  const every = form.querySelector("[name=every_n]");
  const maxes = { hours: 720, days: 365 };
  if (unit && every) {
    const sync = () => {
      const never = unit.value === "never" || unit.value === "always";  
      form.querySelector(".every-part").hidden = never;
      form.querySelector(".never-part").hidden = !never;
      every.disabled = never;
      every.max = maxes[unit.value] || 720;
    };
    unit.addEventListener("change", sync);
    sync();
  }

  function problem() {
    const copying = !copyBlock.hidden;
    const deduping = !form.querySelector('[data-block="dedupe"]').hidden;
    if (!copying && !deduping) return "Drag in a Copy or a Clean block first.";
    if (copying && ![...takes()].some((t) => t.querySelector("input").value.trim()))
      return "A Copy block needs at least one Take block with a playlist in it.";
    if (!copying && !form.querySelector('[data-block="compare"]').hidden)
      return "The Compare block picks which songs Copy copies. Drag in a Copy block too.";
    return null;
  }
  function check() {
    const p = problem();
    if (p) { error.textContent = p; error.hidden = false; error.scrollIntoView({ block: "nearest" }); return false; }
    return form.reportValidity();
  }

  const ask = form.querySelector("[data-ask-name]");
  const dialog = form.querySelector("dialog.name-dialog");
  
  if (ask && dialog) {
    ask.addEventListener("click", () => {
      if (!check()) return;
      dialog.showModal();
      dialog.querySelector("[name=name]").focus();
    });
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  } else {
    form.addEventListener("submit", (e) => { if (!check()) e.preventDefault(); });
  }
});

function myPlaylists() {
  playlistsRequest = playlistsRequest || fetch("/my-playlists.json", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : { playlists: [], problems: ["Couldn't load your playlists."] }))
    .catch(() => ({ playlists: [], problems: ["Couldn't load your playlists."] }));
  return playlistsRequest;
}

const EVERYTHING = [
  { title: "All my playlists", sub: "Spotify + YouTube, every playlist you made", url: "all my playlists" },
  { title: "All my Spotify playlists", sub: "every Spotify playlist you made", url: "all my Spotify playlists", service: "Spotify" },
  { title: "All my YouTube playlists", sub: "every YouTube playlist you made", url: "all my YouTube playlists", service: "YouTube" },
];

function picker(input) {
  const saved = input.nextElementSibling?.matches("[data-picker-value]") ? input.nextElementSibling : input;
  const box = document.createElement("div");
  box.className = "picker";
  box.hidden = true;
  box.setAttribute("role", "listbox");
  saved.after(box);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  let options = [];
  let active = -1;
  let chosenTitle = input.value; 

  function close() {
    box.hidden = true;
    input.setAttribute("aria-expanded", "false");
  }

  function choose(p) {
    saved.value = p.url;
    input.value = chosenTitle = p.title;
    close();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function highlight(i) {
    options.forEach((o, n) => o.classList.toggle("active", n === i));
    active = i;
    if (options[i]) options[i].scrollIntoView({ block: "nearest" });
  }

  function row(p, cover, sub, extra) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "picker-row" + (extra ? " " + extra : "");
    el.setAttribute("role", "option");
    const words = document.createElement("span");
    words.className = "picker-words";
    const title = document.createElement("strong");
    title.textContent = p.title;
    const small = document.createElement("small");
    small.textContent = sub;
    words.append(title, small);
    el.append(cover, words);
    const isChosen = saved.value && p.url === saved.value;
    if (isChosen) {
      el.classList.add("chosen");
      el.setAttribute("aria-selected", "true");
      const tick = document.createElement("span");
      tick.className = "picker-tick";
      tick.textContent = "✓ chosen";
      el.append(tick);
    }
    if (p.service) {
      const logo = document.createElement("img");
      logo.className = "picker-logo";
      logo.src = `/static/media/${p.service.toLowerCase()}.svg`;
      logo.alt = p.service;
      el.append(logo);
    }
    el.addEventListener("mousedown", (e) => e.preventDefault());  
    el.addEventListener("click", () => choose(p));
    el._playlist = p;
    box.append(el);
    options.push(el);
    return isChosen;
  }

  async function open(typing) {
    const data = await myPlaylists();
    const raw = input.value.trim();
    const text = typing && raw !== chosenTitle ? raw.toLowerCase() : "";
    const isLink = !text.startsWith("all my") && /^https?:|list=|spotify/.test(text);
    const fits = (p) => !text || isLink || p.title.toLowerCase().includes(text);
    const shown = data.playlists.filter(fits);
    const everything = input.closest("[data-take]") ? EVERYTHING.filter((p) => fits(p) || p.url.includes(text)) : [];
    box.replaceChildren();
    options = [];
    for (const problem of data.problems) {
      const note = document.createElement("p");
      note.className = "picker-note";
      note.textContent = problem;
      box.append(note);
    }
    if (!shown.length && !everything.length && !data.problems.length) {
      const note = document.createElement("p");
      note.className = "picker-note";
      note.textContent = text ? "No playlist with that name. You can still paste a link." : "No playlists found.";
      box.append(note);
    }
    let chosenAt = -1;
    for (const p of everything) {
      const icon = document.createElement("span");
      icon.className = "picker-cover";
      icon.textContent = "✦";
      if (row(p, icon, p.sub, "picker-all")) chosenAt = options.length - 1;
    }
    for (const p of shown) {
      const cover = document.createElement(p.image ? "img" : "span");
      cover.className = "picker-cover";
      if (p.image) { cover.src = p.image; cover.alt = ""; cover.loading = "lazy"; cover.referrerPolicy = "no-referrer"; }
      if (row(p, cover, p.service + (p.owner ? ` · by ${p.owner}` : ""))) chosenAt = options.length - 1;
    }
    box.hidden = false;
    input.setAttribute("aria-expanded", "true");
    highlight(-1);
    if (chosenAt >= 0) options[chosenAt].scrollIntoView({ block: "nearest" });
  }

  input.addEventListener("focus", () => open(false));
  input.addEventListener("click", () => { if (box.hidden) open(false); });
  input.addEventListener("input", () => {
    saved.value = input.value.trim();  
    open(true);
  });
  input.addEventListener("blur", () => setTimeout(() => {
    close();
    const typed = input.value.trim().toLowerCase();
    if (!typed || typed === chosenTitle.toLowerCase() || !playlistsRequest) return;
    playlistsRequest.then((data) => {
      const pool = (input.closest("[data-take]") ? EVERYTHING : []).concat(data.playlists);
      const hit = pool.find((p) => p.title.toLowerCase() === typed);
      if (hit) choose(hit);
    });
  }, 120));
  input.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); highlight(Math.min(active + 1, options.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
    else if (e.key === "Enter" && options[active]) { e.preventDefault(); choose(options[active]._playlist); }
    else if (e.key === "Escape") { close(); }
  });
}
