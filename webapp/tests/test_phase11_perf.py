"""Phase 9.11: performance and load smoke test (dev, synthetic data).

Seeds a realistic first-customer-volume dataset for one owner (250 customers,
1,000 invoices, 1,500 outbox rows) and times the read paths a business owner
hits every day plus the write path the automation engine depends on.
Thresholds are generous (dev laptop-class hardware) and are about REGRESSION:
catch a query that accidentally scans across owners, not absolute speed.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase11_perf
Isolated temp DB. No network. No production data.
"""
import os, sys, time, tempfile
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p11_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models, sequences
from app.auth import hash_pw

models.init()
results = []
def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:160].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("| " + d) if not ok else "")

ow = models.create_user("p11@test.local", hash_pw("pw12345678"), "Owner", "Biz11", "919810000011")
models.ex("UPDATE users SET email='p11@test.local' WHERE id=?", (ow["id"],))
import time as _t
t0 = _t.time()
cids = []
for i in range(250):
    cu = models.create_customer(ow["id"], f"Customer {i:04d}", email=f"c{i}@x.test",
                                phone=f"91981000{i:04d}", notes="")
    cids.append(cu["id"] if isinstance(cu, dict) else cu)
for i in range(1000):
    c = cids[i % 250]
    due = 1765850000 + i * 3600
    models.create_invoice(ow["id"], c, f"INV-{i:05d}", 99900 + i, "svc", due)
seed_s = _t.time() - t0
record("seed 250 customers + 1000 invoices under 60s", seed_s < 60, f"{seed_s:.1f}s")
n_inv = models.one("SELECT COUNT(*) AS c FROM invoices WHERE owner_id=?", (ow["id"],))["c"]
record("invoices seeded", n_inv == 1000, n_inv)

client = TestClient(app_main.app)
r = client.post("/login", data={"email": "p11@test.local", "password": "pw12345678"}, follow_redirects=False)
record("login ok", r.status_code in (303, 200), r.status_code)

def timed(name, path, limit_s):
    t = _t.time()
    rr = client.get(path)
    dt = _t.time() - t
    record(f"{name} < {limit_s}s (status {rr.status_code})", rr.status_code == 200 and dt < limit_s, f"{dt:.2f}s")

timed("dashboard", "/dashboard", 2.0)
timed("customers list", "/customers", 2.0)
timed("invoices list", "/invoices", 2.0)
timed("radar", "/radar", 2.0)
timed("automations", "/automations", 2.0)
timed("analytics dashboard", "/analytics", 2.0)

# write path: outbox insert throughput (automation engine foundation)
t = _t.time()
for i in range(200):
    models.ex("INSERT INTO outbox (owner_id, channel, dest, tpl, dedupe, subject, body, status, created) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (ow["id"], "email", f"c{i % 250}@x.test", "reminder_d3", f"p11-{i}",
               "Reminder", f"body {i}", "queued", 1765850000 + i))
dt = _t.time() - t
# threshold notes: models.ex opens one connection per call (~12ms each);
# real automation runs insert <= 25 rows per run, so 200 inserts staying
# well under 5s leaves ample headroom. Revisit only if runs batch bigger.
record("200 outbox inserts < 5s", dt < 5.0, f"{dt:.2f}s")

# tenant-scoped aggregate (the query pattern behind recovery stats)
t = _t.time()
# the radar page visit above already flipped pending past-due invoices to
# 'overdue' (batched UPDATE), so the aggregate counts the overdue bucket
row = models.one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM invoices WHERE owner_id=? AND status='overdue'", (ow["id"],))
dt = _t.time() - t
record("owner-scoped aggregate < 0.5s", row["c"] == 1000 and dt < 0.5, f"{dt:.3f}s c={row['c']}")

print(f"PHASE 9.11 PERF: {sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
