// The UI's behavioural JS lives here, served under script-src 'self'. The page
// CSP carries a per-request nonce rather than 'unsafe-inline', so inline on*
// handlers and htmx's new Function(hx-on:*) are both forbidden — behaviour is
// wired by event delegation (which also reaches htmx-swapped content), and the
// live job/queue streams init on load and re-init after every htmx swap.

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

  // A submit/link carrying data-confirm asks first; declining cancels the
  // action (and stops the form submitting). Replaces inline onclick="return
  // confirm(...)" so script-src needs no 'unsafe-inline'.
  document.addEventListener("click", function (evt) {
    var el = evt.target.closest && evt.target.closest("[data-confirm]");
    if (!el) return;
    if (!window.confirm(el.getAttribute("data-confirm"))) {
      evt.preventDefault();
      evt.stopPropagation();
    }
  });

  // Select-all / select-none for a set of checkboxes. data-check is "all" or
  // "none"; data-check-within is the checkbox selector, scoped to the closest
  // data-check-closest ancestor when present, else document-wide.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-check]");
    if (!btn) return;
    var on = btn.getAttribute("data-check") === "all";
    var sel = btn.getAttribute("data-check-within");
    if (!sel) return;
    var closest = btn.getAttribute("data-check-closest");
    var scope = closest ? btn.closest(closest) : document;
    if (!scope) return;
    scope.querySelectorAll(sel).forEach(function (c) { c.checked = on; });
  });

  // data-reload reloads the page (the lock-busy "Try again" button).
  document.addEventListener("click", function (evt) {
    if (evt.target.closest && evt.target.closest("[data-reload]")) {
      evt.preventDefault();
      location.reload();
    }
  });

  // Show/hide a password field. data-toggle-password is the field's id; the
  // button's label flips between Show and Hide.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-toggle-password]");
    if (!btn) return;
    var f = document.getElementById(btn.getAttribute("data-toggle-password"));
    if (!f) return;
    f.type = f.type === "password" ? "text" : "password";
    btn.textContent = f.type === "password" ? "Show" : "Hide";
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
  // Hide every flash regardless of severity. The job page calls this from the
  // SSE done/progress handler — once the job moves on, the "queued" banner is
  // stale even though it's a success.
  window.qlDismissAllFlashes = function () {
    document.querySelectorAll("[data-flash]").forEach(fade);
  };

  // ── Live job/queue streams ─────────────────────────────────────────────
  // Kept out of the partials htmx swaps in: a nonce can't survive an htmx swap
  // (the fragment's nonce ≠ the live document's), so an inline block in swapped
  // content would be CSP-blocked. These bind on load and re-bind after each
  // swap; a dataset flag stops a node from binding twice.

  function fmtProgress(p, verb) {
    if (p.total > 0) {
      return verb + " " + p.current + " / " + p.total + (p.item ? " · " + p.item : "");
    }
    if (p.phase) return p.phase + (p.item ? " · " + p.item : "");
    return "";
  }

  // Dashboard and queue progress cards: update the sub-line in place, then
  // refresh the card region once the job finishes.
  function wireStreamCard(card) {
    if (card.dataset.sseWired === "1") return;
    card.dataset.sseWired = "1";
    var id = card.dataset.jobId;
    var surface = card.dataset.jobSurface;   // "dashboard" | "queue"
    var status = card.dataset.jobStatus;
    if (!id || !surface) return;
    var progId = (surface === "dashboard" ? "dash-prog-" : "card-prog-") + id;
    var containerId = surface === "dashboard" ? "dashboard-active" : "queue-body";
    var reconnect = surface === "dashboard"
      ? document.getElementById("dash-reconnect-" + id) : null;
    var src = new EventSource("/api/jobs/" + id + "/stream");
    function shut() {
      try { src.close(); } catch (e) {}
      document.removeEventListener("htmx:beforeSwap", onSwap);
    }
    function onSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === containerId) shut();
    }
    document.addEventListener("htmx:beforeSwap", onSwap);
    if (reconnect) {
      src.onopen = function () { reconnect.classList.add("hidden"); };
      src.onerror = function () {
        if (src.readyState !== EventSource.CLOSED) reconnect.classList.remove("hidden");
      };
    }
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      var el = document.getElementById(progId);
      if (!el) return;
      var txt = fmtProgress(p, status === "scanning" ? "Scanning" : (p.phase || "Downloading"));
      if (txt) el.textContent = txt;
    });
    src.addEventListener("done", function () {
      shut();
      if (surface === "dashboard") {
        // Don't yank the user mid-search — if search results are showing, just
        // drop this card and leave the rest alone.
        var results = document.getElementById("search-results");
        if (results && results.children.length) {
          var wrap = document.getElementById("dashboard-active");
          if (wrap) wrap.classList.add("hidden");
          return;
        }
        if (window.htmx) {
          window.htmx.ajax("GET", "/",
            { target: "#dashboard-active", swap: "outerHTML", select: "#dashboard-active" });
        } else { location.reload(); }
      } else if (window.htmx) {
        window.htmx.ajax("GET", "/queue",
          { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
      } else { location.reload(); }
    });
  }

  function initStreamCards() {
    document.querySelectorAll("[data-job-card]").forEach(wireStreamCard);
  }

  // The single-job page (#job-content). data-job-view picks the shape:
  // "progress" = a running/scanning job's live log + progress bar; "review" =
  // the candidate list, which for a still-running library/upgrade/downsample
  // scan also streams in groups as the walk finds them.
  function initJobContent() {
    var jc = document.getElementById("job-content");
    if (!jc || jc.dataset.jobWired === "1") return;
    var view = jc.dataset.jobView;
    var id = jc.dataset.jobId;
    if (!view || !id) return;
    jc.dataset.jobWired = "1";
    if (view === "review") wireReview(id, jc.dataset.scanning === "1");
    else if (view === "progress") wireProgress(id);
  }

  function wireProgress(id) {
    var logEl = document.getElementById("log");
    var card = document.getElementById("progress-card");
    var label = document.getElementById("prog-label");
    var count = document.getElementById("prog-count");
    var bar = document.getElementById("prog-bar");
    var item = document.getElementById("prog-item");
    var activity = document.getElementById("job-activity");
    var reconnect = document.getElementById("sse-reconnect");
    var baseTitle = document.title;
    var titleSet = false;
    var src = new EventSource("/api/jobs/" + id + "/stream");
    src.onmessage = function (e) {
      if (!titleSet) { document.title = "▶ " + baseTitle; titleSet = true; }
      if (logEl) { logEl.appendChild(document.createTextNode(e.data + "\n")); logEl.scrollTop = logEl.scrollHeight; }
    };
    src.onopen = function () { if (reconnect) reconnect.classList.add("hidden"); };
    src.onerror = function () {
      if (reconnect && src.readyState !== EventSource.CLOSED) reconnect.classList.remove("hidden");
    };
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (activity && p.phase && activity.textContent !== p.phase) activity.textContent = p.phase;
      if (card) card.classList.remove("hidden");
      if (label) label.textContent = p.phase || "Working";
      var ct = p.total > 0 ? p.current + " / " + p.total : (p.current ? String(p.current) : "");
      if (p.found > 0) ct += (ct ? " · " : "") + p.found + " with gaps";
      if (count) count.textContent = ct;
      if (bar) { if (p.total > 0) { bar.max = 100; bar.value = Math.round(p.current / p.total * 100); } else { bar.removeAttribute("value"); } }
      if (item) item.textContent = p.item || "";
    });
    src.addEventListener("done", function () {
      src.close();
      document.title = baseTitle;
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (window.htmx) {
        window.htmx.ajax("GET", "/jobs/" + id + "/content", { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
  }

  function wireReview(id, scanning) {
    var form = document.getElementById("review-form");
    if (!form) return;
    var submit = document.getElementById("review-submit");
    var countEl = submit ? submit.querySelector("[data-count]") : null;
    var groupsBox = document.getElementById("review-groups");
    var summaryRow = document.getElementById("review-summary-row");
    var summaryCount = document.querySelector("#review-candidates [data-summary-count]");
    var emptyBox = document.getElementById("review-empty");
    var scanDone = !scanning;

    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function fmtBytes(n) {
      n = Number(n) || 0;
      var units = ["B", "KB", "MB", "GB"];
      for (var i = 0; i < units.length; i++) {
        if (n < 1024) return (units[i] === "B" ? Math.floor(n) : n.toFixed(1)) + units[i];
        n /= 1024;
      }
      return n.toFixed(1) + "TB";
    }
    var dsTotal = document.querySelector("[data-downsample-total]");
    function refresh() {
      var checked = form.querySelectorAll(".cb:checked").length;
      if (countEl) countEl.textContent = checked ? " " + checked : "";
      if (submit) submit.disabled = !scanDone || checked === 0;
      if (dsTotal) {
        var bytes = 0;
        form.querySelectorAll(".cb:checked").forEach(function (cb) {
          bytes += parseInt(cb.dataset.saving || "0", 10) || 0;
        });
        dsTotal.textContent = bytes > 0 ? " · ~" + fmtBytes(bytes) + " reclaimable" : "";
      }
      form.querySelectorAll("[data-hide]").forEach(function (btn) {
        var det = btn.closest("details");
        var lbl = btn.querySelector("[data-hide-label]");
        if (!det || !lbl) return;
        var total = det.querySelectorAll(".cb").length;
        var picked = det.querySelectorAll(".cb:checked").length;
        btn.classList.toggle("hidden", total > 0 && picked === total);
        lbl.textContent = picked ? "Hide other " + (total - picked) : "Hide all " + total;
      });
      var albums = groupsBox ? groupsBox.querySelectorAll(".cb").length : 0;
      var artists = groupsBox ? groupsBox.querySelectorAll(":scope > details").length : 0;
      if (summaryCount) summaryCount.textContent = plural(albums, "album") + " across " + plural(artists, "artist");
      if (summaryRow) summaryRow.classList.toggle("hidden", albums === 0);
      if (emptyBox) emptyBox.classList.toggle("hidden", albums > 0 || !scanDone);
    }
    form.addEventListener("change", function (e) {
      if (e.target.classList && e.target.classList.contains("cb")) refresh();
    });
    form.addEventListener("click", function (e) {
      // A Hide button sits inside <summary>; keep its click from toggling the
      // disclosure. htmx still fires the hx-post.
      if (e.target.closest("[data-hide]")) e.preventDefault();
      // All/None flip checkboxes without firing a change event.
      if (e.target.closest("button")) setTimeout(refresh, 0);
    });
    form.addEventListener("htmx:afterSwap", refresh);
    document.body.addEventListener("qlHidden", function () { setTimeout(refresh, 0); });
    refresh();

    if (!scanning) return;

    // ── Live scan: stream log + progress, append artists as the walk finds
    //    them, leaving on-screen groups (and their state) untouched. ──────
    var logEl = document.getElementById("log");
    var card = document.getElementById("progress-card");
    var label = document.getElementById("prog-label");
    var count = document.getElementById("prog-count");
    var bar = document.getElementById("prog-bar");
    var item = document.getElementById("prog-item");
    var scanHeader = document.getElementById("scan-header");
    var reconnect = document.getElementById("sse-reconnect");
    var baseTitle = document.title;
    var titleSet = false;
    var after = -1;
    form.querySelectorAll(".cb[data-seq]").forEach(function (cb) {
      after = Math.max(after, parseInt(cb.dataset.seq, 10));
    });
    var pulling = false;
    function pullGroups(done) {
      if (pulling) { if (done) done(); return; }
      pulling = true;
      fetch("/jobs/" + id + "/groups?after=" + after, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.text(); })
        .then(function (txt) {
          pulling = false;
          var t = txt.trim();
          if (t && groupsBox) {
            var tmp = document.createElement("div");
            tmp.innerHTML = t;
            tmp.querySelectorAll(":scope > details[data-artist]").forEach(function (g) {
              var a = g.getAttribute("data-artist");
              var sel = 'details[data-artist="' + (window.CSS && CSS.escape ? CSS.escape(a) : a) + '"]';
              var existing = groupsBox.querySelector(sel);
              if (existing) {
                existing.replaceWith(g);
              } else {
                g.classList.add("ql-enter");
                groupsBox.appendChild(g);
                setTimeout(function () { g.classList.remove("ql-enter"); }, 260);
              }
              g.querySelectorAll(".cb[data-seq]").forEach(function (cb) {
                after = Math.max(after, parseInt(cb.dataset.seq, 10));
              });
            });
            if (window.htmx) window.htmx.process(groupsBox);
          }
          refresh();
          if (done) done();
        })
        .catch(function () { pulling = false; if (done) done(); });
    }

    var src = new EventSource("/api/jobs/" + id + "/stream");
    src.onmessage = function (e) {
      if (!titleSet) { document.title = "▶ " + baseTitle; titleSet = true; }
      if (logEl) { logEl.appendChild(document.createTextNode(e.data + "\n")); logEl.scrollTop = logEl.scrollHeight; }
    };
    src.onopen = function () { if (reconnect) reconnect.classList.add("hidden"); };
    src.onerror = function () {
      if (reconnect && src.readyState !== EventSource.CLOSED) reconnect.classList.remove("hidden");
    };
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (card) card.classList.remove("hidden");
      if (label) label.textContent = p.phase || "Working";
      var ct = p.total > 0 ? p.current + " / " + p.total : (p.current ? String(p.current) : "");
      if (p.found > 0) ct += (ct ? " · " : "") + p.found + " with gaps";
      if (count) count.textContent = ct;
      if (bar) { if (p.total > 0) { bar.max = 100; bar.value = Math.round(p.current / p.total * 100); } else { bar.removeAttribute("value"); } }
      if (item) item.textContent = p.item || "";
      if (p.hit) pullGroups();
    });
    src.addEventListener("done", function (e) {
      src.close();
      document.title = baseTitle;
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (e.data === "awaiting_review") {
        // Scan finished with results: enable downloading in place, keeping
        // every tick/expansion the user made while it ran.
        if (card) card.classList.add("hidden");
        if (scanHeader) scanHeader.classList.add("hidden");
        var badge = document.getElementById("job-status-badge");
        if (badge) { badge.textContent = "awaiting review"; badge.className = "badge whitespace-nowrap shrink-0 badge-info"; }
        pullGroups(function () { scanDone = true; refresh(); });
      } else if (window.htmx) {
        window.htmx.ajax("GET", "/jobs/" + id + "/content", { target: "#job-content", swap: "outerHTML" });
      } else {
        location.reload();
      }
    });
  }

  function initAll() {
    autoDismissFlashes();
    initStreamCards();
    initJobContent();
  }

  // htmx swaps insert flashes and replace progress cards / the job body after
  // the initial load — re-scan after any swap so they fade and the new streams
  // bind. The dataset markers make re-running idempotent.
  document.addEventListener("htmx:afterSwap", initAll);
  cleanFlashUrl();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
