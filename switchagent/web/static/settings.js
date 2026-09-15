// Settings page: "Copy diagnostics" button. The full safe report text is
// server-rendered into a hidden <textarea> (see settings.html) so we never
// have to worry about JSON/attribute escaping of a large multi-line blob --
// this script only ever reads it back out.
(function () {
  "use strict";

  const btn = document.getElementById("copy-diagnostics-btn");
  const status = document.getElementById("copy-diagnostics-status");
  const textArea = document.getElementById("diagnostics-text");
  if (!btn || !textArea) return;

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) {
        // fall through to the legacy fallback below
      }
    }
    // Graceful fallback for browsers/contexts without Clipboard API access
    // (e.g. no secure context): select the hidden textarea and use the
    // older execCommand copy path.
    try {
      textArea.hidden = false;
      textArea.focus();
      textArea.select();
      const ok = document.execCommand("copy");
      textArea.hidden = true;
      return ok;
    } catch (e) {
      textArea.hidden = true;
      return false;
    }
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "";
    try {
      const ok = await copyText(textArea.value);
      status.textContent = ok ? "Copied to clipboard." : "Could not copy automatically -- select the text manually.";
    } finally {
      btn.disabled = false;
    }
  });

  // -- UI-004: work directory cleanup (preview-then-confirm, never silent) --

  const cleanupBtn = document.getElementById("cleanup-btn");
  const cleanupSummary = document.getElementById("cleanup-summary");
  const cleanupStatus = document.getElementById("cleanup-status");

  function formatBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = n, i = 0;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i += 1; }
    return `${size.toFixed(1)} ${units[i]}`;
  }

  if (cleanupBtn) {
    cleanupBtn.addEventListener("click", async () => {
      cleanupBtn.disabled = true;
      cleanupStatus.textContent = "";
      try {
        // Always re-check right before confirming -- the server-rendered
        // numbers on page load may be stale by the time the user clicks.
        const previewRes = await fetch("/api/work-cleanup/preview");
        const preview = await previewRes.json();
        if (preview.eligible_job_count === 0) {
          cleanupSummary.textContent = "Nothing eligible for cleanup right now.";
          return; // stays disabled -- nothing to do, matches the summary
        }
        const confirmed = confirm(
          `${preview.eligible_job_count} completed job(s), ` +
          `${formatBytes(preview.eligible_size_bytes)} will be removed. Continue?`
        );
        if (!confirmed) {
          cleanupBtn.disabled = false;
          return;
        }

        const res = await fetch("/api/work-cleanup", { method: "POST" });
        if (!res.ok) {
          cleanupStatus.textContent = "Cleanup failed.";
          cleanupBtn.disabled = false;
          return;
        }
        const result = await res.json();
        cleanupStatus.textContent =
          `Cleaned ${result.cleaned_job_count} job(s), freed ${formatBytes(result.freed_bytes)}.`;
        cleanupSummary.textContent = "Nothing eligible for cleanup right now.";
        // stays disabled -- nothing left to clean, matches the summary
      } catch (e) {
        cleanupStatus.textContent = "Request failed: " + e;
        cleanupBtn.disabled = false;
      }
    });
  }

  // -- W3-002: Library folders -- a list you add to and remove from
  // directly (each add is still validated server-side before it's
  // persisted; a remove is just dropping an already-known-good entry, so
  // it saves immediately -- no separate Validate/Save step to remember). -

  const libraryList = document.getElementById("library-dir-list");
  const libraryEmpty = document.getElementById("library-dir-empty");
  const libraryFeedback = document.getElementById("library-dir-feedback");
  const libraryPickBtn = document.getElementById("library-dir-pick-btn");
  const libraryManualInput = document.getElementById("library-dir-manual-input");
  const libraryManualAddBtn = document.getElementById("library-dir-manual-add-btn");

  if (libraryList) {
    let currentDirs = Array.from(libraryList.querySelectorAll(".library-dir-row")).map(li => li.dataset.path);

    function renderLibraryDirs(dirs) {
      currentDirs = dirs;
      libraryList.innerHTML = "";
      for (const dir of dirs) {
        const li = document.createElement("li");
        li.className = "library-dir-row";
        li.dataset.path = dir;
        const span = document.createElement("span");
        span.className = "library-dir-path";
        span.textContent = dir;
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "library-dir-remove-btn";
        removeBtn.title = "Remove";
        removeBtn.setAttribute("aria-label", `Remove ${dir}`);
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", () => removeLibraryDir(dir));
        li.append(span, removeBtn);
        libraryList.appendChild(li);
      }
      if (libraryEmpty) libraryEmpty.hidden = dirs.length > 0;
    }

    // Returns null on success, or a plain-string error message on failure
    // -- never throws, so callers can decide what to do next (retry, give
    // up) instead of only being able to report the failure.
    async function saveLibraryDirs(dirs, busyMessage) {
      if (dirs.length === 0) {
        // Never actually POST this -- LibraryDirRequest.path requires a
        // non-empty string (schemas.py), so an empty `dirs` would 422 with
        // FastAPI's structured (non-string) validation-error detail, not
        // the friendly "Choose at least one folder" services.py raises
        // for every OTHER empty-list case. Same rule, said once, client-side.
        return "At least one folder is required -- add a replacement before removing the last one.";
      }
      libraryFeedback.textContent = busyMessage;
      try {
        const res = await fetch("/api/settings/library-dir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: dirs[0], paths: dirs }),
        });
        const body = await res.json();
        if (!res.ok) return body.detail || "Could not save.";
        renderLibraryDirs(body.library_dirs);
        libraryFeedback.textContent = "Saved -- rescanning the library now.";
        return null;
      } catch (e) {
        return String(e.message || e);
      }
    }

    async function removeLibraryDir(path) {
      const next = currentDirs.filter((d) => d !== path);
      const error = await saveLibraryDirs(next, "Removing…");
      if (error) libraryFeedback.textContent = error;
    }

    async function addLibraryDir(path) {
      if (!path) return;
      if (currentDirs.includes(path)) {
        libraryFeedback.textContent = "That folder is already in the list.";
        return;
      }
      libraryFeedback.textContent = "Checking…";
      try {
        const res = await fetch("/api/settings/library-dir/validate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const body = await res.json();
        if (!res.ok || !body.valid) throw new Error(body.reason || body.detail || "Not a usable folder.");
      } catch (e) {
        libraryFeedback.textContent = String(e.message || e);
        return;
      }

      const baseline = currentDirs;
      let error = await saveLibraryDirs([...baseline, path], "Adding…");
      if (error) {
        // The list is validated as a whole (services.set_library_dir) --
        // an ALREADY-listed folder that's since gone stale (drive
        // unplugged, renamed) blocks adding this new, perfectly good one
        // too, and the user would otherwise be stuck: they can't remove
        // the stale entry either while it's the only one (the server
        // refuses an empty list). If the error names one of the folders
        // already in the list (not the one just typed/picked), drop it
        // and retry once rather than leaving the user to hunt it down.
        const stale = baseline.find((d) => error.includes(d));
        if (stale) {
          const retryError = await saveLibraryDirs(
            [...baseline.filter((d) => d !== stale), path],
            "Removing no-longer-valid folder and adding the new one…",
          );
          if (retryError) {
            libraryFeedback.textContent = retryError;
          } else {
            libraryFeedback.textContent = `Removed "${stale}" (no longer valid) and added "${path}".`;
          }
          return;
        }
        libraryFeedback.textContent = error;
      }
    }

    for (const btn of libraryList.querySelectorAll(".library-dir-remove-btn")) {
      btn.addEventListener("click", () => removeLibraryDir(btn.dataset.path));
    }

    if (libraryManualAddBtn) {
      const submitManualPath = async () => {
        const path = libraryManualInput.value.trim();
        if (!path) { libraryFeedback.textContent = "Enter a path first."; return; }
        libraryManualAddBtn.disabled = true;
        await addLibraryDir(path);
        libraryManualAddBtn.disabled = false;
        libraryManualInput.value = "";
      };
      libraryManualAddBtn.addEventListener("click", submitManualPath);
      libraryManualInput.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter") { evt.preventDefault(); submitManualPath(); }
      });
    }

    if (libraryPickBtn) {
      libraryPickBtn.disabled = !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
      libraryPickBtn.addEventListener("click", async () => {
        libraryPickBtn.disabled = true;
        libraryFeedback.textContent = "Choose a folder in the Windows dialog that just opened…";
        try {
          const res = await fetch("/api/settings/library-dir/pick", { method: "POST" });
          if (!(res.headers.get("content-type") || "").includes("application/json")) {
            throw new Error(`Folder picker returned an unexpected response (HTTP ${res.status}). Restart SwitchAgent after updating; details are in the application log.`);
          }
          const body = await res.json();
          if (!res.ok) throw new Error(body.detail || "Could not open the folder picker");
          if (body.path) {
            await addLibraryDir(body.path);
          } else {
            libraryFeedback.textContent = "Selection cancelled.";
          }
        } catch (e) {
          libraryFeedback.textContent = String(e.message || e);
        } finally {
          libraryPickBtn.disabled = false;
        }
      });
    }
  }

  // -- W3-008: update availability notification (never an auto-updater --
  // fetched asynchronously so a slow/offline check can never delay this
  // page's own initial render) ---------------------------------------

  const updateStatus = document.getElementById("update-check-status");
  const updateLink = document.getElementById("update-check-link");
  const updateNowBtn = document.getElementById("update-check-now-btn");

  function renderUpdateCheck(body) {
    if (body.error && !body.latest_version) {
      updateStatus.textContent = "Could not check for updates.";
      updateLink.hidden = true;
      return;
    }
    if (body.update_available) {
      updateStatus.textContent = `Version ${body.latest_version} available.`;
      updateLink.href = body.release_url;
      updateLink.hidden = false;
    } else {
      updateStatus.textContent = "You are on the latest version.";
      updateLink.hidden = true;
    }
  }

  async function loadUpdateCheck(force) {
    try {
      const res = await fetch("/api/update-check", { method: force ? "POST" : "GET" });
      const body = await res.json();
      renderUpdateCheck(body);
    } catch (e) {
      updateStatus.textContent = "Could not check for updates.";
    }
  }

  if (updateStatus) {
    loadUpdateCheck(false);
    updateNowBtn.addEventListener("click", async () => {
      updateNowBtn.disabled = true;
      updateStatus.textContent = "Checking…";
      try {
        await loadUpdateCheck(true);
      } finally {
        updateNowBtn.disabled = false;
      }
    });
  }

  // -- UI-006: "Restart worker when safe" --------------------------------

  const restartBtn = document.getElementById("restart-worker-btn");
  const restartStatus = document.getElementById("restart-status");

  if (restartBtn) {
    restartBtn.addEventListener("click", async () => {
      restartBtn.disabled = true;
      try {
        const res = await fetch("/api/worker/restart", { method: "POST" });
        if (!res.ok) {
          restartStatus.textContent = "Could not request a restart.";
          return;
        }
        restartStatus.textContent = "Restart queued — will apply at the worker's next safe point.";
      } catch (e) {
        restartStatus.textContent = "Request failed: " + e;
      } finally {
        restartBtn.disabled = false;
      }
    });
  }
})();
