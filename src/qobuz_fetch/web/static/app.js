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
