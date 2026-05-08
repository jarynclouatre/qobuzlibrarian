// CSP-safe htmx hooks. These used to live as hx-on:* attributes inline, but
// htmx evaluates those via `new Function(...)`, which the page CSP (script-src
// 'self' 'unsafe-inline', no 'unsafe-eval') rejects.

(function () {
  var REDUCE = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Collapse an element's height and fade it out, then run `done`. A row
  // leaving the page — a hidden artist group, a dismissed flash — closes the
  // gap smoothly instead of blinking out. Measuring the current height first
  // (auto can't be transitioned) and forcing a reflow before zeroing makes the
  // transition actually animate. Idempotent; a no-op under reduced-motion.
  function collapse(el, done) {
    if (!el || el.dataset.qlCollapsing === "1") return;
    el.dataset.qlCollapsing = "1";
    if (REDUCE) { if (done) done(); return; }
    var h = el.getBoundingClientRect().height;
    el.style.overflow = "hidden";
    el.style.height = h + "px";
    el.style.transition =
      "height 280ms ease 40ms, opacity 200ms ease, " +
      "margin 280ms ease 40ms, padding 280ms ease 40ms";
    void el.offsetHeight;  // force reflow so the start values stick
    el.style.opacity = "0";
    el.style.height = "0px";
    el.style.marginTop = "0";
    el.style.marginBottom = "0";
    el.style.paddingTop = "0";
    el.style.paddingBottom = "0";
    if (done) setTimeout(done, 320);
  }

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

  // Hiding a whole artist returns an empty body (the group is removed via an
  // outerHTML swap). The button carries a swap delay so the node lingers long
  // enough to collapse it as it leaves. A partial hide returns the trimmed
  // group instead — let that one swap normally (it just fades via CSS).
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var t = evt.detail && evt.detail.target;
    if (!t || !t.matches || !t.matches("details[data-artist]")) return;
    if ((evt.detail.serverResponse || "").trim() !== "") return;
    collapse(t);
    // The server's qlHidden fires before this delayed swap, so it recounts
    // with the group still present; htmx's afterSwap doesn't bubble up from a
    // node being removed. Re-announce once the group is actually gone so the
    // summary, submit count and empty-state settle on the right numbers.
    setTimeout(function () {
      document.body.dispatchEvent(new CustomEvent("qlHidden"));
    }, 360);
  });

  // Whole-library scan triggers are plain POST→redirect forms with no htmx
  // feedback, so a slow start on a big library used to look frozen and invite a
  // second click (which stacked a duplicate hours-long scan). Disable the
  // button and show it working; the disable happens after the submit fires so
  // the request still goes out and the navigation isn't cancelled.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-busy-submit]")) return;
    var b = form.querySelector("button[type=submit]");
    if (!b || b.disabled) return;
    setTimeout(function () {
      b.disabled = true;
      b.classList.add("btn-disabled");
      b.innerHTML =
        '<span class="loading loading-spinner loading-sm"></span> Starting…';
    }, 0);
  });

  // Light/dark toggle. The pre-paint init in base.html sets the initial theme;
  // this flips it on click, remembers the choice, and keeps the mobile
  // address-bar colour in step.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("#theme-toggle");
    if (!btn) return;
    var next = document.documentElement.getAttribute("data-theme") === "winter"
      ? "night" : "winter";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ql-theme", next); } catch (e) { /* private mode */ }
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute("content", next === "winter" ? "#ffffff" : "#1d232a");
  });

  // Flash banners. Every server-rendered alert driven by a one-shot query flag
  // (?saved=1, ?error=…) used to stick to the URL forever — refreshing or
  // sharing the page re-rendered the same banner. Strip the known flash params
  // after first paint, and fade banners out so they don't dominate the screen.
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
  function fade(el) { collapse(el, function () { if (el.parentNode) el.remove(); }); }
  function autoDismissFlashes() {
    // Success/info banners fade on their own; errors/warnings stay until the
    // user dismisses them (Esc) — except in the toast corner, where a decline
    // or error from a queue action would otherwise sit there forever.
    document.querySelectorAll("[data-flash].alert-success, [data-flash].alert-info")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 6000); });
    document.querySelectorAll("#download-toast [data-flash]")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 8000); });
  }
  // htmx swaps insert flashes after the initial DOMContentLoaded run (download
  // toast, search-error swap, etc.) — re-scan after any swap so those fade too.
  // The collapse marker makes re-scanning idempotent.
  document.addEventListener("htmx:afterSwap", autoDismissFlashes);
  // Hide every flash regardless of severity. The job page calls this from the
  // SSE done/progress handler — once the job moves on, the "queued" banner is
  // stale even though it's a success.
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
