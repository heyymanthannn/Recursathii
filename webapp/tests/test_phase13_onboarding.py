"""Phase 9.13: onboarding journey test (dev, synthetic).

Walks the full signup -> verify -> onboarding -> first customer -> consent ->
billing path on an isolated temp database and proves the safety gates:
every customer-contacting automation starts OFF, live provider credentials
are refused without the platform flag, and plan selection never produces a
live payment link without configured keys.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase13_onboarding
"""
import os, sys, tempfile, time
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p13_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ.pop("ALLOW_LIVE_PROVIDERS", None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, providers, permissions
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:180].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("| " + d) if not ok else "")

# ---- 1. signup validation ----
r = client.post("/signup", data={"name": "A", "business": "B", "email": "", "password": "pw12345678","password2": "pw12345678", "accept": "1","phone": "919810000130"})
record("signup: missing email rejected", "required" in r.text, r.status_code)
r = client.post("/signup", data={"name": "A", "business": "B", "email": "ob1@test.local", "password": "short","password2": "short", "accept": "1","phone": "919810000130"})
record("signup: short password rejected", "at least 8" in r.text, r.status_code)

# ---- 2. signup success ----
r = client.post("/signup", data={"name": "Ona Owner", "business": "Ona Traders", "email": "ob1@test.local",
                                 "phone": "919810000130", "upi_vpa": "ona@upi", "password": "pw12345678", "password2": "pw12345678", "accept": "1"},
                follow_redirects=False)
# Part 5: confirm-password + terms acceptance + remember-me session lengths
_mismatch = client.post("/signup", data={"name": "A", "business": "B", "email": "mm1@test.local",
                    "password": "pw12345678", "password2": "different99", "accept": "1",
                    "phone": "919810000131"}, follow_redirects=False)
record("signup: mismatched confirm password rejected", _mismatch.status_code == 200
       and "Passwords do not match" in _mismatch.text
       and not models.get_user_by_email("mm1@test.local"))
_noaccept = client.post("/signup", data={"name": "A", "business": "B", "email": "na1@test.local",
                    "password": "pw12345678", "password2": "pw12345678", "accept": "",
                    "phone": "919810000131"}, follow_redirects=False)
record("signup: terms acceptance required", _noaccept.status_code == 200
       and "Terms of Service" in _noaccept.text
       and not models.get_user_by_email("na1@test.local"))
_remember = client.post("/login", data={"email": "ob1@test.local", "password": "pw12345678", "remember": "1"},
                        follow_redirects=False)
_plain = client.post("/login", data={"email": "ob1@test.local", "password": "pw12345678"},
                     follow_redirects=False)
def _max_age(r):
    for c in r.headers.get_list("set-cookie"):
        if "rs_session=" in c and "Max-Age=" in c:
            return int(c.split("Max-Age=")[1].split(";")[0])
    return None
record("login: remember me sets the 30-day session", _max_age(_remember) == 30 * 86400, _max_age(_remember))
record("login: plain login sets the 12-hour session", _max_age(_plain) == 43200, _max_age(_plain))
record("signup: lands on login page", r.status_code == 303 and "/login" in r.headers.get("location", ""), r.status_code)
u = models.get_user_by_email("ob1@test.local")
record("signup: user created unverified", bool(u) and not u.get("verified_at") and bool(u.get("verify_token")), dict(u) if u else None)
record("signup: 14-day trial started", u and u.get("trial_ends") and (u["trial_ends"] - (u.get("trial_started_at") or 0)) > 13.9 * 86400,
       u.get("trial_ends") if u else None)
keys = {k: models.get_automations(u["id"]).get(k) for k in models.AUTO_KEYS}
record("signup: every customer-contacting automation OFF", all(v is False for v in keys.values()), keys)

# ---- 3. email verification ----
r = client.get("/verify/bogus-token")
record("verify: bogus token rejected", "invalid" in r.text, r.status_code)
r = client.get(f"/verify/{u['verify_token']}")
u2 = models.get_user_by_email("ob1@test.local")
record("verify: valid token marks verified + single use", u2.get("verified_at") and not u2.get("verify_token")
       and "verified" in r.text, r.status_code)

# ---- 4. login routes to onboarding ----
r = client.post("/login", data={"email": "ob1@test.local", "password": "pw12345678"}, follow_redirects=False)
record("login: new account routes to onboarding", r.status_code == 303 and "/onboarding" in r.headers.get("location", ""), r.headers.get("location"))
r = client.post("/login", data={"email": "ob1@test.local", "password": "wrongpass123"}, follow_redirects=False)
record("login: wrong password refused", r.status_code == 200 and ("invalid" in r.text.lower() or "wrong" in r.text.lower()), r.status_code)

# ---- 5. onboarding steps ----
r = client.post("/onboarding/step1", data={"name": "Ona Owner", "business": "Ona Traders",
                                           "phone": "919810000130", "upi_vpa": "ona@upi"}, follow_redirects=False)
record("onboarding step1: profile saved", r.status_code == 303 and "/onboarding?step=2" in r.headers.get("location", ""),
       r.headers.get("location"))
u3 = models.get_user_by_email("ob1@test.local")
record("onboarding step1: UPI stored on profile", u3.get("upi_vpa") == "ona@upi", u3.get("upi_vpa"))
r = client.post("/onboarding/step2", data={"business_name": "Ona Traders", "smtp_host": "smtp.test.local",
                                           "smtp_port": "587", "smtp_user": "biz@onatrades.in", "smtp_pass": "smtp-secret"},
                follow_redirects=False)
cn = models.get_connect(u3["id"])
record("onboarding step2: email connection saved encrypted", cn and cn.get("smtp_host") == "smtp.test.local"
       and cn.get("smtp_user") == "biz@onatrades.in" and bool(cn.get("smtp_pass_enc")), dict(cn) if cn else None)
r = client.post("/onboarding/finish", data={"mode": "approval", "cname": "Ravi Kumar", "cemail": "ravi@x.test",
                                            "cphone": "919812345678"}, follow_redirects=False)
u4 = models.get_user_by_email("ob1@test.local")
record("onboarding finish: onboarded + dashboard", u4.get("onboarded") == 1 and r.status_code == 303
       and "/dashboard" in r.headers.get("location", ""), r.headers.get("location"))
custs = models.list_customers(u4["id"])
record("onboarding finish: first customer created", len(custs) == 1 and custs[0]["name"] == "Ravi Kumar",
       [dict(c) for c in custs])
record("onboarding finish: automation mode set to chosen consent", models.get_automation_mode(u4["id"]) == "approval",
       models.get_automation_mode(u4["id"]))
r = client.post("/login", data={"email": "ob1@test.local", "password": "pw12345678"}, follow_redirects=False)
record("login: onboarded account goes to dashboard", r.status_code == 303 and "/dashboard" in r.headers.get("location", ""), r.headers.get("location"))

# ---- 6. first invoice + payment instructions ----
r = client.post("/invoices", data={"customer_id": custs[0]["id"], "number": "OB-001", "amount": "2500",
                                   "description": "Monthly service", "due_date": "2026-11-30"}, follow_redirects=False)
inv = models.list_invoices(u4["id"])
record("onboarding: first invoice created", len(inv) == 1 and inv[0]["number"] == "OB-001" and inv[0]["amount"] == 250000,
       [dict(i) for i in inv])

# ---- 7. customer communication consent ----
r = client.post(f"/customers/{custs[0]['id']}/consent", data={"field": "wa", "value": "1"}, follow_redirects=False)
cu2 = models.get_customer(custs[0]["id"])
record("consent: opt-out recorded on customer", cu2.get("wa_opt_out") == 1, dict(cu2))

# ---- 8. billing: plans visible, no live checkout without keys ----
r = client.get("/billing")
has_all = all(s in r.text for s in ("999", "4,999", "9,999"))
record("billing: three approved plans shown (Rs 999 / 4,999 / 9,999)", r.status_code == 200 and has_all, r.status_code)
record("billing: no live subscription link without keys", "rzp_live" not in r.text and "checkout.razorpay.com" not in r.text, r.status_code)

# ---- 9. live messaging gate ----
try:
    providers.configure_provider(u4["id"], "email", "smtp", {"host": "live.example"}, sandbox=False)
    record("live gate: live credentials refused without platform flag", False, "no exception")
except ValueError:
    record("live gate: live credentials refused without platform flag", True)
record("live gate: permission engine still off before owner activates automations",
       not permissions.get_config(u4["id"]).get("automations_enabled"))

# ---- 10. mobile usability ----
base = open("app/templates/base.html").read()
record("mobile: responsive viewport present", 'name="viewport"' in base and "initial-scale=1" in base)
r = client.get("/login")
record("mobile: login page renders", r.status_code == 200)

print(f"PHASE 9.13 ONBOARDING: {sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
