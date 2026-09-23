/* Keep navigation sourced from the page's existing authorized links. */
(() => {
  'use strict';
  const root = document.documentElement;
  const subnav = document.querySelector('.dash-subnav');
  if (!subnav || root.hasAttribute('data-tv-theme')) return;
  root.setAttribute('data-performance-mobile', '');
  // Recycling already shipped this same phone navigation with its range controls.
  if (root.hasAttribute('data-recycling-mobile')) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'pm-navigation';
  const label = document.createElement('label');
  label.textContent = 'Performance dashboard';
  const picker = document.createElement('select');
  picker.id = 'pm-dashboard';
  picker.setAttribute('aria-label', 'Performance dashboard');
  subnav.querySelectorAll('a').forEach(link => {
    const option = new Option(link.textContent.trim(), link.getAttribute('href'));
    option.selected = link.classList.contains('active');
    picker.add(option);
  });
  label.appendChild(picker);
  wrapper.appendChild(label);
  subnav.after(wrapper);
  picker.addEventListener('change', () => { window.location.href = picker.value; });
  const nav = document.querySelector('.brand-row nav');
  if (!nav) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pm-menu-button';
  button.textContent = 'Menu';
  button.setAttribute('aria-expanded', 'false');
  nav.id = 'pm-app-navigation';
  button.setAttribute('aria-controls', nav.id);
  nav.before(button);
  button.addEventListener('click', () => {
    button.setAttribute('aria-expanded', String(nav.classList.toggle('pm-menu-open')));
  });
})();
