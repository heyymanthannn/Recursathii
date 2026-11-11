"""Security regression tests for the Nov 11 launch-checklist fixes.

Run:  cd webapp && python3 -m tests.test_security_regressions
(or pytest tests/test_security_regressions.py)

Covers: scheduler tick auth, signup/onboard email validation, UTR + coupon
escaping, admin token constant-time compare, admin cookie digest + flags,
portal cookie flags. Runs against an isolated temp SQLite database.
"""
import os, sys, hashlib, tempfile

# Isolated environment BEFORE app modules import.
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_sec_", suffix=".sqlite")
os.environ["MANUAL_UPI_ID"] = "test@upi"
for k in ("RAZORPAY_KEY_ID", "YOUPI_API_URL", "SCHEDULER_TICK_TOKEN",
          "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "BACKUP_ENC_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from app import main as app_main          # noqa: E402
from app import portal                    # noqa: E402

client = TestClient(app_main.app)  # plain client: startup events stay off
ADMIN_TOKEN = app_main.config.ADMIN_TOKEN
ADMIN_DIGEST = hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()

XSS = "<script>alert(1)</script>"

def _signup(email, plan="starter"):
    r = client.post("/api/signup", json={
        "plan": plan, "email": email, "phone": "9876543210",
        "password": "password123", "name": "Test Merchant"})
    assert r.status_code == 200, r.text
    data = r.json()
    url = data.get("url") or data.get("checkout_url") or ""
    assert "/checkout/" in url, data
    return url.rsplit("/", 1)[1]

def test_esc_helper():
    assert app_main._esc(XSS) == "&lt;script&gt;alert(1)&lt;/script&gt;"

def test_scheduler_tick_requires_token():
    r = client.post("/scheduler/tick")
    assert r.status_code == 403, f"unauthenticated tick allowed: {r.status_code}"
    os.environ["SCHEDULER_TICK_TOKEN"] = "test-secret"
    try:
        r2 = client.post("/scheduler/tick", headers={"X-Scheduler-Token": "test-secret"})
        assert r2.status_code == 200, r2.text
        r3 = client.post("/scheduler/tick", headers={"X-Scheduler-Token": "wrong"})
        assert r3.status_code == 403
    finally:
        os.environ.pop("SCHEDULER_TICK_TOKEN", None)

def test_signup_rejects_bad_email():
    r = client.post("/api/signup", json={
        "plan": "starter", "email": "not-an-email", "phone": "9876543210",
        "password": "password123"})
    assert r.status_code == 400, r.text
    r2 = client.post("/api/signup", json={
        "plan": "starter", "email": "a b@test.com", "phone": "9876543210",
        "password": "password123"})
    assert r2.status_code == 400, r2.text

def test_signup_accepts_valid_email_and_normalizes_case():
    r = client.post("/api/signup", json={
        "plan": "starter", "email": "  CaseTest@Example.COM ", "phone": "9876543210",
        "password": "password123", "name": "Case"})
    assert r.status_code == 200, r.text
    from app import models
    assert models.get_user_by_email("casetest@example.com") is not None

def test_onboard_rejects_bad_email():
    r = client.post("/onboard", json={"merchant_id": "m_onb1", "email": "bad", "plan": "starter"})
    assert r.status_code == 400, r.text

def test_claimed_utr_escaped_on_admin_panel():
    mid = _signup("utrtest@example.com")
    r = client.post(f"/checkout/{mid}/paid-claim", data={"utr": XSS})
    assert r.status_code in (200, 303), r.text
    client.cookies.set("admin_token", ADMIN_DIGEST)
    page = client.get("/admin")
    client.cookies.clear()
    assert page.status_code == 200
    body = page.text
    assert "<script" not in body.lower(), "raw UTR rendered in admin panel"
    assert "&lt;script" in body.lower(), "UTR not escaped"

def test_coupon_code_escaped_on_checkout():
    mid = _signup("coupontest@example.com")
    from app import db
    row = db.load_merchants()[mid]
    row["coupon_code"] = XSS
    db.save_merchant(row)
    page = client.get(f"/checkout/{mid}")
    assert page.status_code == 200
    assert XSS not in page.text, "raw coupon code rendered on checkout"
    assert "&lt;script" in page.text.lower()

def test_admin_header_token_constant_time_check():
    r = client.post("/admin/mark-paid", json={"merchant_id": "nope"},
                    headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    r2 = client.post("/admin/mark-paid", json={"merchant_id": "nope"},
                     headers={"X-Admin-Token": ADMIN_TOKEN})
    assert r2.status_code != 401, r2.text

def test_admin_login_sets_digest_cookie_not_raw_token():
    r = client.post("/admin/login", data={"token": "wrong"}, follow_redirects=False)
    assert r.status_code == 303 and "e=1" in r.headers.get("location", "")
    r2 = client.post("/admin/login", data={"token": ADMIN_TOKEN}, follow_redirects=False)
    sc = r2.headers.get("set-cookie", "")
    assert "admin_token=" + ADMIN_DIGEST in sc, sc
    assert ADMIN_TOKEN not in sc, "raw admin token leaked into cookie"
    assert "HttpOnly" in sc and "SameSite=lax" in sc and "Secure" in sc, sc
    # digest cookie grants access; raw token cookie must NOT
    client.cookies.set("admin_token", ADMIN_DIGEST)
    ok = client.get("/admin")
    client.cookies.clear()
    assert ok.status_code == 200 and "Pending payments" in ok.text
    client.cookies.set("admin_token", ADMIN_TOKEN)
    bad = client.get("/admin")
    client.cookies.clear()
    assert "Enter your admin token" in bad.text, "raw-token cookie accepted"

def test_admin_panel_rejects_missing_cookie():
    r = client.get("/admin")
    assert "Enter your admin token" in r.text

def test_portal_cookie_flags():
    resp = None
    from fastapi.responses import Response
    r = Response()
    portal._portal_session(r, "Cust@Example.com")
    sc = r.headers.get("set-cookie", "")
    assert "HttpOnly" in sc and "SameSite=lax" in sc and "Secure" in sc, sc

if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for f in fns:
        try:
            f(); print("PASS", f.__name__)
        except Exception:
            fails += 1
            print("FAIL", f.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
