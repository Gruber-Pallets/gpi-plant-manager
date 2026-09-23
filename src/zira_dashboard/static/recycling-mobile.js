/* Recycling phone controls reuse the existing authorized navigation links. */
(() => {
  'use strict';
  if (!document.documentElement.hasAttribute('data-recycling-mobile')) return;
  const picker = document.getElementById('rm-dashboard');
  document.querySelectorAll('.dash-subnav a').forEach(link => {
    const option = new Option(link.textContent.trim(), link.getAttribute('href'));
    option.selected = link.classList.contains('active');
    picker.add(option);
  });
  picker.addEventListener('change', () => { window.location.href = picker.value; });
  document.getElementById('rm-range').addEventListener('change', event => {
    window.location.href = '/recycling?window=' + encodeURIComponent(event.target.value);
  });
  const nav = document.querySelector('.brand-row nav');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'rm-menu-button';
  button.textContent = 'Menu';
  button.setAttribute('aria-expanded', 'false');
  nav.id = 'rm-app-navigation';
  button.setAttribute('aria-controls', nav.id);
  nav.before(button);
  button.addEventListener('click', () => {
    const open = nav.classList.toggle('rm-menu-open');
    button.setAttribute('aria-expanded', String(open));
  });
})();
