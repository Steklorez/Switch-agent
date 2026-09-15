// W3-005: Settings page "Backup / Restore" section.
//
// Kept in its own file rather than appended to settings.js: restore is the
// one destructive action in the whole Web UI, and its confirm-then-POST
// flow is easier to audit sitting on its own.
//
// The download itself is a plain <a href="/api/backup"> -- the browser's
// own download machinery handles it, and the server's Content-Disposition
// supplies the SwitchAgent-Backup-YYYY-MM-DD.zip filename (same approach
// the existing "Export diagnostics" link already uses). No fetch/blob
// dance is needed, and a plain link keeps working if JS fails to load.
(function () {
  "use strict";

  const fileInput = document.getElementById("restore-file-input");
  const restoreForm = document.getElementById("restore-form");
  const restoreBtn = document.getElementById("restore-btn");
  const restoreStatus = document.getElementById("restore-status");
  const downloadLink = document.getElementById("download-backup-link");
  const backupStatus = document.getElementById("backup-status");

  if (downloadLink && backupStatus) {
    // A backup is taken at request time, so there is a short pause before
    // the browser's download starts. Say so rather than looking frozen.
    downloadLink.addEventListener("click", () => {
      backupStatus.textContent = "Preparing backup…";
      window.setTimeout(() => { backupStatus.textContent = ""; }, 6000);
    });
  }

  if (!restoreForm || !fileInput) return;

  // Restore stays disabled until a file is actually chosen -- there is no
  // meaningful "restore nothing".
  fileInput.addEventListener("change", () => {
    restoreBtn.disabled = fileInput.files.length === 0;
    restoreStatus.textContent = "";
  });

  restoreForm.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    if (fileInput.files.length === 0) return;

    const chosen = fileInput.files[0];
    const confirmed = confirm(
      "Restore from \"" + chosen.name + "\"?\n\n" +
      "This REPLACES your current database and config.yaml with the contents of that " +
      "backup. Your library, queue, history, device names and storage mappings will be " +
      "rolled back to whatever the backup contains, and anything recorded since then " +
      "will be lost.\n\n" +
      "A safety copy of your current state is saved automatically first.\n\n" +
      "Continue?"
    );
    if (!confirmed) {
      restoreStatus.textContent = "Restore cancelled — nothing was changed.";
      return;
    }

    restoreBtn.disabled = true;
    fileInput.disabled = true;
    restoreStatus.textContent = "Validating and restoring… do not close SwitchAgent.";

    try {
      const body = new FormData();
      body.append("file", chosen);
      const res = await fetch("/api/restore", { method: "POST", body: body });

      let payload = null;
      try {
        payload = await res.json();
      } catch (e) {
        payload = null;
      }

      if (!res.ok) {
        // The server deliberately distinguishes these: 409 = refused,
        // nothing happened; 400 = the file is not a usable backup, current
        // state untouched; 500 = failed at/after the replace (the detail
        // says whether the automatic rollback succeeded).
        const detail = (payload && payload.detail) || "Restore failed.";
        restoreStatus.textContent = detail;
        restoreBtn.disabled = false;
        fileInput.disabled = false;
        return;
      }

      restoreStatus.textContent =
        "Restored successfully. Reloading to show the restored data…";
      window.setTimeout(() => { window.location.reload(); }, 1200);
    } catch (e) {
      restoreStatus.textContent = "Request failed: " + e;
      restoreBtn.disabled = false;
      fileInput.disabled = false;
    }
  });
})();
