// CSP-safe htmx hooks. These used to live as hx-on:* attributes inline,
// but htmx evaluates those via `new Function(...)`, which the page CSP
// (script-src 'self' 'unsafe-inline', no 'unsafe-eval') rejects.

// Mark the submit button "Queued" and disable it so a double-click can't
// queue the same album twice. The download endpoint answers 200 even when
// it declines (album already owned, already queued) or errors, so key off
// the genuine "added to queue" success alert rather than the status code —
// on a decline or error htmx re-enables the button on its own.
document.addEventListener("htmx:afterRequest", function (evt) {
  var form = evt.target;
  if (!form || !form.matches || !form.matches("form[data-queue-button]")) return;
  if (!evt.detail || !evt.detail.successful) return;
  var xhr = evt.detail.xhr;
  if (!xhr || xhr.responseText.indexOf("alert-success") === -1) return;
  var b = form.querySelector("button[type=submit]");
  if (!b) return;
  b.disabled = true;
  b.textContent = "Queued";
  b.classList.remove("btn-primary");
  b.classList.add("btn-ghost", "btn-disabled");
});

// Flash banners. Every server-rendered alert that was driven by a one-shot
// query flag (?saved=1, ?approved=1, ?error=…) used to stick to the URL
// forever — refreshing or sharing the page re-rendered the same banner.
// Strip the known flash params from the address bar after first paint, and
// auto-fade success/info banners so they don't dominate the screen.
// Errors and warnings stay until the user dismisses them (Esc).
(function () {
  var FLASH_PARAMS = ["approved", "stale", "saved", "queued", "connected",
                       "unverified", "mode", "error"];
  function cleanFlashUrl() {
    if (typeof URL !== "function" || !history.replaceState) return;
    try {
      var url = new URL(location.href);
      var touched = false;
      FLASH_PARAMS.forEach(function (k) {
        if (url.searchParams.has(k)) { url.searchParams.delete(k); touched = true; }
      });
      if (touched) {
        var qs = url.searchParams.toString();
        history.replaceState(null, "", url.pathname + (qs ? "?" + qs : "") + url.hash);
      }
    } catch (e) { /* malformed URL — leave it alone */ }
  }
  function fade(el) {
    if (!el || el.dataset.flashFading === "1") return;
    el.dataset.flashFading = "1";
    // Animate the height and margins down to 0 alongside the opacity so the
    // content below the banner slides up instead of snapping. Measuring the
    // current height first (auto can't be transitioned) and forcing a reflow
    // before changing to 0 makes the transition actually animate.
    var h = el.getBoundingClientRect().height;
    el.style.overflow = "hidden";
    el.style.height = h + "px";
    el.style.transition =
      "opacity 200ms, height 280ms ease 80ms, " +
      "margin 280ms ease 80ms, padding 280ms ease 80ms";
    void el.offsetHeight;  // force reflow so the start values stick
    el.style.opacity = "0";
    el.style.height = "0px";
    el.style.marginTop = "0";
    el.style.marginBottom = "0";
    el.style.paddingTop = "0";
    el.style.paddingBottom = "0";
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 420);
  }
  function autoDismissFlashes() {
    var els = document.querySelectorAll(
      "[data-flash].alert-success, [data-flash].alert-info");
    els.forEach(function (el) { setTimeout(function () { fade(el); }, 6000); });
  }
  // htmx swaps insert flashes after the initial DOMContentLoaded run
  // (download confirmation toast, search-error swap, etc.) — re-scan
  // after any swap so those fade too. The fade marker (dataset.flashFading)
  // makes re-scanning idempotent, so a whole-document rescan is fine.
  document.addEventListener("htmx:afterSwap", autoDismissFlashes);
  // Hide every flash, regardless of severity. The job page calls this from
  // the SSE done/progress handler — once the job moves on, the "queued"
  // banner is stale even though it's a success.
  window.qlDismissAllFlashes = function () {
    document.querySelectorAll("[data-flash]").forEach(fade);
  };
  cleanFlashUrl();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoDismissFlashes);
  } else {
    autoDismissFlashes();
  }
})();
