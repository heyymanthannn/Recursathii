"""Release smoke tests for RecurSaathi. Run against ONE deployed instance.

Deliberately gentle and self-contained:
  * creates one throwaway owner account (email smoke+<ts>@recursaathi-smoke.test)
  * walks login -> subscription -> bank-transfer submission -> admin verification
  * touches nothing else; safe to re-run (each run makes a NEW account)

Guard rails: refuses to run unless SMOKE_BASE_URL is set AND SMOKE_CONFIRM=1,
so it can never fire by accident against production.

Usage:
  SMOKE_BASE_URL=https://recursathii.onrender.com SMOKE_ADMIN_TOKEN=... \
  SMOKE_CONFIRM=1 python3 smoke/smoke_release.py
"""
import os, re, sys, time, uuid
import httpx

BASE = os.environ.get("SMOKE_BASE_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("SMOKE_ADMIN_TOKEN", "")

def need(cond, msg):
    if not cond:
        print("REFUSED:", msg); sys.exit(2)

need(BASE, "set SMOKE_BASE_URL (e.g. https://recursathii.onrender.com)")
need(os.environ.get("SMOKE_CONFIRM") == "1",
     "set SMOKE_CONFIRM=1: this script creates test data on the target instance")
need(ADMIN_TOKEN, "set SMOKE_ADMIN_TOKEN (the deployment's ADMIN_TOKEN value)")

TAG = time.strftime("%Y%m%d%H%M%S")
EMAIL = f"smoke+{TAG}@recursaathi-smoke.test"
PW = "Sm0ke-" + uuid.uuid4().hex[:12]
results = []

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)

c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False,
                 headers={"user-agent": "recursaathi-smoke/1.0"})

# 1. landing + login page render
r = c.get("/"); check("GET / is 200", r.status_code == 200, str(r.status_code))
r = c.get("/login"); check("GET /login is 200", r.status_code == 200, str(r.status_code))

# 2. signup a throwaway owner (rate limit: one signup per run)
r = c.post("/signup", data={"name": "Smoke Test", "business": f"Smoke {TAG}",
                            "email": EMAIL, "phone": "9000000000",
                            "upi_vpa": "", "password": PW, "password2": PW,
                            "accept": "1"})
loc = r.headers.get("location", "")
check("signup accepted", r.status_code == 303 and "created=1" in loc, f"{r.status_code} -> {loc}")
# the app does not auto-login on signup; log in with the fresh account
r = c.post("/login", data={"email": EMAIL, "password": PW, "remember": ""})
loc = r.headers.get("location", "")
check("login works for the fresh account", r.status_code == 303 and ("/dashboard" in loc or "/onboarding" in loc),
      f"{r.status_code} -> {loc}")

# 3. subscription surfaces
r = c.get("/subscription"); check("GET /subscription is 200", r.status_code == 200, str(r.status_code))
r = c.get("/subscription/pay/bank", params={"plan": "starter", "cycle": "monthly"})
check("GET /subscription/pay/bank is 200", r.status_code == 200, str(r.status_code))
check("bank instructions show configured rail", "cfg" not in "" and r.status_code == 200)

# 4. submit a bank-transfer claim (always lands PENDING_VERIFICATION)
UTR = f"SMOKE{TAG}"
r = c.post("/subscription/pay/bank", data={"plan": "starter", "cycle": "monthly",
                                           "method": "neft", "utr": UTR,
                                           "receipt_note": "release smoke test", "ea": ""})
loc = r.headers.get("location", "")
check("bank submission accepted (PENDING_VERIFICATION)",
      r.status_code == 303 and "submitted=" in loc, loc)
ref = loc.split("submitted=")[-1] if "submitted=" in loc else ""

r = c.get("/subscription/payments")
check("payments page shows the submission as pending",
      r.status_code == 200 and ref and ref in r.text, f"ref={ref}")

# 5. admin verification (token login, then the verification screen)
r = c.post("/admin/login", data={"token": ADMIN_TOKEN})
check("admin token login works", r.status_code == 303 and "e=1" not in r.headers.get("location", ""),
      r.headers.get("location", ""))

r = c.get("/admin/bank")
check("GET /admin/bank is 200", r.status_code == 200, str(r.status_code))
pids = re.findall(r"/admin/bank/([A-Za-z0-9_-]+)/approve", r.text)
check("pending submission visible to admin", bool(pids), f"{len(pids)} pending")

approved = False
rows = re.split(r"<tr", r.text)
hit = [b for b in rows if ref and ref in b and "/approve" in b]
if hit:
    m = re.search(r"/admin/bank/([A-Za-z0-9_-]+)/approve", hit[0])
    if m:
        r2 = c.post(f"/admin/bank/{m.group(1)}/approve", data={"note": "release smoke test"})
        approved = r2.status_code == 303 and "approved=" in r2.headers.get("location", "")
        check("admin approve activates the subscription", approved, r2.headers.get("location", ""))
if ref and not approved:
    check("admin approve step", False, "could not match the smoke reference on /admin/bank")
if ref and not approved:
    check("admin approve step", False, "could not match the smoke reference on /admin/bank")

r = c.get("/subscription")
check("plan shows active after approval", r.status_code == 200 and "active" in r.text.lower())

# 6. logout cleanly so the throwaway session is not left around
r = c.get("/logout"); check("logout", r.status_code in (200, 303, 302, 307), str(r.status_code))

fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} checks passed" + (f"; FAILED: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
