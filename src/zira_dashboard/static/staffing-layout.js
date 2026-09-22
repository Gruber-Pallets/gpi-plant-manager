/* Presentation only: keep the original form controls and their state. */
(function () {
  'use strict';
  const wrapper = document.querySelector('[data-scheduler-view]');
  if (!wrapper) return;
  const buttons = Array.from(document.querySelectorAll('[data-scheduler-layout]'));
  const storageKey = 'scheduler-phone-layout';
  function apply(view) {
    wrapper.dataset.schedulerView = view;
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.schedulerLayout === view)));
  }
  let preference = 'cards';
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved === 'cards' || saved === 'table') preference = saved;
  } catch (_) { /* Private browsing and storage restrictions are harmless. */ }
  apply(preference);
  buttons.forEach(button => button.addEventListener('click', () => {
    const view = button.dataset.schedulerLayout;
    if (view !== 'cards' && view !== 'table') return;
    apply(view);
    try { window.localStorage.setItem(storageKey, view); } catch (_) { /* Keep this session's choice. */ }
  }));
})();
