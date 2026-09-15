(() => {
  const control = document.getElementById('card-motion-preference');
  if (!control) return;
  try {
    const value = localStorage.getItem('card-motion-mode');
    control.value = ['auto', 'on', 'off'].includes(value) ? value : 'auto';
  } catch (_) {}
  control.addEventListener('change', () => {
    try { localStorage.setItem('card-motion-mode', control.value); } catch (_) {}
  });
})();
