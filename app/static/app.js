(function () {
  "use strict";
  // Confirm dialogs for irreversible actions + double-submit protection.
  document.addEventListener("submit", function (e) {
    var f = e.target;
    var msg = f.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var b = f.querySelector("button");
    if (b) { setTimeout(function () { b.disabled = true; }, 0); }
  });

  // Live job status: poll while work is queued/running, reload when it finishes.
  var bar = document.getElementById("jobbar");
  if (!bar) return;
  var eventId = bar.getAttribute("data-poll-event");
  var wasActive = parseInt(bar.getAttribute("data-active") || "0", 10) > 0;
  var timer = null;
  function tick() {
    fetch("/api/jobs?event=" + encodeURIComponent(eventId), { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (d) {
        var s = d.summary, text = document.getElementById("jobtext");
        if (d.active > 0) {
          wasActive = true; bar.classList.remove("idle");
          text.textContent = "Working: " + s.RUNNING + " running, " + s.QUEUED + " queued" + (s.FAILED ? " · " + s.FAILED + " failed" : "");
        } else if (wasActive) {
          window.location.reload();
        } else {
          bar.classList.add("idle");
        }
      })
      .catch(function () {});
  }
  function loop() { tick(); timer = setTimeout(loop, 2500); }
  if (wasActive) { loop(); }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { clearTimeout(timer); } else if (wasActive) { loop(); }
  });
})();
