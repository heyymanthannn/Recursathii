# RecurSaathi 30 acceptance criteria (Part 20/21, Nov 15, 2026)

Each is verifiable by test or by a one-click manual check. Master E2E suite
(tests/test_phase17_master_e2e.py) covers 1-30 in one synthetic journey.

1. Public homepage loads: "Recover More Revenue." hero, Get Started + View Pricing CTAs.
2. All 39 public pages use the exact "Page | RecurSaathi" title format.
3. Signup requires name, business, email, phone, password >= 8, confirm password, terms acceptance.
4. Mismatched confirm-password signup is rejected and creates no user.
5. Terms-unaccepted signup is rejected and creates no user.
6. Email verification token verifies the account; a reused/invalid token is rejected.
7. Wrong password is rejected; login throttled at 10 attempts / 600 s per IP+email.
8. Plain login sets a 12-hour session; remember-me sets a 30-day session.
9. TOTP 2FA can be enabled and is enforced on the next login.
10. A fresh owner routes to the onboarding wizard; finishing it lands on the dashboard.
11. Every onboarding step is skippable without blocking progress.
12. Customers can be added, listed and exported (CSV, formula-injection safe).
13. Invoices are created with amount and due date; overdue ones show in Radar.
14. Manual reminder queues email + WhatsApp drafts to the outbox and sends nothing live
    while providers are off; it is deduplicated per invoice and stage.
15. Every automation starts OFF for a new business.
16. Automation toggle on/off works and writes an audit-log entry each time.
17. Manual automation run completes and only queues work (no live send).
18. The customer-facing portal invoice page renders and carries the payment link.
19. A customer payment claim lands as pending and does NOT settle the invoice;
    reminders continue while it is unverified.
20. Owner verification of the claim settles the invoice and stops reminders for it.
21. Analytics render recovery data for the account.
22. Subscription page shows Starter Rs 999 / Growth Rs 4,999 / Enterprise Rs 9,999 monthly
    and Rs 9,990 / Rs 49,990 / Rs 99,990 annually, plus the Early Adopter banner.
23. Early-adopter availability endpoint reports max 10, remaining 10 (server-side, no fabrication).
24. Team seats can be added/removed with role selection; read-only seats cannot act.
25. Refund policy page is live in draft; Privacy and Terms are live.
26. Logout clears the session; public pages carry nosniff + DENY headers.
27. Unsigned/invalid webhooks fail closed (400); valid HMAC-signed raw-body webhooks are accepted.
28. The app never holds or moves money: funds go Customer -> Business via the business's
    own UPI/Razorpay; subscription billing stays separate from merchant invoice collections.
29. Master E2E: one synthetic business completes the full journey, 30/30 checks pass.
30. Full regression: all suites green in one run; zero live provider sends in any test.
