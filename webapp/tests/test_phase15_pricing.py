"""Phase 15: official pricing integrity test (dev, synthetic).

Official owner-approved prices (Nov 15, 2026): Starter Rs 999/mo,
Growth Rs 4,999/mo, Enterprise Rs 9,999/mo. Verifies the backend config,
the billing checkout, stale-price cleanup on public pages, the boot-time
drift guard, and that merchant invoice payment behavior and existing
subscriptions are untouched. Annual pricing intentionally unchanged.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase15_pricing
"""
import os, sys, tempfile
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p15_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ.pop("ALLOW_LIVE_PROVIDERS", None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, config
from app.webui import inr

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:180].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("| " + d) if not ok else "")

# ---- 1. official prices in backend config ----
record("config: Starter is Rs 999/mo", config.PLANS["starter"]["amount"] == 99900, config.PLANS["starter"]["amount"])
record("config: Growth is Rs 4,999/mo", config.PLANS["growth"]["amount"] == 499900, config.PLANS["growth"]["amount"])
record("config: Enterprise is Rs 9,999/mo", config.PLANS["enterprise"]["amount"] == 999900, config.PLANS["enterprise"]["amount"])
record("config: inr() renders Rs 9,999 for Enterprise", inr(999900) == "₹9,999", inr(999900))
record("config: inr() renders Rs 999 / Rs 4,999 for Starter/Growth",
       inr(99900) == "₹999" and inr(499900) == "₹4,999", (inr(99900), inr(499900)))

# ---- 2. annual pricing untouched (owner decides separately) ----
record("config: annual amounts official (9,990 / 49,990 / 99,990)",
       (config.PLANS["starter"]["annual_amount"], config.PLANS["growth"]["annual_amount"],
        config.PLANS["enterprise"]["annual_amount"]) == (999000, 4999000, 9999000),
       tuple(config.PLANS[k]["annual_amount"] for k in ("starter", "growth", "enterprise")))

# ---- 3. backend rejects outdated or inconsistent plan pricing ----
try:
    config.validate_pricing()
    record("guard: validate_pricing passes on official config", True)
except Exception as e:
    record("guard: validate_pricing passes on official config", False, e)
tampered = {k: dict(v) for k, v in config.PLANS.items()}
tampered["enterprise"]["amount"] = 599900
try:
    config.validate_pricing(tampered)
    record("guard: outdated Enterprise price (5,999) rejected", False, "no error raised")
except ValueError as e:
    record("guard: outdated Enterprise price (5,999) rejected", "999900" in str(e), e)
except Exception as e:
    record("guard: outdated Enterprise price (5,999) rejected", False, e)
tampered2 = {k: dict(v) for k, v in config.PLANS.items()}
tampered2["growth"]["amount"] = 249900
try:
    config.validate_pricing(tampered2)
    record("guard: inconsistent Growth price (2,499) rejected", False, "no error raised")
except ValueError:
    record("guard: inconsistent Growth price (2,499) rejected", True)

# ---- 4. signup helper ----
def new_owner(email, plan="starter"):
    client.post("/signup", data={"name": "P " + email, "business": "P15 Biz", "email": email,
                                 "phone": "919810000201", "upi_vpa": "p15@upi", "password": "pw12345678"},
                follow_redirects=False)
    u = models.get_user_by_email(email)
    client.post("/login", data={"email": email, "password": "pw12345678"}, follow_redirects=False)
    return u

# ---- 5. checkout uses the correct Enterprise amount ----
u1 = new_owner("p15a@test.local")
models.update_user(u1["id"], plan_status="active")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "monthly"}, follow_redirects=False)
pays = models.list_plat_pays(u1["id"])
record("checkout: Enterprise monthly checkout records Rs 9,999 pending",
       r.status_code == 303 and pays and pays[0]["amount"] == 999900 and pays[0]["plan"] == "enterprise"
       and pays[0]["status"] == "pending", pays[0] if pays else r.status_code)
u1b = models.get_user_by_email("p15a@test.local")
record("checkout: user moved to pending_payment, cycle monthly",
       u1b["plan"] == "enterprise" and u1b["plan_status"] == "pending_payment", (u1b["plan"], u1b["plan_status"]))

u2 = new_owner("p15b@test.local")
models.update_user(u2["id"], plan_status="active")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual"}, follow_redirects=False)
pays2 = models.list_plat_pays(u2["id"])
record("checkout: Enterprise annual checkout uses official Rs 99,990",
       pays2 and pays2[0]["amount"] == 9999000, pays2[0]["amount"] if pays2 else r.status_code)

u3 = new_owner("p15c@test.local")
models.update_user(u3["id"], plan_status="active")
r = client.post("/billing/change-plan", data={"plan": "scale", "cycle": "monthly"}, follow_redirects=False)
pays3 = models.list_plat_pays(u3["id"])
record("checkout: legacy 'scale' alias maps to Enterprise at Rs 9,999",
       pays3 and pays3[0]["plan"] == "enterprise" and pays3[0]["amount"] == 999900,
       pays3[0] if pays3 else r.status_code)
before = len(models.list_plat_pays(u3["id"]))
client.post("/billing/change-plan", data={"plan": "notaplan", "cycle": "monthly"}, follow_redirects=False)
record("checkout: unknown plan rejected, no pay record created",
       len(models.list_plat_pays(u3["id"])) == before, len(models.list_plat_pays(u3["id"])))

# ---- 6. billing page shows official prices, no stale Enterprise price ----
r = client.get("/billing")
record("billing: page shows Rs 9,999 for Enterprise",
       r.status_code == 200 and "₹9,999" in r.text, r.status_code)
record("billing: no stale Rs 5,999/6,999/7,999 anywhere on billing page",
       not any(s in r.text for s in ("5,999", "6,999", "7,999")),
       [s for s in ("5,999", "6,999", "7,999") if s in r.text])
record("billing: annual line shows Rs 99,990/yr for Enterprise", "99,990" in r.text, "99,990 missing")

for path in ("/", "/pricing", "/terms", "/privacy"):
    txt = client.get(path).text
    import re as _re
    stale = [s for s in ("5,999", "6,999", "7,999", "2,499")
             if s in txt or _re.search(r"(?<!\\d)" + s.replace(",", "") + r"(?!\\d)", txt)]
    record(f"public: {path} clean of stale plan prices", not stale, stale)

# ---- 7. existing subscriptions not migrated or charged ----
u4 = models.create_user("p15d@test.local", "x", "Legacy Owner", "Legacy Biz", "919810000202",
                        role="owner", plan="scale")
models.update_user(u4["id"], plan_status="active")
row = models.get_user_by_email("p15d@test.local")
record("subs: legacy plan='scale' stored value unchanged (no migration)",
       row["plan"] == "scale", row["plan"])
record("subs: legacy alias still resolves to Enterprise", config.plan_key("scale") == "enterprise",
       config.plan_key("scale"))
record("subs: no pay records created for existing subscription", models.list_plat_pays(u4["id"]) == [],
       models.list_plat_pays(u4["id"]))

# ---- 8. merchant invoice payment behavior unchanged ----
c1 = models.create_customer(u4["id"], "Merch Customer", email="mc@test.local", phone="919810000203")
inv = models.create_invoice(u4["id"], c1["id"], "P15-INV-1", 250000, "monthly", "2026-12-01")
row = models.q("SELECT * FROM invoices WHERE id=?", (inv["id"],))
record("invoices: merchant invoice amount stored as passed (Rs 2,500), unaffected by plan config",
       row and row[0]["amount"] == 250000, row[0]["amount"] if row else None)
src = open("app/models.py", encoding="utf-8").read()
fn = src[src.index("def create_invoice"):src.index("def ", src.index("def create_invoice") + 10)]
record("invoices: create_invoice does not read plan pricing", "PLANS" not in fn and "OFFICIAL" not in fn)

# ---- summary ----
passed, total = sum(results), len(results)
print(f"PHASE 15 PRICING: {passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
