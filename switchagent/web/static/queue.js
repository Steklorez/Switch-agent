// Queue page: retry/cancel buttons + worker pause/resume + live polling.
//
// Previously this only reloaded the whole page every 6s, and only if an
// "active-looking" status was present in the page AT LOAD TIME. For a
// short job (a small file can go CONFIRMED -> RUNNING -> DONE_UNVERIFIED
// in well under 15s total -- confirmed by a real install,
// docs/REAL-HARDWARE-TEST-*.md) that meant the page was a stale snapshot
// for up to 6s at a stretch, and the terminal state was never shown at
// all -- DONE_UNVERIFIED/DONE jobs are deliberately excluded from
// /api/queue (that's History's job), so a fast job could go from
// "just appeared" to "gone" between two reloads with nothing in between.
//
// Fix: genuine periodic polling of /api/queue (in-place DOM patch, no
// full reload) plus a toast when a job that WAS in the active list
// disappears -- looked up via /api/jobs/{id} so the toast shows its real
// terminal outcome, not a guess.
(function () {
  "use strict";

  const jobList = document.getElementById("job-list");
  const emptyState = document.getElementById("empty-state");
  const toastContainer = document.getElementById("toast-container");
  if (!jobList) return; // not on the Queue page

  const POLL_INTERVAL_MS = 2000;
  // Keep in sync with queue.html's retry-button condition and
  // services._RETRYABLE_JOB_STATUSES (retry creates a brand-new job --
  // see services.retry_job docstring -- so this list is "terminal states
  // a user can reasonably re-attempt from", not "resumable states").
  const RETRYABLE_STATUSES = [
    "FAILED", "INTERRUPTED", "DEVICE_UNAVAILABLE",
    "SOURCE_CHANGED", "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY",
  ];
  let knownActiveIds = new Set(
    Array.from(jobList.querySelectorAll(".job-row")).map((el) => el.dataset.jobId)
  );

  // In-theme replacement for window.confirm() -- the browser's own native
  // dialog looks like an unrelated system/security prompt (unstyled white
  // box, generic OS chrome) sitting on top of this app's dark UI, jarring
  // enough to read as suspicious rather than as part of the app. Resolves
  // true/false exactly like a confirm() call would, so the two call sites
  // below only needed `await` added in front of them.
  const actionConfirmModal = document.getElementById("confirm-action-modal");
  const actionConfirmMessage = document.getElementById("confirm-action-message");
  const actionConfirmOkBtn = document.getElementById("confirm-action-ok-btn");
  const actionConfirmCancelBtn = document.getElementById("confirm-action-cancel-btn");
  function confirmAction(message) {
    if (!actionConfirmModal) return Promise.resolve(window.confirm(message)); // pages without the dialog markup
    return new Promise((resolve) => {
      actionConfirmMessage.textContent = message;
      const finish = (result) => {
        actionConfirmOkBtn.removeEventListener("click", onOk);
        actionConfirmCancelBtn.removeEventListener("click", onCancel);
        actionConfirmModal.removeEventListener("cancel", onCancel);
        actionConfirmModal.close();
        resolve(result);
      };
      const onOk = () => finish(true);
      const onCancel = () => finish(false);
      actionConfirmOkBtn.addEventListener("click", onOk);
      actionConfirmCancelBtn.addEventListener("click", onCancel);
      actionConfirmModal.addEventListener("cancel", onCancel); // Esc key
      actionConfirmModal.showModal();
    });
  }

  function statusClass(status) {
    return "status-" + status.toLowerCase();
  }

  // RUNNING only ever means "actively sending file bytes over MTP" (see
  // queue_worker._run_job_transfer) -- it never means DBI has started
  // installing, and this app has no MTP-visible way to observe that
  // separately (see docs/STAGE5B-REAL-MTP.md). SD_INSTALL is DBI's own
  // install destination though, so bytes arriving there really is
  // "Installing" from the user's point of view; SD_CARD (mods) is a
  // plain file copy, never run through DBI's installer at all -- so the
  // two get distinct, honest labels instead of one opaque "RUNNING".
  function statusLabel(j) {
    if (j.status === "RUNNING") {
      return j.target_storage === "SD_INSTALL" ? "Installing" : "Copying to device";
    }
    return j.status.replace(/_/g, " ");
  }

  function renderGroup(g) {
    const li = document.createElement("li");
    li.className = "batch-group";
    li.dataset.batchId = g.batch_id === null || g.batch_id === undefined ? "" : g.batch_id;

    let headerHtml = "";
    if (g.total > 1) {
      const attentionHtml = g.conflict_count > 0 ? `<span class="batch-attention"></span>` : "";
      headerHtml = `
        <div class="batch-header">
          <span class="batch-title"></span>
          <span class="batch-progress"></span>
          ${attentionHtml}
        </div>`;
    }
    li.innerHTML = `${headerHtml}<ol class="batch-jobs"></ol>`;
    if (g.total > 1) {
      li.querySelector(".batch-title").textContent = g.display_name;
      li.querySelector(".batch-progress").textContent = `${g.finished} / ${g.total} finished`;
      if (g.conflict_count > 0) {
        li.querySelector(".batch-attention").textContent =
          `${g.conflict_count} item${g.conflict_count !== 1 ? "s" : ""} requires attention`;
      }
    }
    const jobsList = li.querySelector(".batch-jobs");
    for (const j of g.jobs) jobsList.appendChild(renderJobRow(j));
    return li;
  }

  function renderJobRow(j) {
    const li = document.createElement("li");
    li.className = "job-row";
    li.dataset.status = j.status;
    const done = ["DONE", "DONE_UNVERIFIED"].includes(j.status);
    const fraction = j.target_storage === "SD_CARD" && j.bytes_total > 0 ? Math.min(100, 100 * j.bytes_done / j.bytes_total) : 0;
    li.style.setProperty("--progress", `${done ? 100 : fraction}%`);
    li.classList.toggle("progress-active", j.status === "RUNNING");
    li.dataset.jobId = j.id;

    const spinner = j.status === "RUNNING" ? '<span class="spinner" aria-label="in progress"></span>' : "";
    // A plain spinner with no numbers reads as "frozen" for a real
    // multi-minute, many-small-files mod transfer (each file has real
    // fixed per-file MTP overhead -- see queue_worker.py's own timing
    // notes) -- this makes the already-tracked bytes_done/bytes_total
    // visible instead of just a 13%-opacity background tint. SD_INSTALL
    // deliberately excluded: its bytes are diagnostic-only and can read 0
    // even on a real, physically confirmed success (see
    // _run_job_transfer's own UNVERIFIED-handling comment) -- showing a
    // percentage there would be actively misleading, not just quiet.
    const progressTextHtml = (j.target_storage === "SD_CARD" && j.status === "RUNNING" && j.bytes_total > 0)
      ? `<div class="job-progress-text"></div>` : "";
    const waitingForDevice = j.status === "WAITING_FOR_DEVICE";
    const isConflict = j.status === "DESTINATION_CONFLICT";
    const errorHtml = (j.error && !waitingForDevice && !isConflict) ? `<div class="job-error"></div>` : "";
    const stallHtml = j.possibly_stalled ? `<div class="job-stall-warning"></div>` : "";
    const conflictHtml = isConflict ? `
      <div class="conflict-card">
        <p class="conflict-explanation">SwitchAgent found an existing destination and will not overwrite it automatically.</p>
        <dl class="conflict-details">
          <dt>Content</dt><dd class="conflict-content"></dd>
          <dt>Device</dt><dd class="conflict-device"></dd>
          <dt>Storage</dt><dd class="conflict-storage"></dd>
          ${j.destination_conflict_path ? `<dt>Destination</dt><dd class="conflict-path"></dd>` : ""}
          <dt>Time</dt><dd class="conflict-time"></dd>
        </dl>
        <div class="conflict-actions">
          ${j.destination_conflict_path ? `<button type="button" class="btn btn-small" data-copy-conflict-path="${j.destination_conflict_path}">Copy destination path</button>` : ""}
          <!-- W3-006 originally linked to the generic /devices list as a
               temporary fallback; W3-004 added a real per-device page, so
               this now points at THIS job's target device by its safe
               fingerprint (never the raw, serial-bearing device_id). -->
          <a class="btn btn-small" href="/devices/${j.target_device_fingerprint}">Device Details</a>
        </div>
        <p class="settings-hint">Override replaces the existing file with this one; Skip leaves it alone and drops this install.</p>
      </div>` : "";
    // W3-006 Override: a conflict gets its own pair of actions (Override /
    // Skip below) instead of the generic Retry/Cancel -- a plain retry
    // would just hit the identical conflict again unless the user went and
    // deleted the file by hand first, and "Cancel" doesn't say what happens
    // to the existing destination file either way.
    const retryBtn = !isConflict && !j.abandoned && RETRYABLE_STATUSES.includes(j.status)
      ? `<button class="btn btn-small" data-job-action="retry" data-job-id="${j.id}">Retry installation</button>` : "";
    const overrideBtn = isConflict && !j.abandoned
      ? `<button class="btn btn-small btn-danger" data-job-action="override" data-job-id="${j.id}">Override</button>` : "";
    const cancelBtn = !j.abandoned && ["PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "WAITING_FOR_BASE", "WAITING_FOR_DEVICE", ...RETRYABLE_STATUSES].includes(j.status)
      ? `<button class="btn btn-small ${isConflict ? "btn-ok" : "btn-danger"}" data-job-action="cancel" data-job-id="${j.id}">${isConflict ? "Skip" : "Cancel"}</button>` : "";

    li.innerHTML = `
      <div class="job-main">
        <div class="job-name"></div>
        <div class="job-meta"></div>
        ${progressTextHtml}
        ${errorHtml}
        ${conflictHtml}
        ${stallHtml}
      </div>
      <div class="job-status">
        <span class="status-pill ${statusClass(j.status)}"></span>
        ${spinner}
      </div>
      <div class="job-actions">${retryBtn}${overrideBtn}${cancelBtn}</div>
    `;
    li.querySelector(".job-name").textContent = j.display_name;
    li.querySelector(".job-meta").textContent = waitingForDevice
      ? `Waiting for ${j.target_device_label} to reconnect — resumes automatically`
      : `${j.target_device_label} · ${j.target_storage} · attempt ${j.attempt_count} · created ${j.created_at}`;
    li.querySelector(".status-pill").textContent = statusLabel(j);
    if (progressTextHtml) {
      li.querySelector(".job-progress-text").textContent =
        `${formatBytes(j.bytes_done)} / ${formatBytes(j.bytes_total)} (${Math.round(fraction)}%)`;
    }
    if (j.possibly_stalled) {
      const minutes = Math.floor(j.stall_seconds / 60);
      li.querySelector(".job-stall-warning").textContent =
        `Possibly stalled — no observable progress for ${minutes} min. ` +
        "Large MTP transfers may legitimately take a long time; this is not treated as a failure.";
    }
    if (j.error && !waitingForDevice && !isConflict) {
      const errorEl = li.querySelector(".job-error");
      errorEl.textContent = j.error;
      errorEl.title = j.error; // UX-001: long/technical errors are CSS-clamped -- full text via hover tooltip
    }
    if (isConflict) {
      li.querySelector(".conflict-content").textContent = j.display_name;
      li.querySelector(".conflict-device").textContent = j.target_device_label;
      li.querySelector(".conflict-storage").textContent = j.target_storage;
      if (j.destination_conflict_path) li.querySelector(".conflict-path").textContent = j.destination_conflict_path;
      li.querySelector(".conflict-time").textContent = j.finished_at || j.created_at;
    }
    return li;
  }

  function showToast(text, kind) {
    const el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.textContent = text;
    toastContainer.appendChild(el);
    setTimeout(() => el.remove(), 7000);
  }

  async function handleDisappearedJob(jobId) {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (!res.ok) return;
      const job = await res.json();
      if (job.status === "DONE") {
        showToast(`✓ ${job.display_name} — Installed`, "ok");
      } else if (job.status === "DONE_UNVERIFIED") {
        showToast(`${job.display_name} — Installed (unverified, see History)`, "warn");
      } else if (["FAILED", "INTERRUPTED", "SOURCE_CHANGED", "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY"].includes(job.status)) {
        showToast(`${job.display_name} — ${job.status.replace(/_/g, " ")}`, "error");
      }
      // Any other status (still active) means it just wasn't on this
      // particular poll's page/order -- say nothing, avoid a false toast.
    } catch (e) {
      // Network hiccup -- skip silently, the next poll will retry.
    }
  }

  async function poll() {
    let groups;
    try {
      const res = await fetch("/api/queue/grouped");
      if (!res.ok) return;
      groups = await res.json();
    } catch (e) {
      return; // transient network error -- try again next tick
    }

    const jobs = groups.flatMap((g) => g.jobs);
    const currentIds = new Set(jobs.map((j) => String(j.id)));
    for (const id of knownActiveIds) {
      if (!currentIds.has(id)) handleDisappearedJob(id);
    }
    knownActiveIds = currentIds;

    const represented = await pollPreparation();
    jobList.innerHTML = "";
    for (const g of groups) {
      const remaining = g.jobs.filter(j => !represented.has(String(j.id)));
      if (remaining.length) jobList.appendChild(renderGroup({...g, jobs: remaining}));
    }
    emptyState.hidden = groups.length > 0 || document.getElementById('preparation-list').children.length > 0;
    updateBulkConflictBar();
  }

  // -- Override all / Skip all -----------------------------------------
  // Conflict cards can pile up (one game after another already on the
  // device) -- clicking Override/Skip one at a time is exactly the kind
  // of tedium a bulk action exists for. Reuses the SAME single-job
  // endpoints, one request at a time (never in parallel -- matches how
  // every other multi-step action in this app is deliberately serial),
  // so each one gets the same DESTINATION_CONFLICT-only guard, the same
  // continue_chain() follow-up, and a genuine per-item result rather
  // than one all-or-nothing call.
  function updateBulkConflictBar() {
    const bar = document.getElementById("bulk-resolve-bar");
    if (!bar) return;
    const count = document.querySelectorAll('[data-job-action="override"]').length;
    bar.hidden = count === 0;
    document.getElementById("bulk-resolve-count").textContent =
      count > 0 ? `${count} conflict${count === 1 ? "" : "s"}` : "";
  }

  async function bulkResolveConflicts(action) {
    const ids = Array.from(document.querySelectorAll('[data-job-action="override"]'))
      .map((btn) => btn.dataset.jobId);
    if (!ids.length) return;
    const verb = action === "override" ? "Override" : "Skip";
    const message = action === "override"
      ? `Override all ${ids.length} conflicting file(s) with the new versions? This cannot be undone.`
      : `Skip all ${ids.length} conflicting install(s), leaving the existing files alone?`;
    if (!(await confirmAction(message))) return;
    for (const jobId of ids) {
      try {
        const res = await fetch(`/api/jobs/${jobId}/${action}`, { method: "POST" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          showToast(`${verb} failed for job ${jobId}: ${data.detail || res.status}`, "error");
        }
      } catch (e) {
        showToast(`${verb} failed for job ${jobId}: request error`, "error");
      }
    }
    poll();
  }

  async function pollPreparation() {
    const panel = document.getElementById("preparation-list");
    const represented = new Set();
    try {
      const response = await fetch("/api/preparations");
      if (!response.ok) return represented;
      const states = await response.json();
      panel.replaceChildren();
      for (const state of states) {
        // Prepared jobs now live in the main list; never duplicate them.
        const box = document.createElement("div");
        box.className = "batch-group";
        const items = Object.values(state.items).sort((a, b) => a.order - b.order);
        const ready = items.filter((item) => item.phase === "Ready").length;
        const heading = document.createElement("h3");
        heading.textContent = (state.phase === "Preparing" ? "Sequential installation" : state.phase) +
          ` · ${ready} / ${items.length} finished · ${state.elapsed}s`;
        box.appendChild(heading);
        for (const item of items) {
          if (item.jobs && item.jobs.length) {
            for (const job of item.jobs) {
              represented.add(String(job.id));
              const row = renderJobRow(job);
              row.querySelector('.job-name').textContent = item.jobs.length === 1 ? item.name : job.display_name;
              if (item.phase === 'Cleaning') row.querySelector('.status-pill').textContent = 'Cleaning temporary files';
              box.appendChild(row);
            }
            continue;
          }
          const line = document.createElement("div");
          line.className = "job-row preparation-row";
          // Once the whole sequential run has already stopped (state.phase
          // "Failed"), an item still sitting at "Waiting" never got its own
          // job created at all and never will -- this run is over, not
          // paused. Relabelled so that reads as "abandoned", not "about to
          // start any second" (see preparation.py's own stop condition --
          // the run genuinely never resumes on its own past this point).
          const neverStarted = state.phase === "Failed" && item.phase === "Waiting";
          const displayPhase = neverStarted ? "Not started (installation stopped)" : item.phase;
          line.dataset.status = neverStarted ? "Failed" : item.phase;
          line.classList.toggle("progress-active", ["Extracting", "Analyzing", "Installing", "Cleaning"].includes(item.phase));
          line.style.setProperty("--progress", item.phase === "Ready" ? "100%" : "0%");
          line.textContent = `${item.name} — ${displayPhase}` + (item.error ? `: ${item.error}` : "");
          box.appendChild(line);
        }
        // state.error is a snapshot of why the sequential run stopped,
        // frozen at the moment it happened -- it does NOT know that the
        // item which stopped it may since have been resolved (Override
        // creates a brand-new job the panel now follows forward to, see
        // _resolve_latest_retry). Without this check, overriding a
        // conflict and watching its new job actively copy still showed
        // "Installation stopped" right next to a live, moving progress
        // bar -- flatly wrong, nothing is stopped. Only keep showing it
        // while there's a job still stuck in one of the same statuses
        // that originally halted the run, and it wasn't itself just
        // abandoned via Skip -- i.e. something actually still needs a
        // click. An item that never got a job created at all ("Not
        // started (installation stopped)") does NOT keep this banner
        // alive on its own: that item's own row already says exactly
        // that, permanently (see `neverStarted` above) -- repeating the
        // identical sentence in a banner below it forever, long after
        // the one thing a user COULD act on has already been resolved,
        // is just noise.
        const stillBlocked = items.some((item) =>
          item.jobs && item.jobs.some((job) =>
            [...RETRYABLE_STATUSES, "WAITING_FOR_BASE"].includes(job.status) && !job.abandoned));
        const errors = state.result ? state.result.errors.map((error) => error.error) : [];
        if (state.error && stillBlocked) errors.push(state.error);
        if (errors.length) {
          const error = document.createElement("p");
          error.className = "job-error";
          error.textContent = "Installation stopped: " + errors.join("; ");
          box.appendChild(error);
        }
        panel.appendChild(box);
      }
      if (states.some((state) => ["Waiting", "Preparing"].includes(state.phase))) emptyState.hidden = true;
    } catch (_) { /* the next poll retries a transient connection failure */ }
    return represented;
  }
  async function tick() {
    try { await poll(); } finally { setTimeout(tick, POLL_INTERVAL_MS); }
  }
  updateBulkConflictBar();  // reflect the server-rendered initial state before the first poll lands
  tick();

  // -- actions (event delegation -- works for rows replaced by poll()) ----

  document.addEventListener("click", async (evt) => {
    const bulkBtn = evt.target.closest("[data-bulk-action]");
    if (bulkBtn) {
      bulkBtn.disabled = true;
      try {
        await bulkResolveConflicts(bulkBtn.dataset.bulkAction);
      } finally {
        bulkBtn.disabled = false;
      }
      return;
    }

    const copyBtn = evt.target.closest("[data-copy-conflict-path]");
    if (copyBtn) {
      const path = copyBtn.dataset.copyConflictPath;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(path);
          const original = copyBtn.textContent;
          copyBtn.textContent = "Copied!";
          setTimeout(() => { copyBtn.textContent = original; }, 2000);
        }
      } catch (e) {
        // Clipboard access denied/unavailable -- the path is already
        // visible in the conflict card for manual selection either way.
      }
      return;
    }

    const btn = evt.target.closest("[data-job-action]");
    if (btn) {
      const action = btn.dataset.jobAction;
      const jobId = btn.dataset.jobId;
      if (action === "cancel" && !(await confirmAction("Abandon this attempt and release its temporary files when no other job needs them?"))) return;
      if (action === "override" && !(await confirmAction("Overwrite the existing file(s) at the destination with this install? This cannot be undone."))) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/jobs/${jobId}/${action}`, { method: "POST" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          alert(`Could not ${action}: ${data.detail || res.status}`);
          btn.disabled = false;
          return;
        }
        poll();
      } catch (e) {
        alert("Request failed: " + e);
        btn.disabled = false;
      }
      return;
    }

    const workerBtn = evt.target.closest("[data-worker-action]");
    if (workerBtn) {
      workerBtn.disabled = true;
      await fetch("/api/worker/" + workerBtn.dataset.workerAction, { method: "POST" });
      window.location.reload(); // worker state text lives outside job-list -- a full reload here is fine, it's user-triggered, not a live-progress path
    }
  });
})();
