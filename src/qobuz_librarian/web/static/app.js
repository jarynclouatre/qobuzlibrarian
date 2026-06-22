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
  // feedback, so a slow start on a big library can look frozen and invite a
  // second click (which would stack a duplicate hours-long scan). Disable the
  // button and show it working; the disable happens after the submit fires so
  // the request still goes out and the navigation isn't cancelled.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-busy-submit]")) return;
    var b = (evt.submitter && evt.submitter.type === "submit" ? evt.submitter : null)
          || form.querySelector("button[type=submit]");
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

  // Mobile-nav (and any future daisyUI .dropdown with [aria-expanded]): the
  // dropdown opens on focus-within, so keep aria-expanded in lockstep with
  // whether anything inside the .dropdown holds focus. Without this the
  // hamburger never tells screen-reader users whether the menu is open.
  function syncDropdownExpanded(dd) {
    var btn = dd.querySelector("[aria-expanded]");
    if (btn) btn.setAttribute("aria-expanded", dd.contains(document.activeElement).toString());
  }
  document.addEventListener("focusin", function (evt) {
    var dd = evt.target.closest && evt.target.closest(".dropdown");
    if (dd) syncDropdownExpanded(dd);
  });
  document.addEventListener("focusout", function (evt) {
    var dd = evt.target.closest && evt.target.closest(".dropdown");
    if (!dd) return;
    // focusout fires before the new focus settles, so re-check next tick.
    setTimeout(function () { syncDropdownExpanded(dd); }, 0);
  });

  // iOS Safari doesn't reliably focus a <button> on tap, so the :focus-within
  // CSS that opens a daisyUI .dropdown never fires and the mobile menu is dead
  // on iOS. Drive it explicitly with the .dropdown-open modifier on tap; close
  // on an outside tap or after a menu link is chosen, releasing focus too so the
  // focus-within CSS agrees with the class.
  function closeDropdown(dd) {
    dd.classList.remove("dropdown-open");
    if (dd.contains(document.activeElement) && document.activeElement.blur) {
      document.activeElement.blur();
    }
    var b = dd.querySelector("[aria-expanded]");
    if (b) b.setAttribute("aria-expanded", "false");
  }
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[aria-haspopup='menu']");
    var trigger = (btn && btn.closest(".dropdown")) ? btn : null;
    var triggerDd = trigger ? trigger.closest(".dropdown") : null;
    // Close any open dropdown that isn't the one being toggled (outside tap, a
    // chosen menu link, or switching dropdowns).
    document.querySelectorAll(".dropdown.dropdown-open").forEach(function (o) {
      if (o !== triggerDd) closeDropdown(o);
    });
    if (!triggerDd) return;
    if (triggerDd.classList.contains("dropdown-open")) {
      closeDropdown(triggerDd);
    } else {
      triggerDd.classList.add("dropdown-open");
      trigger.setAttribute("aria-expanded", "true");
    }
  });

  // Flash banners. A server-rendered alert driven by a one-shot query flag
  // (?saved=1, ?error=…) would otherwise stick to the URL forever — refreshing
  // or sharing the page would re-render the same banner. Strip the known flash
  // params after first paint, and fade banners out so they don't dominate.
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

  // A programmatic toast in the #download-toast corner, matching the queued
  // confirmation's styling and auto-dismiss. Used when a background job ends in
  // a state worth surfacing (a failed download) but no htmx response carried a
  // banner — without this it would just vanish from the list like a success.
  function showToast(message, kind) {
    var host = document.getElementById("download-toast");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "alert alert-" + (kind || "info");
    el.setAttribute("data-flash", "");
    el.setAttribute("role", "status");
    var span = document.createElement("span");
    span.textContent = message;
    el.appendChild(span);
    host.appendChild(el);
    setTimeout(function () { fade(el); }, 8000);
  }

  // ── Live job/queue streams ─────────────────────────────────────────────
  // Kept out of the partials htmx swaps in: a nonce can't survive an htmx swap
  // (the fragment's nonce ≠ the live document's), so an inline block in swapped
  // content would be CSP-blocked. These bind on load and re-bind after each
  // swap; a dataset flag stops a node from binding twice.

  // Label a bare "12 / 350" with what it counts ("artists"/"albums"/"tracks")
  // so the numbers aren't a guess. The server sends the singular unit; pluralise
  // by the total. Empty when the push carried no unit (e.g. a download).
  function unitSuffix(p) {
    return p.unit ? " " + p.unit + (p.total === 1 ? "" : "s") : "";
  }

  function fmtProgress(p, verb, withItem) {
    if (withItem === undefined) withItem = true;
    // The dashboard card stays a stable "Scanning N / M artists" — the per-artist
    // item cycles (artist name ↔ album tally) and reads as flicker on a glance
    // card; the detail lives on the job page. The queue card keeps its item.
    var item = (withItem && p.item) ? " · " + p.item : "";
    if (p.total > 0) {
      return verb + " " + p.current + " / " + p.total + unitSuffix(p) + item;
    }
    if (p.phase) return p.phase + item;
    return "";
  }

  // Dashboard and queue progress cards: update the sub-line in place, then
  // refresh the card region once the job finishes.
  function wireStreamCard(card) {
    if (card.dataset.sseWired === "1") return;
    card.dataset.sseWired = "1";
    var id = card.dataset.jobId;
    var surface = card.dataset.jobSurface;   // "dashboard" | "queue" | "repair"
    var status = card.dataset.jobStatus;
    if (!id || !surface) return;
    // A queue card rendered while still PENDING shows the "waiting behind X"
    // explainer; the moment it actually starts (first progress event) we re-pull
    // the queue so it flips to a live Scanning/Downloading card. Guarded so the
    // re-pull fires once.
    var flippedFromPending = false;
    var progId = (surface === "dashboard" ? "dash-prog-"
                : surface === "repair" ? "repair-prog-" : "card-prog-") + id;
    // The repair card lives on a page htmx never swaps, so it has no swap-out
    // container to watch — it tears down on the stream's done/close instead.
    var containerId = surface === "dashboard" ? "dashboard-active"
                    : surface === "queue" ? "queue-body" : "";
    var reconnect = surface === "dashboard"
      ? document.getElementById("dash-reconnect-" + id)
      : surface === "repair"
      ? document.getElementById("repair-reconnect-" + id) : null;
    var src = new EventSource("/api/jobs/" + id + "/stream");
    function shut() {
      try { src.close(); } catch (e) {}
      document.removeEventListener("htmx:beforeSwap", onSwap);
    }
    function onSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === containerId) shut();
    }
    document.addEventListener("htmx:beforeSwap", onSwap);
    // The repair card streams the flagged-album log inline so the sweep is
    // watched right here, not on a tapped-through page. A card that loaded
    // queued (pending, behind another scan) flips to "Scanning" on first output.
    var repairLog = surface === "repair" ? document.getElementById("repair-log-" + id) : null;
    var repairBadge = surface === "repair" ? document.getElementById("repair-badge-" + id) : null;
    var repairWait = surface === "repair" ? document.getElementById("repair-wait-" + id) : null;
    var repairStarted = status !== "pending";
    var repairOpened = false;
    function repairMarkStarted() {
      if (repairStarted) return;
      repairStarted = true;
      if (repairBadge) { repairBadge.textContent = "Scanning"; repairBadge.className = "badge badge-info badge-sm"; }
      if (repairWait) repairWait.classList.add("hidden");
    }
    if (surface === "repair") {
      src.onmessage = function (e) {
        repairMarkStarted();
        if (repairLog) { repairLog.appendChild(document.createTextNode(e.data + "\n")); repairLog.scrollTop = repairLog.scrollHeight; }
      };
    }
    if (reconnect) {
      src.onopen = function () {
        reconnect.classList.add("hidden");
        // Reconnect replays the tail — clear so it repopulates the inline log
        // instead of doubling it. Not on first open (nothing to double yet).
        if (surface === "repair" && repairOpened && repairLog) repairLog.textContent = "";
        repairOpened = true;
      };
      src.onerror = function () {
        if (src.readyState !== EventSource.CLOSED) reconnect.classList.remove("hidden");
      };
    }
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      // A pending queue card just received its first progress → the job started.
      // Re-render the queue so the badge/border/cancel flip to the running shape
      // (the bare "Queued + waiting" card can't restyle itself in place).
      if (surface === "queue" && status === "pending" && !flippedFromPending) {
        flippedFromPending = true;
        shut();
        if (window.htmx) {
          window.htmx.ajax("GET", "/queue",
            { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
        }
        return;
      }
      if (surface === "repair") repairMarkStarted();
      var el = document.getElementById(progId);
      if (!el) return;
      // The repair card shows the sweep's own rich detail line (current artist ·
      // albums checked · flagged) verbatim — it's already a stable single line,
      // so no "Scanning N / M" wrapper. Fall back to the count if it's empty.
      var txt = surface === "repair"
        ? (p.item || fmtProgress(p, "Scanning", false))
        : fmtProgress(p, status === "scanning" ? "Scanning" : (p.phase || "Downloading"),
                      surface !== "dashboard");
      if (txt) el.textContent = txt;
    });
    src.addEventListener("done", function (e) {
      shut();
      if (surface === "repair") {
        // Scan finished → the job left scanning for its results. Go to the job
        // page so the user lands on the flagged-album review, not a stale form.
        location.href = "/jobs/" + id;
        return;
      }
      // A FAILED job just drops out of the active/queue list with the same empty
      // state as a clean finish. Flash a toast so a failed download isn't lost
      // silently — History keeps the detail and a Retry. (A user-initiated
      // cancel is expected, so it doesn't toast.)
      var endStatus = (e && e.data) ? ("" + e.data).trim() : "";
      if (endStatus === "failed") {
        var t = card.querySelector('a[href^="/jobs/"]');
        var label = ((t && t.textContent) || "A job").replace(/\s+/g, " ").trim();
        showToast(label + " failed — open History for details.", "error");
      }
      if (surface === "dashboard") {
        // Don't yank the user mid-search — if search results are showing, drop
        // just THIS finished card. Hiding the whole #dashboard-active container
        // (its previous behaviour) would blank every other still-running job's
        // progress card too.
        var results = document.getElementById("search-results");
        if (results && results.children.length) {
          card.remove();
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
  // "progress" = a running/scanning job's live log + progress bar (a scan shows
  // only this — the tickable list appears once it finishes); "review" = the
  // paginated, server-backed candidate list for an awaiting-review job.
  function initJobContent() {
    var jc = document.getElementById("job-content");
    if (!jc || jc.dataset.jobWired === "1") return;
    var view = jc.dataset.jobView;
    var id = jc.dataset.jobId;
    if (!view || !id) return;
    jc.dataset.jobWired = "1";
    if (view === "review") wireReview(id);
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
    var foundEl = document.getElementById("scan-found");
    var reconnect = document.getElementById("sse-reconnect");
    // Live elapsed clock so a long, mostly-silent scan visibly ticks. The
    // server-computed elapsed-at-load (its own now minus the job start) is the
    // baseline, and the clock advances by the browser's own time since page load
    // — so a phone whose wall clock is off from the server still reads right.
    // Self-clears if the element leaves the DOM (htmx body swap).
    var elapsedEl = document.getElementById("scan-elapsed");
    var elapsedTimer = null;
    if (elapsedEl && elapsedEl.dataset.start) {
      var serverStart = parseFloat(elapsedEl.dataset.start);
      var serverNow = parseFloat(elapsedEl.dataset.now);
      var elapsedAtLoad = (isFinite(serverNow) ? serverNow : Date.now() / 1000) - serverStart;
      if (!(elapsedAtLoad >= 0)) elapsedAtLoad = 0;
      var clientBase = Date.now();
      var tickElapsed = function () {
        if (!document.body.contains(elapsedEl)) { if (elapsedTimer) clearInterval(elapsedTimer); return; }
        var secs = Math.max(0, Math.floor(elapsedAtLoad + (Date.now() - clientBase) / 1000));
        var mm = Math.floor(secs / 60), ss = secs % 60;
        elapsedEl.textContent = "· " + mm + ":" + (ss < 10 ? "0" : "") + ss + " elapsed";
      };
      tickElapsed();
      elapsedTimer = setInterval(tickElapsed, 1000);
    }
    var baseTitle = document.title;
    var titleSet = false;
    // Running tally of artists that turned up a hit, fed by progress 'hit'
    // events — lets the scanning view show "N albums across M artists found"
    // without rendering the (potentially huge) list while the walk runs.
    var foundAlbums = 0, foundArtists = 0;
    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function showFound() {
      if (!foundEl) return;
      foundEl.textContent = foundAlbums
        ? "Found " + plural(foundAlbums, "album") + " across " + plural(foundArtists, "artist") + " so far…"
        : "";
    }
    // A job that loaded PENDING shows a "waiting behind X" note and a "Queued"
    // label; the first streamed line or progress tick means the worker picked it
    // up, so clear that state live instead of leaving it stale until the swap.
    var waitNote = document.getElementById("queue-wait-note");
    function clearQueuedState() {
      if (waitNote) { waitNote.classList.add("hidden"); waitNote = null; }
      if (activity && activity.textContent === "Queued") activity.textContent = "Scanning";
    }
    var src = new EventSource("/api/jobs/" + id + "/stream");
    var opened = false;
    src.onmessage = function (e) {
      if (!titleSet) { document.title = "▶ " + baseTitle; titleSet = true; }
      clearQueuedState();
      if (logEl) { logEl.appendChild(document.createTextNode(e.data + "\n")); logEl.scrollTop = logEl.scrollHeight; }
    };
    src.onopen = function () {
      if (reconnect) reconnect.classList.add("hidden");
      // On a RECONNECT the server replays the last N log lines; without clearing
      // they'd be appended a second time, duplicating a block of the log. Reset
      // the pane so the replay repopulates it instead of doubling it. Not on the
      // first open — there's nothing to duplicate yet.
      if (opened) {
        if (logEl) logEl.textContent = "";
        // Reset the hit counters so replayed progress events repopulate them
        // from scratch rather than doubling the tally.
        foundAlbums = 0;
        foundArtists = 0;
      }
      opened = true;
    };
    src.onerror = function () {
      if (reconnect && src.readyState !== EventSource.CLOSED) reconnect.classList.remove("hidden");
    };
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      clearQueuedState();
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (activity && p.phase && activity.textContent !== p.phase) activity.textContent = p.phase;
      if (card) card.classList.remove("hidden");
      if (label) label.textContent = p.phase || "Working";
      var ct = p.total > 0 ? p.current + " / " + p.total + unitSuffix(p) : (p.current ? String(p.current) : "");
      if (count) count.textContent = ct;
      if (bar) { if (p.total > 0) { bar.max = 100; bar.value = Math.round(p.current / p.total * 100); } else { bar.removeAttribute("value"); } }
      if (item) item.textContent = p.item || "";
      // p.found is the running album tally; p.hit marks one artist with hits.
      if (typeof p.found === "number" && p.found > foundAlbums) foundAlbums = p.found;
      if (p.hit && p.hit.albums > 0) { foundArtists += 1; showFound(); }
      else if (typeof p.found === "number") showFound();
    });
    src.addEventListener("done", function () {
      src.close();
      if (elapsedTimer) clearInterval(elapsedTimer);
      document.title = baseTitle;
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      // Scan finished → load the (now-final) server-rendered body, which for a
      // triage scan is the paginated review. One render path: the server's.
      if (window.htmx) {
        window.htmx.ajax("GET", "/jobs/" + id + "/content", { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
  }

  // Paginated, server-backed review. Selection lives on the server (each tick
  // saves immediately), so it persists across pages, reloads, and tabs, and
  // the download acts on the saved set — never on whatever checkboxes happen to
  // be in the DOM. The list is one page of artist groups; Prev/Next, the
  // whole-set filter, and a hide each re-fetch a fresh page from the server.
  function wireReview(id) {
    var cont = document.getElementById("review-candidates");
    var form = document.getElementById("review-form");
    if (!cont || !form) return;
    var submit = document.getElementById("review-submit");
    var countEl = submit ? submit.querySelector("[data-count]") : null;
    var summaryRow = document.getElementById("review-summary-row");
    var summaryCount = document.querySelector("#review-candidates [data-summary-count]");
    var emptyBox = document.getElementById("review-empty");
    var filterRow = document.getElementById("review-filter-row");
    var filterInput = document.getElementById("review-filter");
    var dsTotal = document.querySelector("[data-downsample-total]");
    var kind = form.getAttribute("data-review-kind") || "";

    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function csrf() {
      var m = document.querySelector('meta[name="csrf-token"]');
      return m ? m.content : "";
    }
    function pageBox() { return document.getElementById("review-groups"); }
    function curPage() { var g = pageBox(); return g ? parseInt(g.dataset.page || "1", 10) : 1; }
    function curQuery() { return (filterInput && filterInput.value || "").trim(); }

    // Apply authoritative counts from a /select or /hide response to the header,
    // submit button, and downsample total — never recounted from the partial
    // DOM, which only holds one page.
    function applyCounts(c) {
      if (!c) return;
      if (countEl) countEl.textContent = c.selected ? " " + c.selected : "";
      if (submit) submit.disabled = c.selected === 0;
      if (summaryCount) {
        summaryCount.textContent = plural(c.total, "album") + " across " + plural(c.artists, "artist");
      }
      if (summaryRow) summaryRow.classList.toggle("hidden", c.total === 0);
      if (emptyBox) emptyBox.classList.toggle("hidden", c.total > 0);
      if (filterRow && !curQuery()) filterRow.classList.toggle("hidden", c.artists < 4);
      if (dsTotal) {
        dsTotal.textContent = c.reclaimable_label
          ? " · ~" + c.reclaimable_label + " reclaimable" : "";
      }
      cont.dataset.reviewTotal = c.total;
      cont.dataset.reviewSelected = c.selected;
    }

    function post(url, body) {
      return fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrf(),
        },
        body: body,
      });
    }

    // Save one tick to the server, then refresh counts from its response.
    function saveTick(cb) {
      // Approval acts on the server-saved flags, so a silently-dropped tick
      // would leave the box showing an intent the server never got (an unwanted
      // track still gets downloaded/downsampled). Roll the box back and flash it
      // red on any failure (network error OR a non-OK response).
      var previous = !cb.checked;
      function revert() {
        cb.checked = previous;
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      }
      // Disable the box until its POST resolves. Two rapid toggles otherwise fire
      // two POSTs that can land out of order, leaving the server's saved flag
      // disagreeing with the box on screen; serializing per-box keeps them in
      // sync. Re-enable on both success and failure so a box never sticks.
      cb.disabled = true;
      var body = "cid=" + encodeURIComponent(cb.value) + "&checked=" + (cb.checked ? "1" : "0");
      post("/jobs/" + id + "/select", body)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (c) { cb.disabled = false; if (c) applyCounts(c); updateHideLabels(); })
        .catch(function () { cb.disabled = false; revert(); });
    }

    // Guard against overlapping bulk runs: a second select-all/page tap before
    // the first POST lands would race two whole-set writes whose order isn't
    // guaranteed, leaving the server's selection disagreeing with the screen.
    var bulkBusy = false;
    function bulkSelect(on, scope) {
      if (bulkBusy) return Promise.resolve();
      bulkBusy = true;
      var body = "on=" + (on ? "1" : "0") + "&scope=" + scope;
      if (scope === "page") {
        pageBox() && pageBox().querySelectorAll(".cb").forEach(function (cb) {
          body += "&cid=" + encodeURIComponent(cb.value);
        });
      }
      return post("/jobs/" + id + "/select-all", body)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (c) {
          bulkBusy = false;
          // Mirror the change on screen only once the server confirms the save,
          // so a failed save can't leave boxes ticked that the server never
          // recorded — which would approve a selection the user never made. On
          // failure, leave the screen as it was and flag it.
          if (!c) { flashSelectError(); return; }
          applyCounts(c);
          var on2 = on;
          if (scope === "all" || scope === "page") {
            pageBox() && pageBox().querySelectorAll(".cb").forEach(function (cb) { cb.checked = on2; });
          }
          updateHideLabels();
        })
        .catch(function () { bulkBusy = false; flashSelectError(); });
    }

    // Briefly red-outline the on-screen checkboxes when a bulk/group save fails,
    // matching the per-tick revert's visual cue, so the user knows the action
    // didn't take and can retry rather than trusting a stale tick.
    function flashSelectError() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll(".cb").forEach(function (cb) {
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      });
    }

    function groupSelect(det, on) {
      // Flip each box only AFTER its save is confirmed (the per-tick path's
      // contract), so a failure mid-batch leaves the already-saved boxes ticked,
      // the unsaved ones unticked, and the screen matching the server — never an
      // optimistic tick the server rejected.
      if (det._selecting) return;  // a second tap mid-batch would race the chain
      var pending = Array.prototype.filter.call(det.querySelectorAll(".cb"),
        function (cb) { return cb.checked !== on; });
      if (!pending.length) return;
      det._selecting = true;
      var failed = false;
      var chain = Promise.resolve(null);
      pending.forEach(function (cb) {
        chain = (function (box, prev) {
          return prev.then(function (acc) {
            if (failed) return acc;
            return post("/jobs/" + id + "/select",
              "cid=" + encodeURIComponent(box.value) + "&checked=" + (on ? "1" : "0"))
              .then(function (r) {
                if (!r.ok) { failed = true; return acc; }
                box.checked = on;
                return r.json();
              })
              .catch(function () { failed = true; return acc; });
          });
        }(cb, chain));
      });
      chain.then(function (c) {
        det._selecting = false;
        if (c) applyCounts(c);
        if (failed) flashSelectError();
        updateHideLabels();
      });
    }

    // The per-artist Hide button hides everything not ticked, so label it for
    // what it'll actually drop and hide it when the whole group is ticked.
    function updateHideLabels() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll("[data-hide]").forEach(function (btn) {
        var det = btn.closest("details");
        var lbl = btn.querySelector("[data-hide-label]");
        if (!det || !lbl) return;
        var total = det.querySelectorAll(".cb").length;
        var picked = det.querySelectorAll(".cb:checked").length;
        btn.classList.toggle("hidden", total > 0 && picked === total);
        lbl.textContent = picked ? "Hide other " + (total - picked) : "Hide all " + total;
      });
    }

    // Fetch + swap one page of the review (Prev/Next, filter, hide-refresh).
    var loading = false;
    function loadPage(page, query) {
      if (loading) return;
      loading = true;
      var url = "/jobs/" + id + "/review?page=" + (page || 1) +
                "&q=" + encodeURIComponent(query || "");
      fetch(url, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          loading = false;
          if (txt == null) return;
          var host = document.getElementById("review-page");
          if (host) {
            // Preserve which artist groups the user has expanded so a
            // cross-tab sync tick doesn't silently collapse them all.
            var openArtists = {};
            host.querySelectorAll("details[data-artist][open]").forEach(function (d) {
              if (d.dataset.artist) openArtists[d.dataset.artist] = true;
            });
            host.innerHTML = txt;
            host.querySelectorAll("details[data-artist]").forEach(function (d) {
              if (d.dataset.artist && openArtists[d.dataset.artist]) d.open = true;
            });
            if (window.htmx) window.htmx.process(host);
            updateHideLabels();
          }
        })
        .catch(function () { loading = false; });
    }

    // ── Wire interactions (delegated so swapped-in pages keep working) ──────
    cont.addEventListener("change", function (e) {
      if (e.target.classList && e.target.classList.contains("cb")) saveTick(e.target);
    });
    cont.addEventListener("click", function (e) {
      var t = e.target;
      if (t.closest("[data-hide]")) { e.preventDefault(); return; }  // htmx handles the post
      var allBtn = t.closest("[data-select-all]");
      if (allBtn) { bulkSelect(allBtn.getAttribute("data-select-all") === "1", "all"); return; }
      var pageBtn = t.closest("[data-select-page]");
      if (pageBtn) { bulkSelect(true, "page"); return; }
      var gsel = t.closest("[data-group-select]");
      if (gsel) {
        var det = gsel.closest("details");
        if (det) groupSelect(det, gsel.getAttribute("data-group-select") === "1");
        return;
      }
      if (t.closest("[data-page-prev]")) { loadPage(curPage() - 1, curQuery()); return; }
      if (t.closest("[data-page-next]")) { loadPage(curPage() + 1, curQuery()); return; }
    });

    // Whole-set filter — re-paginate from the server (debounced) so it spans
    // every page, not just the one on screen.
    var filterTimer = null;
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        if (filterTimer) clearTimeout(filterTimer);
        filterTimer = setTimeout(function () { loadPage(1, curQuery()); }, 250);
      });
      // Enter in the filter box must NOT submit the review form. The form's
      // first submit button is Approve — an irreversible download/downsample —
      // or, when nothing is ticked, Cancel, which discards the whole scan.
      // Filtering then pressing Enter is a natural gesture, so swallow it; the
      // filter already applies live as you type.
      filterInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") e.preventDefault();
      });
    }

    // A hide returns the affected group (or empty) and an HX-Trigger carrying
    // fresh counts; refresh counts and, if a page emptied, reload it.
    function onQlHidden(e) {
      var d = e.detail || {};
      if (d.counts) applyCounts(d.counts);
      var box = pageBox();
      if (box && box.querySelectorAll(":scope > details").length === 0) {
        var p = curPage();
        loadPage(p > 1 ? p - 1 : 1, curQuery());
      } else {
        updateHideLabels();
      }
    }
    document.body.addEventListener("qlHidden", onQlHidden);

    updateHideLabels();

    // ── Multi-tab live sync. A second tab/device ticking, hiding, or
    //    selecting-all fires a `review` event here; refresh this page from the
    //    server so every open view stays in step. When the job leaves review
    //    (someone approved/cancelled it elsewhere), reload to the new state. ──
    var rsrc = new EventSource("/api/jobs/" + id + "/review-stream");
    // Tear down both the EventSource and the body listener when #job-content
    // is swapped out, so a subsequent wireReview call starts clean.
    function shutReview() {
      try { rsrc.close(); } catch (e) {}
      document.body.removeEventListener("qlHidden", onQlHidden);
      document.removeEventListener("htmx:beforeSwap", onReviewSwap);
    }
    function onReviewSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === "job-content") shutReview();
    }
    document.addEventListener("htmx:beforeSwap", onReviewSwap);
    rsrc.addEventListener("review", function () {
      // Re-fetch the current page (picks up others' ticks/hides) and the counts.
      loadPage(curPage(), curQuery());
      post("/jobs/" + id + "/select", "cid=&checked=0")  // no-op tick → returns counts
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (c) { if (c) applyCounts(c); });
    });
    rsrc.addEventListener("closed", function (e) {
      shutReview();
      // "inactive" = this job isn't live in the registry (a restored/archived
      // review): it still works, just without cross-tab sync — don't reload, or
      // we'd loop. Any other reason means the job left review elsewhere, so
      // refresh to the new state.
      if ((e.data || "") === "inactive") return;
      if (window.htmx) {
        window.htmx.ajax("GET", "/jobs/" + id + "/content", { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
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
