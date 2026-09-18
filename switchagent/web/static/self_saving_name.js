// The friendly-name field that saves itself -- shared by the Devices list
// (devices.js) and the Device Details page (device_detail.js).
//
// Neither page has a Save button next to a device's name any more (user
// request, 2026-09-18): a single text field with its own button is a
// button the user has to notice, aim at and click for something the field
// can just do. Storage mappings ("the paths I set up inside the device")
// deliberately KEEP their explicit Save -- those are a decision, a name is
// just a label.
//
// Shared rather than copy-pasted into both pages -- unlike the one-shot
// fetch() calls those two files do duplicate, this is a small state
// machine (debounce timer, last-saved value, fading status) and two copies
// of it would drift. The two pages differ only in which URL they post to:
// the list page uses the raw-device_id route it has always used, Device
// Details uses the fingerprint-addressed twin (W3-004 -- that page never
// puts a raw, serial-bearing device_id in the DOM at all).
(function () {
  "use strict";

  const DEBOUNCE_MS = 700;
  const SAVED_VISIBLE_MS = 1800;

  /**
   * @param {HTMLFormElement} form   wrapper holding the input + status span
   * @param {string} url             POST target taking {friendly_name}
   * @param {function(string):void} [onSaved]  called with the saved value --
   *        for a page that renders the name somewhere else too and used to
   *        rely on the old button's post-save page reload to resync it.
   *        An autosaving field cannot reload (it would do so mid-typing).
   */
  function attachSelfSavingName(form, url, onSaved) {
    const input = form.querySelector("input[name=friendly_name]");
    if (!input) return;
    const status = form.querySelector(".device-rename-status");
    let savedValue = input.value;
    let debounceTimer = null;
    let fadeTimer = null;

    function showStatus(text, failed) {
      if (!status) return;
      window.clearTimeout(fadeTimer);
      status.textContent = text;
      status.classList.toggle("failed", Boolean(failed));
      status.classList.add("visible");
      // A failure stays on screen: with no button to re-click, a save
      // nobody can see having failed looks like a silently lost name.
      if (!failed) {
        fadeTimer = window.setTimeout(() => status.classList.remove("visible"), SAVED_VISIBLE_MS);
      }
    }

    async function save() {
      window.clearTimeout(debounceTimer);
      const value = input.value;
      if (value === savedValue) return;  // blur right after a debounced save sends nothing
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // An emptied field is a real action -- "show whatever the device
          // reports itself as" -- so it must reach the server as null, not
          // as an empty string that would render as a blank name.
          body: JSON.stringify({ friendly_name: value || null }),
        });
        if (!res.ok) {
          // savedValue deliberately NOT updated -- the next blur retries.
          showStatus("Not saved", true);
          return;
        }
        savedValue = value;
        showStatus("Saved", false);
        if (onSaved) onSaved(value);
      } catch (e) {
        showStatus("Not saved", true);
      }
    }

    input.addEventListener("input", () => {
      window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(save, DEBOUNCE_MS);
    });
    // Immediately on blur as well -- a name must never be left unsaved just
    // because the field still had focus when the user walked away.
    input.addEventListener("blur", save);
    // No submit button any more, but Enter in a lone text input still
    // submits the form -- that should mean "save now", not navigate.
    form.addEventListener("submit", (evt) => {
      evt.preventDefault();
      save();
    });
  }

  window.SwitchAgent = window.SwitchAgent || {};
  window.SwitchAgent.attachSelfSavingName = attachSelfSavingName;
})();
