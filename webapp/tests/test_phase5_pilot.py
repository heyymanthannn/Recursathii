"""Phase 5 pilot-readiness tests: activation gate, consent records, validated
imports with reconciliation, monitoring metrics, backup check, and the
three-isolated-workspaces acceptance demo."""
import os, tempfile, time
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_pilot_", suffix=".sqlite")
from fastapi.testclient import TestClient
from app import main as app_main, models, pilot, permissions, intelligence

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

now = time.time()
def date(d): return time.strftime("%Y-%m-%d", time.gmtime(now - d * 86400))

OID = models.create_user("pilot@example.com", "x", "Pilot Owner", "Pilot Co", "+919000000400", role="owner")["id"]
key = models.create_api_key(OID, "pilot")
H = {"X-API-Key": key}

# ---- activation gate ----
a = client.get("/api/pilot/activation", headers=H).json()
record("pilot: automations off by default", a["automations_enabled"] is False, str(a))
cu = models.create_customer(OID, "Ravi", email="ravi@pilot.com")
inv = models.create_invoice(OID, cu["id"], "P-1", 90000, "plan", date(5))
owner = models.get_user(OID)
counts = __import__("app.automations", fromlist=["automations"]).run_for_owner(owner, "")
n1 = models.one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (OID,))["n"]
record("pilot: scheduler queues nothing while inactive", n1 == 0, f"n={n1}")
try:
    pilot.activate(OID, "yes please")
    record("pilot: wrong confirmation word rejected", False)
except ValueError:
    record("pilot: wrong confirmation word rejected", True)
bad = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "go"})
record("pilot: API activation needs ACTIVATE", bad.status_code == 400, str(bad.status_code))
ok = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "ACTIVATE"}).json()
record("pilot: ACTIVATE turns automations on", ok["activation"]["automations_enabled"] is True, str(ok)[:100])
permissions.set_config(OID, {"quiet_hours": {"enabled": False},
                              "max_per_customer_per_day": 20, "max_per_customer_week": 100})
counts = __import__("app.automations", fromlist=["automations"]).run_for_owner(owner, "")
n2 = models.one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=?", (OID,))["n"]
record("pilot: scheduler works once activated", n2 > 0, f"n={n2}")

# ---- consent and authorization records ----
c = client.post("/api/pilot/consents", headers=H,
                json={"subject": "Ravi", "kind": "customer_messaging_consent",
                      "channel": "email", "granted_by": "owner", "evidence": "signup checkbox 2026-11-11"})
record("pilot: consent recorded", c.status_code == 200 and c.json()["consent"]["kind"] == "customer_messaging_consent", str(c.json())[:120])
lst = client.get("/api/pilot/consents", headers=H).json()
record("pilot: consents listed", len(lst["consents"]) == 1, str(lst)[:100])
badk = client.post("/api/pilot/consents", headers=H, json={"subject": "x", "kind": "nope"})
record("pilot: unknown consent kind rejected", badk.status_code == 400)

# ---- validated imports with dedupe and reconciliation ----
r1 = client.post("/api/pilot/import", headers=H, json={"kind": "customers", "rows": [
    {"name": "Meena", "email": "meena@x.com", "phone": "+919000000501"},
    {"name": "Meena K", "email": "meena@x.com"},
    {"name": "", "email": "noname@x.com"}]}).json()["result"]
record("pilot: customer import dedupes and validates", r1["created"] == 1 and r1["updated"] == 1 and r1["skipped"] == 1, str(r1))
meena = models.one("SELECT id FROM customers WHERE email='meena@x.com'")
r2 = client.post("/api/pilot/import", headers=H, json={"kind": "invoices", "rows": [
    {"customer_id": meena["id"], "number": "IMP-1", "amount": 50000, "due_date": date(10)},
    {"customer_id": meena["id"], "number": "IMP-1", "amount": 50000},
    {"customer_id": "ghost", "number": "IMP-2", "amount": 100},
    {"customer_id": meena["id"], "number": "IMP-3", "amount": -5}]}).json()["result"]
record("pilot: invoice import dedupes by number and validates", r2["created"] == 1 and r2["skipped"] == 3, str(r2))
inv_i = models.one("SELECT * FROM invoices WHERE number='IMP-1'")
r3 = client.post("/api/pilot/import", headers=H, json={"kind": "payments", "rows": [
    {"invoice_id": inv_i["id"], "amount": 50000, "method": "upi"}]}).json()["result"]
record("pilot: payment imports as imported, not confirmed", r3["created"] == 1, str(r3))
pay = models.one("SELECT * FROM payments WHERE invoice_id=?", (inv_i["id"],))
record("pilot: imported payment status is 'imported'", pay["status"] == "imported", pay["status"])
rate = intelligence.verified_recovery_rate(OID)
record("pilot: imported payment NOT counted as recovered revenue", rate["verified_recovered"] == 0, str(rate)[:120])
rj = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pay["id"], "verified": False}).json()
record("pilot: reconcile without reference rejected", rj["changed"] is False, str(rj)[:100])
rr = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pay["id"], "verified": True, "reference": "UTR-REC-1"}).json()
record("pilot: reconcile with reference confirms", rr["changed"] is True and rr["payment"]["status"] == "confirmed", str(rr)[:120])
rate2 = intelligence.verified_recovery_rate(OID)
record("pilot: reconciled payment counts once recovered", rate2["verified_recovered"] == 1 and rate2["verified_recovered_value"] == 50000, str(rate2)[:140])

# ---- three isolated pilot workspaces ----
ids = []
for i in range(3):
    oid = models.create_user(f"p{i}@iso.com", "x", f"Owner {i}", f"Co {i}", f"+9190000006{i}0", role="owner")["id"]
    k = models.create_api_key(oid, f"iso{i}")
    c_ = models.create_customer(oid, f"Customer {i}", email=f"c{i}@iso.com")
    iv = models.create_invoice(oid, c_["id"], f"ISO-{i}", 100000 + i, "plan", date(3))
    ids.append((oid, k))
iso_checks = []
for oid, k in ids:
    snap = client.get("/api/intelligence/overview", headers={"X-API-Key": k}).json()
    own = snap["overdue"]["total_overdue_amount"]
    iso_checks.append(own == 100000 + ids.index((oid, k)))
record("pilot: three workspaces each see only their own totals", all(iso_checks), str(iso_checks))
pilot_snap = client.get("/api/intelligence/overview", headers=H).json()
record("pilot: isolation holds against the original workspace too",
       pilot_snap["overdue"]["total_overdue_amount"] == 90000, str(pilot_snap["overdue"])[:120])
leak = client.get("/api/pilot/consents", headers={"X-API-Key": ids[0][1]}).json()
record("pilot: workspace 1 sees none of pilot workspace's consents", len(leak["consents"]) == 0)

# ---- metrics + backup ----
m = client.get("/pilot/metrics", headers={"x-admin-token": "changeme-admin"}).json()
record("pilot: metrics served with admin token", m["businesses_onboarded"] >= 4, str(m)[:140])
m401 = client.get("/pilot/metrics", headers={})
record("pilot: metrics blocked without admin token", m401.status_code == 401)
b = client.get("/pilot/backup-check", headers={"x-admin-token": "changeme-admin"}).json()
record("pilot: backup check runs quick_check", b.get("quick_check") == "ok", str(b)[:120])

print(f"\n{sum(results)}/{len(results)} passed")
