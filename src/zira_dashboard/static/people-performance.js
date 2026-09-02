(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory;
    return;
  }
  root.createPeoplePerformanceController = factory;
  if (root.document) {
    root.peoplePerformanceController = factory(root.document, root);
    root.peoplePerformanceController.init();
  }
})(typeof window !== "undefined" ? window : globalThis, function (document, windowObject) {
  "use strict";

  var triggerSelector = ".pp-interval-trigger, .pp-interval-shortcut";
  var active = null;
  var pinned = null;
  var selectedAtMs = null;
  var popover = null;
  var timer = null;
  var requestController = null;
  var requestEpoch = 0;
  var listeners = [];
  var initialized = false;
  var destroyed = false;
  var synchronizingScroll = false;
  var suppressFocusOpen = false;
  var navigationSignal = {};
  var warningTrigger = null;
  var warningKey = null;
  var warningPinned = false;
  var warningPanel = null;
  var warningRequestController = null;
  var warningRequestEpoch = 0;
  var warningCheckPromise = null;

  function listen(target, type, callback, options) {
    target.addEventListener(type, callback, options);
    listeners.push([target, type, callback, options]);
  }

  function triggerFor(node) {
    return node && node.closest ? node.closest(triggerSelector) : null;
  }

  function warningTriggerFor(node) {
    return node && node.closest ? node.closest(".pp-warning-trigger") : null;
  }

  function warningActionFor(node) {
    return node && node.closest ? node.closest("[data-pp-warning-action]") : null;
  }

  function warningCloseFor(node) {
    return node && node.closest ? node.closest("[data-pp-warning-close]") : null;
  }

  function countControlFor(node) {
    return node && node.closest ? node.closest("[data-pp-count-filter]") : null;
  }

  function peopleUrl(day, status, attention) {
    var params = ["day=" + encodeURIComponent(day)];
    if (status) params.push("status=" + encodeURIComponent(status));
    if (attention) params.push("attention=1");
    return "/people-performance?" + params.join("&");
  }

  function activateCountFilter(control) {
    var rows = document.getElementById("people-performance-live");
    if (!rows || control.disabled) return;
    var status = rows.dataset.status || "";
    var attention = rows.dataset.attention === "1";
    var pressed = control.getAttribute("aria-pressed") === "true";
    if (control.dataset.ppCountFilter === "status") {
      status = pressed ? "" : control.dataset.filterValue;
    } else if (control.dataset.ppCountFilter === "attention") {
      attention = !pressed;
    }
    windowObject.location.assign(peopleUrl(rows.dataset.day, status, attention));
  }

  function triggerKind(trigger) {
    return trigger && trigger.matches && trigger.matches(".pp-interval-shortcut")
      ? "shortcut"
      : "interval";
  }

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement("div");
    popover.id = "pp-detail-popover";
    popover.className = "pp-detail-popover";
    popover.setAttribute("role", "tooltip");
    popover.hidden = true;
    document.body.appendChild(popover);
    return popover;
  }

  function ensureWarningPanel() {
    if (!warningPanel) warningPanel = document.getElementById("pp-warning-popover");
    return warningPanel;
  }

  function warningActionValue(control) {
    if (!control) return null;
    var datasetValue = control.dataset && control.dataset.ppWarningAction;
    return datasetValue || control.getAttribute("data-pp-warning-action");
  }

  function warningPanelFocusIdentity(panel) {
    var focused = document.activeElement;
    if (!panel || !focused || !panel.contains(focused)) return null;
    if (warningCloseFor(focused)) return {kind: "close"};
    var action = warningActionFor(focused);
    if (action) return {kind: "action", value: warningActionValue(action)};
    return {kind: "panel"};
  }

  function replaceWarningContent(panel, content, fallbackTrigger) {
    var focusIdentity = warningPanelFocusIdentity(panel);
    panel.replaceChildren(content);
    if (!focusIdentity) return;

    var focusTarget = null;
    if (focusIdentity.kind === "close") {
      focusTarget = content.querySelector("[data-pp-warning-close]");
    } else if (focusIdentity.kind === "action" && focusIdentity.value) {
      focusTarget = content.querySelector(
        '[data-pp-warning-action="' + escapeSelectorValue(focusIdentity.value) + '"]'
      );
    }
    if (!focusTarget) focusTarget = content.querySelector("[data-pp-warning-close]");
    if (!focusTarget) focusTarget = fallbackTrigger || warningTrigger;
    if (!focusTarget && panel.focus) {
      panel.setAttribute("tabindex", "-1");
      focusTarget = panel;
    }
    focusWithoutScrolling(focusTarget, true);
  }

  function focusWarningPanelFallback() {
    var panel = ensureWarningPanel();
    if (!panel) return;
    var focusTarget = panel.querySelector("[data-pp-warning-close]");
    // A server response without a close control still has one stable keyboard
    // destination: the persistent warning region itself.
    if (!focusTarget && panel.focus) {
      panel.setAttribute("tabindex", "-1");
      focusTarget = panel;
    }
    focusWithoutScrolling(focusTarget, true);
  }

  function renderWarningMessage(message, busy) {
    var panel = ensureWarningPanel();
    if (!panel) return;
    var content = document.createElement("section");
    var heading = document.createElement("h2");
    var body = document.createElement("p");
    heading.textContent = busy ? "Checking warning" : "Warning details";
    body.textContent = message;
    content.appendChild(heading);
    content.appendChild(body);
    replaceWarningContent(panel, content, warningTrigger);
    panel.hidden = false;
    panel.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function renderWarningError() {
    var panel = ensureWarningPanel();
    if (!panel) return;
    var content = document.createElement("section");
    var message = document.createElement("p");
    var retry = document.createElement("button");
    message.textContent = "Details could not be loaded.";
    retry.type = "button";
    retry.textContent = "Retry";
    retry.setAttribute("data-pp-warning-action", "retry");
    content.appendChild(message);
    content.appendChild(retry);
    replaceWarningContent(panel, content, warningTrigger);
    panel.hidden = false;
    panel.setAttribute("aria-busy", "false");
  }

  function renderWarningCheckFailure() {
    var panel = ensureWarningPanel();
    if (!panel) return;
    var content = document.createElement("section");
    var message = document.createElement("p");
    var retry = document.createElement("button");
    message.textContent = "The check could not finish.";
    retry.type = "button";
    retry.textContent = "Try again";
    retry.setAttribute("data-pp-warning-action", "check_again");
    content.appendChild(message);
    content.appendChild(retry);
    replaceWarningContent(panel, content, warningTrigger);
    panel.hidden = false;
    panel.setAttribute("aria-busy", "false");
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(value, maximum));
  }

  function validTimestamp(value) {
    return Number.isFinite(value) && Math.abs(value) <= 8640000000000000;
  }

  function productionPoints(trigger) {
    if (!trigger || trigger.dataset.productionHover == null) return null;
    try {
      var parsed = JSON.parse(trigger.dataset.productionHover);
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (point) {
        return Array.isArray(point) && point.length === 4 && validTimestamp(point[0]);
      });
    } catch (_error) {
      return [];
    }
  }

  function pointAt(points, atMs) {
    var selected = null;
    points.forEach(function (point) {
      if (
        Array.isArray(point)
        && point.length === 4
        && validTimestamp(point[0])
        && point[0] <= atMs
        && (!selected || point[0] >= selected[0])
      ) {
        selected = point;
      }
    });
    return selected;
  }

  function localTime(atMs) {
    if (!validTimestamp(atMs)) return "Time unavailable";
    try {
      var IntlObject = windowObject.Intl || (typeof Intl !== "undefined" ? Intl : null);
      if (!IntlObject || !IntlObject.DateTimeFormat) return "Time unavailable";
      return new IntlObject.DateTimeFormat("en-US", {
        timeZone: "America/Chicago",
        hour: "numeric",
        minute: "2-digit",
      }).format(new Date(atMs));
    } catch (_error) {
      return "Time unavailable";
    }
  }

  function finiteDatasetNumber(value) {
    if (value == null || value === "") return null;
    var number = Number(value);
    return validTimestamp(number) ? number : null;
  }

  function productionDetail(trigger, requestedAtMs) {
    var points = productionPoints(trigger);
    if (points === null) return null;
    var start = finiteDatasetNumber(trigger.dataset.hoverStartMs);
    var end = finiteDatasetNumber(trigger.dataset.hoverEndMs);
    var validBounds = start !== null && end !== null && end >= start;
    var atMs = null;
    if (validBounds) {
      if (!Number.isFinite(requestedAtMs) || requestedAtMs >= end) atMs = end;
      else if (requestedAtMs <= start) atMs = start;
      else atMs = clamp(Math.round(requestedAtMs / 60000) * 60000, start, end);
    } else if (Number.isFinite(requestedAtMs)) {
      atMs = Math.round(requestedAtMs / 60000) * 60000;
    } else if (end !== null) {
      atMs = end;
    } else if (points.length) {
      points.forEach(function (point) {
        if (atMs === null || point[0] > atMs) atMs = point[0];
      });
    } else if (start !== null) {
      atMs = start;
    }
    var point = pointAt(points, atMs);
    var time = localTime(atMs);
    var production = point && Number.isFinite(point[1]) && Number.isFinite(point[2])
      ? Number(point[1]).toFixed(1) + " / " + Number(point[2]).toFixed(1)
      : "N/A";
    var uptime = point && Number.isFinite(point[3])
      ? Math.round(point[3]) + "%"
      : "N/A";
    return {
      atMs: atMs,
      text: time + "\nProduction: " + production + "\nUptime " + uptime,
    };
  }

  function position(trigger) {
    if (!trigger || !popover) return;
    var box = trigger.getBoundingClientRect();
    var tip = popover.getBoundingClientRect();
    var viewportTop = windowObject.scrollY + 8;
    var viewportBottom = windowObject.scrollY + windowObject.innerHeight - 8;
    var minLeft = windowObject.scrollX + 8;
    var maxLeft = windowObject.scrollX + windowObject.innerWidth - tip.width - 8;
    var left = box.left + windowObject.scrollX + box.width / 2 - tip.width / 2;
    var below = box.bottom + windowObject.scrollY + 8;
    var above = box.top + windowObject.scrollY - tip.height - 8;
    var top = below;

    if (below + tip.height > viewportBottom && above >= viewportTop) top = above;
    top = clamp(top, viewportTop, Math.max(viewportTop, viewportBottom - tip.height));
    popover.style.left = clamp(left, minLeft, Math.max(minLeft, maxLeft)) + "px";
    popover.style.top = top + "px";
  }

  function positionWarning(trigger) {
    var panel = ensureWarningPanel();
    if (!trigger || !panel) return;
    var box = trigger.getBoundingClientRect();
    var viewportTop = windowObject.scrollY + 8;
    var viewportBottom = windowObject.scrollY + windowObject.innerHeight - 8;
    var below = box.bottom + windowObject.scrollY + 8;
    var spaceBelow = Math.max(0, viewportBottom - below);
    var spaceAbove = Math.max(
      0,
      box.top + windowObject.scrollY - 8 - viewportTop
    );
    panel.style.maxHeight = "";
    var naturalHeight = panel.getBoundingClientRect().height;
    var useAbove = naturalHeight > spaceBelow && spaceAbove > spaceBelow;
    var availableHeight = useAbove ? spaceAbove : spaceBelow;
    panel.style.maxHeight = availableHeight + "px";
    var detail = panel.getBoundingClientRect();
    var minLeft = windowObject.scrollX + 8;
    var maxLeft = windowObject.scrollX + windowObject.innerWidth - detail.width - 8;
    var left = box.left + windowObject.scrollX + box.width / 2 - detail.width / 2;
    var above = box.top + windowObject.scrollY - detail.height - 8;
    var top = useAbove ? above : below;
    top = clamp(top, viewportTop, Math.max(viewportTop, viewportBottom - detail.height));
    panel.style.left = clamp(left, minLeft, Math.max(minLeft, maxLeft)) + "px";
    panel.style.top = top + "px";
  }

  function hideMarker(trigger) {
    var marker = trigger && trigger.querySelector
      ? trigger.querySelector(".pp-hover-marker")
      : null;
    if (marker) marker.classList.remove("is-visible");
  }

  function updateMarker(trigger, atMs) {
    var marker = trigger && trigger.querySelector
      ? trigger.querySelector(".pp-hover-marker")
      : null;
    if (!marker) return;
    var start = finiteDatasetNumber(trigger.dataset.hoverStartMs);
    var end = finiteDatasetNumber(trigger.dataset.hoverEndMs);
    if (start === null || end === null || end < start || !Number.isFinite(atMs)) {
      hideMarker(trigger);
      return;
    }
    var percent = end > start ? 100 * (atMs - start) / (end - start) : 100;
    marker.style.left = clamp(percent, 0, 100) + "%";
    marker.classList.add("is-visible");
  }

  function open(trigger, shouldPin, requestedAtMs) {
    if (!trigger) return;
    var tip = ensurePopover();
    if (active && active !== trigger) {
      hideMarker(active);
      active.setAttribute("aria-expanded", "false");
      active.removeAttribute("aria-describedby");
    }
    active = trigger;
    pinned = shouldPin ? trigger : null;
    var precise = productionDetail(trigger, requestedAtMs);
    if (precise) {
      selectedAtMs = precise.atMs;
      tip.textContent = precise.text;
      updateMarker(trigger, precise.atMs);
    } else {
      selectedAtMs = null;
      hideMarker(trigger);
      tip.textContent = trigger.dataset.detail || trigger.getAttribute("aria-label") || "";
    }
    tip.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-describedby", tip.id);
    position(trigger);
  }

  function focusWithoutScrolling(element, suppressDetails) {
    if (!element || !element.focus) return;
    suppressFocusOpen = Boolean(suppressDetails);
    try {
      try {
        element.focus({preventScroll: true});
      } catch (_error) {
        element.focus();
      }
    } finally {
      suppressFocusOpen = false;
    }
  }

  function close(restoreFocus) {
    var previous = pinned || active;
    if (active) {
      hideMarker(active);
      active.setAttribute("aria-expanded", "false");
      active.removeAttribute("aria-describedby");
    }
    active = null;
    pinned = null;
    selectedAtMs = null;
    if (popover) popover.hidden = true;
    if (restoreFocus) focusWithoutScrolling(previous, true);
  }

  function leftTrigger(trigger, relatedTarget) {
    return relatedTarget !== trigger && !(trigger.contains && trigger.contains(relatedTarget));
  }

  function escapeSelector(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function escapeSelectorValue(value) {
    return escapeSelector(value);
  }

  function warningTriggerForKey(key) {
    if (!key) return null;
    return document.querySelector(
      '.pp-warning-trigger[data-warning-key="' + escapeSelectorValue(key) + '"]'
    );
  }

  function abortWarningRequest() {
    if (warningRequestController) warningRequestController.abort();
    warningRequestController = null;
  }

  function announceAction(message) {
    var status = document.getElementById("pp-action-status");
    if (status && status.textContent !== message) status.textContent = message;
  }

  function loadWarningDetail(key, trigger, announceResult) {
    var panel = ensureWarningPanel();
    var rows = document.getElementById("people-performance-live");
    var fetcher = windowObject.gpiFetch || (
      windowObject.fetch ? windowObject.fetch.bind(windowObject) : null
    );
    if (destroyed || !panel || !rows || !key || !fetcher) {
      if (!destroyed) renderWarningError();
      return Promise.resolve(false);
    }

    warningRequestEpoch += 1;
    var epoch = warningRequestEpoch;
    abortWarningRequest();
    var AbortControllerType = windowObject.AbortController;
    warningRequestController = typeof AbortControllerType === "function"
      ? new AbortControllerType()
      : null;
    var signal = warningRequestController ? warningRequestController.signal : undefined;
    var url = "/people-performance/warnings/" + encodeURIComponent(key)
      + "?day=" + encodeURIComponent(rows.dataset.day);

    return Promise.resolve(fetcher(url, {
      cache: "no-store",
      redirect: "follow",
      signal: signal,
    })).then(function (response) {
      if (epoch !== warningRequestEpoch || destroyed) return null;
      if (
        response.redirected
        || response.status === 401
        || response.status === 403
        || !response.ok
        || responseHeader(response) !== "warning-detail"
      ) {
        throw new Error("Warning detail request failed");
      }
      return response.text();
    }).then(function (html) {
      if (html === null || epoch !== warningRequestEpoch || destroyed) return false;
      var parsed = new windowObject.DOMParser().parseFromString(html, "text/html");
      var content = parsed.getElementById("pp-warning-panel-content");
      if (!content) throw new Error("Warning detail response was incomplete");
      replaceWarningContent(panel, content, trigger || warningTrigger);
      panel.hidden = false;
      panel.setAttribute("aria-busy", "false");
      if (trigger) positionWarning(trigger);
      if (announceResult) {
        var state = content.dataset
          ? content.dataset.warningState
          : content.getAttribute("data-warning-state");
        announceAction(
          state === "cleared" ? "Issue cleared." : "The warning is still active."
        );
      }
      return true;
    }).catch(function () {
      if (epoch !== warningRequestEpoch || destroyed) return false;
      renderWarningError();
      if (trigger) positionWarning(trigger);
      return false;
    }).finally(function () {
      if (epoch === warningRequestEpoch) warningRequestController = null;
    });
  }

  function closeWarning(restoreFocus) {
    var previous = warningTrigger;
    warningRequestEpoch += 1;
    abortWarningRequest();
    if (warningTrigger) warningTrigger.setAttribute("aria-expanded", "false");
    warningTrigger = null;
    warningKey = null;
    warningPinned = false;
    var panel = ensureWarningPanel();
    if (panel) {
      panel.hidden = true;
      panel.setAttribute("aria-busy", "false");
    }
    if (restoreFocus) focusWithoutScrolling(previous, true);
  }

  function detachWarningForRefresh() {
    warningRequestEpoch += 1;
    abortWarningRequest();
    if (warningTrigger) warningTrigger.setAttribute("aria-expanded", "false");
    warningTrigger = null;
    warningKey = null;
    warningPinned = false;
  }

  function previewWarning(trigger) {
    if (warningPinned) return;
    if (warningTrigger && warningTrigger !== trigger) {
      warningTrigger.setAttribute("aria-expanded", "false");
    }
    warningTrigger = trigger;
    warningKey = trigger.dataset.warningKey;
    trigger.setAttribute("aria-expanded", "true");
    renderWarningMessage(trigger.dataset.warningSummary, false);
    positionWarning(trigger);
  }

  function pinWarning(trigger) {
    if (warningPinned && warningTrigger === trigger) {
      closeWarning(true);
      return Promise.resolve(false);
    }
    if (warningTrigger && warningTrigger !== trigger) closeWarning(false);
    warningPinned = true;
    warningTrigger = trigger;
    warningKey = trigger.dataset.warningKey;
    trigger.setAttribute("aria-expanded", "true");
    renderWarningMessage("Loading warning details…", true);
    positionWarning(trigger);
    return loadWarningDetail(warningKey, trigger, false);
  }

  function pinWarningReplacement(trigger, key, announceResult) {
    if (warningTrigger && warningTrigger !== trigger) {
      warningTrigger.setAttribute("aria-expanded", "false");
    }
    warningTrigger = trigger;
    warningKey = key;
    warningPinned = true;
    trigger.setAttribute("aria-expanded", "true");
    var panel = ensureWarningPanel();
    if (panel) {
      panel.hidden = false;
      panel.setAttribute("aria-busy", "true");
    }
    positionWarning(trigger);
    return loadWarningDetail(key, trigger, Boolean(announceResult));
  }

  function checkWarningAgain(button) {
    if (warningCheckPromise) return warningCheckPromise;
    button.disabled = true;
    button.textContent = "Checking…";
    var checkingKey = warningKey;
    warningCheckPromise = refreshRows().then(function (refreshed) {
      if (destroyed) return false;
      if (!warningPinned || warningKey !== checkingKey) return false;
      if (!refreshed) {
        renderWarningCheckFailure();
        announceAction("The check could not finish.");
        return false;
      }
      var replacement = warningTriggerForKey(checkingKey);
      var detailPromise = replacement
        ? pinWarningReplacement(replacement, checkingKey, true)
        : loadWarningDetail(checkingKey, null, true);
      return detailPromise.then(function (loaded) {
        if (
          !loaded
          && !destroyed
          && warningPinned
          && warningKey === checkingKey
        ) {
          announceAction("The check could not finish.");
        }
        return loaded;
      });
    }).finally(function () {
      warningCheckPromise = null;
    });
    return warningCheckPromise;
  }

  function selectorFor(kind, key) {
    var className = kind === "shortcut" ? ".pp-interval-shortcut" : ".pp-interval-trigger";
    return className + '[data-interval-key="' + escapeSelector(key) + '"]';
  }

  function restoredTrigger(kind, key) {
    if (!key) return null;
    var exact = document.querySelector(selectorFor(kind, key));
    if (exact) return exact;
    return document.querySelector(
      '[data-interval-key="' + escapeSelector(key) + '"]'
    );
  }

  function managerControlFor(node) {
    return node && node.closest ? node.closest("[data-pp-control-key]") : null;
  }

  function restoredManagerControl(key) {
    if (!key) return null;
    return document.querySelector(
      '[data-pp-control-key="' + escapeSelector(key) + '"]'
    );
  }

  function horizontalScrollLeft() {
    var viewports = document.querySelectorAll(".pp-horizontal-scroll");
    return viewports.length ? viewports[0].scrollLeft : 0;
  }

  function syncHorizontalScroll(value, source) {
    if (synchronizingScroll) return;
    synchronizingScroll = true;
    try {
      Array.prototype.forEach.call(
        document.querySelectorAll(".pp-horizontal-scroll"),
        function (viewport) {
          if (viewport !== source && viewport.scrollLeft !== value) viewport.scrollLeft = value;
        }
      );
    } finally {
      synchronizingScroll = false;
    }
  }

  function captureState() {
    var focused = triggerFor(document.activeElement);
    var managerControl = managerControlFor(document.activeElement);
    var focusedWarning = warningTriggerFor(document.activeElement);
    return {
      scrollX: windowObject.scrollX,
      scrollY: windowObject.scrollY,
      horizontalScroll: horizontalScrollLeft(),
      focusKey: focused ? focused.dataset.intervalKey : null,
      focusKind: triggerKind(focused),
      managerControlKey: managerControl ? managerControl.dataset.ppControlKey : null,
      managerControlValue: managerControl && managerControl.dataset.ppControlKey === "day"
        ? managerControl.value
        : null,
      managerControlChecked: managerControl && managerControl.dataset.ppControlKey === "attention"
        ? managerControl.checked
        : null,
      pinnedKey: pinned ? pinned.dataset.intervalKey : null,
      pinnedKind: triggerKind(pinned),
      pinnedAtMs: pinned ? selectedAtMs : null,
      warningKey: warningPinned ? warningKey : null,
      warningFocused: Boolean(warningPinned && focusedWarning === warningTrigger),
    };
  }

  function restoreState(state) {
    var focusTarget = restoredTrigger(state.focusKind, state.focusKey);
    var pinTarget = restoredTrigger(state.pinnedKind, state.pinnedKey);
    var managerControl = restoredManagerControl(state.managerControlKey);
    var restoredWarning = warningTriggerForKey(state.warningKey);

    if (managerControl && state.managerControlKey === "day") {
      managerControl.value = state.managerControlValue;
    }
    if (managerControl && state.managerControlKey === "attention") {
      managerControl.checked = state.managerControlChecked;
    }

    if (managerControl) focusWithoutScrolling(managerControl);
    else if (focusTarget) focusWithoutScrolling(focusTarget);
    if (pinTarget) open(pinTarget, true, state.pinnedAtMs);
    else if (focusTarget) open(focusTarget, false);
    else close(false);
    if (state.warningKey) {
      if (restoredWarning) {
        pinWarningReplacement(restoredWarning, state.warningKey, false);
        if (state.warningFocused) focusWithoutScrolling(restoredWarning, true);
      } else {
        var displacedWarningFocus = state.warningFocused
          ? document.activeElement
          : null;
        warningTrigger = null;
        warningKey = state.warningKey;
        warningPinned = true;
        renderWarningMessage("Loading warning details…", true);
        loadWarningDetail(state.warningKey, null, false).then(function (loaded) {
          if (
            loaded
            && state.warningFocused
            && document.activeElement === displacedWarningFocus
          ) {
            focusWarningPanelFallback();
          }
        });
      }
    }
    syncHorizontalScroll(state.horizontalScroll, null);
    windowObject.scrollTo(state.scrollX, state.scrollY);
  }

  function livePage() {
    var page = document.querySelector('.pp-page[data-today="1"]');
    if (page && page.dataset.pollDisabled === "1") return null;
    return page;
  }

  function setBusy(rows, busy) {
    if (rows && rows.setAttribute) rows.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function abortRequest() {
    if (requestController) requestController.abort();
    requestController = null;
  }

  function safeFullNavigation(response, target) {
    requestEpoch += 1;
    abortRequest();
    setBusy(document.getElementById("people-performance-live"), false);
    var location = windowObject.location;
    var redirectTarget = response && response.redirected && response.url
      ? response.url
      : target;
    if (redirectTarget && location && location.assign) location.assign(redirectTarget);
    else if (location && location.reload) location.reload();
    else if (location) location.href = redirectTarget || location.href;
    throw navigationSignal;
  }

  function responseHeader(response) {
    if (!response.headers || typeof response.headers.get !== "function") return null;
    return response.headers.get("X-People-Performance-Response");
  }

  function refreshRows() {
    var page = livePage();
    var rows = document.getElementById("people-performance-live");
    if (destroyed || !page || !rows || document.visibilityState === "hidden") {
      return Promise.resolve(false);
    }
    if (warningCheckPromise) return warningCheckPromise;

    requestEpoch += 1;
    var epoch = requestEpoch;
    abortRequest();
    var AbortControllerType = windowObject.AbortController;
    requestController = typeof AbortControllerType === "function"
      ? new AbortControllerType()
      : null;
    var signal = requestController ? requestController.signal : undefined;
    var rowsUrl = page.dataset.rowsUrl || "/people-performance/rows";
    var url = rowsUrl + "?day=" + encodeURIComponent(rows.dataset.day)
      + (rows.dataset.status ? "&status=" + encodeURIComponent(rows.dataset.status) : "")
      + "&attention=" + encodeURIComponent(rows.dataset.attention || "0");
    var fetcher = windowObject.gpiFetch || (
      windowObject.fetch ? windowObject.fetch.bind(windowObject) : null
    );
    if (!fetcher) return Promise.resolve(false);

    setBusy(rows, true);
    return Promise.resolve(fetcher(url, {
      cache: "no-store",
      redirect: "follow",
      signal: signal,
    })).then(function (response) {
      if (epoch !== requestEpoch || destroyed) return null;
      if (response.redirected) safeFullNavigation(response);
      if (response.status === 401 || response.status === 403) safeFullNavigation(response);
      if (!response.ok) throw new Error("People Performance refresh failed");
      var marker = responseHeader(response);
      if (marker !== "rows") safeFullNavigation(response);
      return response.text();
    }).then(function (html) {
      if (html === null || epoch !== requestEpoch || destroyed) return false;
      var parsed = new windowObject.DOMParser().parseFromString(html, "text/html");
      var replacement = parsed.getElementById("people-performance-live");
      var responseKind = replacement && (
        replacement.dataset.responseKind
        || replacement.getAttribute("data-response-kind")
      );
      if (!replacement || responseKind !== "people-performance-rows") {
        safeFullNavigation(null);
      }
      if (
        replacement.dataset.day !== rows.dataset.day
        || replacement.dataset.isToday !== "1"
      ) {
        var filterParams = [];
        if (rows.dataset.status) {
          filterParams.push("status=" + encodeURIComponent(rows.dataset.status));
        }
        if (rows.dataset.attention === "1") filterParams.push("attention=1");
        var todayTarget = "/people-performance"
          + (filterParams.length ? "?" + filterParams.join("&") : "");
        safeFullNavigation(null, todayTarget);
      }

      // Capture after every await and immediately before replacing the DOM so
      // interactions made while the request was in flight win.
      var state = captureState();
      close(false);
      detachWarningForRefresh();
      setBusy(replacement, false);
      rows.replaceWith(replacement);
      restoreState(state);
      var status = document.getElementById("pp-live-status");
      if (status) status.textContent = "Updated through " + replacement.dataset.asOf;
      return true;
    }).catch(function (error) {
      if (error === navigationSignal || epoch !== requestEpoch || destroyed) return false;
      var status = document.getElementById("pp-live-status");
      if (status) status.textContent = "Update paused — showing the last good view";
      return false;
    }).finally(function () {
      if (epoch !== requestEpoch) return;
      requestController = null;
      setBusy(document.getElementById("people-performance-live"), false);
    });
  }

  function scheduleRefresh() {
    if (!livePage()) return;
    timer = windowObject.setInterval(refreshRows, 30000);
  }

  function onPointerOver(event) {
    var warning = warningTriggerFor(event.target);
    var coarsePointer = windowObject.matchMedia
      && windowObject.matchMedia("(pointer: coarse)").matches;
    if (warning && !warningPinned && !coarsePointer) {
      previewWarning(warning);
      return;
    }
    var trigger = triggerFor(event.target);
    if (trigger && !pinned) open(trigger, false);
  }

  function onPointerMove(event) {
    var trigger = triggerFor(event.target);
    if (!trigger || pinned || productionPoints(trigger) === null) return;
    var box = trigger.getBoundingClientRect();
    var fraction = box.width > 0
      ? clamp((event.clientX - box.left) / box.width, 0, 1)
      : 1;
    var start = finiteDatasetNumber(trigger.dataset.hoverStartMs);
    var end = finiteDatasetNumber(trigger.dataset.hoverEndMs);
    var requestedAtMs = start !== null && end !== null && end >= start
      ? start + fraction * (end - start)
      : null;
    open(trigger, false, requestedAtMs);
  }

  function onPointerOut(event) {
    var warning = warningTriggerFor(event.target);
    var panel = ensureWarningPanel();
    var leftWarning = warning && warning === warningTrigger;
    var leftPanel = panel && panel.contains(event.target);
    var relatedInside = (
      warningTrigger && warningTrigger.contains && warningTrigger.contains(event.relatedTarget)
    ) || (panel && panel.contains(event.relatedTarget));
    if (!warningPinned && (leftWarning || leftPanel) && !relatedInside) {
      closeWarning(false);
      return;
    }
    var trigger = triggerFor(event.target);
    if (trigger && trigger === active && !pinned && leftTrigger(trigger, event.relatedTarget)) {
      close(false);
    }
  }

  function onFocusIn(event) {
    var warning = warningTriggerFor(event.target);
    if (warning && !warningPinned && !suppressFocusOpen) {
      previewWarning(warning);
      return;
    }
    var trigger = triggerFor(event.target);
    if (trigger && !pinned && !suppressFocusOpen) open(trigger, false);
  }

  function onFocusOut(event) {
    var warning = warningTriggerFor(event.target);
    var panel = ensureWarningPanel();
    var leftWarning = warning && warning === warningTrigger;
    var leftPanel = panel && panel.contains(event.target);
    var relatedInside = (
      warningTrigger && warningTrigger.contains && warningTrigger.contains(event.relatedTarget)
    ) || (panel && panel.contains(event.relatedTarget));
    if (!warningPinned && (leftWarning || leftPanel) && !relatedInside) {
      closeWarning(false);
      return;
    }
    var trigger = triggerFor(event.target);
    if (trigger && trigger === active && !pinned && leftTrigger(trigger, event.relatedTarget)) {
      close(false);
    }
  }

  function onClick(event) {
    var countControl = countControlFor(event.target);
    if (countControl) {
      event.preventDefault();
      activateCountFilter(countControl);
      return;
    }
    var warningAction = warningActionFor(event.target);
    if (warningAction) {
      var action = warningActionValue(warningAction);
      if (action === "retry") {
        event.preventDefault();
        renderWarningMessage("Loading warning details…", true);
        return loadWarningDetail(warningKey, warningTrigger, false);
      }
      if (action === "check_again") {
        event.preventDefault();
        return checkWarningAgain(warningAction);
      }
      return;
    }
    if (warningCloseFor(event.target)) {
      event.preventDefault();
      closeWarning(true);
      return;
    }
    var warning = warningTriggerFor(event.target);
    if (warning) {
      event.preventDefault();
      return pinWarning(warning);
    }
    var trigger = triggerFor(event.target);
    if (!trigger) return;
    event.preventDefault();
    if (trigger === active && pinned === trigger) close(false);
    else open(trigger, true, trigger === active ? selectedAtMs : null);
  }

  function onPointerDown(event) {
    var panel = ensureWarningPanel();
    if (warningKey || warningTrigger) {
      if (!warningTriggerFor(event.target) && !(panel && panel.contains(event.target))) {
        closeWarning(false);
      }
    }
    if (!pinned || triggerFor(event.target)) return;
    if (popover && popover.contains(event.target)) return;
    close(false);
  }

  function onKeyDown(event) {
    if (event.key !== "Escape") return;
    var panel = ensureWarningPanel();
    if ((warningKey || warningTrigger) && panel && !panel.hidden) {
      closeWarning(true);
      return;
    }
    if (active) close(Boolean(pinned));
  }

  function onVisibilityChange() {
    if (document.visibilityState !== "visible" || !livePage()) return undefined;
    return refreshRows();
  }

  function onFilterChange(event) {
    var target = event && event.target;
    var control = target && target.closest
      ? target.closest("[data-pp-auto-submit]")
      : null;
    if (!control || !control.form) return;
    if (typeof control.form.requestSubmit === "function") control.form.requestSubmit();
    else if (typeof control.form.submit === "function") control.form.submit();
  }

  function onDocumentScroll(event) {
    var target = event.target;
    if (target && target.matches && target.matches(".pp-horizontal-scroll")) {
      syncHorizontalScroll(target.scrollLeft, target);
    }
    if (active) position(active);
    if (warningTrigger && warningPanel && !warningPanel.hidden) positionWarning(warningTrigger);
  }

  function onViewportChange() {
    if (active) position(active);
    if (warningTrigger && warningPanel && !warningPanel.hidden) positionWarning(warningTrigger);
  }

  function init() {
    if (initialized || destroyed) return;
    initialized = true;
    ensurePopover();
    ensureWarningPanel();
    listen(document, "pointerover", onPointerOver);
    listen(document, "pointermove", onPointerMove);
    listen(document, "pointerout", onPointerOut);
    listen(document, "focusin", onFocusIn);
    listen(document, "focusout", onFocusOut);
    listen(document, "click", onClick);
    listen(document, "pointerdown", onPointerDown, true);
    listen(document, "keydown", onKeyDown);
    listen(document, "visibilitychange", onVisibilityChange);
    listen(document, "change", onFilterChange);
    listen(document, "scroll", onDocumentScroll, true);
    listen(windowObject, "scroll", onViewportChange, true);
    listen(windowObject, "resize", onViewportChange);
    scheduleRefresh();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    requestEpoch += 1;
    abortRequest();
    warningRequestEpoch += 1;
    abortWarningRequest();
    if (timer) windowObject.clearInterval(timer);
    timer = null;
    listeners.forEach(function (entry) {
      entry[0].removeEventListener(entry[1], entry[2], entry[3]);
    });
    listeners = [];
    close(false);
    closeWarning(false);
    if (popover && popover.remove) popover.remove();
    popover = null;
  }

  return {
    init: init,
    refreshRows: refreshRows,
    destroy: destroy,
  };
});
