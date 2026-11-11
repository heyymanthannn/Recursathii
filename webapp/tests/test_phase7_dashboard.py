"""Phase 7 private SaaS dashboard tests.

Covers the Phase 7 directive: separate private dashboard, secure auth
(signup, login/logout, password recovery), login -> /dashboard redirect,
logged-in -> /dashboard, private -> login redirects, business onboarding,
owner/staff backend-enforced permissions, all 17 dashboard modules,
follow-up sequences module, help & support module, customer-contacting
automations OFF for new businesses, three isolated synthetic businesses
and strict cross-tenant isolation.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase7_dashboard
Isolated temp DB, synthetic data only, no network, no real providers,
production untouched.
"""
import os, sys, tempfile, time, json
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p7_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from fastapi.testclient import TestClient
from app import main as app_main, models, sequences
from app.auth import hash_pw
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from seed_phase7 import seed  # noqa: E402

models.init()
sequences.ensure_schema()
client = TestClient(app_main.app)
results = []

def record(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, ("| " + str(detail)) if not ok else "")

def login(email, pw):
    return client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)

def login_as(email, pw):
    r = login(email, pw)
    assert r.status_code in (303, 200), f"login {email} -> {r.status_code}"
    return r

PW = "dash-test-pass-1"

# ---- 1. fresh install + additive schema ----
record("sequences table created additively (fresh install)",
       models.one("SELECT name FROM sqlite_master WHERE name='sequences'") is not None)
sequences.ensure_schema()  # second call must be a no-op
record("sequences ensure_schema idempotent", True)

# ---- 2. signup: automations OFF by default ----
r = client.post("/signup", data={"name": "Test Owner", "business": "Test Biz",
                                 "email": "owner@test.test", "phone": "9000000000",
                                 "upi_vpa": "test@upi", "password": PW},
                follow_redirects=False)
record("signup accepted -> login page", r.status_code == 303 and "/login" in r.headers.get("location", ""),
       r.status_code)
u = models.get_user_by_email("owner@test.test")
autos = models.get_automations(u["id"])
record("new business automations all OFF", all(v is False for v in autos.values()), autos)

# ---- 3. login -> onboarding, then dashboard ----
r = login("owner@test.test", PW)
record("first login -> onboarding (not onboarded yet)",
       r.status_code == 303 and "/onboarding" in r.headers.get("location", ""), r.status_code)
r = client.post("/onboarding/finish", data={"mode": "auto", "skip": "1"}, follow_redirects=False)
r2 = login("owner@test.test", PW)
record("after onboarding, login -> /dashboard",
       r2.status_code == 303 and r2.headers.get("location") == "/dashboard",
       (r.status_code, r.headers.get("location")))
record("wrong password rejected", login("owner@test.test", "wrongpass").status_code == 200)

# ---- 4. logged-in /login -> dashboard; anonymous -> login ----
r = client.get("/login", follow_redirects=False)
record("logged-in visiting /login -> /dashboard",
       r.status_code == 303 and r.headers.get("location") == "/dashboard", r.status_code)
client.cookies.clear()
for path in ("/dashboard", "/customers", "/invoices", "/sequences", "/help", "/team"):
    r = client.get(path, follow_redirects=False)
    if not (r.status_code == 303 and "/login" in r.headers.get("location", "")):
        record(f"anonymous {path} -> login", False, r.status_code)
        break
else:
    record("anonymous private pages all -> /login", True)

# ---- 5. password recovery ----
login_as("owner@test.test", PW)
client.cookies.clear()
r = client.post("/forgot", data={"email": "owner@test.test"}, follow_redirects=False)
tok_row = models.one("SELECT id FROM users WHERE reset_token IS NOT NULL AND email=?",
                     ("owner@test.test",))
tok = tok_row["id"] and models.q("SELECT reset_token FROM users WHERE email=?",
                                 ("owner@test.test",))[0]["reset_token"]
record("forgot generates reset token", bool(tok), r.status_code)
r = client.get(f"/reset/{tok}", follow_redirects=False)
record("reset page renders for valid token", r.status_code == 200, r.status_code)
r = client.post(f"/reset/{tok}", data={"password": "new-pass-99"}, follow_redirects=False)
record("reset accepts new password", r.status_code in (200, 303), r.status_code)
record("old password now rejected", login("owner@test.test", PW).status_code == 200)
record("new password works", login("owner@test.test", "new-pass-99").status_code == 303)
login_as("owner@test.test", "new-pass-99")

# ---- 6. all 17 dashboard modules render for the owner ----
MODULES = {
    "Overview": "/dashboard", "Customers": "/customers", "Invoices": "/invoices",
    "Payments": "/payments", "Payment Recovery": "/radar",
    "AI Customer Reactivation": "/campaigns", "Revenue Intelligence": "/analytics",
    "Automation Centre": "/automations", "Follow-up Sequences": "/sequences",
    "Message Centre": "/notifications", "Approval Centre": "/approvals",
    "Reports & Analytics": "/reports", "Integrations": "/integrations",
    "Team & Staff": "/team", "Subscription & Billing": "/billing",
    "Business Settings": "/settings", "Help & Support": "/help",
}
fails = [n for n, p in MODULES.items() if client.get(p).status_code != 200]
record("all 17 module pages render (owner)", not fails, fails)

# ---- 7. owner/staff backend-enforced permissions ----
models.create_user("staff1@test.test", hash_pw("staff-pass-1"), "S1",
                   u["business"], "", role="staff", parent_id=u["id"])
client.cookies.clear()
login_as("staff1@test.test", "staff-pass-1")
r = client.get("/team", follow_redirects=False)
record("staff blocked from Team (403)", r.status_code == 403, r.status_code)
r = client.get("/billing", follow_redirects=False)
record("staff blocked from Billing (403)", r.status_code == 403, r.status_code)
r = client.get("/settings", follow_redirects=False)
record("staff blocked from Business Settings (403)", r.status_code == 403, r.status_code)
r = client.get("/sequences", follow_redirects=False)
record("staff can view Follow-up Sequences", r.status_code == 200, r.status_code)
r = client.post("/sequences/save", data={"n": "1", "sid_0": "s1", "off_0": "0",
                                         "ch_0_email": "on"}, follow_redirects=False)
loc = r.headers.get("location", "")
record("staff cannot edit sequences (owner only)",
       r.status_code == 303 and "owner" in loc, (r.status_code, loc))
client.cookies.clear()

# ---- 8. sequences save + validation (owner) ----
login_as("owner@test.test", "new-pass-99")
r = client.post("/sequences/save",
                data={"n": "2", "sid_0": "first", "off_0": "0", "ch_0_email": "on",
                      "sid_1": "second", "off_1": "7", "ch_1_email": "on",
                      "ch_1_whatsapp": "on", "appr_1": "on", "ma_1": "3"},
                follow_redirects=False)
loc = r.headers.get("location", "")
record("owner saves a 2-step sequence", r.status_code == 303 and "Sequence+saved" in loc,
       (r.status_code, loc))
steps = sequences.get_sequence(u["id"])
ok = len(steps) == 2 and steps[0]["id"] == "first" and steps[1].get("approval_required")
record("saved sequence persists and round-trips", ok, steps)
r = client.post("/sequences/save",
                data={"n": "2", "sid_0": "a", "off_0": "9", "ch_0_email": "on",
                      "sid_1": "b", "off_1": "3", "ch_1_email": "on"},
                follow_redirects=False)
record("invalid sequence (decreasing offsets) rejected",
       r.status_code == 200 and "Could not save" in r.text, r.status_code)

# ---- 9. help & support ----
r = client.get("/help")
record("help page shows channel status",
       r.status_code == 200 and "Channel status" in r.text, r.status_code)

# ---- 10. three isolated synthetic businesses + cross-tenant isolation ----
client.cookies.clear()
biz = seed()
record("seeder creates three synthetic businesses",
       len(biz) == 3 and len({b["owner_id"] for b in biz}) == 3, biz)
counts = {b["owner_id"]: models.q("SELECT COUNT(*) AS n FROM customers WHERE owner_id=?",
                                  (b["owner_id"],))[0]["n"] for b in biz}
record("each synthetic business has its own 5 customers",
       all(v == 5 for v in counts.values()), counts)
a, b = biz[0], biz[1]
cu_a = models.list_customers(a["owner_id"])[0]["id"]
cu_b = models.list_customers(b["owner_id"])[0]["id"]
inv_b = models.list_invoices(b["owner_id"])[0]
client.post("/login", data={"email": a["email"], "password": "synthetic-only-do-not-login"},
            follow_redirects=False)
r = client.get(f"/customers/{cu_b}")
record("owner A cannot open owner B's customer page",
       "Customer not found" in r.text, r.status_code)
r = client.get(f"/invoices/{inv_b['id']}/pdf", follow_redirects=False)
record("owner A cannot export owner B's invoice", r.status_code in (303, 404), r.status_code)
rows = models.q("SELECT * FROM customers WHERE owner_id=? AND id=?", (a["owner_id"], cu_b))
record("customer rows are tenant-scoped at the data layer", rows == [])
r = client.get("/customers")
record("owner A sees only own customers",
       cu_b[:8] not in r.text and cu_a[:8] in r.text, r.status_code)
client.cookies.clear()

print()
print(f"phase 7 dashboard: {sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
