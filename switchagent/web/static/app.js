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

  // With no Switch plugged in there is no real card to measure, and the
  // bar used to simply vanish -- so the one moment you most want a sense
  // of scale (picking what to take with you, console not to hand) was the
  // one moment it told you nothing. Stand in a plausible empty card
  // instead: 256 GB is a common size and keeps a realistic selection to a
  // readable fraction of the track. Explicitly an assumption, never
  // presented as a measurement -- see renderStorageBar's label/title and
  // the .storage-bar-assumed styling.
  const ASSUMED_CARD_TOTAL_BYTES = 256 * 1024 ** 3;

  let usingAssumedCard = false;
  // WHY there is no card to measure. "no Switch connected" is only one of
  // the three, and the bar said exactly that in all of them until a mock
  // run with two consoles plugged in cheerfully reported "(no Switch)".
  let assumedCardReason = "none";
  const ASSUMED_CARD_REASONS = {
    none: "No Switch is connected",
    ambiguous: "More than one Switch is connected and there is no single target to measure",
    unreadable: "The connected Switch did not report its SD card size",
  };

  // Only pages that can actually select games (Library, Game Details --
  // both render _install_selection.html) get the stand-in. On History or
  // Settings there is nothing to add to a card, so a card-shaped bar with
  // nothing to say would be pure furniture.
  function pageCanSelectGames() {
    return document.getElementById("selection-bar") !== null;
  }

  function applyAssumedCard(reason) {
    if (!pageCanSelectGames()) {
      if (bar) bar.hidden = true;
      return;
    }
    usingAssumedCard = true;
    assumedCardReason = reason;
    sdCardTotalBytes = ASSUMED_CARD_TOTAL_BYTES;
    sdCardFreeBytes = ASSUMED_CARD_TOTAL_BYTES;  // assumed empty
    renderStorageBar();
  }

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
    bar.classList.toggle("storage-bar-assumed", usingAssumedCard);
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
    if (usingAssumedCard) {
      // Never "X used of Y" here -- nothing was measured. The tilde and
      // the "no Switch" suffix carry that in the few characters the label
      // has (it is ellipsised at 170px, and hidden outright on narrow
      // screens), with the full caveat in the tooltip.
      barLabel.textContent = (pendingBytes > 0 ? "+" + formatBytes(estimatedPendingBytes) + " of " : "")
        + "~" + formatBytes(sdCardTotalBytes) + " (est.)";
      bar.title = ASSUMED_CARD_REASONS[assumedCardReason]
        + ", so this is an estimate against an assumed empty "
        + formatBytes(sdCardTotalBytes) + " card, not a real measurement"
        + (pendingBytes > 0
          ? ". Selected games would take about " + formatBytes(estimatedPendingBytes)
            + " (includes a safety margin for on-device unpacking)."
          : ". Tick some games to see roughly how much they need.")
        + (assumedCardReason === "ambiguous"
          ? " Disconnect all but one Switch for its real free space."
          : " Connect a Switch for its real free space.");
    } else {
      barLabel.textContent = formatBytes(usedBytes) + " / " + formatBytes(sdCardTotalBytes)
        + (pendingBytes > 0 ? " (+" + formatBytes(estimatedPendingBytes) + ")" : "");
      bar.title = "SD card: " + formatBytes(usedBytes) + " used of " + formatBytes(sdCardTotalBytes)
        + (pendingBytes > 0
          ? ". Selected games would add about " + formatBytes(estimatedPendingBytes)
            + " (includes a safety margin for on-device unpacking)."
          : ".");
    }
    bar.hidden = false;
  }

  async function refreshStorageBar(devices) {
    if (!bar) return;
    const fingerprint = pickTargetFingerprint(devices);
    if (!fingerprint) {
      applyAssumedCard(devices.filter((d) => d.connected).length > 1 ? "ambiguous" : "none");
      return;
    }
    try {
      const res = await fetch("/api/devices/by-fingerprint/" + encodeURIComponent(fingerprint) + "/storages");
      if (!res.ok) throw new Error("bad response");
      const storages = await res.json();
      const sdCard = storages.find((s) => s.effective_logical_name === "SD_CARD");
      if (!sdCard || sdCard.free_bytes === null || sdCard.total_bytes === null) {
        // A Switch is there but its card did not report usable numbers --
        // still nothing measured, so the same honest stand-in applies.
        applyAssumedCard("unreadable");
        return;
      }
      usingAssumedCard = false;
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
