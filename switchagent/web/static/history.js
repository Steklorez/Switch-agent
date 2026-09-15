// History page: UI-003 user verification of a DONE_UNVERIFIED transport
// outcome. Never touches the transport outcome itself (that column is
// immutable, see db.set_user_verified_outcome()'s docstring) -- this only
// posts the user's own separate confirmation of what actually happened on
// the console, then reloads to show the updated state (a discrete,
// occasional action, not a live-progress path -- same reload-is-fine
// convention as queue.js's worker pause/resume button).
(function () {
  "use strict";

  document.addEventListener("click", async (evt) => {
    const btn = evt.target.closest("[data-verify-action]");
    if (!btn) return;

    const action = btn.dataset.verifyAction; // "SUCCESS" | "FAILED" | "clear"
    const historyId = btn.dataset.historyId;
    const outcome = action === "clear" ? null : action;

    btn.disabled = true;
    try {
      const res = await fetch(`/api/history/${historyId}/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(`Could not update verification: ${data.detail || res.status}`);
        btn.disabled = false;
        return;
      }
      window.location.reload();
    } catch (e) {
      alert("Request failed: " + e);
      btn.disabled = false;
    }
  });
})();
