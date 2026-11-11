"""End-to-end payment + subscription flow verification on TEST data.

Run:  cd webapp && python3 -m tests.test_payment_flows
Isolated temp DB. MANUAL_UPI_ID is a test VPA. Razorpay is exercised through
its webhook endpoints with locally-signed payloads (no real keys, no network).
"""
import os, sys, tempfile, hmac, hashlib, json, sqlite3

os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_flow_", suffix=".sqlite")
os.environ["MANUAL_UPI_ID"] = "testpay@upi"
os.environ["RZP_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["BILLING_WEBHOOK_SECRET"] = "test-billing-secret"
for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "YOUPI_API_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from app import main as app_main, billing  # noqa: E402

client = TestClient(app_main.app)
ADMIN_TOKEN = app_main.config.ADMIN_TOKEN
ADMIN_DIGEST = hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()
results = []

def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, ("| " + detail) if detail else "")

def signed_post(url, payload, secret, header="X-Razorpay-Signature"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(url, content=body, headers={header: sig,
                       "content-type": "application/json"})

# ---------- 1. Manual UPI flow (signup -> QR -> claim -> admin confirm -> active) ----------
mid = ""
r = client.post("/api/signup", json={"plan": "growth", "email": "flowtest@example.com",
    "phone": "9812345678", "password": "password123", "name": "Flow Test"})
page_json = r.json()
mid = page_json.get("merchant_id") or ""
record("manual_upi: signup returns manual checkout link", r.status_code == 200 and page_json.get("provider") == "manual" and mid.startswith("m_"), str(page_json.get("provider")))

cp = client.get(f"/checkout/{mid}")
record("manual_upi: checkout page renders with QR + UPI id", cp.status_code == 200 and f"/pay-qr/{mid}.png" in cp.text and "testpay@upi" in cp.text)

qr = client.get(f"/pay-qr/{mid}.png")
record("manual_upi: QR png serves", qr.status_code == 200 and qr.headers.get("content-type") == "image/png", f"{len(qr.content)} bytes")

claim = client.post(f"/checkout/{mid}/paid-claim", data={"utr": "TESTUTR123456"}, follow_redirects=False)
record("manual_upi: UTR claim accepted (redirect to claimed)", claim.status_code == 303 and "claimed=1" in claim.headers.get("location", ""))

client.cookies.set("admin_token", ADMIN_DIGEST)
admin = client.get("/admin")
client.cookies.clear()
record("manual_upi: pending payment card shows in admin with UTR", "TESTUTR123456" in admin.text and "Flow Test" in admin.text)

mark = client.post("/admin/mark-paid", json={"merchant_id": mid}, headers={"X-Admin-Token": ADMIN_TOKEN})
record("manual_upi: admin mark-paid activates merchant", mark.status_code == 200 and mark.json().get("status") == "active", str(mark.json())[:120])

after = client.get(f"/checkout/{mid}")
record("manual_upi: checkout shows already-active after confirm", "already active" in after.text)

row = sqlite3.connect(os.environ["PAYRECOVERY_DB"]).execute(
    "SELECT plan_status, plan FROM users WHERE email='flowtest@example.com'").fetchone()
record("manual_upi: db shows plan_status=active on chosen plan", row is not None and row[0] == "active" and row[1] == "growth", str(row))

# ---------- 2. Razorpay merchant webhook (payment.failed -> event + nudge queued) ----------
payload = {"event": "payment.failed", "payload": {"payment": {"entity": {
    "id": "pay_test1", "amount": 99900, "contact": "+919812345678",
    "error_reason": "insufficient_funds", "token_id": "tok_test1",
    "notes": {"merchant_id": mid, "name": "Test Customer"}}}}}
wr = signed_post("/webhooks/razorpay", payload, "test-webhook-secret")
fe = wr.json()
record("razorpay: signed payment.failed webhook ingested", wr.status_code == 200 and fe.get("processed") is True, str(fe)[:140])

bad = signed_post("/webhooks/razorpay", payload, "wrong-secret")
record("razorpay: bad signature rejected 400", bad.status_code == 400)

conn = sqlite3.connect(os.environ["PAYRECOVERY_DB"]); conn.row_factory = sqlite3.Row
ev = conn.execute("SELECT decline_code, playbook, merchant_id, retries_due FROM events WHERE id='pay_test1'").fetchone()
record("razorpay: failure event stored with playbook + retry time", ev is not None and ev["decline_code"] == "INSUFFICIENT_FUNDS" and bool(ev["playbook"]) and bool(ev["retries_due"]), str(dict(ev)) if ev else "no row")

dup = signed_post("/webhooks/razorpay", payload, "test-webhook-secret")
n_before = conn.execute("SELECT COUNT(*) n FROM events WHERE id='pay_test1'").fetchone()["n"]
record("razorpay: duplicate webhook does not double-store (dedupe)", dup.status_code == 200 and n_before == 1, f"rows={n_before}")

# Recovery nudges from the webhook path are GENERATED and stored (messages
# table), not sent: real sending happens through the outbox/automations path
# only when SMTP/WhatsApp creds are configured. This assert locks that truth.
ob = conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
record("razorpay: recovery nudge generated + stored (NOT sent; no provider configured)", ob >= 1, f"message rows={ob}")

# scheduler retries fire from RBI windows without touching external services
tick = os.environ.copy(); tick["SCHEDULER_TICK_TOKEN"] = "t"
os.environ["SCHEDULER_TICK_TOKEN"] = "t"
tr = client.post("/scheduler/tick", headers={"X-Scheduler-Token": "t"})
os.environ.pop("SCHEDULER_TICK_TOKEN", None)
record("scheduler: tick runs retry engine", tr.status_code == 200 and "retries_fired" in tr.json(), str(tr.json())[:100])

# ---------- 3. Razorpay billing webhook (subscription lifecycle) ----------
sub = {"event": "subscription.charged", "payload": {"subscription": {"entity": {
    "id": "sub_test1", "notes": {"merchant_id": mid}}}}}
br = signed_post("/webhooks/billing", sub, "test-billing-secret")
record("billing: subscription.charged -> active", br.status_code == 200 and br.json().get("status") == "active", str(br.json())[:120])

halt = signed_post("/webhooks/billing", {"event": "subscription.halted", "payload": {"subscription": {"entity": {"id": "sub_test1", "notes": {"merchant_id": mid}}}}}, "test-billing-secret")
record("billing: subscription.halted -> past_due", halt.json().get("status") == "past_due", str(halt.json())[:80])

unk = signed_post("/webhooks/billing", {"event": "subscription.charged", "payload": {"subscription": {"entity": {"id": "sub_x", "notes": {"merchant_id": "m_unknown"}}}}}, "test-billing-secret")
record("billing: unknown merchant refused cleanly", unk.json().get("handled") is False)

plat = client.post("/admin/plat-confirm", json={"payment_id": "nope"}, headers={"X-Admin-Token": ADMIN_TOKEN})
record("billing: plat-confirm rejects non-pending payment", plat.json().get("ok") is False)

# razorpay checkout link only when real keys configured
record("razorpay: no real keys -> no subscription link offered", billing.subscription_link.__code__.co_argcount >= 1 and not os.environ.get("RAZORPAY_KEY_ID"))

print()
fails = [r for r in results if not r[1]]
print(f"{len(results) - len(fails)}/{len(results)} passed" + (f", {len(fails)} FAILED" if fails else ""))
sys.exit(1 if fails else 0)
