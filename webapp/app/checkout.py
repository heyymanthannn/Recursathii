"""Server-side checkout verification for Razorpay orders.

The frontend success callback is never trusted on its own. Every confirmation
goes through verify(), which checks the HMAC signature Razorpay generates over
"order_id|payment_id" with the right key secret, binds the payment to an order
we created and stored server-side, and consumes that order exactly once.
Subscription state stays webhook-authoritative (subscription.charged); this
module records verified money, audit entries and activity only.
"""
from . import models, payments


def _ensure():
    """Own this module's table (same lazy pattern as pilot tables). Created on
    first checkout use, so database init stays byte-identical to production."""
    with models.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS checkout_orders(
          id TEXT PRIMARY KEY, order_id TEXT UNIQUE, provider TEXT, purpose TEXT,
          ref TEXT, owner_id TEXT, merchant_id TEXT, amount INTEGER, key_id TEXT,
          status TEXT DEFAULT 'pending', payment_id TEXT, created REAL, consumed_at REAL
        )""")


def create_order(creds, *, purpose, ref, amount, owner_id="", merchant_id="",
                 receipt="", notes=None):
    """Create a Razorpay order and persist the binding BEFORE any payment
    happens, so verification can never settle anything but this exact order."""
    _ensure()
    order = payments.create_order(creds, amount, receipt or f"chk-{int(models.now())}",
                                  notes or {})
    if order.get("error"):
        return {"ok": False, "error": str(order.get("detail", "order creation failed"))[:200]}
    if not order.get("id"):
        return {"ok": False, "error": "razorpay returned no order id"}
    row = {"id": models.uid(), "order_id": order["id"], "provider": "razorpay",
           "purpose": purpose, "ref": ref, "owner_id": owner_id,
           "merchant_id": merchant_id, "amount": int(amount),
           "key_id": (creds or {}).get("key_id", ""), "status": "pending",
           "payment_id": "", "created": models.now(), "consumed_at": 0}
    models.ex("INSERT INTO checkout_orders(id,order_id,provider,purpose,ref,owner_id,"
              "merchant_id,amount,key_id,status,payment_id,created,consumed_at) "
              "VALUES(:id,:order_id,:provider,:purpose,:ref,:owner_id,:merchant_id,"
              ":amount,:key_id,:status,:payment_id,:created,:consumed_at)", row)
    return {"ok": True, "order_id": row["order_id"], "key_id": row["key_id"],
            "amount": row["amount"], "checkout_id": row["id"]}


def _creds_for(row):
    """Credential resolution is server-side only: subscription checkout verifies
    against the platform key pair, invoice checkout against that invoice
    owner's own keys. The caller can never choose which secret verifies."""
    if row["purpose"] == "subscription":
        return payments.platform_creds()
    if row["purpose"] == "invoice":
        owner = models.get_user(row["owner_id"])
        return payments.owner_creds(owner) if owner else None
    return None


def _api_payment_ok(creds, order_id, payment_id, amount):
    """Belt and braces: when the API is reachable, require the payment entity
    itself to confirm the amount, the order binding and a captured status.
    Network or API errors fall back to the signature verdict, which is already
    cryptographic; a reachable API that contradicts the payload fails closed."""
    pay = payments.api(creds, "GET", f"/payments/{payment_id}")
    if pay.get("error"):
        return True, "api_unavailable"
    if pay.get("order_id") != order_id or int(pay.get("amount", -1)) != int(amount):
        return False, "payment does not match the stored order"
    if pay.get("status") not in ("captured", "authorized"):
        return False, "payment not captured"
    return True, "api_confirmed"


def verify(order_id, payment_id, signature, *, cross_check=True):
    _ensure()
    """Verify and consume exactly once.

    Returns {ok, outcome, ...}: verified | already_verified | unknown_order |
    bad_signature | bad_creds | wrong_payment | mismatch. Side effects (invoice
    marked paid, audit entry, activity log) run only on the first verified
    pass; replaying the same verified triple is a no-op that returns the same
    verdict."""
    order_id, payment_id, signature = order_id or "", payment_id or "", signature or ""
    row = models.one("SELECT * FROM checkout_orders WHERE order_id=?",
                     (order_id,)) if order_id else None
    if not row:
        return {"ok": False, "outcome": "unknown_order"}
    creds = _creds_for(row)
    if not creds or not creds.get("key_secret"):
        return {"ok": False, "outcome": "bad_creds"}
    if row["status"] == "consumed":
        # Idempotent replay of the same verified triple: same verdict, no side
        # effects. A different payment id against a consumed order is rejected.
        if payment_id and payment_id == (row["payment_id"] or ""):
            return {"ok": True, "outcome": "already_verified", "ref": row["ref"],
                    "purpose": row["purpose"]}
        return {"ok": False, "outcome": "wrong_payment"}
    if not payment_id or not signature:
        return {"ok": False, "outcome": "bad_signature"}
    if not payments.verify_checkout_signature(order_id, payment_id, signature,
                                              creds["key_secret"]):
        return {"ok": False, "outcome": "bad_signature"}
    if cross_check:
        good, why = _api_payment_ok(creds, order_id, payment_id, row["amount"])
        if not good:
            return {"ok": False, "outcome": "mismatch", "why": why}
    # Consume pending -> consumed, then re-read. Only the first verified
    # caller wins; a concurrent duplicate lands on 'consumed' instead.
    models.ex("UPDATE checkout_orders SET status='consumed', payment_id=?, consumed_at=? "
              "WHERE id=? AND status='pending'", (payment_id, models.now(), row["id"]))
    row = models.one("SELECT * FROM checkout_orders WHERE id=?", (row["id"],))
    if row["status"] != "consumed":
        return {"ok": True, "outcome": "already_verified", "ref": row["ref"],
                "purpose": row["purpose"]}
    if row["purpose"] == "invoice":
        inv = models.get_invoice_by_token(row["ref"]) or models.get_invoice(row["ref"])
        if inv and inv["status"] != "paid":
            models.mark_invoice_paid(inv, "razorpay", reference=payment_id,
                                     note="checkout verified server-side")
        if inv:
            models.log_activity(inv["owner_id"], "payment",
                                f"Checkout verified server-side: Rs {row['amount'] / 100:,.0f} "
                                f"for invoice {inv['number']} ({payment_id})")
            return {"ok": True, "outcome": "verified", "ref": row["ref"],
                    "invoice": inv["number"], "purpose": row["purpose"]}
        return {"ok": False, "outcome": "unknown_order"}
    if row["purpose"] == "subscription":
        models.log_audit(row["owner_id"] or row["merchant_id"], "checkout_verified",
                         f"subscription order {order_id} paid by {payment_id}")
        # Merchant activation stays with the subscription.* webhook, by design.
        return {"ok": True, "outcome": "verified", "ref": row["ref"],
                "purpose": row["purpose"]}
    return {"ok": True, "outcome": "verified", "ref": row["ref"], "purpose": row["purpose"]}
