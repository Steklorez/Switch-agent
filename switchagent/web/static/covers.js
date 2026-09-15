/* Poll queue state; hidden images must never use lazy loading. */
(async () => {
  let images = [...document.querySelectorAll('[data-cover-id]')];
  const text = document.getElementById('cover-status-text');
  const retry = document.getElementById('cover-retry');
  if (!text) return;
  if (!images.length) text.parentElement.hidden = true;
  let timer, imageError = false;
  // Restore known images immediately, independently of metadata/search requests.
  const cacheKey = 'switchagent-ready-covers';
  let known = new Set();
  try { known = new Set(JSON.parse(sessionStorage.getItem(cacheKey) || '[]')); } catch (_) {}
  function saveKnown() {
    try { sessionStorage.setItem(cacheKey, JSON.stringify([...known])); } catch (_) {}
  }
  function loadImage(image, restored = false) {
    const id = image.dataset.coverId.toUpperCase();
    image.dataset.requested = 'true';
    image.onload = () => { image.hidden = false; known.add(id); saveKnown(); };
    image.onerror = () => {
      image.hidden = true;
      known.delete(id); saveKnown();
      delete image.dataset.requested;
      if (!restored) {
        imageError = true;
        text.textContent = 'Could not display a cached cover. Retry covers.';
        retry.hidden = false;
      }
    };
    image.src = `/api/covers/${encodeURIComponent(id)}`;
    if (image.complete && image.naturalWidth) image.hidden = false;
  }
  for (const image of images) {
    if (known.has(image.dataset.coverId.toUpperCase())) loadImage(image, true);
  }
  async function json(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }
  async function poll() {
    try {
      const state = await json('/api/covers/status');
      if (!state.enabled) {
        known.clear(); saveKnown();
        for (const image of images) image.hidden = true;
        text.textContent = 'Covers disabled — enable background covers in Settings.';
        retry.hidden = true;
        return;
      }
      const ready = new Set(state.ready);
      for (const image of images) {
        if (!ready.has(image.dataset.coverId.toUpperCase()) || image.dataset.requested) continue;
        loadImage(image);
      }
      text.textContent = `Covers: ${state.ready.length} / ${state.total} · ${state.phase}` +
        (state.error ? ` · ${state.error}` : imageError ? ' · Some images could not be displayed' : '');
      retry.hidden = state.running || (!state.error && !imageError && state.ready.length === state.total);
      if (state.running) timer = setTimeout(poll, 1500);
    } catch (error) {
      text.textContent = `Cover loading failed: ${error.message}`;
      retry.hidden = false;
    }
  }
  async function start(force = false) {
    clearTimeout(timer);
    retry.hidden = true;
    text.textContent = 'Starting background cover search…';
    if (force) {
      imageError = false;
      for (const image of images.filter(i => i.hidden)) delete image.dataset.requested;
    }
    try {
      await json(`/api/covers/refresh?retry=${force}`, {method: 'POST'});
      await poll();
    } catch (error) {
      text.textContent = `Could not start cover search: ${error.message}`;
      retry.hidden = false;
    }
  }
  retry.addEventListener('click', () => start(true));
  document.addEventListener('library-updated', () => {
    images = [...document.querySelectorAll('[data-cover-id]')];
    text.parentElement.hidden = images.length === 0;
    for (const image of images) {
      if (!image.dataset.requested && known.has(image.dataset.coverId.toUpperCase())) loadImage(image, true);
    }
    start();
  });
  await start();
})();
