// Devices page: friendly-name rename. device_id itself is never editable
// here or sent anywhere except as the URL identifier -- renaming can never
// change identity (see docs/WEB-UI.md point 33).
(function () {
  "use strict";

  document.querySelectorAll(".device-rename-form").forEach((form) => {
    form.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const input = form.querySelector("input[name=friendly_name]");
      const btn = form.querySelector("button");
      btn.disabled = true;
      try {
        const res = await fetch(form.dataset.url, {
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

  // -- UI-007: device storage mapping (AUTO / SD_CARD / SD_INSTALL only) --

  document.querySelectorAll(".storage-mapping-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("tr");
      const deviceId = row.dataset.deviceId;
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
