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
    return new Date(Number(seconds) * 1000).toLocaleString();
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
  function inventory(kind) { return state.inventory?.[chosenDevice()]?.[kind] || []; }
  function snapshots() { return state.catalog?.snapshots || []; }
  function gameExports() { return state.catalog?.games || []; }
  function clearSelection(kind) { selected[kind].clear(); render(); }
  function titleId(row) {
    const id = row?.identity?.title_id || row?.title_id;
    return /^[0-9a-f]{16}$/i.test(String(id || "")) ? String(id).toUpperCase() : null;
  }
  function cover(row) {
    const id = titleId(row);
    if (!id) return null;
    const image = el("img", "backup-cover");
    image.alt = "";
    image.loading = "lazy";
    image.onerror = () => { image.hidden = true; };
    image.src = `/api/covers/${encodeURIComponent(id)}`;
    return image;
  }
  function empty(list, text) { list.replaceChildren(el("p", "empty-state", text)); }

  function rowShell(item, kind, title, meta, canSelect = true) {
    const row = el("div", "backup-row");
    if (kind) {
      const input = el("input");
      input.type = "checkbox";
      input.disabled = !canSelect;
      input.checked = canSelect && selected[kind].has(kind === "local" ? item.id : item.path);
      input.setAttribute("aria-label", `Select ${title}`);
      input.addEventListener("change", () => {
        const key = kind === "local" ? item.id : item.path;
        if (input.checked) selected[kind].add(key); else selected[kind].delete(key);
        updateSelection();
      });
      row.append(input);
    }
    const image = cover(item);
    if (image) row.append(image);
    const main = el("div", "backup-row-main");
    main.append(el("div", "backup-row-title", title), el("div", "backup-row-meta", meta));
    if (item.reason) main.append(el("div", "backup-row-reason", item.reason));
    row.append(main);
    return row;
  }
  function saveRows() {
    const rows = inventory("saves");
    const device = devices.find(item => item.device_id === chosenDevice());
    const filter = $("backup-profile-filter");
    const prior = filter.value;
    const names = [...new Set(rows.filter(r => String(r.path || "").split("/").length === 3)
      .map(r => String(r.path).split("/")[2]))].sort((a, b) => a.localeCompare(b));
    filter.replaceChildren(new Option("All profiles", ""), ...names.map(name => new Option(name, name)));
    filter.value = names.includes(prior) ? prior : "";
    const shown = rows.filter(r => !filter.value || String(r.path || "").split("/")[2] === filter.value);
    const list = $("backup-save-list");
    if (!shown.length) { empty(list, rows.length ? "No saves for this profile." : "No saves found. Scan a connected Switch."); return; }
    list.replaceChildren(...shown.map(item => {
      const parts = String(item.path || "").split("/");
      const title = parts.length === 3 ? parts[1] : (item.name || item.path);
      const profile = parts.length === 3 ? parts[2] : "Profile unknown";
      const count = item.file_count == null ? "Files not counted" : `${item.file_count} files`;
      const type = item.identity?.save_type || "Type unknown";
      const lastCopy = snapshots().find(copy => copy.source_path === item.path &&
        copy.origin?.device_fingerprint === device?.device_fingerprint);
      const copyLabel = lastCopy ? ` · Last copy ${formatDate(lastCopy.created_at)}` : " · No local copy";
      return rowShell(item, "saves", title, `${profile} · ${parts[0] || "Saves"} · ${type} · ${formatBytes(item.size)} · ${count}${copyLabel}`, item.selectable === true);
    }));
  }
  function localRows() {
    const rows = snapshots();
    const list = $("backup-local-list");
    if (!rows.length) { empty(list, "No local copies yet. Select saves to copy or import a ZIP."); return; }
    list.replaceChildren(...rows.map(item => {
      const source = item.source_path || "";
      const size = Array.isArray(item.files) ? item.files.reduce((sum, file) => sum + Number(file.size || 0), 0) : null;
      const origin = item.origin?.device_fingerprint ? `Device ${item.origin.device_fingerprint}` : "Origin unknown";
      const meta = `${formatDate(item.created_at)} · ${formatBytes(size)} · ${item.files?.length ?? "Unknown"} files · ${origin}`;
      return rowShell(item, "local", labelPath(source), meta);
    }));
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
    $("backup-copy-saves").disabled = saves.length === 0 || !chosenDevice();
    $("backup-export-games").disabled = games.length === 0 || !chosenDevice();
    const availableLocal = new Set(snapshots().map(item => item.id));
    for (const id of selected.local) if (!availableLocal.has(id)) selected.local.delete(id);
    $("backup-download-selected").disabled = selected.local.size === 0;
    restoreChoices();
  }
  function restoreChoices() {
    const sourceSelect = $("backup-restore-source");
    const sourcePrior = sourceSelect.value;
    sourceSelect.replaceChildren(new Option("Choose a copy", ""), ...snapshots().map(item => new Option(labelPath(item.source_path) + " · " + formatDate(item.created_at), item.id)));
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
      const row = rowShell(job, null, names[job.action] || "Backup task", job.error ||
        (job.total == null ? `${formatBytes(job.done)} processed · total unknown` : `${formatBytes(job.done)} / ${formatBytes(job.total)}`));
      const tag = el("span", `backup-state backup-state-${job.state}`, job.state === "ready" ? "Ready" :
        job.state === "running" ? "Working" : job.state === "queued" ? "Waiting" :
        job.state === "cancelled" ? "Cancelled" : "Failed");
      row.append(tag);
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
        if (job.total != null && job.total > 0) {
          const progress = el("progress", "backup-job-progress");
          progress.max = job.total; progress.value = Math.min(job.done || 0, job.total);
          row.querySelector(".backup-row-main").append(progress);
        }
      }
      return row;
    }));
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
  function render() { saveRows(); localRows(); gameRows(); updateSelection(); activityRows(); renderPreparedPlan(); }
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
      else if (job.action === "create_snapshots") notify("Local copies are ready. Open Local copies to download them.");
      else if (job.action === "import_archive") notify("ZIP imported. Review its origin before restoring.");
      else if (job.action === "export_games") notify("Game exports are ready for download.");
    }
  }
  async function refreshState() {
    if (polling) return;
    polling = true;
    try {
      state = await api("/api/backups/state");
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
      $("backup-device-status").textContent = connected.length === 0 ? "No Switch connected" :
        connected.length > 1 && !select.value ? "Choose a Switch to avoid mixing devices" : "Connected";
      if (select.value !== prior) {
        if (plan) $("backup-confirm-dialog").close();
        selected.saves.clear(); selected.games.clear(); render();
      }
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
    $("backup-device-status").textContent = chosenDevice() ? "Connected" :
      devices.some(device => device.connected) ? "Choose a Switch to avoid mixing devices" : "No Switch connected";
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
      const rows = inventory(kind).filter(r => kind !== "saves" || r.selectable);
      for (const item of rows) {
        if (kind === "saves" && $("backup-profile-filter").value && String(item.path).split("/")[2] !== $("backup-profile-filter").value) continue;
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
  $("backup-select-all-local").addEventListener("click", () => { snapshots().forEach(item => selected.local.add(item.id)); render(); });
  $("backup-clear-local").addEventListener("click", () => clearSelection("local"));
  $("backup-download-selected").addEventListener("click", async () => {
    try { await submit("/api/backups/archives", { snapshot_ids: [...selected.local] }); }
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
