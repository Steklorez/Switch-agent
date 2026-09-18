// Devices page: friendly-name rename, storage mapping, and Forget.
// device_id itself is never editable here or sent anywhere except as the
// URL identifier -- renaming can never change identity (see docs/WEB-UI.md
// point 33).
(function () {
  "use strict";

  document.querySelectorAll(".device-rename-form").forEach((form) => {
    form.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const input = form.querySelector("input[name=friendly_name]");
      const btn = form.querySelector("button");
      btn.disabled = true;
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
          body: JSON.stringify({ friendly_name: input.value || null }),
        });
        if (!res.ok) {
          alert("Could not rename device.");
        }
      } catch (e) {
        alert("Request failed: " + e);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // -- Forget: erase SwitchAgent's own memory of a device it has seen --
  // the devices row, its friendly name, its storage mapping and its cached
  // installed-title list. Never the Queue/History records that mention it
  // (those stay, labelled by fingerprint), and never anything on the
  // console itself. The button only exists on disconnected rows.

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
