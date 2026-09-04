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

// ---------- Unified light bulb modal ----------
(function () {
  var screenshot = null;   // {file, name, url}
  var currentType = '';
  var activeModal = null;
  var activeOpener = null;
  var activeTab = 'send';
  var sessionGeneration = 0;

  function $(id) { return document.getElementById(id); }

  var tabs = {
    send: $('lightbulb-tab-send'),
    mine: $('lightbulb-tab-mine'),
    news: $('lightbulb-tab-news')
  };
  var panels = {
    send: $('lightbulb-panel-send'),
    mine: $('lightbulb-panel-mine'),
    news: $('lightbulb-panel-news')
  };

  function focusableElements(el) {
    var candidates = Array.prototype.slice.call(el.querySelectorAll(
      'button:not([disabled]):not([hidden]), [href]:not([hidden]), input:not([disabled]):not([hidden]), '
      + 'textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ));
    return candidates.filter(function (candidate) {
      return candidate.getClientRects().length > 0
        && window.getComputedStyle(candidate).visibility !== 'hidden';
    });
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

  function closeLightbulb() {
    sessionGeneration += 1;
    closeModal($('lightbulb-modal'));
    resetSendForm();
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

  function setChooserChrome() {
    var title = $('fb-title');
    var step = $('fb-step-label');
    if (title) title.textContent = 'What are you reporting?';
    if (step) step.textContent = 'Step 1 of 2';
  }

  function setDetailChrome(button) {
    var title = $('fb-title');
    var step = $('fb-step-label');
    var behavior = button ? button.getAttribute('data-behavior') : '';
    if (title) {
      title.textContent = behavior === 'review' ? 'Send to Dale for review' : 'Send feedback';
    }
    if (step) step.textContent = 'Step 2 of 2';
  }

  function resetSendForm() {
    revokeScreenshotUrl();
    screenshot = null;
    var desc = $('fb-desc');
    if (desc) desc.value = '';
    var defaultTypeButton = document.querySelector('.fb-type-btn');
    if (defaultTypeButton) {
      setType(defaultTypeButton.getAttribute('data-type'));
    }
    showTypeStep(false);
    renderScreenshot();
    var submitter = $('fb-submitter');
    if (submitter) submitter.selectedIndex = 0;
    var status = $('fb-status');
    if (status) { status.hidden = true; status.textContent = ''; }
    var submit = $('fb-submit');
    if (submit) submit.disabled = false;
  }

  function setType(type) {
    var selectedButton = null;
    var buttons = document.querySelectorAll('.fb-type-btn');
    Array.prototype.forEach.call(buttons, function (btn) {
      if (btn.getAttribute('data-type') === type) selectedButton = btn;
    });
    if (!selectedButton) return;
    currentType = type;
    Array.prototype.forEach.call(buttons, function (btn) {
      var active = btn.getAttribute('data-type') === currentType;
      btn.classList.toggle('is-active', active);
    });
    var desc = $('fb-desc');
    if (desc) desc.placeholder = selectedButton.getAttribute('data-placeholder') || '';
    setDetailChrome(selectedButton);
    showDetailStep();
  }

  function showDetailStep() {
    var typeStep = $('fb-type-step');
    var detailStep = $('fb-detail-step');
    if (typeStep) typeStep.hidden = true;
    if (detailStep) detailStep.hidden = false;
    var desc = $('fb-desc');
    if (desc) desc.focus();
  }

  function showTypeStep(restoreFocus) {
    var typeStep = $('fb-type-step');
    var detailStep = $('fb-detail-step');
    if (typeStep) typeStep.hidden = false;
    if (detailStep) detailStep.hidden = true;
    setChooserChrome();
    if (restoreFocus === false) return;
    var chosen = document.querySelector(
      '.fb-type-btn[data-type="' + currentType + '"]'
    );
    if (chosen) chosen.focus();
  }

  function isTimeclockPath() {
    return window.location.pathname.indexOf('/timeclock') === 0;
  }

  function setSubmitterMessage(message) {
    var submitter = $('fb-submitter');
    if (!submitter) return;
    submitter.innerHTML = '';
    var option = document.createElement('option');
    option.value = '';
    option.textContent = message;
    submitter.appendChild(option);
  }

  function loadSubmitters() {
    var field = $('fb-submitter-field');
    var submitter = $('fb-submitter');
    if (!field || !submitter) return;
    field.hidden = !isTimeclockPath();
    if (field.hidden) return;
    submitter.disabled = true;
    setSubmitterMessage('Loading names…');
    window.gpiFetch('/api/feedback/submitters')
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (!resp || !resp.ok || !Array.isArray(resp.people)) {
          throw new Error('names unavailable');
        }
        setSubmitterMessage('Choose your name');
        resp.people.forEach(function (person) {
          var option = document.createElement('option');
          option.value = String(person.employee_id);
          option.textContent = person.name;
          submitter.appendChild(option);
        });
        submitter.disabled = false;
      })
      .catch(function () {
        setSubmitterMessage('Names are unavailable. Try again.');
        submitter.disabled = true;
      });
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
    var submitter = $('fb-submitter');
    var submissionSession = sessionGeneration;
    var message = ((desc && desc.value) || '').trim();
    if (status) status.hidden = false;
    if (isTimeclockPath() && (!submitter || !submitter.value)) {
      if (status) status.textContent = 'Choose your name and try again.';
      if (submitter && !submitter.disabled) submitter.focus();
      return;
    }
    if (!message) { if (status) status.textContent = 'Please enter a description.'; return; }
    if (submit) submit.disabled = true;
    if (status) status.textContent = 'Sending…';

    var form = new FormData();
    form.append('type', currentType);
    form.append('description', message);
    form.append('page_url', window.location.href);
    if (isTimeclockPath()) {
      form.append('submitter_employee_id', submitter.value);
    }
    if (screenshot) form.append('screenshot', screenshot.file, screenshot.name);

    var submitUrl = isTimeclockPath() ? '/timeclock/feedback' : '/feedback';
    window.gpiFetch(submitUrl, { method: 'POST', body: form })
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (submissionSession !== sessionGeneration) return;
        if (resp && resp.ok) {
          if (status) status.textContent = 'Thanks — saved and sending it to the app owner.';
          setTimeout(function () {
            if (submissionSession === sessionGeneration) closeLightbulb();
          }, 1200);
        } else if (status) {
          status.textContent = 'Failed: ' + ((resp && resp.error) || 'unknown');
        }
        if (submit) submit.disabled = false;
      })
      .catch(function () {
        if (submissionSession !== sessionGeneration) return;
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
      var typeLabel = it.type_label || 'Unknown';
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

  function loadMyFeedback(refresh) {
    var body = $('fb-view-body');
    if (body) body.textContent = 'Loading…';
    window.gpiFetch('/api/feedback/mine')
      .then(function (r) { return r.json(); })
      .then(renderMyFeedback)
      .catch(function () {
        if (body) body.innerHTML = '<p class="fb-view-empty">Could not load your feedback.</p>';
      });
  }

  function selectTab(name, options) {
    options = options || {};
    if (!tabs[name] || !panels[name]) return;
    activeTab = name;
    Object.keys(tabs).forEach(function (key) {
      var selected = key === name;
      tabs[key].classList.toggle('is-active', selected);
      tabs[key].setAttribute('aria-selected', selected ? 'true' : 'false');
      panels[key].hidden = !selected;
    });
    if (name === 'mine') loadMyFeedback(!!options.refresh);
    if (name === 'news' && window.gpiLightbulbChangelog) {
      window.gpiLightbulbChangelog.show();
    }
    if (options.focus !== false) {
      var target = focusableElements(panels[name])[0];
      (target || tabs[name]).focus();
    }
  }

  function openLightbulb(opener) {
    sessionGeneration += 1;
    resetSendForm();
    loadSubmitters();
    selectTab('send', { focus: false });
    openModal($('lightbulb-modal'), opener, tabs.send);
  }

  window.gpiLightbulb = { open: openLightbulb };

  function wire() {
    Object.keys(tabs).forEach(function (name) {
      var tab = tabs[name];
      if (!tab) return;
      tab.addEventListener('click', function () { selectTab(name); });
      tab.addEventListener('keydown', function (event) {
        if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
        event.preventDefault();
        var names = ['send', 'mine', 'news'];
        var offset = event.key === 'ArrowRight' ? 1 : -1;
        var next = names[(names.indexOf(activeTab) + offset + names.length) % names.length];
        selectTab(next, { focus: false });
        tabs[next].focus();
      });
    });

    var close = $('lightbulb-close'), cancel = $('fb-cancel'), backdrop = $('lightbulb-backdrop');
    [close, cancel, backdrop].forEach(function (el) {
      if (el) el.addEventListener('click', closeLightbulb);
    });

    Array.prototype.forEach.call(document.querySelectorAll('.fb-type-btn'), function (btn) {
      btn.addEventListener('click', function () {
        if (btn.getAttribute('data-behavior') === 'external') {
          closeLightbulb();
          return;
        }
        setType(btn.getAttribute('data-type'));
      });
    });
    var back = $('fb-back');
    if (back) back.addEventListener('click', showTypeStep);

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
      var modal = $('lightbulb-modal');
      if (modal && !modal.hidden) closeLightbulb();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
