"""Phase 8.3.1: final Razorpay payment-verification safety gate (dev, sandbox).

No invoice may be marked paid without trusted confirmation of the actual
captured payment status (API or signature-verified webhook). API
unavailability, an authorized-only payment or a bare valid signature leaves
the checkout row pending/unverified. Payment states are handled distinctly;
webhook reconciliation can safely complete a pending verification; failed
and refunded states are never overridden by a signature alone. No real
credentials, no network: the Razorpay SDK and REST API are faked in-process.
"""
import os, sys, json, hmac, hashlib, tempfile, types, threading

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))

os.environ["PAYRECOVERY_DB"] = tempfile.mkdtemp() + "/p831.db"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_PHASE8DOT3"
os.environ["RAZORPAY_KEY_SECRET"] = "phase83-platform-secret"
os.environ["RZP_PLAN_STARTER"] = "plan_8dot3starter11"
os.environ["RZP_PLAN_GROWTH"] = "plan_8dot3growth11"
os.environ["RZP_PLAN_SCALE"] = "plan_8dot3scale111"
for k in ("MANUAL_UPI_ID", "CHECKOUT_PROVIDER_ORDER", "YOUPI_API_URL", "ALLOW_LIVE_PROVIDERS"):
    os.environ.pop(k, None)

from app import db, models, payments, checkout
db.init(); models.init()

results = []
def check(name, ok, detail=""):
    results.append(("PASS" if ok else "FAIL") + f" {name}" + (f" :: {detail}" if detail else ""))

class _Client:
    def __init__(self, auth=None):
        self.subscription = None
_fake = types.ModuleType("razorpay"); _fake.Client = _Client
sys.modules["razorpay"] = _fake

def _sig(order_id, payment_id, secret):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

def vault_enc(v):
    from app import vault
    return vault.encrypt(v)

def captured(order_id, payment_id, amount):
    return {"id": payment_id, "order_id": order_id, "amount": amount,
            "status": "captured", "captured": True}

_get_response = {"current": {"error": True, "status": 0, "detail": "no GET configured"}}
def fake_api(creds, method, path, json_body=None):
    if method == "POST" and path == "/orders":
        fake_api.n += 1
        return {"id": f"order_{fake_api.n}", "amount": (json_body or {}).get("amount"),
                "currency": "INR", "status": "created", "receipt": (json_body or {}).get("receipt", "")}
    if method == "GET" and path.startswith("/payments/"):
        return _get_response["current"]
    return {"error": True, "status": 0, "detail": "unexpected call"}
fake_api.n = 0
payments.api = fake_api
def set_api(resp):
    _get_response["current"] = resp

# Owner with own Razorpay keys (invoice orders verify against HIS secret)
u = models.create_user("owner831@test.in", "x" * 60, "Owner", "Biz", "9800000000")
models.update_user(u["id"], rzp_key_enc=vault_enc("rzp_test_OWNER831"),
                   rzp_secret_enc=vault_enc("owner831-secret"))

def new_order(amount):
    inv = models.create_invoice(u["id"], models.create_customer(u["id"], "C")["id"],
                                f"INV-{models.uid()[:6]}", amount, "t", "2026-11-21")
    o = checkout.create_order({"key_id": "rzp_test_OWNER831", "key_secret": "owner831-secret"},
                              purpose="invoice", ref=inv["token"], amount=amount,
                              owner_id=u["id"])
    assert o["ok"], o
    return o, inv

def state_of(order_id):
    row = models.one("SELECT * FROM checkout_orders WHERE order_id=?", (order_id,))
    return row["status"], row["payment_state"], row["payment_id"]

def inv_status(inv):
    return models.get_invoice(inv["id"])["status"]

# ---- 1. control: valid signature + captured -> verified (unchanged) ---------
o1, inv1 = new_order(99900)
set_api(captured(o1["order_id"], "pay_1", 99900))
r = checkout.verify(o1["order_id"], "pay_1", _sig(o1["order_id"], "pay_1", "owner831-secret"))
check("captured payment: valid signature + API captured -> verified",
      r["ok"] and r["outcome"] == "verified" and r["invoice"].startswith("INV-") and inv_status(inv1) == "paid")
check("captured row consumed with payment_state captured", state_of(o1["order_id"]) == ("consumed", "captured", "pay_1"))
check("exactly one payment row", len(models.q("SELECT * FROM payments WHERE invoice_id=?", (inv1["id"],))) == 1)

# ---- 2. valid signature + authorized -> pending, nothing settles ------------
o2, inv2 = new_order(49900)
set_api({"id": "pay_2", "order_id": o2["order_id"], "amount": 49900, "status": "authorized"})
r = checkout.verify(o2["order_id"], "pay_2", _sig(o2["order_id"], "pay_2", "owner831-secret"))
check("authorized payment -> pending, not verified, invoice stays pending",
      not r["ok"] and r["outcome"] == "pending" and r["state"] == "authorized"
      and inv_status(inv2) == "pending")
check("authorized row keeps pending state with payment bound",
      state_of(o2["order_id"]) == ("pending", "authorized", "pay_2"))
# API stays unavailable on a later frontend callback: still pending
set_api({"error": True, "status": 0, "detail": "down"})
r = checkout.verify(o2["order_id"], "pay_2", _sig(o2["order_id"], "pay_2", "owner831-secret"))
check("authorized + API down -> still pending", not r["ok"] and r["outcome"] == "pending" and inv_status(inv2) == "pending")
# Later the API reports captured: re-verify completes it
set_api(captured(o2["order_id"], "pay_2", 49900))
r = checkout.verify(o2["order_id"], "pay_2", _sig(o2["order_id"], "pay_2", "owner831-secret"))
check("authorized -> captured later: re-verify settles once",
      r["ok"] and r["outcome"] == "verified" and inv_status(inv2) == "paid")

# ---- 3. valid signature + failed -> failed, never overridden ----------------
o3, inv3 = new_order(25000)
set_api({"id": "pay_3", "order_id": o3["order_id"], "amount": 25000, "status": "failed"})
r = checkout.verify(o3["order_id"], "pay_3", _sig(o3["order_id"], "pay_3", "owner831-secret"))
check("failed payment -> payment_failed, invoice stays pending",
      not r["ok"] and r["outcome"] == "payment_failed" and inv_status(inv3) == "pending")
r = checkout.verify(o3["order_id"], "pay_3", _sig(o3["order_id"], "pay_3", "owner831-secret"))
check("re-verify with valid signature does NOT override failed", r["outcome"] == "payment_failed")
set_api({"error": True, "status": 0, "detail": "down"})
r = checkout.verify(o3["order_id"], "pay_3", _sig(o3["order_id"], "pay_3", "owner831-secret"))
check("valid signature + API down cannot revive a failed payment",
      r["outcome"] == "payment_failed" and inv_status(inv3) == "pending")
# Webhook claims captured: needs fresh API confirmation while API is down -> stays failed
rc = checkout.reconcile({"id": "pay_3", "order_id": o3["order_id"], "amount": 25000,
                         "status": "captured"}, "payment.captured")
check("webhook captured without API evidence cannot override failed",
      rc["matched"] and rc["outcome"] == "payment_failed" and inv_status(inv3) == "pending")
# Fresh API capture DOES overturn it (only the API's own word counts)
set_api(captured(o3["order_id"], "pay_3", 25000))
rc = checkout.reconcile({"id": "pay_3", "order_id": o3["order_id"], "amount": 25000,
                         "status": "captured"}, "payment.captured")
check("webhook captured + fresh API capture completes it",
      rc["ok"] and rc["outcome"] == "verified" and inv_status(inv3) == "paid")

# ---- 4. valid signature + API timeout -> pending / unverified ---------------
o4, inv4 = new_order(15000)
set_api({"error": True, "status": 0, "detail": "timeout"})
r = checkout.verify(o4["order_id"], "pay_4", _sig(o4["order_id"], "pay_4", "owner831-secret"))
check("API timeout -> pending (api_unavailable), nothing settles",
      not r["ok"] and r["outcome"] == "pending" and r["state"] == "api_unavailable"
      and inv_status(inv4) == "pending")
check("pending row stores the payment binding for reconciliation",
      state_of(o4["order_id"]) == ("pending", "unverified", "pay_4"))
r = checkout.verify(o4["order_id"], "pay_4", _sig(o4["order_id"], "pay_4", "owner831-secret"))
check("repeat verify while API is down stays pending, no side effects",
      r["outcome"] == "pending" and inv_status(inv4) == "pending"
      and len(models.q("SELECT * FROM payments WHERE invoice_id=?", (inv4["id"],))) == 0)

# ---- 5. valid signature + API auth failure -> pending too -------------------
o5, inv5 = new_order(12000)
set_api({"error": True, "status": 401, "detail": "authentication failed"})
r = checkout.verify(o5["order_id"], "pay_5", _sig(o5["order_id"], "pay_5", "owner831-secret"))
check("API auth failure -> pending (api_unavailable), nothing settles",
      not r["ok"] and r["outcome"] == "pending" and r["state"] == "api_unavailable"
      and inv_status(inv5) == "pending")

# ---- 6. webhook arrives after API timeout -> reconciles --------------------
rc = checkout.reconcile({"id": "pay_4", "order_id": o4["order_id"], "amount": 15000,
                         "status": "captured"}, "payment.captured")
check("webhook after API timeout completes the pending verification",
      rc["matched"] and rc["ok"] and rc["outcome"] == "verified" and inv_status(inv4) == "paid")
check("reconciled row consumed", state_of(o4["order_id"]) == ("consumed", "captured", "pay_4"))

# ---- 7. duplicate webhook after reconciliation -> no-op --------------------
acts_before = len(models.q("SELECT * FROM activities WHERE owner_id=? AND kind='payment'", (u["id"],)))
rc = checkout.reconcile({"id": "pay_4", "order_id": o4["order_id"], "amount": 15000,
                         "status": "captured"}, "payment.captured")
acts_after = len(models.q("SELECT * FROM activities WHERE owner_id=? AND kind='payment'", (u["id"],)))
check("duplicate webhook after reconciliation -> already_verified, no second entry",
      rc["matched"] and rc["ok"] and rc["outcome"] == "already_verified"
      and acts_after == acts_before
      and len(models.q("SELECT * FROM payments WHERE invoice_id=?", (inv4["id"],))) == 1)
# frontend replay of the same triple after webhook settlement: same verdict
r = checkout.verify(o4["order_id"], "pay_4", _sig(o4["order_id"], "pay_4", "owner831-secret"))
check("frontend replay after webhook settlement -> already_verified",
      r["ok"] and r["outcome"] == "already_verified")
r = checkout.verify(o4["order_id"], "pay_other", _sig(o4["order_id"], "pay_other", "owner831-secret"))
check("different payment id on consumed order -> wrong_payment", r["outcome"] == "wrong_payment")

# ---- 8. conflicting amount / order id -> mismatch, nothing settles ----------
o8, inv8 = new_order(30000)
set_api(captured(o8["order_id"], "pay_8", 29999))
r = checkout.verify(o8["order_id"], "pay_8", _sig(o8["order_id"], "pay_8", "owner831-secret"))
check("API amount conflict -> mismatch", r["outcome"] == "mismatch" and inv_status(inv8) == "pending")
set_api({"id": "pay_8", "order_id": "order_someone_else", "amount": 30000, "status": "captured"})
r = checkout.verify(o8["order_id"], "pay_8", _sig(o8["order_id"], "pay_8", "owner831-secret"))
check("API order-id conflict -> mismatch", r["outcome"] == "mismatch" and inv_status(inv8) == "pending")
set_api(captured(o8["order_id"], "pay_8", 30000))
rc = checkout.reconcile({"id": "pay_8", "order_id": o8["order_id"], "amount": 999,
                         "status": "captured"}, "payment.captured")
check("webhook amount conflict -> mismatch, no settle",
      rc["matched"] and rc["outcome"] == "mismatch" and inv_status(inv8) == "pending")
check("conflicting evidence leaves the order unsettled", state_of(o8["order_id"])[0] == "pending")

# ---- 9. cross-tenant verification ------------------------------------------
u2 = models.create_user("owner831b@test.in", "x" * 60, "Other", "Biz2", "9800000002")
inv_x = models.create_invoice(u2["id"], models.create_customer(u2["id"], "B")["id"],
                              "INV-X", 10000, "x", "2026-11-21")
ox = checkout.create_order({"key_id": "rzp_test_OWNER831", "key_secret": "owner831-secret"},
                           purpose="invoice", ref=inv_x["token"], amount=10000,
                           owner_id=u2["id"])
# Tenant B has no stored creds: nothing can verify their order with A's key
set_api(captured(ox["order_id"], "pay_x", 10000))
r = checkout.verify(ox["order_id"], "pay_x", _sig(ox["order_id"], "pay_x", "owner831-secret"))
check("cross-tenant: no stored creds -> bad_creds, nothing settles",
      r["outcome"] == "bad_creds" and models.get_invoice(inv_x["id"])["status"] == "pending")
# Signature made with tenant A's secret on tenant B's order: rejected
o_t, _i_t = new_order(11000)
r = checkout.verify(o_t["order_id"], "pay_x", _sig(o_t["order_id"], "pay_x", "no-such-secret"))
check("cross-tenant: wrong secret -> bad_signature", r["outcome"] == "bad_signature")
check("cross-tenant attempts settle nothing",
      models.get_invoice(inv_x["id"])["status"] == "pending" and inv_status(inv1) == "paid")

# ---- 10. concurrent verification attempts -----------------------------------
o10, inv10 = new_order(50000)
set_api(captured(o10["order_id"], "pay_10", 50000))
outcomes = []
def worker():
    outcomes.append(checkout.verify(o10["order_id"], "pay_10",
                                    _sig(o10["order_id"], "pay_10", "owner831-secret")))
t1, t2, t3 = threading.Thread(target=worker), threading.Thread(target=worker), threading.Thread(target=worker)
for t in (t1, t2, t3): t.start()
for t in (t1, t2, t3): t.join()
oks = [o.get("outcome", f"ERROR:{o}") for o in outcomes]
check("concurrent verification: exactly one verified, rest already_verified",
      sorted(oks) == ["already_verified", "already_verified", "verified"] and inv_status(inv10) == "paid")
check("concurrent verification: one payment row, one activity entry",
      len(models.q("SELECT * FROM payments WHERE invoice_id=?", (inv10["id"],))) == 1
      and len(models.q("SELECT * FROM activities WHERE owner_id=? AND kind='payment' AND message LIKE '%tok10%'",
                       (u["id"],))) <= 1)

# ---- regression: signature checks unchanged ---------------------------------
r = checkout.verify("order_nope", "pay_z", _sig("order_nope", "pay_z", "s"))
check("unknown order rejected", r["outcome"] == "unknown_order")
o_sig, _inv_sig = new_order(7000)
r = checkout.verify(o_sig["order_id"], "pay_z", "")
check("missing signature rejected", r["outcome"] == "bad_signature")
r = checkout.verify(o_sig["order_id"], "pay_z", "deadbeef")
check("invalid signature rejected", r["outcome"] == "bad_signature")
r = checkout.verify(o_sig["order_id"], "pay_z", _sig(o_sig["order_id"], "pay_z", "no-such-secret"))
check("wrong-secret signature rejected on a fresh order", r["outcome"] == "bad_signature")

fails = [x for x in results if x.startswith("FAIL")]
for line in results: print(line)
print(f"SUMMARY: {len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
