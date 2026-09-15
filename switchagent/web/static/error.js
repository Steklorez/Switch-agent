// W3-009: generic "Something went wrong" page. "Copy issue summary" reads
// the already-built, already-sanitized plain-text block server-rendered
// into a hidden <textarea> (see error.html) -- same pattern as
// settings.js's "Copy diagnostics" button, deliberately not reimplemented
// twice. "Report issue" is a plain <a target="_blank"> to GitHub's own
// "new issue" form with the summary pre-filled via ?body= -- nothing here
// ever submits that form or POSTs anywhere itself; the user still reviews
// and explicitly submits on GitHub's own page.
(function () {
  "use strict";

  const btn = document.getElementById("error-copy-summary-btn");
  const status = document.getElementById("error-copy-status");
  const textArea = document.getElementById("error-issue-summary");
  if (!btn || !textArea) return;

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) {
        // fall through to the legacy fallback below
      }
    }
    try {
      textArea.hidden = false;
      textArea.focus();
      textArea.select();
      const ok = document.execCommand("copy");
      textArea.hidden = true;
      return ok;
    } catch (e) {
      textArea.hidden = true;
      return false;
    }
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "";
    try {
      const ok = await copyText(textArea.value);
      status.textContent = ok ? "Copied to clipboard." : "Could not copy automatically -- select the text manually.";
    } finally {
      btn.disabled = false;
    }
  });
})();
