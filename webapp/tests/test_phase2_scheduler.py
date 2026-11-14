"""Phase 2 tests: configurable sequences, delivery honesty, control centre.

Run:  cd webapp && python3 -m tests.test_phase2_scheduler
Isolated temp DB, no network.
"""
import os, sys, tempfile, time

os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_phase2_", suffix=".sqlite")
os.environ["MANUAL_UPI_ID"] = "testpay@upi"
os.environ["PROVIDER_STATUS_TOKEN"] = "phase2-status-token"  # phase 9: webhook is fail-closed
os.environ.pop("RAZORPAY_KEY_ID", None)
os.environ.pop("RAZORPAY_KEY_SECRET", None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import clock as _rsclock  # deterministic suite clock (12:30 IST default; see tests/clock.py)

from fastapi.testclient import TestClient  # noqa: E402
from app import main as app_main, models, sequences, permissions, automations  # noqa: E402

models.init()
client = TestClient(app_main.app)
results = []

def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL") + " " + name + ((" | " + detail) if detail else ""))

u = models.create_user("p2owner@example.com", "x", "P2 Owner", "P2 Gym", "+919000000000", role="owner")
OID = u["id"]
KEY = models.create_api_key(OID, "test")
permissions.set_config(OID, {"automations_enabled": True})
H = {"X-API-Key": KEY}

# ---- 1. default sequence matches the spec example ----
seq = sequences.get_sequence(OID)
offs = [(s["id"], s["offset"], bool(s.get("escalate"))) for s in seq]
record("default sequence 0/2/5/7", offs == [("day0", 0, False), ("day2", 2, False),
                                            ("day5", 5, False), ("day7", 7, True)], str(offs))

# ---- 2. custom sequences validate and persist ----
custom = [{"id": "d1", "offset": 1, "channels": ["email"]},
          {"id": "d3", "offset": 3, "channels": ["whatsapp"], "escalate": True}]
out = sequences.set_sequence(OID, custom)
record("custom sequence saved", sequences.get_sequence(OID) == out and len(out) == 2)
try:
    sequences.set_sequence(OID, [{"id": "x", "offset": 5}, {"id": "x", "offset": 6}])
    record("duplicate ids rejected", False)
except ValueError:
    record("duplicate ids rejected", True)
# phase 9: validated negative offsets are now allowed (before-due reminders);
# decreasing offsets and sub-floor values are still rejected
ok_neg = False
try:
    out_neg = sequences.set_sequence(OID, [{"id": "y", "offset": -1}])
    ok_neg = bool(out_neg)
except ValueError:
    ok_neg = False
record("negative offset accepted (validated)", ok_neg)
try:
    sequences.set_sequence(OID, [{"id": "y", "offset": -99}])
    record("sub-floor offset rejected", False)
except ValueError:
    record("sub-floor offset rejected", True)
sequences.set_sequence(OID, [])  # restore defaults
record("empty list restores defaults", sequences.get_sequence(OID)[0]["id"] == "day0")

# ---- 3. scheduler fires the due step once, through the permission engine ----
permissions.authorize_channel(OID, "email")
permissions.authorize_channel(OID, "whatsapp")
cu = models.create_customer(OID, "Asha", email="asha@example.com", phone="+919000000002")
today = time.strftime("%Y-%m-%d")
inv = models.create_invoice(OID, cu["id"], "INV-1", 150000, "membership", today)  # due today
counts = automations.run_for_owner(models.get_user(OID), "https://test.local")
e = models.one("SELECT * FROM outbox WHERE dedupe=?", (f"seq:day0:{inv['id']}:email",))
w = models.one("SELECT * FROM outbox WHERE dedupe=?", (f"seq:day0:{inv['id']}:wa",))
record("day0 fired for due invoice", bool(e and w),
       f"counts={counts}")
record("no provider = no fake send", counts.get("no_channel", 0) >= 1 and
       (e["status"] == "no_channel" if e else False), "sent means provider accepted, not delivered")
counts2 = automations.run_for_owner(models.get_user(OID), "https://test.local")
n_day0 = models.one("SELECT COUNT(*) AS n FROM outbox WHERE dedupe LIKE ?", (f"seq:day0:{inv['id']}%",))
record("step fires once per invoice", int(n_day0["n"]) == 2, f"rows={n_day0['n']}")

# ---- 4. later steps wait for their day ----
d2 = models.one("SELECT COUNT(*) AS n FROM outbox WHERE dedupe LIKE ?", (f"seq:day2:{inv['id']}%",))
record("day2 waits until day 2", int(d2["n"]) == 0)

# ---- 5. a 6-day-overdue invoice gets day0+day2+day5, not day7 ----
# (tests compress several days into one run, so lift the per-customer daily cap)
permissions.set_config(OID, {"automations_enabled": True, "max_per_customer_per_day": 20, "max_per_customer_week": 100, "max_per_channel_per_day": {"email": 100, "whatsapp": 100}})
import datetime
d6 = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
inv6 = models.create_invoice(OID, cu["id"], "INV-6", 250000, "plan", d6)
automations.run_for_owner(models.get_user(OID), "https://test.local")
for sid, want in (("day0", True), ("day2", True), ("day5", True), ("day7", False)):
    n = models.one("SELECT COUNT(*) AS n FROM outbox WHERE dedupe LIKE ?",
                   (f"seq:{sid}:{inv6['id']}%",))
    record(f"6-day overdue: {sid} {'fires' if want else 'waits'}",
           int(n["n"]) == (2 if want else 0), f"rows={n['n']}")

# ---- 6. escalation step holds for owner approval, never sends itself ----
d8 = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
inv8 = models.create_invoice(OID, cu["id"], "INV-8", 99000, "plan", d8)
automations.run_for_owner(models.get_user(OID), "https://test.local")
rows = models.q("SELECT status, dedupe FROM outbox WHERE dedupe LIKE ?",
                (f"seq:day7:{inv8['id']}%",))
record("day7 escalates to approval", rows and all(r["status"] == "approval" for r in rows),
       str([r["status"] for r in rows]))

# ---- 7. owner-paused and cancelled invoices are skipped ----
sequences.set_invoice_paused(OID, inv6["id"], True)
before = models.one("SELECT COUNT(*) AS n FROM outbox").get("n")
automations.run_for_owner(models.get_user(OID), "https://test.local")
after = models.one("SELECT COUNT(*) AS n FROM outbox").get("n")
record("paused case skipped", before == after, f"{before} -> {after}")
models.ex("UPDATE invoices SET status='cancelled' WHERE id=?", (inv8["id"],))
b = models.one("SELECT COUNT(*) AS n FROM outbox").get("n")
automations.run_for_owner(models.get_user(OID), "https://test.local")
a = models.one("SELECT COUNT(*) AS n FROM outbox").get("n")
record("cancelled case skipped", b == a)

# ---- 8. provider status webhook: API accept never counts as delivered ----
ref = "wamid.TESTREF123"
models.ex("UPDATE outbox SET provider_ref=?, delivery_status='accepted' WHERE id=?",
          (ref, e["id"]))
STH = {"X-Provider-Token": "phase2-status-token"}
r = client.post("/webhooks/provider-status", json={"provider_ref": ref, "status": "delivered"}, headers=STH)
row = models.get_outbox(e["id"])
record("provider confirms delivered", r.status_code == 200 and row["delivery_status"] == "delivered")
r = client.post("/webhooks/provider-status", json={"provider_ref": "nope", "status": "delivered"}, headers=STH)
record("unknown ref rejected", r.status_code == 404)
r = client.post("/webhooks/provider-status", json={"provider_ref": ref, "status": "successful"}, headers=STH)
record("bogus status rejected", r.status_code == 400)
r = client.post("/webhooks/provider-status", json={"provider_ref": ref, "status": "delivered"})
record("missing token rejected", r.status_code == 401)

# ---- 9. control centre: config, pause, approvals, audit ----
r = client.get("/api/automations/control", headers=H)
ok = r.status_code == 200 and "permissions" in r.json() and "sequence" in r.json()
record("control centre reads", ok, r.text[:120])
r = client.get("/api/automations/control", headers={"X-API-Key": "rsx_bogus"})
record("bad key rejected", r.status_code == 401)
r = client.put("/api/automations/config", headers=H,
               json={"permissions": {"max_per_customer_per_day": 3},
                     "sequence": [{"id": "only", "offset": 0, "channels": ["email"]}]})
record("config updates", r.status_code == 200 and
       permissions.get_config(OID)["max_per_customer_per_day"] == 3 and
       len(sequences.get_sequence(OID)) == 1)
models.save_connect(OID, "P2 Gym")
r = client.post("/api/automations/pause", headers=H, json={"paused": True})
cn = models.get_connect(OID) or {}
record("pause stops automations", r.status_code == 200 and
       cn.get("paused") == 1 and permissions.get_config(OID)["emergency_paused"])
r = client.post("/api/automations/pause", headers=H, json={"paused": False})
r = client.get("/api/automations/approvals", headers=H)
n_app = len(r.json().get("approvals", []))
record("approvals listed", r.status_code == 200 and n_app >= 2, f"n={n_app}")
if n_app:
    oid_ = r.json()["approvals"][0]["id"]
    r = client.post(f"/api/automations/approvals/{oid_}", headers=H, json={"decision": "approve"})
    row = models.get_outbox(oid_)
    record("owner approval delivers attempt", r.status_code == 200 and row["status"] in ("sent", "no_channel"),
           f"status={row['status'] if row else '?'}")
# tenant isolation: another owner cannot touch this owner's pending item
u2 = models.create_user("p2other@example.com", "x", "Other", "Other Co", "+919000000009", role="owner")
key2 = models.create_api_key(u2["id"], "other")
pend = models.q("SELECT id FROM outbox WHERE owner_id=? AND status='approval' LIMIT 1", (OID,))
if pend:
    r = client.post(f"/api/automations/approvals/{pend[0]['id']}", headers={"X-API-Key": key2},
                    json={"decision": "approve"})
    record("cross-tenant approval blocked", r.status_code == 404)
else:
    record("cross-tenant approval blocked", True, "no pending item left")
r = client.get("/api/automations/audit", headers=H)
aud = r.json().get("audit", [])
record("audit history served", r.status_code == 200 and len(aud) > 5, f"n={len(aud)}")

failed = [x for x in results if not x[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
