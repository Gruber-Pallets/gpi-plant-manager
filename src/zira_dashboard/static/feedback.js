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
  var attachments = [];   // {file, name, url}
  var currentType = 'bug';

  function $(id) { return document.getElementById(id); }

  function openModal(el) {
    if (!el) return;
    el.hidden = false;
    document.documentElement.style.overflow = 'hidden';
  }
  function closeModal(el) {
    if (!el) return;
    el.hidden = true;
    document.documentElement.style.overflow = '';
  }

  function resetSendForm() {
    revokeAttachmentUrls();
    attachments = [];
    currentType = 'bug';
    var desc = $('fb-desc');
    if (desc) { desc.value = ''; desc.placeholder = PLACEHOLDERS.bug; }
    setType('bug');
    renderAttachments();
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

  function addFiles(fileList) {
    Array.prototype.forEach.call(fileList || [], function (file) {
      if (!file) return;
      var isImage = /^image\//.test(file.type);
      attachments.push({
        file: file,
        name: file.name || (isImage ? 'screenshot.png' : 'file'),
        url: isImage ? URL.createObjectURL(file) : null,
      });
    });
    renderAttachments();
  }

  function revokeAttachmentUrls() {
    attachments.forEach(function (att) {
      if (att.url) URL.revokeObjectURL(att.url);
    });
  }

  function renderAttachments() {
    var box = $('fb-attachments');
    if (!box) return;
    box.innerHTML = '';
    attachments.forEach(function (att, idx) {
      var chip = document.createElement('span');
      chip.className = 'fb-attachment-chip';
      if (att.url) {
        var img = document.createElement('img');
        img.src = att.url; img.alt = '';
        chip.appendChild(img);
      }
      var label = document.createElement('span');
      label.textContent = att.name;
      chip.appendChild(label);
      var rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'fb-attachment-remove';
      rm.setAttribute('aria-label', 'Remove attachment');
      rm.textContent = '×';
      rm.addEventListener('click', function () {
        if (att.url) URL.revokeObjectURL(att.url);
        attachments.splice(idx, 1);
        renderAttachments();
      });
      chip.appendChild(rm);
      box.appendChild(chip);
    });
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
    attachments.forEach(function (att) { form.append('files', att.file, att.name); });

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

  function statusLabel(s) {
    return { open: 'Open', done: 'Done', rejected: 'Rejected' }[s] || 'Open';
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
      pill.className = 'fb-status-pill is-' + (it.status || 'open');
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

  function openView() {
    var body = $('fb-view-body');
    if (body) body.textContent = 'Loading…';
    openModal($('fb-view-modal'));
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
        openModal($('fb-modal'));
        var d = $('fb-desc');
        if (d) d.focus();
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
      fileInput.addEventListener('change', function () { addFiles(fileInput.files); fileInput.value = ''; });
    }

    var desc = $('fb-desc');
    if (desc) {
      desc.addEventListener('paste', function (event) {
        var items = (event.clipboardData && event.clipboardData.items) || [];
        var imgs = [];
        Array.prototype.forEach.call(items, function (it) {
          if (it.kind === 'file' && /^image\//.test(it.type)) {
            var f = it.getAsFile();
            if (f) imgs.push(f);
          }
        });
        if (imgs.length) { event.preventDefault(); addFiles(imgs); }
      });
    }

    var submit = $('fb-submit');
    if (submit) submit.addEventListener('click', submitFeedback);

    document.addEventListener('keydown', function (event) {
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
