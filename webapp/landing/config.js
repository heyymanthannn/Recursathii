// RecurSaathi Early Access site configuration.
// To change the contact email for the whole Early Access site, edit the email line below only.
window.EA_CONFIG = {
  email: "recursathi@gmail.com",
  subject: "RecurSaathi Early Access Request",
};

// Applies the configured email everywhere: CTA mailto links ([data-ea-mail]),
// plain contact links ([data-contact-mailto]) and inline email placeholders
// ([data-contact-email]). No page hardcodes the address.
(function () {
  function apply() {
    var cfg = window.EA_CONFIG || {};
    if (!cfg.email) return;
    var ea = "mailto:" + cfg.email + "?subject=" + encodeURIComponent(cfg.subject || "RecurSaathi Early Access Request");
    var plain = "mailto:" + cfg.email;
    document.querySelectorAll("[data-ea-mail]").forEach(function (a) { a.setAttribute("href", ea); });
    document.querySelectorAll("[data-contact-mailto]").forEach(function (a) { a.setAttribute("href", plain); });
    document.querySelectorAll("[data-contact-email]").forEach(function (el) { el.textContent = cfg.email; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply);
  else apply();
})();
