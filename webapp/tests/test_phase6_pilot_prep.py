"""Phase 6 controlled-pilot preparation tests.

Covers the Phase 6 directive: fresh-install and existing-database migrations,
duplicate webhook events, retry safety (no duplicate messages), tenant
isolation across APIs, reports, compliance exports and background jobs,
owner vs staff vs viewer roles, dispute/cancellation suppression,
quiet hours (21:00-08:00 IST), verified-payment suppression, and imported
payments staying out of recovered revenue.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase6_pilot_prep
Isolated temp DB, synthetic data only, no network, no real providers.
"""
import os, sys, tempfile, time, hmac, hashlib, json, datetime
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p6_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from app import main as app_main, models, pilot, permissions, intelligence, sequences, automations
from app.auth import hash_pw

models.init()
pilot._ensure()
client = TestClient(app_main.app)
ADMIN_TOKEN = app_main.config.ADMIN_TOKEN
_webhook_secret = app_main.config.WEBHOOK_SECRET
if isinstance(_webhook_secret, bytes):
    _webhook_secret = _webhook_secret.decode()
results = []

def record(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, ("| " + str(detail)) if not ok else "")

def signed_post(url, payload):
    body = json.dumps(payload).encode()
    sig = hmac.new(_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(url, content=body,
                       headers={"X-Razorpay-Signature": sig, "content-type": "application/json"})

now = time.time()
def date(d): return time.strftime("%Y-%m-%d", time.gmtime(now - d * 86400))
def n_outbox(owner_id, dest=None):
    if dest:
        return models.q("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND dest=?", (owner_id, dest))[0]["n"]
    return models.q("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (owner_id,))[0]["n"]

# ---- 1. fresh install: schema complete, pilot layer usable ------------------
cols = [r["name"] for r in models.q("PRAGMA table_info(outbox)")]
record("p6: fresh install has outbox.customer_id", "customer_id" in cols, str(cols))
record("p6: fresh install has provider delivery-ladder columns",
       all(c in cols for c in ("provider_message_id", "accepted_at", "delivered_at",
                               "read_at", "failed_at", "bounced_at")), str(cols))
ptables = ("pilot_consents", "pilot_imports", "pilot_reauth", "trust_references",
           "reference_verifications", "pilot_backups", "pilot_workspaces")
have = {r["name"] for r in models.q("SELECT name FROM sqlite_master WHERE type='table'")}
record("p6: fresh install has all pilot tables", all(t in have for t in ptables), str(sorted(have))[:400])
m = pilot.pilot_metrics()
record("p6: monitoring metrics work on fresh install", isinstance(m, dict) and "verified_recovered" in m, str(m)[:120])

# ---- 2. two isolated owners --------------------------------------------------
A = models.create_user("p6a@example.com", hash_pw("ownerpass1"), "Owner A", "A Co", "+919000000011", role="owner")["id"]
B = models.create_user("p6b@example.com", hash_pw("ownerpass2"), "Owner B", "B Co", "+919000000012", role="owner")["id"]
permissions.set_config(A, {"automations_enabled": True})
permissions.authorize_channel(A, "email")
permissions.authorize_channel(A, "whatsapp")
permissions.set_config(B, {"automations_enabled": False})
cuA = models.create_customer(A, "Asha A", email="asha@p6.com", phone="+919000000021")
models.create_invoice(A, cuA["id"], "P6-A1", 50000, "plan", date(5))
models.ex("UPDATE users SET plan='enterprise', plan_status='active' WHERE id IN (?,?)", (A, B))

keyA = models.create_api_key(A, "p6")
keyB = models.create_api_key(B, "p6")
rA = client.get("/api/v1/customers", headers={"X-API-Key": keyA})
rB = client.get("/api/v1/customers", headers={"X-API-Key": keyB})
record("p6: A's API key sees A's customers", rA.status_code == 200 and "asha@p6.com" in rA.text, f"{rA.status_code} {rA.text[:100]}")
record("p6: B's API key sees none of A's customers", rB.status_code == 200 and "asha@p6.com" not in rB.text, f"{rB.status_code} {rB.text[:100]}")
rno = client.get("/api/v1/customers", headers={"X-API-Key": "not-a-key"})
record("p6: bad API key rejected", rno.status_code in (401, 403), str(rno.status_code))

# background job isolation: scheduler run for A queues only A
ownerA = models.get_user(A)
automations.run_for_owner(ownerA, "")
rows = models.q("SELECT DISTINCT owner_id FROM outbox")
record("p6: scheduler run for A queues only A's rows",
       len(rows) == 1 and rows[0]["owner_id"] == A and n_outbox(A) > 0,
       f"owners={[r['owner_id'] for r in rows]} n={n_outbox(A)}")
record("p6: B stays untouched by A's scheduler run", n_outbox(B) == 0, f"n={n_outbox(B)}")

# report isolation
snapB = json.dumps(intelligence.intelligence_snapshot(B))
odA, odB = intelligence.overdue_analysis(A), intelligence.overdue_analysis(B)
record("p6: B's intelligence report contains none of A's data",
       "Asha" not in snapB and "P6-A1" not in snapB and odB["total_overdue_invoices"] == 0,
       f"overdueB={odB['total_overdue_invoices']}")
record("p6: A's report shows A's overdue invoice", odA["total_overdue_invoices"] >= 1, str(odA)[:120])

# compliance export isolation
ceB = json.dumps(pilot.compliance_export(B))
record("p6: B's compliance export carries no A records", A not in ceB and "asha" not in ceB.lower(), ceB[:120])

# ---- 3. roles: owner vs staff vs viewer --------------------------------------
S = models.create_user("p6staff@example.com", hash_pw("staffpass1"), "Staff Sam", "A Co",
                       "+919000000013", role="staff", parent_id=A)["id"]
V = models.create_user("p6viewer@example.com", hash_pw("viewerpass1"), "Vera V", "A Co",
                       "+919000000014", role="viewer", parent_id=A)["id"]
r = client.post("/login", data={"email": "p6staff@example.com", "password": "staffpass1"})
record("p6: staff can log in", r.status_code == 200, str(r.status_code))
for path in ("/billing", "/settings", "/team"):
    resp = client.get(path)
    record(f"p6: staff blocked from {path}", resp.status_code == 403, str(resp.status_code))
resp = client.get("/admin")
record("p6: staff cannot open the admin console",
       "Pending payments" not in resp.text and "Confirm payment" not in resp.text,
       f"console content leaked: {'Pending' in resp.text}")
client.post("/logout")
r = client.post("/login", data={"email": "p6viewer@example.com", "password": "viewerpass1"})
resp = client.post("/team/add", data={"name": "x", "email": "x@x.com"})
record("p6: viewer POST is read-only 403", resp.status_code == 403, str(resp.status_code))
client.post("/logout")

# ---- 4. duplicate webhook events ---------------------------------------------
r = client.post("/api/signup", json={"plan": "growth", "email": "p6merchant@example.com",
    "phone": "9812300001", "password": "password123", "name": "P6 Merchant"})
mid = r.json().get("merchant_id") or ""
mp = client.post("/admin/mark-paid", json={"merchant_id": mid}, headers={"X-Admin-Token": ADMIN_TOKEN})
record("p6: pilot merchant activated for webhook test", mp.status_code == 200, str(mp.json())[:100])
wpayload = {"event": "payment.failed", "payload": {"payment": {"entity": {
    "id": "pay_p6_dup", "amount": 250000, "contact": "+919812300001",
    "error_reason": "insufficient_funds", "token_id": "tok_p6",
    "notes": {"merchant_id": mid, "name": "Dup Test"}}}}}
r1 = signed_post("/webhooks/razorpay", wpayload)
r2 = signed_post("/webhooks/razorpay", wpayload)
record("p6: duplicate webhook event ingested once",
       r1.json().get("processed") is True and r2.json().get("processed") is False,
       f"r1={r1.json()} r2={r2.json()}")
import copy as _copy
_tampered = _copy.deepcopy(wpayload); _tampered["payload"]["payment"]["entity"]["amount"] = 99000
bt = client.post("/webhooks/razorpay", content=json.dumps(_tampered).encode(),
                 headers={"X-Razorpay-Signature": hmac.new(_webhook_secret.encode(), json.dumps(wpayload).encode(), hashlib.sha256).hexdigest()})
record("p6: tampered webhook body rejected 400", bt.status_code == 400, str(bt.status_code))

# ---- 5. retry safety: re-running the pipeline never duplicates ----------------
n0 = n_outbox(A)
automations.run_for_owner(ownerA, "")
n1 = n_outbox(A)
record("p6: scheduler re-run queues no duplicate messages", n1 == n0, f"before={n0} after={n1}")

# ---- 6. suppression: disputed, cancelled, paused, verified payment -----------
cuD = models.create_customer(A, "Disputed D", email="d@p6.com", phone="+919000000022")
models.create_invoice(A, cuD["id"], "P6-D1", 30000, "plan", date(3))
models.ex("UPDATE invoices SET status='disputed' WHERE number='P6-D1' AND owner_id=?", (A,))
cuC = models.create_customer(A, "Cancelled C", email="c@p6.com", phone="+919000000023")
models.create_invoice(A, cuC["id"], "P6-C1", 20000, "plan", date(3))
models.ex("UPDATE invoices SET status='cancelled' WHERE number='P6-C1' AND owner_id=?", (A,))
automations.run_for_owner(ownerA, "")
record("p6: disputed invoice produces no messages", n_outbox(A, "d@p6.com") == 0, f"n={n_outbox(A, 'd@p6.com')}")
record("p6: cancelled invoice produces no messages", n_outbox(A, "c@p6.com") == 0, f"n={n_outbox(A, 'c@p6.com')}")

cuP = models.create_customer(A, "Paused P", email="p@p6.com", phone="+919000000024")
invP = models.create_invoice(A, cuP["id"], "P6-P1", 40000, "plan", date(2))
automations.run_for_owner(ownerA, "")
nq = n_outbox(A, "p@p6.com")
record("p6: overdue invoice queues messages before pause", nq > 0, f"n={nq}")
sequences.set_invoice_paused(A, invP["id"], True)
automations.run_for_owner(ownerA, "")
record("p6: invoice pause stops further messages", n_outbox(A, "p@p6.com") == nq,
       f"nq={nq} now={n_outbox(A, 'p@p6.com')}")

cuV = models.create_customer(A, "Verified V", email="v@p6.com", phone="+919000000025")
invV = models.create_invoice(A, cuV["id"], "P6-V1", 60000, "plan", date(2))
automations.run_for_owner(ownerA, "")
nv = n_outbox(A, "v@p6.com")
record("p6: reminders queued before verified payment", nv > 0, f"n={nv}")
inv_row = models.one("SELECT * FROM invoices WHERE id=?", (invV["id"],))
models.mark_invoice_paid(inv_row, "upi", reference="P6-VERIFIED-1", note="phase 6 suppression test")
confirmed = models.q("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=? AND status='confirmed'", (invV["id"],))[0]["n"]
record("p6: verified payment recorded as confirmed", confirmed == 1, f"n={confirmed}")
automations.run_for_owner(ownerA, "")
od_after = intelligence.overdue_analysis(A)
record("p6: paid invoice leaves the overdue queue",
       "P6-V1" not in json.dumps(od_after), str(od_after)[:140])
record("p6: verified payment stops all further reminders", n_outbox(A, "v@p6.com") == nv,
       f"nv={nv} now={n_outbox(A, 'v@p6.com')}")

# ---- 7. quiet hours 21:00-08:00 IST -------------------------------------------
ist = ZoneInfo("Asia/Kolkata")
night = datetime.datetime(2026, 11, 11, 22, 30, tzinfo=ist).timestamp()
day = datetime.datetime(2026, 11, 11, 10, 30, tzinfo=ist).timestamp()
cuQ = models.create_customer(A, "Quiet Q", email="q@p6.com", phone="+919000000026")
r_night = permissions.resolve(A, "send_routine_email_reminder", channel="email", customer=cuQ, ts=night)
r_day = permissions.resolve(A, "send_routine_email_reminder", channel="email", customer=cuQ, ts=day)
record("p6: 22:30 IST deferred by quiet hours", r_night.get("decision") == "deferred" and "quiet" in r_night.get("reason", ""), str(r_night)[:140])
record("p6: 10:30 IST not blocked by quiet hours", r_day.get("decision") == "auto", str(r_day)[:140])

# opt-out suppression
models.ex("UPDATE customers SET email_opt_out=1 WHERE id=?", (cuD["id"],))
cuD_now = models.one("SELECT * FROM customers WHERE id=?", (cuD["id"],))
r_opt = permissions.resolve(A, "send_routine_email_reminder", channel="email", customer=cuD_now)
record("p6: opted-out customer is suppressed", r_opt.get("decision") == "denied" and "opted out" in r_opt.get("reason", ""), str(r_opt)[:140])

# ---- 8. imported payments stay out of recovered revenue ----------------------
pilot.import_data(A, "payments", [{"invoice_id": invV["id"], "amount": 60000, "reference": "UNVERIFIED-P6", "email": "v@p6.com"}])
imp = models.q("SELECT COUNT(*) AS n FROM payments WHERE owner_id=? AND status='imported'", (A,))[0]["n"]
record("p6: imported payment stays 'imported'", imp >= 1, f"n={imp}")
mm = pilot.pilot_metrics()
confirmed_total = models.q("SELECT COUNT(*) AS n FROM payments WHERE status='confirmed'")[0]["n"]
record("p6: verified_recovered excludes unverified imports",
       mm["verified_recovered"]["count"] == confirmed_total, str(mm["verified_recovered"]))

# ---- 9. backup before structural change; old-database migration --------------
b = pilot.create_backup("phase 6 pre-migration")
record("p6: backup taken before migration verifies", pilot.verify_backup(b["id"])["verified"], str(b["id"]))
models.ex("ALTER TABLE outbox DROP COLUMN customer_id")
models.ex("ALTER TABLE outbox DROP COLUMN delivery_detail")
marker = f"legacy-{models.uid()[:6]}"
models.ex("INSERT INTO outbox(id,owner_id,channel,dest,tpl,dedupe,subject,body,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
          (models.uid(), A, "email", "legacy@p6.com", "overdue_1", marker, marker, "legacy body", "queued", models.now()))
models.init()
cols2 = [r["name"] for r in models.q("PRAGMA table_info(outbox)")]
record("p6: existing DB migration restores customer_id additively", "customer_id" in cols2, str(cols2))
record("p6: existing DB migration restores delivery columns additively",
       all(c in cols2 for c in ("delivery_status", "provider_ref", "provider_updated")), str(cols2))
pilot._ensure_outbox_cols()
cols3 = [r["name"] for r in models.q("PRAGMA table_info(outbox)")]
record("p6: delivery-status healing layer restores the full ladder",
       all(c in cols3 for c in ("delivery_detail", "delivery_status", "customer_id")), str(cols3))
kept = models.q("SELECT COUNT(*) AS n FROM outbox WHERE subject=?", (marker,))[0]["n"]
record("p6: pre-migration data survives the upgrade", kept == 1, f"n={kept}")
m2 = pilot.pilot_metrics()
record("p6: metrics still work after migration", isinstance(m2, dict) and "verified_recovered" in m2, str(m2)[:120])
b_old = pilot.verify_backup(b["id"])
record("p6: pre-migration backup stays intact after the upgrade",
       b_old["sha256_ok"] and b_old["quick_check"] == "ok", str(b_old)[:160])
b_new = pilot.create_backup("phase 6 post-migration")
record("p6: post-migration backup verifies", pilot.verify_backup(b_new["id"])["verified"], str(b_new["id"]))

print(f"{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
