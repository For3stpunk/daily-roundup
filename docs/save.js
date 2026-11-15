(() => {
  const KEY = "daily-roundup-saved";
  let mem = [];
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return mem; } };
  const store = list => { mem = list; try { localStorage.setItem(KEY, JSON.stringify(list)); } catch {} };
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const has = (list, link) => list.some(x => x.link === link);

  function counts() {
    const n = load().length;
    document.querySelectorAll(".saved-count").forEach(b => b.textContent = n ? n : "");
  }
  function syncButtons() {
    const list = load();
    document.querySelectorAll("li[data-link] button.save").forEach(b => {
      const on = has(list, b.closest("li").dataset.link);
      b.setAttribute("aria-pressed", on); b.textContent = on ? "Saved ✓" : "Save";
    });
    counts();
  }
  document.addEventListener("click", e => {
    const b = e.target.closest("button.save"); if (!b) return;
    const li = b.closest("li"), d = li.dataset; let list = load();
    if (has(list, d.link)) list = list.filter(x => x.link !== d.link);
    else list.unshift({title: d.title, link: d.link, source: d.source, topic: d.topic, date: d.date, summary: d.summary, saved: new Date().toISOString()});
    store(list); syncButtons(); if (box) render();
  });
  window.addEventListener("storage", e => { if (e.key === KEY) { syncButtons(); if (box) render(); } });

  // Saved page
  const box = document.getElementById("saved");
  const msg = t => { const m = document.getElementById("msg"); if (m) { m.textContent = t; setTimeout(() => m.textContent = "", 4000); } };
  function render() {
    const q = (document.getElementById("q")?.value || "").toLowerCase();
    const all = load(), list = all.filter(x => !q || (x.title + " " + x.source + " " + x.topic).toLowerCase().includes(q));
    document.querySelector(".saved-total").textContent = all.length ? `${all.length}` : "";
    if (!all.length) { box.innerHTML = '<p>No saved stories yet. Hit <b>Save</b> next to any headline.</p>'; return; }
    const byTopic = {};
    list.forEach(x => (byTopic[x.topic || "Other"] ||= []).push(x));
    box.innerHTML = Object.keys(byTopic).sort().map(t => `<section><h2>${esc(t)} <small>${byTopic[t].length}</small></h2><ol class="items">` +
      byTopic[t].map((x, i) => `<li data-link="${esc(x.link)}"><span class="n">${String(i + 1).padStart(2, "0")}</span><div>
        <a class="t" href="${esc(x.link)}">${esc(x.title)}</a>
        <div class="s">${esc(x.source)} · from ${esc(x.date)} issue · <button type="button" class="remove">Remove</button></div>
        ${x.summary ? `<p class="sum">${esc(x.summary)}</p>` : ""}</div></li>`).join("") + "</ol></section>").join("") || "<p>No matches.</p>";
  }
  if (box) {
    box.addEventListener("click", e => {
      const b = e.target.closest("button.remove"); if (!b) return;
      e.stopPropagation(); store(load().filter(x => x.link !== b.closest("li").dataset.link)); render(); counts();
    }, true);
    document.getElementById("q").addEventListener("input", render);
    document.getElementById("md").addEventListener("click", async () => {
      const md = load().map(x => `- [${x.title}](${x.link}) — ${x.source} (${x.topic}, ${x.date})`).join("\n");
      try { await navigator.clipboard.writeText(md); msg("Copied " + load().length + " stories as Markdown."); }
      catch { msg("Couldn't reach the clipboard in this browser."); }
    });
    document.getElementById("exp").addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(load(), null, 1)], {type: "application/json"}));
      a.download = `daily-roundup-saved-${new Date().toISOString().slice(0, 10)}.json`; a.click();
    });
    document.getElementById("imp").addEventListener("change", async e => {
      try {
        const incoming = JSON.parse(await e.target.files[0].text());
        const list = load(); let added = 0;
        incoming.forEach(x => { if (x && x.link && !has(list, x.link)) { list.push(x); added++; } });
        store(list); render(); counts(); msg(`Imported ${added} new stories.`);
      } catch { msg("That file isn't a Daily Roundup export."); }
      e.target.value = "";
    });
    document.getElementById("clr").addEventListener("click", () => {
      const b = document.getElementById("clr");
      if (b.dataset.armed) { store([]); render(); counts(); b.textContent = "Remove all"; delete b.dataset.armed; }
      else { b.dataset.armed = 1; b.textContent = "Click again to remove all"; setTimeout(() => { b.textContent = "Remove all"; delete b.dataset.armed; }, 4000); }
    });
    render();
  }
  syncButtons();
})();
