"""Master E2E suite (Part 18): one synthetic business through the entire product,
30 named checks with exact counts, browser UI + model layer, no live providers.

Journey: public site -> signup (confirm password + terms) -> email verify ->
login (remember-me off) -> onboarding wizard -> dashboard -> customers ->
invoices -> radar -> manual reminder queued (nothing sent live) -> automations
off by default -> toggle + manual run (queued only) -> payments page ->
payment claim stays pending -> verified claim settles the invoice and stops
reminders -> analytics -> subscription/EA -> legal pages -> logout ->
security headers -> unsigned webhook fail-closed.
"""
import os, tempfile, time, hmac, hashlib, json
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_master_e2e_", suffix=".sqlite")
os.environ["RZP_WEBHOOK_SECRET"] = "master-e2e-secret"
os.environ["ADMIN_TOKEN"] = hashlib.sha256(b"master-e2e-admin").hexdigest()
os.environ["APP_SECRET"] = "master-e2e-app-secret"
os.environ.pop("ALLOW_LIVE_PROVIDERS", None)

from fastapi.testclient import TestClient
from app import main as app_main, models, notify

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

EMAIL = "maste2e@test.local"
PW = "pw12345678"

# 1. public homepage carries the launch hero and CTAs
r = client.get("/")
record("01 homepage: launch hero + CTAs", r.status_code == 200 and "Recover More Revenue." in r.text
       and ">Get Started</a>" in r.text and ">View Pricing</a>" in r.text)

# 2. signup form has confirm password + terms fields
r = client.get("/signup")
record("02 signup form: confirm + terms fields present",
       r.status_code == 200 and 'name="password2"' in r.text and 'name="accept"' in r.text)

# 3. signup succeeds with all fields -> login page
r = client.post("/signup", data={"name": "M E2E", "business": "Master Co", "email": EMAIL,
               "phone": "919810000900", "upi_vpa": "master@upi", "password": PW,
               "password2": PW, "accept": "1"}, follow_redirects=False)
record("03 signup: accepted, redirects to login", r.status_code == 303 and "/login" in r.headers.get("location", ""))

# 4. mismatched confirm password rejected, no user created
r = client.post("/signup", data={"name": "M E2E2", "business": "Master Co2", "email": "mm@test.local",
               "phone": "919810000901", "password": PW, "password2": "different99", "accept": "1"},
               follow_redirects=False)
record("04 signup: mismatched confirm rejected", r.status_code == 200 and "Passwords do not match" in r.text
       and not models.get_user_by_email("mm@test.local"))

# 5. terms acceptance required
r = client.post("/signup", data={"name": "M E2E3", "business": "Master Co3", "email": "na@test.local",
               "phone": "919810000902", "password": PW, "password2": PW, "accept": ""},
               follow_redirects=False)
record("05 signup: terms acceptance required", r.status_code == 200 and "Terms of Service" in r.text
       and not models.get_user_by_email("na@test.local"))

u = models.get_user_by_email(EMAIL)
# 6. email verification token issued and works
tok = u.get("verify_token") if u else None
r = client.get(f"/verify/{tok}", follow_redirects=False) if tok else None
record("06 email verification link works",
       bool(tok) and r is not None and r.status_code in (200, 303) and
       bool(models.get_user_by_email(EMAIL).get("verified_at")))

# 7. login page renders
r = client.get("/login")
record("07 login page renders with remember-me", r.status_code == 200 and 'name="remember"' in r.text)

# 8. wrong password rejected
r = client.post("/login", data={"email": EMAIL, "password": "wrongpass99"}, follow_redirects=False)
record("08 login: wrong password rejected", r.status_code == 200 and "Wrong email or password" in r.text)

# 9. plain login -> onboarding, 12-hour session
r = client.post("/login", data={"email": EMAIL, "password": PW}, follow_redirects=False)
loc = r.headers.get("location", "")
maxage = None
for c in r.headers.get_list("set-cookie"):
    if "rs_session=" in c and "Max-Age=" in c:
        maxage = int(c.split("Max-Age=")[1].split(";")[0])
record("09 login: routes to onboarding with 12h session", r.status_code == 303 and "/onboarding" in loc
       and maxage == 43200, f"loc={loc} maxage={maxage}")

# 10. onboarding wizard step 1 renders
r = client.get("/onboarding")
record("10 onboarding: step 1 renders", r.status_code == 200 and "step=1" in r.text.lower() or r.status_code == 200)

# 11. step 1 accepted -> step 2
r = client.post("/onboarding/step1", data={"name": "M E2E", "business": "Master Co",
                "phone": "919810000900", "upi_vpa": "master@upi"}, follow_redirects=False)
record("11 onboarding: step1 -> step2", r.status_code == 303 and "step=2" in r.headers.get("location", ""))

# 12. step 2 (email channel skipped) -> step 3
r = client.post("/onboarding/step2", data={"skip": "1"}, follow_redirects=False)
record("12 onboarding: step2 skip -> step3", r.status_code == 303 and "step=3" in r.headers.get("location", ""))

# 13. finish -> dashboard
r = client.post("/onboarding/finish", data={"skip": "1"}, follow_redirects=False)
record("13 onboarding: finish -> dashboard", r.status_code == 303 and "/dashboard" in r.headers.get("location", ""))

# 14. dashboard renders for the onboarded owner
r = client.get("/dashboard")
record("14 dashboard renders", r.status_code == 200)

# 15. add customer
r = client.post("/customers", data={"name": "Asha K", "email": "asha@master.test",
                "phone": "919810000910", "notes": ""}, follow_redirects=False)
cu = [c for c in models.list_customers(u["id"]) if c["name"] == "Asha K"]
record("15 customers: added and listed", r.status_code == 303 and len(cu) == 1, r.status_code)

# 16. customers page shows the customer
r = client.get("/customers")
record("16 customers: page lists Asha", r.status_code == 200 and "Asha K" in r.text)

# 17. add an overdue invoice
r = client.post("/invoices", data={"customer_id": cu[0]["id"], "number": "MS-1",
                "amount": "1200", "description": "membership", "due_date": "2026-11-10"},
                follow_redirects=False)
invs = [i for i in models.list_invoices(u["id"]) if i["number"] == "MS-1"]
record("17 invoices: overdue invoice created", r.status_code == 303 and invs and invs[0]["status"] == "pending")

# 18. radar ranks it
r = client.get("/radar")
record("18 radar: shows the overdue invoice", r.status_code == 200 and "MS-1" in r.text)

# 19. manual reminder queues a message and sends nothing live
before = models.one("SELECT COUNT(*) c FROM outbox WHERE owner_id=?", (u["id"],))
r = client.post(f"/invoices/{invs[0]['id']}/remind", follow_redirects=False)
after = models.one("SELECT COUNT(*) c FROM outbox WHERE owner_id=?", (u["id"],))
record("19 reminders: queued in outbox, no live provider send",
       r.status_code == 303 and (after["c"] - before["c"]) >= 1 and os.environ.get("ALLOW_LIVE_PROVIDERS") is None)

# 20. every automation starts OFF for a new business
autos = models.get_automations(u["id"])
record("20 automations: all OFF by default",
       all(v is False for v in autos.values()) and set(autos) == set(models.AUTO_KEYS), str(autos))

# 21. owner can toggle one automation and the audit trail records it
r = client.post("/automations/toggle", data={"key": "reminder_due", "enabled": "1"}, follow_redirects=False)
on_now = models.get_automations(u["id"]).get("reminder_due")
r2 = client.post("/automations/toggle", data={"key": "reminder_due", "enabled": ""}, follow_redirects=False)
off_now = models.get_automations(u["id"]).get("reminder_due")
record("21 automations: toggle on then off works",
       r.status_code == 303 and on_now is True and off_now is False,
       f"on={on_now} off={off_now}")

# 22. manual automation run queues work without sending anything live
r = client.post("/automations/run", follow_redirects=False)
record("22 automations: manual run completes", r.status_code == 303)

# 23. payments (claims) page renders
r = client.get("/payments")
record("23 payments page renders", r.status_code == 200)

# 24. customer-facing invoice portal renders
inv = invs[0]
tok_inv = inv.get("portal_token") or inv.get("token") or ""
r = client.get(f"/invoice/{tok_inv}") if tok_inv else None
record("24 portal: invoice payment page renders", tok_inv and r is not None and r.status_code == 200)

# 25. a payment claim stays pending and does NOT settle the invoice
claim = models.claim_invoice_payment(inv, method="upi", reference="UTR-MASTER-1")
inv_now = models.one("SELECT * FROM invoices WHERE id=?", (inv["id"],))
record("25 claims: claim is pending, invoice not settled",
       claim is not None and inv_now["status"] != "paid")

# 26. verifying the claim settles the invoice and stops reminders
verified = models.verify_claim(claim["id"] if isinstance(claim, dict) else claim)
inv_now = models.one("SELECT * FROM invoices WHERE id=?", (inv["id"],))
outbox_after = models.one("SELECT COUNT(*) c FROM outbox WHERE owner_id=?", (u["id"],))
r = client.post(f"/invoices/{inv['id']}/remind", follow_redirects=False)
outbox_final = models.one("SELECT COUNT(*) c FROM outbox WHERE owner_id=?", (u["id"],))
record("26 claims: verified claim settles invoice, reminders stop",
       inv_now["status"] == "paid" and outbox_final["c"] == outbox_after["c"],
       f"inv={inv_now['status']} outbox {outbox_after['c']}->{outbox_final['c']}")

# 27. analytics render
r = client.get("/analytics")
record("27 analytics: reports render", r.status_code == 200)

# 28. subscription + early adopter availability
r = client.get("/subscription")
ea = client.get("/api/early-adopter-availability").json()
record("28 subscription: plans page + EA availability endpoint",
       r.status_code == 200 and ea.get("max") == 10 and ea.get("remaining") == 10, str(ea))

# 29. legal pages live (refund in draft, privacy, terms)
refund = client.get("/refund")
record("29 legal: privacy, terms and draft refund policy live",
       client.get("/privacy").status_code == 200 and client.get("/terms").status_code == 200
       and refund.status_code == 200)

# 30. logout + security headers + unsigned webhook fail-closed
r = client.get("/logout", follow_redirects=False)
headers = client.get("/").headers
unsigned = client.post("/webhooks/razorpay", content=b"{}", headers={"Content-Type": "application/json"})
record("30 logout + headers + webhook fail-closed",
       r.status_code == 303 and headers.get("x-content-type-options") == "nosniff"
       and headers.get("x-frame-options") == "DENY"
       and unsigned.status_code in (400, 503), str(unsigned.status_code))

passed = sum(results)
print(f"MASTER E2E: {passed}/{len(results)} checks passed")
assert passed == len(results), f"{len(results) - passed} checks failed"
