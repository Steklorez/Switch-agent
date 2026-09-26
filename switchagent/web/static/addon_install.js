// One sentence for where an install from GitHub is (web/addons_service.py's
// snapshot): the same words on Add-ons, the Amiibo page and Library's
// install confirmation, whether it is one add-on or a chain of them
// (FPSLocker with SaltyNX and Ultrahand).
(function () {
  "use strict";

  function kb(bytes) {
    return Math.round((bytes || 0) / 1024) + " KB";
  }

  function label(step) {
    return step.name + (step.version ? " " + step.version : "");
  }

  function joined(names) {
    if (names.length <= 1) return names.join("");
    return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
  }

  function describe(d) {
    if (!d) return "";
    const steps = d.steps && d.steps.length ? d.steps : [{ name: d.name || "emuiibo", version: d.version }];
    const current = steps.find((s) => ["checking", "downloading", "adding"].includes(s.state)) || steps[0];
    const of = steps.length > 1 ? " (" + (steps.indexOf(current) + 1) + " of " + steps.length + ")" : "";
    switch (d.state) {
      case "checking":
        return "Asking GitHub for the current " + current.name + "…" + of;
      case "downloading":
        return "Downloading " + label(current) + " from GitHub… " + kb(current.received) +
          (current.total ? " of " + kb(current.total) : "") + of;
      case "adding":
        return label(current) + " downloaded and checked — adding it to your Library…" + of;
      case "done": {
        const names = joined(steps.map(label));
        const many = steps.length > 1;
        return d.queued
          ? names + (many ? " are" : " is") + " in the Queue for this Switch."
          : names + (many ? " are" : " is") + " in your Library.";
      }
      case "failed": {
        const bad = steps.find((s) => s.state === "failed");
        return "Installing " + (bad ? bad.name : "it") + " failed: " + ((bad && bad.error) || d.error || "unknown error") +
          ". Nothing was queued.";
      }
      default:
        return "";
    }
  }

  function busy(d) {
    return Boolean(d && d.state !== "done" && d.state !== "failed");
  }

  window.SwitchAgentAddons = { describe: describe, busy: busy };
})();
