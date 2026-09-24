// The queue dock along the bottom of every page: one row per game being
// installed, with one bar over all of its parts (game, update, DLC, mods).
// Loaded synchronously right after its own markup, so the last known state
// (sessionStorage) is on screen before the page's first paint -- moving
// between pages never shows a frame without it.
//
// Desktop only: on a phone the bottom nav already takes that space, and the
// Queue tab is one tap away (see queue_dock.css).
(function () {
  "use strict";

  // Settings' animation preference (auto/on/off, motion_preferences.js), on
  // <html> for the progress bars' running blocks -- see .seg-bar in style.css.
  try {
    const motion = localStorage.getItem("card-motion-mode");
    if (motion === "on" || motion === "off") document.documentElement.dataset.motion = motion;
  } catch (e) { /* storage unavailable: follow the system */ }

  // A game's cover as a small row icon, shared with the Queue page
  // (queue.js). Rows there are rebuilt on every poll, so a cover that does
  // not exist is remembered and not asked for again.
  const missingCovers = new Set();
  window.SwitchAgentGameThumb = function (coverId, name) {
    const thumb = document.createElement("span");
    thumb.className = "game-thumb";
    thumb.setAttribute("aria-hidden", "true");
    thumb.textContent = (name || "?").trim().charAt(0).toUpperCase();
    if (coverId && !missingCovers.has(coverId)) {
      const img = document.createElement("img");
      img.alt = "";
      img.onerror = () => { missingCovers.add(coverId); img.remove(); };
      img.src = `/api/covers/${encodeURIComponent(coverId)}`;
      thumb.appendChild(img);
    }
    return thumb;
  };

  const dock = document.getElementById("queue-dock");
  if (!dock || dock.hasAttribute("data-off")) return;

  const MAX_ROWS = 3;
  const CACHE_KEY = "switchagent-queue-dock";
  const COLLAPSED_KEY = "switchagent-queue-dock-collapsed";
  const DISMISSED_KEY = "switchagent-queue-dock-dismissed";
  const desktop = window.matchMedia("(min-width: 768px)");
  const list = document.getElementById("queue-dock-list");
  const count = document.getElementById("queue-dock-count");
  const summary = document.getElementById("queue-dock-summary");
  const line = document.getElementById("queue-dock-line").firstElementChild;
  const more = document.getElementById("queue-dock-more");
  const toggle = document.getElementById("queue-dock-toggle");
  const close = document.getElementById("queue-dock-close");
  const rows = new Map();
  let lastGames = [];

  function read(storage, key) {
    try { return storage.getItem(key); } catch (e) { return null; }
  }
  function write(storage, key, value) {
    try { storage.setItem(key, value); } catch (e) { /* private window etc. */ }
  }

  function formatBytes(n) {
    if (!n) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = n, i = 0;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i += 1; }
    return `${size.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
  }

  function percent(game) {
    return game.bytes_total ? Math.min(100, Math.floor((game.bytes_done / game.bytes_total) * 100)) : 0;
  }

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  // Row: cover | name + what it is made of | status over the bar | size, %.
  function buildRow(game) {
    const li = el("li", "queue-dock-row");
    const link = el("a", "queue-dock-link");
    link.href = "/queue";
    const cover = window.SwitchAgentGameThumb(game.cover_id, game.name);
    const title = el("span", "queue-dock-title-col");
    title.append(el("span", "queue-dock-name"), el("span", "queue-dock-parts"));
    const progress = el("span", "queue-dock-progress");
    const bar = el("span", "queue-dock-bar seg-bar");
    bar.appendChild(el("span", "seg-bar-fill"));
    progress.append(el("span", "queue-dock-status"), bar);
    const numbers = el("span", "queue-dock-numbers");
    numbers.append(el("span", "queue-dock-size"), el("span", "queue-dock-pct"));
    link.append(cover, title, progress, numbers);
    li.appendChild(link);
    return li;
  }

  function updateRow(li, game) {
    li.dataset.state = game.state;
    const pct = percent(game);
    const installing = game.state === "installing" && game.bytes_total;
    const name = li.querySelector(".queue-dock-name");
    name.textContent = game.name;
    name.title = game.name;
    li.querySelector(".queue-dock-parts").textContent = game.parts_label;
    li.querySelector(".queue-dock-status").textContent = game.status_label;
    li.querySelector(".queue-dock-size").textContent = installing
      ? `${formatBytes(game.bytes_done) || "0 B"} / ${formatBytes(game.bytes_total)}`
      : formatBytes(game.bytes_total);
    li.querySelector(".queue-dock-pct").textContent = installing ? `${pct}%` : "";
    const bar = li.querySelector(".seg-bar");
    bar.classList.toggle("is-running", game.state === "installing");
    bar.classList.toggle("is-sweeping", game.state === "preparing");
    bar.querySelector(".seg-bar-fill").style.width = `${pct}%`;
  }

  function setCollapsed(collapsed) {
    dock.classList.toggle("is-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.title = collapsed ? "Expand" : "Minimize";
  }

  // "×" hides the dock for what is queued right now; anything new queued
  // afterwards brings it back.
  function dismissed(games) {
    let keys = [];
    try { keys = JSON.parse(read(localStorage, DISMISSED_KEY) || "[]"); } catch (e) { /* ignore */ }
    return games.length > 0 && games.every((g) => keys.includes(g.key));
  }

  function render(data) {
    const games = (data && data.games) || [];
    lastGames = games;
    if (!games.length) write(localStorage, DISMISSED_KEY, "[]");
    const visible = games.length > 0 && !dismissed(games);
    dock.hidden = !visible;
    document.body.classList.toggle("has-queue-dock", visible);
    if (!visible) return;

    count.textContent = `(${games.length})` + (data.paused ? " · Paused" : "");
    const first = games[0];
    summary.textContent = first.state === "installing"
      ? `${first.name} · ${percent(first)}%`
      : `${first.name} · ${first.status_label}`;
    line.style.width = `${percent(first)}%`;

    const shown = games.slice(0, MAX_ROWS);
    const keep = new Set(shown.map((g) => g.key));
    rows.forEach((li, key) => {
      if (!keep.has(key)) { li.remove(); rows.delete(key); }
    });
    shown.forEach((game, index) => {
      let li = rows.get(game.key);
      if (!li) { li = buildRow(game); rows.set(game.key, li); }
      updateRow(li, game);
      if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
    });

    const hiddenCount = games.length - shown.length;
    more.hidden = hiddenCount <= 0;
    more.textContent = hiddenCount > 0 ? `+${hiddenCount} more in Queue →` : "";
  }

  // Page padding follows the dock's real height, so it never covers the
  // last row of whatever page is underneath.
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty("--queue-dock-h", `${dock.offsetHeight}px`);
    }).observe(dock);
  }

  setCollapsed(read(localStorage, COLLAPSED_KEY) === "1");
  toggle.addEventListener("click", () => {
    const collapsed = !dock.classList.contains("is-collapsed");
    setCollapsed(collapsed);
    write(localStorage, COLLAPSED_KEY, collapsed ? "1" : "0");
  });
  close.addEventListener("click", () => {
    write(localStorage, DISMISSED_KEY, JSON.stringify(lastGames.map((g) => g.key)));
    render({ games: lastGames });
  });

  try { render(JSON.parse(read(sessionStorage, CACHE_KEY) || "null")); } catch (e) { /* stale shape */ }

  let timer = null;
  async function poll() {
    clearTimeout(timer);
    if (!desktop.matches) return; // resumed by the media listener below
    let busy = lastGames.length > 0;
    if (!document.hidden) {
      try {
        const res = await fetch("/api/queue/dock", { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          render(data);
          write(sessionStorage, CACHE_KEY, JSON.stringify(data));
          busy = data.games.length > 0;
        }
      } catch (e) { /* server restarting -- keep the last state, try again */ }
    }
    timer = setTimeout(poll, busy ? 1500 : 2500);
  }
  desktop.addEventListener("change", poll);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  document.addEventListener("DOMContentLoaded", poll);
})();
