// Library page: multi-select + sticky confirmation bar + install
// confirmation modal + game detail modal + non-blocking rescan.
//
// Hard rule this file exists to enforce client-side (mirrored server-side
// in switchagent/web/services.py -- this is UX, not the real guard):
// selecting a card NEVER creates a job. A job is only ever created after
// the user explicitly presses "Confirm Install" in the modal below.
(function () {
  "use strict";

  const selected = new Map(); // id -> {name, size, destination}

  const selectionBar = document.getElementById("selection-bar");
  const selectionCount = document.getElementById("selection-count");
  const selectionSize = document.getElementById("selection-size");
  const targetSelect = document.getElementById("target-device");
  const installBtn = document.getElementById("install-selected-btn");

  function updateSelectionBar() {
    const n = selected.size;
    selectionCount.textContent = String(n);
    let totalSize = 0;
    selected.forEach((v) => { totalSize += v.size; });
    selectionSize.textContent = formatBytes(totalSize);
    selectionBar.hidden = n === 0;
  }
  updateSelectionBar();

  // Auto-select the target Switch when exactly one is currently connected
  // (point: never guess when 2+ are connected, never invent one when 0
  // are). Runs once, at page load -- if the user later explicitly picks a
  // (or a different) device, nothing here ever overrides that choice
  // again; and if their chosen device disconnects, the target is never
  // silently switched to another one (see the disabled `option` for a
  // disconnected device above -- the browser just leaves the select on
  // its already-chosen value, which becomes a proper validation error at
  // confirm time instead of a silent redirect).
  if (targetSelect && targetSelect.dataset.connectedCount === "1") {
    const onlyOption = Array.from(targetSelect.options).find((o) => o.value && !o.disabled);
    if (onlyOption) targetSelect.value = onlyOption.value;
  }

  function bindSelection() {
  document.querySelectorAll(".select-box:not([data-selection-bound])").forEach((box) => {
    box.dataset.selectionBound = 'true';
    box.addEventListener("change", () => {
      const id = box.value;
      if (box.checked) {
        selected.set(id, {
          name: box.dataset.name,
          size: parseInt(box.dataset.size, 10) || 0,
          destination: box.dataset.destination || "SD Card install",
        });
      } else {
        selected.delete(id);
      }
      updateSelectionBar();
    });
  });

  // Selecting a game's Base Game checkbox auto-selects every related
  // Update/DLC/Mod (same TITLE_ID family, see data-family) -- the default
  // "select a game" action is "select the whole installable set". The user
  // can still uncheck individual siblings afterward; unchecking a sibling
  // never affects the base or its other siblings. Unchecking the base
  // itself mirrors the selection back off, so nothing is left selected
  // "invisibly" once the game card itself looks unselected. Never reaches
  // into a DIFFERENT TITLE_ID family (data-family scopes the query).
  document.querySelectorAll('.select-box[data-role="base"]:not([data-family-bound])').forEach((baseBox) => {
    baseBox.dataset.familyBound = 'true';
    baseBox.addEventListener("change", () => {
      const family = baseBox.dataset.family;
      if (!family) return;
      document.querySelectorAll(`.select-box[data-family="${CSS.escape(family)}"]`).forEach((box) => {
        if (box === baseBox || box.disabled || box.checked === baseBox.checked) return;
        box.checked = baseBox.checked;
        box.dispatchEvent(new Event("change"));
      });
    });
  });

  }
  bindSelection();
  document.addEventListener('library-updated', bindSelection);
  // -- game detail modal --------------------------------------------------

  const detailModal = document.getElementById("detail-modal");
  const detailBody = document.getElementById("detail-modal-body");

  function bindDetails() {
  document.querySelectorAll(".card-body[data-detail-url]:not([data-detail-bound])").forEach((el) => {
    el.dataset.detailBound = 'true';
    el.addEventListener("click", async () => {
      const url = el.dataset.detailUrl;
      detailBody.innerHTML = "<p>Loading…</p>";
      detailModal.showModal();
      try {
        const res = await fetch(url);
        const item = await res.json();
        detailBody.innerHTML = renderDetail(item);
      } catch (e) {
        detailBody.innerHTML = "<p>Could not load details.</p>";
      }
    });
  });

  }
  bindDetails();
  document.addEventListener('library-updated', bindDetails);

  function renderDetail(item) {
    const rows = [
      ["Name", item.name],
      ["TITLE_ID", item.title_id || "unknown"],
      ["Format", item.package_format || item.content_type || item.file_type],
      ["Size", formatBytes(item.size)],
      ["SHA-256", item.content_hash || "—"],
      ["Source path", item.absolute_path],
      ["Content type", item.content_type || "—"],
      ["Status", item.status.replace(/_/g, " ")],
    ];
    if (item.job) {
      rows.push(["Last job", "#" + item.job.id + " — " + item.job.status.replace(/_/g, " ")]);
      rows.push(["Last target", item.job.target_device_label]);
      if (item.job.error) rows.push(["Last error", item.job.error]);
    }
    let html = "<h3>" + escapeHtml(item.name) + "</h3><table class='detail-table'>";
    for (const [label, value] of rows) {
      html += "<tr><th>" + escapeHtml(label) + "</th><td>" + escapeHtml(String(value)) + "</td></tr>";
    }
    html += "</table>";
    return html;
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  // -- install confirmation modal -----------------------------------------

  const confirmModal = document.getElementById("confirm-modal");
  const confirmCount = document.getElementById("confirm-count");
  const confirmTarget = document.getElementById("confirm-target");
  const confirmDestination = document.getElementById("confirm-destination");
  const confirmSize = document.getElementById("confirm-size");
  const confirmList = document.getElementById("confirm-list");
  const confirmResult = document.getElementById("confirm-result");
  const confirmCancelBtn = document.getElementById("confirm-cancel-btn");
  const confirmInstallBtn = document.getElementById("confirm-install-btn");

  function renderConfirmList() {
    confirmCount.textContent = String(selected.size);
    let totalSize = 0;
    const destinations = new Set();
    confirmList.innerHTML = "";
    selected.forEach((v, id) => {
      totalSize += v.size;
      destinations.add(v.destination);
      const li = document.createElement("li");
      li.dataset.id = id;
      const nameSpan = document.createElement("span");
      nameSpan.textContent = v.name + " — " + v.destination;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "confirm-item-remove";
      removeBtn.setAttribute("aria-label", "Remove " + v.name);
      removeBtn.textContent = "✕";
      li.appendChild(nameSpan);
      li.appendChild(removeBtn);
      confirmList.appendChild(li);
    });
    confirmSize.textContent = formatBytes(totalSize);
    // A batch can legitimately mix destinations (a game install + an
    // atmosphere mod merge) -- never claim a single destination that
    // isn't true for every item; the per-item list above always has the
    // exact answer regardless.
    confirmDestination.textContent = destinations.size === 1
      ? Array.from(destinations)[0]
      : destinations.size + " different (see list below)";
    confirmInstallBtn.disabled = selected.size === 0;
    if (selected.size === 0) confirmModal.close();
  }

  // Lets the user drop an individual Update/DLC/Mod (or anything else)
  // right at the confirmation step, not just before opening the modal --
  // unchecks the matching card checkbox too, so the two stay in sync if
  // the user cancels and looks at the page again.
  confirmList.addEventListener("click", (evt) => {
    const btn = evt.target.closest(".confirm-item-remove");
    if (!btn) return;
    const id = btn.closest("li").dataset.id;
    selected.delete(id);
    const box = document.querySelector(`.select-box[value="${CSS.escape(id)}"]`);
    if (box) box.checked = false;
    updateSelectionBar();
    renderConfirmList();
  });

  installBtn.addEventListener("click", () => {
    if (selected.size === 0) return;
    if (!targetSelect.value) {
      alert("Choose a target Switch first.");
      return;
    }
    confirmTarget.textContent = targetSelect.options[targetSelect.selectedIndex].textContent.trim();
    renderConfirmList();
    confirmResult.hidden = true;
    confirmResult.textContent = "";
    confirmModal.showModal();
  });

  confirmCancelBtn.addEventListener("click", () => confirmModal.close());

  confirmInstallBtn.addEventListener("click", async () => {
    if (selected.size === 0) return;
    const originalBtnText = confirmInstallBtn.textContent;
    confirmInstallBtn.disabled = true;
    confirmInstallBtn.textContent = "Preparing…";
    // Queue owns preparation progress; this request only accepts the batch.
    confirmResult.hidden = false;
    confirmResult.textContent =
      "Preparing " + (selected.size === 1 ? "installation" : "installations") +
      "… extracting archives if needed — large files can take a while, please wait.";
    const body = {
      library_item_ids: Array.from(selected.keys()).map((id) => parseInt(id, 10)),
      target_device_id: targetSelect.value,
    };
    try {
      const res = await fetch("/api/preparations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (res.ok && data.preparation_id) {
        window.location.href = "/queue";
        return;
      }
      confirmResult.hidden = false;
      if (data.created && data.created.length) {
        const n = data.created.length;
        confirmResult.textContent =
          n + (n === 1 ? " installation" : " installations") + " added to queue." +
          (data.errors && data.errors.length ? " " + data.errors.length + " failed -- see Library for details." : "");
        // Every created job already exists as its own persisted row the
        // instant this response comes back (services.create_and_confirm_jobs
        // creates+confirms each one before returning) -- the worker thread
        // hasn't necessarily started any of them yet. A fast job can still
        // finish (and leave Queue for History) before this redirect lands,
        // which is correct, not lost -- see Queue's own live view/History.
        setTimeout(() => { window.location.href = "/queue"; }, 1500);
      } else {
        confirmResult.textContent = "Nothing was queued: " + (data.errors || []).map((e) => e.error).join("; ");
        confirmInstallBtn.disabled = false;
        confirmInstallBtn.textContent = originalBtnText;
      }
    } catch (e) {
      confirmResult.hidden = false;
      confirmResult.textContent = "Request failed: " + e;
      confirmInstallBtn.disabled = false;
      confirmInstallBtn.textContent = originalBtnText;
    }
  });

  // -- rescan (non-blocking, point 16) -------------------------------------
  //
  // W3-007: more than one trigger can exist on the page now (the toolbar's
  // own #rescan-btn, plus an onboarding banner's own Rescan button when the
  // library is empty -- see library.html/onboarding.py) -- every element
  // with the shared .rescan-trigger class is wired up identically, all
  // sharing the single #scan-status readout below.

  const rescanBtns = document.querySelectorAll(".rescan-trigger");
  const scanStatus = document.getElementById("scan-status");

  function setRescanButtonsDisabled(disabled) {
    rescanBtns.forEach((btn) => { btn.disabled = disabled; });
  }

  rescanBtns.forEach((btn) => {
    btn.addEventListener("click", async () => {
      setRescanButtonsDisabled(true);
      await fetch(btn.dataset.scanUrl, { method: "POST" });
      pollScanStatus(btn.dataset.statusUrl);
    });
  });

  async function pollScanStatus(statusUrl) {
    scanStatus.hidden = false;
    try {
      const res = await fetch(statusUrl);
      const state = await res.json();
      if (state.running) {
        const elapsed = state.elapsed_seconds != null ? ` (${Math.round(state.elapsed_seconds)}s)` : "";
        scanStatus.textContent = state.current_filename
          ? `Scanning… ${state.current_filename}${elapsed}`
          : `Scanning…${elapsed}`;
        setTimeout(() => pollScanStatus(statusUrl), 1000);
      } else {
        setRescanButtonsDisabled(false);
        if (state.error) {
          scanStatus.textContent = "Scan error: " + state.error;
        } else if (state.summary) {
          const s = state.summary;
          scanStatus.textContent =
            "Scan complete: " + s.new + " new, " + s.updated + " updated, " + s.removed + " removed.";
          setTimeout(() => window.location.reload(), 1000);
        }
      }
    } catch (e) {
      setRescanButtonsDisabled(false);
      scanStatus.textContent = "Could not check scan status.";
    }
  }
})();
