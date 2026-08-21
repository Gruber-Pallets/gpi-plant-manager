(function () {
  if (!window.gpiFetch) {
    window.gpiFetch = function (url, opts) {
      opts = opts || {};
      if (typeof fetch === 'function') return fetch(url, opts);
      return new Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        xhr.open(opts.method || 'GET', url, true);
        var headers = opts.headers || {};
        Object.keys(headers).forEach(function (name) {
          xhr.setRequestHeader(name, headers[name]);
        });
        xhr.onload = function () {
          var responseText = xhr.responseText || '';
          resolve({
            ok: xhr.status >= 200 && xhr.status < 300,
            status: xhr.status,
            json: function () {
              return Promise.resolve(responseText ? JSON.parse(responseText) : {});
            },
            text: function () { return Promise.resolve(responseText); },
          });
        };
        xhr.onerror = function () { reject(new Error('network error')); };
        xhr.send(opts.body || null);
      });
    };
  }
})();

// ---------- Feedback modal (Send) + View Feedback list ----------
(function () {
  var PLACEHOLDERS = {
    bug: 'What broke, and what did you expect?',
    feature: 'What would you like to see, and why?',
  };
  var screenshot = null;   // {file, name, url}
  var currentType = 'bug';
  var activeModal = null;
  var activeOpener = null;

  function $(id) { return document.getElementById(id); }

  function focusableElements(el) {
    return Array.prototype.slice.call(el.querySelectorAll(
      'button:not([disabled]):not([hidden]), [href], input:not([disabled]):not([hidden]), '
      + 'textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ));
  }

  function openModal(el, opener, preferredFocus) {
    if (!el) return;
    activeModal = el;
    activeOpener = opener || document.activeElement;
    el.hidden = false;
    document.documentElement.style.overflow = 'hidden';
    document.dispatchEvent(new CustomEvent('gpi:feedback-opened'));
    var target = preferredFocus || focusableElements(el)[0];
    if (target) target.focus();
  }

  function closeModal(el) {
    if (!el) return;
    el.hidden = true;
    document.documentElement.style.overflow = '';
    if (activeModal === el) {
      var opener = activeOpener;
      activeModal = null;
      activeOpener = null;
      document.dispatchEvent(new CustomEvent('gpi:feedback-closed'));
      if (opener && typeof opener.focus === 'function') opener.focus();
    }
  }

  function trapFocus(event) {
    if (event.key !== 'Tab' || !activeModal || activeModal.hidden) return;
    var focusable = focusableElements(activeModal);
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    var current = document.activeElement;
    var currentIndex = focusable.indexOf(current);
    if (event.shiftKey && (current === first || currentIndex === -1)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (current === last || currentIndex === -1)) {
      event.preventDefault();
      first.focus();
    }
  }

  function resetSendForm() {
    revokeScreenshotUrl();
    screenshot = null;
    currentType = 'bug';
    var desc = $('fb-desc');
    if (desc) { desc.value = ''; desc.placeholder = PLACEHOLDERS.bug; }
    setType('bug');
    renderScreenshot();
    var status = $('fb-status');
    if (status) { status.hidden = true; status.textContent = ''; }
  }

  function setType(type) {
    currentType = (type === 'feature') ? 'feature' : 'bug';
    Array.prototype.forEach.call(document.querySelectorAll('.fb-type-btn'), function (btn) {
      var active = btn.getAttribute('data-type') === currentType;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    var desc = $('fb-desc');
    if (desc) desc.placeholder = PLACEHOLDERS[currentType];
  }

  function setScreenshot(file) {
    if (!file || !/^image\/(jpeg|png|webp)$/.test(file.type)) return;
    revokeScreenshotUrl();
    screenshot = {
      file: file,
      name: file.name || 'screenshot.jpg',
      url: URL.createObjectURL(file),
    };
    renderScreenshot();
  }

  function revokeScreenshotUrl() {
    if (screenshot && screenshot.url) URL.revokeObjectURL(screenshot.url);
  }

  function renderScreenshot() {
    var box = $('fb-attachments');
    if (!box) return;
    box.innerHTML = '';
    if (!screenshot) return;
    var chip = document.createElement('span');
    chip.className = 'fb-attachment-chip';
    var img = document.createElement('img');
    img.src = screenshot.url; img.alt = '';
    chip.appendChild(img);
    var label = document.createElement('span');
    label.textContent = screenshot.name;
    chip.appendChild(label);
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'fb-attachment-remove';
    remove.setAttribute('aria-label', 'Remove screenshot');
    remove.textContent = '×';
    remove.addEventListener('click', function () {
      revokeScreenshotUrl();
      screenshot = null;
      renderScreenshot();
    });
    chip.appendChild(remove);
    box.appendChild(chip);
  }

  function submitFeedback() {
    var desc = $('fb-desc');
    var status = $('fb-status');
    var submit = $('fb-submit');
    var message = ((desc && desc.value) || '').trim();
    if (status) status.hidden = false;
    if (!message) { if (status) status.textContent = 'Please enter a description.'; return; }
    if (submit) submit.disabled = true;
    if (status) status.textContent = 'Sending…';

    var form = new FormData();
    form.append('type', currentType);
    form.append('description', message);
    form.append('page_url', window.location.href);
    if (screenshot) form.append('screenshot', screenshot.file, screenshot.name);

    window.gpiFetch('/feedback', { method: 'POST', body: form })
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (resp && resp.ok) {
          if (status) status.textContent = 'Thanks — sent!';
          setTimeout(function () { closeModal($('fb-modal')); resetSendForm(); }, 1200);
        } else if (status) {
          status.textContent = 'Failed: ' + ((resp && resp.error) || 'unknown');
        }
        if (submit) submit.disabled = false;
      })
      .catch(function () {
        if (status) status.textContent = 'Network error.';
        if (submit) submit.disabled = false;
      });
  }

  function statusLabel(status) {
    return {
      requested: 'Requested',
      in_progress: 'In Progress',
      completed: 'Completed',
      declined: 'Declined'
    }[status] || 'Requested';
  }

  function renderMyFeedback(data) {
    var body = $('fb-view-body');
    if (!body) return;
    var items = (data && data.items) || [];
    if (!items.length) {
      body.innerHTML = '<p class="fb-view-empty">You haven\'t sent any feedback yet.</p>';
      return;
    }
    body.innerHTML = '';
    items.forEach(function (it) {
      var row = document.createElement('div');
      row.className = 'fb-view-item';
      var main = document.createElement('div');
      main.className = 'fb-view-main';
      var title = document.createElement('div');
      title.className = 'fb-view-title';
      title.textContent = it.title;
      var meta = document.createElement('div');
      meta.className = 'fb-view-meta';
      var typeLabel = it.type === 'feature' ? 'Feature request' : 'Bug';
      meta.textContent = typeLabel + ' · ' + (it.created_at || '').slice(0, 10);
      main.appendChild(title); main.appendChild(meta);
      var pill = document.createElement('span');
      pill.className = 'fb-status-pill is-' + (it.status || 'requested');
      pill.textContent = statusLabel(it.status);
      row.appendChild(main); row.appendChild(pill);
      body.appendChild(row);
    });
    if (data && data.status_available === false) {
      var note = document.createElement('p');
      note.className = 'fb-view-empty';
      note.textContent = 'Status temporarily unavailable.';
      body.appendChild(note);
    }
  }

  function openView(event) {
    var body = $('fb-view-body');
    if (body) body.textContent = 'Loading…';
    openModal(
      $('fb-view-modal'),
      event ? event.currentTarget : null,
      $('fb-view-close')
    );
    window.gpiFetch('/api/feedback/mine')
      .then(function (r) { return r.json(); })
      .then(renderMyFeedback)
      .catch(function () {
        if (body) body.innerHTML = '<p class="fb-view-empty">Could not load your feedback.</p>';
      });
  }

  function wire() {
    var openButtons = document.querySelectorAll('[data-feedback-open]');
    var viewBtn = $('fb-view-open');
    if (!openButtons.length && !viewBtn) return;
    Array.prototype.forEach.call(openButtons, function (openBtn) {
      openBtn.addEventListener('click', function () {
        resetSendForm();
        var d = $('fb-desc');
        openModal($('fb-modal'), openBtn, d);
      });
    });
    if (viewBtn) viewBtn.addEventListener('click', openView);

    var close = $('fb-close'), cancel = $('fb-cancel'), backdrop = $('fb-backdrop');
    [close, cancel, backdrop].forEach(function (el) {
      if (el) el.addEventListener('click', function () { closeModal($('fb-modal')); });
    });
    var vClose = $('fb-view-close'), vBackdrop = $('fb-view-backdrop');
    [vClose, vBackdrop].forEach(function (el) {
      if (el) el.addEventListener('click', function () { closeModal($('fb-view-modal')); });
    });

    Array.prototype.forEach.call(document.querySelectorAll('.fb-type-btn'), function (btn) {
      btn.addEventListener('click', function () { setType(btn.getAttribute('data-type')); });
    });

    var uploadBtn = $('fb-upload-btn'), fileInput = $('fb-file-input');
    if (uploadBtn && fileInput) {
      uploadBtn.addEventListener('click', function () { fileInput.click(); });
      fileInput.addEventListener('change', function () {
        setScreenshot(fileInput.files && fileInput.files[0]);
        fileInput.value = '';
      });
    }

    var desc = $('fb-desc');
    if (desc) {
      desc.addEventListener('paste', function (event) {
        var items = (event.clipboardData && event.clipboardData.items) || [];
        var image = null;
        Array.prototype.forEach.call(items, function (it) {
          if (!image && it.kind === 'file' && /^image\/(jpeg|png|webp)$/.test(it.type)) {
            image = it.getAsFile();
          }
        });
        if (image) { event.preventDefault(); setScreenshot(image); }
      });
    }

    var submit = $('fb-submit');
    if (submit) submit.addEventListener('click', submitFeedback);

    document.addEventListener('keydown', function (event) {
      trapFocus(event);
      if (event.key !== 'Escape') return;
      var m = $('fb-modal'), v = $('fb-view-modal');
      if (m && !m.hidden) closeModal(m);
      if (v && !v.hidden) closeModal(v);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
