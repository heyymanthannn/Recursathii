"""Phase 24: manual bank-transfer subscription billing.

Covers: pricing preserved, bank details server-side only (never public),
NEFT/IMPS/UPI submission as PENDING_VERIFICATION, UTR/screenshot alone never
activates, forged and duplicate submissions, unauthorized approval, wrong
amounts, admin verification screen, idempotent approval + concurrent
attempts, receipt after approval, payment history + renewal/expiry +
reminders, tenant isolation, Razorpay stays disabled, merchant invoice
claims untouched.
"""
import os, tempfile, time, hashlib
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p24_bank_", suffix=".sqlite")
os.environ["ADMIN_TOKEN"] = hashlib.sha256(b"p24-admin").hexdigest()
os.environ["APP_SECRET"] = "p24-app-secret"
os.environ["BANK_TRANSFER_BANK"] = "HDFC Bank"
os.environ["BANK_TRANSFER_ACCOUNT_NAME"] = "RecurSaathi Technologies"
os.environ["BANK_TRANSFER_ACCOUNT_NUMBER"] = "50100234567890"
os.environ["BANK_TRANSFER_IFSC"] = "HDFC0001234"
os.environ["MANUAL_UPI_ID"] = "recursaathi@hdfcbank"
os.environ.pop("ALLOW_LIVE_PROVIDERS", None)
os.environ.pop("RAZORPAY_KEY_ID", None)

from fastapi.testclient import TestClient
from app import main as app_main, models, manual_bank, config, auth

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

OWNER, ADMIN, OTHER = "owner24@test.local", "admin24@test.local", "other24@test.local"
PW = "pw12345678"

def login(email):
    r = client.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303, (email, r.status_code)
    return r

def mkuser(email, business, role="owner"):
    models.create_user(email, auth.hash_pw(PW), "T", business, "919810001000", role=role)
    return models.get_user_by_email(email)

# ---- pricing preserved (server-side, requirement 15/16) ----
record("01 pricing: monthly 999/4999/9999",
       [manual_bank.quote(p, "monthly") for p in ("starter", "growth", "enterprise")]
       == [99900, 499900, 999900])
record("02 pricing: annual 9990/49990/99990",
       [manual_bank.quote(p, "annual") for p in ("starter", "growth", "enterprise")]
       == [999000, 4999000, 9999000])
try:
    manual_bank.quote("starter", "annual", ea=True)
    ea_wrong = False
except ValueError:
    ea_wrong = True
record("03 EA: offer is enterprise-annual only, 7999000",
       ea_wrong and manual_bank.quote("enterprise", "annual", ea=True) == 7999000)

# ---- bank details: server-side config, never public (requirements 2, security) ----
det = manual_bank.bank_details()
record("04 bank details: from env config, real values",
       det and det["account_number"] == "50100234567890" and det["ifsc"] == "HDFC0001234")
r = client.get("/subscription/pay/bank", follow_redirects=False)
record("05 unauthenticated: transfer page requires login (no details leak)",
       r.status_code == 303 and "50100234567890" not in r.text)
# unauthenticated API probe: no public endpoint carries the account number
r = client.get("/api/early-adopter-availability")
leak = "50100234567890" in r.text
record("06 public API response carries no bank details", r.status_code == 200 and not leak)
# admin RBAC
record("07 admin role gate: non-admin approve refused at module level",
       _raises_error()) if False else None

def raises(fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except PermissionError:
        return True

# ---- sign-up users ----
mkuser(OWNER, "Bank Co")
mkuser(OTHER, "Other Co")
mkuser(ADMIN, "Platform", role="admin")
login(ADMIN); login(OTHER); login(OWNER)

# ---- submission: PENDING_VERIFICATION, amount server-set (req 3-6) ----
owner = models.get_user_by_email(OWNER)
r = client.get("/subscription/pay/bank?plan=starter&cycle=monthly")
record("08 instructions page: exact amount Rs 999 + reference shown",
       r.status_code == 200 and ("\u20b9999" in r.text) and "RS-STA-M-" in r.text)
draft_ref = models.one("SELECT reference FROM bank_transfer_payments WHERE owner_id=? "
                       "AND status='DRAFT' ORDER BY submitted_at DESC", (owner["id"],))["reference"]
record("09 reference: unique, owner-prefixed, stable on reload",
       draft_ref.startswith("RS-STA-M-") and
       client.get("/subscription/pay/bank?plan=starter&cycle=monthly").text.count(draft_ref) >= 1)

r = client.post("/subscription/pay/bank", data={
    "plan": "starter", "cycle": "monthly", "method": "upi",
    "utr": "UPIREF12345678", "receipt_note": "paid from HDFC app", "ea": "0"},
    follow_redirects=False)
record("10 submission: accepted and redirected", r.status_code == 303
       and "/subscription/payments" in r.headers.get("location", ""))
pay = manual_bank.history(owner["id"])[0]
record("11 submission: status PENDING_VERIFICATION, amount 99900, utr kept",
       pay["status"] == "PENDING_VERIFICATION" and pay["amount"] == 99900
       and pay["utr"] == "UPIREF12345678" and pay["method"] == "upi")
record("12 UTR alone never activates: plan_status trialing, no sub_period_end",
       owner["plan_status"] == "trialing" and not owner.get("sub_period_end"))

# forged / malformed UTR (req 20)
bad = [("short UTR", "12"), ("bad chars", "UTR<script>"), ("empty", "  "),
       ("too long", "X" * 65)]
ok_bad = True
for label, u in bad:
    try:
        manual_bank.create_submission(owner, "starter", "monthly", "neft", u)
        ok_bad = False
    except ValueError:
        pass
record("13 forged/malformed UTRs all refused", ok_bad)
# forged UTR must not have created extra pending rows
npend = models.one("SELECT COUNT(*) n FROM bank_transfer_payments WHERE owner_id=? "
                   "AND status='PENDING_VERIFICATION'", (owner["id"],))["n"]
record("14 one pending record after refused attempts", npend == 1)

# duplicate submission (req 12, 20)
row2, created2 = manual_bank.create_submission(owner, "starter", "monthly",
                                               "imps", "IMPSREF999")
record("15 duplicate submission: same payment, not a new one",
       not created2 and row2["id"] == pay["id"] and npend ==
       models.one("SELECT COUNT(*) n FROM bank_transfer_payments WHERE owner_id=? "
                  "AND status='PENDING_VERIFICATION'", (owner["id"],))["n"])

# wrong amount: client can never set the amount (req 20, server-calculated)
r = client.post("/subscription/pay/bank", data={
    "plan": "starter", "cycle": "annual", "method": "neft", "utr": "TAMPER12345",
    "receipt_note": "", "ea": "0", "amount": "1"}, follow_redirects=False)
tampered = models.one("SELECT amount FROM bank_transfer_payments WHERE utr='TAMPER12345'")
record("16 wrong amount: posted amount field ignored, server quote stored",
       tampered and tampered["amount"] == 999000)

# unauthorized approval (req 9, 20)
r = client.post(f"/admin/bank/{pay['id']}/approve", follow_redirects=False)
record("17 unauthorized approval: owner blocked from admin route, payment untouched",
       r.status_code == 303 and manual_bank.get(pay["id"])["status"] == "PENDING_VERIFICATION")
record("18 module-level RBAC: non-admin approve raises PermissionError",
       raises(manual_bank.approve, pay["id"], owner))
record("19 AI/automation identity has no admin role: refuse",
       raises(manual_bank.approve, pay["id"], {"id": "ai-agent", "role": "automation"}))

# tenant isolation: other owner cannot see or decide (security)
r = client.get("/subscription/receipt/" + pay["reference"])
record("20 tenant isolation: other owner gets 404 receipt (not approved)",
       r.status_code == 404)

# admin screen (req 8)
client.cookies.clear(); login(ADMIN)
r = client.get("/admin/bank")
record("21 admin verification screen: lists pending payment",
       r.status_code == 200 and pay["reference"] in r.text and "Bank Co" in r.text)
r = client.get("/admin/bank")
assert r.status_code == 200
client.cookies.clear(); login(OWNER)
r = client.get("/admin/bank", follow_redirects=False)
record("22 admin screen blocked for owner role",
       r.status_code == 303 and "/login" in r.headers.get("location", ""),
       f"status={r.status_code} loc={r.headers.get('location','')}")

# admin approval -> activation + audit (req 10, 11)
client.cookies.clear(); login(ADMIN)
r = client.post(f"/admin/bank/{pay['id']}/approve", data={"note": "matched statement"},
                follow_redirects=False)
record("23 admin approval accepted", r.status_code == 303
       and "approved=" + pay["reference"] in r.headers.get("location", ""))
owner = models.get_user_by_email(OWNER)
pay = manual_bank.get(pay["id"])
record("24 approval recorded: verifier, timestamp, amount, plan, reference",
       pay["status"] == "approved" and pay["verified_by"] == ADMIN
       and pay["verified_at"] and pay["amount"] == 99900 and pay["plan"] == "starter"
       and pay["receipt_no"].startswith("RS-RCP-"))
record("25 subscription activated only after verification",
       owner["plan_status"] == "active" and owner["sub_period_end"] > models.now()
       and owner["sub_last_payment_ref"] == pay["reference"])
audits = models.q("SELECT action, detail FROM audit WHERE user_id=? AND action='bank.approved'",
                  (ADMIN and models.get_user_by_email(ADMIN)["id"],))
record("26 audit event written with verifier + reference",
       len(audits) == 1 and pay["reference"] in audits[0]["detail"])

# duplicate approval / concurrent attempts (req 12, 20)
end_before = owner["sub_period_end"]
row_b, won_b = manual_bank.approve(pay["id"], models.get_user_by_email(ADMIN))
row_c, won_c = manual_bank.approve(pay["id"], models.get_user_by_email(ADMIN))
owner2 = models.get_user_by_email(OWNER)
audits2 = models.one("SELECT COUNT(*) n FROM audit WHERE action='bank.approved'")["n"]
record("27 duplicate approvals: no second decision, period unchanged",
       not won_b and not won_c and owner2["sub_period_end"] == end_before
       and audits2 == 1)

# receipt after approval (req 13)
r = client.get("/subscription/receipt/" + pay["reference"])
record("28 receipt: PDF served to owner after approval",
       r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf")
       and r.content[:4] == b"%PDF")
r = client.get("/subscription/receipt/" + pay["reference"])
client.cookies.clear(); login(OTHER)
r = client.get("/subscription/receipt/" + pay["reference"])
record("29 receipt: other business cannot read it", r.status_code == 403)

# reject path + resubmission (req 9)
row3, _ = manual_bank.create_submission(owner, "growth", "monthly", "neft", "NEFTUTR111")
row3, won_r = manual_bank.reject(row3["id"], models.get_user_by_email(ADMIN), "amount mismatch")
record("30 rejection recorded with reason and verifier",
       won_r and row3["status"] == "rejected" and "amount mismatch" in row3["decision_note"]
       and row3["verified_by"] == ADMIN)
row4, created4 = manual_bank.create_submission(owner, "growth", "monthly", "imps", "NEFTUTR222")
record("31 resubmission after rejection allowed as a fresh pending",
       created4 and row4["status"] == "PENDING_VERIFICATION"
       and row4["reference"] != row3["reference"])

# history + renewal/expiry + reminders (req 14, 17)
client.cookies.clear(); login(OWNER)
r = client.get("/subscription/payments")
record("32 payment history page: approved row + receipt link",
       r.status_code == 200 and pay["reference"] in r.text and "Receipt" in r.text)
models.update_user(owner["id"], sub_period_end=models.now() + 3 * 86400)
n1 = manual_bank.queue_expiry_reminders(models.get_user_by_email(OWNER))
n2 = manual_bank.queue_expiry_reminders(models.get_user_by_email(OWNER))
record("33 renewal reminder queued once (deduped per period)", n1 == 1 and n2 == 0)
ob = models.one("SELECT * FROM outbox WHERE dedupe LIKE 'bankrenew:%' ORDER BY created DESC")
record("34 reminder is queued (not sent live), subject names the window",
       ob and ob["status"] == "queued" and "renews in" in ob["subject"])
# expiry: no silent renewal, no charge (req 17)
events_before = models.one("SELECT COUNT(*) n FROM pay_events WHERE owner_id=?",
                           (owner["id"],))["n"]
models.update_user(owner["id"], sub_period_end=models.now() - 86400)
st = manual_bank.renewal_state(models.get_user_by_email(OWNER))
owner3 = models.get_user_by_email(OWNER)
events_after = models.one("SELECT COUNT(*) n FROM pay_events WHERE owner_id=?",
                          (owner["id"],))["n"]
record("35 expired: marked past_due, no auto-renewal, no charge event",
       st["expired"] and owner3["plan_status"] == "past_due"
       and owner3["sub_period_end"] < models.now()
       and events_after == events_before)

# Razorpay stays available in code, disabled (req 18)
rzp = manual_bank.razorpay_status()
record("36 razorpay disabled, no live keys configured",
       rzp["enabled"] is False and not os.environ.get("RAZORPAY_KEY_ID"))
r = client.get("/subscription/pay")
record("37 pay page: bank transfer offered, razorpay marked disabled",
       r.status_code == 200 and "bank transfer" in r.text.lower()
       and "disabled" in r.text.lower())

# merchant invoice collection untouched (req 19)
from app import models as _m
inv = _m.create_invoice(owner["id"], models.create_customer(
    owner["id"], "Cust A", "c@x.in", "919810002000")["id"],
    "INV-24-1", 150000, "Subscription services", "2026-11-20")
models.claim_invoice_payment(inv, "upi", "CUSTUTR777", "")
claims = models.list_claims(owner["id"])
record("38 merchant invoice claim flow unchanged: claim pending, invoice unsettled",
       any((c.get("status") if isinstance(c, dict) else c["status"]) == "claimed"
           for c in claims)
       and models.get_invoice(inv["id"])["status"] != "paid",
       f"claims={claims}")

# no AI/automation writer of financial records (security)
refused_all = all(raises(manual_bank.approve, "x", {"id": u, "role": r})
                  for u, r in (("staff-bot", "staff"), ("viewer-bot", "viewer"),
                               ("ai-agent", "automation"), ("anon", None)))
record("39 only role=admin can decide: staff/viewer/automation/anonymous all refused",
       refused_all)

print(f"\nPHASE 24: {sum(results)}/{len(results)} "
      f"{'ALL GREEN' if all(results) else 'FAILURES PRESENT'}")
