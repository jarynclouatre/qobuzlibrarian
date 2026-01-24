// CSP-safe htmx hooks. These used to live as hx-on:* attributes inline,
// but htmx evaluates those via `new Function(...)`, which the page CSP
// (script-src 'self' 'unsafe-inline', no 'unsafe-eval') rejects.

// Scroll an arbitrary target into view after the swap finishes. Used on
// the Settings page's "Test token" button — the auth-status result is
// often below the fold on mobile.
document.addEventListener("htmx:afterOnLoad", function (evt) {
  var trigger = evt.target;
  if (!trigger || !trigger.hasAttribute) return;
  var sel = trigger.getAttribute("data-scroll-target");
  if (!sel) return;
  var t = document.querySelector(sel);
  if (t) t.scrollIntoView({ block: "center", behavior: "smooth" });
});

// On a successful download POST, mark the submit button "Queued" and
// disable it so a double-click doesn't queue the same album twice.
document.addEventListener("htmx:afterRequest", function (evt) {
  var form = evt.target;
  if (!form || !form.matches || !form.matches("form[data-queue-button]")) return;
  if (!evt.detail || !evt.detail.successful) return;
  var b = form.querySelector("button[type=submit]");
  if (!b) return;
  b.disabled = true;
  b.textContent = "Queued";
  b.classList.remove("btn-primary");
  b.classList.add("btn-ghost", "btn-disabled");
});
