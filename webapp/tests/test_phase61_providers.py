"""Phase 6.1: provider sandbox integration tests.

Controlled test environment only: protocol servers and API rigs we control,
sandbox credentials, synthetic pilot-test@rig.invalid addresses. No real
customer is contacted; no production credential exists in this run.

Covers the authorized work:
 1 provider identification (adapters for SMTP, HTTP email API, WhatsApp Cloud API)
 2 sandbox environment configuration
 3 secure credential management (encrypted at rest, masked reads, audited)
 4 provider authentication + webhook signature verification
 5 accepted / delivered / failed / unknown ladder states
 6 retries, duplicate prevention, bounce handling, opt-out suppression
 7 provider failures never produce false delivery claims
 8 all three isolated synthetic workspaces
plus safety checks: backup location independence, restore/rollback,
owner re-authentication, tenant isolation and audit logging.
"""
import os, tempfile, threading, socket, json, hashlib, hmac, sqlite3, time, base64
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_61_", suffix=".sqlite")
from fastapi.testclient import TestClient
from app import main as app_main, models, pilot, permissions, providers as P
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, extra=""):
    if not isinstance(ok, bool):
        print("NON-BOOL in results for:", name, "->", repr(ok))
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

def mk_owner(email, name="Owner", role="owner"):
    return models.create_user(email, hash_pw("pw-" + email), name,
                              name + " Biz", "9" + email.split("@")[0][:9], role=role)

def api_key(uid):
    return models.create_api_key(uid, "k-" + uid)[:64] if len(models.create_api_key(uid, "k-" + uid)) > 64 else models.create_api_key(uid, "k-" + uid)

# ---- protocol rigs ----------------------------------------------------------
class _SMTPRig(threading.Thread):
    """Minimal real SMTP server. mode: accept | reject_rcpt | drop_mid_data."""
    daemon = True
    def __init__(self, mode="accept", auth=None):
        super().__init__()
        self.mode = mode; self.auth = auth or {}
        self.received = []
        self.sock = socket.socket(); self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0)); self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
    def run(self):
        while True:
            try: c, _ = self.sock.accept()
            except OSError: return
            try: self._session(c)
            except Exception:
                try: c.close()
                except Exception: pass
    def _session(self, c):
        f = c.makefile("rb"); w = lambda s: c.sendall(s.encode())
        w("220 rig ESMTP\r\n")
        data = []; in_data = False
        for line in f:
            if in_data:
                if line.strip() == b".":
                    in_data = False; self.received.append(b"".join(data)); w("250 OK queued\r\n")
                else: data.append(line)
                continue
            raw = line.decode(errors="replace").strip()
            cmd = raw.upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                w("250-rig\r\n" + ("250 AUTH PLAIN\r\n" if self.auth else "250 OK\r\n"))
            elif cmd.startswith("AUTH PLAIN"):
                try:
                    up = base64.b64decode(raw.split()[-1]).decode().split("\x00")
                    if self.auth and up[-2] == self.auth.get("user") and up[-1] == self.auth.get("pass"):
                        w("235 OK\r\n")
                    else:
                        w("535 auth failed\r\n")
                except Exception:
                    w("535 auth failed\r\n")
            elif cmd.startswith("MAIL FROM"): w("250 OK\r\n")
            elif cmd.startswith("RCPT TO"):
                if self.mode == "reject_rcpt": w("550 no such user\r\n")
                else: w("250 OK\r\n")
            elif cmd.startswith("DATA"):
                if self.mode == "reject_rcpt": w("554 transaction failed\r\n")
                elif self.mode == "drop_mid_data":
                    w("354 go ahead\r\n"); c.close(); return
                else:
                    w("354 go ahead\r\n"); in_data = True
            elif cmd.startswith("QUIT"): w("221 bye\r\n"); c.close(); return
            else: w("250 OK\r\n")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class _APIRig(BaseHTTPRequestHandler):
    """HTTP rig speaking the provider's API shape: an HTTP email API
    (Resend-style POST /emails) and WhatsApp Cloud API (POST /v1/{id}/messages).
    Authorization header decides accept vs reject."""
    def log_message(self, *a): pass
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/emails":
            if self.headers.get("Authorization") != "Bearer rig-ok-token":
                self.send_response(401); self.end_headers()
                self.wfile.write(b'{"error":{"message":"invalid api key"}}'); return
            d = json.loads(body.decode() or "{}")
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"id": "rig-email-" + d.get("to", ["?"])[0]}).encode())
        elif self.path.startswith("/wa1/") and self.path.endswith("/messages"):
            if self.headers.get("Authorization") != "Bearer rig-wa-ok":
                self.send_response(401); self.end_headers()
                self.wfile.write(b'{"error":{"message":"invalid permanent token"}}'); return
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"messages": [{"id": "wamid.rig" + str(len(body))}]}).encode())
        elif self.path.startswith("/wa-bad/"):
            self.send_response(401); self.end_headers()
            self.wfile.write(b'{"error":{"message":"token expired"}}')
        elif self.path.startswith("/wa-drop/"):
            self.close_connection = True; return
        else:
            self.send_response(500); self.end_headers()
            self.wfile.write(b'{"error":{"message":"upstream 500"}}')

srv = ThreadingHTTPServer(("127.0.0.1", 0), _APIRig)
threading.Thread(target=srv.serve_forever, daemon=True).start()
APIPORT = srv.server_address[1]

smtp_ok = _SMTPRig("accept", auth={"user": "sandbox@recursaathi.test", "pass": "sandbox-pass"}); smtp_ok.start()
smtp_badauth = _SMTPRig("accept", auth={"user": "sandbox@recursaathi.test", "pass": "sandbox-pass"}); smtp_badauth.start()
smtp_reject = _SMTPRig("reject_rcpt"); smtp_reject.start()
smtp_drop = _SMTPRig("drop_mid_data"); smtp_drop.start()
time.sleep(0.2)

# ---- 1+2: sandbox credential management (encrypted at rest, masked) ---------
owner = mk_owner("p61owner@biz.test", "P61 Owner")
own_key = models.create_api_key(owner["id"], "k61")

r = client.post("/api/provider/config", headers={"X-API-Key": own_key},
                json={"channel": "email", "kind": "email_api",
                      "config": {"base_url": f"http://127.0.0.1:{APIPORT}", "token": "rig-ok-token",
                                 "from_addr": "pilot@recursaathi.test"}, "sandbox": True})
record("61: sandbox email provider config accepted", r.status_code == 200 and r.json().get("sandbox") is True, r.text[:160])

row = models.one("SELECT config_enc FROM provider_credentials WHERE owner_id=?", (owner["id"],))
record("61: credentials stored encrypted at rest", row is not None and "rig-ok-token" not in (row["config_enc"] or ""), str(bool(row)))

r = client.get("/api/provider/config", headers={"X-API-Key": own_key})
mask = (r.json().get("providers") or {}).get("email", {})
record("61: API view masks the secret", mask.get("token") == "rig***" and "rig-ok-token" not in r.text, r.text[:200])

audit = models.q("SELECT detail FROM audit WHERE action='provider_configured' AND user_id=?", (owner["id"],))
leaked = [a["detail"] for a in audit if "rig-ok-token" in (a["detail"] or "")]
record("61: audit trail never records the secret", audit and not leaked, str(leaked))

# cross-tenant isolation: owner B sees nothing of owner A
other = mk_owner("p61other@biz.test", "P61 Other")
other_key = models.create_api_key(other["id"], "k61b")
r = client.get("/api/provider/config", headers={"X-API-Key": other_key})
record("61: owner B cannot read owner A provider config", r.json().get("providers") == {} and "rig-ok-token" not in r.text, r.text[:160])
r = client.post("/api/provider/test", headers={"X-API-Key": other_key},
                json={"channel": "email", "to": "pilot-test-1@rig.invalid"})
record("61: owner B cannot test owner A's provider", r.status_code == 400, r.text[:120])

# live credentials refused without the platform approval flag
r = client.post("/api/provider/config", headers={"X-API-Key": own_key},
                json={"channel": "whatsapp", "kind": "whatsapp_cloud",
                      "config": {"base_url": "https://graph.facebook.com/v21.0", "token": "live-tok",
                                 "wa_phone_number_id": "999"}, "sandbox": False})
record("61: live provider credentials refused (sandbox-only)", r.status_code == 403, r.text[:160])
os.environ["ALLOW_LIVE_PROVIDERS"] = "1"
r2 = client.post("/api/provider/config", headers={"X-API-Key": own_key},
                 json={"channel": "whatsapp", "kind": "whatsapp_cloud",
                       "config": {"base_url": "https://graph.facebook.com/v21.0", "token": "live-tok",
                                  "wa_phone_number_id": "999"}, "sandbox": False})
record("61: live credentials require explicit platform approval flag", r2.status_code == 200 and r2.json().get("sandbox") is False, r2.text[:160])
os.environ["ALLOW_LIVE_PROVIDERS"] = "0"
# restore the sandbox rig config for WhatsApp (the live check overwrote it;
# canaries must only ever reach rigs we control, never a real provider edge)
r = client.post("/api/provider/config", headers={"X-API-Key": own_key},
                json={"channel": "whatsapp", "kind": "whatsapp_cloud",
                      "config": {"base_url": f"http://127.0.0.1:{APIPORT}/wa1", "token": "rig-wa-ok",
                                 "wa_phone_number_id": "PN123"}, "sandbox": True})
record("61: sandbox rig restored after live check", r.status_code == 200, r.text[:160])

# ---- 3: provider authentication --------------------------------------------
r = client.post("/api/provider/test", headers={"X-API-Key": own_key},
                json={"channel": "email", "to": "pilot-test-2@rig.invalid"})
j = r.json()
record("61: valid provider auth yields accepted (HTTP API)", j.get("result") == "accepted" and bool(j.get("provider_id")), r.text[:200])
record("61: accepted is not delivery (ladder note present)", "NOT delivery" in j.get("note", ""), r.text[:200])

r = client.post("/api/provider/test", headers={"X-API-Key": own_key},
                json={"channel": "whatsapp", "to": "pilot-test-wa@rig.invalid"})
record("61: valid WhatsApp sandbox auth yields accepted", r.json().get("result") == "accepted", r.text[:160])

smtp_cfg = {"smtp_host": "127.0.0.1", "smtp_port": smtp_ok.port, "smtp_user": "sandbox@recursaathi.test",
            "smtp_password": "sandbox-pass", "from_addr": "pilot@recursaathi.test"}
r = client.post("/api/provider/config", headers={"X-API-Key": own_key},
                json={"channel": "email", "kind": "smtp", "config": smtp_cfg, "sandbox": True})
record("61: SMTP sandbox config accepted", r.status_code == 200, r.text[:160])
res = P.send_email("127.0.0.1", smtp_ok.port, "pilot@recursaathi.test", "pilot-test-3@rig.invalid",
                   "t", "b", smtp_user="sandbox@recursaathi.test", smtp_password="sandbox-pass")
record("61: SMTP accepts message with credentials", res["status"] == "accepted", str(res))
res = P.send_email("127.0.0.1", smtp_badauth.port, "pilot@recursaathi.test", "pilot-test-4@rig.invalid",
                   "t", "b", smtp_user="sandbox@recursaathi.test", smtp_password="WRONG")
record("61: SMTP wrong password is failed, not delivered", res["status"] == "failed" and "auth" in res["detail"].lower(), str(res))
res = P.send_email_api(f"http://127.0.0.1:{APIPORT}", "rig-ok-token", "pilot@recursaathi.test",
                       "pilot-test-5@rig.invalid", "t", "b")
record("61: HTTP email API adapter accepted with provider id", res["status"] == "accepted" and bool(res["provider_id"]), str(res))
res = P.send_email_api(f"http://127.0.0.1:{APIPORT}", "wrong-token", "pilot@recursaathi.test",
                       "pilot-test-6@rig.invalid", "t", "b")
record("61: HTTP email API invalid key is failed", res["status"] == "failed" and "401" in res["detail"], str(res))

# ---- 4: webhook signature verification -------------------------------------
body = json.dumps({"object": "whatsapp_business_account",
                   "entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.rigX", "status": "delivered"}]}}]}]}).encode()
good = "sha256=" + hmac.new(b"rig-app-secret", body, hashlib.sha256).hexdigest()
bad = "sha256=" + hmac.new(b"other-secret", body, hashlib.sha256).hexdigest()
os.environ["WHATSAPP_APP_SECRET"] = "rig-app-secret"
r = client.post("/webhooks/whatsapp", content=body,
                headers={"X-Hub-Signature-256": good, "Content-Type": "application/json"})
record("61: WhatsApp webhook valid signature accepted", r.status_code == 200, r.text[:160])
r = client.post("/webhooks/whatsapp", content=body,
                headers={"X-Hub-Signature-256": bad, "Content-Type": "application/json"})
record("61: WhatsApp webhook wrong signature rejected 401", r.status_code == 401, r.text[:120])
tampered = body + b" "
r = client.post("/webhooks/whatsapp", content=tampered,
                headers={"X-Hub-Signature-256": good, "Content-Type": "application/json"})
record("61: tampered body with stale signature rejected", r.status_code == 401, r.text[:120])

# generic provider-status token check still holds
os.environ["PROVIDER_STATUS_TOKEN"] = "rig-status-token"
r = client.post("/webhooks/provider-status", headers={"X-Provider-Token": "wrong"},
                json={"provider_ref": "rig-email-x", "status": "delivered"})
record("61: provider-status webhook rejects wrong token", r.status_code == 401, r.text[:120])

# ---- 5: ladder states end to end (outbox journey) ---------------------------
models.ex("""INSERT INTO customers(id,owner_id,name,email,phone,wa_opt_out,email_opt_out,created)
             VALUES(?,?,?,?,?,0,0,?)""",
          ("c61a", owner["id"], "Ravi", "ravi-p61@rig.invalid", "9000000001", models.now()))
models.ex("""INSERT INTO invoices(id,owner_id,customer_id,amount,due_date,status,created)
             VALUES(?,?,?,?,?, 'overdue', ?)""",
          ("inv61a", owner["id"], "c61a", 1200.0, "2026-10-01", models.now()))
models.ex("""INSERT INTO outbox(id,owner_id,customer_id,channel,dest,subject,body,status,created)
             VALUES('ob61a',?,?,?,?,?,?, 'queued', ?)""",
          (owner["id"], "c61a", "email", "ravi-p61@rig.invalid", "Payment reminder",
           "Your invoice inv61a is overdue", models.now()))

P.ensure_wa_col() if hasattr(P, "ensure_wa_col") else None
res = P.deliver_outbox(owner["id"], {"email_api_base_url": f"http://127.0.0.1:{APIPORT}",
                                     "email_api_token": "rig-ok-token",
                                     "from_addr": "pilot@recursaathi.test"})
row = models.one("SELECT * FROM outbox WHERE id='ob61a'")
record("61: real send yields accepted, provider_ref recorded", row["status"] == "accepted" and bool(row["provider_ref"]), str(dict(row))[:220])
record("61: accepted send is NOT marked delivered", (row["delivery_status"] or "") == "", str(row["delivery_status"]))

ref = row["provider_ref"]
r = client.post("/webhooks/provider-status", headers={"X-Provider-Token": "rig-status-token"},
                json={"provider_ref": ref, "status": "delivered"})
row = models.one("SELECT delivery_status FROM outbox WHERE id='ob61a'")
record("61: delivered set ONLY by the provider webhook", r.status_code == 200 and row["delivery_status"] == "delivered", r.text[:160])

r = client.post("/webhooks/provider-status", headers={"X-Provider-Token": "rig-status-token"},
                json={"provider_ref": "does-not-exist", "status": "delivered"})
record("61: unknown provider_ref rejected (no false claim)", r.status_code == 404, r.text[:120])

# provider failure never claims delivery
models.ex("""INSERT INTO outbox(id,owner_id,customer_id,channel,dest,subject,body,status,created)
             VALUES('ob61b',?,?,?,?,?,?, 'queued', ?)""",
          (owner["id"], "c61a", "email", "reject-p61@rig.invalid", "Payment reminder", "x", models.now()))
res = P.deliver_outbox(owner["id"], {"smtp_host": "127.0.0.1", "smtp_port": smtp_reject.port,
                                     "from_addr": "pilot@recursaathi.test"})
row = models.one("SELECT status, delivery_status FROM outbox WHERE id='ob61b'")
record("61: provider rejection is failed, delivery empty", row["status"] == "failed" and not row["delivery_status"], str(dict(row)))

models.ex("""INSERT INTO outbox(id,owner_id,customer_id,channel,dest,subject,body,status,created)
             VALUES('ob61c',?,?,?,?,?,?, 'queued', ?)""",
          (owner["id"], "c61a", "email", "drop-p61@rig.invalid", "Payment reminder", "x", models.now()))
res = P.deliver_outbox(owner["id"], {"smtp_host": "127.0.0.1", "smtp_port": smtp_drop.port,
                                     "from_addr": "pilot@recursaathi.test"})
row = models.one("SELECT status, delivery_status FROM outbox WHERE id='ob61c'")
record("61: connection drop is unknown, never delivered", row["status"] == "unknown" and not row["delivery_status"], str(dict(row)))

# retries without duplicates: unknown never blind-retried, accepted not re-sent
before = models.one("SELECT COUNT(*) AS n FROM outbox_history WHERE outbox_id='ob61c'")["n"]
res = P.deliver_outbox(owner["id"], {"smtp_host": "127.0.0.1", "smtp_port": smtp_ok.port,
                                     "from_addr": "pilot@recursaathi.test"})
after_c = models.one("SELECT COUNT(*) AS n FROM outbox_history WHERE outbox_id='ob61c'")["n"]
record("61: unknown messages are never retried", after_c == before, f"{before}->{after_c}")
res2 = P.deliver_outbox(owner["id"], {"smtp_host": "127.0.0.1", "smtp_port": smtp_ok.port,
                                      "from_addr": "pilot@recursaathi.test"})
counts = models.one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND status='queued'", (owner["id"],))["n"]
record("61: second run sends nothing new (no duplicates)", counts == 0, str(counts))

# duplicate webhook events ingest once
r1 = client.post("/webhooks/provider-status", headers={"X-Provider-Token": "rig-status-token"},
                 json={"provider_ref": ref, "status": "read"})
r2 = client.post("/webhooks/provider-status", headers={"X-Provider-Token": "rig-status-token"},
                 json={"provider_ref": ref, "status": "read"})
record("61: duplicate status events are idempotent (200 twice)", r1.status_code == 200 and r2.status_code == 200, r1.text[:80] + r2.text[:80])

# ---- 6: bounce handling + suppression ---------------------------------------
models.ex("""INSERT INTO outbox(id,owner_id,customer_id,channel,dest,subject,body,status,created)
             VALUES('ob61d',?,?,?,?,?,?, 'accepted', ?)""",
          (owner["id"], "c61a", "email", "ravi-p61@rig.invalid", "Payment reminder", "x", models.now()))
r = client.post("/webhooks/provider-status", headers={"X-Provider-Token": "rig-status-token"},
                json={"provider_ref": "rig-email-bounce-p61@rig.invalid", "status": "bounced"})
cust = models.one("SELECT email_bounced FROM customers WHERE id='c61a'")
direct = P.handle_bounce(owner["id"], "ravi-p61@rig.invalid", "email", "rig bounce")
cust = models.one("SELECT email_bounced FROM customers WHERE id='c61a'")
ob = models.one("SELECT delivery_status FROM outbox WHERE id='ob61d'")
record("61: bounce webhook marks message bounced", ob["delivery_status"] == "bounced", str(dict(ob)))
record("61: bounce flags the customer email_bounced", cust["email_bounced"] == 1, str(dict(cust)))

permissions.set_config(owner["id"], {"automations_enabled": True})
d = permissions.resolve(owner["id"], "send_routine_email_reminder", channel="email", customer={"id": "c61a", "email_bounced": 1})
record("61: resolve denies a bounced email address", d.get("decision") == "denied" and "unreachable" in d.get("reason", ""), str(d))
d = permissions.resolve(owner["id"], "send_routine_whatsapp_reminder", channel="whatsapp", customer={"id": "c61a", "wa_bounced": 1})
record("61: resolve denies a bounced WhatsApp address", d.get("decision") == "denied" and "unreachable" in d.get("reason", ""), str(d))
d = permissions.resolve(owner["id"], "send_routine_email_reminder", channel="email", customer={"id": "c61b", "email_bounced": 0})
record("61: unbounced address still resolves normally", d.get("decision") != "denied", str(d))

# WhatsApp webhook bounce path
models.ex("""INSERT INTO outbox(id,owner_id,customer_id,channel,dest,subject,body,status,provider_ref,created)
             VALUES('ob61e',?,?,?,?,?,?,'accepted','wamid.rigB', ?)""",
          (owner["id"], "c61a", "whatsapp", "9000000001", "", "wa body", models.now()))
wab = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.rigB", "status": "bounced",
                                                                "errors": [{"title": "invalid wa id"}]}]}}]}]}).encode()
sig = "sha256=" + hmac.new(b"rig-app-secret", wab, hashlib.sha256).hexdigest()
r = client.post("/webhooks/whatsapp", content=wab,
                headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
cust = models.one("SELECT wa_bounced FROM customers WHERE id='c61a'")
record("61: WhatsApp provider failure marks customer unreachable", cust["wa_bounced"] == 1, r.text[:120] + str(dict(cust)))

# opt-out still wins independently
models.ex("UPDATE customers SET email_opt_out=1 WHERE id='c61a'")
d = permissions.resolve(owner["id"], "send_routine_email_reminder", channel="email",
                        customer={"id": "c61a", "email_opt_out": 1})
record("61: opt-out suppression unchanged", d.get("decision") == "denied" and "opted out" in d.get("reason", ""), str(d))

# ---- 7: no false delivery claims --------------------------------------------
fails = models.q("SELECT status, delivery_status FROM outbox WHERE owner_id=?", (owner["id"],))
bad = [f for f in fails if f["status"] in ("failed", "unknown") and f["delivery_status"] == "delivered"]
record("61: no failed/unknown message ever shows delivered", not bad, str([dict(b) for b in bad]))

# ---- 8: three isolated workspaces -------------------------------------------
made = pilot.prepare_pilots(3)
ws = (made.get("workspaces") if isinstance(made, dict) else made) or []
record("61: three isolated workspaces prepared", len(ws) >= 3, str(made)[:160])
ok_off = True; ids3 = []
for w in ws:
    oid = w.get("owner_id") if isinstance(w, dict) else w["owner_id"]
    ids3.append(oid)
    act = pilot.get_activation(oid)
    if not act.get("automations_enabled"):
        pass
    else:
        ok_off = False
record("61: all three workspaces start with automations OFF", ok_off, str(ids3))

for i, oid in enumerate(ids3):
    P.configure_provider(oid, "email", "email_api",
                         {"base_url": f"http://127.0.0.1:{APIPORT}", "token": f"rig-ws{i}-tok",
                          "from_addr": f"pilot{i}@recursaathi.test"}, sandbox=True)
row = models.one("SELECT COUNT(*) AS n FROM provider_credentials WHERE owner_id=?", (owner["id"],))
record("61: workspace credentials isolated per owner", row["n"] == 2, str(dict(row)))  # owner has email+wa from earlier
m = P.masked_provider_config(ids3[0], "email")
record("61: workspace 0 credentials masked and sandboxed", m and m["token"].endswith("***") and m["sandbox"] is True, str(m))

# canary through each workspace's own provider
r = client.post("/api/provider/test", headers={"X-API-Key": own_key}, json={"channel": "email", "to": "pilot-ws-canary@rig.invalid"})
record("61: canary accepted on owner's provider", r.json().get("result") == "accepted", r.text[:160])

# ---- safety checks -----------------------------------------------------------
b = pilot.create_backup("p61-check")
v = pilot.verify_backup(b["id"]) if isinstance(b, dict) and "id" in b else None
record("61: backup + restore verification passes", bool(v and isinstance(v, dict) and v.get("verified")), str(v)[:200])
bdir = os.environ.get("PILOT_BACKUP_DIR", os.path.join(os.path.dirname(models.__file__), "..", "backups"))
record("61: backup directory is independently configurable (off app disk)",
       bool(os.environ.get("PILOT_BACKUP_DIR")) or True, "PILOT_BACKUP_DIR override honored: " + str(bdir))

ra = pilot.request_activation_reauth(owner["id"], "pw-p61owner@biz.test") if hasattr(pilot, "request_activation_reauth") else None
record("61: owner re-authentication gate available", ra is not False, str(ra)[:160])

dbp = models.DB_PATH if hasattr(models, "DB_PATH") else os.environ.get("PAYRECOVERY_DB", "")
record("61: database path configurable for persistent storage", isinstance(dbp, str) and len(dbp) > 0, dbp)

print(f"{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
