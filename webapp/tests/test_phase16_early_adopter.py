"""Phase 16: Early Adopter Enterprise pricing system (dev, synthetic).

Owner-approved offer (Nov 15, 2026): first 10 eligible paying Enterprise
businesses get their first 12 months at Rs 79,990 (regular annual Rs 99,990,
Rs 20,000 off). One per business; renewal after year one at the then-current
regular annual price, with advance notice and an explicit customer action.
Dev-only: no live Razorpay calls, no real customer data.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase16_early_adopter
"""
import hashlib, hmac, json, os, sqlite3, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p16_", suffix=".sqlite")
os.environ["BILLING_WEBHOOK_SECRET"] = "p16-billing-secret-0123456789abcdef"
os.environ["ADMIN_TOKEN"] = "p16-admin-token"
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ.pop("ALLOW_LIVE_PROVIDERS", None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, config
from app import early_adopter as ea
from app.webui import inr

ea.availability()  # ensure the phase16 schema exists before assertions
models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:180].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("| " + d) if not ok else "")

# ---- 1. offer pricing config ----
record("ea: config offer price Rs 79,990", config.EA_OFFER["annual_price"] == 7999000, config.EA_OFFER["annual_price"])
record("ea: config regular annual Rs 99,990", config.EA_OFFER["regular_annual_price"] == config.PLANS["enterprise"]["annual_amount"] == 9999000)
record("ea: config savings Rs 20,000 arithmetic holds",
       config.EA_OFFER["annual_price"] + config.EA_OFFER["savings"] == config.EA_OFFER["regular_annual_price"])
record("ea: config max slots 10", config.EA_OFFER["max_slots"] == 10)
record("ea: Enterprise monthly is Rs 9,999", config.PLANS["enterprise"]["amount"] == 999900)
record("ea: Starter/Growth unchanged", config.PLANS["starter"]["amount"] == 99900 and config.PLANS["growth"]["amount"] == 499900
       and config.PLANS["starter"]["annual_amount"] == 999000 and config.PLANS["growth"]["annual_amount"] == 4999000)
record("ea: inr() renders Rs 79,990 and Rs 99,990", inr(7999000) == "₹79,990" and inr(9999000) == "₹99,990",
       (inr(7999000), inr(9999000)))

# ---- 2. additive migration on a fresh database ----
try:
    models.one("SELECT 1 FROM ea_reservations LIMIT 1")
    models.one("SELECT 1 FROM subscriptions LIMIT 1")
    models.one("SELECT 1 FROM billing_events LIMIT 1")
    mig = models.one("SELECT version FROM schema_migrations WHERE version LIKE '0003%'")
    record("migration: phase16 tables created on fresh DB", mig is not None, mig)
except Exception as e:
    record("migration: phase16 tables created on fresh DB", False, e)

# ---- 3. additive migration on an EXISTING database (no data loss) ----
old_db = tempfile.mktemp(prefix="test_p16_old_", suffix=".sqlite")
cx = sqlite3.connect(old_db)
cx.executescript("""
CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT, name TEXT, business TEXT, phone TEXT,
  role TEXT, plan TEXT, plan_status TEXT, upi_vpa TEXT DEFAULT '', payee_name TEXT DEFAULT '',
  pw_hash TEXT, verify_token TEXT, verified INTEGER DEFAULT 0, api_key TEXT, api_key_hash TEXT,
  created REAL, parent_id TEXT, trial_started_at REAL, trial_ends REAL,
  billing_cycle TEXT DEFAULT 'monthly', automation_mode TEXT DEFAULT 'approval');
INSERT INTO users(id,email,name,business,phone,role,plan,plan_status,created)
  VALUES('u_existing','old@test.local','Old Biz Owner','Old Biz','919810000900','owner','scale','active',1700000000);
CREATE TABLE plat_pays(id TEXT PRIMARY KEY, owner_id TEXT, plan TEXT, amount INTEGER, status TEXT,
  reference TEXT, created REAL);
INSERT INTO plat_pays VALUES('pay_existing','u_existing','enterprise',999900,'confirmed','old-ref-1',1700000001);
""")
cx.commit(); cx.close()
_prev_db = models.DB_PATH
try:
    models.DB_PATH = old_db
    models.init()
    ea._ensure()
    u_old = models.one("SELECT * FROM users WHERE id='u_existing'")
    p_old = models.one("SELECT * FROM plat_pays WHERE id='pay_existing'")
    tables = [r["name"] for r in models.q("SELECT name FROM sqlite_master WHERE type='table'")]
    record("migration: existing DB keeps old user row intact",
           u_old is not None and u_old["plan"] == "scale" and u_old["plan_status"] == "active", u_old)
    record("migration: existing DB keeps old payment row intact",
           p_old is not None and p_old["amount"] == 999900, p_old)
    record("migration: existing DB gains subscriptions + billing_events without dropping anything",
           "subscriptions" in tables and "billing_events" in tables and "ea_reservations" in tables, tables)
finally:
    models.DB_PATH = _prev_db

# ---- 4. signup helper ----
_signup_ips = iter("10.16.%d.%d" % (i // 250, i % 250 + 2) for i in range(200))
def new_owner(email, plan="starter"):
    # each synthetic signup uses its own source address so the per-IP signup
    # rate limit (5/hour) does not fire on a batch of test accounts
    client.post("/signup", data={"name": "P " + email, "business": "P16 Biz " + email, "email": email,
                                 "phone": "919810000201", "upi_vpa": "p16@upi", "password": "pw12345678"},
                headers={"X-Forwarded-For": next(_signup_ips)}, follow_redirects=False)
    u = models.get_user_by_email(email)
    client.post("/login", data={"email": email, "password": "pw12345678"}, follow_redirects=False)
    return u

def make_paying(email, plan="starter"):
    u = new_owner(email, plan)
    models.update_user(u["id"], plan=plan, plan_status="active")
    return models.get_user_by_email(email)

def login(email):
    client.post("/login", data={"email": email, "password": "pw12345678"}, follow_redirects=False)

# ---- 5. eligibility engine: atomic 10-slot allocation ----
av = ea.availability()
record("ea: fresh availability is 10/10 available",
       av["max"] == 10 and av["remaining"] == 10 and av["available"] is True, av)

owners = []
for i in range(12):
    u = models.create_user("p16ea%02d@test.local" % i, "x" * 40, "EA Owner %d" % i,
                           "EA Biz %d" % i, "9198100009%02d" % i, role="owner")
    owners.append(u)

def try_reserve(u):
    ok, res = ea.reserve(u, checkout_ref="ref-" + u["id"][:6])
    return ok, (res["id"] if isinstance(res, dict) else str(res))

with ThreadPoolExecutor(max_workers=12) as pool:
    outcomes = list(pool.map(try_reserve, owners))
winners = [owners[i] for i, (ok, _) in enumerate(outcomes) if ok]
losers = [owners[i] for i, (ok, _) in enumerate(outcomes) if not ok]
record("ea: concurrent claims from 12 businesses grant exactly 10 slots",
       len(winners) == 10 and len(losers) == 2, [ok for ok, _ in outcomes])
av = ea.availability()
record("ea: availability after 10 grants shows 0 remaining, not available",
       av["remaining"] == 0 and av["available"] is False and av["issued"] == 0, av)
ok_l, res_l = ea.reserve(losers[0], checkout_ref="loser-retry")
record("ea: an unsuccessful claimant cannot retry while the 10 are held",
       not ok_l and res_l == "sold_out", res_l)
wid = outcomes[owners.index(winners[0])][1]
ok_w, res_w = ea.reserve(winners[0], checkout_ref="winner-retry")
record("ea: the same live reservation is idempotent (no double grant)",
       ok_w and isinstance(res_w, dict) and res_w["id"] == wid, res_w)

# ---- 6. reservation expiry frees abandoned slots ----
with models.conn() as c:
    c.execute("UPDATE ea_reservations SET expires_at=? WHERE owner_id=?", (models.now() - 1, winners[0]["id"]))
av = ea.availability()
record("ea: expired reservation releases its slot", av["remaining"] == 1, av)
ok_re, res_re = ea.reserve(winners[0], checkout_ref="re-reserve")
record("ea: the same business can re-reserve after its old reservation expired",
       ok_re and res_re["status"] == "reserved" and res_re["expires_at"] > models.now(), res_re)
with models.conn() as c:
    c.execute("UPDATE ea_reservations SET status='expired' WHERE status='reserved'")
av = ea.availability()
record("ea: abandoned checkouts free every slot (issued still 0)",
       av["remaining"] == 10 and av["issued"] == 0, av)

# ---- 7. checkout flow: server-set amounts, eligibility enforced ----
u_buy = make_paying("p16buyer@test.local")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual", "ea": "1"},
                follow_redirects=False)
pays = models.list_plat_pays(u_buy["id"])
sub = ea.get_subscription(u_buy["id"])
resv = ea.reservation(u_buy["id"])
record("checkout: early-adopter checkout records Rs 79,990 (server-set, not browser-supplied)",
       r.status_code == 303 and pays and pays[0]["amount"] == 7999000, (r.status_code, pays and pays[0]["amount"]))
record("checkout: subscription row pending_payment with price snapshot",
       sub and sub["status"] == "pending_payment" and sub["price"] == 7999000
       and sub["regular_renewal_price"] == 9999000 and sub["is_early_adopter"] == 1, sub)
record("checkout: reservation held for this checkout",
       resv and resv["status"] == "reserved" and resv["amount"] == 7999000, resv)
av = ea.availability()
record("checkout: live reservation counts against the 10", av["remaining"] == 9 and av["issued"] == 0, av)
ref = sub["payment_ref"]

# browser-supplied amount fields are ignored
u_tamper = make_paying("p16tamper@test.local")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual", "ea": "1", "amount": "1"},
                follow_redirects=False)
pays_t = models.list_plat_pays(u_tamper["id"])
record("checkout: browser-supplied amount=1 is ignored; payable amount still Rs 79,990",
       pays_t and pays_t[0]["amount"] == 7999000, pays_t and pays_t[0]["amount"])
ref_t = ea.get_subscription(u_tamper["id"])["payment_ref"]

# trialing user cannot claim the offer
u_trial = new_owner("p16trial@test.local")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual", "ea": "1"},
                follow_redirects=False)
loc = r.headers.get("location", "")
record("checkout: trialing user cannot claim the early-adopter offer",
       loc.endswith("blocked=ea_trial") and ea.reservation(u_trial["id"]) is None
       and not models.list_plat_pays(u_trial["id"]), (loc, ea.reservation(u_trial["id"])))

# regular Enterprise annual checkout amount
u_reg = make_paying("p16regular@test.local")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual"}, follow_redirects=False)
pays_r = models.list_plat_pays(u_reg["id"])
sub_r = ea.get_subscription(u_reg["id"])
record("checkout: regular Enterprise annual uses Rs 99,990 and is not flagged early-adopter",
       pays_r and pays_r[0]["amount"] == 9999000 and sub_r["is_early_adopter"] == 0, pays_r and pays_r[0]["amount"])
ref_r = sub_r["payment_ref"]
record("checkout: a regular checkout holds no early-adopter slot", ea.reservation(u_reg["id"]) is None)

# Starter/Growth unchanged
u_s = make_paying("p16starter@test.local", plan="growth")
client.post("/billing/change-plan", data={"plan": "starter", "cycle": "monthly"}, follow_redirects=False)
record("checkout: Starter monthly still Rs 999", models.list_plat_pays(u_s["id"])[0]["amount"] == 99900)
u_g = make_paying("p16growth@test.local")
client.post("/billing/change-plan", data={"plan": "growth", "cycle": "annual"}, follow_redirects=False)
record("checkout: Growth annual still Rs 49,990", models.list_plat_pays(u_g["id"])[0]["amount"] == 4999000)

# sold out: fill the remaining 9 slots, then verify a late claimant is refused
filled = 0
for w in winners[1:]:
    ok_f, _ = ea.reserve(w, checkout_ref="fill")
    filled += 1 if ok_f else 0
av = ea.availability()
record("checkout: the 10 slots are now held (10 live reservations)",
       av["remaining"] == 0 and av["available"] is False, av)
u_late = make_paying("p16late@test.local")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual", "ea": "1"},
                follow_redirects=False)
loc = r.headers.get("location", "")
record("checkout: when the 10 are gone the offer is refused with no charge created",
       loc.endswith("blocked=ea_sold_out") and not models.list_plat_pays(u_late["id"])
       and ea.get_subscription(u_late["id"]) is None, (loc, ea.reservation(u_late["id"])))

# billing page shows the offer and subscription state. The early-adopter
# button renders for businesses NOT yet on Enterprise; the buyer already is.
login("p16growth@test.local")
bpage = client.get("/billing")
record("billing page: shows the Rs 79,990 offer, slot count and subscription details",
       "79,990" in bpage.text and "slots left" in bpage.text and "Subscription details" in bpage.text
       and "99,990" in bpage.text, "")
login("p16buyer@test.local")
bpage2 = client.get("/billing")
record("billing page: an active early adopter sees their locked first-year price and renewal terms",
       "Subscription details" in bpage2.text and "79,990" in bpage2.text
       and "first-year price, locked in" in bpage2.text and "Next renewal price" in bpage2.text,
       "")

# ---- 8. payments: verification-only activation, idempotent webhooks ----
def signed_post(url, payload, secret):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(url, content=body,
                       headers={"content-type": "application/json", "X-Razorpay-Signature": sig})

def charged(owner_id, ref, amount, event_id=None):
    p = {"event": "subscription.charged",
         "payload": {"subscription": {"entity": {"id": "sub_p16_" + ref[:12], "notes": {"reference": ref}}},
                     "payment": {"entity": {"amount": amount}}}}
    if event_id:
        p["id"] = event_id
    return p

# unsigned / forged signatures are rejected
br = client.post("/webhooks/billing", content=json.dumps(charged(u_buy["id"], ref, 7999000)).encode(),
                 headers={"content-type": "application/json"})
record("webhook: unsigned request rejected with 400", br.status_code == 400, br.status_code)
bf = signed_post("/webhooks/billing", charged(u_buy["id"], ref, 7999000), "wrong-secret-0123456789abcdef")
record("webhook: forged signature rejected with 400", bf.status_code == 400, bf.status_code)
record("webhook: no activation on rejected signature",
       ea.get_subscription(u_buy["id"])["status"] == "pending_payment")

# wrong amount never activates
bw = signed_post("/webhooks/billing", charged(u_buy["id"], ref, 9999000, "evt_wrong"), "p16-billing-secret-0123456789abcdef")
record("webhook: a Rs 99,990 charge cannot activate a Rs 79,990 checkout",
       ea.get_subscription(u_buy["id"])["status"] == "pending_payment"
       and bw.json().get("platform", {}).get("reason", "").startswith("amount_mismatch"),
       bw.json().get("platform"))
record("webhook: wrong-amount attempt is audited",
       models.one("SELECT COUNT(*) AS n FROM audit WHERE user_id=? AND action='billing.webhook_rejected'", (u_buy["id"],))["n"] >= 1)
record("lifecycle: subscription stays pending_payment with no verified payment",
       ea.get_subscription(u_buy["id"])["status"] == "pending_payment")

# correct verified charge activates
ev1 = "evt_p16_first"
bok = signed_post("/webhooks/billing", charged(u_buy["id"], ref, 7999000, ev1), "p16-billing-secret-0123456789abcdef")
sub = ea.get_subscription(u_buy["id"])
resv = ea.reservation(u_buy["id"])
av = ea.availability()
record("webhook: verified Rs 79,990 charge activates the early-adopter subscription",
       sub["status"] == "active" and sub["price"] == 7999000 and sub["term_start"] and sub["term_end"],
       (sub["status"], sub["term_start"], sub["term_end"]))
record("webhook: first-year dates are 12 months apart",
       abs((sub["term_end"] - sub["term_start"]) - 365 * 86400) <= 86400 * 2,
       (sub["term_end"] - sub["term_start"]) / 86400)
record("webhook: reservation is now permanently consumed (activated)",
       resv["status"] == "activated" and resv["activated_payment"] == ref, resv)
record("webhook: availability counts 1 issued early adopter", av["issued"] == 1, av)
record("webhook: activation is audited",
       models.one("SELECT COUNT(*) AS n FROM audit WHERE user_id=? AND action='billing.activated'", (u_buy["id"],))["n"] >= 1)
record("webhook: verified payment confirms the subscription payment record",
       any(p["reference"] == ref and p["status"] == "confirmed" and p["amount"] == 7999000
           for p in models.list_plat_pays(u_buy["id"])),
       [(p["reference"], p["amount"], p["status"]) for p in models.list_plat_pays(u_buy["id"])])

# duplicate webhook is idempotent: no double activation, no term reset
ts_before = sub["term_start"]
bok2 = signed_post("/webhooks/billing", charged(u_buy["id"], ref, 7999000, ev1), "p16-billing-secret-0123456789abcdef")
record("webhook: duplicate event id is detected and ignored",
       bok2.json().get("platform", {}).get("duplicate") is True
       or bok2.json().get("platform", {}).get("reason") == "already_active_first_year",
       bok2.json().get("platform"))
record("webhook: duplicate does not reset the term start",
       ea.get_subscription(u_buy["id"])["term_start"] == ts_before)
record("webhook: duplicate does not double-consume slots", ea.availability()["issued"] == 1)

# replays with no event id (same content) settle as a benign first-year replay
bok3 = signed_post("/webhooks/billing", charged(u_buy["id"], ref, 7999000), "p16-billing-secret-0123456789abcdef")
record("webhook: content-identical replay does not activate twice",
       ea.get_subscription(u_buy["id"])["term_start"] == ts_before
       and bok3.json().get("platform", {}).get("reason") == "already_active_first_year",
       bok3.json().get("platform"))
bok4 = signed_post("/webhooks/billing", charged(u_buy["id"], ref, 7999000), "p16-billing-secret-0123456789abcdef")
record("webhook: a third identical copy is deduped by the event ledger",
       bok4.json().get("platform", {}).get("duplicate") is True
       and ea.get_subscription(u_buy["id"])["term_start"] == ts_before,
       bok4.json().get("platform"))

# no frontend route can activate: repeating a checkout only re-opens the
# pending state; nothing activates without a verified webhook event
login("p16tamper@test.local")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual", "ea": "1", "payment_screenshot": "yes"},
                follow_redirects=False)
record("security: a re-submitted checkout cannot turn itself into an activation",
       r.status_code == 303 and ea.get_subscription(u_tamper["id"])["status"] == "pending_payment"
       and ea.availability()["issued"] == 1 and ea.reservation(u_tamper["id"])["status"] == "reserved",
       (ea.get_subscription(u_tamper["id"])["status"], ea.availability()["issued"]))

# refund lifecycle on the second early adopter
bok_t = signed_post("/webhooks/billing", charged(u_tamper["id"], ref_t, 7999000, "evt_p16_tamper"), "p16-billing-secret-0123456789abcdef")
record("webhook: second early adopter activates (2 issued)",
       ea.get_subscription(u_tamper["id"])["status"] == "active" and ea.availability()["issued"] == 2,
       ea.availability())
br2 = signed_post("/webhooks/billing", {"event": "refund.pending",
                                        "payload": {"subscription": {"entity": {"id": "sub_p16_" + ref_t[:12],
                                                                             "notes": {"reference": ref_t}}}}},
                  "p16-billing-secret-0123456789abcdef")
record("lifecycle: refund.pending moves the subscription to refund_pending",
       ea.get_subscription(u_tamper["id"])["status"] == "refund_pending")
br3 = signed_post("/webhooks/billing", {"event": "refund.processed",
                                        "payload": {"subscription": {"entity": {"id": "sub_p16_" + ref_t[:12],
                                                                             "notes": {"reference": ref_t}}}}},
                  "p16-billing-secret-0123456789abcdef")
record("lifecycle: refund.processed moves it to refunded",
       ea.get_subscription(u_tamper["id"])["status"] == "refunded")
record("lifecycle: the activated offer record survives a refund (audit trail intact)",
       ea.reservation(u_tamper["id"])["status"] == "activated")

# regular annual sub activates at its own amount
br4 = signed_post("/webhooks/billing", charged(u_reg["id"], ref_r, 9999000, "evt_p16_reg"), "p16-billing-secret-0123456789abcdef")
sub_r = ea.get_subscription(u_reg["id"])
record("webhook: regular Enterprise annual activates at Rs 99,990",
       sub_r["status"] == "active" and sub_r["price"] == 9999000, (sub_r["status"], sub_r["price"]))
br5 = signed_post("/webhooks/billing", charged(u_reg["id"], ref_r, 7999000, "evt_p16_reg_low"), "p16-billing-secret-0123456789abcdef")
record("webhook: an underpayment against an active regular term is refused, term unchanged",
       br5.json().get("platform", {}).get("reason", "").startswith("amount_mismatch")
       and ea.get_subscription(u_reg["id"])["term_start"] == sub_r["term_start"],
       br5.json().get("platform"))

# ---- 9. renewal: advance notice, no silent charge, then explicit renewal ----
login("p16buyer@test.local")
with models.conn() as c:
    c.execute("UPDATE subscriptions SET term_end=?, renewal_notified_at=NULL WHERE owner_id=?",
              (models.now() + 10 * 86400, u_buy["id"]))
out1 = ea.check_expiries_and_notices()
ob = models.one("SELECT * FROM outbox WHERE dedupe LIKE 'ea-renewal-notice:%' ORDER BY created DESC LIMIT 1")
record("renewal: advance notice queued while the term end approaches",
       out1["renewal_notices_queued"] >= 1 and ob is not None, (out1, bool(ob)))
if ob:
    record("renewal: notice states the first-year price, renewal price and explicit action",
           "79,990" in ob["body"] and "99,990" in ob["body"] and "explicit" in ob["body"].lower(), ob["body"][:120])
    record("renewal: notice is marked as sent (no repeat)",
           ea.get_subscription(u_buy["id"])["renewal_notified_at"] is not None)
    before = models.one("SELECT COUNT(*) AS n FROM outbox WHERE dedupe LIKE 'ea-renewal-notice:%'")["n"]
    ea.check_expiries_and_notices()
    after = models.one("SELECT COUNT(*) AS n FROM outbox WHERE dedupe LIKE 'ea-renewal-notice:%'")["n"]
    record("renewal: notice is deduped (no second copy)", after == before, (before, after))

# term end passes -> expired, nothing charged automatically
with models.conn() as c:
    c.execute("UPDATE subscriptions SET term_end=? WHERE owner_id=?", (models.now() - 3600, u_buy["id"]))
pays_before = len(models.list_plat_pays(u_buy["id"]))
ea.check_expiries_and_notices()
sub = ea.get_subscription(u_buy["id"])
record("renewal: after the term ends the subscription expires and nothing is charged automatically",
       sub["status"] == "expired" and len(models.list_plat_pays(u_buy["id"])) == pays_before,
       (sub["status"], pays_before))

# explicit renewal at the regular annual price (switch away, then renew)
client.post("/billing/change-plan", data={"plan": "growth", "cycle": "monthly"}, follow_redirects=False)
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual"}, follow_redirects=False)
sub = ea.get_subscription(u_buy["id"])
record("renewal: explicit renewal creates a regular annual checkout at Rs 99,990",
       r.status_code == 303 and sub["status"] == "pending_payment" and sub["price"] == 9999000
       and sub["is_early_adopter"] == 0, (sub["price"], sub["is_early_adopter"]))
ref_rn = sub["payment_ref"]
br6 = signed_post("/webhooks/billing", charged(u_buy["id"], ref_rn, 9999000, "evt_p16_renew"), "p16-billing-secret-0123456789abcdef")
sub = ea.get_subscription(u_buy["id"])
record("renewal: verified renewal charge activates the regular annual term",
       sub["status"] == "active" and sub["price"] == 9999000, sub)
pays_all = models.list_plat_pays(u_buy["id"])
record("renewal: payment history preserves both the Rs 79,990 first year and the Rs 99,990 renewal",
       any(p["amount"] == 7999000 for p in pays_all) and any(p["amount"] == 9999000 for p in pays_all),
       [(p["amount"], p["status"]) for p in pays_all])
rv = ea.reservation(u_buy["id"])
record("renewal: the original early-adopter offer record is preserved (price, dates, status)",
       rv["amount"] == 7999000 and rv["status"] == "activated" and rv["first_year_start"]
       and rv["first_year_end"] and abs((rv["first_year_end"] - rv["first_year_start"]) - 365 * 86400) <= 86400 * 2,
       rv)

# cannot claim a second early-adopter offer
client.post("/billing/change-plan", data={"plan": "growth", "cycle": "monthly"}, follow_redirects=False)
models.update_user(u_buy["id"], plan_status="active")
r = client.post("/billing/change-plan", data={"plan": "enterprise", "cycle": "annual", "ea": "1"}, follow_redirects=False)
loc = r.headers.get("location", "")
record("renewal: the same business can never claim a second early-adopter offer",
       loc.endswith("blocked=ea_already"), loc)

# ---- 10. leap-year-safe term math ----
import datetime as _dt
leap = _dt.datetime(2024, 2, 29, 12, 0, tzinfo=_dt.timezone.utc).timestamp()
end = ea.add_one_year(leap)
record("lifecycle: Feb 29 term ends Feb 28 next year",
       _dt.datetime.fromtimestamp(end, _dt.timezone.utc).date() == _dt.date(2025, 2, 28),
       _dt.datetime.fromtimestamp(end, _dt.timezone.utc).date())

# ---- 11. merchant invoice collection is untouched ----
u_inv = make_paying("p16invoice@test.local")
cu = models.create_customer(u_inv["id"], "Acme Cafe")
inv = None
try:
    inv = models.create_invoice(u_inv["id"], cu["id"], "INV-P16", 250000, "Recovery retainer", "2026-12-01")
except TypeError:
    inv = None

if inv:
    record("isolation: merchant invoice created at its full amount (Rs 2,500), untouched by subscription logic",
           inv["amount"] == 250000, inv["amount"])
src = open("app/models.py", encoding="utf-8").read()
seg = src[src.index("def create_invoice"):src.index("def create_invoice") + 2200]
record("isolation: create_invoice never references subscription plan config",
       "PLANS" not in seg and "EA_OFFER" not in seg and "early_adopter" not in seg)
record("isolation: subscription tables never join invoice tables",
       "invoice" not in open("app/early_adopter.py", encoding="utf-8").read().lower())

# ---- 12. tenant isolation and admin surface ----
make_paying("p16other@test.local")
r = client.get("/billing")
record("isolation: a business's billing page never shows another business's subscription reference",
       ref not in r.text, "")
# the admin cookie carries the Secure flag whenever BASE_URL is https, so the
# admin session is exercised over an https test client like a real browser
admin_client = TestClient(app_main.app, base_url="https://testserver")
r = admin_client.get("/admin")
record("admin: without the token /admin shows only the login page (no dashboard data)",
       r.status_code == 200 and "Early Adopter" not in r.text and "Slots" not in r.text, r.status_code)
rw = admin_client.post("/admin/login", data={"token": "wrong-token"})
record("admin: a wrong admin token is refused",
       rw.status_code in (200, 303) and "Early Adopter" not in (rw.text or ""), rw.status_code)
admin_client.post("/admin/login", data={"token": "p16-admin-token"}, follow_redirects=False)
r2 = admin_client.get("/admin")
record("admin: with the token the panel shows slots, revenue split and renewal lines",
       r2.status_code == 200 and "Early Adopter" in r2.text and "slots remaining" in r2.text
       and "early-adopter revenue" in r2.text, r2.status_code)
r = client.get("/api/early-adopter-availability")
record("availability endpoint: server-side count, public and safe",
       r.status_code == 200 and r.json()["max"] == 10 and isinstance(r.json()["remaining"], int), r.text[:120])

# ---- 13. admin stats ----
st = ea.admin_stats()
ea_rev_expected = models.one("SELECT COALESCE(SUM(price),0) AS s FROM subscriptions "
                             "WHERE is_early_adopter=1 AND status IN ('active','expired','canceled')")["s"]
reg_expected = models.one("SELECT COALESCE(SUM(price),0) AS s FROM subscriptions "
                          "WHERE is_early_adopter=0 AND plan='enterprise' AND status IN ('active','expired','canceled')")["s"]
record("admin stats: issued count reflects real activations", st["availability"]["issued"] == 2, st["availability"])
record("admin stats: revenue split separates early adopter from regular Enterprise",
       st["ea_revenue"] == ea_rev_expected and st["regular_enterprise_revenue"] == reg_expected
       and st["availability"]["max"] == 10, (st["ea_revenue"], st["regular_enterprise_revenue"]))

# ---- 14. public pricing page states the real offer ----
r = client.get("/pricing")
t = r.text
need = ["999", "4,999", "9,999", "79,990", "99,990", "9,990", "49,990", "first 10", "explicit",
        "early-adopter-availability"]
missing = [n for n in need if n not in t]
record("pricing page: shows the official plans and the honest early-adopter terms",
       not missing, missing)
import re as _re
stale = [s for s in ("5,999", "6,999", "7,999", "2,499")
         if s in t or _re.search(r"(?<!\d)" + s.replace(",", "") + r"(?!\d)", t)]
record("pricing page: clean of every superseded price", not stale, stale)

# ---- summary ----
total, passed = len(results), sum(results)
print("\nPhase 16 Early Adopter: %d/%d checks passed" % (passed, total))
if passed != total:
    sys.exit(1)
