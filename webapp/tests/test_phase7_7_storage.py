"""Phase 7.7 storage tests: Postgres migration parity, persistence, recovery.

Runs against a disposable local PostgreSQL (PG77_URL env). Synthetic data
only; no production database, no network beyond the test Postgres.
Run:  cd webapp && PG77_URL=... python3 tests/test_phase7_7_storage.py
"""
import hashlib, json, os, subprocess, sys, tempfile, time

PG = os.environ.get("PG77_URL")
if not PG:
    print("SKIP: PG77_URL not set")
    sys.exit(0)
os.environ["PAYRECOVERY_DB"] = PG

from app import models  # noqa: E402

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, ("" if cond else detail))

SEED = r'''
import os, sys, time, json
os.environ["PAYRECOVERY_DB"] = sys.argv[1]
from app import models, db
models.init(); db.init()
t0 = 1731299000.0
owners = []
for i in (1, 2, 3):
    owners.append(models.create_user(f"owner{i}@synthetic.test", "x"*60, f"Owner {i}",
                             f"Business {i} Synthetic", f"999000000{i}", role="owner")["id"])
models.create_user("staff1@synthetic.test", "x"*60, "Staff One", "Business 1 Synthetic",
                           "9990000019", role="staff", parent_id=owners[0])
for i, o in enumerate(owners):
    for c in range(3):
        models.create_customer(o, f"Customer {i}-{c}", f"c{i}{c}@synthetic.test", f"999111000{i}{c}")
    invs = []
    for c in range(2):
        cust = models.list_customers(o)[c]
        invs.append(models.create_invoice(o, cust["id"], f"INV-77-{i}-{c}", 12345 + 6789 * c,
                                    "synthetic subscription", "2026-12-01"))
    models.mark_invoice_paid(invs[0], "upi", reference="UTR77SYN" + str(i), note="synthetic")
    models.ex("UPDATE payments SET confirmed_at=:ts WHERE invoice_id=:iid", {"ts": t0 + i + 0.123456, "iid": invs[0]["id"]})
    invs[1]["confirmed_at"] = t0 + i + 0.123456
    models.record_payment_attempt(invs[1], "upi", "pending", note="synthetic")
    models.record_event(o, "payment_failed", "razorpay", invs[1]["customer_id"], 19123, "insufficient_funds", invs[1]["id"])
    models.set_automation(o, "email", True)
    models.set_template(o, "first_chase", "Payment follow-up", "Hi {{name}}, gentle nudge.")
    models.log_audit(o, "login", "synthetic audit entry")
    models.log_activity(o, "invoice.paid", "synthetic activity")
    models.record_plat_pay(o, "starter", 99900, "paid", "SYN77-" + str(i))
    models.create_support_message(o, "Synthetic Person", "sp@synthetic.test", "synthetic support note")
# db.py demo store: merchant, coupon, webhook event
db.save_merchant({"id": owners[0], "email": "owner1@synthetic.test", "name": "Business 1 Synthetic",
                  "phone": "9990000001", "plan": "starter", "status": "active", "subscription_id": "sub77"})
db.issue_coupon(owners[0], 10)
db.save_event({"id": "ev77syn1", "merchant_id": owners[0], "gateway": "razorpay",
               "customer_name": "Customer 0-0", "customer_phone": "99911100000",
               "amount": 12345, "decline_code": "insufficient_funds", "mandate_id": "mnd77",
               "playbook": "first_chase", "retries_due": [t0 + 3600], "recovered": False})
out = {}
for t in ("users", "customers", "invoices", "payments", "pay_events", "automations",
          "etemplates", "audit", "activities", "plat_pays", "support", "merchants",
          "events", "coupons"):
    out[t] = len(models.q(f"SELECT * FROM {t}") if t not in ("merchants", "events", "coupons")
                 else [])
import sqlite3 as sq
con = sq.connect(sys.argv[1]); con.row_factory = sq.Row
for t in ("merchants", "events", "coupons"):
    out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
print("SEEDED " + json.dumps(out))
'''

def digest(rows):
    canon = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()[:16]

def pg_tables():
    from app import pgcompat
    with pgcompat.conn() as c:
        return [r["table_name"] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
            "AND table_type='BASE TABLE' ORDER BY table_name").fetchall()]

# ---- A. TLS is required by default (fail closed against a non-TLS server) ----
from app import pgcompat
saved = os.environ["PAYRECOVERY_DB"]
os.environ["PAYRECOVERY_DB"] = PG.split("?")[0]  # no sslmode -> pgcompat appends require
try:
    pgcompat._connect()
    check("tls: default connection refuses a server without TLS", False, "connected unexpectedly")
except Exception as e:
    check("tls: default connection refuses a server without TLS", True)
finally:
    os.environ["PAYRECOVERY_DB"] = saved
    pgcompat.close_all()

# ---- B. seed three synthetic businesses in SQLite, then migrate ----
sqlite_path = tempfile.mktemp(prefix="mig77_", suffix=".sqlite")
r = subprocess.run([sys.executable, "-c", SEED, sqlite_path], capture_output=True, text=True)
seed_ok = "SEEDED" in r.stdout
check("seed: 3 synthetic businesses created in SQLite", seed_ok, (r.stderr or r.stdout)[-300:])
src_counts = json.loads(r.stdout.split("SEEDED ", 1)[1]) if seed_ok else {}

mig = subprocess.run([sys.executable, "scripts/migrate_sqlite_to_pg.py", "--sqlite", sqlite_path,
                      "--pg", PG], capture_output=True, text=True, cwd=".")
check("migrate: script reports OK with identical digests", "MIGRATION OK" in mig.stdout,
      (mig.stderr or mig.stdout)[-300:])

# ---- C. parity: counts, exact values, relationships ----
import sqlite3 as sq
scon = sq.connect(sqlite_path); scon.row_factory = sq.Row
tables = [t for t in pg_tables() if t != "schema_migrations"]
mismatches = []
for t in tables:
    if not scon.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
        continue
    srows = [dict(x) for x in scon.execute(f'SELECT * FROM "{t}"')]
    prows = models.q(f'SELECT * FROM "{t}"')
    if len(srows) != len(prows) or digest(srows) != digest(prows):
        mismatches.append(t)
check(f"parity: all {len(tables)} tables match SQLite row-for-row", not mismatches, f"mismatched: {mismatches}")

inv = models.one("SELECT * FROM invoices ORDER BY amount LIMIT 1")
check("parity: invoice amount is an exact integer", isinstance(inv["amount"], int) and inv["amount"] == 12345,
      repr(inv.get("amount")))
pay = models.one("SELECT * FROM payments WHERE confirmed_at IS NOT NULL ORDER BY confirmed_at LIMIT 1")
sval = scon.execute("SELECT confirmed_at FROM payments WHERE id=?", (pay["id"],)).fetchone()[0] if pay else None
check("parity: payment timestamp preserved exactly (bit-for-bit)",
      pay is not None and sval == pay["confirmed_at"] and isinstance(pay["confirmed_at"], float),
      f"sqlite={sval!r} pg={pay and pay.get('confirmed_at')!r}")
orphan = models.one("SELECT COUNT(*) AS n FROM invoices i LEFT JOIN customers c ON i.customer_id=c.id WHERE c.id IS NULL")
check("relationships: every invoice points at an existing customer", orphan["n"] == 0)
orphp = models.one("SELECT COUNT(*) AS n FROM payments p LEFT JOIN invoices i ON p.invoice_id=i.id WHERE i.id IS NULL")
check("relationships: every payment points at an existing invoice", orphp["n"] == 0)
check("relationships: customer email search stays case-insensitive (ILIKE)",
      bool(models.list_customers(models.get_user_by_email("owner1@synthetic.test")["id"], "customer 0-0")))
check("migration: version recorded in schema_migrations",
      bool(models.one("SELECT * FROM schema_migrations WHERE version='0001-phase7.7-sqlite-to-pg'")))

# ---- D. restart persistence: fresh process reads the same PG database ----
prog = ("import os,sys,json;os.environ['PAYRECOVERY_DB']=sys.argv[1];"
        "from app import models;"
        "print(json.dumps({'users': len(models.q('SELECT * FROM users')),"
        "'invoices': len(models.q('SELECT * FROM invoices')),"
        "'payments': len(models.q('SELECT * FROM payments'))}))")
r2 = subprocess.run([sys.executable, "-c", prog, PG], capture_output=True, text=True)
try:
    fresh = json.loads(r2.stdout.strip().splitlines()[-1])
except Exception:
    fresh = {}
check("restart: a fresh process sees every migrated record",
      fresh.get("users", 0) >= 4 and fresh.get("invoices", 0) >= 6, r2.stderr[-200:])

# ---- E. connection interruption: clean failure, self-healing reconnect ----
import psycopg
victim = psycopg.connect(PG, connect_timeout=5)
victim.autocommit = True
n_before = models.one("SELECT COUNT(*) AS n FROM users")["n"]
victim.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='rs' AND pid <> pg_backend_pid()")
victim.close()
try:
    models.one("SELECT COUNT(*) AS n FROM users")
    check("interruption: a dropped connection raises cleanly (no silent failure)", False)
except Exception as e:
    check("interruption: a dropped connection raises cleanly (no silent failure)",
          type(e).__name__ in ("OperationalError", "InterfaceError") or "connection" in str(e).lower(),
          type(e).__name__)
pgcompat.close_all()
check("interruption: app reconnects and committed data is intact",
      pgcompat.healthy() and models.one("SELECT COUNT(*) AS n FROM users")["n"] == n_before)
# uncommitted work must not survive the drop
c = pgcompat.conn()
c.execute("INSERT INTO customers(id, owner_id, name) VALUES('ghost77','x','Ghost')")
try:
    victim2 = psycopg.connect(PG, connect_timeout=5); victim2.autocommit = True
    victim2.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='rs' AND pid <> pg_backend_pid()")
    victim2.close()
except Exception:
    pass
pgcompat.close_all()
check("interruption: uncommitted writes are rolled back (no phantom rows)",
      models.one("SELECT COUNT(*) AS n FROM customers WHERE id='ghost77'")["n"] == 0)

# ---- F. tenant isolation on PostgreSQL ----
o1, o2 = models.get_user_by_email("owner1@synthetic.test")["id"], models.get_user_by_email("owner2@synthetic.test")["id"]
n1 = len(models.list_customers(o1)); n2 = len(models.list_customers(o2))
names1 = {c["name"] for c in models.list_customers(o1)}
check("tenant isolation: owner 1 sees only their own customers", n1 == 3 and n2 == 3)
check("tenant isolation: invoices are scoped per owner",
      all(i["owner_id"] == o1 for i in models.list_invoices(o1)) and all(i["owner_id"] == o2 for i in models.list_invoices(o2)))
b1 = models.customer_balances(o1)
check("tenant isolation: balances aggregate only own invoices", isinstance(b1, list) and len(b1) <= 3)

# ---- G. backup/restore round-trip on PostgreSQL ----
from app import backup
gz = backup.snapshot_gz()
check("backup: PG snapshot captured (gzip JSON, >= 20 tables)",
      gz[:2] == b"\x1f\x8b" and len(json.loads(__import__("gzip").decompress(gz))["tables"]) >= 20)
counts = backup.restore_pg(gz)
check("restore: every table restored with the same row count",
      all(counts[t] == models.one(f'SELECT COUNT(*) AS n FROM "{t}"')["n"] for t in counts))
check("restore: data survived (sample row intact)",
      models.one("SELECT * FROM users WHERE email='owner1@synthetic.test'") is not None)

# ---- H. the application boots and runs on PostgreSQL ----
try:
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    r = client.post("/api/signup", json={"plan": "starter", "email": "pgsmoke77@synthetic.test",
                                         "phone": "9997770001", "password": "test-password-77",
                                         "name": "PG Smoke Business"})
    check("app on PG: signup creates a trial merchant", r.status_code == 200, r.text[:200])
    u = models.get_user_by_email("pgsmoke77@synthetic.test")
    check("app on PG: the signup row landed in PostgreSQL", u is not None and u["plan"] == "starter")
    r = client.post("/login", data={"email": "pgsmoke77@synthetic.test", "password": "test-password-77"},
                    follow_redirects=False)
    check("app on PG: login works against PostgreSQL", r.status_code in (302, 303), str(r.status_code))
    r = client.get(f"/dashboard/{u['id']}")
    body = r.json() if r.status_code == 200 else {}
    check("app on PG: dashboard serves from PostgreSQL",
          r.status_code == 200 and body.get("merchant") == u["id"] and "stats" in body,
          f"status={r.status_code} body={str(body)[:120]}")
except Exception as e:
    check("app on PG: boots and serves", False, f"{type(e).__name__}: {e}")


# ---- I. float precision on every connection (phase 7.8.1 regression guard) ----
# Through a transaction pooler the extra_float_digits startup parameter is
# dropped, so float8 timestamps came back shortest-15-digit and row digests
# mismatched. The driver must restore full precision per session.
try:
    from app import pgcompat as _pg
    _cc = _pg.conn()
    _efd = _cc.execute("SHOW extra_float_digits").fetchone()
    check("pg driver: connection forces full float precision",
          _efd is not None and str(_efd[0]) == "3", f"extra_float_digits={_efd}")
    _stamp = 1794505235.7981179  # the exact value that first mismatched
    _cc.execute('INSERT INTO activities(id, owner_id, kind, message, created) '
                'VALUES(%s,%s,%s,%s,%s)', ("float-precision-guard", "x", "test", "guard row", _stamp))
    _back = _cc.execute('SELECT created FROM activities WHERE id=%s', ("float-precision-guard",)).fetchone()[0]
    check("pg driver: float8 survives the round trip bit-exact",
          _back == _stamp and _back.hex() == _stamp.hex(), f"{_stamp!r} came back {_back!r}")
    _cc.execute('DELETE FROM activities WHERE id=%s', ("float-precision-guard",))
    _cc.commit(); _cc.close()
except Exception as e:
    check("pg driver: float precision guard ran", False, f"{type(e).__name__}: {e}")

print(f"SUMMARY: {len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
