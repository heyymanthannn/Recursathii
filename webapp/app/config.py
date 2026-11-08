"""Settings and SaaS pricing plans. All secrets come from env, never hardcoded."""
import os

WEBHOOK_SECRET = os.environ.get("RZP_WEBHOOK_SECRET", "test-secret").encode()
BILLING_WEBHOOK_SECRET = os.environ.get("BILLING_WEBHOOK_SECRET", "billing-secret").encode()
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
PAY_LINK_BASE = os.environ.get("PAY_LINK_BASE", "https://recover.example/pay")
# Public base URL of this app, used in links inside automated messages.
BASE_URL = os.environ.get("BASE_URL", "https://recursathii.onrender.com")

# Our own SaaS subscription model, billed through Razorpay Subscriptions.
# plan_id values are created once in the Razorpay dashboard and set in env.
PLANS = {
    "starter": {
        "plan_id_env": "RZP_PLAN_STARTER",
        "amount": 99900,             # paise: Rs 999/mo
        "annual_amount": 999000,     # Rs 9,990/yr (2 months free)
        "name": "Starter",
        "trial_days": 14,
        "max_mrr_tracked": None,
        "max_team": 1,
        "max_customers": 500,
        "max_invoices_month": 100,
        "gateways": 1,
        "features": ["Customer management", "Invoice management", "Basic Recovery Radar",
                      "Manual UPI payment collection", "Basic analytics", "Email notifications",
                      "1 team member", "500 customers", "100 invoices/month"],
    },
    "growth": {
        "plan_id_env": "RZP_PLAN_GROWTH",
        "amount": 249900,            # Rs 2,499/mo
        "annual_amount": 2499000,    # Rs 24,990/yr
        "name": "Growth",
        "trial_days": 14,
        "max_mrr_tracked": None,
        "max_team": 5,
        "max_customers": 2500,
        "max_invoices_month": 1000,
        "gateways": 2,
        "features": ["Everything in Starter", "Advanced Recovery Radar", "Automated email reminders",
                      "WhatsApp integration (needs your WhatsApp Business account)", "Dormant customer recovery",
                      "Failed payment tracking", "Advanced analytics", "5 team members",
                      "2,500 customers", "1,000 invoices/month"],
    },
    "scale": {
        "plan_id_env": "RZP_PLAN_SCALE",
        "amount": 599900,            # Rs 5,999/mo
        "annual_amount": 5999000,    # Rs 59,990/yr
        "name": "Scale",
        "trial_days": 14,
        "max_mrr_tracked": None,
        "max_team": 15,
        "max_customers": 10000,
        "max_invoices_month": 5000,
        "gateways": 99,
        "features": ["Everything in Growth", "AI Recovery Assistant", "Advanced reports",
                      "Custom integrations", "Team management", "Priority support",
                      "15 team members", "10,000 customers", "5,000 invoices/month"],
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
