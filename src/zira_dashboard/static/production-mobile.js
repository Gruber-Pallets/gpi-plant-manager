/* Grid editing stops on phones, but the work center picker remains available. */
(() => {
  if (!document.documentElement.hasAttribute('data-production-mobile')) return;
  const picker = document.getElementById('wc-picker');
  if (picker) picker.addEventListener('change', () => {
    if (!matchMedia('(max-width: 760px)').matches) return;
    const next = new URL('/wc/' + picker.value, location.origin);
    const day = new URLSearchParams(location.search).get('day');
    if (day) next.searchParams.set('day', day);
    location.href = next.pathname + next.search;
  });
})();
