"""Phase 8.3: Razorpay integration completion tests (dev branch, sandbox only).

Covers: SDK dependency pin, subscription creation validation + idempotency,
server-side checkout verification (signature, order binding, replay/duplicate,
cross-check, credential separation, tenant isolation) and the checkout
provider priority switch (default behaviour unchanged). No real credentials,
no network: the Razorpay SDK and its REST API are faked in-process.
"""
import os, sys, json, hmac, hashlib, tempfile, types, pathlib

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))

os.environ["PAYRECOVERY_DB"] = tempfile.mkdtemp() + "/p83.db"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_PHASE8DOT3"
os.environ["RAZORPAY_KEY_SECRET"] = "phase83-platform-secret"
os.environ["RZP_PLAN_STARTER"] = "plan_8dot3starter11"
os.environ["RZP_PLAN_GROWTH"] = "plan_8dot3growth11"
os.environ["RZP_PLAN_SCALE"] = "plan_8dot3scale111"
for k in ("MANUAL_UPI_ID", "CHECKOUT_PROVIDER_ORDER", "YOUPI_API_URL", "ALLOW_LIVE_PROVIDERS"):
    os.environ.pop(k, None)

from app import config, db, models, billing, payments, checkout
from app.main import app
db.init(); models.init()

results = []
def check(name, ok, detail=""):
    results.append(("PASS" if ok else "FAIL") + f" {name}" + (f" :: {detail}" if detail else ""))

# ---- fake Razorpay SDK -----------------------------------------------------
_calls = {"create": [], "fetch": []}
_created = {}
_fetch_state = {}
class _Subscription:
    @staticmethod
    def create(payload):
        _calls["create"].append(payload)
        n = len(_calls["create"])
        _created[f"sub_new{n}"] = {"id": f"sub_new{n}", "status": "created",
                                   "short_url": f"https://rzp.io/i/new{n}"}
        return _created[f"sub_new{n}"]
    @staticmethod
    def fetch(sid):
        _calls["fetch"].append(sid)
        state = _fetch_state.get(sid) or _created.get(sid)
        if state:
            return state
        raise Exception("subscription not found")
class _Client:
    def __init__(self, auth=None):
        self.subscription = _Subscription
_fake = types.ModuleType("razorpay"); _fake.Client = _Client
sys.modules["razorpay"] = _fake

def _sig(order_id, payment_id, secret):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

# ---- A. dependency + config switch -----------------------------------------
req = pathlib.Path(os.path.join(os.path.dirname(_here), "requirements.txt")).read_text()
check("requirements pins razorpay", "razorpay==2.0.1" in req)
check("provider order default is manual_upi first",
      config.CHECKOUT_PROVIDER_ORDER == ["manual_upi", "razorpay", "youpi"])

# ---- B. subscription creation ----------------------------------------------
billing.onboard("m_ok", "ok@t.io", "starter")                       # trial -> billable
billing.onboard("m_cancelled", "c@t.io", "starter")
billing.handle_billing_event("subscription.cancelled", {"id": "s", "notes": {"merchant_id": "m_cancelled"}})

try:
    billing.subscription_link("nope", "m_ok"); check("unknown plan raises", False)
except ValueError: check("unknown plan raises", True)
_kid, _ksec = os.environ.pop("RAZORPAY_KEY_ID"), os.environ.pop("RAZORPAY_KEY_SECRET")
check("no keys -> sandbox placeholder preserved",
      billing.subscription_link("starter", "m_x").startswith("https://rzp.io/i/checkout-starter-"))
os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"] = _kid, _ksec
_saved = os.environ.pop("RZP_PLAN_STARTER")
try:
    billing.subscription_link("starter", "m_ok"); check("missing plan id raises naming env var", False)
except ValueError as e: check("missing plan id raises naming env var", "RZP_PLAN_STARTER" in str(e))
os.environ["RZP_PLAN_STARTER"] = "plan_bad!id"
try:
    billing.subscription_link("starter", "m_ok"); check("malformed plan id raises", False)
except ValueError: check("malformed plan id raises", True)
os.environ["RZP_PLAN_STARTER"] = "plan_8dot3starter11"
try:
    billing.subscription_link("starter", "m_ghost"); check("unknown merchant raises", False)
except ValueError: check("unknown merchant raises", True)
try:
    billing.subscription_link("starter", "m_cancelled"); check("non-billable merchant raises", False)
except ValueError: check("non-billable merchant raises", True)

url = billing.subscription_link("starter", "m_ok")
c0 = _calls["create"][0]
check("subscription created with plan id, 12 cycles, notify, merchant note",
      c0["plan_id"] == "plan_8dot3starter11" and c0["total_count"] == 12
      and c0["customer_notify"] == 1 and c0["notes"].get("merchant_id") == "m_ok", json.dumps(c0))
check("subscription short_url returned", url == "https://rzp.io/i/new1")
check("merchant row keeps subscription id",
      models.one("SELECT subscription_id FROM merchants WHERE id='m_ok'")["subscription_id"] == "sub_new1")
n_create = len(_calls["create"])
url2 = billing.subscription_link("starter", "m_ok")
check("idempotent: second call reuses existing subscription",
      url2 == "https://rzp.io/i/new1" and len(_calls["create"]) == n_create and _calls["fetch"][-1] == "sub_new1")
_fetch_state["sub_new1"] = {"id": "sub_new1", "status": "expired", "short_url": "https://rzp.io/i/old"}
url3 = billing.subscription_link("starter", "m_ok")
check("expired subscription -> fresh creation",
      url3 == f"https://rzp.io/i/new{n_create + 1}" and len(_calls["create"]) == n_create + 1)
_fetch_state.clear()

# ---- C. checkout verification ----------------------------------------------
_orig_api = payments.api
_api_calls = []
_get_payment_response = {"error": True, "status": 0, "detail": "no GET configured"}
def fake_api(creds, method, path, json_body=None):
    _api_calls.append((method, path))
    if method == "POST" and path == "/orders":
        return {"id": f"order_{len(_api_calls)}", "amount": (json_body or {}).get("amount"),
                "currency": "INR", "status": "created", "receipt": (json_body or {}).get("receipt", "")}
    if method == "GET" and path.startswith("/payments/"):
        return _get_payment_response
    return {"error": True, "status": 0, "detail": "unexpected call"}
payments.api = fake_api

def vault_enc(v):
    from app import vault
    return vault.encrypt(v)
u = models.create_user("owner83@test.in", "x" * 60, "Owner", "Biz", "9800000000")
models.update_user(u["id"], rzp_key_enc=vault_enc("rzp_test_OWNER83"),
                   rzp_secret_enc=vault_enc("owner83-secret"))
c1 = models.create_customer(u["id"], "Asha", phone="9800000001")
inv = models.create_invoice(u["id"], c1["id"], "INV-83A", 99900, "test A", "2026-11-21")
inv_b = models.create_invoice(u["id"], c1["id"], "INV-83B", 49900, "test B", "2026-11-21")

out = checkout.create_order({"key_id": "rzp_test_PHASE8DOT3", "key_secret": "phase83-platform-secret"},
                            purpose="invoice", ref=inv["token"], amount=99900, owner_id=u["id"])
check("order binding stored server-side",
      out["ok"] and out["amount"] == 99900 and out["order_id"].startswith("order_")
      and models.one("SELECT purpose,ref,status FROM checkout_orders WHERE order_id=?", (out["order_id"],))["status"] == "pending")

payments.api = lambda *a, **k: {"error": True, "status": 0, "detail": "network down"}
bad = checkout.create_order({"key_id": "x", "key_secret": "y"}, purpose="invoice", ref="t", amount=1)
check("order creation API failure reported, no row stored",
      not bad["ok"] and not models.one("SELECT * FROM checkout_orders WHERE order_id=?", (bad.get("order_id", ""),)))
payments.api = fake_api

r = checkout.verify("order_nope", "pay_1", _sig("order_nope", "pay_1", "k"))
check("unknown order rejected", r["outcome"] == "unknown_order")
r = checkout.verify(out["order_id"], "pay_1", "")
check("missing signature rejected", r["outcome"] == "bad_signature")
r = checkout.verify(out["order_id"], "pay_1", "deadbeef")
check("wrong signature rejected", r["outcome"] == "bad_signature")
r = checkout.verify(out["order_id"], "pay_1", _sig(out["order_id"], "pay_1", "phase83-platform-secret"))
check("invoice order refuses platform secret (creds resolved server-side)", r["outcome"] == "bad_signature")
_get_payment_response = {"id": "pay_1", "order_id": out["order_id"], "amount": 99900, "status": "captured"}
r = checkout.verify(out["order_id"], "pay_1", _sig(out["order_id"], "pay_1", "owner83-secret"))
check("valid signature + API confirms captured -> invoice verified", r["ok"] and r["outcome"] == "verified" and r["invoice"] == "INV-83A")
check("invoice marked paid exactly once",
      models.get_invoice(inv["id"])["status"] == "paid"
      and len(models.q("SELECT * FROM payments WHERE invoice_id=?", (inv["id"],))) == 1)
check("activity logged", bool(models.q("SELECT * FROM activities WHERE owner_id=? AND kind='payment'", (u["id"],))))
r2 = checkout.verify(out["order_id"], "pay_1", _sig(out["order_id"], "pay_1", "owner83-secret"))
check("replay of same triple -> already_verified, no side effects",
      r2["ok"] and r2["outcome"] == "already_verified"
      and len(models.q("SELECT * FROM payments WHERE invoice_id=?", (inv["id"],))) == 1)
r3 = checkout.verify(out["order_id"], "pay_attacker", _sig(out["order_id"], "pay_attacker", "phase83-platform-secret"))
check("different payment id on consumed order rejected", r3["outcome"] == "wrong_payment")

# cross-check against the API when it is reachable
out2 = checkout.create_order({"key_id": "rzp_test_PHASE8DOT3", "key_secret": "phase83-platform-secret"},
                             purpose="invoice", ref=inv_b["token"], amount=49900, owner_id=u["id"])
_get_payment_response = {"id": "pay_x", "order_id": out2["order_id"], "amount": 999, "status": "captured"}
r = checkout.verify(out2["order_id"], "pay_x", _sig(out2["order_id"], "pay_x", "owner83-secret"))
check("reachable API contradicting payload -> mismatch", r["outcome"] == "mismatch")
_get_payment_response = {"error": True, "status": 500, "detail": "down"}
r = checkout.verify(out2["order_id"], "pay_x", _sig(out2["order_id"], "pay_x", "owner83-secret"))
check("API unavailable -> pending, nothing settles (8.3.1 gate)",
      not r["ok"] and r["outcome"] == "pending"
      and models.get_invoice(inv_b["id"])["status"] == "pending")
_get_payment_response = {"id": "pay_x", "order_id": out2["order_id"], "amount": 49900, "status": "captured"}
r = checkout.verify(out2["order_id"], "pay_x", _sig(out2["order_id"], "pay_x", "owner83-secret"))
check("pending verification completes when the API returns captured",
      r["ok"] and r["outcome"] == "verified"
      and models.get_invoice(inv_b["id"])["status"] == "paid")
_get_payment_response = {"error": True, "status": 0, "detail": "no GET configured"}

# credential separation + tenant isolation
inv_c = models.create_invoice(u["id"], c1["id"], "INV-83C", 25000, "test C", "2026-11-21")
out3 = checkout.create_order({"key_id": "rzp_test_OWNER83", "key_secret": "owner83-secret"},
                             purpose="invoice", ref=inv_c["token"], amount=25000, owner_id=u["id"])
r = checkout.verify(out3["order_id"], "pay_c", _sig(out3["order_id"], "pay_c", "phase83-platform-secret"))
check("invoice order refuses platform secret", r["outcome"] == "bad_signature")
_get_payment_response = {"id": "pay_c", "order_id": out3["order_id"], "amount": 25000, "status": "captured"}
r = checkout.verify(out3["order_id"], "pay_c", _sig(out3["order_id"], "pay_c", "owner83-secret"))
check("invoice order verifies with the OWNER's own secret (API captured)", r["ok"] and r["outcome"] == "verified")
check("isolation: only the bound invoice settled (B paid by its own order)",
      models.get_invoice(inv_b["id"])["status"] == "paid")
inv_d = models.create_invoice(u["id"], c1["id"], "INV-83D", 15000, "test D", "2026-11-21")
check("isolation: invoice D still pending (no cross-settling)",
      models.get_invoice(inv_d["id"])["status"] == "pending")

u2 = models.create_user("owner83b@test.in", "x" * 60, "Other", "Biz2", "9800000002")
inv_e = models.create_invoice(u2["id"], models.create_customer(u2["id"], "B")["id"], "INV-83E", 10000, "e", "2026-11-21")
out4 = checkout.create_order({"key_id": "rzp_test_OWNER83", "key_secret": "owner83-secret"},
                             purpose="invoice", ref=inv_e["token"], amount=10000, owner_id=u2["id"])
r = checkout.verify(out4["order_id"], "pay_e", _sig(out4["order_id"], "pay_e", "owner83-secret"))
check("tenant isolation: no creds -> bad_creds, nothing settles",
      r["outcome"] == "bad_creds" and models.get_invoice(inv_e["id"])["status"] == "pending")

# subscription purpose: platform secret only, webhook stays authoritative
m2 = billing.onboard("m_sub", "s@t.io", "growth")
out5 = checkout.create_order({"key_id": "rzp_test_PHASE8DOT3", "key_secret": "phase83-platform-secret"},
                             purpose="subscription", ref="m_sub", amount=499900, merchant_id="m_sub")
r = checkout.verify(out5["order_id"], "pay_s", _sig(out5["order_id"], "pay_s", "owner83-secret"))
check("subscription order refuses owner secret", r["outcome"] == "bad_signature")
_get_payment_response = {"id": "pay_s", "order_id": out5["order_id"], "amount": 499900, "status": "captured"}
r = checkout.verify(out5["order_id"], "pay_s", _sig(out5["order_id"], "pay_s", "phase83-platform-secret"))
audit_before = len(models.q("SELECT * FROM audit WHERE action='checkout_verified'"))
check("subscription verify -> verified + audited", r["ok"] and r["outcome"] == "verified" and audit_before >= 1)
check("subscription state NOT set by verify (webhook authoritative)",
      billing.MERCHANTS["m_sub"].status == "trial")
checkout.verify(out5["order_id"], "pay_s", _sig(out5["order_id"], "pay_s", "phase83-platform-secret"))
check("replay adds no second audit entry",
      len(models.q("SELECT * FROM audit WHERE action='checkout_verified'")) == audit_before)

# ---- D. endpoints + provider priority switch --------------------------------
from fastapi.testclient import TestClient
tc = TestClient(app)

r = tc.post(f"/checkout/m_ok/razorpay-order")
check("subscription order endpoint -> 200 with order", r.status_code == 200 and r.json().get("order_id", "").startswith("order_"))
r = tc.post("/checkout/verify", json={"order_id": "order_nope", "payment_id": "p", "signature": "s"})
check("verify endpoint 404 on unknown order", r.status_code == 404)
oid = r.json().get("order_id", "") if False else None
r2 = tc.post(f"/checkout/m_ok/razorpay-order")
oid = r2.json()["order_id"]
r = tc.post("/checkout/verify", json={"order_id": oid, "payment_id": "pay_e2", "signature": "bad"})
check("verify endpoint 400 on bad signature", r.status_code == 400)
_amt = models.one("SELECT amount FROM checkout_orders WHERE order_id=?", (oid,))["amount"]
_get_payment_response = {"id": "pay_e2", "order_id": oid, "amount": _amt, "status": "captured"}
r = tc.post("/checkout/verify", json={"order_id": oid, "payment_id": "pay_e2",
                                      "signature": _sig(oid, "pay_e2", "phase83-platform-secret")})
check("verify endpoint 200 on valid signature", r.status_code == 200 and r.json()["outcome"] == "verified")
r = tc.post("/checkout/verify", json={"order_id": oid, "payment_id": "pay_e2",
                                      "signature": _sig(oid, "pay_e2", "phase83-platform-secret")})
check("verify endpoint replay -> 200 already_verified", r.status_code == 200 and r.json()["outcome"] == "already_verified")

# signup provider priority
def signup(email):
    return tc.post("/api/signup", json={"plan": "starter", "email": email, "phone": "9800000000",
                                        "password": "longenough1", "name": "T"}).json()
os.environ["MANUAL_UPI_ID"] = "owner@upi"; config.MANUAL_UPI_ID = "owner@upi"
s = signup("prio1@test.in")
check("default priority: manual UPI still first", s["provider"] == "manual")
config.CHECKOUT_PROVIDER_ORDER = ["razorpay", "manual_upi", "youpi"]
s = signup("prio2@test.in")
check("switch works: razorpay first when reordered", s["provider"] == "razorpay" and s["checkout_url"].startswith("https://rzp.io/i/"))
config.CHECKOUT_PROVIDER_ORDER = ["manual_upi", "razorpay", "youpi"]
config.MANUAL_UPI_ID = ""
os.environ.pop("MANUAL_UPI_ID", None)
s = signup("prio3@test.in")
check("razorpay-only config -> provider razorpay", s["provider"] == "razorpay")
_kid2, _ksec2 = os.environ.pop("RAZORPAY_KEY_ID"), os.environ.pop("RAZORPAY_KEY_SECRET")
s = signup("prio4@test.in")
check("no providers configured -> stub fallback", s["provider"] == "stub")
os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"] = _kid2, _ksec2

payments.api = _orig_api
check("no live providers flag", "ALLOW_LIVE_PROVIDERS" not in os.environ)

print("\n".join(results))
print(f"SUMMARY: {sum(r.startswith('PASS') for r in results)}/{len(results)} passed")
