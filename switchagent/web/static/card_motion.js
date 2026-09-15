/* One frame per pointer event, cached layout, no idle animation loop. */
(() => {
  const grid = document.querySelector('.library-tiles');
  if (!grid) return;
  let cards = [...grid.children].filter(el => el.matches('.game-group, .card'));
  cards.forEach(card => card.classList.add('motion-card'));
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const control = document.getElementById('card-motion-mode');
  const status = document.getElementById('card-motion-status');
  let mode = 'auto';
  try { mode = localStorage.getItem('card-motion-mode') || 'auto'; } catch (_) {}
  if (!['auto', 'on', 'off'].includes(mode)) mode = 'auto';
  const isEnabled = () => mode === 'on' || (mode === 'auto' && !reduced.matches);
  function updateMode() {
    clear(); geometry = null;
    grid.dataset.motionEnabled = String(isEnabled());
    if (control) control.value = mode;
    if (status) status.textContent = isEnabled() ? 'Move the mouse across a cover' :
      mode === 'off' ? 'Animation disabled' : 'Disabled by system motion preference — select On to enable';
  }
  let geometry = null, frame = 0, pointer = null, lastPaint = 0;
  const active = new Set();
  // How far from a card's own edge it still reacts at all (partial tilt/
  // light, full strength only right at the card itself, dx=dy=0) -- a
  // second, cheap lever on top of throttling: this scales the AREA (not
  // just the radius) of how many neighboring cards react to one cursor
  // position at once, on a large grid, without changing the hovered
  // card's own effect at all.
  const PROXIMITY_RADIUS_PX = 60;
  // ~30fps: plenty smooth for a slow, hand-driven tilt (the difference
  // from 60fps is only visible on a fast, sharp mouse flick across many
  // cards at once) and halves the per-active-card DOM-write/compositor
  // work a full 60fps cap would otherwise do on every animation frame.
  const PAINT_INTERVAL_MS = 32;
  function reset(card) {
    card.classList.remove('is-hovered');
    ['--light', '--tilt-x', '--tilt-y', '--lift'].forEach(key => card.style.removeProperty(key));
    card.style.removeProperty('will-change');
  }
  function clear() {
    cancelAnimationFrame(frame); frame = 0; pointer = null;
    active.forEach(reset); active.clear();
  }
  function paint() {
    frame = 0;
    if (!isEnabled() || !pointer) return;
    const now = performance.now();
    if (now - lastPaint < PAINT_INTERVAL_MS) { frame = requestAnimationFrame(paint); return; }
    lastPaint = now;
    if (!geometry) geometry = cards.map(card => ({card, rect: card.getBoundingClientRect()}));
    const next = new Set();
    for (const {card, rect} of geometry) {
      if (rect.bottom < 0 || rect.top > innerHeight) continue;
      const x = pointer.x - rect.left, y = pointer.y - rect.top;
      const dx = Math.max(-x, x - rect.width, 0), dy = Math.max(-y, y - rect.height, 0);
      const proximity = Math.max(0, 1 - Math.hypot(dx, dy) / PROXIMITY_RADIUS_PX);
      if (!proximity) continue;
      const hovered = dx === 0 && dy === 0;
      const nx = Math.max(-.5, Math.min(.5, x / rect.width - .5));
      const ny = Math.max(-.5, Math.min(.5, y / rect.height - .5));
      card.style.setProperty('--light-x', `${x}px`);
      card.style.setProperty('--light-y', `${y}px`);
      card.style.setProperty('--light', String(proximity * (hovered ? 1 : .45)));
      card.style.setProperty('--tilt-x', `${-ny * (hovered ? 4 : .5) * proximity}deg`);
      card.style.setProperty('--tilt-y', `${nx * (hovered ? 4 : .5) * proximity}deg`);
      card.style.setProperty('--lift', `${-(hovered ? 7 : 1) * proximity}px`);
      card.classList.toggle('is-hovered', hovered);
      // Promote to its own GPU layer only while actually animating (never
      // for the whole grid at once -- that would trade CPU cost for
      // standing GPU memory instead) -- removed again the instant a card
      // leaves proximity, in reset() above.
      if (!active.has(card)) card.style.willChange = 'transform, opacity';
      next.add(card);
    }
    active.forEach(card => { if (!next.has(card)) reset(card); });
    active.clear(); next.forEach(card => active.add(card));
  }
  grid.addEventListener('pointermove', event => {
    if (!isEnabled() || event.pointerType === 'touch') return;
    pointer = {x: event.clientX, y: event.clientY};
    if (!frame) frame = requestAnimationFrame(paint);
  }, {passive: true});
  grid.addEventListener('pointerleave', clear);
  grid.addEventListener('pointercancel', clear);
  grid.addEventListener('toggle', () => { geometry = null; clear(); }, true);
  const invalidate = () => { clear(); geometry = null; };
  document.addEventListener('library-updated', () => {
    invalidate();
    cards = [...grid.children].filter(el => el.matches('.game-group, .card'));
    cards.forEach(card => card.classList.add('motion-card'));
  });
  window.addEventListener('scroll', invalidate, {passive: true});
  window.addEventListener('resize', invalidate, {passive: true});
  window.addEventListener('blur', clear);
  document.addEventListener('visibilitychange', clear);
  reduced.addEventListener('change', updateMode);
  if (control) control.addEventListener('change', () => {
    mode = control.value;
    try { localStorage.setItem('card-motion-mode', mode); } catch (_) {}
    updateMode();
  });
  updateMode();
  new ResizeObserver(() => { geometry = null; }).observe(grid);
})();
