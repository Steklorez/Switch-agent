// Amiibo page (templates/amiibo.html): renders GET /api/amiibo, polls the
// cheap GET /api/amiibo/activity while the worker reads or changes this
// Switch, and reloads the full view only when that finishes.
(function () {
  "use strict";

  const root = document.getElementById("amiibo-root");
  const deviceSelect = document.getElementById("amiibo-device");
  const refreshBtn = document.getElementById("amiibo-refresh");
  const dialog = document.getElementById("amiibo-remove-dialog");
  if (!root) return;

  let view = null;
  let device = root.dataset.device || "";
  let lastReadAt = null;
  let lastBusy = false;
  let search = "";
  // Checked amiibo, kept across re-renders: console paths, and per
  // collection the library destinations.
  const consoleSelected = new Set();
  const librarySelected = new Map();  // collection id -> Set(dest)
  const openGroups = new Set();       // "<section>:<group>" of expanded folders

  // -- tiny DOM helpers ----------------------------------------------------

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function plural(n, one, many) {
    // "amiibo" is its own plural.
    return n + " " + (n === 1 ? one : (many || (one === "amiibo" ? one : one + "s")));
  }

  function downloading(d) {
    return Boolean(d && d.state !== "done" && d.state !== "failed");
  }

  function kb(bytes) {
    return Math.round((bytes || 0) / 1024) + " KB";
  }

  async function downloadEmuiibo(button, install) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      await post("/api/amiibo/emuiibo/download", { device: install && view.device ? view.device.fingerprint : null });
      view.activity = Object.assign({}, view.activity, { download: { state: "checking" } });
      lastBusy = true;
      renderActivity();
      button.textContent = "Downloading…";
    } catch (e) {
      button.textContent = original;
      button.disabled = false;
      alert("Could not start the download: " + e.message);
    }
  }

  async function installAddon(button, addon) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      await post("/api/addons/install", { device: view.device.fingerprint, addon: addon });
      view.activity = Object.assign({}, view.activity, { download: { state: "checking" } });
      lastBusy = true;
      renderActivity();
      button.textContent = "Downloading…";
    } catch (e) {
      button.textContent = original;
      button.disabled = false;
      alert("Could not start the install: " + e.message);
    }
  }

  function matches(entry) {
    if (!search) return true;
    const hay = [entry.label, entry.name, entry.amiibo_id, entry.group, entry.path, entry.dest]
      .filter(Boolean).join(" ").toLowerCase();
    return hay.includes(search);
  }

  async function api(url, options) {
    const res = await fetch(url, options);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const detail = data && (data.detail || (data.errors || []).map((e) => e.error).join("; "));
      throw new Error(detail || ("HTTP " + res.status));
    }
    return data;
  }

  function post(url, body) {
    return api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  }

  // -- loading ---------------------------------------------------------------

  async function load() {
    const url = "/api/amiibo" + (device ? "?device=" + encodeURIComponent(device) : "");
    try {
      view = await api(url);
    } catch (e) {
      root.replaceChildren(el("p", { class: "empty-state", text: "Could not load: " + e.message }));
      return;
    }
    if (view.device) {
      device = view.device.fingerprint;
      const target = new URL(window.location.href);
      target.searchParams.set("device", device);
      window.history.replaceState(null, "", target);
    }
    lastReadAt = view.console ? view.console.read_at : null;
    lastBusy = Boolean(view.activity && (view.activity.reading || (view.activity.removals || []).length
      || downloading(view.activity.download)));
    render();
  }

  async function poll() {
    if (!device || document.hidden) return;
    let activity;
    try {
      activity = await api("/api/amiibo/activity?device=" + encodeURIComponent(device));
    } catch (_) {
      return;
    }
    const busy = Boolean(activity.reading || activity.removals.length || downloading(activity.download));
    const changed = activity.read_at !== lastReadAt || (lastBusy && !busy)
      || (view && view.device && Boolean(view.device.connected) !== Boolean(activity.connected));
    if (changed) {
      await load();
      return;
    }
    if (view) {
      view.activity = activity;
      renderActivity();
    }
    lastBusy = busy;
  }

  // -- rendering -------------------------------------------------------------

  function render() {
    renderDevices();
    const parts = [];
    if (!view.device) {
      parts.push(el("p", { class: "empty-state",
        text: "No Switch has been connected yet. Connect one with DBI's MTP responder running." }));
      parts.push(renderLibrary());
      root.replaceChildren(...parts);
      return;
    }
    parts.push(el("div", { id: "amiibo-activity", class: "amiibo-activity", hidden: true }));
    parts.push(renderEmuiibo());
    parts.push(renderConsole());
    parts.push(renderLibrary());
    root.replaceChildren(...parts);
    renderActivity();
  }

  function renderDevices() {
    deviceSelect.replaceChildren(...view.devices.map((d) => el("option", {
      value: d.fingerprint, selected: view.device && d.fingerprint === view.device.fingerprint,
      text: d.name + (d.connected ? "" : " (not connected)"),
    })));
    deviceSelect.disabled = view.devices.length < 2;
    refreshBtn.disabled = !(view.device && view.device.connected);
    refreshBtn.title = view.device && view.device.connected
      ? "Read emuiibo and every amiibo off this Switch again"
      : "This Switch is not connected";
  }

  function renderActivity() {
    const box = document.getElementById("amiibo-activity");
    if (!box || !view) return;
    const a = view.activity || {};
    const lines = [];
    if (a.reading) {
      lines.push("Reading emuiibo and amiibo off the Switch… " +
        (a.folders_read ? plural(a.folders_read, "folder") + " so far" : "starting") +
        (a.amiibo_read ? ", " + plural(a.amiibo_read, "amiibo") + " found" : ""));
    }
    for (const r of a.removals || []) {
      lines.push("Removing " + plural(r.amiibo, "amiibo") + " from the Switch… " +
        plural(r.deleted, "file or folder", "files and folders") + " removed");
    }
    if (a.read_error) lines.push("The last read did not finish: " + a.read_error);
    const d = a.download;
    if (d && (downloading(d) || (d.finished && Date.now() / 1000 - d.finished < 300))) {
      lines.push(window.SwitchAgentAddons.describe(d) +
        (d.state === "done" && d.queued ? " Restart the Switch once it is installed." : ""));
    }
    for (const r of (a.finished_removals || []).slice(0, 1)) {
      if (Date.now() / 1000 - r.finished > 120) continue;
      lines.push(r.state === "done"
        ? "Removed " + plural(r.removed.length, "item") + " (" + plural(r.deleted, "file or folder", "files and folders") + ") from the Switch."
        : "Removal stopped: " + (r.error || "unknown error") + " — " + plural(r.removed.length, "item") + " removed before that.");
    }
    box.hidden = lines.length === 0;
    box.classList.toggle("is-busy", Boolean(a.reading || (a.removals || []).length || downloading(a.download)));
    box.replaceChildren(...lines.map((line) => el("div", { text: line })));
  }

  function renderEmuiibo() {
    const c = view.console;
    const section = el("section", { class: "detail-section amiibo-panel", id: "amiibo-emuiibo" });
    const heading = el("div", { class: "amiibo-panel-head" },
      el("h3", { text: "emuiibo on " + view.device.name }),
      c && c.read_at_label ? el("span", { class: "settings-hint", text: "as read at " + c.read_at_label }) : null,
    );
    section.append(heading);
    if (!c) {
      section.append(el("p", { class: "settings-hint", text: view.device.connected
        ? "Reading this Switch for the first time…"
        : "This Switch has not been read yet — connect it to see what it has." }));
      return section;
    }
    const list = el("ul", { class: "amiibo-components" });
    for (const comp of c.components) {
      const version = comp.key === "overlay" && comp.present && c.overlay_version ? " " + c.overlay_version : "";
      list.append(el("li", { class: "amiibo-component " + (comp.present ? "is-present" : "is-missing") },
        el("span", { class: "amiibo-component-mark", "aria-hidden": "true", text: comp.present ? "✓" : "✕" }),
        el("span", { class: "amiibo-component-name", text: comp.name + version }),
        el("span", { class: "amiibo-component-purpose", text: comp.purpose }),
        comp.present ? null : el("a", { class: "amiibo-component-source", href: comp.source,
          target: "_blank", rel: "noopener", text: comp.in_release ? "emuiibo releases →" : "releases →" }),
      ));
    }
    section.append(list);

    const facts = [];
    if (c.installed && c.emulation_on !== null && c.emulation_on !== undefined) {
      facts.push(c.emulation_on ? "Emulation is on after a restart." : "Emulation is off — turn it on in the overlay.");
    }
    if (facts.length) section.append(el("p", { class: "settings-hint", text: facts.join(" ") }));

    if (view.advice.length) {
      const advice = el("ul", { class: "amiibo-advice" });
      for (const item of view.advice) {
        const li = el("li", { class: "amiibo-advice-" + item.kind }, el("span", { text: item.text }));
        if (item.kind === "install_release") {
          li.append(el("button", { type: "button", class: "btn btn-primary btn-small",
            disabled: !view.device.connected, text: "Install",
            onclick: (ev) => installItems(ev.currentTarget, [item.item_id], null) }));
        } else if (item.kind === "download_release") {
          const busy = downloading(view.activity && view.activity.download);
          li.append(el("button", { type: "button", class: "btn btn-primary btn-small",
            disabled: !view.device.connected || busy, text: busy ? "Downloading…" : "Download and install",
            title: "Downloads emuiibo.zip from github.com/XorTroll/emuiibo, checks it, adds it to your Library " +
                   "and queues it for this Switch",
            onclick: (ev) => downloadEmuiibo(ev.currentTarget, true) }));
        } else if (item.kind === "install_addon") {
          const busy = downloading(view.activity && view.activity.download);
          li.append(el("button", { type: "button", class: "btn btn-primary btn-small",
            disabled: !view.device.connected || busy, text: busy ? "Downloading…" : "Install",
            title: "Downloads it from its GitHub releases, checks it, adds it to your Library and queues it for this Switch",
            onclick: (ev) => installAddon(ev.currentTarget, item.addon) }));
        } else if (item.kind === "link") {
          li.append(el("a", { class: "btn btn-secondary btn-small", href: item.href, target: "_blank",
            rel: "noopener", text: "Open releases" }));
        }
        advice.append(li);
      }
      section.append(advice);
    }
    section.append(el("p", { class: "settings-hint amiibo-howto" },
      "emuiibo starts with the Switch: restart it after installing or updating emuiibo. New amiibo show up the " +
      "next time the overlay is opened. In a game, hold L + D-pad Down and press the right stick, then choose emuiibo. ",
      el("a", { href: "/addons#addon-emuiibo", text: "How to use emuiibo →" })));
    return section;
  }

  function badge(text, kind, title) {
    return el("span", { class: "amiibo-badge amiibo-badge-" + kind, title: title || null, text });
  }

  function groupBlock(sectionKey, group, rows, { checkedAll, onToggleAll, countText }) {
    const key = sectionKey + ":" + group.name;
    const details = el("details", { class: "amiibo-group", open: openGroups.has(key) || Boolean(search) });
    details.addEventListener("toggle", () => {
      if (details.open) openGroups.add(key); else openGroups.delete(key);
    });
    const box = el("input", { type: "checkbox", class: "amiibo-group-box", "aria-label": "Select the whole folder" });
    box.checked = checkedAll;
    box.addEventListener("click", (ev) => ev.stopPropagation());
    box.addEventListener("change", () => onToggleAll(box.checked));
    details.append(el("summary", {},
      box,
      el("span", { class: "amiibo-group-name", text: group.name || "(top level)" }),
      el("span", { class: "amiibo-group-count", text: countText }),
    ));
    details.append(el("div", { class: "amiibo-rows" }, rows));
    return details;
  }

  function renderConsole() {
    const c = view.console;
    const section = el("section", { class: "detail-section amiibo-panel", id: "amiibo-console" });
    if (!c) return section;
    const facts = [plural(c.count, "amiibo")];
    if (c.with_save_data) facts.push(c.with_save_data + " with game save data");
    if (c.favorites) facts.push(plural(c.favorites, "favorite"));
    if (c.hidden) facts.push(c.hidden + " hidden (no amiibo.flag)");
    if (c.invalid) facts.push(c.invalid + " emuiibo cannot read");
    if (c.dumps) facts.push(plural(c.dumps, "raw dump") + " converted at the next restart");
    section.append(el("div", { class: "amiibo-panel-head" },
      el("h3", { text: "On this Switch" }), el("span", { class: "settings-hint", text: facts.join(" · ") })));

    if (!c.library_exists && !c.count) {
      section.append(el("p", { class: "settings-hint", text: "There is no emuiibo/amiibo folder on this Switch yet — " +
        "installing amiibo from your Library creates it." }));
      return section;
    }

    const toolbar = el("div", { class: "amiibo-toolbar" });
    const searchBox = el("input", { type: "search", class: "search-box", placeholder: "Search amiibo…", value: search });
    searchBox.addEventListener("input", () => {
      search = searchBox.value.trim().toLowerCase();
      const pos = searchBox.selectionStart;
      render();
      const again = document.querySelector("#amiibo-console .search-box");
      if (again) { again.focus(); again.setSelectionRange(pos, pos); }
    });
    const removeBtn = el("button", { type: "button", class: "btn btn-danger btn-small",
      disabled: consoleSelected.size === 0 || !view.device.connected,
      title: view.device.connected ? null : "This Switch is not connected",
      text: consoleSelected.size ? "Remove " + plural(consoleSelected.size, "amiibo") + "…" : "Remove…",
      onclick: openRemoveDialog });
    toolbar.append(searchBox, removeBtn);
    section.append(toolbar);

    let shown = 0;
    for (const group of c.groups) {
      const visible = group.amiibo.filter(matches);
      if (!visible.length) continue;
      shown += visible.length;
      const rows = visible.map((a) => {
        const box = el("input", { type: "checkbox", "aria-label": "Select " + a.label });
        box.checked = consoleSelected.has(a.path);
        box.addEventListener("change", () => {
          if (box.checked) consoleSelected.add(a.path); else consoleSelected.delete(a.path);
          render();
        });
        const badges = [];
        if (a.kind === "dump") badges.push(badge("raw dump", "dump", "emuiibo converts it into a virtual amiibo at the next restart"));
        else if (!a.name) badges.push(badge("unreadable", "bad", "emuiibo cannot use this amiibo.json"));
        if (a.kind !== "dump" && !a.enabled) badges.push(badge("hidden", "hidden", "No amiibo.flag — emuiibo does not list it"));
        if (a.save_data) badges.push(badge("save data", "save", "Holds game save data (areas/)"));
        if (a.favorite) badges.push(badge("★", "fav", "A favorite in the emuiibo overlay"));
        return el("label", { class: "amiibo-row" }, box,
          el("span", { class: "amiibo-label", text: a.label }),
          a.name && a.name !== a.label ? el("span", { class: "amiibo-name", text: a.name }) : null,
          a.amiibo_id ? el("code", { class: "amiibo-id", text: a.amiibo_id }) : null,
          el("span", { class: "amiibo-badges" }, badges));
      });
      const allChecked = visible.every((a) => consoleSelected.has(a.path));
      section.append(groupBlock("console", group, rows, {
        checkedAll: allChecked,
        countText: visible.length === group.amiibo.length ? String(group.amiibo.length)
          : visible.length + " of " + group.amiibo.length,
        onToggleAll: (on) => {
          for (const a of visible) { if (on) consoleSelected.add(a.path); else consoleSelected.delete(a.path); }
          render();
        },
      }));
    }
    if (!shown) section.append(el("p", { class: "settings-hint", text: search ? "Nothing matches." : "No amiibo yet." }));
    return section;
  }

  function renderLibrary() {
    const section = el("section", { class: "detail-section amiibo-panel", id: "amiibo-library" });
    section.append(el("div", { class: "amiibo-panel-head" }, el("h3", { text: "In your Library" })));
    if (!view.collections.length && !view.releases.length) {
      section.append(el("p", { class: "settings-hint", text: "No virtual amiibo and no emuiibo release in your Library " +
        "folders yet. A folder (or archive) of amiibo — each one a folder holding amiibo.json and amiibo.flag, " +
        "as emuiigen makes them — is found by the next scan." }));
    }
    const busyDownload = downloading(view.activity && view.activity.download);
    section.append(el("div", { class: "amiibo-release amiibo-release-get" },
      el("span", { class: "settings-hint", text: "emuiibo itself: SwitchAgent can fetch its current release from " +
        "github.com/XorTroll/emuiibo into your Library." }),
      el("button", { type: "button", class: "btn btn-secondary btn-small", disabled: busyDownload,
        text: busyDownload ? "Downloading…" : "Download the current emuiibo",
        onclick: (ev) => downloadEmuiibo(ev.currentTarget, false) })));
    for (const r of view.releases) {
      section.append(el("div", { class: "amiibo-release" },
        el("span", { class: "amiibo-release-name", text: r.name }),
        el("span", { class: "settings-hint", text: "emuiibo " + (r.version || "(version unknown)") +
          (r.outdated ? " — outdated, " + view.latest_version + " is current" : "") }),
        el("button", { type: "button", class: "btn btn-secondary btn-small",
          disabled: !(view.device && view.device.connected) || !r.can_install, text: "Install",
          onclick: (ev) => installItems(ev.currentTarget, [r.id], null) }),
      ));
    }
    for (const tool of view.pc_tools || []) {
      section.append(el("div", { class: "amiibo-release" },
        el("span", { class: "amiibo-release-name", text: tool.name }),
        el("span", { class: "settings-hint", text: "A PC app for making virtual amiibo — it runs on a PC; " +
          "nothing of it goes to the Switch." })));
    }
    for (const col of view.collections) section.append(renderCollection(col));
    return section;
  }

  function renderCollection(col) {
    const selected = librarySelected.get(col.id) || new Set();
    librarySelected.set(col.id, selected);
    const card = el("div", { class: "amiibo-collection" });
    const counts = col.counts;
    const facts = [plural(col.count, "amiibo")];
    if (col.folders) facts.push(plural(col.folders, "folder"));
    if (counts) {
      facts.push(counts.on_console + " on this Switch");
      if (counts.different) facts.push(counts.different + " where a different amiibo already is");
    }
    if (col.disabled) facts.push(col.disabled + " without amiibo.flag (emuiibo hides those)");
    if (col.dumps) facts.push(plural(col.dumps, "raw dump") + " (converted by emuiibo at boot)");
    const missing = [];
    for (const g of col.groups) for (const a of g.amiibo) if (a.status === "missing") missing.push(a.dest);
    const connected = Boolean(view.device && view.device.connected);
    card.append(el("div", { class: "amiibo-collection-head" },
      el("div", {},
        el("div", { class: "amiibo-collection-name", title: col.path, text: col.name }),
        el("div", { class: "settings-hint", text: facts.join(" · ") })),
      el("div", { class: "amiibo-collection-actions" },
        el("button", { type: "button", class: "btn btn-primary btn-small",
          disabled: !connected || !col.can_install || (counts && !missing.length) || (!counts && !col.count),
          text: counts ? (missing.length ? "Install " + missing.length + " not on this Switch" : "All on this Switch")
            : "Install all",
          onclick: (ev) => installItems(ev.currentTarget, [col.id], counts ? { [col.id]: missing } : null) }),
        el("button", { type: "button", class: "btn btn-secondary btn-small",
          disabled: !connected || !col.can_install || selected.size === 0,
          text: selected.size ? "Install " + selected.size + " selected" : "Install selected",
          onclick: (ev) => installItems(ev.currentTarget, [col.id], { [col.id]: Array.from(selected) }) }),
      )));
    for (const group of col.groups) {
      const visible = group.amiibo.filter(matches);
      if (!visible.length) continue;
      const rows = visible.map((a) => {
        const box = el("input", { type: "checkbox", "aria-label": "Select " + a.label });
        box.checked = selected.has(a.dest);
        box.addEventListener("change", () => {
          if (box.checked) selected.add(a.dest); else selected.delete(a.dest);
          render();
        });
        const badges = [];
        if (a.status === "on_console") badges.push(badge("on this Switch", "on", null));
        if (a.status === "different") badges.push(badge("different amiibo there", "bad",
          "The Switch already has a different amiibo in this folder; installing leaves it as it is"));
        if (a.kind === "dump") badges.push(badge("raw dump", "dump", "Goes to the top of emuiibo/amiibo — emuiibo converts it at boot"));
        else if (!a.enabled) badges.push(badge("no amiibo.flag", "hidden", "emuiibo ignores an amiibo without its flag"));
        return el("label", { class: "amiibo-row" + (a.status === "on_console" ? " is-done" : "") }, box,
          el("span", { class: "amiibo-label", text: a.label }),
          a.name && a.name !== a.label ? el("span", { class: "amiibo-name", text: a.name }) : null,
          a.amiibo_id ? el("code", { class: "amiibo-id", text: a.amiibo_id }) : null,
          el("span", { class: "amiibo-badges" }, badges));
      });
      const onConsole = group.amiibo.filter((a) => a.status === "on_console").length;
      card.append(groupBlock("lib" + col.id, group, rows, {
        checkedAll: visible.every((a) => selected.has(a.dest)),
        countText: (counts ? onConsole + " / " : "") + group.amiibo.length,
        onToggleAll: (on) => {
          for (const a of visible) { if (on) selected.add(a.dest); else selected.delete(a.dest); }
          render();
        },
      }));
    }
    return card;
  }

  // -- actions ---------------------------------------------------------------

  async function installItems(button, itemIds, selection) {
    if (!view.device) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Adding to Queue…";
    try {
      const body = { library_item_ids: itemIds, target_device_id: view.device.fingerprint };
      if (selection) body.amiibo_selection = selection;
      await post("/api/preparations", body);
      button.textContent = "Added to Queue";
      for (const id of itemIds) librarySelected.delete(id);
    } catch (e) {
      button.textContent = original;
      button.disabled = false;
      alert("Could not add to Queue: " + e.message);
    }
  }

  function openRemoveDialog() {
    const c = view.console;
    const chosen = c.amiibo.filter((a) => consoleSelected.has(a.path));
    if (!chosen.length) return;
    const withSave = chosen.filter((a) => a.save_data);
    document.getElementById("amiibo-remove-title").textContent = "Remove " + plural(chosen.length, "amiibo") +
      " from " + view.device.name + "?";
    document.getElementById("amiibo-remove-text").textContent =
      "Their folders are deleted from emuiibo/amiibo on the SD card, and they leave the overlay's favorites. " +
      "Your Library is not touched — you can install them again at any time.";
    const list = document.getElementById("amiibo-remove-list");
    const items = chosen.slice(0, 12).map((a) => el("li", { text: a.path + (a.save_data ? " — save data" : "") }));
    if (chosen.length > 12) items.push(el("li", { text: "…and " + (chosen.length - 12) + " more" }));
    list.replaceChildren(...items);
    const saveLabel = document.getElementById("amiibo-remove-save");
    const saveBox = document.getElementById("amiibo-remove-save-box");
    saveLabel.hidden = withSave.length === 0;
    saveBox.checked = false;
    const who = chosen.length === 1 ? "It holds"
      : withSave.length === 1 ? "1 of them holds" : withSave.length + " of them hold";
    document.getElementById("amiibo-remove-save-text").textContent = withSave.length
      ? who + " game save data — per-game progress stored on the amiibo. It is deleted too and cannot be " +
        "brought back; I understand."
      : "";
    const confirmBtn = document.getElementById("amiibo-remove-confirm");
    const error = document.getElementById("amiibo-remove-error");
    error.hidden = true;
    const sync = () => { confirmBtn.disabled = withSave.length > 0 && !saveBox.checked; };
    saveBox.onchange = sync;
    sync();
    confirmBtn.onclick = async () => {
      confirmBtn.disabled = true;
      try {
        await post("/api/amiibo/remove", { device: view.device.fingerprint, paths: chosen.map((a) => a.path),
          confirm_save_data: saveBox.checked });
        consoleSelected.clear();
        dialog.close();
        await load();
      } catch (e) {
        error.hidden = false;
        error.textContent = e.message;
        sync();
      }
    };
    dialog.showModal();
  }

  document.getElementById("amiibo-remove-cancel").addEventListener("click", () => dialog.close());

  deviceSelect.addEventListener("change", () => {
    device = deviceSelect.value;
    consoleSelected.clear();
    librarySelected.clear();
    load();
  });

  refreshBtn.addEventListener("click", async () => {
    if (!view || !view.device) return;
    refreshBtn.disabled = true;
    try {
      await post("/api/amiibo/refresh", { device: view.device.fingerprint });
      view.activity = Object.assign({}, view.activity, { reading: true, folders_read: 0, amiibo_read: 0, read_error: null });
      lastBusy = true;
      renderActivity();
    } catch (e) {
      alert("Could not start reading: " + e.message);
    } finally {
      refreshBtn.disabled = false;
    }
  });

  load();
  setInterval(poll, 2500);
  // A slow catch-all for what the activity poll cannot see (a console
  // plugged in, a scan that found a new collection) -- never while the
  // person is typing a search or deciding in the removal dialog.
  setInterval(() => {
    const typing = document.activeElement && root.contains(document.activeElement)
      && document.activeElement.matches("input[type=search]");
    if (!document.hidden && !dialog.open && !typing) load();
  }, 30000);
})();
