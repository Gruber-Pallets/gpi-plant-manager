// Live balance + in-flight calc for the time-off request wizard.
//
// Updates the balance panel as type, date(s), and time(s) change.
// Disables submit if the request exceeds available_practical (only for
// types that require allocation; Custom-Hours-style types skip the
// check). The balance numbers themselves come pre-rendered from the
// server into window.__TIME_OFF_BALANCES__; this file only does math
// and DOM updates — no network calls.
//
// Shape semantics (matches routes/kiosk_time_off.py):
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
  var sizeEl = document.getElementById("request-size");
  var remainEl = document.getElementById("balance-remaining");
  var submitBtn = document.getElementById("submit-btn");

  if (!typeSel || !availEl || !sizeEl || !remainEl || !submitBtn) return;

  function timeStrToFloat(s) {
    if (!s) return null;
    var parts = s.split(":");
    return parseInt(parts[0], 10) + parseInt(parts[1] || "0", 10) / 60.0;
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

  function recalc() {
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

    if (!requiresAlloc) {
      availEl.textContent = "Unpaid · no balance required";
    } else if (bal) {
      availEl.textContent = bal.available.toFixed(2) + " " + bal.unit +
        " (" + bal.pending.toFixed(2) + " pending)";
    } else {
      availEl.textContent = "—";
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
    sizeEl.textContent = requestSize > 0
      ? requestSize.toFixed(2) + " " + unit
      : "—";

    if (!requiresAlloc) {
      remainEl.textContent = "—";
      submitBtn.disabled = false;
    } else if (bal) {
      var remaining = bal.available_practical - requestSize;
      remainEl.textContent = remaining.toFixed(2) + " " + bal.unit;
      submitBtn.disabled = (requestSize > bal.available_practical);
    } else {
      remainEl.textContent = "—";
      submitBtn.disabled = true;
    }
  }

  [typeSel, dateFrom, dateTo, timeA, timeB].forEach(function (el) {
    if (el) {
      el.addEventListener("change", recalc);
      el.addEventListener("input", recalc);
    }
  });
  recalc();
})();
