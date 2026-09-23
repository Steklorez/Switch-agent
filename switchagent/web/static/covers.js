/* Loads cover images as the background cover job makes them available.
   Hidden images must never use lazy loading. What the job is doing is shown
   by activity.js (the Library page's activity panel), from /api/activity --
   this file only tells it when an image failed to display. */
(async () => {
  let images = [...document.querySelectorAll('[data-cover-id]')];
  const retry = document.getElementById('cover-retry');
  let timer;
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
      if (!restored) document.dispatchEvent(new CustomEvent('cover-image-error'));
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
        return;
      }
      const ready = new Set(state.ready);
      for (const image of images) {
        if (!ready.has(image.dataset.coverId.toUpperCase()) || image.dataset.requested) continue;
        loadImage(image);
      }
      if (state.running) timer = setTimeout(poll, 1500);
    } catch (_) {
      // The activity panel reads the same job state and says what went wrong.
    }
  }
  async function start(force = false) {
    clearTimeout(timer);
    if (force) {
      document.dispatchEvent(new CustomEvent('cover-retry-started'));
      for (const image of images.filter(i => i.hidden)) delete image.dataset.requested;
    }
    try {
      await json(`/api/covers/refresh?retry=${force}`, {method: 'POST'});
      document.dispatchEvent(new Event('activity-poke'));
      await poll();
    } catch (_) {
      document.dispatchEvent(new Event('activity-poke'));
    }
  }
  if (retry) retry.addEventListener('click', () => start(true));
  document.addEventListener('library-updated', () => {
    images = [...document.querySelectorAll('[data-cover-id]')];
    for (const image of images) {
      if (!image.dataset.requested && known.has(image.dataset.coverId.toUpperCase())) loadImage(image, true);
    }
    start();
  });
  await start();
})();
