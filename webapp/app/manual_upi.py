"""Manual UPI mode: direct-to-bank collection with zero providers.

Signup -> payment page with a UPI QR (Google Pay shows at the customer's end),
owner confirms receipt in the dashboard and activates the account.
"""
import io, time
from . import config, db

def upi_uri(plan: str, merchant_id: str) -> str:
    from .config import PLANS
    amt = PLANS[plan]["amount"] / 100
    note = f"RecurSaathi {plan} {merchant_id}"
    vpa = config.MANUAL_UPI_ID
    payee = config.MANUAL_UPI_PAYEE.replace(" ", "%20")
    return (f"upi://pay?pa={vpa}&pn={payee}&am={amt:.2f}&cu=INR&tn={note.replace(' ', '%20')}")

def qr_png(plan: str, merchant_id: str) -> bytes:
    import qrcode
    img = qrcode.make(upi_uri(plan, merchant_id))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def mark_paid(merchant_id: str) -> dict:
    """Owner confirms the UPI payment landed in the bank; activate the account."""
    m = db.load_merchants().get(merchant_id)
    if not m:
        return {"handled": False, "reason": "unknown merchant"}
    db.save_merchant({**m, "status": "active", "subscription_id": f"manual-{int(time.time())}"})
    return {"handled": True, "merchant_id": merchant_id, "status": "active"}

def pending() -> list[dict]:
    """Trial merchants waiting for payment confirmation."""
    return [m for m in db.load_merchants().values() if m.get("status") == "trial"]
