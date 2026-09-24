// Backups Lab sends commands only to the HTTP API. The existing server worker owns MTP.
(() => {
  "use strict";
  const root = document.getElementById("backup-lab");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const selected = { saves: new Set(), local: new Set(), games: new Set() };
  const seenJobs = new Set();
  const downloads = new Set();
  const cancellingPlans = new Set();
  let devices = [];
  let state = { inventory: {}, catalog: { snapshots: [], incomplete: [], games: [] }, jobs: [] };
  let plan = null;
  let uploadRequest = null;
  let messageTimer;
  let polling = false;
  let coverSignature = "";
  let readyCovers = new Set();
  let visibleSaveRows = [];
  let visibleSaveGroups = new Map();
  let visibleLocalRows = [];
  let visibleLocalGroups = new Map();
  const expandedGames = new Set();
  const expandedLocalGames = new Set();

  function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }
  function formatBytes(value) {
    if (value == null || !Number.isFinite(Number(value))) return "Size unknown";
    if (Number(value) === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const unit = Math.min(units.length - 1, Math.floor(Math.log(Number(value)) / Math.log(1024)));
    return `${(Number(value) / (1024 ** unit)).toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }
  function formatDate(seconds) {
    if (!Number.isFinite(Number(seconds))) return "Date unknown";
    return new Date(Number(seconds) * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }
  function labelPath(path) {
    const parts = String(path || "").split("/");
    return parts.length >= 3 ? `${parts[1]} · ${parts[2]}` : String(path || "Unknown save");
  }
  function notify(message, error = false) {
    const box = $("backup-message");
    box.textContent = message;
    box.classList.toggle("is-error", error);
    box.hidden = false;
    clearTimeout(messageTimer);
    messageTimer = setTimeout(() => { box.hidden = true; }, error ? 12000 : 6500);
  }
  async function api(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const data = await response.json();
        if (typeof data.detail === "string") detail = data.detail;
        else if (Array.isArray(data.detail)) detail = data.detail.map(item => item.msg || "Invalid value").join("; ");
      } catch (_) { /* Keep the HTTP status. */ }
      throw new Error(detail);
    }
    return response.json();
  }
  function post(url, body) {
    return api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  }
  function chosenDevice() { return $("backup-device").value; }
  function activeView() { return root.querySelector('[role="tab"][aria-selected="true"]')?.id || "backup-tab-saves"; }
  function inventory(kind) { return state.inventory?.[chosenDevice()]?.[kind] || []; }
  function snapshots() { return state.catalog?.snapshots || []; }
  function gameExports() { return state.catalog?.games || []; }
  function sourceDeviceLabel(item) {
    const fingerprint = item.origin?.device_fingerprint;
    const device = devices.find(row => row.device_fingerprint === fingerprint);
    return device?.friendly_name || device?.display_name ||
      (fingerprint ? `Switch •${fingerprint.slice(-4).toUpperCase()}` : "Source Switch unknown");
  }
  function clearSelection(kind) { selected[kind].clear(); render(); }
  function renderDeviceStatus() {
    const view = activeView(), local = view === "backup-tab-local", games = view === "backup-tab-games";
    $("backup-heading-title").textContent = local ? "Your saved copies" :
      games ? "Export game packages" : "Back up your Switch saves";
    $("backup-device-label").textContent = local ? "Target Switch (for restore)" : "Switch";
    $("backup-device").setAttribute("aria-label", local ? "Target Switch for restore" :
      games ? "Switch for game export" : "Switch for save backup");
    $("backup-heading-subtitle").textContent = local
      ? "Find, check, and download copies saved on this computer."
      : games ? "Export game packages separately from save copies."
        : "Find saves on your console, then keep a copy on this computer.";
    $("backup-device-status").textContent = local
      ? chosenDevice() ? "Selected for restore" : "No Switch needed to download"
      : chosenDevice() ? "Connected" :
        !devices.some(device => device.connected) ? "No Switch connected" :
          games ? "Choose a Switch to scan packages" : "Choose a Switch to find saves";
  }
  function titleId(row) {
    const id = row?.cover_id || row?.identity?.title_id || row?.title_id;
    return /^[0-9a-f]{16}$/i.test(String(id || "")) ? String(id).toUpperCase() : null;
  }
  function cover(row) {
    const id = titleId(row);
    const shell = el("span", "backup-cover-shell");
    const parts = String(row?.source_path || row?.path || "").split("/");
    const gameName = parts.length >= 3 ? parts[1] : row?.name || "?";
    shell.append(el("span", "backup-cover-placeholder", gameName.charAt(0).toUpperCase()));
    if (!id || !readyCovers.has(id)) return shell;
    const image = el("img", "backup-cover");
    image.alt = "";
    image.loading = "lazy";
    image.onerror = () => { image.hidden = true; };
    image.onload = () => { image.hidden = false; shell.classList.add("has-cover"); };
    image.src = `/api/covers/${encodeURIComponent(id)}`;
    shell.append(image);
    return shell;
  }
  function empty(list, text) { list.replaceChildren(el("p", "empty-state", text)); }

  function rowShell(item, kind, title, meta, canSelect = true, showCover = true) {
    const row = el("div", "backup-row");
    if (kind) {
      const input = el("input");
      input.type = "checkbox";
      if (kind === "saves") input.dataset.savePath = item.path;
      if (kind === "local") input.dataset.snapshotId = item.id;
      input.disabled = !canSelect;
      input.checked = canSelect && selected[kind].has(kind === "local" ? item.id : item.path);
      input.setAttribute("aria-label", `Select ${title}` +
        (kind === "local" ? ` from ${sourceDeviceLabel(item)} saved ${formatDate(item.created_at)}` : ""));
      input.addEventListener("change", () => {
        const key = kind === "local" ? item.id : item.path;
        if (input.checked) selected[kind].add(key); else selected[kind].delete(key);
        updateSelection();
      });
      row.append(input);
    }
    if (showCover && (kind || item.cover_id)) row.append(cover(item));
    const main = el("div", "backup-row-main");
    main.append(el("div", "backup-row-title", title), el("div", "backup-row-meta", meta));
    if (item.reason) main.append(el("div", "backup-row-reason", item.reason));
    row.append(main);
    return row;
  }
  function saveRows() {
    const rows = inventory("saves");
    const device = devices.find(item => item.device_id === chosenDevice());
    const copies = new Map();
    for (const copy of snapshots()) {
      if (copy.origin?.device_fingerprint !== device?.device_fingerprint) continue;
      const older = copies.get(copy.source_path);
      if (!older || Number(copy.created_at) > Number(older.created_at)) copies.set(copy.source_path, copy);
    }
    const filter = $("backup-profile-filter");
    const prior = filter.value;
    const names = [...new Set(rows.filter(r => String(r.path || "").split("/").length === 3)
      .map(r => String(r.path).split("/")[2]))].sort((a, b) => a.localeCompare(b));
    filter.replaceChildren(new Option("All profiles", ""), ...names.map(name => new Option(name, name)));
    filter.value = names.includes(prior) ? prior : "";
    $("backup-profile-options").hidden = names.length < 2;
    const search = $("backup-save-search").value.trim().toLocaleLowerCase();
    const uncopiedOnly = $("backup-uncopied-only").checked;
    const shown = rows.filter(item => {
      const parts = String(item.path || "").split("/");
      if (filter.value && parts[2] !== filter.value) return false;
      if (uncopiedOnly && copies.has(item.path)) return false;
      return !search || [parts[0], parts[1], parts[2], item.name]
        .some(part => String(part || "").toLocaleLowerCase().includes(search));
    });
    visibleSaveRows = shown;
    const savedCount = rows.filter(item => copies.has(item.path)).length;
    const unavailableCount = rows.filter(item => !item.selectable).length;
    const countLabel = `${shown.length} shown of ${rows.length} save entries · ${savedCount} with a local copy` +
      (unavailableCount ? ` · ${unavailableCount} unavailable` : "");
    $("backup-save-counts").textContent = countLabel;
    $("backup-save-counts").hidden = rows.length === 0;
    const list = $("backup-save-list");
    visibleSaveGroups = new Map();
    if (!shown.length) {
      empty(list, rows.length ? "No saves match these filters." : "Press Find saves to list console saves.");
      return;
    }
    const groups = new Map();
    for (const item of shown) {
      const parts = String(item.path || "").split("/");
      const key = parts.length >= 2 ? JSON.stringify(parts.slice(0, 2)) : item.path;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    const saveMeta = (item, includeProfile) => {
      const parts = String(item.path || "").split("/");
      const count = item.file_count == null ? "Files not counted" :
        `${item.file_count} ${item.file_count === 1 ? "file" : "files"}`;
      const lastCopy = copies.get(item.path);
      return [includeProfile ? parts[2] || "Profile unknown" : null, formatBytes(item.size), count,
        lastCopy ? `Last backup ${formatDate(lastCopy.created_at)}` : "Never backed up"].filter(Boolean).join(" · ");
    };
    const cards = [];
    for (const [key, groupRows] of groups) {
      const first = groupRows[0], parts = String(first.path || "").split("/");
      const game = parts.length >= 2 ? parts[1] : first.name || first.path;
      const available = groupRows.filter(item => item.selectable);
      visibleSaveGroups.set(key, available.map(item => item.path));
      if (groupRows.length === 1) {
        const row = rowShell(first, "saves", game, saveMeta(first, true), first.selectable === true);
        row.classList.add("backup-save-single");
        cards.push(row);
        continue;
      }
      const card = el("section", "backup-game-card");
      const header = el("div", "backup-game-header");
      const checkbox = el("input", "backup-group-select");
      checkbox.type = "checkbox";
      checkbox.dataset.groupKey = key;
      checkbox.disabled = available.length === 0;
      checkbox.setAttribute("aria-label", `Select all available saves for ${game}`);
      checkbox.addEventListener("change", () => {
        for (const item of available) {
          if (checkbox.checked) selected.saves.add(item.path);
          else selected.saves.delete(item.path);
        }
        updateSelection();
      });
      const main = el("div", "backup-game-main");
      main.append(el("strong", "backup-row-title", game),
        el("span", "backup-row-meta", `${groupRows.length} save folders · ` +
          `${groupRows.filter(item => copies.has(item.path)).length} with a local copy`));
      const body = el("div", "backup-game-profiles");
      body.id = `backup-group-${cards.length}`;
      body.hidden = !expandedGames.has(key);
      const toggle = el("button", "backup-group-toggle", body.hidden ? "Show profiles" : "Hide profiles");
      toggle.type = "button";
      toggle.setAttribute("aria-controls", body.id);
      toggle.setAttribute("aria-expanded", String(!body.hidden));
      toggle.addEventListener("click", () => {
        body.hidden = !body.hidden;
        if (body.hidden) expandedGames.delete(key); else expandedGames.add(key);
        toggle.textContent = body.hidden ? "Show profiles" : "Hide profiles";
        toggle.setAttribute("aria-expanded", String(!body.hidden));
      });
      header.append(checkbox, cover(first), main, toggle);
      body.replaceChildren(...groupRows.map(item => {
        const profile = String(item.path || "").split("/")[2] || item.name || "Unknown profile";
        return rowShell(item, "saves", profile, saveMeta(item, false), item.selectable === true, false);
      }));
      card.append(header, body);
      cards.push(card);
    }
    list.replaceChildren(...cards);
    syncSaveSelectionControls();
  }
  function syncSaveSelectionControls() {
    const list = $("backup-save-list");
    for (const input of list.querySelectorAll("input[data-save-path]")) {
      input.checked = !input.disabled && selected.saves.has(input.dataset.savePath);
    }
    for (const input of list.querySelectorAll(".backup-group-select")) {
      const paths = visibleSaveGroups.get(input.dataset.groupKey) || [];
      const count = paths.filter(path => selected.saves.has(path)).length;
      input.checked = paths.length > 0 && count === paths.length;
      input.indeterminate = count > 0 && count < paths.length;
    }
  }
  function localRows() {
    const rows = snapshots();
    const list = $("backup-local-list");
    const search = $("backup-local-search").value.trim().toLocaleLowerCase();
    visibleLocalRows = rows.filter(item => !search ||
      [item.source_path, sourceDeviceLabel(item)].some(value => String(value || "").toLocaleLowerCase().includes(search)));
    const games = new Set(rows.map(item => item.origin?.game || String(item.source_path || "").split("/")[1]));
    const consoles = new Set(rows.map(item => item.origin?.device_fingerprint || "unknown"));
    const total = rows.reduce((sum, item) => sum + (item.files || []).reduce((n, file) => n + Number(file.size || 0), 0), 0);
    $("backup-local-summary").textContent = `${rows.length} ${rows.length === 1 ? "copy" : "copies"} · ` +
      `${games.size} ${games.size === 1 ? "game" : "games"} · ` +
      `${consoles.size} ${consoles.size === 1 ? "Switch" : "Switches"} · ${formatBytes(total)} of save files` +
      (search ? ` · ${visibleLocalRows.length} shown` : "");
    $("backup-local-summary").hidden = rows.length === 0;
    $("backup-local-toolbar").hidden = rows.length === 0;
    visibleLocalGroups = new Map();
    if (!rows.length) { empty(list, "No local copies yet. Select saves to copy or import a ZIP."); return; }
    if (!visibleLocalRows.length) { empty(list, "No saved copies match this search."); return; }
    const copyMeta = item => {
      const size = Array.isArray(item.files) ? item.files.reduce((sum, file) => sum + Number(file.size || 0), 0) : null;
      const count = item.files?.length;
      return `${sourceDeviceLabel(item)} · ${formatDate(item.created_at)} · ${formatBytes(size)} · ` +
        (count == null ? "Files not counted" : `${count} ${count === 1 ? "file" : "files"}`);
    };
    const groups = new Map();
    for (const item of visibleLocalRows) {
      const parts = String(item.source_path || "").split("/");
      const key = parts.length >= 2 ? JSON.stringify(parts.slice(0, 2)) : item.source_path;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    const cards = [];
    for (const [key, groupRows] of groups) {
      const first = groupRows[0];
      const parts = String(first.source_path || "").split("/");
      const game = parts.length >= 2 ? parts[1] : first.source_path || "Unknown game";
      visibleLocalGroups.set(key, groupRows.map(item => item.id));
      if (groupRows.length === 1) {
        cards.push(rowShell(first, "local", labelPath(first.source_path), copyMeta(first)));
        continue;
      }
      const card = el("section", "backup-game-card");
      const header = el("div", "backup-game-header");
      const checkbox = el("input", "backup-group-select");
      checkbox.type = "checkbox";
      checkbox.dataset.localGroupKey = key;
      checkbox.setAttribute("aria-label", `Select all ${groupRows.length} local copies for ${game}`);
      checkbox.addEventListener("change", () => {
        for (const item of groupRows) {
          if (checkbox.checked) selected.local.add(item.id);
          else selected.local.delete(item.id);
        }
        updateSelection();
      });
      const main = el("div", "backup-game-main");
      const profiles = new Set(groupRows.map(item => item.origin?.profile || String(item.source_path || "").split("/")[2]));
      const sources = new Set(groupRows.map(item => item.origin?.device_fingerprint || "unknown"));
      main.append(el("strong", "backup-row-title", game),
        el("span", "backup-row-meta", `${groupRows.length} copies · ${profiles.size} ` +
          `${profiles.size === 1 ? "profile" : "profiles"} · ` +
          `${sources.size} ${sources.size === 1 ? "Switch" : "Switches"}`));
      const body = el("div", "backup-game-profiles");
      body.id = `backup-local-group-${cards.length}`;
      body.hidden = !expandedLocalGames.has(key);
      const toggle = el("button", "backup-group-toggle", body.hidden ? "Show copies" : "Hide copies");
      toggle.type = "button";
      toggle.setAttribute("aria-controls", body.id);
      toggle.setAttribute("aria-expanded", String(!body.hidden));
      toggle.addEventListener("click", () => {
        body.hidden = !body.hidden;
        if (body.hidden) expandedLocalGames.delete(key); else expandedLocalGames.add(key);
        toggle.textContent = body.hidden ? "Show copies" : "Hide copies";
        toggle.setAttribute("aria-expanded", String(!body.hidden));
      });
      header.append(checkbox, cover(first), main, toggle);
      body.replaceChildren(...groupRows.map(item => {
        const profile = item.origin?.profile || String(item.source_path || "").split("/")[2] || "Unknown profile";
        return rowShell(item, "local", profile, copyMeta(item), true, false);
      }));
      card.append(header, body);
      cards.push(card);
    }
    list.replaceChildren(...cards);
  }
  function syncLocalSelectionControls() {
    const list = $("backup-local-list");
    for (const input of list.querySelectorAll("input[data-snapshot-id]")) {
      input.checked = selected.local.has(input.dataset.snapshotId);
    }
    for (const input of list.querySelectorAll("input[data-local-group-key]")) {
      const ids = visibleLocalGroups.get(input.dataset.localGroupKey) || [];
      const count = ids.filter(id => selected.local.has(id)).length;
      input.checked = ids.length > 0 && count === ids.length;
      input.indeterminate = count > 0 && count < ids.length;
    }
  }
  function gameRows() {
    const rows = inventory("games");
    const list = $("backup-game-list");
    if (!rows.length) empty(list, "No game packages found. Scan a connected Switch.");
    else list.replaceChildren(...rows.map(item => rowShell(item, "games", item.name || item.path, formatBytes(item.size))));
    const exports = gameExports();
    const output = $("backup-export-list");
    if (!exports.length) { empty(output, "No game exports yet."); return; }
    output.replaceChildren(...exports.map(item => {
      const row = rowShell(item, null, item.name || "Game export", `${formatDate(item.created_at)} · ${formatBytes(item.size)}`);
      const button = el("button", "btn btn-small btn-secondary backup-row-action", "Download");
      button.type = "button";
      button.addEventListener("click", async () => {
        try { await submit("/api/backups/games/" + encodeURIComponent(item.id) + "/download", {}); }
        catch (error) { notify(error.message, true); }
      });
      row.append(button);
      return row;
    }));
  }
  function updateSelection() {
    const availableSaves = new Set(inventory("saves").filter(r => r.selectable).map(r => r.path));
    const availableGames = new Set(inventory("games").map(r => r.path));
    for (const path of selected.saves) if (!availableSaves.has(path)) selected.saves.delete(path);
    for (const path of selected.games) if (!availableGames.has(path)) selected.games.delete(path);
    const saves = inventory("saves").filter(r => selected.saves.has(r.path) && r.selectable);
    const games = inventory("games").filter(r => selected.games.has(r.path));
    const summary = rows => {
      if (!rows.length) return "None selected";
      const known = rows.reduce((sum, r) => sum + Number(r.size || 0), 0);
      const unknown = rows.some(r => r.size == null);
      return `${rows.length} selected · ${formatBytes(known)} known${unknown ? " + unknown size" : ""}`;
    };
    $("backup-save-selection").textContent = summary(saves);
    $("backup-game-selection").textContent = summary(games);
    $("backup-clear-saves").hidden = selected.saves.size === 0;
    $("backup-clear-games").hidden = selected.games.size === 0;
    $("backup-copy-saves").disabled = saves.length === 0 || !chosenDevice();
    $("backup-export-games").disabled = games.length === 0 || !chosenDevice();
    const availableLocal = new Set(snapshots().map(item => item.id));
    for (const id of selected.local) if (!availableLocal.has(id)) selected.local.delete(id);
    $("backup-clear-local").hidden = selected.local.size === 0;
    $("backup-download-selected").hidden = selected.local.size === 0;
    const visibleLocalIds = new Set(visibleLocalRows.map(item => item.id));
    const hiddenSelected = [...selected.local].filter(id => !visibleLocalIds.has(id)).length;
    $("backup-local-selection").textContent = `${selected.local.size} ${selected.local.size === 1 ? "copy" : "copies"} selected` +
      (hiddenSelected ? ` · ${hiddenSelected} outside current search` : "");
    $("backup-local-selection").hidden = selected.local.size === 0;
    $("backup-select-all-local").disabled = visibleLocalRows.length === 0;
    const archiveBusy = (state.jobs || []).some(job => job.action === "create_archive" &&
      (job.state === "queued" || job.state === "running"));
    $("backup-download-all").disabled = availableLocal.size === 0 || archiveBusy;
    $("backup-download-selected").disabled = selected.local.size === 0 || archiveBusy;
    syncSaveSelectionControls();
    syncLocalSelectionControls();
    restoreChoices();
  }
  function restoreChoices() {
    const sourceSelect = $("backup-restore-source");
    const sourcePrior = sourceSelect.value;
    sourceSelect.replaceChildren(new Option("Choose a copy", ""), ...snapshots().map(item =>
      new Option(`${labelPath(item.source_path)} · ${sourceDeviceLabel(item)} · ${formatDate(item.created_at)}`, item.id)));
    sourceSelect.value = snapshots().some(item => item.id === sourcePrior) ? sourcePrior : "";
    const source = snapshots().find(item => item.id === sourceSelect.value);
    const targetSelect = $("backup-restore-target");
    const targetPrior = targetSelect.value;
    const origin = source?.origin;
    const parts = String(source?.source_path || "").split("/");
    const group = origin?.group || parts[0], game = origin?.game || parts[1];
    const targets = source ? inventory("saves").filter(r => r.selectable &&
      String(r.path || "").split("/")[0] === group && String(r.path || "").split("/")[1] === game) : [];
    targetSelect.replaceChildren(new Option("Choose a save on the connected Switch", ""),
      ...targets.map(item => new Option(labelPath(item.path), item.path)));
    targetSelect.value = targets.some(item => item.path === targetPrior) ? targetPrior : "";
    $("backup-prepare-restore").disabled = !chosenDevice() || !source || !targetSelect.value;
    $("backup-restore-help").textContent = source && !targets.length
      ? "No matching existing save folder is available on this Switch. Scan saves or choose another copy."
      : "The server checks the console, game, save type, and target again before any write.";
  }
  function activityRows() {
    const list = $("backup-job-list");
    const jobs = [...(state.jobs || [])].reverse().slice(0, 20);
    if (!jobs.length) { empty(list, "No backup activity yet."); return; }
    const names = {
      inventory_saves: "Scan saves", inventory_games: "Scan games", create_snapshots: "Copy saves",
      create_archive: "Prepare ZIP", import_archive: "Import ZIP", export_games: "Export games",
      prepare_restore: "Check restore and make safety copy", confirm_restore: "Restore save",
      cancel_restore: "Cancel restore", verify_game_download: "Prepare game download",
      restore_diagnostics: "Check restore support"
    };
    list.replaceChildren(...jobs.map(job => {
      const row = rowShell(job, null, names[job.action] || "Backup task", jobDetail(job));
      const tag = el("span", `backup-state backup-state-${job.state}`, job.state === "ready" ? "Ready" :
        job.state === "running" ? "Working" : job.state === "queued" ? "Waiting" :
        job.state === "cancelled" ? "Cancelled" : "Failed");
      row.append(tag);
      if (job.action === "create_archive" && job.state === "ready" &&
          job.result?.download_url?.startsWith("/api/backups/download/")) {
        const button = el("button", "btn btn-small btn-secondary backup-row-action", "Download ZIP");
        button.type = "button";
        button.addEventListener("click", () => download(job.result.download_url));
        row.append(button);
      }
      if (job.state === "running" || job.state === "queued") {
        if (job.action === "confirm_restore") {
          row.querySelector(".backup-row-main").append(el("div", "backup-row-reason",
            "Restore is in progress. Do not close SwitchAgent or disconnect the Switch; the target may be partially changed if interrupted."));
        } else {
          const cancel = el("button", "btn btn-small btn-secondary backup-row-action", "Cancel");
          cancel.type = "button";
          cancel.addEventListener("click", async () => {
            try { await post(`/api/backups/jobs/${encodeURIComponent(job.id)}/cancel`, {}); notify("Cancellation requested."); }
            catch (error) { notify(error.message, true); }
          });
          row.append(cancel);
        }
        if ((job.action === "inventory_saves" && job.games_total > 0) ||
            (job.items_total != null && job.items_total > 0) || (job.total != null && job.total > 0)) {
          const progress = el("progress", "backup-job-progress");
          progress.max = job.action === "inventory_saves" && job.games_total > 0
            ? job.games_total : (job.items_total || job.total);
          progress.value = Math.min(job.action === "inventory_saves" && job.games_total > 0
            ? job.games_done || 0 : (job.items_total != null ? job.items_done || 0 : job.done || 0), progress.max);
          row.querySelector(".backup-row-main").append(progress);
        }
      }
      return row;
    }));
  }
  function jobDetail(job) {
    if (job.error) return job.error;
    if (job.action === "inventory_saves") {
      const games = job.games_total > 0 ? `${job.games_done || 0} of ${job.games_total} games checked` : "Reading game list";
      if (job.state === "ready") return `${job.items_done || 0} saves found · ${games}`;
      return `${games} · ${job.items_done || 0} saves found` +
        (job.current_game ? ` · ${job.current_game}` : "");
    }
    if (job.action === "create_snapshots") {
      const done = job.items_done || 0, total = job.items_total || 0;
      const bytes = job.total > 0 ? ` · current save ${formatBytes(job.done)} / ${formatBytes(job.total)}` : "";
      return `${done} of ${total} copies saved · ${Math.max(0, total - done)} remaining${bytes}`;
    }
    if (job.state === "queued") return "Waiting to start";
    if (job.state === "ready") return "Finished";
    return job.total == null ? `${formatBytes(job.done)} processed` :
      `${formatBytes(job.done)} / ${formatBytes(job.total)}`;
  }
  function renderOverview() {
    const device = chosenDevice();
    const view = activeView(), localView = view === "backup-tab-local", gamesView = view === "backup-tab-games";
    const jobs = [...(state.jobs || [])].reverse().filter(job => job.device_id === device || !job.device_id);
    const active = jobs.find(job => job.state === "queued" || job.state === "running");
    const latest = active || jobs[0];
    const readyArchive = latest?.action === "create_archive" && latest.state === "ready" &&
      latest.result?.download_url?.startsWith("/api/backups/download/") ? latest : null;
    const lastBackup = jobs.find(job => job.action === "create_snapshots" && job.state === "ready" &&
      Array.isArray(job.result) && job.result.length > 0);
    const rows = inventory("saves");
    const available = rows.filter(row => row.selectable).length;
    const overview = $("backup-overview");
    const title = $("backup-overview-title"), detail = $("backup-overview-detail");
    const bar = $("backup-overview-progress");
    const latestDownload = $("backup-download-latest");
    latestDownload.hidden = !lastBackup && !readyArchive;
    latestDownload.disabled = !!active;
    latestDownload.textContent = readyArchive ? "Download prepared ZIP" : "Download last backup ZIP";
    overview.dataset.state = active ? "busy" : latest?.state === "failed" ? "error" : "idle";
    if (active) {
      const action = {inventory_saves:"Finding saves", create_snapshots:"Backing up saves", create_archive:"Preparing ZIP",
        inventory_games:"Finding game packages", export_games:"Exporting game packages"}[active.action] || "Working";
      title.textContent = active.state === "queued" ? `${action} · waiting` : action;
      detail.textContent = jobDetail(active);
    } else if (latest?.state === "failed") {
      title.textContent = latest.action === "create_snapshots"
        ? `Backup stopped · ${latest.items_done || 0} of ${latest.items_total || 0} saved`
        : "Last task failed";
      detail.textContent = (latest.error || "Open Recent activity for details.") +
        (latest.action === "create_snapshots" && latest.items_done ? " Finished copies remain in Saved copies." : "");
    } else if (latest?.action === "create_archive" && latest.state === "ready") {
      title.textContent = "ZIP ready";
      detail.textContent = "Download it above or from Recent activity. Your local copies remain available below.";
    } else if (localView) {
      const count = snapshots().length;
      title.textContent = count ? `${count} ${count === 1 ? "copy" : "copies"} saved on this computer` : "No local copies yet";
      detail.textContent = count ? "Download copies as a ZIP. No Switch is needed." :
        "Back up saves from a Switch or import a ZIP below.";
    } else if (gamesView) {
      const count = inventory("games").length;
      title.textContent = !device ? "Choose a Switch to scan game packages" :
        count ? `${count} game ${count === 1 ? "package" : "packages"} found` : "Ready to scan game packages";
      detail.textContent = !device ? "Game packages are separate from save backups." :
        count ? "Select packages below to export them." : "Press Scan games to read the connected Switch.";
    } else if (latest?.action === "create_snapshots" && latest.state === "ready") {
      title.textContent = `Backup complete · ${latest.items_done} of ${latest.items_total} copies saved`;
      detail.textContent = "Your copies are on this computer. Download a ZIP to keep them elsewhere.";
    } else if (!device) {
      title.textContent = "Choose a connected Switch";
      detail.textContent = `${snapshots().length} local copies remain available.`;
    } else if (rows.length) {
      title.textContent = `${available} of ${rows.length} ${rows.length === 1 ? "save" : "saves"} ready to back up`;
      detail.textContent = `${snapshots().length} local copies stored on this computer.`;
    } else if (latest?.action === "inventory_saves" && latest.state === "ready") {
      title.textContent = "No saves found on this Switch";
      detail.textContent = "Check DBI and try Find saves again.";
    } else {
      title.textContent = "Ready to find saves";
      detail.textContent = "Press Find saves to read the connected Switch.";
    }
    bar.hidden = !active;
    if (active) {
      const scanning = active.action === "inventory_saves" && active.games_total > 0;
      const total = scanning ? active.games_total : (active.items_total || active.total);
      if (total > 0) { bar.max = total; bar.value = scanning ? active.games_done || 0 :
        (active.items_total != null ? active.items_done || 0 : active.done || 0); }
      else bar.removeAttribute("value");
    }
    $("backup-save-toolbar").hidden = rows.length === 0;
    $("backup-save-actions").hidden = rows.length === 0;
    $("backup-game-toolbar").hidden = inventory("games").length === 0;
    $("backup-game-actions").hidden = inventory("games").length === 0;
    $("backup-scan-saves").disabled = !device || !!active;
    $("backup-scan-games").disabled = !device || !!active;
  }
  function reservationToReview() {
    const entries = Object.entries(state.reservations || {})
      .filter(([, reservation]) => reservation.state === "prepared" && reservation.plan_id);
    if (!entries.length) return null;
    const chosen = entries.find(([deviceId]) => deviceId === chosenDevice());
    const [deviceId, reservation] = chosen || entries[0];
    const job = (state.jobs || []).find(row => row.id === reservation.job_id &&
      row.action === "prepare_restore" && row.state === "ready" &&
      row.result?.id === reservation.plan_id && row.result?.device_id === deviceId);
    return { deviceId, reservation, result: job?.result || null, count: entries.length };
  }
  function renderPreparedPlan() {
    const banner = $("backup-prepared-plan");
    const current = reservationToReview();
    if (!current || plan) { banner.hidden = true; return; }
    const { deviceId, reservation, result, count } = current;
    const device = devices.find(item => item.device_id === deviceId);
    const label = device?.friendly_name || device?.display_name || "the original Switch";
    const target = result?.target_path || `profile ${reservation.target_profile || "unknown"}`;
    $("backup-prepared-summary").textContent = `${label} · ${target} · expires ${formatDate(reservation.expires_at)}` +
      (count > 1 ? ` · ${count} plans ready; choose a Switch to view another` : "") +
      (cancellingPlans.has(reservation.plan_id) ? " · Cancellation requested" : "");
    $("backup-review-plan").disabled = !result || cancellingPlans.has(reservation.plan_id);
    $("backup-discard-plan").disabled = cancellingPlans.has(reservation.plan_id);
    banner.hidden = false;
  }
  function render() { saveRows(); localRows(); gameRows(); updateSelection(); activityRows(); renderPreparedPlan(); renderDeviceStatus(); renderOverview(); }
  function download(url) {
    const anchor = el("a");
    anchor.href = url;
    anchor.download = "";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  }
  function handleReadyJobs() {
    for (const job of state.jobs || []) {
      if (!seenJobs.has(job.id)) {
        seenJobs.add(job.id);
        // A prior session's finished job is history, not a fresh download
        // or a restore plan that should suddenly open a confirmation modal.
        if (["ready", "failed", "cancelled"].includes(job.state)) downloads.add(job.id);
        continue;
      }
      if (!(["ready", "failed", "cancelled"].includes(job.state))) continue;
      if (downloads.has(job.id)) continue;
      downloads.add(job.id);
      if (job.state === "failed") { notify(job.error || "Backup task failed.", true); continue; }
      if (job.state === "cancelled") { notify("Backup task cancelled."); continue; }
      if (job.action === "create_archive" || job.action === "verify_game_download") {
        if (job.result?.download_url?.startsWith("/api/backups/download/")) download(job.result.download_url);
      }
      if (job.action === "prepare_restore") showPlan(job.result);
      else if (job.action === "confirm_restore") notify("Restore transfer finished. Check the save in the game before relying on it.");
      else if (job.action === "create_snapshots") notify("Local copies are ready. Download your ZIP above or from Saved copies.");
      else if (job.action === "import_archive") notify("ZIP imported. Review its origin before restoring.");
      else if (job.action === "export_games") notify("Game exports are ready for download.");
    }
  }
  async function refreshState() {
    if (polling) return;
    polling = true;
    try {
      state = await api("/api/backups/state");
      const signature = JSON.stringify({inventory: state.inventory, snapshots: state.catalog?.snapshots?.map(row => row.id)});
      if (signature !== coverSignature) {
        coverSignature = signature;
        void post("/api/covers/refresh", {}).catch(() => {});
      }
      try {
        const covers = await api("/api/covers/status");
        readyCovers = new Set(covers.enabled ? covers.ready || [] : []);
      } catch (_) { /* Backup actions remain available without artwork. */ }
      const reservedIds = new Set(Object.values(state.reservations || {}).map(item => item.plan_id));
      for (const id of cancellingPlans) if (!reservedIds.has(id)) cancellingPlans.delete(id);
      handleReadyJobs();
      render();
    } catch (error) { notify(error.message, true); }
    finally { polling = false; }
  }
  async function refreshDevices() {
    try {
      devices = await api("/api/devices");
      const select = $("backup-device");
      const prior = select.value;
      const connected = devices.filter(d => d.connected);
      select.replaceChildren(new Option("Choose a connected Switch", ""), ...connected.map(d =>
        new Option(d.friendly_name || d.display_name || d.device_id, d.device_id)));
      select.value = connected.some(d => d.device_id === prior) ? prior : (connected.length === 1 ? connected[0].device_id : "");
      if (select.value !== prior) {
        if (plan) $("backup-confirm-dialog").close();
        selected.saves.clear(); selected.games.clear(); render();
      } else renderDeviceStatus();
    } catch (error) { notify(error.message, true); }
  }
  async function submit(url, body) {
    const result = await post(url, body);
    if (result.job_id) { seenJobs.add(result.job_id); notify("Task queued."); await refreshState(); }
    return result;
  }
  function showPlan(result) {
    if (!result?.id) { notify("Restore plan is unavailable.", true); return; }
    plan = result;
    const profile = result.target_profile || String(result.target_path || "").split("/")[2] || "unknown";
    $("backup-confirm-summary").textContent = `Target: ${result.target_path}. A safety copy was created (${result.prebackup_id}). ` +
      (result.expires_at ? `This plan expires at ${formatDate(result.expires_at)}. ` : "") +
      (result.cross_profile ? `This writes into a different profile: ${profile}.` : "This writes into the same profile.");
    $("backup-profile-confirm-wrap").hidden = !result.requires_profile_confirmation;
    $("backup-profile-confirm").value = "";
    if (!$("backup-confirm-dialog").open) $("backup-confirm-dialog").showModal();
  }
  async function requestCancelPlan(deviceId, planId) {
    if (cancellingPlans.has(planId)) return;
    cancellingPlans.add(planId);
    renderPreparedPlan();
    try { await submit("/api/backups/restores/cancel", { device_id: deviceId, plan_id: planId }); }
    catch (error) { cancellingPlans.delete(planId); notify(`Could not cancel restore plan: ${error.message}`, true); renderPreparedPlan(); }
  }
  async function cancelPlan() {
    if (!plan) return;
    const held = plan;
    plan = null;
    await requestCancelPlan(held.device_id, held.id);
  }

  // Tabs preserve the selected device and selected rows.
  const tabs = [...root.querySelectorAll('[role="tab"]')];
  function activateTab(tab) {
    for (const candidate of tabs) {
      const active = candidate === tab;
      candidate.setAttribute("aria-selected", String(active));
      candidate.tabIndex = active ? 0 : -1;
      $(candidate.getAttribute("aria-controls")).hidden = !active;
    }
    renderDeviceStatus();
    renderOverview();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
        (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      activateTab(tabs[next]); tabs[next].focus();
    });
  });
  $("backup-device").addEventListener("change", () => {
    if (plan) $("backup-confirm-dialog").close();
    selected.saves.clear(); selected.games.clear(); render();
  });
  $("backup-review-plan").addEventListener("click", () => {
    const current = reservationToReview();
    if (!current?.result) { notify("Restore plan details are unavailable. Cancel this plan and prepare again.", true); return; }
    if (!devices.some(device => device.device_id === current.deviceId && device.connected)) {
      notify("Reconnect the original Switch or cancel this restore plan.", true);
      return;
    }
    if (chosenDevice() !== current.deviceId) {
      $("backup-device").value = current.deviceId;
      selected.saves.clear(); selected.games.clear();
    }
    showPlan(current.result);
    render();
  });
  $("backup-discard-plan").addEventListener("click", () => {
    const current = reservationToReview();
    if (current) void requestCancelPlan(current.deviceId, current.reservation.plan_id);
  });
  $("backup-refresh").addEventListener("click", async () => { await refreshDevices(); await refreshState(); });
  $("backup-profile-filter").addEventListener("change", () => { saveRows(); updateSelection(); });
  $("backup-save-search").addEventListener("input", () => { saveRows(); updateSelection(); });
  $("backup-uncopied-only").addEventListener("change", () => { saveRows(); updateSelection(); });
  $("backup-local-search").addEventListener("input", () => { localRows(); updateSelection(); });
  $("backup-restore-source").addEventListener("change", restoreChoices);
  $("backup-restore-target").addEventListener("change", restoreChoices);
  for (const kind of ["saves", "games"]) {
    $(kind === "saves" ? "backup-scan-saves" : "backup-scan-games").addEventListener("click", async () => {
      if (!chosenDevice()) { notify("Choose a connected Switch first.", true); return; }
      try { await submit("/api/backups/inventory", { device_id: chosenDevice(), kind }); }
      catch (error) { notify(error.message, true); }
    });
    $(kind === "saves" ? "backup-clear-saves" : "backup-clear-games").addEventListener("click", () => clearSelection(kind));
    $(kind === "saves" ? "backup-select-all-saves" : "backup-select-all-games").addEventListener("click", () => {
      const rows = kind === "saves" ? visibleSaveRows.filter(r => r.selectable) : inventory("games");
      for (const item of rows) {
        selected[kind].add(item.path);
      }
      render();
    });
  }
  $("backup-copy-saves").addEventListener("click", async () => {
    try { await submit("/api/backups/snapshots", { device_id: chosenDevice(), paths: [...selected.saves] }); }
    catch (error) { notify(error.message, true); }
  });
  $("backup-export-games").addEventListener("click", async () => {
    try { await submit("/api/backups/games", { device_id: chosenDevice(), paths: [...selected.games] }); }
    catch (error) { notify(error.message, true); }
  });
  $("backup-select-all-local").addEventListener("click", () => { visibleLocalRows.forEach(item => selected.local.add(item.id)); render(); });
  $("backup-clear-local").addEventListener("click", () => clearSelection("local"));
  $("backup-download-all").addEventListener("click", async () => {
    try { await submit("/api/backups/archives", { snapshot_ids: snapshots().map(item => item.id) }); }
    catch (error) { notify(error.message, true); }
  });
  $("backup-download-selected").addEventListener("click", async () => {
    try { await submit("/api/backups/archives", { snapshot_ids: [...selected.local] }); }
    catch (error) { notify(error.message, true); }
  });
  $("backup-download-latest").addEventListener("click", async () => {
    const recent = [...(state.jobs || [])].reverse().find(job => job.device_id === chosenDevice() || !job.device_id);
    if (recent?.action === "create_archive" && recent.state === "ready" &&
        recent.result?.download_url?.startsWith("/api/backups/download/")) {
      download(recent.result.download_url);
      return;
    }
    const latest = [...(state.jobs || [])].reverse().find(job => job.action === "create_snapshots" &&
      job.device_id === chosenDevice() && job.state === "ready" && Array.isArray(job.result) && job.result.length);
    if (!latest) return;
    try { await submit("/api/backups/archives", { snapshot_ids: latest.result.map(item => item.id) }); }
    catch (error) { notify(error.message, true); }
  });
  $("backup-upload-form").addEventListener("submit", async event => {
    event.preventDefault();
    const file = $("backup-upload-file").files[0];
    if (!file) return;
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    const cancel = $("backup-upload-cancel");
    const status = $("backup-upload-status");
    cancel.hidden = false;
    status.textContent = "Uploading ZIP…";
    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        uploadRequest = xhr;
        xhr.open("POST", "/api/backups/upload");
        xhr.setRequestHeader("Content-Type", "application/zip");
        xhr.upload.onprogress = progress => {
          status.textContent = progress.lengthComputable
            ? `Uploading ZIP: ${formatBytes(progress.loaded)} / ${formatBytes(progress.total)}`
            : `Uploading ZIP: ${formatBytes(progress.loaded)} · total unknown`;
        };
        xhr.onerror = () => reject(new Error("ZIP upload failed."));
        xhr.onabort = () => reject(new Error("ZIP upload cancelled."));
        xhr.onload = () => {
          let data = {};
          try { data = JSON.parse(xhr.responseText); } catch (_) { /* Keep HTTP status. */ }
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error(typeof data.detail === "string" ? data.detail : `ZIP upload failed (${xhr.status}).`));
        };
        xhr.send(file);
      });
      seenJobs.add(result.job_id);
      status.textContent = "Upload complete. Import queued.";
      notify("ZIP import queued.");
      $("backup-upload-file").value = "";
      await refreshState();
    } catch (error) { notify(error.message, true); }
    finally { uploadRequest = null; button.disabled = false; cancel.hidden = true; }
  });
  $("backup-upload-cancel").addEventListener("click", () => { uploadRequest?.abort(); });
  $("backup-prepare-restore").addEventListener("click", async () => {
    const source = $("backup-restore-source").value, target = $("backup-restore-target").value;
    if (!chosenDevice() || !source || !target) return;
    try { await submit("/api/backups/restores/prepare", { device_id: chosenDevice(), snapshot_id: source, target_path: target }); }
    catch (error) { notify(error.message, true); }
  });
  $("backup-confirm-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!plan) return;
    const typed = $("backup-profile-confirm").value;
    if (plan.requires_profile_confirmation && typed !== plan.target_profile) {
      notify("Type the target profile name exactly to continue.", true);
      return;
    }
    const held = plan;
    const button = $("backup-confirm-write");
    button.disabled = true;
    try {
      await submit("/api/backups/restores/confirm", {
        device_id: held.device_id, plan_id: held.id,
        confirm_profile: held.requires_profile_confirmation ? typed : null
      });
      plan = null;
      $("backup-confirm-dialog").close();
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
  });
  $("backup-cancel-plan").addEventListener("click", () => { $("backup-confirm-dialog").close(); });
  $("backup-confirm-dialog").addEventListener("close", () => { if (plan) void cancelPlan(); });
  void (async () => { await refreshDevices(); await refreshState(); setInterval(refreshState, 3000); setInterval(refreshDevices, 15000); })();
})();
