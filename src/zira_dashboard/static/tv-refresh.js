/* GPI Plant Manager — resilient TV / dashboard auto-refresh.
 *
 * The plant-floor TVs reload every 60s to keep production data live. A plain
 * `location.reload()` (or `<meta http-equiv="refresh">`) re-requests the whole
 * document; if the backend is momentarily unreachable — a Railway deploy swap,
 * a network blip — the browser replaces the dashboard with the edge's
 * "upstream error" page and the screen goes black until the next reload.
 *
 * Instead we PROBE first: hit the tiny /tv/ping endpoint (an empty 204 behind
 * the same auth middleware as the dashboards — probing the full page URL
 * downloaded the entire document twice per reload), and only reload when it
 * comes back OK. On any failure we leave the last good frame on screen and
 * re-check sooner, so a brief outage is invisible on the floor and the board
 * snaps back on its own once the backend is answering again.
 */
(function () {
  "use strict";

  var NORMAL_MS = 60000; // steady-state refresh cadence (matches the old reload)
  var RETRY_MS = 7000; // while the backend is unreachable, re-check faster

  // Off-network TVs authenticate via a ?device= token in the page URL; carry
  // it onto the probe so /tv/ping authenticates exactly like the page does
  // (session cookie, IP allowlist, or device token).
  var PROBE_URL = (function () {
    var url = "/tv/ping";
    try {
      var device = new URLSearchParams(window.location.search).get("device");
      if (device) url += "?device=" + encodeURIComponent(device);
    } catch (e) {
      // Very old engine without URLSearchParams — fall back to the bare probe.
    }
    return url;
  })();

  function check() {
    fetch(PROBE_URL, {
      method: "GET",
      cache: "no-store",
      redirect: "manual",
    })
      .then(function (resp) {
        // resp.ok => a real 2xx, safe to reload into. A 5xx edge error,
        // or a redirect to login (opaqueredirect => ok === false), means
        // "don't reload" — keep the current frame and try again shortly.
        if (resp && resp.ok) {
          window.location.reload();
        } else {
          window.setTimeout(check, RETRY_MS);
        }
      })
      .catch(function () {
        // Network / DNS / edge down — hold the frame and retry.
        window.setTimeout(check, RETRY_MS);
      });
  }

  window.setTimeout(check, NORMAL_MS);
})();
