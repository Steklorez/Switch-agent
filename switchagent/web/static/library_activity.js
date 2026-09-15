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
        const status = row.querySelector('.card-status');
        if (status) {
          status.className = 'card-status status-' + entry.status.toLowerCase();
          status.textContent = entry.status.replaceAll('_', ' ');
        }
        // "On Switch" -- DBI's own live Installed Games report (see
        // services._library_entry_view's confirmed_on_device), a second,
        // independent badge next to the job-history status above, never
        // merged into it.
        const body = row.querySelector('.card-body, .variant-body');
        if (body) {
          let onDevice = body.querySelector('.status-on-device');
          if (entry.confirmed_on_device) {
            if (!onDevice) {
              onDevice = document.createElement(status && status.tagName === 'SPAN' ? 'span' : 'div');
              onDevice.className = 'card-status status-on-device';
              onDevice.textContent = 'On Switch';
              body.appendChild(onDevice);
            }
          } else if (onDevice) {
            onDevice.remove();
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
