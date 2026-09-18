// W3-004 Device Details page: rename, storage mapping, device-scoped
// diagnostics export.
//
// This page is addressed by FINGERPRINT, never by the raw, serial-bearing
// device_id (see mtp/windows.py's mask_device_id/device_fingerprint) -- so
// unlike devices.js, nothing here can read a device id out of the DOM: the
// template deliberately never puts one there.
//
// Privacy finding (independent verification, 2026-09-12): this file used
// to resolve the fingerprint to the raw device_id client-side and call the
// raw-id mutation routes directly. That never leaked into the page's own
// HTML, but the raw id ended up in every rename/mapping request's URL --
// which uvicorn's own access log records. Every action below now calls a
// fingerprint-addressed route instead (server resolves the raw id
// internally, same as the diagnostics-export call already did) -- the raw
// device_id never crosses into the browser, the JS runtime, or a logged
// URL at all.
(function () {
  "use strict";

  const fingerprintEl = document.getElementById("device-fingerprint");
  if (!fingerprintEl) return;
  const fingerprint = fingerprintEl.textContent.trim();

  // -- rename ---------------------------------------------------------------

  const renameForm = document.getElementById("device-rename-form");
  if (renameForm) {
    renameForm.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const input = renameForm.querySelector("input[name=friendly_name]");
      const btn = renameForm.querySelector("button");
      btn.disabled = true;
      try {
        const res = await fetch(`/api/devices/by-fingerprint/${encodeURIComponent(fingerprint)}/rename`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ friendly_name: input.value || null }),
        });
        if (!res.ok) {
          alert("Could not rename device.");
          return;
        }
        window.location.reload();
      } catch (e) {
        alert("Request failed: " + e);
      } finally {
        btn.disabled = false;
      }
    });
  }

  // -- storage mapping --------------------------------------------------------

  document.querySelectorAll(".storage-mapping-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("tr");
      const rawStorageName = row.dataset.rawStorageName;
      const logicalName = row.querySelector(".storage-mapping-select").value;

      btn.disabled = true;
      try {
        const base = `/api/devices/by-fingerprint/${encodeURIComponent(fingerprint)}/storages/mapping`;
        const res = logicalName === ""
          // "AUTO: ..." selected -- revert to automatic resolution.
          ? await fetch(`${base}/clear`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ raw_storage_name: rawStorageName }),
            })
          : await fetch(base, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ raw_storage_name: rawStorageName, logical_name: logicalName }),
            });
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

  // -- device-scoped diagnostics export ------------------------------------
  // Same single diagnostics engine as Settings' whole-app export, just
  // narrowed to this device (see web/detail_views.py). Rendered inline so
  // the user can read and copy it; nothing is uploaded anywhere.

  const exportBtn = document.getElementById("export-device-diagnostics");
  const exportOutput = document.getElementById("device-diagnostics-output");
  if (exportBtn && exportOutput) {
    exportBtn.addEventListener("click", async () => {
      exportBtn.disabled = true;
      exportOutput.hidden = false;
      exportOutput.textContent = "Collecting…";
      try {
        const res = await fetch(
          `/api/devices/by-fingerprint/${encodeURIComponent(fingerprint)}/diagnostics/export?format=txt`
        );
        exportOutput.textContent = res.ok
          ? await res.text()
          : "Could not collect diagnostics for this device.";
      } catch (e) {
        exportOutput.textContent = "Request failed: " + e;
      } finally {
        exportBtn.disabled = false;
      }
    });
  }
})();
