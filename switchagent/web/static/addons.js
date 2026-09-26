// Add-ons tab (templates/addons.html): the preview enlarger, and the one
// action there is -- Install: the add-on and whatever it requires that this
// Switch lacks, from GitHub, into the ordinary queue
// (POST /api/addons/install, web/addons_service.py), followed here until it
// is in the Queue.
(function () {
  "use strict";

  const dialog = document.getElementById("addon-preview-dialog");
  document.querySelectorAll(".addon-preview").forEach((button) => {
    button.addEventListener("click", () => {
      dialog.querySelector("img").src = button.dataset.full;
      dialog.querySelector("img").alt = button.dataset.caption || "";
      dialog.querySelector("p").textContent = button.dataset.caption || "";
      dialog.showModal();
    });
  });
  if (dialog) {
    dialog.querySelector("button").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog) dialog.close(); });
  }

  const box = document.getElementById("addons-download");
  const addons = window.SwitchAgentAddons;

  function show(d, done) {
    box.hidden = false;
    box.classList.toggle("is-busy", !done);
    box.classList.toggle("is-failed", d.state === "failed");
    let text = addons.describe(d);
    if (d.state === "done" && d.queued) {
      const restart = (d.steps || []).some((s) => ["ultrahand", "emuiibo", "saltynx", "fizeau"].includes(s.id));
      text += restart ? " Restart the Switch once they are installed." : "";
      if ((d.addons || []).includes("emuiibo") && !document.querySelector('.main-nav a[href="/amiibo"]')) {
        text += " The Amiibo tab appears then.";
      }
    }
    box.replaceChildren(document.createTextNode(text));
    if (d.state === "done" && d.queued) {
      const link = document.createElement("a");
      link.href = "/queue";
      link.textContent = " Open Queue →";
      box.append(link);
    }
  }

  async function follow() {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 800));
      let data;
      try {
        const res = await fetch("/api/addons/activity");
        data = await res.json();
      } catch (_) {
        continue;
      }
      const d = data.download;
      if (!d) continue;
      const done = !addons.busy(d);
      show(d, done);
      if (done) return d;
    }
  }

  const buttons = Array.from(document.querySelectorAll(".addon-install"));

  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      const device = button.dataset.device;
      if (!device) return;
      // One install at a time: the others wait for this one.
      const wasDisabled = buttons.map((b) => b.disabled);
      buttons.forEach((b) => { b.disabled = true; });
      const original = button.textContent;
      button.textContent = "Starting…";
      try {
        const res = await fetch("/api/addons/install", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device, addon: button.dataset.addon }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
        show(data, false);
        button.textContent = "Working…";
        const result = await follow();
        button.textContent = result.state === "done" ? "Queued" : original;
        buttons.forEach((b, i) => { b.disabled = wasDisabled[i] || b === button && result.state === "done"; });
      } catch (e) {
        button.textContent = original;
        buttons.forEach((b, i) => { b.disabled = wasDisabled[i]; });
        show({ state: "failed", error: e.message, steps: [] }, true);
      }
    });
  });

  // An install already under way (started on another page) is shown.
  fetch("/api/addons/activity").then((r) => r.json()).then((data) => {
    if (data.download && addons.busy(data.download)) {
      show(data.download, false);
      follow();
    }
  }).catch(() => {});
})();
