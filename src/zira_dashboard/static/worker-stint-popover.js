/* Compact worker-stint details shared by Recycling and New dashboards. */
(function (root, makeWorkerStintPopover) {
  "use strict";
  if (typeof module === 'object' && module.exports) {
    module.exports = makeWorkerStintPopover;
    return;
  }
  makeWorkerStintPopover(root.document, root).init();
})(typeof window !== 'undefined' ? window : globalThis, function (document, windowObject) {
  "use strict";
  var selector = '.worker-stint-hitarea, .vworker-stint-hitarea';
  var active = null;
  var pinned = false;
  var popover = null;

  function triggerFrom(target) {
    return target && target.closest ? target.closest(selector) : null;
  }

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement('div');
    popover.id = 'worker-stint-popover';
    popover.className = 'worker-stint-popover';
    popover.setAttribute('role', 'tooltip');
    popover.hidden = true;
    document.body.appendChild(popover);
    return popover;
  }

  function position(trigger) {
    var box = trigger.getBoundingClientRect();
    var tip = popover.getBoundingClientRect();
    var minLeft = windowObject.scrollX + 8;
    var maxLeft = windowObject.scrollX + windowObject.innerWidth - tip.width - 8;
    var left = box.left + windowObject.scrollX + (box.width / 2) - (tip.width / 2);
    var top = box.top + windowObject.scrollY - tip.height - 8;
    left = Math.max(minLeft, Math.min(left, maxLeft));
    if (top < windowObject.scrollY + 8) top = box.bottom + windowObject.scrollY + 8;
    popover.style.left = left + 'px';
    popover.style.top = top + 'px';
  }

  function open(trigger, shouldPin) {
    var tip = ensurePopover();
    if (active && active !== trigger) active.removeAttribute('aria-describedby');
    active = trigger;
    pinned = Boolean(shouldPin);
    tip.textContent = trigger.dataset.stintDetail;
    tip.hidden = false;
    trigger.setAttribute('aria-describedby', tip.id);
    position(trigger);
  }

  function close(returnFocus) {
    var previous = active;
    if (previous) previous.removeAttribute('aria-describedby');
    if (popover) popover.hidden = true;
    active = null;
    pinned = false;
    if (returnFocus && previous && previous.focus) previous.focus();
  }

  function leftTrigger(trigger, relatedTarget) {
    return relatedTarget !== trigger
      && !(trigger.contains && trigger.contains(relatedTarget));
  }

  function init() {
    document.addEventListener('pointerover', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger && !pinned) open(trigger, false);
    });
    document.addEventListener('pointerout', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger && trigger === active && !pinned && leftTrigger(trigger, event.relatedTarget)) close(false);
    });
    document.addEventListener('focusin', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger) open(trigger, false);
    });
    document.addEventListener('focusout', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger && trigger === active && !pinned && leftTrigger(trigger, event.relatedTarget)) close(false);
    });
    document.addEventListener('click', function (event) {
      var trigger = triggerFrom(event.target);
      if (!trigger) return;
      event.preventDefault();
      if (trigger === active && pinned) close(false);
      else open(trigger, true);
    });
    document.addEventListener('pointerdown', function (event) {
      if (!active || triggerFrom(event.target)) return;
      if (popover && popover.contains(event.target)) return;
      close(false);
    }, true);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && active) close(true);
    });
    windowObject.addEventListener('resize', function () {
      if (active) position(active);
    });
    windowObject.addEventListener('scroll', function () {
      if (active) position(active);
    }, true);
  }

  return {init: init, open: open, close: close};
});
