// Live balance + in-flight calc for the time-off request wizard.
//
// Updates the balance panel as type, date(s), and time(s) change.
// Disables submit if the request exceeds available_practical (only for
// types that require allocation; Custom-Hours-style types skip the
// check). The balance numbers themselves come pre-rendered from the
// server into window.__TIME_OFF_BALANCES__; this file only does math
// and DOM updates — no network calls.
//
// Also raises a non-blocking yellow warning when the user picks a date
// outside the global plant schedule (e.g., Saturday for a Mon-Fri
// plant). Workdays come from window.__WORK_WEEKDAYS__ (server-rendered
// from schedule_store.current().work_weekdays). Not a hard block — an
// employee scheduled for weekend OT may legitimately need to request
// Saturday off — but the warning catches accidental weekend picks.
//
// Shape semantics (matches routes/timeclock_time_off.py):
//   full_day      → request size = business days in [date_from, date_to]
//   late_arrival  → request size = arrival_time - shift_from (hours)
//   early_leave   → request size = shift_to - leave_time (hours)
//   midday_gap    → request size = time_b - time_a (hours)

(function () {
  var root = document.getElementById("time-off-details");
  if (!root) return;
  var shape = root.dataset.shape;
  var shiftFrom = parseFloat(root.dataset.shiftFrom);
  var shiftTo = parseFloat(root.dataset.shiftTo);
  var balances = window.__TIME_OFF_BALANCES__ || {};

  var typeSel = document.getElementById("holiday-status-select");
  var dateFrom = document.getElementById("date-from");
  var dateTo = document.getElementById("date-to");
  var timeA = document.getElementById("time-a");
  var timeB = document.getElementById("time-b");
  var availEl = document.getElementById("balance-available");
  var pendingEl = document.getElementById("balance-pending");
  var sizeEl = document.getElementById("request-size");
  var remainEl = document.getElementById("balance-remaining");
  var submitBtn = document.getElementById("submit-btn");
  var warningEl = document.getElementById("schedule-warning");

  if (!typeSel || !submitBtn) return;
  // availEl/sizeEl/remainEl only exist for full_day; partial-day shapes
  // drop the balance panel entirely (see template guard). The schedule
  // warning still needs to run in both branches.

  function timeStrToFloat(s) {
    if (!s) return null;
    var parts = s.split(":");
    return parseInt(parts[0], 10) + parseInt(parts[1] || "0", 10) / 60.0;
  }

  // Format a day/hour count: drop a trailing ".00" so whole numbers read as
  // integers (15 days, not 15.00 days), but keep real fractions like an
  // accrued 4.17 or a half day 4.5.
  function fmt(n) {
    return (Math.round(n * 100) / 100).toFixed(2).replace(/\.?0+$/, "");
  }

  function businessDaysBetween(a, b) {
    var d1 = new Date(a + "T00:00:00");
    var d2 = new Date(b + "T00:00:00");
    if (d2 < d1) return 0;
    var count = 0;
    var cur = new Date(d1);
    while (cur <= d2) {
      var dow = cur.getDay();
      if (dow !== 0 && dow !== 6) count++;
      cur.setDate(cur.getDate() + 1);
    }
    return count;
  }

  function dateIsWorkday(isoDate) {
    if (!isoDate) return true; // empty input = no warning yet
    var workdays = window.__WORK_WEEKDAYS__ || [0, 1, 2, 3, 4];
    var d = new Date(isoDate + "T00:00:00");
    // JS Date.getDay(): 0=Sun..6=Sat. Convert to 0=Mon..6=Sun.
    var dow = (d.getDay() + 6) % 7;
    return workdays.indexOf(dow) !== -1;
  }

  function checkSchedule() {
    if (!warningEl) return;
    var msgs = [];
    if (shape === "full_day") {
      if (dateFrom && dateFrom.value && !dateIsWorkday(dateFrom.value)) {
        msgs.push("Start date (" + dateFrom.value + ") falls outside the standard schedule.");
      }
      if (dateTo && dateTo.value && dateTo.value !== (dateFrom && dateFrom.value)
          && !dateIsWorkday(dateTo.value)) {
        msgs.push("End date (" + dateTo.value + ") falls outside the standard schedule.");
      }
    } else {
      if (dateFrom && dateFrom.value && !dateIsWorkday(dateFrom.value)) {
        msgs.push(dateFrom.value + " falls outside the standard schedule.");
      }
    }
    if (msgs.length > 0) {
      warningEl.innerHTML =
        "<strong>Heads up — </strong>" + msgs.join(" ") +
        " Submit anyway only if you're sure (e.g. weekend overtime day).";
      warningEl.style.display = "block";
    } else {
      warningEl.style.display = "none";
    }
  }

  function recalc() {
    // Balance panel only renders for full_day. Skip the panel-related
    // math entirely on partial-day shapes so we don't crash on missing
    // DOM elements.
    var hasBalancePanel = !!(availEl && sizeEl && remainEl);

    var hsid = typeSel.value;
    var bal = balances[hsid];
    // typeSel is a <select> for full_day, or a hidden <input> for the
    // three partial-day shapes (which always use the unpaid Custom Hours
    // type — no user picker). Read requires-alloc from the selected
    // option on the SELECT path, or directly from the input's dataset
    // on the hidden-input path. Hidden inputs don't expose `.options`.
    var requiresAlloc;
    if (typeSel.tagName === "SELECT") {
      var selectedOpt = typeSel.options[typeSel.selectedIndex];
      requiresAlloc = selectedOpt
        ? (selectedOpt.dataset.requiresAlloc === "yes")
        : true;
    } else {
      requiresAlloc = (typeSel.dataset.requiresAlloc === "yes");
    }

    if (hasBalancePanel) {
      if (!requiresAlloc) {
        // The type has `requires_allocation=no` in Odoo. That can mean
        // either (a) genuinely unpaid (Custom Hours) or (b) paid but
        // unlimited (e.g. "Paid Time Off" with no allocation cap). The
        // panel doesn't know which without the work_entry_type, so use
        // copy that's accurate in both cases. The user already sees the
        // type name in the dropdown above.
        availEl.textContent = "No allocation tracked";
        if (pendingEl) pendingEl.textContent = "";
      } else if (bal) {
        availEl.textContent = fmt(bal.available) + " " + bal.unit;
        if (pendingEl) pendingEl.textContent = fmt(bal.pending) + " pending";
      } else {
        availEl.textContent = "—";
        if (pendingEl) pendingEl.textContent = "";
      }
    }

    // Pick the unit to display. For full_day with an hour-unit type
    // (e.g., "Unpaid Time Off"), the type's unit wins — we display
    // hours, not days, even though the shape is full_day.
    var typeUnit;
    if (typeSel.tagName === "SELECT") {
      var optForUnit = typeSel.options[typeSel.selectedIndex];
      typeUnit = optForUnit ? optForUnit.dataset.unit : null;
    } else {
      typeUnit = typeSel.dataset.unit;
    }
    var requestSize = 0;
    var unit = bal
      ? bal.unit
      : (shape === "full_day"
          ? (typeUnit === "hour" ? "hours" : "days")
          : "hours");
    if (shape === "full_day") {
      if (dateFrom && dateTo && dateFrom.value && dateTo.value) {
        var days = businessDaysBetween(dateFrom.value, dateTo.value);
        // Hour-unit type used for full-day: convert days → hours
        // (business days × shift hours) so the "This request" panel
        // shows the same unit as the type's allocation.
        if (typeUnit === "hour") {
          requestSize = days * (shiftTo - shiftFrom);
        } else {
          requestSize = days;
        }
      }
    } else {
      var a, b;
      if (shape === "late_arrival") {
        a = shiftFrom;
        b = timeB ? timeStrToFloat(timeB.value) : null;
      } else if (shape === "early_leave") {
        a = timeA ? timeStrToFloat(timeA.value) : null;
        b = shiftTo;
      } else {
        a = timeA ? timeStrToFloat(timeA.value) : null;
        b = timeB ? timeStrToFloat(timeB.value) : null;
      }
      if (a !== null && b !== null && b > a) {
        requestSize = b - a;
      }
    }
    if (hasBalancePanel) {
      sizeEl.textContent = requestSize > 0
        ? fmt(requestSize) + " " + unit
        : "—";

      if (!requiresAlloc) {
        remainEl.textContent = "—";
        submitBtn.disabled = false;
      } else if (bal) {
        var remaining = bal.available_practical - requestSize;
        remainEl.textContent = fmt(remaining) + " " + bal.unit;
        submitBtn.disabled = (requestSize > bal.available_practical);
      } else {
        remainEl.textContent = "—";
        submitBtn.disabled = true;
      }
    }

    checkSchedule();
  }

  [typeSel, dateFrom, dateTo, timeA, timeB].forEach(function (el) {
    if (el) {
      el.addEventListener("change", recalc);
      el.addEventListener("input", recalc);
    }
  });
  recalc();
})();
