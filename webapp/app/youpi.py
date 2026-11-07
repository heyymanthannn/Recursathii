"""YouPI-PG adapter (sandbox mode only).

YouPI-PG is a self-hosted experimental gateway: its own documentation says it is
NOT certified, audited or production-ready, and that a regulated provider must
handle real revenue. We use it as a free sandbox to exercise the full
checkout -> webhook -> recovery flow without a paid gateway account.
Wire format per its docs: HMAC-SHA256 over the raw body with the merchant
webhook secret, sent as X-Gateway-Signature with an optional sha256= prefix.
Outcomes: SUCCESS, FAILED, EXPIRED, CANCELLED.
"""
import hmac, hashlib, os

def youpi_secret() -> bytes:
    return os.environ.get("YOUPI_WEBHOOK_SECRET", "youpi-test-secret").encode()

def verify_signature(raw_body: bytes, header: str | None, secret: bytes) -> bool:
    if not header:
        return False
    sig = header
    if sig.lower().startswith("sha256="):
        sig = sig[7:]
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)

def create_payment(api_url: str, merchant_id: str, order_id: str, amount: str,
                   return_url: str, currency: str = "INR") -> dict:
    """Server-to-server: POST /api/v1/payments. Returns checkoutUrl for redirect."""
    import httpx
    r = httpx.post(f"{api_url.rstrip('/')}/api/v1/payments",
                   headers={"Content-Type": "application/json", "X-Merchant-Id": merchant_id},
                   json={"orderId": order_id, "amount": amount, "currency": currency,
                         "returnUrl": return_url}, timeout=15)
    r.raise_for_status()
    return r.json()

OUTCOME_DECLINE_CODES = {
    "FAILED": "DO_NOT_HONOR",
    "EXPIRED": "SESSION_EXPIRED",
    "CANCELLED": "CUSTOMER_CANCELLED",
}

def ingest_outcome(payload: dict):
    from . import engine
    outcome = (payload.get("outcome") or payload.get("status") or "").upper()
    meta = payload.get("metadata") or {}
    payment_id = payload.get("paymentId") or payload.get("payment_id") or ""
    if outcome == "SUCCESS":
        engine.mark_recovered(payment_id)
        return {"handled": True, "outcome": "SUCCESS"}
    decline = OUTCOME_DECLINE_CODES.get(outcome)
    if decline is None:
        return {"handled": False, "outcome": outcome or "UNKNOWN"}
    fe = engine.ingest_normal(
        event_id=payment_id, merchant_id=meta.get("merchant_id", "demo"),
        gateway="youpi", customer_name=meta.get("name", "there"),
        customer_phone=(meta.get("phone") or "").lstrip("+"),
        amount=int(round(float(payload.get("amount", 0)) * 100)),
        decline_code=decline, mandate_id=None)
    return {"handled": fe is not None, "outcome": outcome,
            "playbook": fe.playbook if fe else None}
