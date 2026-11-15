"""Checkout plan-page fix: route shadowing + GET record writes.

Problem: @app.get("/checkout/{merchant_id}") was registered before
@app.get("/checkout/{plan}"), so plan URLs like /checkout/enterprise matched
the merchant route and 404'd ("checkout page not found"). The shadowed plan
route also created merchant records on GET (billing.onboard with a synthetic
email, merchant_id defaulting to "demo").

Fix: one checkout route. A path segment that names a valid plan serves a
read-only plan page (no record writes); anything else falls through to the
unchanged merchant checkout. Onboarding URLs /checkout/{plan} and
/checkout/{plan}?merchant_id=... keep working; ?merchant_id= links to that
merchant's checkout instead of creating data.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_checkout_plan_page
Isolated temp DB, synthetic data only, no network, no real providers,
production untouched.
"""
import os, sys, tempfile
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_cpk_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from fastapi.testclient import TestClient
from app import main as app_main, models
from app import db as app_db

models.init()
client = TestClient(app_main.app)
results, fails = [], []

def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, (("| " + str(detail)) if not ok else ""))
    if not ok:
        fails.append(name)

def merchants():
    return app_db.load_merchants()

def users_count():
    return len(models.q("SELECT id FROM users", ()))

before_m, before_u = merchants(), users_count()

# 1. Plan slugs serve a read-only page, and create nothing
r = client.get("/checkout/enterprise")
check("GET /checkout/enterprise -> 200", r.status_code == 200, r.status_code)
check("enterprise page names the plan", "Enterprise plan" in r.text)
check("enterprise page shows the monthly price", "9,999" in r.text)
check("enterprise page has a trial CTA", "/signup" in r.text)
check("enterprise GET created no merchant row", merchants() == before_m,
      list(merchants().keys()))
check("enterprise GET created no user row", users_count() == before_u)

r = client.get("/checkout/starter")
check("GET /checkout/starter -> 200", r.status_code == 200, r.status_code)
check("starter page names the plan", "Starter plan" in r.text)
check("starter page shows the monthly price", "999" in r.text)

r = client.get("/checkout/scale")
check("GET /checkout/scale -> 200 (alias to Enterprise)",
      r.status_code == 200 and "Enterprise plan" in r.text, r.status_code)

r = client.get("/checkout/notaplan")
check("GET /checkout/notaplan -> 404", r.status_code == 404, r.status_code)
check("unknown slug created no rows",
      merchants() == before_m and users_count() == before_u)

# 2. The merchant checkout is unchanged for a real merchant
r = client.post("/onboard", json={"merchant_id": "test-m1", "email": "t1@example.test",
                                  "plan": "growth"})
check("POST /onboard creates the test merchant", r.status_code == 200, r.text[:120])
r = client.get("/checkout/test-m1")
check("GET /checkout/test-m1 -> merchant checkout page",
      r.status_code == 200 and "Checkout · RecurSaathi" in r.text, r.status_code)
check("merchant page keeps the paid-claim form", "/checkout/test-m1/paid-claim" in r.text)

# 3. ?merchant_id= links onward instead of writing
r = client.get("/checkout/growth?merchant_id=test-m1")
check("plan page with known merchant -> 200", r.status_code == 200, r.status_code)
check("plan page links to the merchant checkout",
      "Continue to your checkout" in r.text and "/checkout/test-m1" in r.text)
r = client.get("/checkout/growth?merchant_id=nosuch")
check("plan page with unknown merchant -> generic CTA",
      r.status_code == 200 and "Start your free trial" in r.text and "/signup" in r.text)
check("merchant_id query wrote nothing",
      set(merchants().keys()) == {"test-m1"}, list(merchants().keys()))

# 4. Regression: no GET anywhere in this suite created a user
check("no user rows created by any GET", users_count() == before_u)

# 5. Route shape: exactly one GET route handles /checkout/{segment}
get_checkout = [rt for rt in app_main.app.routes
                if getattr(rt, "path", "").startswith("/checkout")
                and "GET" in getattr(rt, "methods", set())]
check("exactly one GET /checkout/* route remains", len(get_checkout) == 1,
      [getattr(rt, "path", "?") for rt in get_checkout])
check("the shadowed /checkout/{plan} route is gone",
      all(getattr(rt, "path", "") != "/checkout/{plan}" for rt in app_main.app.routes))

print()
print(f"RESULT: {sum(results)}/{len(results)} passed" + ("" if not fails else f"; FAILURES: {fails}"))
sys.exit(0 if not fails else 1)
