"""Our own SaaS subscription billing, running on Razorpay Subscriptions.

Merchants subscribe to Starter / Growth / Scale. A subscription.charged webhook
from OUR Razorpay account activates the merchant; subscription.halted marks them
past_due and the engine stops spending WhatsApp credits for them.
"""
from .config import PLANS
from . import db

db.init()

class Merchant:
    def __init__(self, merchant_id: str, email: str, plan: str):
        self.id = merchant_id
        self.email = email
        self.plan = plan
        self.status = "trial"          # trial | active | past_due | cancelled
        self.subscription_id = None
        self.name = ""
        self.phone = ""
        self.created = __import__("time").time()
        self.events: list = []

MERCHANTS: dict[str, Merchant] = {}

def onboard(merchant_id: str, email: str, plan: str) -> Merchant:
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan}")
    m = MERCHANTS.get(merchant_id) or Merchant(merchant_id, email, plan)
    m.plan = plan
    MERCHANTS[merchant_id] = m
    _persist(m)
    return m

def _persist(m: Merchant):
    db.save_merchant({"id": m.id, "email": m.email, "name": getattr(m, "name", ""),
                      "phone": getattr(m, "phone", ""), "plan": m.plan,
                      "status": m.status, "subscription_id": m.subscription_id,
                      "created": getattr(m, "created", 0)})

def subscription_link(plan: str, sub_id: str = "sub_placeholder") -> str:
    """Real flow: create the subscription with the Razorpay SDK and return its
    short_url checkout link. With no keys set we return a placeholder so the
    flow can be tested end to end without live credentials."""
    import os
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    if key_id:
        import razorpay  # type: ignore
        client = razorpay.Client(auth=(key_id, os.environ["RAZORPAY_KEY_SECRET"]))
        import os as _os
        plan_id = _os.environ.get(PLANS[plan]["plan_id_env"], "")
        sub = client.subscription.create({
            "plan_id": plan_id, "total_count": 12, "customer_notify": 1,
            "notes": {"product": "payment-recovery"},
        })
        return sub.get("short_url", "")
    return f"https://rzp.io/i/checkout-{plan}-{sub_id}"

def handle_billing_event(event: str, sub_entity: dict) -> dict:
    sid = sub_entity.get("id", "")
    notes = sub_entity.get("notes") or {}
    merchant_id = notes.get("merchant_id", "")
    m = MERCHANTS.get(merchant_id)
    if m is None:
        return {"handled": False, "reason": "unknown merchant"}
    if event == "subscription.charged":
        m.status = "active"
        m.subscription_id = sid
    elif event == "subscription.halted":
        m.status = "past_due"
    elif event == "subscription.cancelled":
        m.status = "cancelled"
    _persist(m)
    return {"handled": True, "merchant": m.id, "status": m.status}

def is_billable(merchant_id: str) -> bool:
    m = MERCHANTS.get(merchant_id)
    return bool(m and m.status in ("trial", "active"))
