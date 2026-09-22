/* Warning presentation never changes assignments or enables a work center. */
(function () {
  'use strict';
  function centersFor(issue) {
    return [...new Set((Array.isArray(issue.centers) ? issue.centers : [issue.center])
      .filter(name => typeof name === 'string' && name))];
  }
  function targetFor(center) {
    return [...document.querySelectorAll('tr[data-loc]')].find(row => row.dataset.loc === center);
  }
  function focusTarget(target) {
    target.scrollIntoView({ block: 'center' });
    target.classList.add('scheduler-warning-target');
    setTimeout(() => target.classList.remove('scheduler-warning-target'), 2000);
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    target.focus({ preventScroll: true });
  }
  function reviewCenter(center) {
    const row = targetFor(center);
    if (!row) return;
    const readonly = window.SCHEDULE_VIEWING_POSTED
      || (window.SCHEDULE_PUBLISHED && window.SCHEDULE_VIEW_MODE === 'posted');
    const picker = row.querySelector('details.sched-dd');
    focusTarget(row);
    if (!readonly && row.dataset.on === 'true' && picker && !picker.closest('[inert]')) {
      picker.open = true;
      picker.querySelector('summary')?.focus({ preventScroll: true });
    }
  }
  function button(label, action) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'scheduler-warning-link';
    el.textContent = label;
    el.addEventListener('click', event => {
      event.stopPropagation();
      action();
    });
    return el;
  }
  function normalizedIssues(issues) {
    const unique = new Map();
    for (const issue of issues || []) {
      if (!issue || typeof issue !== 'object') continue;
      const centers = centersFor(issue);
      const key = JSON.stringify([issue.code, issue.message, issue.person, [...centers].sort()]);
      if (!unique.has(key)) unique.set(key, { ...issue, centers, rejections: [] });
      const entry = unique.get(key);
      for (const reason of issue.rejections || []) {
        if (!entry.rejections.some(r => r.person === reason.person && r.detail === reason.detail)) {
          entry.rejections.push(reason);
        }
      }
    }
    return [...unique.values()];
  }
  function issueItem(issue) {
    const item = document.createElement('li');
    item.className = 'coverage-issue';
    item.dataset.issueCode = issue.code || '';
    const text = document.createElement('span');
    text.textContent = issue.message || 'A work center needs attention.';
    item.append(text);
    if (issue.person && !issue.centers.length) {
      const person = [...document.querySelectorAll('.unscheduled li[data-name]')]
        .find(el => el.dataset.name === issue.person);
      if (person) item.append(button(`Find ${issue.person}`, () => focusTarget(person)));
    }
    if (issue.rejections.length) {
      const details = document.createElement('details');
      details.className = 'coverage-why';
      const summary = document.createElement('summary');
      summary.textContent = 'Why?';
      const reasons = document.createElement('ul');
      issue.rejections.forEach(reason => {
        const li = document.createElement('li');
        li.textContent = `${reason.person}: ${reason.detail}`;
        reasons.append(li);
      });
      details.append(summary, reasons);
      item.append(details);
    }
    return item;
  }
  function render(list, warnings, issues) {
    const fragment = document.createDocumentFragment();
    const persistent = (list.dataset.persistentWarning || '').trim();
    const seenMessages = new Set();
    function plain(text) {
      if (!text || seenMessages.has(text)) return;
      seenMessages.add(text);
      const item = document.createElement('li');
      item.textContent = text;
      if (/training block|day-one pairing/i.test(text)) item.className = 'training-warning';
      fragment.append(item);
    }
    plain(persistent);
    const groups = new Map();
    for (const issue of normalizedIssues(issues)) {
      seenMessages.add(issue.message);
      const key = issue.centers.length ? JSON.stringify([...issue.centers].sort())
        : issue.code === 'person_unplaced' ? 'unassigned' : 'general';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(issue);
    }
    for (const [key, group] of groups) {
      const centers = group[0].centers.filter(targetFor);
      const container = document.createElement('li');
      container.className = 'scheduler-warning-group';
      if (key === 'unassigned') {
        const heading = document.createElement('strong');
        heading.textContent = `Unassigned people (${group.length})`;
        container.append(heading);
      }
      centers.forEach(center => container.append(button(`Review ${center}`, () => reviewCenter(center))));
      const items = document.createElement('ul');
      group.forEach(issue => items.append(issueItem(issue)));
      container.append(items);
      fragment.append(container);
    }
    (warnings || []).forEach(warning => plain(String(warning)));
    list.replaceChildren(fragment);
  }
  window.SchedulerWarnings = { render };
})();
