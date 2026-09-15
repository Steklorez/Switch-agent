(async () => {
  const form = document.getElementById("automation-form");
  if (!form) return;
  const auto = document.getElementById("auto-scan");
  const interval = document.getElementById("scan-interval");
  const covers = document.getElementById("auto-covers");
  const feedback = document.getElementById("automation-feedback");
  try {
    const values = await (await fetch("/api/preferences")).json();
    auto.checked = values.auto_scan;
    interval.value = values.scan_interval;
    covers.checked = values.covers;
    if (values.covers_running) feedback.textContent = "Downloading covers…";
    else if (values.covers_error) feedback.textContent = values.covers_error;
  } catch (_) { feedback.textContent = "Could not load settings"; }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch("/api/preferences", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({auto_scan: auto.checked, scan_interval: Number(interval.value), covers: covers.checked})});
      if (!response.ok) throw new Error("Could not save settings");
      feedback.textContent = "Saved";
    } catch (error) { feedback.textContent = error.message; }
    finally { button.disabled = false; }
  });
})();
