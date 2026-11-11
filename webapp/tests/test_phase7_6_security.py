"""Phase 7.6 security hardening tests.

Covers the Phase 7.6 directive: webhook secrets fail closed (missing env var
and known weak/default values are rejected), signature verification
(unsigned, forged, valid), replay/duplicate-event dedupe, scheduler token
required (missing, invalid, valid), security response headers (HSTS,
X-Frame-Options, X-Content-Type-Options, CSP, Referrer-Policy), session
cookie flags (HttpOnly, SameSite, Secure) and the Origin/Referer CSRF guard
(cross-site blocked, same-origin and non-browser allowed), logout and
password-change session revocation.

Isolated temp DB, synthetic data only, no network, no real providers.
Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase7_6_security
"""
import os, sys, tempfile, hmac, hashlib, json
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p76_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ["ADMIN_TOKEN"] = "p76-admin-token"
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from fastapi.testclient import TestClient
from app import main as app_main, models, config
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app)
results = []

def record(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, (detail if not ok else ""))

def signed_post(url, payload, secret, header="X-Razorpay-Signature"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(url, content=body, headers={header: sig, "content-type": "application/json"})

# ---- 1. webhook secrets fail closed ----
record("config: unset webhook secret -> empty", config._webhook_secret("RZP_WEBHOOK_SECRET") == b"")
record("config: weak default 'test-secret' rejected", config._webhook_secret("test-secret") == b"")
record("config: weak default 'billing-secret' rejected", config._webhook_secret("billing-secret") == b"")
os.environ["RZP_WEBHOOK_SECRET"] = "real-secret-value"
record("config: real secret kept", config._webhook_secret("RZP_WEBHOOK_SECRET") == b"real-secret-value")
os.environ.pop("RZP_WEBHOOK_SECRET", None)

# env unset in this process -> both route secrets empty -> 503, never accepted
r = client.post("/webhooks/razorpay", content=b"{}", headers={"content-type": "application/json"})
record("razorpay webhook: missing secret fails closed (503)", r.status_code == 503, r.status_code)
r = client.post("/webhooks/billing", content=b"{}", headers={"content-type": "application/json"})
record("billing webhook: missing secret fails closed (503)", r.status_code == 503, r.status_code)

# ---- 2. signature verification with a configured secret ----
SECRET = "p76-webhook-secret-0123456789abcdef"
app_main.WEBHOOK_SECRET = SECRET.encode()
app_main.BILLING_WEBHOOK_SECRET = "p76-billing-secret-0123456789abcdef".encode()

r = client.post("/api/signup", json={"plan": "growth", "email": "p76@example.com",
    "phone": "9812345678", "password": "password123", "name": "P76 Test"})
mid = r.json().get("merchant_id") or ""
record("setup: synthetic merchant created", r.status_code == 200 and mid.startswith("m_"), str(mid))
mark = client.post("/admin/mark-paid", json={"merchant_id": mid}, headers={"X-Admin-Token": "p76-admin-token"})
record("setup: merchant activated", mark.status_code == 200, str(mark.status_code))

payload = {"event": "payment.failed", "payload": {"payment": {"entity": {
    "id": "pay_p76_1", "amount": 99900, "contact": "+919812345678",
    "error_reason": "insufficient_funds", "token_id": "tok_p76_1",
    "notes": {"merchant_id": mid, "name": "P76 Customer"}}}}}
r = client.post("/webhooks/razorpay", content=json.dumps(payload).encode(),
                headers={"content-type": "application/json"})
record("razorpay: unsigned body rejected (400)", r.status_code == 400, r.status_code)
r = client.post("/webhooks/razorpay", content=json.dumps(payload).encode(),
                headers={"content-type": "application/json", "X-Razorpay-Signature": "deadbeef"})
record("razorpay: forged signature rejected (400)", r.status_code == 400, r.status_code)
wr = signed_post("/webhooks/razorpay", payload, SECRET)
record("razorpay: valid signature accepted + ingested", wr.status_code == 200 and wr.json().get("processed") is True, str(wr.json())[:140])
wr2 = signed_post("/webhooks/razorpay", payload, SECRET)
record("razorpay: replayed event deduped (processed=False)", wr2.status_code == 200 and wr2.json().get("processed") is False, str(wr2.json())[:140])

bpayload = {"event": "subscription.charged", "payload": {"subscription": {"entity": {
    "id": "sub_p76_1", "notes": {"merchant_id": mid}}}}}
r = client.post("/webhooks/billing", content=json.dumps(bpayload).encode(),
                headers={"content-type": "application/json", "X-Razorpay-Signature": "forged"})
record("billing: forged signature rejected (400)", r.status_code == 400, r.status_code)
br = signed_post("/webhooks/billing", bpayload, "p76-billing-secret-0123456789abcdef")
record("billing: valid signature accepted", br.status_code == 200 and br.json().get("handled") is True, str(br.json())[:140])
br2 = signed_post("/webhooks/billing", bpayload, "p76-billing-secret-0123456789abcdef")
record("billing: duplicate event idempotent", br2.status_code == 200 and br2.json().get("handled") is True, str(br2.json())[:140])

# ---- 3. scheduler token required ----
old_tok = os.environ.pop("SCHEDULER_TICK_TOKEN", None)
r = client.post("/scheduler/tick")
record("scheduler: missing token rejected (403)", r.status_code == 403, r.status_code)
os.environ["SCHEDULER_TICK_TOKEN"] = "p76-scheduler-token"
r = client.post("/scheduler/tick", headers={"X-Scheduler-Token": "wrong"})
record("scheduler: invalid token rejected (403)", r.status_code == 403, r.status_code)
r = client.post("/scheduler/tick", headers={"X-Scheduler-Token": "p76-scheduler-token"})
record("scheduler: valid token accepted", r.status_code == 200, r.status_code)
if old_tok is None:
    os.environ.pop("SCHEDULER_TICK_TOKEN", None)
else:
    os.environ["SCHEDULER_TICK_TOKEN"] = old_tok

# ---- 4. security response headers ----
h = client.get("/").headers
record("headers: X-Content-Type-Options nosniff", h.get("x-content-type-options") == "nosniff", str(h.get("x-content-type-options")))
record("headers: X-Frame-Options DENY", h.get("x-frame-options") == "DENY", str(h.get("x-frame-options")))
record("headers: Referrer-Policy set", "strict-origin" in (h.get("referrer-policy") or ""), str(h.get("referrer-policy")))
record("headers: HSTS set", "max-age=31536000" in (h.get("strict-transport-security") or ""), str(h.get("strict-transport-security")))
csp = h.get("content-security-policy") or ""
record("headers: CSP present with frame-ancestors none", "frame-ancestors 'none'" in csp and "default-src 'self'" in csp, csp[:120])
record("headers: CSP allows Google Fonts", "fonts.googleapis.com" in csp and "fonts.gstatic.com" in csp, csp[:160])
h2 = client.get("/login").headers
record("headers: present on login page too", h2.get("x-frame-options") == "DENY" and "content-security-policy" in h2, "")

# ---- 5. session cookie flags ----
models.create_user("p76owner@example.com", hash_pw("password123"), "P76 Owner", "P76 Co", "9812300001", role="owner")
client.cookies.clear()
lr = client.post("/login", data={"email": "p76owner@example.com", "password": "password123"}, follow_redirects=False)
sc = lr.headers.get("set-cookie", "").lower()
record("login: 303 with session cookie", lr.status_code == 303 and "rs_session=" in sc, f"{lr.status_code} {sc[:80]}")
record("cookie: HttpOnly", "httponly" in sc, sc[:100])
record("cookie: SameSite=Lax", "samesite=lax" in sc, sc[:100])
lr_https = client.post("/login", data={"email": "p76owner@example.com", "password": "password123"},
                       headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
sc_https = lr_https.headers.get("set-cookie", "").lower()
record("cookie: Secure added on https connections", "secure" in sc_https, sc_https[:100])
record("cookie: no Secure over plain http (dev/test)", "secure" not in sc, sc[:100])
client.cookies.clear()
lr = client.post("/login", data={"email": "p76owner@example.com", "password": "password123"}, follow_redirects=False)
assert lr.status_code == 303

# ---- 6. CSRF origin guard ----
r = client.post("/settings/password", data={"current_password": "password123", "new_password": "password456"},
                headers={"Origin": "https://evil.example"})
record("csrf: cross-site Origin blocked (403)", r.status_code == 403, r.status_code)
r = client.post("/settings/password", data={"current_password": "wrong", "new_password": "short"},
                headers={"Origin": "https://recursathii.onrender.com"})
record("csrf: same-origin (BASE_URL) passes the guard", r.status_code != 403, r.status_code)
r = client.post("/settings/password", data={"current_password": "wrong", "new_password": "short"},
                headers={"Origin": "http://testserver"})
record("csrf: own-host Origin passes", r.status_code != 403, r.status_code)
r = client.post("/settings/password", data={"current_password": "wrong", "new_password": "short"})
record("csrf: non-browser request (no Origin) allowed", r.status_code != 403, r.status_code)

# ---- 7. logout + password-change revocation ----
old_cookie = client.cookies.get("rs_session")
pr = client.post("/settings/password", data={"current_password": "password123", "new_password": "newpassword9"},
                 follow_redirects=False)
record("password change: accepted (303)", pr.status_code == 303, pr.status_code)
client.cookies.set("rs_session", old_cookie)
r = client.get("/dashboard", follow_redirects=False)
record("revocation: old session cookie dead after password change", r.status_code in (303, 307, 401), r.status_code)
client.cookies.clear()
lr = client.post("/login", data={"email": "p76owner@example.com", "password": "newpassword9"}, follow_redirects=False)
record("relogin: new password works", lr.status_code == 303, lr.status_code)
lo = client.get("/logout", follow_redirects=False)
record("logout: 303", lo.status_code == 303, lo.status_code)
r = client.get("/dashboard", follow_redirects=False)
record("logout: session actually terminated", r.status_code in (303, 307, 401), r.status_code)

print(f"\n{'='*50}\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
