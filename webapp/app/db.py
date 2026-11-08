"""Free database backend: SQLite.

Zero cost, zero setup, ships inside the repo (stdlib only). Every merchant,
failure event and WhatsApp message is written through here, so state survives
restarts. Swap PAYRECOVERY_DB for a Postgres/Supabase URL later without
touching the engine.
"""
import os, sqlite3

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
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, to_phone TEXT, event_id TEXT,
          playbook TEXT, body TEXT, at REAL
        );
        """)
        # Column migrations (safe to re-run).
        for stmt in (
            "ALTER TABLE merchants ADD COLUMN next_month_discount INTEGER DEFAULT 0",
            "ALTER TABLE merchants ADD COLUMN welcome_sent INTEGER DEFAULT 0",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass

def save_merchant(m):
    with _conn() as c:
        init()
        c.execute(
            "INSERT OR REPLACE INTO merchants(id,email,name,phone,plan,status,"
            "subscription_id,next_month_discount,welcome_sent) VALUES(?,?,?,?,?,?,?,?,?)",
            (m["id"], m.get("email"), m.get("name"), m.get("phone"),
             m.get("plan"), m.get("status"), m.get("subscription_id"),
             m.get("next_month_discount", 0), m.get("welcome_sent", 0)))

def load_merchants() -> dict:
    init()
    with _conn() as c:
        return {r["id"]: dict(r) for r in c.execute("SELECT * FROM merchants")}

def save_event(e):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
