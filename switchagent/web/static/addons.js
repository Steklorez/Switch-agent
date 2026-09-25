// Add-ons tab (templates/addons.html): the preview enlarger, and the one
// action there is -- installing emuiibo, which is the Amiibo tab's own
// GitHub download (POST /api/amiibo/emuiibo/download), followed here until
// it is in the Queue.
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

  function kb(bytes) {
    return Math.round((bytes || 0) / 1024) + " KB";
  }

  function describe(d) {
    const version = d.version ? "emuiibo " + d.version : "emuiibo";
    switch (d.state) {
      case "checking": return "Asking GitHub for the current emuiibo…";
      case "downloading": return "Downloading " + version + " from GitHub… " + kb(d.received) + (d.total ? " of " + kb(d.total) : "");
      case "adding": return version + " downloaded and checked — adding it to your Library…";
      case "done": return version + (d.reused ? " was already in your Library" : " downloaded from GitHub" +
        (d.verified ? ", its SHA-256 checked against GitHub's," : "") + " and added to your Library") +
        (d.queued ? " — it is in the Queue now. Restart the Switch once it has been installed." +
          (document.querySelector('.main-nav a[href="/amiibo"]') ? "" : " The Amiibo tab appears then.") : ".");
      case "failed": return "Installing emuiibo failed: " + (d.error || "unknown error");
      default: return "";
    }
  }

  function show(d, done) {
    box.hidden = false;
    box.classList.toggle("is-busy", !done);
    box.classList.toggle("is-failed", d.state === "failed");
    box.replaceChildren(document.createTextNode(describe(d)));
    if (d.state === "done" && d.queued) {
      const link = document.createElement("a");
      link.href = "/queue";
      link.textContent = " Open Queue →";
      box.append(link);
    }
  }

  async function follow(device) {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      let data;
      try {
        const res = await fetch("/api/amiibo/activity?device=" + encodeURIComponent(device));
        data = await res.json();
      } catch (_) {
        continue;
      }
      const d = data.download;
      if (!d) continue;
      const done = d.state === "done" || d.state === "failed";
      show(d, done);
      if (done) return d;
    }
  }

  document.querySelectorAll(".addon-install").forEach((button) => {
    button.addEventListener("click", async () => {
      const device = button.dataset.device;
      if (!device) return;
      button.disabled = true;
      const original = button.textContent;
      button.textContent = "Starting…";
      try {
        const res = await fetch("/api/amiibo/emuiibo/download", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
        show(data, false);
        button.textContent = "Working…";
        const result = await follow(device);
        button.textContent = result.state === "done" ? "Queued" : original;
        button.disabled = result.state === "done";
      } catch (e) {
        button.textContent = original;
        button.disabled = false;
        show({ state: "failed", error: e.message }, true);
      }
    });
  });
})();
