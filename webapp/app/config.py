"""Settings and SaaS pricing plans. All secrets come from env, never hardcoded."""
import os

# Phase 7.6: webhook secrets are REQUIRED. Fail closed when the env var is
# unset, blank, or set to a known weak/default value: _verify() rejects every
# request whose secret is empty, so a missing env var can never silently
# accept unsigned or forged gateway payloads.
_WEAK_WEBHOOK_SECRETS = {
    "", "test-secret", "billing-secret", "youpi-test-secret",
    "changeme", "secret", "changeme-admin",
}

def _webhook_secret(name: str) -> bytes:
    value = os.environ.get(name, "")
    return value.encode() if value.strip() and value not in _WEAK_WEBHOOK_SECRETS else b""

WEBHOOK_SECRET = _webhook_secret("RZP_WEBHOOK_SECRET")
BILLING_WEBHOOK_SECRET = _webhook_secret("BILLING_WEBHOOK_SECRET")
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
        "max_invoices_month": 200,
        "max_emails_month": 500,
        "max_ai_ops_month": 100,
        "max_workflows": 2,
        "branches": 1,
        "gateways": 1,
        "features": ["Customer management", "Invoice management",
                      "Recovery Radar (overdue detection)",
                      "Manual UPI payment collection", "Email reminders",
                      "Basic analytics", "Failed payment tracking",
                      "1 team member", "500 customers", "200 invoices/month"],
    },
    "growth": {
        "plan_id_env": "RZP_PLAN_GROWTH",
        "amount": 499900,            # Rs 4,999/mo
        "annual_amount": 4999000,    # Rs 49,990/yr
        "name": "Growth",
        "trial_days": 14,
        "max_mrr_tracked": None,
        "max_team": 5,
        "max_customers": 5000,
        "max_invoices_month": 2000,
        "max_emails_month": 5000,
        "max_ai_ops_month": 1000,
        "max_workflows": 10,
        "branches": 1,
        "gateways": 2,
        "features": ["Everything in Starter",
                      "Automated email & WhatsApp follow-ups (bring your WhatsApp Business account)",
                      "Dormant customer reactivation", "AI Recovery Assistant",
                      "Advanced analytics & weekly reports",
                      "5 team members", "5,000 customers", "2,000 invoices/month"],
    },
    "enterprise": {
        "plan_id_env": "RZP_PLAN_SCALE",   # keeps the existing Razorpay plan env var
        "amount": 599900,            # Rs 5,999/mo
        "annual_amount": 5999000,    # Rs 59,990/yr
        "name": "Enterprise",
        "trial_days": 14,
        "max_mrr_tracked": None,
        "max_team": 10,
        "max_customers": 10000,
        "max_invoices_month": 5000,
        "max_emails_month": 10000,
        "max_ai_ops_month": 2500,
        "max_workflows": 20,
        "branches": 5,
        "gateways": 99,
        "features": ["Everything in Growth",
                      "Advanced AI assistant & daily insights",
                      "Custom business reports", "Team roles & permissions",
                      "Priority support",
                      "10 team members", "10,000 customers", "5,000 invoices/month"],
    },
}

# Older accounts may still carry plan="scale"; it maps to Enterprise.
PLAN_ALIASES = {"scale": "enterprise"}


def plan_key(plan):
    return PLAN_ALIASES.get(plan, plan)

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

# ---- Plan feature entitlements -------------------------------------------
# A feature only appears in the UI or API when it is implemented in the app.
# These keys gate exactly that: Enterprise adds ten capabilities on top of
# everything in Growth. Starter and Growth keep every existing feature.
FEATURE_LABELS = {
    "multi_branch": "Multi-branch management (up to 5 branches)",
    "custom_rules": "Custom automation rules",
    "api_access": "Advanced API integrations (REST API + keys)",
    "custom_reports": "Custom business reports (PDF)",
    "advanced_permissions": "Advanced employee permissions (manager role)",
    "predictive_leaks": "Predictive revenue leak detection",
    "advanced_assistant": "Advanced AI business assistant (daily insights)",
    "priority_support": "Priority support",
    "onboarding": "Dedicated onboarding",
}

PLAN_FEATURES = {
    "starter": [],
    "growth": [],
    "enterprise": ["multi_branch", "custom_rules", "api_access", "custom_reports",
                   "advanced_permissions", "predictive_leaks", "advanced_assistant",
                   "priority_support", "onboarding"],
}

# ---- Checkout provider priority (prepared, NOT activated) -----------------
# New signups try providers in this order; the first one that is configured
# wins. The default reproduces the legacy behaviour exactly: manual UPI first,
# then Razorpay, then the YouPI sandbox. MANUAL_UPI_ID itself is untouched;
# reordering to put Razorpay first happens only after the sandbox verification
# battery passes, by setting CHECKOUT_PROVIDER_ORDER in the environment.
CHECKOUT_PROVIDER_ORDER = [p.strip() for p in
                           os.environ.get("CHECKOUT_PROVIDER_ORDER",
                                          "manual_upi,razorpay,youpi").split(",")
                           if p.strip()]

def has_feature(plan, key):
    """Backend entitlement check: is this feature part of the plan?"""
    return key in PLAN_FEATURES.get(plan_key(plan), [])
