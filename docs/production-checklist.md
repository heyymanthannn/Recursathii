# RecurSaathi production checklist (Part 19, Nov 15, 2026)

Ordered go-live sequence. Every row needs Manthan's explicit written approval
before it runs; nothing here is auto-executed. Zero-rupee constraint holds
throughout: no paid plan, no new paid service, no paid AI.

## A. Inputs only Manthan can supply
1. Razorpay sandbox (not live) credentials: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
   RZP_WEBHOOK_SECRET — via Render secure env settings only, never chat (task eca61ec9).
2. Decision: approve the six plan ID vars (RZP_PLAN_STARTER/GROWTH/SCALE) once
   sandbox plans exist. Scale env name is kept, mapped to the Enterprise plan.
3. WhatsApp/Meta: WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN + template
   recursaathi_reminder_v1 approval in the Meta dashboard.
4. PROVIDER_STATUS_TOKEN for the provider status endpoint.
5. Legal sign-off on the refund policy draft (docs/refund-policy-draft-review.md).
6. Merge approval for dev commits ae27a55, 80fb6ff, 2ef4af2 (Parts 4-6, master
   instruction) -> a second code-only merge into main.

## B. Render environment (after creds arrive, still sandbox)
7. Set the six RZP_* vars via Render env settings (never chat, never .env).
8. .env on Render stays untouched until every credential in it is replaced and rotated.
9. Auto-Deploy stays OFF; any change takes effect only via a manual, approved deploy.

## C. Verification sequence (dev + sandbox only)
10. Signed-webhook round-trip on staging/dev against live Razorpay sandbox
    (X-Hub-Signature-256, raw body, fail-closed) for /webhooks/razorpay and /billing.
11. Full sandbox payment cycle: subscription created -> paid -> charged -> active;
    halted -> past_due; cancelled -> cancelled. No UTR/screenshot/front-end success
    ever counts as verified payment.
12. Enterprise plan creation at Rs 9,999/month (task 867636c2) — owner-created
    step, only after creds are saved.
13. Only after 10-12 all pass: owner explicitly sets ALLOW_LIVE_PROVIDERS=1.
    Before that it stays unset: providers are sandbox-only.

## D. Controlled pilot (Parts 6 + 20 prep, dev-only migrations already coded)
14. Owner approval for the additive production migrations
    (customer_id/delivery columns on the existing DB; no destructive change).
15. Pilot cohort picked by Manthan only; no real customer message before approval.
16. Backup restore drill from the runbook (RPO 24h, RTO ~30min) before first real data.

## E. Production deploy (separate explicit approval)
17. Confirm Auto-Deploy still OFF; deploy main HEAD manually once approved.
18. Smoke tests on prod: GET /, robots, sitemap, IndexNow key; login round-trip;
    unsigned webhook rejected 400.
19. Record deploy id, confirm prod DB unchanged except the approved migrations.

## F. Standing rules re-checked before every launch step
20. Subscription billing stays separate from merchant invoice collections.
21. No deploy, no Render change, no DB migration, no real customer message,
    no merge without a separate written approval each time.
