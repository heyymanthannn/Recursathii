"""Phase 9: communication-first recovery system tests.

Covers the founder-approved ten-change plan on the dev branch:
fail-closed provider-status and WhatsApp webhooks, production secret
fail-fast, header-based WhatsApp verification (no token in URLs),
validated negative sequence offsets, promised-date anchoring, day-14
owner review, inbound WhatsApp conversations with rule-based intent
(idempotent), pending-claim-only payment replies, portal more-time and
dispute requests with rate limits, approved Meta template send path,
bank details / business payment link context, WhatsApp disconnect, and
owner-guarded inbox / comm-stats / CSV import APIs.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase9_comm
Isolated temp DB, synthetic data only, no network, no real providers.
"""
import os, sys, tempfile, time, hmac, hashlib, json, threading, socket
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p9_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
os.environ["RZP_WEBHOOK_SECRET"] = "pilot-webhook-secret"
os.environ.pop("PROVIDER_STATUS_TOKEN", None)
os.environ.pop("WHATSAPP_APP_SECRET", None)
os.environ.pop("WHATSAPP_VERIFY_TOKEN", None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, automations, notify, inbound, vault, sequences
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app)
results = []

def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    print(("PASS" if ok else "FAIL"), name, ("| " + str(detail)) if not ok else "")

# ---- fixture: two owners, a customer + invoice each ----
ow1 = models.create_user("p9owner1@test.local", hash_pw("pw12345678"), "Owner One", "Biz One", "919810000001")
ow2 = models.create_user("p9owner2@test.local", hash_pw("pw12345678"), "Owner Two", "Biz Two", "919810000002")
key1 = models.create_api_key(ow1["id"], "test")
key2 = models.create_api_key(ow2["id"], "test")
cu1 = models.create_customer(ow1["id"], "Ravi Kumar", email="ravi@test.local", phone="+919812345678")
inv1 = models.create_invoice(ow1["id"], cu1["id"], "P9-INV-1", 250000, "monthly retainer", "2026-11-01")
TODAY = time.strftime("%Y-%m-%d")

H = {"X-API-Key": key1}

# ============ 1. provider-status webhook fail-closed ============
r = client.post("/webhooks/provider-status", json={"provider_ref": "wamid.X", "status": "delivered"})
record("provider-status 503 when token unconfigured (fail closed)", r.status_code == 503, r.status_code)

os.environ["PROVIDER_STATUS_TOKEN"] = "p9-provider-secret"
r = client.post("/webhooks/provider-status", json={"provider_ref": "wamid.X", "status": "delivered"})
record("provider-status 401 without token header when configured", r.status_code == 401, r.status_code)
r = client.post("/webhooks/provider-status", json={"provider_ref": "wamid.X", "status": "delivered"},
                headers={"X-Provider-Token": "wrong"})
record("provider-status 401 with wrong token", r.status_code == 401, r.status_code)
r = client.post("/webhooks/provider-status", json={"provider_ref": "wamid.X", "status": "delivered"},
                headers={"X-Provider-Token": "p9-provider-secret"})
record("provider-status 404 unknown ref with valid token", r.status_code == 404, r.status_code)
# a valid token + real ref moves delivery, never the send path
o1 = models.queue(ow1["id"], "whatsapp", "+919812345678", "overdue_1", "s", "b", dedupe="p9-prov-1")
models.ex("UPDATE outbox SET provider_ref='wamid.OK' WHERE id=?", (o1["id"],))
r = client.post("/webhooks/provider-status", json={"provider_ref": "wamid.OK", "status": "delivered"},
                headers={"X-Provider-Token": "p9-provider-secret"})
record("provider-status valid ref -> delivered", r.status_code == 200 and r.json().get("delivery_status") == "delivered", r.text)
row = models.get_outbox(o1["id"])
record("delivered set only by webhook", row["delivery_status"] == "delivered", dict(row))
del os.environ["PROVIDER_STATUS_TOKEN"]

# ============ 2. production secret fail-fast ============
try:
    vault.require_production_secrets({"RENDER": "1", "APP_SECRET": ""})
    record("require_production_secrets raises with RENDER and no APP_SECRET", False)
except RuntimeError:
    record("require_production_secrets raises with RENDER and no APP_SECRET", True)
try:
    _env = {"RENDER": "1", "APP_SECRET": "x" * 40, "BACKUP_ENC_KEY": "k" * 44,
            "BILLING_WEBHOOK_SECRET": "b" * 20, "YOUPI_WEBHOOK_SECRET": "y" * 20}
    _w = vault.require_production_secrets(_env)
    record("require_production_secrets ok with full secret set", isinstance(_w, list))
except Exception:
    record("require_production_secrets ok with full secret set", False)
record("require_production_secrets no-op outside production", vault.require_production_secrets({}) == [])

# ============ 3. WhatsApp verification: token in header, never the URL ============
captured = {}
class _FakeResp:
    status_code = 200
    def json(self): return {"name": "Biz One", "id": "123"}
def _fake_get(url, **kw):
    captured["url"], captured["kw"] = url, kw
    return _FakeResp()
orig_get = notify.httpx.Client
class _FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, **kw): return _fake_get(url, **kw)
notify.httpx.Client = _FakeClient
cn = models.save_connect(ow1["id"], wa_phone_id="123456789", wa_token_enc=vault.encrypt("wa-token-1"))
ok, err = notify.verify_retailer_whatsapp(ow1["id"])
record("verify_retailer_whatsapp succeeds", ok, err)
record("verification token in Authorization header, not URL", 
       captured["kw"].get("headers", {}).get("Authorization") == "Bearer wa-token-1"
       and "access_token" not in captured["kw"].get("params", {}), captured)
notify.httpx.Client = orig_get

# ============ 4. validated negative offsets ============
try:
    sequences.set_sequence(ow1["id"], [{"offset": -8, "channels": ["email"]}])
    record("offset -8 rejected", False)
except ValueError:
    record("offset -8 rejected", True)
try:
    sequences.set_sequence(ow1["id"], [{"offset": -3, "channels": ["email"]}, {"offset": -5, "channels": ["email"]}])
    record("decreasing offsets rejected", False)
except ValueError:
    record("decreasing offsets rejected", True)
seq = sequences.set_sequence(ow1["id"], [
    {"id": "s1", "offset": -3, "channels": ["email"]}, {"id": "s2", "offset": 0, "channels": ["email"]},
    {"id": "s3", "offset": 2, "channels": ["email"]}, {"id": "s4", "offset": 5, "channels": ["email"]},
    {"id": "s5", "offset": 7, "channels": ["email"]}])
record("negative offset -3 accepted", bool(seq) and seq[0]["offset"] == -3, seq[:1])
record("before-due default template is reminder_due", seq[0]["template_key"] == "reminder_due", seq[:1])

# ============ 5. promised-date anchoring + before-due firing ============
models.save_connect(ow1["id"], smtp_host="smtp.test.local", smtp_user="biz@test.local")
# the permission engine pauses Level-B sends until the owner activates automations
from app import permissions as _perm9
_perm9.set_config(ow1["id"], {"automations_enabled": True, "quiet_hours": {"enabled": False},
                              "authorized_channels": ["email", "whatsapp"],
                              "max_per_customer_per_day": 100, "max_per_customer_week": 500,
                              "max_per_owner_per_day": 1000, "max_per_owner_week": 5000,
                              "max_per_channel_per_day": {"email": 100, "whatsapp": 100}})
inv_fut = models.create_invoice(ow1["id"], cu1["id"], "P9-INV-FUT", 100000, "", TODAY)
models.ex("UPDATE invoices SET promised_date=? WHERE id=?", ("2026-12-10", inv_fut["id"]))
# import time module for date math
import datetime as _dt
def _iso(days_from_today):
    return (_dt.date.today() + _dt.timedelta(days=days_from_today)).isoformat()
inv_before = models.create_invoice(ow1["id"], cu1["id"], "P9-INV-BEFORE", 150000, "", _iso(3))
seq2 = sequences.set_sequence(ow1["id"], [{"id": "t1", "offset": -3, "channels": ["email"]}, {"id": "t2", "offset": 0, "channels": ["email"]}])
counts = automations.run_for_owner(ow1, "http://test")
b_rows = models.q("SELECT * FROM outbox WHERE owner_id=? AND dedupe LIKE ?",
                  (ow1["id"], f"seq:t1:{inv_before['id']}%"))
record("before-due step fires 3 days before due", len(b_rows) == 1, [dict(r) for r in b_rows])
f_rows = models.q("SELECT * FROM outbox WHERE owner_id=? AND dedupe LIKE ?",
                  (ow1["id"], f"seq:%:{inv_fut['id']}%")) + models.q(
    "SELECT * FROM outbox WHERE owner_id=? AND body LIKE '%P9-INV-FUT%'", (ow1["id"],))
record("future promised date fires nothing (paused by promise)", len(f_rows) == 0, [dict(r) for r in f_rows])
# after the promise passes, steps anchor on the promised date
# due 30 days out, promised 2 days AGO: the day-2 step can only fire because
# the promised date (not the due date) anchors the sequence
inv_prom = models.create_invoice(ow1["id"], cu1["id"], "P9-INV-PROM", 300000, "", _iso(30))
models.ex("UPDATE invoices SET promised_date=? WHERE id=?", (_iso(-2), inv_prom["id"]))
sequences.set_sequence(ow1["id"], [{"id": "u1", "offset": 2, "channels": ["email"]}])
automations.run_for_owner(ow1, "http://test")
p_any = models.q("SELECT * FROM outbox WHERE owner_id=? AND dedupe LIKE ?",
                 (ow1["id"], f"seq:u1:{inv_prom['id']}%"))
record("step anchored on promised date (day 2 of promise) fires", len(p_any) >= 1, [dict(r) for r in p_any])

# ============ 6. day-14 owner review ============
inv_stale = models.create_invoice(ow1["id"], cu1["id"], "P9-INV-STALE", 500000, "", _iso(-20))
before_audit = models.q("SELECT * FROM audit WHERE user_id=? AND action='day14_review'", (ow1["id"],))
automations._day14_owner_review(ow1)
after_audit = models.q("SELECT * FROM audit WHERE user_id=? AND action='day14_review'", (ow1["id"],))
record("day-14 review logged", len(after_audit) == len(before_audit) + 1)
acts = models.list_activities(ow1["id"], 10)
record("day-14 activity names the invoice", any("P9-INV-STALE" in a["message"] for a in acts), [a["message"] for a in acts])
automations._day14_owner_review(ow1)
after2 = models.q("SELECT * FROM audit WHERE user_id=? AND action='day14_review'", (ow1["id"],))
record("day-14 review fires once per day", len(after2) == len(after_audit))
# a fresh owner with nothing stale gets no row
before2 = models.q("SELECT * FROM audit WHERE user_id=? AND action='day14_review'", (ow2["id"],))
automations._day14_owner_review(ow2)
after3 = models.q("SELECT * FROM audit WHERE user_id=? AND action='day14_review'", (ow2["id"],))
record("day-14 silent when nothing stale", len(after3) == len(before2))

# ============ 7. inbound conversations: idempotency + isolation ============
os.environ["WHATSAPP_APP_SECRET"] = "p9-wa-app-secret"
WA_SECRET = os.environ["WHATSAPP_APP_SECRET"]
def wa_signed(payload):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(WA_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}

payload = {"entry": [{"changes": [{"value": {
    "metadata": {"phone_number_id": "123456789"},
    "messages": [{"id": "wamid.IN-1", "from": "919812345678", "type": "text",
                  "text": {"body": "ok boss, payment done yesterday"}}]}}]}]}
body, hdrs = wa_signed(payload)
r = client.post("/webhooks/whatsapp", content=body, headers=hdrs)
record("inbound webhook 200", r.status_code == 200, r.text)
record("inbound recorded 1 conversation", r.json().get("inbound", {}).get("recorded") == 1, r.text)
convs = models.list_conversations(ow1["id"])
record("conversation stored with intent paid", convs and convs[0]["intent"] == "paid", [dict(c) for c in convs])
record("conversation linked to customer", convs and convs[0]["customer_id"] == cu1["id"], [dict(c) for c in convs])
r = client.post("/webhooks/whatsapp", content=body, headers=hdrs)
record("replayed message id -> duplicate, no new row", r.json().get("inbound", {}).get("duplicates") == 1
       and len(models.list_conversations(ow1["id"])) == 1, r.text)
claims = models.q("SELECT * FROM payments WHERE invoice_id=? AND status='pending'", (convs[0]["invoice_id"],))
record("paid reply -> PENDING claim, never confirmed", len(claims) == 1 and claims[0]["status"] == "pending", [dict(c) for c in claims])
inv_row = models.get_invoice(convs[0]["invoice_id"])
record("paid reply never settles the invoice", inv_row["status"] != "paid", inv_row["status"])
# unsigned / unconfigured
del os.environ["WHATSAPP_APP_SECRET"]
r = client.post("/webhooks/whatsapp", content=body)
record("whatsapp webhook 503 when app secret unset (fail closed)", r.status_code == 503, r.status_code)
r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "42"})
record("whatsapp GET verify 403 without configured verify token", r.status_code == 403, r.status_code)
os.environ["WHATSAPP_VERIFY_TOKEN"] = "p9-verify"
r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "p9-verify", "hub.challenge": "42"})
record("whatsapp GET verify returns challenge", r.status_code == 200 and r.text == "42", r.text)
del os.environ["WHATSAPP_VERIFY_TOKEN"]
os.environ["WHATSAPP_APP_SECRET"] = WA_SECRET
# bad signature
bad = dict(hdrs); bad["X-Hub-Signature-256"] = "sha256=" + "0" * 64
r = client.post("/webhooks/whatsapp", content=body, headers=bad)
record("whatsapp webhook 401 bad signature", r.status_code == 401, r.status_code)
# unknown business number is skipped politely
payload2 = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "999999"},
    "messages": [{"id": "wamid.IN-X", "from": "919800000000", "type": "text", "text": {"body": "hi"}}]}}]}]}
body2, hdrs2 = wa_signed(payload2)
n_before = len(models.list_conversations(ow1["id"])) + len(models.list_conversations(ow2["id"]))
r = client.post("/webhooks/whatsapp", content=body2, headers=hdrs2)
n_after = len(models.list_conversations(ow1["id"])) + len(models.list_conversations(ow2["id"]))
record("unknown business number skipped, webhook ok", r.status_code == 200 and n_after == n_before, r.text)
# tenant isolation: owner2 sees nothing
record("owner2 inbox empty (isolation)", len(models.list_conversations(ow2["id"])) == 0)
r = client.get("/api/inbox", headers={"X-API-Key": key2})
record("inbox API isolation", r.json()["conversations"] == [] and r.json()["open_requests"] == [], r.text)

# ============ 8. intent classification ============
cases = [("payment done sir", "paid"), ("already paid last week", "dispute"),
         ("please give more time till month end", "more_time"),
         ("what is this invoice for", "other"), ("kal kar diya tha", "paid"),
         ("galat invoice hai, cancel karo", "dispute"),
         ("salary aane ke baad bhej dunga", "more_time")]
for text, want in cases:
    got = inbound.classify_intent(text)
    record(f"classify '{text}' -> {want}", got == want, got)

# more_time via WhatsApp pauses the case and opens a request
models.ex("DELETE FROM conversations")
payload3 = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "123456789"},
    "messages": [{"id": "wamid.IN-2", "from": "919812345678", "type": "text",
                  "text": {"body": "can you give me more time, salary comes next week"}}]}}]}]}
body3, hdrs3 = wa_signed(payload3)
r = client.post("/webhooks/whatsapp", content=body3, headers=hdrs3)
reqs = models.list_customer_requests(ow1["id"])
record("more_time reply opens a request", any(x["kind"] == "more_time" for x in reqs), [dict(x) for x in reqs])
req_more = [x for x in reqs if x["kind"] == "more_time"][0]
inv_m = models.get_invoice(req_more["invoice_id"])
record("more_time pauses reminders", inv_m.get("paused") == 1, dict(inv_m))
# dispute via WhatsApp stops reminders
inv_d = models.create_invoice(ow1["id"], cu1["id"], "P9-INV-D", 420000, "", _iso(-3))
payload4 = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "123456789"},
    "messages": [{"id": "wamid.IN-3", "from": "919812345678", "type": "text",
                  "text": {"body": "this invoice is wrong, I never ordered this"}}]}}]}]}
body4, hdrs4 = wa_signed(payload4)
r = client.post("/webhooks/whatsapp", content=body4, headers=hdrs4)
inv_d = models.get_invoice(inv_d["id"])
record("dispute reply marks invoice disputed", inv_d["status"] == "disputed", dict(inv_d))
reqs = models.list_customer_requests(ow1["id"])
record("dispute opens a dispute request", any(x["kind"] == "dispute" and x["invoice_id"] == inv_d["id"] for x in reqs))

# ============ 9. owner resolves requests ============
r = client.post(f"/api/requests/{req_more['id']}/resolve", json={"decision": "approved", "promised_date": _iso(10)}, headers=H)
record("approve more_time -> 200", r.status_code == 200, r.text)
inv_m = models.get_invoice(inv_m["id"])
record("approved more_time sets promised date + unpauses",
       inv_m["promised_date"] == _iso(10) and inv_m.get("paused") == 0, dict(inv_m))
record("approved request closed", models.get_customer_request(req_more["id"])["status"] == "approved")
# isolation: owner2 cannot resolve owner1's request
r = client.post(f"/api/requests/{req_more['id']}/resolve", json={"decision": "rejected"}, headers={"X-API-Key": key2})
record("cross-tenant resolve refused", r.status_code == 404, r.status_code)
# dispute rejected restores pending
req_dis = [x for x in models.list_customer_requests(ow1["id"]) if x["kind"] == "dispute" and x["invoice_id"] == inv_d["id"]][0]
r = client.post(f"/api/requests/{req_dis['id']}/resolve", json={"decision": "rejected"}, headers=H)
record("rejected dispute restores pending", models.get_invoice(inv_d["id"])["status"] == "pending", r.text)

# ============ 10. WhatsApp disconnect ============
r = client.post("/api/connect/whatsapp/disconnect", headers=H)
record("disconnect ok", r.status_code == 200 and r.json()["disconnected"] is True, r.text)
cn = models.get_connect(ow1["id"])
record("disconnect clears wa credentials only",
       not cn.get("wa_phone_id") and not cn.get("wa_token_enc") and not cn.get("wa_app_secret_enc")
       and cn.get("smtp_host") == "smtp.test.local", dict(cn))
au = models.q("SELECT * FROM audit WHERE user_id=? AND action='whatsapp_disconnected'", (ow1["id"],))
record("disconnect audited", len(au) >= 1)
r = client.post("/api/connect/whatsapp/disconnect", headers=H)
record("second disconnect is a polite no-op", r.json().get("disconnected") is False, r.text)

# ============ 11. approved Meta template send path ============
# the deliver pipeline is exercised with the send layer stubbed: payload shape
# and per-send refs are what this phase owns (the real protocol path is
# covered by the Phase 5.1 provider tests)
_got = []
def _fake_wa_send(phone, body, owner_id=None):
    _got.append({"kind": "text", "body": body})
    return "sent", "", "wamid.FF-%d" % len(_got)
def _fake_wa_tpl(phone, meta_name, lang, params, owner_id=None):
    _got.append({"kind": "template", "name": meta_name, "lang": lang, "params": params})
    return "sent", "", "wamid.TPL-%d" % len(_got)
_orig_wa_send, _orig_wa_tpl = notify.send_whatsapp, notify.send_whatsapp_template
notify.send_whatsapp = _fake_wa_send
notify.send_whatsapp_template = _fake_wa_tpl
models.save_connect(ow1["id"], wa_phone_id="123456789", wa_token_enc=vault.encrypt("wa-token-1"))
models.save_wa_template_map(ow1["id"], "overdue_1", "recursaathi_reminder_v1", "en")
o2 = models.queue(ow1["id"], "whatsapp", "+919812345678", "overdue_1", "s", "free text body",
                  dedupe="p9-tpl-1", params=["Ravi Kumar", "P9-INV-2", "1,500", "http://pay", "Biz One"])
st = notify.deliver(models.get_outbox(o2["id"]))
row = models.get_outbox(o2["id"])
record("mapped template send accepted", row["status"] == "sent" and row["delivery_status"] == "accepted", dict(row))
record("template payload sent to Meta shape", _got and _got[-1]["kind"] == "template"
       and _got[-1]["name"] == "recursaathi_reminder_v1", _got[-1] if _got else None)
record("template carries body params", _got and len(_got[-1]["params"]) == 5, _got[-1] if _got else None)
ref_row = models.get_outbox(o2["id"])
record("per-send provider ref recorded", ref_row["provider_ref"] == "wamid.TPL-1", dict(ref_row))
# free-form when no mapping
models.ex("DELETE FROM wa_templates WHERE owner_id=?", (ow1["id"],))
o3 = models.queue(ow1["id"], "whatsapp", "+919812345678", "overdue_1", "s", "free text", dedupe="p9-tpl-2")
notify.deliver(models.get_outbox(o3["id"]))
row = models.get_outbox(o3["id"])
record("no mapping -> free-form path used", row["status"] == "sent" and row["delivery_status"] == "accepted"
       and _got and _got[-1]["kind"] == "text", dict(row))
refs = [models.get_outbox(x)["provider_ref"] for x in (o2["id"], o3["id"])]
notify.send_whatsapp, notify.send_whatsapp_template = _orig_wa_send, _orig_wa_tpl
record("two sends, two distinct provider refs", len(set(refs)) == 2 and all(refs), refs)

# ============ 12. bank details / payment link in message context ============
models.save_connect(ow1["id"], bank_name="HDFC Bank", bank_account_note="A/c ending 4821",
                    payment_link_url="https://rzp.io/l/bizone")
ctx = automations._ctx(ow1, models.get_invoice(inv1["id"]), cu1, "http://test")
record("ctx carries bank_details", ctx["bank_details"] == "HDFC Bank, A/c ending 4821", ctx["bank_details"])
record("ctx carries business_pay_link", ctx["business_pay_link"] == "https://rzp.io/l/bizone", ctx)

# ============ 13. portal more-time / dispute + rate limits ============
tok = inv1["token"]
r = client.post(f"/invoice/{tok}/more-time", data={"note": "salary next week", "date": _iso(7)},
                follow_redirects=False)
record("portal more-time redirects", r.status_code == 303, r.status_code)
reqs = [x for x in models.list_customer_requests(ow1["id"]) if x["kind"] == "more_time" and x["invoice_id"] == inv1["id"]]
record("portal more-time created request", len(reqs) == 1)
inv1_row = models.get_invoice(inv1["id"])
record("portal more-time pauses invoice", inv1_row.get("paused") == 1, dict(inv1_row))
# rate limit: 5 more within window -> 429
for _ in range(5):
    client.post(f"/invoice/{tok}/more-time", data={"note": "x"})
r = client.post(f"/invoice/{tok}/more-time", data={"note": "x"})
record("portal more-time rate limited", r.status_code == 429, r.status_code)
# resolve via API: approve without date uses the suggested date
req_p = [x for x in models.list_customer_requests(ow1["id"])
         if x["invoice_id"] == inv1["id"] and x["status"] == "open"
         and x["note"] == "salary next week"][0]
r = client.post(f"/api/requests/{req_p['id']}/resolve", json={"decision": "approved"}, headers=H)
inv1_row = models.get_invoice(inv1["id"])
record("portal more-time approved with suggested date",
       inv1_row["promised_date"] == _iso(7) and inv1_row.get("paused") == 0, dict(inv1_row))
# dispute flow
r = client.post(f"/invoice/{tok}/dispute", data={"note": "wrong amount"}, follow_redirects=False)
record("portal dispute redirects", r.status_code == 303, r.status_code)
inv1_row = models.get_invoice(inv1["id"])
record("portal dispute marks disputed", inv1_row["status"] == "disputed", dict(inv1_row))
req_d = [x for x in models.list_customer_requests(ow1["id"]) if x["kind"] == "dispute" and x["status"] == "open"][0]
r = client.post(f"/api/requests/{req_d['id']}/resolve", json={"decision": "rejected"}, headers=H)
record("rejected dispute restores pending", models.get_invoice(inv1["id"])["status"] == "pending", r.text)
# opt-out rate limited (5 per hour per IP)
for _ in range(5):
    client.post("/portal/opt-out", data={"phone": "+919812345678"})
r = client.post("/portal/opt-out", data={"phone": "+919812345678"})
record("opt-out rate limited", r.status_code == 429, r.status_code)

# ============ 14. inbox + comm-stats + promised APIs ============
r = client.get("/api/inbox", headers=H)
record("inbox API returns conversations + requests", r.status_code == 200 and len(r.json()["conversations"]) >= 1, r.text[:200])
r = client.get("/api/comm-stats", headers=H)
d = r.json()["delivery"]
record("comm-stats delivery counts sane", d["total"] >= 2 and d["accepted_not_confirmed"] >= 2, d)
record("comm-stats activity listed", isinstance(r.json()["activity"], list) and len(r.json()["activity"]) >= 1, r.json()["activity"])
r = client.get("/api/comm-stats", headers={"X-API-Key": key2})
record("comm-stats isolation (owner2 zero)", r.json()["delivery"]["total"] == 0, r.json()["delivery"])
r = client.get("/api/invoices/promised", headers=H)
record("promised invoices listed", any(i["number"] == "P9-INV-FUT" for i in r.json()["invoices"]), r.text[:200])

# ============ 15. CSV import ============
r = client.post("/api/invoices/import-csv", headers=H, json={"rows": [
    {"customer": "New Biz", "email": "newbiz@test.local", "phone": "+919899887766",
     "number": "P9-CSV-1", "amount": "1,250", "due_date": _iso(10),
     "issue_date": _iso(0), "promised_date": ""},
    {"customer": "Bad Row", "number": "P9-CSV-2", "amount": "-5", "due_date": _iso(5)},
    {"customer": "Dup", "number": "P9-INV-1", "amount": "100", "due_date": _iso(5)},
]})
record("csv import created 1", r.json()["created"] == 1, r.text[:300])
record("csv import reported 2 row errors", len(r.json()["errors"]) == 2, r.json().get("errors"))
inv_csv = models.one("SELECT * FROM invoices WHERE number='P9-CSV-1'")
record("csv import set issue_date", inv_csv and inv_csv["issue_date"] == _iso(0), dict(inv_csv) if inv_csv else None)
cu_new = models.one("SELECT * FROM customers WHERE email='newbiz@test.local'")
record("csv import created customer", bool(cu_new))
# no auth -> 401
r = client.post("/api/invoices/import-csv", json={"rows": []})
record("csv import requires API key", r.status_code == 401, r.status_code)
r = client.post("/api/invoices/import-csv", headers=H, json={"rows": [{"x": "y"}] * 501})
record("csv import capped at 500", r.status_code == 400, r.status_code)

# ============ 16. conversations model: idempotency + fields ============
c1, dup1 = models.record_conversation(ow1["id"], customer_id=cu1["id"], body="hi",
                                      provider_message_id="wamid.U1")
c2, dup2 = models.record_conversation(ow1["id"], body="hi again", provider_message_id="wamid.U1")
record("record_conversation idempotent on provider id", dup2 is True and c2["id"] == c1["id"])
c3, dup3 = models.record_conversation(ow1["id"], body="fresh", provider_message_id="wamid.U2")
record("distinct provider ids store separately", dup3 is False and c3["id"] != c1["id"])

print()
print(f"PHASE 9 SUITE: {sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
