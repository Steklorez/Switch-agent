// Shared across every page: keeps the top-bar device pill fresh via plain
// polling (see docs/WEB-UI.md -- no WebSocket/SSE, a simple periodic
// fetch() is enough for a handful of devices on a home LAN).
(function () {
  "use strict";
  const pill = document.getElementById("device-pill");
  if (!pill) return;

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
