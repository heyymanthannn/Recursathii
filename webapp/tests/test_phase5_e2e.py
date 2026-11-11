"""Phase 5 end-to-end test: one synthetic business through the whole workflow.

Covers: signup/API key -> invoice -> Razorpay failed-payment webhook -> event +
nudge queued -> scheduler sequence -> approval escalation -> owner approves ->
no fake sends -> payment verified -> reminders stop -> intelligence sees it ->
assistant read-only -> cross-tenant blocked -> reactivation campaign with
opt-out -> control-centre audit trail.
"""
import os, tempfile, time, hmac, hashlib, json
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_e2e_", suffix=".sqlite")
os.environ.pop("RZP_WEBHOOK_SECRET", None)  # keep the test default secret
from fastapi.testclient import TestClient
from app import main as app_main, models, intelligence, permissions, sequences

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

def signed(path, payload, secret="test-secret"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(path, content=body,
                       headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig})

now = time.time()
def date(days_ago): return time.strftime("%Y-%m-%d", time.gmtime(now - days_ago * 86400))

# ---------- 1. business onboards ----------
OID = models.create_user("e2e@example.com", "x", "E2E Owner", "E2E Co", "+919000000100", role="owner")["id"]
BID = models.create_user("other@example.com", "x", "Other", "Other Co", "+919000000101", role="owner")["id"]
key = models.create_api_key(OID, "e2e")
H = {"X-API-Key": key}
record("e2e: owner + api key ready", bool(key))

cu = models.create_customer(OID, "Asha", email="asha@e2e.com", phone="+919000000201")
inv = models.create_invoice(OID, cu["id"], "E2E-1", 120000, "membership", date(1))
record("e2e: customer + overdue invoice", inv["status"] == "pending")

# ---------- 2. failed payment arrives by webhook ----------
MID = "m_e2e01"
from app import billing
billing.onboard(MID, "e2e@example.com", "starter")
payload = {"event": "payment.failed", "payload": {"payment": {"entity": {
    "id": "pay_e2e1", "amount": 120000, "contact": "+919000000201",
    "error_reason": "insufficient_funds", "token_id": "tok_e2e1",
    "notes": {"merchant_id": MID, "name": "Asha"}}}}}
wr = signed("/webhooks/razorpay", payload)
record("e2e: signed payment.failed webhook accepted", wr.status_code == 200 and wr.json().get("processed") is True, str(wr.json())[:120])
bad = client.post("/webhooks/razorpay", content=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "X-Razorpay-Signature": "nope"})
record("e2e: forged signature rejected", bad.status_code == 400, str(bad.status_code))

# ---------- 3. scheduler runs the sequence (no provider -> no fake send) ----------
permissions.set_config(OID, {"quiet_hours": {"enabled": False},
                              "max_per_customer_per_day": 20, "max_per_customer_week": 100,
                              "max_per_channel_per_day": {"email": 100, "whatsapp": 100}})
owner = models.get_user(OID)
counts = __import__("app.automations", fromlist=["automations"]).run_for_owner(owner, "https://test.local")
o1 = models.one("SELECT * FROM outbox WHERE dedupe LIKE 'seq:day0%' AND owner_id=?", (OID,))
record("e2e: day0 step queued by scheduler", bool(o1), f"counts={counts}")
record("e2e: no provider means no fake send", o1["status"] in ("no_channel", "queued", "approval"), o1["status"])

# ---------- 4. escalation step holds for approval; owner approves the exact message ----------
inv7 = models.create_invoice(OID, cu["id"], "E2E-7", 50000, "plan", date(9))
__import__("app.automations", fromlist=["automations"]).run_for_owner(owner, "https://test.local")
appr = models.q("SELECT * FROM outbox WHERE owner_id=? AND status='approval'", (OID,))
record("e2e: escalation held for owner approval", len(appr) >= 1, f"n={len(appr)}")
if appr:
    before_text = appr[0]["body"]
    res = models.approve_outbox(OID, appr[0]["id"])
    after = models.get_outbox(appr[0]["id"])
    record("e2e: owner approval releases the exact message", after["status"] in ("queued", "sent", "no_channel") and after["body"] == before_text, after["status"])

# ---------- 5. payment verified -> reminders stop, recovery recorded ----------
p = models.record_payment_attempt(inv, "upi")
models.update_payment(p["id"], status="confirmed", amount=120000, confirmed_at=now)
models.mark_invoice_paid(inv, "upi", reference="UTR-OK-1")
client.post(f"/api/automations/invoices/{inv7['id']}/pause", headers=H, json={"paused": True})
before = models.one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (OID,))["n"]
__import__("app.automations", fromlist=["automations"]).run_for_owner(owner, "https://test.local")
after_n = models.one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (OID,))["n"]
record("e2e: verified payment stops further reminders", after_n == before, f"{before} -> {after_n}")
rec = intelligence.verified_recovery_rate(OID)
record("e2e: recovery rate sees the verified payment", rec["verified_recovered"] >= 1 and rec["verified_recovered_value"] >= 120000, str(rec)[:140])

# ---------- 6. intelligence + assistant, read-only, tenant-scoped ----------
ov = intelligence.overdue_analysis(OID)
record("e2e: intelligence reflects real records", ov["total_overdue_amount"] >= 50000, str(ov)[:120])
ans = intelligence.assistant_answer(OID, "how many overdue invoices do I have?")
record("e2e: assistant answers from records", ("1" in ans or "overdue" in ans.lower()) and "cannot" not in ans[:20], ans[:100])
ref = intelligence.assistant_answer(OID, "cancel invoice E2E-7")
record("e2e: assistant still refuses writes", "cannot" in ref.lower(), ref[:100])
bkey = models.create_api_key(BID, "other")
cross = client.get("/api/intelligence/overview", headers={"X-API-Key": bkey})
cross_empty = cross.json().get("overview", {}).get("overdue", {}).get("total_overdue_amount", 0) == 0
record("e2e: other tenant sees zero of owner A's data", cross.status_code == 200 and cross_empty, str(cross.json())[:140])

# ---------- 7. reactivation: dormant customer, campaign, opt-out ----------
old = models.create_customer(OID, "Vikram", email="vikram@e2e.com", phone="+919000000202")
# old verified purchase, 95 days ago, nothing open since
for _ in range(1):
    oi = models.create_invoice(OID, old["id"], "E2E-OLD", 8000, "shop", date(95))
    op = models.record_payment_attempt(oi, "upi")
    models.update_payment(op["id"], status="confirmed", amount=8000, confirmed_at=now - 86400 * 95)
    models.mark_invoice_paid(oi, "upi", reference="UTR-OLD")
dorm = client.get("/api/reactivation/dormant", headers=H)
dorm_ids = [d["id"] for d in dorm.json().get("dormant", [])]
record("e2e: dormant detection finds Vikram, not Asha", old["id"] in dorm_ids and cu["id"] not in dorm_ids, str(dorm_ids))
camp = client.post("/api/reactivation/campaigns", headers=H,
                   json={"segment": "One-time", "channels": ["email"],
                         "email_subject": "We miss you, Vikram",
                         "email_body": "Your saved 10% code is inside."})
record("e2e: campaign created as draft", camp.status_code == 200 and camp.json().get("campaign", {}).get("status") == "draft", str(camp.json())[:120])
cid = camp.json().get("campaign", {}).get("id")
ap = client.post(f"/api/reactivation/campaigns/{cid}/approve", headers=H)
run = client.post(f"/api/reactivation/campaigns/{cid}/run", headers=H)
queued = models.q("SELECT * FROM outbox WHERE owner_id=? AND tpl='reactivation'", (OID,))
record("e2e: approved campaign queues messages", run.status_code == 200 and len(queued) >= 1, str(run.json())[:120])
resp = client.post("/api/reactivation/response", headers=H,
                   json={"campaign_id": cid, "customer_id": old["id"], "kind": "opt_out"})
record("e2e: opt-out accepted", resp.status_code == 200, str(resp.json())[:100])
permissions.set_config(OID, {"max_per_customer_week": 1000})
run2 = client.post(f"/api/reactivation/campaigns/{cid}/run", headers=H)
after_opt = models.q("SELECT * FROM outbox WHERE owner_id=? AND tpl='reactivation' AND customer_id=?", (OID, old["id"]))
record("e2e: opted-out customer got nothing new", run2.status_code == 200 and len(after_opt) == len(queued), f"n={len(after_opt)}")

# ---------- 8. control centre: audit + config + pause ----------
aud = client.get("/api/automations/audit?limit=200", headers=H)
n_audit = len(aud.json().get("audit", []))
record("e2e: audit trail has the full history", n_audit >= 5, f"n={n_audit}")
cfg = client.get("/api/automations/control", headers=H)
record("e2e: control centre serves permissions + caps + sequence", cfg.status_code == 200 and "permissions" in cfg.json(), str(cfg.json())[:100])
pz = client.post("/api/automations/pause", headers=H, json={"paused": True, "reason": "e2e"})
c2 = __import__("app.automations", fromlist=["automations"]).run_for_owner(owner, "https://test.local")
record("e2e: owner pause stops all automations", c2.get("queued", 0) == 0 and c2.get("sent", 0) == 0, str(c2))
unpz = client.post("/api/automations/pause", headers=H, json={"paused": False})
record("e2e: pause releases", unpz.status_code == 200)

print(f"\n{sum(results)}/{len(results)} passed")
