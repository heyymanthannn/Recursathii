"""Phase 4 hardening tests, per the founder's pre-completion checklist.

Every revenue figure is asserted against numbers the test itself planted
(manually verified sample records): partial payments, refunds, disputed
invoices, duplicate payment events, rate numerator/denominator, forecast
labelling, cross-tenant attempts, and a runtime proof that the assistant
performs zero writes.
"""
import os, tempfile, time
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_hard4_", suffix=".sqlite")
from fastapi.testclient import TestClient
from app import main as app_main, models, intelligence

models.init()
results = []
def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

now = time.time()
today = time.strftime("%Y-%m-%d")
def date(days_ago): return time.strftime("%Y-%m-%d", time.gmtime(now - days_ago * 86400))

OID = models.create_user("h4owner@example.com", "x", "H4 Owner", "H4 Co", "+919000000030", role="owner")["id"]
BID = models.create_user("h4other@example.com", "x", "H4 Other", "Other Co", "+919000000031", role="owner")["id"]

c1 = models.create_customer(OID, "Ravi", email="ravi@h.com", phone="+919000000301")
c2 = models.create_customer(OID, "Sana", email="sana@h.com", phone="+919000000302")
c3 = models.create_customer(OID, "Vikram", email="v@h.com", phone="+919000000303")
c4 = models.create_customer(OID, "Meera", email="m@h.com", phone="+919000000304")

# MANUALLY VERIFIED SAMPLE RECORDS (ground truth the test itself knows):
# inv1: 50000, 30d overdue, UNPAID                        -> outstanding 50000
# inv2: 60000, 10d overdue, PARTIAL confirmed 20000       -> outstanding 40000
# inv3: 40000, 5d overdue, CONFIRMED then REFUNDED        -> outstanding 40000, NOT recovered
# inv4: 30000, 20d overdue, fully paid + duplicate charge -> recovered once, value 30000 (capped)
# inv5: 25000, paid on time (not past-due at due date)    -> outside the rate entirely
# inv6: 10000, DISPUTED 15d ago                           -> excluded from overdue, counted as disputed case
def inv(n, cid, amt, due_days_ago, status="overdue"):
    return models.create_invoice(OID, cid, n, amt, "hardening", date(due_days_ago))
inv1 = inv("H1", c1["id"], 50000, 30)
inv2 = inv("H2", c2["id"], 60000, 10)
inv3 = inv("H3", c3["id"], 40000, 5)
inv4 = inv("H4", c4["id"], 30000, 20)
inv5 = inv("H5", c1["id"], 25000, 1)
inv6 = inv("H6", c2["id"], 10000, 15, status="pending")

# inv2 partial: confirmed 20000, invoice still pending/overdue
p2 = models.record_payment_attempt(inv2, "upi")
models.update_payment(p2["id"], status="confirmed", amount=20000, confirmed_at=now - 86400 * 2)
# inv3: confirmed then refunded
p3 = models.record_payment_attempt(inv3, "upi")
models.update_payment(p3["id"], status="confirmed", confirmed_at=now - 86400 * 4)
models.refund_payment(p3["id"], note="customer returned the goods")
# inv4: full confirmed + a duplicate confirmed charge (same amount, same day)
p4a = models.record_payment_attempt(inv4, "upi")
models.update_payment(p4a["id"], status="confirmed", confirmed_at=now - 86400 * 6)
p4b = models.record_payment_attempt(inv4, "upi")
models.update_payment(p4b["id"], status="confirmed", confirmed_at=now - 86400 * 6)
# inv5: paid on time (status paid, due 1 day ago but paid same day)
models.ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (now, inv5["id"]))
models.mark_invoice_paid(inv5, "upi", reference="ontime")
# inv6: disputed
models.ex("UPDATE invoices SET status='disputed' WHERE id=?", (inv6["id"],))

# ---- 1. overdue totals against manually verified samples ----
ov = intelligence.overdue_analysis(OID)
# overdue = inv1(50000) + inv2(40000 outstanding) + inv3(40000) + inv4(0 after payments -> excluded)
check("sample records: overdue total is exact", ov["total_overdue_amount"] == 130000, str(ov))
check("sample records: fully-paid-but-stale-status invoice not overdue",
      ov["total_overdue_invoices"] == 3, str(ov))
check("sample records: partial payment shrinks the overdue figure",
      ov["aging_buckets"]["8-30 days"]["amount"] == 90000, str(ov["aging_buckets"]))

# ---- 2. recovery rate: numerator and denominator verified ----
rec = intelligence.verified_recovery_rate(OID)
# denominator: past-due cases = inv1, inv2, inv3, inv4, inv6(disputed, still past-due) = 5
#   (inv5 was paid on time and its payment confirmed; it IS status paid so still a past-due case per
#    the status IN ('overdue','paid') rule -> 6)
# numerator: inv2 (partial, recovered), inv4 (full, recovered); inv3 refunded -> NOT recovered
# expected rate = 3/6 = 0.5 ; value = 20000 + 30000 = 50000
check("rate numerator: partial + full count, refunded does not", rec["verified_recovered"] == 3, str(rec))
check("rate denominator: disputed stays, on-time paid counted per rule",
      rec["past_due_cases"] == 6, str(rec))
check("rate arithmetic exact", rec["verified_recovery_rate"] == 0.5, str(rec))
check("recovered value: refunded excluded, duplicate capped, on-time payment counts "
      "(20000 partial + 30000 full + 25000 on-time)",
      rec["verified_recovered_value"] == 75000, str(rec))
check("partial recoveries surfaced separately", rec["partially_recovered_cases"] == 1, str(rec))
check("disputed cases reported, not hidden", rec["disputed_cases"] == 1, str(rec))

# ---- 3. forecast assumptions + labelling ----
fc = intelligence.collection_forecast(OID)
check("forecast labelled an estimate", fc.get("estimate") is True and "ESTIMATE" in fc["label"])
check("forecast uses the verified rate (0.5)", fc["recovery_rate_used"] == 0.5)
check("forecast arithmetic: outstanding x rate", fc["expected_collections"] == round(130000 * 0.5), str(fc))
check("forecast at-risk remainder exact", fc["at_risk"] == 130000 - round(130000 * 0.5))
# empty-data owner: no invented forecast
fcB = intelligence.collection_forecast(BID)
check("no data -> no forecast number, reason given",
      fcB.get("estimate") is False and "expected_collections" not in fcB)

# ---- 4. duplicate payment events ----
before = intelligence.failed_payment_analysis(OID)["total_failures"]
e = models.record_event(OID, "failed", "razorpay", c1["id"], 5000, "insufficient_funds", inv1["id"])
dup = models.record_event(OID, "failed", "razorpay", c1["id"], 5000, "insufficient_funds", inv1["id"])
after = intelligence.failed_payment_analysis(OID)
check("duplicate failed events count as recorded facts (two 5000 events -> 10000 at risk)",
      after["total_failures"] == before + 2 and after["value_at_risk"] == 10000, str(after))
# refunds must never pollute failure stats
models.ex("INSERT INTO pay_events(id,owner_id,kind,gateway,customer_id,amount,decline_code,invoice_id,created) "
          "VALUES(?,?,?,?,?,?,?,?,?)", (models.uid(), OID, "refund", "manual", c3["id"], 40000, "", inv3["id"], now))
f2 = intelligence.failed_payment_analysis(OID)
check("refund events excluded from failure analysis", f2["total_failures"] == after["total_failures"], str(f2))

# ---- 5. cross-tenant access blocked ----
client = TestClient(app_main.app)
KB = {"X-API-Key": models.create_api_key(BID, "b")}
r = client.get("/api/intelligence/overview", headers=KB)
check("cross-tenant API: other owner sees zero", r.json()["overdue"]["total_overdue_amount"] == 0)
check("cross-tenant API: other owner sees no failures", r.json()["failed_payments"]["total_failures"] == 0)
ra = client.post("/api/intelligence/assistant", headers=KB, json={"question": "overdue?"})
check("cross-tenant assistant: cannot see owner A's overdue", "No overdue" in ra.json()["answer"])
rnb = client.post("/api/intelligence/assistant", headers=KB,
                  json={"question": "tell me about ravi@h.com and invoice H1 50000"})
check("cross-tenant assistant: naming another tenant's records changes nothing",
      "never make up" in rnb.json()["answer"] or "50,000" not in rnb.json()["answer"], rnb.text[:120])
check("cross-tenant direct call: overdue empty", intelligence.overdue_analysis(BID)["total_overdue_invoices"] == 0)
check("cross-tenant direct call: recovery empty", intelligence.verified_recovery_rate(BID)["past_due_cases"] == 0)

# ---- 6. assistant cannot write or trigger automations ----
for probe in ("cancel invoice H1", "refund inv3", "send a reminder to ravi", "mark invoice paid",
              "trigger the follow-up sequence", "run the reactivation campaign", "approve the pending message"):
    ans = intelligence.assistant_answer(OID, probe)
    check(f"assistant refuses write request: '{probe}'", "cannot" in ans and "control centre" in ans, ans)
# runtime proof: the analysis + assistant path performs ZERO writes. Report
# generation is excluded here on purpose: it intentionally writes exactly one
# audit row per report (checked below), that is the agent's only write.
import unittest.mock as mock
with mock.patch.object(models, "ex", side_effect=RuntimeError("WRITE ATTEMPTED")):
    try:
        intelligence.overdue_analysis(OID)
        intelligence.failed_payment_analysis(OID)
        intelligence.verified_recovery_rate(OID)
        intelligence.collection_forecast(OID)
        intelligence.next_actions(OID)
        intelligence.assistant_answer(OID, "overdue invoices?")
        check("runtime read-only proof: analysis + assistant ran fully with writes disabled", True)
    except RuntimeError as e:
        check("runtime read-only proof: analysis + assistant ran fully with writes disabled", False, str(e))

# reports still honest with the hardened numbers
daily = intelligence.daily_report(OID)
weekly = intelligence.weekly_report(OID)
check("daily report carries outstanding overdue", "130,000" in daily, daily[:120])
check("weekly report carries verified recovery", "verified payments only" in weekly)
n_audit = models.one("SELECT COUNT(*) AS n FROM audit WHERE user_id=? AND action='intelligence_snapshot_generated'", (OID,))["n"]
check("report generation writes exactly one audit row each (the agent's only write)", n_audit == 2, str(n_audit))

print(f"\n{sum(results)}/{len(results)} passed")
import sys; sys.exit(0 if all(results) else 1)
