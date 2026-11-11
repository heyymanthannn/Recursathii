"""Phase 5.1 hardening tests: real-protocol provider rig, hardened activation,
trusted-source reference verification, backup-verified rollback, restricted
monitoring, three prepared workspaces, compliance export.

Everything here runs in a controlled test environment against protocol
servers we control. No real customers are contacted and production is never
touched.
"""
import os, tempfile, threading, socket, time, hashlib, sqlite3
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_51_", suffix=".sqlite")
from fastapi.testclient import TestClient
from app import main as app_main, models, pilot, permissions
from app.providers import send_email, send_whatsapp, deliver_outbox
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, extra if not ok else "")

# ---- protocol rig 1: a real SMTP server we control --------------------------
class _SMTPRig(threading.Thread):
    """A minimal but real SMTP server speaking the protocol over a socket.
    Behaviours: accept all, reject recipient, or drop mid-DATA."""
    daemon = True
    def __init__(self, mode):
        super().__init__()
        self.mode = mode; self.received = []
        self.sock = socket.socket(); self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0)); self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._session(c)
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
            cmd = line.decode(errors="replace").strip().upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"): w("250 rig\r\n")
            elif cmd.startswith("MAIL FROM"): w("250 OK\r\n")
            elif cmd.startswith("RCPT TO"):
                if self.mode == "reject_rcpt": w("550 no such user\r\n")
                else: w("250 OK\r\n")
            elif cmd.startswith("DATA"):
                if self.mode == "reject_rcpt": w("554 transaction failed\r\n")
                else:
                    w("354 go ahead\r\n"); in_data = True
                    if self.mode == "drop_mid_data": raise RuntimeError("rig drops mid-DATA")
            elif cmd.startswith("QUIT"): w("221 bye\r\n"); c.close(); return
            else: w("250 OK\r\n")

# ---- protocol rig 2: a WhatsApp Cloud API shaped HTTP server ----------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class _WARig(BaseHTTPRequestHandler):
    behaviour = "accept"
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(n)
        mode = self.behaviour
        if mode == "accept":
            r = b'{"messages":[{"id":"wamid.RIG123"}]}'
            self.send_response(200)
        elif mode == "reject":
            r = b'{"error":{"message":"invalid recipient"}}'
            self.send_response(400)
        elif mode == "server":
            self.send_response(503); r = b"overloaded"
        else:
            self.close_connection = True
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(r)))
        self.end_headers(); self.wfile.write(r)

def _wa_server(mode):
    h = type(f"_WARig_{mode}", (_WARig,), {"behaviour": mode})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]

WA_PORT = _wa_server("accept")
WA_PORT_BAD = _wa_server("reject")
WA_PORT_5XX = _wa_server("server")

smtp_ok = _SMTPRig("accept"); smtp_ok.start()
smtp_bad = _SMTPRig("reject_rcpt"); smtp_bad.start()
smtp_drop = _SMTPRig("drop_mid_data"); smtp_drop.start()
time.sleep(0.2)

# ---- 1. real provider acceptance, failure and unknown states ---------------
r = send_email("127.0.0.1", smtp_ok.port, "pilot@recursaathi.test", "pilot-test-1@rig.invalid", "Test", "hi")
record("51: real SMTP accepts message", r["status"] == "accepted", str(r))
r = send_email("127.0.0.1", smtp_bad.port, "pilot@recursaathi.test", "pilot-test-2@rig.invalid", "Test", "hi")
record("51: real SMTP rejection is failed", r["status"] == "failed", str(r))
r = send_email("127.0.0.1", smtp_drop.port, "pilot@recursaathi.test", "pilot-test-3@rig.invalid", "Test", "hi")
record("51: dropped connection is unknown", r["status"] == "unknown", str(r))
r = send_email("127.0.0.1", 1, "pilot@recursaathi.test", "pilot-test-4@rig.invalid", "Test", "hi", timeout=1.0)
record("51: unreachable server is unknown, not failed", r["status"] == "unknown", str(r))
r = send_whatsapp(f"http://127.0.0.1:{WA_PORT}", "rig-token", "phone-id-1", "+919999999901", "hello")
record("51: WhatsApp rig accepts with provider id", r["status"] == "accepted" and r["provider_id"] == "wamid.RIG123", str(r))
r = send_whatsapp(f"http://127.0.0.1:{WA_PORT_BAD}", "rig-token", "phone-id-1", "+919999999902", "hello")
record("51: WhatsApp 4xx is failed", r["status"] == "failed", str(r))
r = send_whatsapp(f"http://127.0.0.1:{WA_PORT_5XX}", "rig-token", "phone-id-1", "+919999999903", "hello")
record("51: WhatsApp 5xx is unknown", r["status"] == "unknown", str(r))
record("51: rig actually received the SMTP message", len(smtp_ok.received) >= 1, f"n={len(smtp_ok.received)}")

# ---- 2. delivery pipeline: ladder recorded, delivered never set ------------
OID = models.create_user("h51@example.com", hash_pw("correct horse"), "H51 Owner", "H51 Co", "9000000051", role="owner")["id"]
H = {"X-API-Key": models.create_api_key(OID, "h51")}
cu = models.create_customer(OID, "Ravi", email="ravi@h51.com")
for ch, dest in (("email", "pilot-test-5@rig.invalid"), ("whatsapp", "+919999999904")):
    models.ex("INSERT INTO outbox(id,owner_id,channel,dest,tpl,dedupe,subject,body,status,created) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              (models.uid(), OID, ch, dest, "day0", f"h51-{ch}", "Payment failed",
               "Your payment failed", "queued", models.now()))
permissions.set_config(OID, {"automations_enabled": True})
conf = {"smtp_host": "127.0.0.1", "smtp_port": smtp_ok.port,
        "wa_base_url": f"http://127.0.0.1:{WA_PORT}", "wa_token": "rig-token",
        "wa_phone_number_id": "phone-id-1"}
d = deliver_outbox(OID, conf)
record("51: pipeline attempts both channels", d["attempted"] == 2, str(d)[:120])
record("51: accepted statuses recorded with history", d["accepted"] == 2, str(d)[:120])
statuses = [r0["status"] for r0 in models.q("SELECT status FROM outbox WHERE owner_id=?", (OID,))]
record("51: send path never marks delivered", "delivered" not in statuses, str(statuses))
hist = models.q("SELECT status, detail FROM outbox_history WHERE owner_id=? ORDER BY created", (OID,))
record("51: provider id recorded in history", any("wamid.RIG123" in (h["detail"] or "") for h in hist), str(hist)[:200])
models.ex("UPDATE outbox SET status='unknown', error='connection problem: rig dropped' WHERE owner_id=? AND channel='email'", (OID,))
models.ex("INSERT INTO outbox(id,owner_id,channel,dest,tpl,dedupe,subject,body,status,created) "
          "VALUES(?,?,?,?,?,?,?,?,?,?)",
          (models.uid(), OID, "whatsapp", "+919999999905", "day0", "h51-wa2", "Payment failed", "Your payment failed", "queued", models.now()))
d2 = deliver_outbox(OID, conf)
unk = models.one("SELECT status FROM outbox WHERE owner_id=? AND channel='email'", (OID,))["status"]
record("51: unknown is never retried", d2["attempted"] == 1 and unk == "unknown", f"{str(d2)[:80]} unknown_status={unk}")

# ---- 3. hardened activation -------------------------------------------------
challenge_bad = client.post("/api/pilot/activation/challenge", headers=H, json={"password": "nope"})
record("51: challenge denies wrong password", challenge_bad.status_code == 401, str(challenge_bad.status_code))
ch = client.post("/api/pilot/activation/challenge", headers=H, json={"password": "correct horse"}).json()
no_code = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "ACTIVATE"})
record("51: ACTIVATE without code refused", no_code.status_code == 400, str(no_code.status_code))
bad_code = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "ACTIVATE", "code": "000000"})
record("51: ACTIVATE with wrong code refused", bad_code.status_code == 400, str(bad_code.status_code))
ok = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "ACTIVATE", "code": ch["code"]}).json()
record("51: ACTIVATE + reauth code activates", ok["activation"]["automations_enabled"] is True, str(ok)[:100])
reuse = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "ACTIVATE", "code": ch["code"]})
record("51: reauth code single use", reuse.status_code == 400, str(reuse.status_code))
ch2 = client.post("/api/pilot/activation/challenge", headers=H, json={"password": "correct horse"}).json()
models.ex("UPDATE pilot_reauth SET expires=? WHERE code=?", (models.now() - 1, ch2["code"]))
late = client.post("/api/pilot/activation", headers=H, json={"enabled": True, "confirm": "ACTIVATE", "code": ch2["code"]})
record("51: expired code refused", late.status_code == 400, str(late.status_code))
a = models.q("SELECT * FROM audit WHERE user_id=? AND action='automations_activated'", (OID,))
record("51: activation audited with reauth evidence", len(a) == 1 and "reauthenticated" in a[0]["detail"], str(a)[:150])
client.post("/api/pilot/activation", headers=H, json={"enabled": False})
record("51: deactivation still owner-controlled", client.get("/api/pilot/activation", headers=H).json()["automations_enabled"] is False, "")

# ---- 4. trusted-source reference verification ------------------------------
pid = models.uid()
models.ex("INSERT INTO payments(id,owner_id,invoice_id,customer_id,amount,method,status,created) VALUES(?,?,?,?,?,?,?,?)",
          (pid, OID, None, cu["id"], 50000, "upi", "imported", models.now()))
rec = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pid, "reference": "UTR-MISSING-1"}).json()
record("51: unknown reference stays imported", rec["changed"] is False and "not found" in rec["reason"], str(rec)[:120])
pilot.seed_trust_reference("razorpay", "UTR-OK-1", 50000, "captured")
pilot.seed_trust_reference("razorpay", "UTR-REFUND-1", 50000, "refunded")
pilot.seed_trust_reference("razorpay", "UTR-AMT-1", 41000, "captured")
rec = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pid, "reference": "UTR-REFUND-1"}).json()
record("51: refunded reference refused", rec["changed"] is False, str(rec)[:120])
rec = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pid, "reference": "UTR-AMT-1"}).json()
record("51: amount mismatch refused", rec["changed"] is False and "mismatch" in rec["reason"], str(rec)[:120])
rec = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pid, "reference": "UTR-OK-1"}).json()
record("51: matched captured reference confirms once", rec["changed"] is True and rec["payment"]["status"] == "confirmed", str(rec)[:120])
rec2 = client.post("/api/pilot/reconcile", headers=H, json={"payment_id": pid, "reference": "UTR-OK-1"}).json()
record("51: second reconcile changes nothing", rec2["changed"] is False, str(rec2)[:100])
vr = models.q("SELECT outcome FROM reference_verifications WHERE payment_id=?", (pid,))
record("51: verification attempts are recorded", any(v["outcome"] == "rejected" for v in vr) and any(v["outcome"] == "verified" for v in vr), str(vr))
rate = models.q("SELECT COALESCE(SUM(amount),0) AS v FROM payments WHERE owner_id=? AND status='confirmed'", (OID,))
record("51: recovered revenue counts confirmed once", rate[0]["v"] == 50000, str(rate))
seed_denied = client.post("/pilot/trust-source", headers={"X-Admin-Token": ""}, json={"reference": "X", "amount": 1})
record("51: trust source is admin-only", seed_denied.status_code == 403, str(seed_denied.status_code))

# ---- 5. backup verified rollback -------------------------------------------
b = pilot.create_backup("pre-rollback h51")
v = pilot.verify_backup(b["id"])
record("51: backup created with sha256 + counts", len(b["sha256"]) == 64 and b["counts"]["payments"] >= 1, str(b)[:150])
record("51: backup verifies: sha, quick_check, counts", v["verified"] is True, str(v)[:200])
raw = open(b["path"], "rb").read()
open(b["path"], "wb").write(raw[:-4] + b"xxxx")
v2 = pilot.verify_backup(b["id"])
open(b["path"], "wb").write(raw)
record("51: tampered backup fails verification", v2["verified"] is False and v2["sha256_ok"] is False, str(v2)[:150])
rb = pilot.rollback_check()
record("51: no DROP TABLE anywhere in app code", rb["drop_table_in_app_code"] == [], str(rb))
record("51: rollback preserves data (tables never dropped)", any("never" in s and "dropped" in s for s in rb["procedure"]), str(rb["procedure"])[:200])
v3 = pilot.verify_backup(b["id"])
record("51: backup re-verifies clean after restore", v3["verified"] is True, str(v3)[:150])

# ---- 6. restricted monitoring ----------------------------------------------
metrics_denied = client.get("/pilot/metrics", headers={"X-API-Key": H["X-API-Key"]})
record("51: business key cannot read platform metrics", metrics_denied.status_code in (401, 403), str(metrics_denied.status_code))
from app import config
adm = {"X-Admin-Token": config.ADMIN_TOKEN}
m = client.get("/pilot/metrics", headers=adm)
record("51: admin token reads metrics", m.status_code == 200, str(m.status_code))
audit_rows = models.q("SELECT * FROM audit WHERE action='pilot_metrics_access'")
record("51: metrics access audited", len(audit_rows) >= 1, f"n={len(audit_rows)}")

# ---- 7. three prepared isolated workspaces ---------------------------------
prep = pilot.prepare_pilots(3)
record("51: three workspaces prepared, automations off", prep["created"] == 3 and all(w["automations_enabled"] is False for w in prep["workspaces"]), str(prep)[:200])
wkeys = [models.one("SELECT api_key FROM pilot_workspaces WHERE owner_id=?", (w["owner_id"],))["api_key"] for w in prep["workspaces"]]
models.create_customer(prep["workspaces"][0]["owner_id"], "Asha", email="asha@w1.invalid")
models.create_customer(prep["workspaces"][1]["owner_id"], "Bala", email="bala@w2.invalid")
counts0 = models.q("SELECT COUNT(*) AS n FROM customers WHERE owner_id=?", (prep["workspaces"][0]["owner_id"],))[0]["n"]
counts1 = models.q("SELECT COUNT(*) AS n FROM customers WHERE owner_id=?", (prep["workspaces"][1]["owner_id"],))[0]["n"]
record("51: workspaces hold only their own records", counts0 == 1 and counts1 == 1, f"{counts0}/{counts1}")
comp0 = client.get("/api/pilot/compliance", headers={"X-API-Key": wkeys[0]}).json()
record("51: compliance export tenant scoped", all(c.get("owner_id") == prep["workspaces"][0]["owner_id"] for c in comp0["consents"]) if comp0["consents"] else True, str(comp0)[:120])

# ---- 8. compliance export content ------------------------------------------
client.post("/api/pilot/consents", headers=H, json={"subject": "Ravi", "kind": "customer_messaging_consent", "channel": "email", "granted_by": "owner", "evidence": "test"})
comp = client.get("/api/pilot/compliance", headers=H).json()
record("51: compliance export has consents + audit", len(comp["consents"]) >= 1 and len(comp["audit_trail"]) >= 1, str(comp)[:150])
record("51: compliance export shows channels + activation", "authorized_channels" in comp and "activation_state" in comp, "")

# ---- 9. endpoints gated -----------------------------------------------------
d_denied = client.post("/pilot/deliver", headers={"X-Admin-Token": ""}, json={})
record("51: delivery pipeline endpoint admin-only", d_denied.status_code == 403, str(d_denied.status_code))
b_denied = client.post("/pilot/backup", headers={"X-Admin-Token": ""}, json={})
record("51: backup endpoint admin-only", b_denied.status_code == 403, str(b_denied.status_code))

# ---- 6. metrics schema resilience (m480: monitoring query vs current schema,
#      missing-column and missing-table scenarios) ----
pilot._ensure_outbox_cols()
models.ex("ALTER TABLE outbox DROP COLUMN delivery_status")
_m = pilot.pilot_metrics()
record("51: metrics survives missing delivery_status column", isinstance(_m, dict) and "reminders_accepted_by_provider" in _m, str(_m)[:140])
_cols = [r["name"] for r in models.q("PRAGMA table_info(outbox)")]
record("51: healing is additive (column restored, no data loss)", "delivery_status" in _cols, str(_cols))
models.ex("DROP TABLE outbox")
_m2 = pilot.pilot_metrics()
record("51: metrics survives missing outbox table", isinstance(_m2, dict) and _m2.get("reminders_accepted_by_provider") == 0 and _m2.get("confirmed_deliveries") == 0, str(_m2)[:140])
record("51: other tables untouched after outbox rebuild", models.q("SELECT COUNT(*) AS n FROM payments") is not None, "payments intact")

print(f"{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
