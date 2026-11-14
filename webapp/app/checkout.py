"""Server-side checkout verification for Razorpay orders.

The frontend success callback is never trusted on its own, and neither is the
HMAC signature alone. Every confirmation goes through verify(), which checks
the HMAC signature Razorpay generates over "order_id|payment_id" with the
right key secret, binds the payment to an order we created and stored
server-side, and requires the Razorpay API (or a signature-verified webhook)
to confirm the payment entity itself before ANY invoice is marked paid.

Phase 8.3.1 gate: API unavailability is never success. When the API cannot
confirm the payment, the checkout row keeps a pending/unverified state (with
the payment id bound) and a later signature-verified webhook can complete the
verification. Payment states (captured / authorized / failed / refunded /
unverified) are handled distinctly; a valid signature alone can never revive
a failed or pending payment. Subscription state stays webhook-authoritative
(subscription.charged); this module records verified money, audit entries and
activity only.
"""
from . import models, payments

# Payment states a checkout row can hold.
CAPTURED, AUTHORIZED, FAILED, REFUNDED, UNVERIFIED = ("captured", "authorized",
                                                      "failed", "refunded", "unverified")


def _ensure():
    """Own this module's table (same lazy pattern as pilot tables). Created on
    first checkout use, so database init stays byte-identical to production."""
    with models.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS checkout_orders(
          id TEXT PRIMARY KEY, order_id TEXT UNIQUE, provider TEXT, purpose TEXT,
          ref TEXT, owner_id TEXT, merchant_id TEXT, amount INTEGER, key_id TEXT,
          status TEXT DEFAULT 'pending', payment_id TEXT, created REAL, consumed_at REAL
        )""")
        # Phase 8.3.1: track the observed payment state on the row itself.
        try:
            c.execute("ALTER TABLE checkout_orders ADD COLUMN payment_state TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists


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


def _api_state(creds, order_id, payment_id, amount):
    """Ask Razorpay what actually happened to this payment. Returns
    (state, detail): CAPTURED / AUTHORIZED / FAILED / REFUNDED confirm the
    payment entity's own status (only after the amount and order binding
    match); MISMATCH is contradicting evidence; UNAVAILABLE is API
    unavailability, which is NOT success and must never settle anything."""
    pay = payments.api(creds, "GET", f"/payments/{payment_id}")
    if pay.get("error"):
        return "unavailable", "api_unavailable"
    if pay.get("order_id") != order_id or int(pay.get("amount", -1)) != int(amount):
        return "mismatch", "payment does not match the stored order"
    status = pay.get("status")
    if status == "captured":
        return "captured", "api_confirmed"
    if status == "authorized":
        return "authorized", "authorized_not_captured"
    if status == "failed":
        return "failed", "payment failed"
    if status == "refunded":
        return "refunded", "payment refunded"
    return "pending", f"unexpected payment status: {status}"


def _persist_state(row, payment_id, state):
    """Record observed state on a still-pending row without clobbering an
    already-bound payment id."""
    models.ex("UPDATE checkout_orders SET payment_state=?, payment_id=? "
              "WHERE id=? AND status='pending' "
              "AND (payment_id='' OR payment_id IS NULL OR payment_id=?)",
              (state, payment_id, row["id"], payment_id))


def _outcome_failed(row):
    state = row["payment_state"] or ""
    if state == REFUNDED:
        return {"ok": False, "outcome": "payment_refunded", "state": REFUNDED,
                "ref": row["ref"], "purpose": row["purpose"]}
    return {"ok": False, "outcome": "payment_failed", "state": state or FAILED,
            "ref": row["ref"], "purpose": row["purpose"]}


def _settle(row, payment_id, via="checkout verification"):
    """Trusted confirmation in hand (API or signed webhook): consume the order
    exactly once and settle only what it is bound to."""
    if row["purpose"] == "invoice":
        inv = models.get_invoice_by_token(row["ref"]) or models.get_invoice(row["ref"])
        if inv and inv["status"] != "paid":
            models.mark_invoice_paid(inv, "razorpay", reference=payment_id,
                                     note="checkout verified server-side")
        if inv:
            models.log_activity(inv["owner_id"], "payment",
                                f"Checkout verified server-side ({via}): "
                                f"Rs {row['amount'] / 100:,.0f} "
                                f"for invoice {inv['number']} ({payment_id})")
            return {"ok": True, "outcome": "verified", "ref": row["ref"],
                    "invoice": inv["number"], "purpose": row["purpose"]}
        return {"ok": False, "outcome": "unknown_order"}
    if row["purpose"] == "subscription":
        models.log_audit(row["owner_id"] or row["merchant_id"], "checkout_verified",
                         f"subscription order {row['order_id']} paid by {payment_id} ({via})")
        # Merchant activation stays with the subscription.* webhook, by design.
        return {"ok": True, "outcome": "verified", "ref": row["ref"],
                "purpose": row["purpose"]}
    return {"ok": True, "outcome": "verified", "ref": row["ref"], "purpose": row["purpose"]}


def _consume(row, payment_id, state=CAPTURED, via="checkout verification"):
    """Consume pending -> consumed, atomically. The UPDATE's own rowcount is
    the claim: only the caller whose UPDATE actually matched the still-pending
    row wins and settles; concurrent duplicates (or the webhook racing the
    frontend callback) match zero rows and return already_verified untouched."""
    won = False
    with models.conn() as c:
        cur = c.execute("UPDATE checkout_orders SET status='consumed', payment_id=?, "
                        "payment_state=?, consumed_at=? WHERE id=? AND status='pending'",
                        (payment_id, state, models.now(), row["id"]))
        won = (cur.rowcount == 1)
    row2 = models.one("SELECT * FROM checkout_orders WHERE id=?", (row["id"],))
    if not won:
        return {"ok": True, "outcome": "already_verified", "ref": row2["ref"],
                "purpose": row2["purpose"]}
    return _settle(row2, payment_id, via=via)


def verify(order_id, payment_id, signature, *, cross_check=True):
    _ensure()
    """Verify and consume exactly once.

    Returns {ok, outcome, ...}: verified | already_verified | unknown_order |
    bad_signature | bad_creds | wrong_payment | mismatch | pending |
    payment_failed | payment_refunded.

    "pending" means the signature was valid but the payment is NOT yet
    confirmed: the API is unreachable, the payment is only authorized, or the
    status is unrecognized. Nothing settles on "pending"; a signature-verified
    webhook can complete it later. Side effects (invoice marked paid, audit
    entry, activity log) run only on a confirmed capture."""
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
    # Signature is valid from here on. It is necessary but NOT sufficient.
    # A payment id already bound to this order must not be swapped, and
    # terminal-failure evidence can never be overridden by a bare signature.
    if row["payment_id"] and payment_id != row["payment_id"]:
        return {"ok": False, "outcome": "wrong_payment"}
    if (row["payment_state"] or "") in (FAILED, REFUNDED):
        return _outcome_failed(row)
    if not cross_check:
        # Signature-only is no longer a settlement path, by design.
        _persist_state(row, payment_id, UNVERIFIED)
        return {"ok": False, "outcome": "pending", "state": "api_unavailable",
                "why": "cross-check disabled", "ref": row["ref"],
                "purpose": row["purpose"]}
    state, detail = _api_state(creds, order_id, payment_id, row["amount"])
    if state == "captured":
        return _consume(row, payment_id)
    if state == "authorized":
        _persist_state(row, payment_id, AUTHORIZED)
        return {"ok": False, "outcome": "pending", "state": AUTHORIZED,
                "why": detail, "ref": row["ref"], "purpose": row["purpose"]}
    if state in (FAILED, REFUNDED):
        _persist_state(row, payment_id, state)
        row = models.one("SELECT * FROM checkout_orders WHERE id=?", (row["id"],))
        return _outcome_failed(row)
    if state == "mismatch":
        # Contradicting evidence: report it, settle nothing, bind nothing.
        return {"ok": False, "outcome": "mismatch", "why": detail,
                "ref": row["ref"], "purpose": row["purpose"]}
    # API unavailable, or an unrecognized status: keep the order pending with
    # the payment bound; a webhook can complete it. Never treat this as paid.
    _persist_state(row, payment_id, UNVERIFIED)
    return {"ok": False, "outcome": "pending",
            "state": "api_unavailable" if state == "unavailable" else state,
            "why": detail, "ref": row["ref"], "purpose": row["purpose"]}


def reconcile(entity, event):
    """Complete a pending checkout verification from the merchant webhook.

    Runs INSIDE /webhooks/razorpay, after that endpoint has verified the
    webhook's HMAC signature with the webhook secret, so the payload is a
    trusted server-side confirmation. The signed webhook can safely finish a
    verification that stalled on API unavailability. It still must match the
    stored order binding (order id + amount) and can never settle an order
    consumed by a different payment, or override a failed/refunded state
    without fresh API evidence of the capture.

    Returns {"matched": False, ...} when the order_id is not a checkout order,
    so the caller's invoice-token handling proceeds unchanged."""
    _ensure()
    entity = entity or {}
    order_id = entity.get("order_id") or ""
    payment_id = entity.get("id") or ""
    if not order_id or not payment_id:
        return {"matched": False, "outcome": "unknown_order"}
    row = models.one("SELECT * FROM checkout_orders WHERE order_id=?", (order_id,))
    if not row:
        return {"matched": False, "outcome": "unknown_order"}
    def out(o):
        o["matched"] = True
        return o
    if row["status"] == "consumed":
        if payment_id == (row["payment_id"] or ""):
            return out({"ok": True, "outcome": "already_verified",
                        "ref": row["ref"], "purpose": row["purpose"]})
        return out({"ok": False, "outcome": "wrong_payment"})
    # Amount + order binding straight from the signed payload.
    try:
        amount = int(entity.get("amount", -1))
    except (TypeError, ValueError):
        amount = -1
    if amount != int(row["amount"]):
        return out({"ok": False, "outcome": "mismatch",
                    "why": "webhook payment does not match the stored order",
                    "ref": row["ref"], "purpose": row["purpose"]})
    state = entity.get("status") or ""
    captured_event = event in ("payment.captured", "payment_link.paid", "order.paid")
    if captured_event and state == "captured":
        if (row["payment_state"] or "") in (FAILED, REFUNDED):
            # A stored failure can only be overturned by the API itself,
            # never by a bare signed webhook claim.
            creds = _creds_for(row)
            if creds and creds.get("key_secret"):
                st, _d = _api_state(creds, order_id, payment_id, row["amount"])
                if st != "captured":
                    return out(_outcome_failed(row))
            else:
                return out({"ok": False, "outcome": "pending",
                            "state": row["payment_state"], "ref": row["ref"],
                            "purpose": row["purpose"]})
        return out(_consume(row, payment_id, via="webhook reconciliation"))
    if state == "authorized" or event == "payment.authorized":
        _persist_state(row, payment_id, AUTHORIZED)
        return out({"ok": False, "outcome": "pending",
                    "state": AUTHORIZED, "ref": row["ref"], "purpose": row["purpose"]})
    if event == "payment.failed" or state == "failed":
        _persist_state(row, payment_id, FAILED)
        return out(_outcome_failed(models.one("SELECT * FROM checkout_orders WHERE id=?", (row["id"],))))
    if event == "payment.refunded" or state == "refunded":
        _persist_state(row, payment_id, REFUNDED)
        return out(_outcome_failed(models.one("SELECT * FROM checkout_orders WHERE id=?", (row["id"],))))
    return out({"ok": False, "outcome": "pending", "state": state or "unknown",
                "ref": row["ref"], "purpose": row["purpose"]})
