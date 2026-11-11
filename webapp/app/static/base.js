/* RecurSaathi shared page behaviour (moved out of inline <script> blocks for CSP). */
(function () {
  /* Cookie consent bar */
  var cbar = document.getElementById("cbar");
  if (cbar) {
    try { if (localStorage.getItem("rs_cookie_ok") !== "1") cbar.classList.add("on"); } catch (e) {}
    var cok = document.getElementById("cbar-ok");
    if (cok) cok.addEventListener("click", function () {
      try { localStorage.setItem("rs_cookie_ok", "1"); } catch (e) {}
      cbar.classList.remove("on");
    });
  }

  /* Mobile nav toggle */
  var navbtn = document.getElementById("navbtn"), navscrim = document.getElementById("scrim");
  if (navbtn) navbtn.addEventListener("click", function () {
    var open = document.body.classList.toggle("nav-open");
    navbtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (navscrim) navscrim.hidden = !open;
  });
  if (navscrim) navscrim.addEventListener("click", function () {
    document.body.classList.remove("nav-open");
    if (navbtn) navbtn.setAttribute("aria-expanded", "false");
    navscrim.hidden = true;
  });

  /* Trial subscription popup (only when rendered) */
  var pop = document.getElementById("subpop");
  if (pop) {
    var key = "rs_sub_pop";
    var today = new Date().toISOString().slice(0, 10);
    var seen = null;
    try { seen = localStorage.getItem(key); } catch (e) {}
    if (seen !== today) {
      pop.style.display = "flex";
      var skipBtn = document.getElementById("subpop-skip");
      if (skipBtn) skipBtn.addEventListener("click", function () {
        pop.style.display = "none";
        try { localStorage.setItem(key, today); } catch (e) {}
      });
    }
  }

  /* Confirm dialogs for destructive forms */
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (ev) {
      if (!window.confirm(f.getAttribute("data-confirm"))) ev.preventDefault();
    });
  });

  /* Print button */
  var pbtn = document.querySelector("[data-print]");
  if (pbtn) pbtn.addEventListener("click", function () { window.print(); });
})();
