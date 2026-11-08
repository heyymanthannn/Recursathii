"""Manual UPI mode: direct-to-bank collection with zero providers.

Signup -> payment page with a UPI QR (Google Pay shows at the customer's end),
owner confirms receipt in the dashboard and activates the account.
"""
import io, os, time
from . import config, db

# Welcome gift: the discount (percent off) a brand-new paying merchant gets on
# their next month. Env-configurable so it can change without a code push.
WELCOME_DISCOUNT_PCT = int(os.environ.get("WELCOME_DISCOUNT_PCT", "20"))
WELCOME_FROM_NAME = os.environ.get("WELCOME_FROM_NAME", "Team RecurSaathi")

def upi_uri(plan: str, merchant_id: str, amount: float | None = None) -> str:
    from .config import PLANS
    amt = amount if amount is not None else PLANS[plan]["amount"] / 100
    note = f"RecurSaathi {plan} {merchant_id}"
    vpa = config.MANUAL_UPI_ID
    payee = config.MANUAL_UPI_PAYEE.replace(" ", "%20")
    return (f"upi://pay?pa={vpa}&pn={payee}&am={amt:.2f}&cu=INR&tn={note.replace(' ', '%20')}")

def qr_png(plan: str, merchant_id: str, amount: float | None = None) -> bytes:
    import qrcode
    img = qrcode.make(upi_uri(plan, merchant_id, amount))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def next_bill_amount(plan: str, discount_pct: int) -> float:
    """Full plan price minus the welcome discount, for the NEXT month."""
    from .config import PLANS
    return PLANS[plan]["amount"] / 100 * (100 - discount_pct) / 100


def welcome_email(plan: str, merchant_id: str, email: str, discount_pct: int) -> tuple[str, str, str]:
    """(subject, body, note) for the automatic thank-you a new merchant gets."""
    from .config import PLANS
    full = PLANS[plan]["amount"] / 100
    next_amt = next_bill_amount(plan, discount_pct)
    subject = f"Welcome to the RecurSaathi team - your {plan.capitalize()} plan is live"
    body = f"""Hi,

Thank you for joining the RecurSaathi team. Your payment landed and your
{plan} plan is now active.

Your welcome gift: {discount_pct}% off your NEXT month.
Next month you pay Rs {next_amt:,.0f} instead of Rs {full:,.0f}. The discount is
already applied to your account - nothing for you to do, it shows up
automatically on your next renewal.

While you are here, the two things that matter in your first week:
1. Add your customers and their invoices (type them in or upload a CSV).
2. Connect WhatsApp so reminders and recovery nudges go out on their own.

If anything is unclear, just reply to this email - a human reads it.

{WELCOME_FROM_NAME}
RecurSaathi - we chase your failed payments, so you don't have to."""
    return subject, body, f"welcome email queued to {email} ({discount_pct}% off next month)"


def mark_paid(merchant_id: str) -> dict:
    """Owner confirms the UPI payment landed in the bank; activate the account.

    First payment also fires the automatic thank-you email with the next-month
    discount gift. Email sends from the brand mailbox (SMTP env vars); without
    SMTP it is recorded as pending, never pretended.
    """
    m = db.load_merchants().get(merchant_id)
    if not m:
        return {"handled": False, "reason": "unknown merchant"}
    first_time = m.get("status") == "trial"
    row = {**m, "status": "active", "subscription_id": f"manual-{int(time.time())}"}
    if first_time:
        row["next_month_discount"] = WELCOME_DISCOUNT_PCT
    mail_state = {"status": "skipped" if not first_time else "not_sent", "detail": ""}
    if first_time and m.get("email"):
        from . import notify
        subject, body, _note = welcome_email(m.get("plan") or "starter",
                                             merchant_id, m["email"], WELCOME_DISCOUNT_PCT)
        st, err = notify.send_email(m["email"], subject, body)
        mail_state = {"status": st, "detail": err}
        row["welcome_sent"] = 1 if st == "sent" else 0
    db.save_merchant(row)
    return {"handled": True, "merchant_id": merchant_id, "status": "active",
            "first_payment": first_time,
            "next_month_discount_pct": row.get("next_month_discount", 0),
            "welcome_email": mail_state}


def activations() -> list[dict]:
    """Merchants activated by manual payment, with their welcome-gift state."""
    out = []
    for m in db.load_merchants().values():
        if m.get("status") == "active":
            out.append({"id": m["id"], "email": m.get("email"), "plan": m.get("plan"),
                        "next_month_discount_pct": m.get("next_month_discount", 0),
                        "welcome_email_sent": bool(m.get("welcome_sent"))})
    return out

def pending() -> list[dict]:
    """Trial merchants waiting for payment confirmation."""
    return [m for m in db.load_merchants().values() if m.get("status") == "trial"]
