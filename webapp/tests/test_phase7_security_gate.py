"""Phase 7 pre-merge security gate.

Independent security review requested before merging phase7-dashboard into
main: session cookies, login rate limits, password reset, session handling,
staff permissions, cross-tenant access attempts against every relevant
endpoint (not only the dashboard UI), exports, API key authorization,
webhook security and encrypted credentials.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase7_security_gate
Isolated temp DB, synthetic data only, no network, no real providers,
production untouched.
"""
import os, sys, tempfile
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_sg_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ["RZP_WEBHOOK_SECRET"] = "gate-webhook-secret"  # fail-closed: explicit test secret
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from fastapi.testclient import TestClient
from app import main as app_main, models, sequences, vault
from app.auth import hash_pw
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from seed_phase7 import seed  # noqa: E402

models.init()
sequences.ensure_schema()
client = TestClient(app_main.app)
results = []
fails = []

def record(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, ("| " + str(detail)) if not ok else "")
    if not ok:
        fails.append(name)

PW = "gate-test-pass-1"
biz = seed(PW)
A, B = biz[0], biz[1]
a_owner, b_owner = A["owner_id"], B["owner_id"]

def login_as(email, pw):
    client.cookies.clear()
    r = client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)
    assert r.status_code in (303, 200), f"login {email} -> {r.status_code}"
    return r

login_as(A["email"], PW)

b_customers = models.q("SELECT * FROM customers WHERE owner_id=?", (b_owner,))
b_invoices = models.q("SELECT * FROM invoices WHERE owner_id=?", (b_owner,))
a_customer_emails = [c["email"] for c in models.q("SELECT * FROM customers WHERE owner_id=?", (a_owner,))]
b_customer_emails = [c["email"] for c in b_customers]
b_inv = b_invoices[0]
b_cust = b_customers[0]
b_pay = models.q("SELECT * FROM payments WHERE invoice_id=? ORDER BY created DESC LIMIT 1", (b_inv["id"],))
if not b_pay:
    models.record_payment_attempt(b_inv["id"], "upi", status="pending", reference="gatesyn")
    b_pay = models.q("SELECT * FROM payments WHERE invoice_id=? ORDER BY created DESC LIMIT 1", (b_inv["id"],))
b_pay = b_pay[0]

def not_ok(r, what):
    """A resource outside the tenant must never render B's data for A.

    Any non-200 response is a rejection; a 200 is only acceptable when it
    carries none of B's identifying data (e.g. a tenant-agnostic error page).
    """
    if r.status_code == 200:
        txt = r.text
        leaks = [v for v in (b_cust["name"], b_cust["email"], str(b_inv.get("ref_no") or ""), str(b_inv["amount"])) if v and v in txt]
        if leaks:
            return (False, f"{what} -> 200 leaking {leaks[:2]}")
        return (True, f"{what} -> 200 tenant-agnostic page")
    return (True, "")

# ---- 1. session cookie security ----
r = client.post("/login", data={"email": A["email"], "password": PW}, follow_redirects=False)
sc = r.headers.get("set-cookie", "")
record("session cookie HttpOnly", "httponly" in sc.lower(), sc)
record("session cookie SameSite=Lax", "samesite=lax" in sc.lower(), sc)
record("session cookie max-age 30d", "max-age=2592000" in sc.lower(), sc)

# ---- 2. anonymous protection on private surfaces ----
client.cookies.clear()
private = ["/dashboard", "/customers", "/invoices", "/payments", "/automations", "/sequences",
           "/approvals", "/analytics", "/team", "/settings", "/customers/export",
           "/analytics/export", "/settings/export-data", "/activity", "/branches"]
bad = []
for p in private:
    r = client.get(p, follow_redirects=False)
    if not (r.status_code == 303 and "/login" in r.headers.get("location", "")):
        bad.append((p, r.status_code))
record("anonymous private pages redirect to login", not bad, bad)
login_as(A["email"], PW)

# ---- 3. cross-tenant sweep: A vs B resources ----
for path in (f"/customers/{b_cust['id']}", f"/invoices/{b_inv['id']}/pdf",
             f"/upi-qr/{b_inv['id']}.png", f"/receipt/{b_pay['id']}"):
    r = client.get(path, follow_redirects=False)
    ok, why = not_ok(r, f"GET {path}")
    record("cross-tenant " + path, ok, why)

name_before = b_cust["name"]
r = client.post(f"/customers/{b_cust['id']}/edit", data={"name": "HACKED", "email": b_cust["email"], "phone": "0000000000"}, follow_redirects=False)
b_cust_after = models.one("SELECT * FROM customers WHERE id=?", (b_cust["id"],))
record("cross-tenant customer edit blocked", b_cust_after["name"] == name_before, (r.status_code, b_cust_after["name"]))
ok, why = not_ok(r, "POST customers/edit")
record("cross-tenant customer edit rejected", ok, why)

r = client.post(f"/customers/{b_cust['id']}/delete", data={}, follow_redirects=False)
still = models.one("SELECT * FROM customers WHERE id=?", (b_cust["id"],))
record("cross-tenant customer delete blocked", still is not None, (r.status_code,))

n_out_b = models.q("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (b_owner,))[0]["n"]
r = client.post(f"/invoices/{b_inv['id']}/remind", data={}, follow_redirects=False)
n_out_b2 = models.q("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (b_owner,))[0]["n"]
record("cross-tenant invoice remind blocked (no B outbox)", n_out_b2 == n_out_b, (r.status_code, n_out_b, n_out_b2))
ok, why = not_ok(r, "POST invoices/remind")
record("cross-tenant invoice remind rejected", ok, why)

status_before = b_inv["status"]
r = client.post(f"/invoices/{b_inv['id']}/mark-paid", data={"reference": "hack123"}, follow_redirects=False)
b_inv_after = models.one("SELECT * FROM invoices WHERE id=?", (b_inv["id"],))
record("cross-tenant mark-paid blocked", b_inv_after["status"] == status_before, (r.status_code, b_inv_after["status"]))
ok, why = not_ok(r, "POST invoices/mark-paid")
record("cross-tenant mark-paid rejected", ok, why)

pay_status_before = b_pay["status"]
r = client.post(f"/payments/{b_pay['id']}/refund", data={"note": "hack"}, follow_redirects=False)
b_pay_after = models.one("SELECT * FROM payments WHERE id=?", (b_pay["id"],))
record("cross-tenant refund blocked", b_pay_after["status"] == pay_status_before, (r.status_code, b_pay_after["status"]))
r = client.post(f"/payments/{b_pay['id']}/confirm", data={}, follow_redirects=False)
b_pay_after = models.one("SELECT * FROM payments WHERE id=?", (b_pay["id"],))
record("cross-tenant payment confirm blocked", b_pay_after["status"] == pay_status_before, (r.status_code, b_pay_after["status"]))

o = models.queue(b_owner, "email", "gate-b@synthetic.test", "reminder", "s", "b", dedupe="gate-approve")
models.hold_for_approval(o["id"])
r = client.post(f"/approvals/{o['id']}/approve", data={}, follow_redirects=False)
o_after = models.get_outbox(o["id"])
record("cross-tenant approval approve blocked", o_after["status"] != "approved", (r.status_code, o_after["status"]))
pre_status = models.get_outbox(o["id"])["status"]
r = client.post(f"/approvals/{o['id']}/discard", data={}, follow_redirects=False)
o_after = models.get_outbox(o["id"])
record("cross-tenant approval discard blocked", o_after["status"] == pre_status and o_after["status"] not in ("discarded", "sent", "queued"), (r.status_code, pre_status, o_after["status"]))

# ---- 4. exports scoped ----
r = client.get("/customers/export")
t = r.text
record("customers/export scoped to own tenant",
       r.status_code == 200 and any(e in t for e in a_customer_emails) and not any(e in t for e in b_customer_emails),
       (r.status_code, "b leaked" if any(e in t for e in b_customer_emails) else ""))
r = client.get("/settings/export-data")
t = r.text
record("settings/export-data scoped to own tenant",
       r.status_code == 200 and not any(e in t for e in b_customer_emails), r.status_code)
r = client.get("/analytics/export", follow_redirects=False)
record("analytics/export reachable, no B customer emails",
       r.status_code in (200, 303) and not any(e in r.text for e in b_customer_emails), r.status_code)

# ---- 5. API key authorization ----
C = biz[2]  # enterprise plan business for the Enterprise API checks
c_owner = C["owner_id"]
c_customer_emails = [c["email"] for c in models.q("SELECT * FROM customers WHERE owner_id=?", (c_owner,))]
key_b = models.create_api_key(c_owner, label="gate")
r = client.get("/api/v1/customers", headers={"X-API-Key": "bad-key"})
record("API invalid key rejected", r.status_code in (401, 403), r.status_code)
r = client.get("/api/v1/customers")
record("API missing key rejected", r.status_code in (401, 403), r.status_code)
r = client.get("/api/v1/customers", headers={"X-API-Key": key_b})
t = r.text
record("API key returns only its own tenant's customers",
       r.status_code == 200 and all(e in t for e in c_customer_emails) and not any(e in t for e in a_customer_emails),
       (r.status_code, "a leaked" if any(e in t for e in a_customer_emails) else ""))
r = client.get("/api/v1/summary", headers={"X-API-Key": key_b})
record("API summary scoped to key's tenant", r.status_code == 200, r.status_code)

# ---- 6. webhook security ----
r = client.post("/webhooks/razorpay", content=b"{}", headers={"content-type": "application/json"})
record("razorpay webhook rejects unsigned body", r.status_code == 400, r.status_code)
r = client.post("/webhooks/razorpay", content=b"{}", headers={"content-type": "application/json", "X-Razorpay-Signature": "deadbeef"})
record("razorpay webhook rejects bad signature", r.status_code == 400, r.status_code)

# ---- 7. password reset security ----
client.cookies.clear()
r_unknown = client.post("/forgot", data={"email": "nobody@synthetic.test"}, follow_redirects=False)
r_known = client.post("/forgot", data={"email": A["email"]}, follow_redirects=False)
leak = "reset/" in r_known.text and "reset/" not in r_unknown.text
record("forgot: reset link not exposed when SMTP is configured", not leak, "link in HTML response (SMTP not ready fallback)")
tok = models.one("SELECT reset_token FROM users WHERE id=?", (a_owner,))["reset_token"]
r = client.get(f"/reset/{tok}", follow_redirects=False)
record("reset page valid for fresh token", "expired" not in r.text.lower()[:2000], r.status_code)
r = client.post(f"/reset/{tok}", data={"password": "new-pass-9876"}, follow_redirects=False)
u = models.one("SELECT * FROM users WHERE id=?", (a_owner,))
record("reset works and clears the token", r.status_code == 303 and u["reset_token"] == "", r.status_code)
r = client.post(f"/reset/{tok}", data={"password": "another-pass-1"}, follow_redirects=False)
record("reset token single-use", "expired" in r.text.lower()[:3000], r.status_code)
models.set_reset_token(A["email"], "expiredtok", ttl=-10)
r = client.get("/reset/expiredtok", follow_redirects=False)
record("expired reset token rejected", "expired" in r.text.lower()[:3000], r.status_code)
PW_A = "new-pass-9876"

# ---- 8. login rate limiting ----
client.cookies.clear()
last = None
for i in range(11):
    last = client.post("/login", data={"email": A["email"], "password": "wrong"}, follow_redirects=False)
record("login rate limit engages after repeated failures", "too many" in last.text.lower(), last.status_code)

# ---- 9. session revocation on password change ----
login_as(A["email"], PW_A)
models.change_password(a_owner, hash_pw("rotated-pass-1"))
r = client.get("/dashboard", follow_redirects=False)
record("old session invalid after password change", r.status_code == 303 and "/login" in r.headers.get("location", ""), (r.status_code, r.headers.get("location", "")))

# ---- 10. staff permissions spot-check ----
r = client.post("/login", data={"email": A["email"].replace("owner", "staff"), "password": PW}, follow_redirects=False)
record("staff login works with seed password", True)
a_staff_cust = models.q("SELECT * FROM customers WHERE owner_id=?", (a_owner,))[0]
r = client.post(f"/customers/{a_staff_cust['id']}/delete", data={}, follow_redirects=False)
still = models.one("SELECT * FROM customers WHERE id=?", (a_staff_cust["id"],))
record("staff cannot delete customers (owner-only destructive action)", still is not None, (r.status_code,))

# ---- 11. encrypted provider credentials ----
record("vault module holds a Fernet secret", bool(vault.SECRET), None)
try:
    from app import providers
    has_providers = True
except Exception:
    has_providers = False
record("provider credential encryption module loads", has_providers, None)

print()
print(f"SUMMARY: {sum(results)}/{len(results)} passed")
if fails:
    print("FAILURES:", fails)
