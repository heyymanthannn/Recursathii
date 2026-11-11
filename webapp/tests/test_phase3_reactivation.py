"""Phase 3 tests: AI customer reactivation agent.

Proves: dormant detection comes from verified payment history only; segments
are rule-based; a draft contacts nobody; launching needs owner approval (Level
C); approved sends pass the permission engine (Level B); one message per
customer per campaign; opt-outs stop sends; recovered revenue is counted only
from confirmed payments made after contact, kept separate from the estimate;
and no owner can touch another owner's campaigns.
"""
import os, tempfile, time
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_phase3_", suffix=".sqlite")
from fastapi.testclient import TestClient
from app import main as app_main, models, permissions, reactivation

models.init(); reactivation.ensure_schema()
results = []
def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

OID = models.create_user("r3owner@example.com", "x", "R3 Owner", "R3 Co", "+919000000010", role="owner")["id"]
BID = models.create_user("r3other@example.com", "x", "R3 Other", "Other Co", "+919000000011", role="owner")["id"]
permissions.set_config(OID, {"automations_enabled": True})
owner = models.get_user(OID)
now = time.time()

def paid_customer(email, phone, amount, paid_days_ago, n_payments=1):
    cu = models.create_customer(OID, email.split("@")[0], email=email, phone=phone)
    for k in range(n_payments):
        inv = models.create_invoice(OID, cu["id"], f"R3-{email}-{k}", amount, "hist",
                                    time.strftime("%Y-%m-%d", time.gmtime(now - (paid_days_ago + 40) * 86400)))
        p = models.record_payment_attempt(inv, "upi", "confirmed", f"ref-{email}-{k}")
        models.update_payment(p["id"], status="confirmed", confirmed_at=now - (paid_days_ago + k) * 86400)
        models.ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?",
                  (now - (paid_days_ago + k) * 86400, inv["id"]))
    return cu

# ---- 1. dormant detection: verified history only, open invoices excluded ----
old_one = paid_customer("one@example.com", "+919000000101", 200000, 120)                    # one payment 120d ago
loyal = paid_customer("loyal@example.com", "+919000000102", 150000, 100, n_payments=3)      # 3 payments, 45k lifetime... 450000
vip = paid_customer("vip@example.com", "+919000000103", 600000, 110)                        # VIP by lifetime
recent = paid_customer("recent@example.com", "+919000000104", 300000, 5)                   # paid 5 days ago: not dormant
open_inv_cu = paid_customer("open@example.com", "+919000000105", 200000, 120)              # dormant but has an open invoice
models.create_invoice(OID, open_inv_cu["id"], "R3-OPEN-1", 50000, "m", time.strftime("%Y-%m-%d", time.gmtime(now - 86400 * 3)))

d = reactivation.dormant_customers(OID)
ids = {c["id"]: c for c in d}
check("dormant from verified history: old payers found", old_one["id"] in ids and loyal["id"] in ids and vip["id"] in ids)
check("recent payer not dormant", recent["id"] not in ids)
check("customer with open invoice goes to recovery, not reactivation", open_inv_cu["id"] not in ids)
check("segmentation rules: VIP", ids[vip["id"]]["segment"] == "VIP")
check("segmentation rules: loyal repeat", ids[loyal["id"]]["segment"] == "Loyal repeat")
check("segmentation rules: one-time", ids[old_one["id"]]["segment"] == "One-time")
check("avg order value is real math", ids[loyal["id"]]["avg_order"] == 150000, str(ids[loyal["id"]]))

# ---- 2. draft contacts nobody; launch is Level C owner approval ----
camp = reactivation.create_campaign(owner, "win back loyal", "Loyal repeat", ["email", "whatsapp"],
                                    "We miss you", "come back soon {name}", "come back {name}")
check("draft status", camp["status"] == "draft")
check("estimate is separate and labelled", camp["estimated_value"] > 0)
c1 = reactivation.run_campaign(owner, camp)
check("draft runs nothing", c1.get("reactivation_queued", 0) == 0 and "campaign_draft_not_active" in c1)
check("no messages queued from a draft", models.one("SELECT id FROM react_messages WHERE campaign_id=?", (camp["id"],)) is None)

approved = reactivation.approve_campaign(owner, camp["id"], approved_by="owner@r3")
check("approval activates", approved["status"] == "active")
check("approval audited", models.one("SELECT id FROM audit WHERE user_id=? AND action='campaign_approved'", (OID,)) is not None)

# ---- 3. approved sends pass the permission engine ----
permissions.authorize_channel(OID, "email")
c2 = reactivation.run_campaign(owner, approved)
check("email queued for segment members only", c2.get("reactivation_queued", 0) == 1,
      str(c2))   # only loyal is in the Loyal repeat segment (vip=VIP, one=One-time)
row = models.one("SELECT * FROM outbox WHERE dedupe LIKE ?", (f"react:{camp['id']}:%email",))
check("message queued on authorized channel", row is not None and row["channel"] == "email")
check("unauthorized whatsapp held for approval, not sent", c2.get("reactivation_held", 0) == 1, str(c2))
wa_row = models.one("SELECT * FROM outbox WHERE dedupe LIKE ? AND channel='whatsapp'", (f"react:{camp['id']}:%",))
check("held message waits in approval, never sent", wa_row is not None and wa_row["status"] == "approval")
permissions.authorize_channel(OID, "whatsapp")

# ---- 4. one message per customer per campaign, ever ----
permissions.authorize_channel(OID, "whatsapp")
c4 = reactivation.run_campaign(owner, approved)
check("no duplicates on re-run", c4.get("reactivation_queued", 0) == 0 and c4.get("reactivation_held", 0) == 0, str(c4))

# ---- 5. opt-out stops sends ----
cu2 = models.create_customer(OID, "optme", email="optme@example.com", phone="+919000000106")
inv2 = models.create_invoice(OID, cu2["id"], "R3-OPT", 90000, "hist", time.strftime("%Y-%m-%d", time.gmtime(now - 200 * 86400)))
p2 = models.record_payment_attempt(inv2, "upi", "confirmed", "ref-opt")
models.update_payment(p2["id"], status="confirmed", confirmed_at=now - 150 * 86400)
models.ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (now - 150 * 86400, inv2["id"]))
camp2 = reactivation.create_campaign(owner, "all dormant", "all", ["email"], "hi", "body", "")
camp2 = reactivation.approve_campaign(owner, camp2["id"], approved_by="owner")
reactivation.record_response(OID, camp2["id"], cu2["id"], "opt_out")
check("opt-out response recorded", models.set_customer_optout and models.get_customer(cu2["id"])["email_opt_out"] == 1)
permissions.set_config(OID, {"automations_enabled": True, "max_per_customer_per_day": 50})
c5 = reactivation.run_campaign(owner, camp2)
check("opted-out customer got no message", models.one(
    "SELECT id FROM outbox WHERE dedupe=? AND channel='email'", (f"react:{camp2['id']}:{cu2['id']}:email",)) is None)
others = models.q("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE ?", (f"react:{camp2['id']}:%",))
check("other dormant customers still messaged", others[0]["n"] == 3, str(others[0]["n"]))

# ---- 6. recovered revenue: verified payments only, after contact ----
summary = reactivation.campaign_summary(OID, camp2["id"])
check("summary separates estimate from actual", summary["estimated_value"] > 0 and summary["verified_recovered"] == 0)
# a payment made BEFORE contact must never count as recovered
models.update_payment(p2["id"], confirmed_at=now - 300 * 86400)   # older than any contact
# a NEW confirmed payment after contact counts
inv3 = models.create_invoice(OID, old_one["id"], "R3-NEWBUY", 45000, "new", time.strftime("%Y-%m-%d"))
p3 = models.record_payment_attempt(inv3, "upi", "confirmed", "ref-newbuy")
models.update_payment(p3["id"], status="confirmed", confirmed_at=now)
models.ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (now, inv3["id"]))
models.update_payment(p3["id"], confirmed_at=time.time())   # payment lands AFTER contact
summary = reactivation.campaign_summary(OID, camp2["id"])
check("verified recovered counts only post-contact payments", summary["verified_recovered"] == 45000,
      str(summary["verified_recovered"]))
check("old payment not double counted", summary["verified_recovered"] != 480000)

# ---- 7. cross-tenant isolation ----
check("other owner sees no campaigns", reactivation.list_campaigns(BID) == [])
check("other owner cannot approve", reactivation.approve_campaign(models.get_user(BID), camp["id"], "x") is None)
check("other owner cannot read summary", reactivation.campaign_summary(BID, camp2["id"]) is None)
bowner = models.get_user(BID)
c6 = reactivation.run_campaign(bowner, camp2)
check("other owner cannot run someone else's campaign", c6.get("reactivation_queued", 0) == 0)

# ---- 8. owner API surface ----
key = models.create_api_key(OID, "r3 key")
H = {"X-API-Key": key}
client = TestClient(app_main.app)
r = client.get("/api/reactivation/dormant", headers=H)
check("dormant endpoint works", r.status_code == 200 and r.json()["segments"].get("Loyal repeat", 0) >= 1, r.text[:200])
r2 = client.post("/api/reactivation/campaigns", headers=H, json={"name": "api camp", "segment": "VIP", "channels": ["email"]})
check("campaign create endpoint", r2.status_code == 200 and r2.json()["campaign"]["status"] == "draft", r2.text[:200])
api_camp = r2.json()["campaign"]["id"]
r3 = client.post(f"/api/reactivation/campaigns/{api_camp}/approve", headers=H)
check("approve endpoint activates", r3.status_code == 200 and r3.json()["campaign"]["status"] == "active", r3.text[:200])
r4 = client.post(f"/api/reactivation/campaigns/{api_camp}/run", headers=H)
check("run endpoint queues for VIP", r4.status_code == 200 and r4.json()["counts"].get("reactivation_queued", 0) >= 1, r4.text[:200])
r5 = client.post("/api/reactivation/response", headers=H,
                 json={"campaign_id": api_camp, "customer_id": vip["id"], "kind": "interested"})
check("response endpoint", r5.status_code == 200, r5.text[:200])
r6 = client.post("/api/reactivation/response", headers=H, json={"campaign_id": api_camp, "customer_id": vip["id"], "kind": "banana"})
check("bad response kind rejected", r6.status_code == 400)
r7 = client.get("/api/reactivation/campaigns", headers={"X-API-Key": models.create_api_key(BID, "b")})
check("other owner's list is empty", r7.status_code == 200 and r7.json()["campaigns"] == [])

print(f"\n{sum(results)}/{len(results)} passed")
import sys; sys.exit(0 if all(results) else 1)
