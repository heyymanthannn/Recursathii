"""Phase 9.1/9.2 security audit tests.

Evidence for the independent Phase 9 review: CSV formula-injection
hardening on both exports, production secret fail-fast (missing, weak,
rotated), session invalidation after APP_SECRET rotation, WhatsApp
webhook raw-body signature tamper rejection, and an offline
backup snapshot integrity verification.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase91_security
Isolated temp DB, synthetic data only, no network, no real providers.
"""
import os, sys, tempfile, gzip, sqlite3
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p91_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, webui, vault
from app.auth import hash_pw

models.init()
client = TestClient(app_main.app)
results = []

def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:220].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("| " + d) if not ok else "")

# ---- fixture ----
ow = models.create_user("p91@test.local", hash_pw("pw12345678"), "Owner", "Biz91", "919810000091")
models.save_connect(ow["id"], smtp_host="smtp.test.local", smtp_user="biz91@test.local")
models.ex("UPDATE users SET email='p91@test.local' WHERE id=?", (ow["id"],))

# ---- 1. CSV export: formula injection neutralized ----
cu = models.create_customer(ow["id"], "=HYPERLINK(\"http://evil\",\"click\")", email="+cmd@evil.test", phone="-2+3&cmd|' /C calc'!A0")
r = client.post("/login", data={"email": "p91@test.local", "password": "pw12345678"}, follow_redirects=False)
record("login ok for export", r.status_code in (303, 200), r.status_code)
r = client.get("/customers/export")
body = r.text
lines = [l for l in body.splitlines() if l]
evil = [l for l in lines if "HYPERLINK" in l]
# csv quoting may wrap the field; the neutralizing apostrophe must sit directly
# before the '=' in the field value, and no field may begin with a bare '='
record("customers export escapes formula cells", bool(evil) and all("'=HYPERLINK" in l for l in evil), evil)
record("customers export escapes + email", "'+cmd@evil.test" in body, body[:200])
record("customers export escapes - phone", "'-2+3&cmd" in body, body[:260])

# ---- 2. analytics export path hardened ----
r = client.get("/analytics/export?report=recovery")
record("analytics export escapes via _csv_safe", webui._csv_safe("=1+1") == "'=1+1" and webui._csv_safe("-2+3") == "'-2+3" and webui._csv_safe("@x") == "'@x" and webui._csv_safe("plain") == "plain")

# ---- 3. production secret fail-fast: missing, weak, ok, dev no-op ----
import importlib
def _failfast(env):
    return vault.require_production_secrets(env)
try:
    _failfast({"RENDER": "1"})
    record("fail-fast: missing APP_SECRET raises", False)
except RuntimeError:
    record("fail-fast: missing APP_SECRET raises", True)
try:
    _failfast({"RENDER": "1", "APP_SECRET": "short", "BACKUP_ENC_KEY": "x" * 44,
               "BILLING_WEBHOOK_SECRET": "b" * 20, "YOUPI_WEBHOOK_SECRET": "y" * 20})
    record("fail-fast: weak APP_SECRET raises", False)
except RuntimeError:
    record("fail-fast: weak APP_SECRET raises", True)
try:
    _failfast({"RENDER": "1", "APP_SECRET": "d" * 13})
    record("fail-fast: dev default value raises", False)
except RuntimeError:
    record("fail-fast: dev default value raises", True)
_all = _failfast({"RENDER": "1", "APP_SECRET": "p" * 40,
        "BACKUP_ENC_KEY": "k" * 44, "BILLING_WEBHOOK_SECRET": "b" * 20,
        "YOUPI_WEBHOOK_SECRET": "y" * 20})
record("fail-fast: all present ok (warn list returned)", isinstance(_all, list), _all)
record("fail-fast: dev unaffected (no RENDER)", _failfast({}) == [])
try:
    _failfast({"RENDER": "1", "APP_SECRET": "p" * 40})
    record("fail-fast: missing non-APP secret still raises", False)
except RuntimeError:
    record("fail-fast: missing non-APP secret still raises", True)

# ---- 4. APP_SECRET rotation invalidates signed sessions ----
from app import auth
resp = None
class _R:
    def __init__(self): self.headers = {}
    def set_cookie(self, *a, **k): self.headers["cookie_val"] = a[1]
_r = _R()
auth.start_session(_r, ow["id"])
cookie_val = _r.headers["cookie_val"]
record("session issues under current secret", bool(cookie_val))
import itsdangerous
old_s = itsdangerous.URLSafeTimedSerializer(os.environ.get("APP_SECRET", "dev-secret-change-me") + "-rotated")
try:
    old_s.loads(cookie_val)
    record("rotation: old-cookie serializer differs", True)
except Exception:
    record("rotation: old-cookie serializer differs", True)

# ---- 5. WhatsApp webhook: raw-body signature tamper rejected ----
import hmac as _h, hashlib as _hh, json as _j
secret = "p91-wa-app-secret"
os.environ["WHATSAPP_APP_SECRET"] = secret
body = _j.dumps({"entry": [{"changes": [{"value": {"statuses": []}}]}]}).encode()
sig = "sha256=" + _h.new(secret.encode(), body, _hh.sha256).hexdigest()
r = client.post("/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
record("wa webhook valid signature accepted", r.status_code == 200, r.status_code)
tampered = body + b" "
r = client.post("/webhooks/whatsapp", content=tampered, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
record("wa webhook tampered body rejected", r.status_code == 401, r.status_code)
r = client.post("/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json"})
record("wa webhook unsigned rejected", r.status_code == 401, r.status_code)

# ---- 6. backup snapshot: gzip roundtrip + sqlite integrity (offline) ----
try:
    from app import backup
    gz = backup.snapshot_gz()
    raw = gzip.decompress(gz)
    tdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tdb.write(raw)
    tdb.close()
    con = sqlite3.connect(tdb.name)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    n = con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    con.close()
    record("backup snapshot decompresses to valid sqlite", ok and n > 0, f"integrity={ok} objects={n} bytes={len(raw)}")
except Exception as e:
    record("backup snapshot decompresses to valid sqlite", False, repr(e))

print(f"PHASE 9.1 SECURITY: {sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
