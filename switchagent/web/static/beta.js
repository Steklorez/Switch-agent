// Settings -> Beta features: turning them on asks first (the dialog says
// they are experimental); turning them off does not. The page reloads so
// the navigation shows -- or stops showing -- the Add-ons tab.
(function () {
  "use strict";

  const toggle = document.getElementById("beta-toggle");
  const dialog = document.getElementById("beta-dialog");
  if (!toggle || !dialog) return;

  async function save(enabled) {
    const res = await fetch("/api/preferences/beta", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    window.location.reload();
  }

  toggle.addEventListener("change", () => {
    if (!toggle.checked) {
      save(false).catch((e) => { toggle.checked = true; alert("Could not save: " + e.message); });
      return;
    }
    toggle.checked = false;  // only once confirmed
    dialog.showModal();
  });

  document.getElementById("beta-cancel").addEventListener("click", () => dialog.close());
  document.getElementById("beta-confirm").addEventListener("click", () => {
    dialog.close();
    toggle.checked = true;
    save(true).catch((e) => { toggle.checked = false; alert("Could not save: " + e.message); });
  });
})();
