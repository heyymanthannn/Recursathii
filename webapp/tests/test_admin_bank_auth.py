"""Admin-auth fix tests: ADMIN_TOKEN holders reach the bank verification
screens; everyone else stays out.

Covers the owner's five requirements (Nov 15, 2026):
  1. authorized admin (ADMIN_TOKEN cookie from /admin/login) can access
     /admin/bank and its approve/reject routes;
  2. unauthenticated visitors are rejected;
  3. ordinary business users (role owner/staff) are rejected;
  4. invalid or wrong admin credentials are rejected;
  5. payment approval stays audited and idempotent.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_admin_bank_auth
Isolated temp DB, synthetic data only, no network, no real providers.
"""
import os, sys, tempfile
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_admbank_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ["ADMIN_TOKEN"] = "admbank-test-token"
# keep the admin cookie sendable over the plain-http test client
os.environ["BASE_URL"] = "http://test.local"
os.environ.setdefault("BILLING_WEBHOOK_SECRET", "admbank-bh")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, config, manual_bank
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app, follow_redirects=False)
results = []

def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:220].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("" if ok else "| " + d))

# ---- fixture: one ordinary business owner with a bank-transfer submission ----
ow = models.create_user("admbank-owner@test.local", hash_pw("pw12345678"),
                        "Owner", "Biz", "919810000099")
r = client.post("/login", data={"email": "admbank-owner@test.local",
                                "password": "pw12345678"}, follow_redirects=False)
record("owner login ok", r.status_code == 303, r.status_code)
r = client.post("/subscription/pay/bank", data={"plan": "starter", "cycle": "monthly",
                                                "method": "neft", "utr": "UTRTEST001",
                                                "receipt_note": "", "ea": ""})
record("bank submission redirect accepted", r.status_code == 303
       and "submitted=" in r.headers.get("location", ""),
       (r.status_code, r.headers.get("location", "")))
pend = manual_bank.pending_list()
record("submission sits PENDING_VERIFICATION",
       len(pend) == 1 and pend[0]["status"] == "PENDING_VERIFICATION", [p["status"] for p in pend])
pid = pend[0]["id"]
sub_ref = pend[0]["reference"]

# ---- 2. unauthenticated rejected ----
anon = TestClient(app_main.app, follow_redirects=False)
r = anon.get("/admin/bank")
record("unauthenticated GET /admin/bank rejected", r.status_code == 303
       and "/login" in r.headers.get("location", ""), r.headers.get("location", ""))
r = anon.post(f"/admin/bank/{pid}/approve", data={"note": ""})
record("unauthenticated approve rejected", r.status_code == 303
       and "/login" in r.headers.get("location", ""), r.headers.get("location", ""))

# ---- 3. ordinary business user rejected ----
r = client.get("/admin/bank")
record("business owner without cookie rejected", r.status_code == 303
       and "/login" in r.headers.get("location", ""), r.headers.get("location", ""))
r = client.post(f"/admin/bank/{pid}/approve", data={"note": ""})
record("business owner approve rejected", r.status_code == 303
       and "/login" in r.headers.get("location", ""), r.headers.get("location", ""))

# ---- 4. invalid / wrong admin credentials rejected ----
bad = TestClient(app_main.app, follow_redirects=False)
bad.cookies.set("admin_token", "deadbeef" * 8)
r = bad.get("/admin/bank")
record("wrong admin-token cookie rejected", r.status_code == 303
       and "/login" in r.headers.get("location", ""), r.headers.get("location", ""))
r = bad.post("/admin/login", data={"token": "totally-wrong-token"})
record("wrong token at /admin/login rejected", r.status_code == 303
       and "e=1" in r.headers.get("location", ""), r.headers.get("location", ""))
r = bad.get("/admin/bank")
record("no cookie granted after failed login", r.status_code == 303
       and "/login" in r.headers.get("location", ""), r.headers.get("location", ""))

# ---- 1. authorized admin can access /admin/bank ----
adm = TestClient(app_main.app, follow_redirects=False)
r = adm.post("/admin/login", data={"token": "admbank-test-token"})
record("admin token login ok", r.status_code == 303
       and "e=1" not in r.headers.get("location", ""), r.headers.get("location", ""))
r = adm.get("/admin/bank")
record("ADMIN_TOKEN holder reaches /admin/bank", r.status_code == 200
       and sub_ref in r.text, r.status_code)
r = adm.post(f"/admin/bank/{pid}/approve", data={"note": "verified in smoke"})
record("ADMIN_TOKEN holder can approve", r.status_code == 303
       and "approved=" in r.headers.get("location", ""), r.headers.get("location", ""))
row = manual_bank.get(pid)
record("subscription activated exactly once",
       row["status"] == "approved" and row["receipt_no"].startswith("RS-RCP-"), dict(row))
u = models.get_user(ow["id"])
record("owner plan active with period end",
       u["plan"] == "starter" and u["plan_status"] == "active" and u["sub_period_end"], dict(u))

# ---- 5. approval remains audited and idempotent ----
audit = models.q("SELECT * FROM audit WHERE action='bank.approved' ORDER BY created DESC LIMIT 1")
record("approval audited with the admin-token actor",
       bool(audit) and audit[0]["detail"].find(sub_ref) >= 0
       and "admin-token" in audit[0]["detail"], audit[0]["detail"] if audit else "none")
before = manual_bank.get(pid)
r = adm.post(f"/admin/bank/{pid}/approve", data={"note": "second click"})
record("second approval is idempotent (already decided)", r.status_code == 303
       and "already=" in r.headers.get("location", ""), r.headers.get("location", ""))
after = manual_bank.get(pid)
record("no state change on re-approval",
       before["receipt_no"] == after["receipt_no"]
       and before["status"] == after["status"] == "approved", "receipt unchanged")
u2 = models.get_user(ow["id"])
record("plan period unchanged by re-approval", u2["sub_period_end"] == u["sub_period_end"], "period equal")
r = adm.post("/admin/bank/no-such-id/approve", data={"note": ""})
record("approve of unknown id handled without error", r.status_code == 303
       and "err=not_found" in r.headers.get("location", ""),
       (r.status_code, r.headers.get("location", "")))

# ---- narrow scope: other admin.py routes stay session-only ----
r = adm.post("/admin/owners/no-such-id/plan", data={"plan": "growth"})
record("ADMIN_TOKEN cookie does NOT open session-only admin.py routes",
       r.status_code == 303 and "/login" in r.headers.get("location", ""),
       (r.status_code, r.headers.get("location", "")))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if sum(results) == len(results) else 1)
