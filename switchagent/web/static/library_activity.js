/* Keep selection available after completion; expire the temporary highlight. */
(() => {
  function expire() {
    document.querySelectorAll('[data-recent-until]').forEach(row => {
      row.classList.toggle('recent-transfer', Date.parse(row.dataset.recentUntil) > Date.now());
    });
  }
  async function refresh() {
    expire();
    if (document.hidden) return;
    try {
      const response = await fetch('/api/library');
      if (!response.ok) return;
      const entries = new Map((await response.json()).map(e => [String(e.id), e]));
      document.querySelectorAll('.select-box').forEach(box => {
        const entry = entries.get(box.value);
        if (!entry) return;
        box.disabled = !entry.can_install;
        if (box.disabled && box.checked) {
          box.checked = false;
          box.dispatchEvent(new Event('change', {bubbles: true}));
        }
        const row = box.closest('.card, .variant-row');
        if (!row) return;
        row.dataset.recentUntil = entry.recent_transfer_until || '';
        // "On Switch" -- DBI's own live Installed Games report (see
        // services._library_entry_view's confirmed_on_device) -- is
        // strictly the stronger signal once true, exactly like
        // library.html's own card_status() macro: the raw job-status pill
        // is redundant at that point and is removed, never left stacked
        // next to "On Switch" (a plain generic `.card-status` lookup here
        // used to grab whichever of the two happened to already be in the
        // DOM -- including "On Switch" itself -- and overwrite it with the
        // raw status text, then add a second "On Switch" since the first
        // one no longer matched; that's the stacking bug this replaces).
        const body = row.querySelector('.card-body, .variant-body');
        if (!body) return;
        const rawStatus = body.querySelector('.card-status:not(.status-on-device)');
        let onDevice = body.querySelector('.status-on-device');
        const tag = (rawStatus || onDevice)?.tagName === 'SPAN' ? 'span' : 'div';
        if (entry.confirmed_on_device) {
          if (rawStatus) rawStatus.remove();
          if (!onDevice) {
            onDevice = document.createElement(tag);
            onDevice.className = 'card-status status-on-device';
            onDevice.textContent = 'On Switch';
            body.appendChild(onDevice);
          }
        } else if (entry.hide_unverified_badge) {
          // INSTALLED_UNVERIFIED specifically, with its own target device
          // now disconnected (see services._library_entry_view's own
          // comment) -- no badge beats an unverified positive claim with
          // zero live chance of confirming it right now.
          if (onDevice) onDevice.remove();
          if (rawStatus) rawStatus.remove();
        } else {
          if (onDevice) onDevice.remove();
          if (rawStatus) {
            rawStatus.className = 'card-status status-' + entry.status.toLowerCase();
            rawStatus.textContent = entry.status.replaceAll('_', ' ');
          } else {
            const newStatus = document.createElement(tag);
            newStatus.className = 'card-status status-' + entry.status.toLowerCase();
            newStatus.textContent = entry.status.replaceAll('_', ' ');
            body.appendChild(newStatus);
          }
        }
      });
      expire();
    } catch (_) { /* Preserve selection during temporary network outages. */ }
  }
  setInterval(refresh, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  refresh();
})();
