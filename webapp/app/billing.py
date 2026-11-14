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

_PLAN_ID_RE = __import__("re").compile(r"^plan_[A-Za-z0-9]{8,20}$")

def _plan_id(plan: str) -> str:
    """Read and validate the plan-id env var for this plan. A missing or
    malformed id fails loudly instead of creating an unnamed subscription."""
    import os
    env_key = PLANS[plan]["plan_id_env"]
    pid = os.environ.get(env_key, "").strip()
    if not pid:
        raise ValueError(f"{env_key} is not set: create the plan in the Razorpay dashboard first")
    if not _PLAN_ID_RE.fullmatch(pid):
        raise ValueError(f"{env_key} does not look like a Razorpay plan id (expected plan_xxxxxxxxxx)")
    return pid

def subscription_link(plan: str, merchant_id: str = "sub_placeholder") -> str:
    """Create (or reuse) the Razorpay subscription for a merchant and return its
    short_url checkout link. With no keys configured we return a placeholder so
    the sandbox flow keeps running end to end without live credentials.

    With keys configured everything validates loudly: unknown plan, missing or
    malformed plan id, or a merchant that is not onboarded and billable all
    raise. An existing usable subscription is reused (idempotent: no duplicate
    subscriptions per merchant); an expired or cancelled one gets a fresh
    creation. The merchant row keeps the subscription id for that reuse."""
    import os
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan}")
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not (key_id and key_secret):
        return f"https://rzp.io/i/checkout-{plan}-{merchant_id}"
    import razorpay  # type: ignore
    if merchant_id:
        m = MERCHANTS.get(merchant_id)
        if m is None:
            raise ValueError(f"unknown merchant {merchant_id}")
        if not is_billable(merchant_id):
            raise ValueError(f"merchant {merchant_id} is not billable (status {m.status})")
        if m.subscription_id:
            try:
                client = razorpay.Client(auth=(key_id, key_secret))
                sub = client.subscription.fetch(m.subscription_id)
                if sub.get("status") in ("created", "authenticated", "pending", "active"):
                    url = sub.get("short_url", "")
                    if url:
                        return url
            except Exception:
                pass  # fetch failed or subscription expired -> create fresh below
    plan_id = _plan_id(plan)
    client = razorpay.Client(auth=(key_id, key_secret))
    sub = client.subscription.create({
        "plan_id": plan_id, "total_count": 12, "customer_notify": 1,
        "notes": {"product": "payment-recovery", "merchant_id": merchant_id},
    })
    url = sub.get("short_url", "")
    if not url:
        raise ValueError("razorpay returned no checkout short_url for the subscription")
    if merchant_id:
        m = MERCHANTS.get(merchant_id)
        if m is not None and sub.get("id"):
            m.subscription_id = sub["id"]
            _persist(m)
    return url

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
