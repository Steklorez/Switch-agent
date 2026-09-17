// Shared across every page: keeps the top-bar device pill (and, on pages
// with a resolvable target Switch, the SD card space bar next to it)
// fresh via plain polling (see docs/WEB-UI.md -- no WebSocket/SSE, a
// simple periodic fetch() is enough for a handful of devices on a home
// LAN).
(function () {
  "use strict";
  const pill = document.getElementById("device-pill");
  if (!pill) return;

  // A selected item's on-disk file size is not necessarily its installed
  // size (NSZ/XCZ are decompressed onto the SD card) -- this is a small,
  // deliberately approximate safety margin on top of the selected total,
  // not a per-format decompression estimate (same "small reserve" spirit
  // as preparation.py's RESERVE_BYTES for the extraction workspace).
  const INSTALL_SIZE_MARGIN = 1.1;

  // Real SD cards are large (256GB-1TB+) and a selection is typically a
  // handful of GB -- proportionally that's often under 1% of the track's
  // width, i.e. a sub-pixel sliver that reads as "nothing happened" even
  // though the underlying percentage did grow. Floor the pending segment's
  // width so any nonzero selection is always clearly visible; still capped
  // by the actual remaining space below, so it never claims more free
  // space than really exists.
  const MIN_VISIBLE_PENDING_PCT = 1.5;

  const bar = document.getElementById("storage-bar");
  const barUsed = document.getElementById("storage-bar-used");
  const barPending = document.getElementById("storage-bar-pending");
  const barLabel = document.getElementById("storage-bar-label");

  let sdCardFreeBytes = null;
  let sdCardTotalBytes = null;
  let pendingBytes = 0;

  function pickTargetFingerprint(devices) {
    // Prefers whatever the user already chose on the install-selection
    // bar (present on Library/Game Details once rendered, regardless of
    // whether anything is selected yet); otherwise the same "exactly one
    // connected -- never guess among several, never invent one" rule
    // library.js already applies when auto-picking the target select.
    const targetSelect = document.getElementById("target-device");
    if (targetSelect && targetSelect.value) return targetSelect.value;
    const connected = devices.filter((d) => d.connected);
    return connected.length === 1 ? connected[0].device_fingerprint : null;
  }

  function renderStorageBar() {
    if (!bar || sdCardTotalBytes === null || sdCardFreeBytes === null || sdCardTotalBytes <= 0) {
      if (bar) bar.hidden = true;
      return;
    }
    const usedBytes = sdCardTotalBytes - sdCardFreeBytes;
    const estimatedPendingBytes = pendingBytes * INSTALL_SIZE_MARGIN;
    const usedPct = Math.max(0, Math.min(100, (100 * usedBytes) / sdCardTotalBytes));
    const remainingPct = Math.max(0, 100 - usedPct);
    let pendingPct = Math.max(0, Math.min(remainingPct, (100 * estimatedPendingBytes) / sdCardTotalBytes));
    if (pendingPct > 0) pendingPct = Math.min(remainingPct, Math.max(pendingPct, MIN_VISIBLE_PENDING_PCT));
    barUsed.style.width = usedPct + "%";
    barPending.style.left = usedPct + "%";
    barPending.style.width = pendingPct + "%";
    bar.classList.toggle("storage-bar-overflow", estimatedPendingBytes > sdCardFreeBytes);
    barLabel.textContent = formatBytes(usedBytes) + " / " + formatBytes(sdCardTotalBytes)
      + (pendingBytes > 0 ? " (+" + formatBytes(estimatedPendingBytes) + ")" : "");
    bar.title = "SD card: " + formatBytes(usedBytes) + " used of " + formatBytes(sdCardTotalBytes)
      + (pendingBytes > 0
        ? ". Selected games would add about " + formatBytes(estimatedPendingBytes)
          + " (includes a safety margin for on-device unpacking)."
        : ".");
    bar.hidden = false;
  }

  async function refreshStorageBar(devices) {
    if (!bar) return;
    const fingerprint = pickTargetFingerprint(devices);
    if (!fingerprint) {
      sdCardFreeBytes = null;
      sdCardTotalBytes = null;
      bar.hidden = true;
      return;
    }
    try {
      const res = await fetch("/api/devices/by-fingerprint/" + encodeURIComponent(fingerprint) + "/storages");
      if (!res.ok) throw new Error("bad response");
      const storages = await res.json();
      const sdCard = storages.find((s) => s.effective_logical_name === "SD_CARD");
      if (!sdCard || sdCard.free_bytes === null || sdCard.total_bytes === null) {
        sdCardFreeBytes = null;
        sdCardTotalBytes = null;
        bar.hidden = true;
        return;
      }
      sdCardFreeBytes = sdCard.free_bytes;
      sdCardTotalBytes = sdCard.total_bytes;
      renderStorageBar();
    } catch (e) {
      // Network hiccup -- leave the last known bar in place, same as the
      // device pill below.
    }
  }

  document.addEventListener("storage:selection-changed", (e) => {
    pendingBytes = (e.detail && e.detail.bytes) || 0;
    renderStorageBar();
  });

  async function refresh() {
    try {
      const res = await fetch("/api/devices");
      if (!res.ok) throw new Error("bad response");
      const devices = await res.json();
      const connected = devices.filter((d) => d.connected);
      pill.textContent = connected.length
        ? connected.length + " connected"
        : "No Switch connected";
      pill.classList.toggle("has-connected", connected.length > 0);
      pill.classList.toggle("none-connected", connected.length === 0);
      refreshStorageBar(devices);
    } catch (e) {
      // Network hiccup or server briefly restarting -- leave the last known
      // text in place rather than flashing an alarming error every 5s.
    }
  }

  refresh();
  setInterval(refresh, 5000);
})();

function formatBytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = n;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i += 1;
  }
  return size.toFixed(1) + " " + units[i];
}
