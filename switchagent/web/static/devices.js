// Devices page: friendly-name rename (self-saving), storage mapping, and
// the corner × that forgets a device.
// device_id itself is never editable here or sent anywhere except as the
// URL identifier -- renaming can never change identity (see docs/WEB-UI.md
// point 33).
(function () {
  "use strict";

  // -- rename: no Save button, the field saves itself ------------------------
  // Debounced while typing, plus an immediate save on blur and on Enter, so
  // a name is never left unsaved just because the field still has focus.
  // Every path funnels through save(), which no-ops when the value has not
  // actually changed -- so blur right after a debounced save sends nothing.
  // A short inline status replaces the button as the only feedback the user
  // gets; a failure stays on screen (unlike "Saved", which fades) because
  // with no button to re-click, an unnoticed failure would look like a
  // silently lost name.

  const RENAME_DEBOUNCE_MS = 700;
  const SAVED_VISIBLE_MS = 1800;

  document.querySelectorAll(".device-rename-form").forEach((form) => {
    const input = form.querySelector("input[name=friendly_name]");
    const status = form.querySelector(".device-rename-status");
    let savedValue = input.value;
    let debounceTimer = null;
    let fadeTimer = null;

    function showStatus(text, failed) {
      if (!status) return;
      window.clearTimeout(fadeTimer);
      status.textContent = text;
      status.classList.toggle("failed", Boolean(failed));
      status.classList.add("visible");
      if (!failed) {
        fadeTimer = window.setTimeout(() => status.classList.remove("visible"), SAVED_VISIBLE_MS);
      }
    }

    async function save() {
      window.clearTimeout(debounceTimer);
      const value = input.value;
      if (value === savedValue) return;
      try {
        // The raw device_id embeds the device's USB descriptor path (can
        // contain '#', '&', ...) -- unencoded, a literal '#' is read as a
        // URL fragment and silently truncates the path before it ever
        // reaches the server, so the device_id the server sees never
        // matches any row (device_detail.js already encodes its own
        // fingerprint-addressed URLs for the same reason).
        const url = `/api/devices/${encodeURIComponent(form.dataset.deviceId)}/rename`;
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ friendly_name: value || null }),
        });
        if (!res.ok) {
          // savedValue is deliberately NOT updated -- the next blur retries.
          showStatus("Not saved", true);
          return;
        }
        savedValue = value;
        showStatus("Saved", false);
      } catch (e) {
        showStatus("Not saved", true);
      }
    }

    input.addEventListener("input", () => {
      window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(save, RENAME_DEBOUNCE_MS);
    });
    input.addEventListener("blur", save);
    // The form has no submit button any more; Enter in a lone text input
    // still submits it, and that should mean "save now", not navigate.
    form.addEventListener("submit", (evt) => {
      evt.preventDefault();
      save();
    });
  });

  // -- the corner ×: erase SwitchAgent's own memory of a device it has seen --
  // the devices row, its friendly name, its storage mapping and its cached
  // installed-title list. Never the Queue/History records that mention it
  // (those stay, labelled by fingerprint), and never anything on the
  // console itself. Only rendered on disconnected rows.

  document.querySelectorAll(".device-forget").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const confirmed = window.confirm(
        `Forget "${btn.dataset.displayName}"?\n\n` +
        "SwitchAgent stops listing this Switch and loses its name and storage " +
        "mapping. Queue and History entries are kept, and nothing on the console " +
        "itself is touched.\n\n" +
        "If this Switch is ever connected again, it reappears here as a new device."
      );
      if (!confirmed) return;

      btn.disabled = true;
      try {
        const fp = encodeURIComponent(btn.dataset.fingerprint);
        const res = await fetch(`/api/devices/by-fingerprint/${fp}/forget`, { method: "POST" });
        if (!res.ok) {
          // 409 carries a written-for-the-user reason (connected right now,
          // unfinished jobs) -- show it verbatim rather than a generic error.
          const data = await res.json().catch(() => ({}));
          alert(`Could not forget this device: ${data.detail || res.status}`);
          btn.disabled = false;
          return;
        }
        window.location.reload();
      } catch (e) {
        alert("Request failed: " + e);
        btn.disabled = false;
      }
    });
  });

  // -- UI-007: device storage mapping (AUTO / SD_CARD / SD_INSTALL only) --

  document.querySelectorAll(".storage-mapping-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("tr");
      const deviceId = encodeURIComponent(row.dataset.deviceId);
      const rawStorageName = row.dataset.rawStorageName;
      const select = row.querySelector(".storage-mapping-select");
      const logicalName = select.value;

      btn.disabled = true;
      try {
        let res;
        if (logicalName === "") {
          // "AUTO: ..." selected -- revert to automatic resolution.
          res = await fetch(`/api/devices/${deviceId}/storages/mapping/clear`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ raw_storage_name: rawStorageName }),
          });
        } else {
          res = await fetch(`/api/devices/${deviceId}/storages/mapping`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ raw_storage_name: rawStorageName, logical_name: logicalName }),
          });
        }
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          alert(`Could not save mapping: ${data.detail || res.status}`);
          btn.disabled = false;
          return;
        }
        window.location.reload();
      } catch (e) {
        alert("Request failed: " + e);
        btn.disabled = false;
      }
    });
  });
})();
