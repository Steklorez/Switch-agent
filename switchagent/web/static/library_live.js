/* Refresh the grid after a scan without navigating or interrupting selection. */
(() => {
  let applied = null;
  async function refresh() {
    try {
      if (document.hidden) return;
      const response = await fetch('/api/scan/status');
      if (!response.ok) return;
      const state = await response.json();
      if (state.running || !state.finished_at || state.finished_at === applied) return;
      // Keep an installation choice, open extras and dialogs intact.
      if (document.querySelector('.select-box:checked, dialog[open], .game-variants[open]')) return;
      const page = await fetch(location.href, {cache: 'no-store'});
      if (!page.ok) return;
      const parsed = new DOMParser().parseFromString(await page.text(), 'text/html');
      const grid = document.querySelector('.library-tiles');
      const next = parsed.querySelector('.library-tiles');
      if (!grid || !next) return;
      if (document.querySelector('.select-box:checked, dialog[open], .game-variants[open]')) return;
      grid.replaceChildren(...next.children);
      document.querySelectorAll('.empty-state').forEach(el => { if (grid.children.length) el.remove(); });
      applied = state.finished_at;
      document.dispatchEvent(new Event('library-updated'));
    } catch (_) { /* Retry on the next poll after a temporary outage. */ }
    finally { setTimeout(refresh, 3000); }
  }
  refresh();
})();
