document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (e.defaultPrevented) return;
  if (!/\/automations\/\d+\/(run|understand|undo|save|restore\/\d+)$/.test(form.getAttribute("action") || "")) return;
  e.preventDefault();
  const card = form.closest(".script");
  if (!card || card.classList.contains("running")) return;
  const id = card.id.replace("auto-", "");
  const box = card.querySelector(".run-progress");
  const bar = box.querySelector("progress");
  const stepText = box.querySelector(".run-step");
  
  card.classList.add("running");
  card.querySelectorAll(".run-msg, .run-error").forEach((el) => el.remove());
  card.querySelectorAll(".script-actions button, .held button, .versions button").forEach((b) => { b.disabled = true; });
  box.hidden = false;
  bar.value = 0;
  stepText.textContent = "Starting...";

  let finished = false;
  const poll = async () => {
    while (!finished) {
      await new Promise((r) => setTimeout(r, 600));
      if (finished) break;
      try {
        const p = await (await fetch(`/automations/${id}/progress`, { credentials: "same-origin" })).json();
        if (!finished && p.done !== null && p.done !== undefined) {
          bar.value = Math.max(bar.value, p.done * 100);
          stepText.textContent = p.step;
        }
      } catch {}
    }
  };
  poll();

  let result;
  try {
    const r = await fetch(form.action, { method: "POST", headers: { "X-Scan": "1" }, credentials: "same-origin" });
    result = await r.json();
  } catch {
    result = { error: "Couldn't reach Syncer. Check your connection and try again." };
  }
  finished = true;
  if (result.html) {
    bar.value = 100;
    const holder = document.createElement("div");
    holder.innerHTML = result.html.trim();
    const fresh = holder.querySelector(".script");
    fresh.classList.add("just-ran");
    card.replaceWith(fresh);
    return;
  }
  card.classList.remove("running");
  box.hidden = true;
  card.querySelectorAll(".script-actions button, .held button, .versions button").forEach((b) => { b.disabled = false; });
  const note = document.createElement("div");
  note.className = "blk cap run-error";
  note.textContent = result.error || "Something went wrong.";
  card.querySelector(".script-actions").before(note);
});
