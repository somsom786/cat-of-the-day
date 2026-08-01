/* ===========================================================================
   CAT OF THE DAY -- the only script on the site.
   No dependencies, no third-party requests, no analytics.
   Everything here is progressive: with JS off the page still works, the
   navigation still navigates, and the counter still shows its number.
   =========================================================================== */
(function () {
  "use strict";

  var CFG = window.CATDAY || {};
  var root = document.documentElement;

  /* -----------------------------------------------------------------------
     Reduced motion. The one and only thing we put in localStorage.
     (Applied early by an inline snippet in <head> so there is no flash;
     this block only wires up the toggle button.)
     ----------------------------------------------------------------------- */
  var KEY = "catday.motion";

  function motionOff() {
    try { return localStorage.getItem(KEY) === "off"; } catch (e) { return false; }
  }

  function setMotion(off) {
    try { localStorage.setItem(KEY, off ? "off" : "on"); } catch (e) { /* private mode */ }
    root.classList.toggle("no-motion", off);
    syncToggle();
  }

  var toggle = document.querySelector(".motion-toggle");

  function syncToggle() {
    if (!toggle) return;
    var off = root.classList.contains("no-motion");
    toggle.textContent = off ? "[ MOTION: OFF -- TURN IT BACK ON ]"
                             : "[ MOTION: ON -- MAKE IT STOP ]";
    toggle.setAttribute("aria-pressed", off ? "true" : "false");
  }

  if (toggle) {
    syncToggle();
    toggle.addEventListener("click", function () {
      setMotion(!root.classList.contains("no-motion"));
    });
  }

  function prefersReduced() {
    if (root.classList.contains("no-motion")) return true;
    return window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /* -----------------------------------------------------------------------
     Date helpers. Same arithmetic as the build script, so the client and the
     generator can never disagree about which cat today is.
     ----------------------------------------------------------------------- */
  function dayToISO(day) {
    var d = new Date(day * 86400000);
    var m = String(d.getUTCMonth() + 1).padStart(2, "0");
    var dd = String(d.getUTCDate()).padStart(2, "0");
    return d.getUTCFullYear() + "-" + m + "-" + dd;
  }

  /* -----------------------------------------------------------------------
     RANDOM CAT -- jump to a random past permalink.
     ----------------------------------------------------------------------- */
  var randomLink = document.querySelector("[data-random-cat]");
  if (randomLink && typeof CFG.launch === "number" && typeof CFG.today === "number") {
    randomLink.addEventListener("click", function (ev) {
      ev.preventDefault();
      var span = CFG.today - CFG.launch + 1;
      var pick = CFG.launch + Math.floor(Math.random() * span);
      // Don't send someone to the page they are already on.
      if (span > 1 && dayToISO(pick) === CFG.date) {
        pick = pick === CFG.today ? pick - 1 : pick + 1;
      }
      window.location.href = CFG.base + "cat/" + dayToISO(pick) + "/";
    });
  }

  /* -----------------------------------------------------------------------
     Arrow keys -- left/right for previous/next day.
     Ignored while the user is typing or holding a modifier.
     ----------------------------------------------------------------------- */
  document.addEventListener("keydown", function (ev) {
    if (ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
    var t = ev.target;
    if (t && (t.isContentEditable
      || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ""))) return;

    var sel = ev.key === "ArrowLeft" ? "[data-nav-prev]"
            : ev.key === "ArrowRight" ? "[data-nav-next]"
            : null;
    if (!sel) return;

    var el = document.querySelector(sel);
    if (el && el.tagName === "A" && el.getAttribute("href")
        && el.getAttribute("aria-disabled") !== "true") {
      ev.preventDefault();
      window.location.href = el.href;
    }
  });

  /* -----------------------------------------------------------------------
     The totally real visitor counter.
     The final number is baked in at build time and is identical for every
     visitor on a given day -- it is a joke, not analytics. All this does is
     roll the digits up to it, and only if motion is allowed.
     ----------------------------------------------------------------------- */
  var odo = document.querySelector("[data-odometer]");
  if (odo) {
    var target = parseInt(odo.getAttribute("data-odometer"), 10);
    if (isFinite(target)) {
      var digits = odo.querySelectorAll("b");
      var width = digits.length;

      var render = function (value) {
        var s = String(Math.max(0, Math.floor(value))).padStart(width, "0");
        for (var i = 0; i < width; i++) {
          if (digits[i].textContent !== s[i]) digits[i].textContent = s[i];
        }
      };

      if (prefersReduced()) {
        render(target);
      } else {
        var from = Math.max(0, target - 40);
        var start = null;
        var dur = 900;
        var tick = function (ts) {
          if (start === null) start = ts;
          var p = Math.min(1, (ts - start) / dur);
          var eased = 1 - Math.pow(1 - p, 3);
          render(from + (target - from) * eased);
          if (p < 1) requestAnimationFrame(tick);
        };
        render(from);
        requestAnimationFrame(tick);
      }
    }
  }

  /* -----------------------------------------------------------------------
     Once the real image decodes, drop the LQIP behind it. Without this the
     low-quality placeholder sits underneath a partially transparent PNG-ish
     edge forever and slightly muddies the picture.
     ----------------------------------------------------------------------- */
  var hero = document.querySelector("[data-hero]");
  if (hero) {
    var clear = function () { hero.style.backgroundImage = "none"; };
    if (hero.complete && hero.naturalWidth) clear();
    else hero.addEventListener("load", clear);
  }
})();
