"""Phase 4 tests: revenue intelligence agent (read-only).

Synthetic data only, as instructed. Every assertion checks arithmetic against
figures the test itself planted - the agent must reproduce exactly those
numbers and never invent one. Forecasts must carry their estimate label.
Tenant isolation: another owner's numbers are their own.
"""
import os, tempfile, time
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_phase4_", suffix=".sqlite")
from fastapi.testclient import TestClient
from app import main as app_main, models, intelligence

models.init()
results = []
def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

OID = models.create_user("i4owner@example.com", "x", "I4 Owner", "I4 Co", "+919000000020", role="owner")["id"]
BID = models.create_user("i4other@example.com", "x", "I4 Other", "Other Co", "+919000000021", role="owner")["id"]
now = time.time()
today = time.strftime("%Y-%m-%d")

def invoice(number, customer_id, amount, due_days_ago, status=None):
    inv = models.create_invoice(OID, customer_id, number, amount, "synthetic",
                                time.strftime("%Y-%m-%d", time.gmtime(now - due_days_ago * 86400)))
    if status:
        models.ex("UPDATE invoices SET status=? WHERE id=?", (status, inv["id"]))
    return inv

cuA = models.create_customer(OID, "Asha", email="asha@x.com", phone="+919000000201")
cuB = models.create_customer(OID, "Bilal", email="bilal@x.com", phone="+919000000202")
cuC = models.create_customer(OID, "Chitra", email="chitra@x.com", phone="+919000000203")

# synthetic ground truth:
# - 3 overdue invoices: 1-day (40000), 10-day (60000), 40-day (100000) = 200000 total
# - 1 paid invoice that was once overdue (verified recovery)
# - 5 failed events: 3 insufficient_funds (retryable, 150000), 1 bank timeout (50000),
#   1 authentication declined (30000)
inv1 = invoice("I4-1", cuA["id"], 40000, 1)
inv2 = invoice("I4-2", cuB["id"], 60000, 10)
inv3 = invoice("I4-3", cuC["id"], 100000, 40)
inv_paid = invoice("I4-4", cuA["id"], 50000, 60)
p = models.record_payment_attempt(inv_paid, "upi", "confirmed", "ref-i4-4")
models.update_payment(p["id"], status="confirmed", confirmed_at=now - 86400 * 5)
models.ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (now - 86400 * 5, inv_paid["id"]))
for k, (code, amount) in enumerate([("insufficient_funds", 50000), ("insufficient_funds", 40000),
                                    ("insufficient_funds", 60000), ("bank timeout", 50000),
                                    ("authentication declined", 30000)]):
    models.record_event(OID, "failed", "razorpay", (cuA, cuB, cuC)[k % 3]["id"], amount, code)

# ---- overdue analysis: exact arithmetic, no invention ----
ov = intelligence.overdue_analysis(OID)
check("overdue total matches planted data", ov["total_overdue_amount"] == 200000, str(ov))
check("overdue count matches", ov["total_overdue_invoices"] == 3)
check("aging buckets correct", ov["aging_buckets"]["1-7 days"]["amount"] == 40000
      and ov["aging_buckets"]["8-30 days"]["amount"] == 60000
      and ov["aging_buckets"]["30+ days"]["amount"] == 100000, str(ov["aging_buckets"]))

# ---- failure patterns: real codes only ----
fpa = intelligence.failed_payment_analysis(OID)
check("failure count matches", fpa["total_failures"] == 5, str(fpa))
check("value at risk matches", fpa["value_at_risk"] == 230000)
check("retryable 72h window correct", fpa["retryable_within_72h_value"] == 200000)  # insufficient_funds x3 + bank timeout
top_codes = {t["code"]: t["count"] for t in fpa["top_decline_codes"]}
check("recurring pattern: insufficient_funds most common", top_codes.get("insufficient_funds") == 3, str(top_codes))
check("no invented codes", set(top_codes) <= {"insufficient_funds", "bank timeout", "authentication declined"})

# ---- verified recovery rate: confirmed payments only ----
rr = intelligence.verified_recovery_rate(OID)
check("verified recovery counts only confirmed payments", rr["verified_recovered"] == 1, str(rr))
check("verified recovery rate is exact", rr["verified_recovery_rate"] == round(1 / 4, 3), str(rr))
check("verified recovered value matches", rr["verified_recovered_value"] == 50000)

# ---- forecast: labelled estimate derived from verified data ----
fc = intelligence.collection_forecast(OID)
check("forecast is labelled an estimate", fc.get("estimate") is True and "ESTIMATE" in fc.get("label", ""))
check("forecast uses the verified rate", fc["recovery_rate_used"] == round(1 / 4, 3))
check("forecast arithmetic exact", fc["expected_collections"] == round(200000 * 0.25), str(fc))

# ---- next actions ranked by verified money at stake ----
acts = intelligence.next_actions(OID)
check("actions ranked", acts == sorted(acts, key=lambda a: -a["money_at_stake"]))
check("actions reference real numbers", all(a["money_at_stake"] > 0 for a in acts))

# ---- reports are generated and audited ----
daily = intelligence.daily_report(OID)
weekly = intelligence.weekly_report(OID)
check("daily report contains real figures", "200,000" in daily or "200000" in daily.replace(",", ""), daily[:80])
check("weekly report lists verified recoveries", "verified payments only" in weekly)
check("report generation audited", models.one("SELECT id FROM audit WHERE user_id=? AND action='intelligence_snapshot_generated'", (OID,)) is not None)

# ---- read-only assistant ----
a1 = intelligence.assistant_answer(OID, "how much is overdue?")
check("assistant answers overdue from records", "200,000" in a1 or "200000" in a1.replace(",", ""), a1)
a2 = intelligence.assistant_answer(OID, "what failed?")
check("assistant answers failures from records", "insufficient_funds" in a2, a2)
a3 = intelligence.assistant_answer(OID, "who won the cricket match?")
check("assistant refuses to invent", "never make up" in a3 or "only answer from your own" in a3, a3)

# ---- tenant isolation: the other owner sees none of this ----
ov2 = intelligence.overdue_analysis(BID)
fpa2 = intelligence.failed_payment_analysis(BID)
check("tenant isolation: no other owner's overdue", ov2["total_overdue_invoices"] == 0 and ov2["total_overdue_amount"] == 0)
check("tenant isolation: no other owner's failures", fpa2["total_failures"] == 0)
check("tenant isolation: no forecast without own data", intelligence.collection_forecast(BID).get("estimate") is False)
check("tenant isolation: assistant returns empty truth", "No overdue" in intelligence.assistant_answer(BID, "overdue?") or
      "no overdue" in intelligence.assistant_answer(BID, "overdue?").lower())

# ---- owner API surface ----
client = TestClient(app_main.app)
H = {"X-API-Key": models.create_api_key(OID, "i4 key")}
r = client.get("/api/intelligence/overview", headers=H)
check("overview endpoint", r.status_code == 200 and r.json()["overdue"]["total_overdue_amount"] == 200000, r.text[:150])
r2 = client.get("/api/intelligence/report?kind=weekly", headers=H)
check("weekly report endpoint", r2.status_code == 200 and "Verified recoveries" in r2.json()["report"])
r3 = client.get("/api/intelligence/report?kind=monthly", headers=H)
check("bad report kind rejected", r3.status_code == 400)
r4 = client.post("/api/intelligence/assistant", headers=H, json={"question": "recovery rate?"})
check("assistant endpoint", r4.status_code == 200 and "90 days" in r4.json()["answer"], r4.text[:150])
r5 = client.get("/api/intelligence/overview", headers={"X-API-Key": models.create_api_key(BID, "b")})
check("tenant isolation over API", r5.json()["overdue"]["total_overdue_amount"] == 0)
r6 = client.get("/api/intelligence/overview")
check("no key rejected", r6.status_code == 401)

print(f"\n{sum(results)}/{len(results)} passed")
import sys; sys.exit(0 if all(results) else 1)
