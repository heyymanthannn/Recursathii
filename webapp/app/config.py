"""Settings and SaaS pricing plans. All secrets come from env, never hardcoded."""
import os

WEBHOOK_SECRET = os.environ.get("RZP_WEBHOOK_SECRET", "test-secret").encode()
BILLING_WEBHOOK_SECRET = os.environ.get("BILLING_WEBHOOK_SECRET", "billing-secret").encode()
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
PAY_LINK_BASE = os.environ.get("PAY_LINK_BASE", "https://recover.example/pay")

# Our own SaaS subscription model, billed through Razorpay Subscriptions.
# plan_id values are created once in the Razorpay dashboard and set in env.
PLANS = {
    "starter": {
        "plan_id_env": "RZP_PLAN_STARTER",
        "amount": 299900,            # paise: Rs 2,999/mo
        "name": "Starter",
        "trial_days": 14,
        "max_mrr_tracked": 100000,   # Rs 1L MRR
        "gateways": 1,
        "features": ["WhatsApp dunning", "RBI-window retries", "recovery dashboard"],
    },
    "growth": {
        "plan_id_env": "RZP_PLAN_GROWTH",
        "amount": 799900,            # Rs 7,999/mo
        "name": "Growth",
        "trial_days": 14,
        "max_mrr_tracked": 1000000,  # Rs 10L
        "gateways": 2,
        "features": ["everything in Starter", "2 gateways", "vernacular templates",
                      "mandate-repair flow", "pre-expiry outreach"],
    },
    "scale": {
        "plan_id_env": "RZP_PLAN_SCALE",
        "amount": 0,                 # custom: 10% of recovered revenue
        "name": "Scale",
        "trial_days": 0,
        "max_mrr_tracked": None,
        "gateways": 99,
        "features": ["everything in Growth", "unlimited gateways",
                      "10% of recovered revenue pricing"],
    },
}

# Razorpay transaction fee ~2% (verify at activation); GST applies on our invoices.
RAZORPAY_FEE_PCT = 2.0

# YouPI-PG sandbox (see app/youpi.py): sandbox only, never real money.
YOUPI_WEBHOOK_SECRET = os.environ.get("YOUPI_WEBHOOK_SECRET", "youpi-test-secret").encode()
YOUPI_API_URL = os.environ.get("YOUPI_API_URL", "")   # e.g. http://localhost:6100
YOUPI_MERCHANT_ID = os.environ.get("YOUPI_MERCHANT_ID", "")

# Manual Google Pay / UPI mode: no provider, money lands directly in the
# owner's bank account. Customers scan the QR in Google Pay (or any UPI app).
MANUAL_UPI_ID = os.environ.get("MANUAL_UPI_ID", "")   # owner UPI handle, set in Render env (kept out of the public repo)
MANUAL_UPI_PAYEE = os.environ.get("MANUAL_UPI_PAYEE", "RecurSaathi")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme-admin")  # for mark-paid endpoint
