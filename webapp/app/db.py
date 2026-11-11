"""Free database backend: SQLite.

Zero cost, zero setup, ships inside the repo (stdlib only). Every merchant,
failure event and WhatsApp message is written through here, so state survives
restarts. Swap PAYRECOVERY_DB for a Postgres/Supabase URL later without
touching the engine.
"""
import os, sqlite3, time

DB_PATH = os.environ.get("PAYRECOVERY_DB", "data/payrecovery.db")

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS merchants(
          id TEXT PRIMARY KEY, email TEXT, name TEXT, phone TEXT, plan TEXT,
          status TEXT, subscription_id TEXT
        );
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, merchant_id TEXT, gateway TEXT,
          customer_name TEXT, customer_phone TEXT, amount INTEGER,
          decline_code TEXT, mandate_id TEXT, playbook TEXT,
          retries_due TEXT, retries_done INTEGER, recovered INTEGER
        );
        CREATE TABLE IF NOT EXISTS coupons(
          code TEXT PRIMARY KEY, merchant_id TEXT, pct INTEGER,
          active INTEGER DEFAULT 1, created REAL, redeemed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, to_phone TEXT, event_id TEXT,
          playbook TEXT, body TEXT, at REAL
        );
        CREATE TABLE IF NOT EXISTS usage_counters(
          owner_id INTEGER, month TEXT, key TEXT,
          count INTEGER DEFAULT 0,
          PRIMARY KEY(owner_id, month, key)
        );
        """)
        # Column migrations (safe to re-run).
        for stmt in (
            "ALTER TABLE merchants ADD COLUMN next_month_discount INTEGER DEFAULT 0",
            "ALTER TABLE merchants ADD COLUMN welcome_sent INTEGER DEFAULT 0",
            "ALTER TABLE merchants ADD COLUMN created REAL DEFAULT 0",
            "ALTER TABLE merchants ADD COLUMN discount_pct INTEGER DEFAULT 0",
            "ALTER TABLE merchants ADD COLUMN coupon_code TEXT DEFAULT ''",
            "ALTER TABLE merchants ADD COLUMN retention_state TEXT DEFAULT ''",
            "ALTER TABLE merchants ADD COLUMN retention_outcome TEXT DEFAULT ''",
            "ALTER TABLE merchants ADD COLUMN retention_at REAL DEFAULT 0",
            "ALTER TABLE merchants ADD COLUMN claimed_utr TEXT DEFAULT ''",
            "ALTER TABLE merchants ADD COLUMN claimed_at REAL DEFAULT 0",
            "ALTER TABLE events ADD COLUMN status TEXT DEFAULT 'failed'",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass

def save_merchant(m):
    with _conn() as c:
        init()
        existing = c.execute("SELECT created FROM merchants WHERE id=?", (m["id"],)).fetchone()
        created = m.get("created") or (existing["created"] if existing and existing["created"] else time.time())
        c.execute(
            "INSERT OR REPLACE INTO merchants(id,email,name,phone,plan,status,"
            "subscription_id,next_month_discount,welcome_sent,created,discount_pct,"
            "coupon_code,retention_state,retention_outcome,retention_at,"
            "claimed_utr,claimed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m.get("email"), m.get("name"), m.get("phone"),
             m.get("plan"), m.get("status"), m.get("subscription_id"),
             m.get("next_month_discount", 0), m.get("welcome_sent", 0),
             created, m.get("discount_pct", 0), m.get("coupon_code", ""),
             m.get("retention_state", ""), m.get("retention_outcome", ""),
             m.get("retention_at", 0), m.get("claimed_utr", ""),
             m.get("claimed_at", 0)))

# ---- coupons ----
def issue_coupon(merchant_id: str, pct: int) -> str:
    """One coupon code per merchant; reusable until redeemed."""
    with _conn() as c:
        init()
        row = c.execute("SELECT code FROM coupons WHERE merchant_id=? AND active=1", (merchant_id,)).fetchone()
        if row:
            return row["code"]
        code = "RS" + os.urandom(3).hex().upper()
        c.execute("INSERT INTO coupons(code,merchant_id,pct,active,created) VALUES(?,?,?,?,?)",
                  (code, merchant_id, pct, 1, time.time()))
        return code

def find_coupon(code: str):
    code = (code or "").strip().upper()
    if not code:
        return None
    with _conn() as c:
        init()
        r = c.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None

def mark_coupon_redeemed(code: str):
    with _conn() as c:
        init()
        c.execute("UPDATE coupons SET redeemed=1, active=0 WHERE code=?", (code,))

def load_merchants() -> dict:
    init()
    with _conn() as c:
        return {r["id"]: dict(r) for r in c.execute("SELECT * FROM merchants")}

def get_event(event_id):
    with _conn() as c:
        return c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()


def set_event_status(event_id, status):
    with _conn() as c:
        init()
        c.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))


def save_event(e):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO events(id,merchant_id,gateway,customer_name,customer_phone,amount,decline_code,mandate_id,playbook,retries_due,retries_done,recovered) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (e["id"], e["merchant_id"], e["gateway"], e["customer_name"],
                   e["customer_phone"], e["amount"], e["decline_code"], e["mandate_id"],
                   e["playbook"], ",".join(str(t) for t in e.get("retries_due", [])),
                   e.get("retries_done", 0), 1 if e.get("recovered") else 0))

def update_event(event_id: str, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [event_id]
    with _conn() as c:
        c.execute(f"UPDATE events SET {cols} WHERE id=?", vals)

def load_events() -> list[dict]:
    init()
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM events")]
    for r in rows:
        r["retries_due"] = [float(t) for t in (r["retries_due"] or "").split(",") if t]
        r["recovered"] = bool(r["recovered"])
    return rows

def save_message(m):
    with _conn() as c:
        c.execute("INSERT INTO messages(to_phone,event_id,playbook,body,at) VALUES(?,?,?,?,?)",
                  (m["to"], m.get("event_id"), m["playbook"], m["body"], m["at"]))

def load_messages() -> list[dict]:
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY id")]
