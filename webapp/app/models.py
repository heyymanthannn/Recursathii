"""RecurSaathi platform data layer (stdlib sqlite3, swap PAYRECOVERY_DB for Postgres later)."""
import os, sqlite3, threading, time, uuid

# serializes cap-check + queue-insert so concurrent jobs cannot bypass frequency caps
QUEUE_LOCK = threading.Lock()

DB_PATH = os.environ.get("PAYRECOVERY_DB", "data/payrecovery.db")

def conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def now(): return time.time()
def uid(): return uuid.uuid4().hex
def token(n=24): return uuid.uuid4().hex[:n]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  name TEXT, business TEXT, phone TEXT, role TEXT DEFAULT 'owner', parent_id TEXT,
  upi_vpa TEXT, payee_name TEXT, plan TEXT DEFAULT 'starter',
  plan_status TEXT DEFAULT 'trialing', trial_ends REAL, trial_started_at REAL,
  rzp_key_enc TEXT, rzp_secret_enc TEXT, reset_token TEXT, reset_expires REAL, created REAL
);
CREATE TABLE IF NOT EXISTS customers(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
  email TEXT, phone TEXT, notes TEXT, wa_opt_out INTEGER DEFAULT 0, created REAL
);
CREATE TABLE IF NOT EXISTS invoices(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  number TEXT, amount INTEGER NOT NULL, description TEXT, due_date TEXT,
  status TEXT DEFAULT 'pending', token TEXT UNIQUE, created REAL, paid_at REAL
);
CREATE TABLE IF NOT EXISTS payments(
  id TEXT PRIMARY KEY, owner_id TEXT, invoice_id TEXT, customer_id TEXT,
  amount INTEGER NOT NULL, method TEXT, status TEXT, reference TEXT,
  note TEXT, created REAL, confirmed_at REAL
);
CREATE TABLE IF NOT EXISTS pay_events(
  id TEXT PRIMARY KEY, owner_id TEXT, kind TEXT, gateway TEXT,
  customer_id TEXT, amount INTEGER, decline_code TEXT, invoice_id TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS automations(
  owner_id TEXT, key TEXT, enabled INTEGER DEFAULT 1, updated REAL,
  PRIMARY KEY(owner_id, key)
);
CREATE TABLE IF NOT EXISTS etemplates(
  owner_id TEXT, key TEXT, subject TEXT, body TEXT, updated REAL,
  PRIMARY KEY(owner_id, key)
);
CREATE TABLE IF NOT EXISTS outbox(
  id TEXT PRIMARY KEY, owner_id TEXT, channel TEXT, dest TEXT, tpl TEXT,
  dedupe TEXT UNIQUE, subject TEXT, body TEXT, status TEXT, error TEXT,
  created REAL, sent REAL
);
CREATE TABLE IF NOT EXISTS activities(
  id TEXT PRIMARY KEY, owner_id TEXT, kind TEXT, message TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS outbox_history(
  id TEXT PRIMARY KEY, outbox_id TEXT, owner_id TEXT,
  status TEXT, detail TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS audit(
  id TEXT PRIMARY KEY, user_id TEXT, action TEXT, detail TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS support(
  id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, email TEXT, message TEXT,
  status TEXT DEFAULT 'open', created REAL
);
CREATE TABLE IF NOT EXISTS plat_pays(
  id TEXT PRIMARY KEY, owner_id TEXT, plan TEXT, amount INTEGER,
  status TEXT, reference TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
  city TEXT DEFAULT '', created REAL
);
CREATE TABLE IF NOT EXISTS api_keys(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, key_hash TEXT UNIQUE,
  label TEXT DEFAULT '', created REAL, last_used REAL, revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rules(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
  trigger TEXT, threshold REAL, action TEXT, template TEXT,
  enabled INTEGER DEFAULT 1, created REAL
);
CREATE INDEX IF NOT EXISTS ix_inv_owner ON invoices(owner_id);
CREATE INDEX IF NOT EXISTS ix_cust_owner ON customers(owner_id);
"""

def init():
    with conn() as c:
        c.executescript(SCHEMA)
        c.execute("""CREATE TABLE IF NOT EXISTS portal_users(
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  created INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS usage_counters(
          owner_id TEXT, month TEXT, key TEXT, count INTEGER DEFAULT 0,
          PRIMARY KEY(owner_id, month, key))""")
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
        if "parent_id" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN parent_id TEXT")
        ocols = [r["name"] for r in c.execute("PRAGMA table_info(outbox)")]
        for col, ddl in [("provider_message_id", "TEXT"), ("accepted_at", "REAL"),
                         ("delivered_at", "REAL"), ("read_at", "REAL"), ("failed_at", "REAL"),
                         ("bounced_at", "REAL"), ("approval_binding", "TEXT")]:
            if col not in ocols:
                c.execute(f"ALTER TABLE outbox ADD COLUMN {col} {ddl}")
        if "reset_token" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN reset_token TEXT")
        if "reset_expires" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN reset_expires REAL")
        if "trial_started_at" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN trial_started_at REAL")
        if "billing_cycle" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN billing_cycle TEXT DEFAULT 'monthly'")
        if "verify_token" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN verify_token TEXT")
        if "verified_at" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN verified_at REAL")
        if "automation_mode" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN automation_mode TEXT DEFAULT 'auto'")
        if "totp_secret" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT DEFAULT ''")
        if "totp_enabled" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0")
        if "onboarded" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER DEFAULT 0")
        ccols = [r["name"] for r in c.execute("PRAGMA table_info(customers)")]
        if "email_opt_out" not in ccols:
            c.execute("ALTER TABLE customers ADD COLUMN email_opt_out INTEGER DEFAULT 0")
        if "unsub_token" not in ccols:
            c.execute("ALTER TABLE customers ADD COLUMN unsub_token TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_unsub_token ON customers(unsub_token) WHERE unsub_token IS NOT NULL")
        for tbl, col in (("customers", "branch_id"), ("invoices", "branch_id")):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")]
            if col not in cols:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS connect(
          owner_id TEXT PRIMARY KEY, business_name TEXT, logo TEXT,
          smtp_host TEXT, smtp_port INTEGER, smtp_user TEXT, smtp_pass_enc TEXT,
          mail_from_name TEXT, wa_phone_id TEXT, wa_token_enc TEXT,
          paused INTEGER DEFAULT 0, created REAL
        );
        """)

def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]

def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None

def ex(sql, args=()):
    with conn() as c:
        c.execute(sql, args)

# ---- users ----
def create_user(email, pw_hash, name, business, phone, role="owner",
                upi_vpa="", payee_name="", plan="starter", parent_id=""):
    u = {"id": uid(), "email": email.strip().lower(), "pw_hash": pw_hash,
         "name": name, "business": business, "phone": phone or "", "role": role,
         "upi_vpa": upi_vpa, "payee_name": payee_name or name, "plan": plan,
         "plan_status": "trialing", "trial_ends": None, "trial_started_at": None, "created": now()}
    ex("INSERT INTO users(id,email,pw_hash,name,business,phone,role,upi_vpa,payee_name,plan,plan_status,trial_ends,trial_started_at,created,parent_id) "
       "VALUES(:id,:email,:pw_hash,:name,:business,:phone,:role,:upi_vpa,:payee_name,:plan,:plan_status,:trial_ends,:trial_started_at,:created,:parent_id)",
       {**u, "parent_id": parent_id})
    return get_user(u["id"])

def get_user(uid_): return one("SELECT * FROM users WHERE id=?", (uid_,))
def get_user_by_email(email): return one("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))
def update_user(uid_, **kw):
    if not kw: return
    cols = ",".join(f"{k}=:{k}" for k in kw)
    ex(f"UPDATE users SET {cols} WHERE id=:id", {**kw, "id": uid_})
    return get_user(uid_)

def list_team(owner_id):
    return q("SELECT id,name,email,role,created FROM users WHERE parent_id=? ORDER BY created DESC", (owner_id,))

def remove_team_member(member_id, owner_id):
    ex("DELETE FROM users WHERE id=? AND parent_id=?", (member_id, owner_id))

def set_user_secret(uid_, col, value):
    ex(f"UPDATE users SET {col}=? WHERE id=?", (value, uid_))

# ---- customers ----
def create_customer(owner_id, name, email="", phone="", notes=""):
    c = {"id": uid(), "owner_id": owner_id, "name": name, "email": email or "",
         "phone": (phone or "").replace(" ", ""), "notes": notes or "", "created": now()}
    ex("INSERT INTO customers(id,owner_id,name,email,phone,notes,created) "
       "VALUES(:id,:owner_id,:name,:email,:phone,:notes,:created)", c)
    return get_customer(c["id"])

def get_customer(cid): return one("SELECT * FROM customers WHERE id=?", (cid,))
def list_customers(owner_id, search=""):
    if search:
        like = f"%{search}%"
        return q("SELECT * FROM customers WHERE owner_id=? AND (name LIKE ? OR email LIKE ? OR phone LIKE ?) ORDER BY created DESC",
                 (owner_id, like, like, like))
    return q("SELECT * FROM customers WHERE owner_id=? ORDER BY created DESC", (owner_id,))
def update_customer(cid, **kw):
    if not kw: return
    cols = ",".join(f"{k}=:{k}" for k in kw)
    ex(f"UPDATE customers SET {cols} WHERE id=:id", {**kw, "id": cid})
    return get_customer(cid)

def customer_balances(owner_id):
    rows = q("""SELECT cu.id, cu.name, cu.email, cu.phone, cu.wa_opt_out, cu.created,
        COUNT(i.id) AS invoices, COALESCE(SUM(CASE WHEN i.status IN ('pending','overdue') THEN i.amount END),0) AS outstanding,
        COALESCE(SUM(CASE WHEN i.status='paid' THEN i.amount END),0) AS paid_total,
        COUNT(DISTINCT CASE WHEN i.status='paid' THEN i.id END) AS paid_invoices,
        MAX(p.confirmed_at) AS last_purchase,
        MAX(i.due_date) AS last_due
        FROM customers cu
        LEFT JOIN invoices i ON i.customer_id=cu.id
        LEFT JOIN payments p ON p.customer_id=cu.id AND p.status='confirmed'
        WHERE cu.owner_id=? GROUP BY cu.id ORDER BY outstanding DESC, cu.created DESC""", (owner_id,))
    return rows

SEGMENTS = {
    "all": "All customers",
    "high_value": "High value (lifetime paid Rs 10,000+)",
    "outstanding": "Has outstanding balance",
    "returning": "Returning (2+ paid invoices)",
    "dormant": "Dormant (no payment in 45 days)",
    "new": "New (no payments yet)",
}

def segment_rows(owner_id, seg="all"):
    rows = customer_balances(owner_id)
    cutoff = time.time() - 45 * 86400
    if seg == "high_value":
        rows = [r for r in rows if r["paid_total"] >= 1000000]
    elif seg == "outstanding":
        rows = [r for r in rows if r["outstanding"] > 0]
    elif seg == "returning":
        rows = [r for r in rows if r["paid_invoices"] >= 2]
    elif seg == "dormant":
        rows = [r for r in rows if r["paid_total"] > 0 and (not r["last_purchase"] or r["last_purchase"] < cutoff)]
    elif seg == "new":
        rows = [r for r in rows if not r["paid_total"]]
    return rows

def automation_stats(owner_id):
    return q("SELECT tpl, status, COUNT(*) AS n FROM outbox WHERE owner_id=? GROUP BY tpl, status ORDER BY tpl, status", (owner_id,))

def reactivation_outcomes(owner_id):
    return q("""SELECT cu.id AS cid, cu.name, o.dest AS contact, o.created AS msg_at,
        (SELECT MAX(p.confirmed_at) FROM payments p WHERE p.customer_id=cu.id AND p.status='confirmed' AND p.confirmed_at > o.created) AS paid_after
        FROM outbox o
        JOIN customers cu ON cu.owner_id=o.owner_id AND (cu.email=o.dest OR cu.phone=o.dest)
        WHERE o.owner_id=? AND o.tpl='dormant_reactivate'
        GROUP BY cu.id ORDER BY o.created DESC LIMIT 50""", (owner_id,))

# ---- invoices ----
def create_invoice(owner_id, customer_id, number, amount, description, due_date):
    inv = {"id": uid(), "owner_id": owner_id, "customer_id": customer_id,
           "number": number or f"INV-{int(now())}", "amount": int(amount),
           "description": description or "", "due_date": due_date,
           "status": "pending", "token": token(24), "created": now()}
    ex("INSERT INTO invoices(id,owner_id,customer_id,number,amount,description,due_date,status,token,created) "
       "VALUES(:id,:owner_id,:customer_id,:number,:amount,:description,:due_date,:status,:token,:created)", inv)
    return get_invoice(inv["id"])

def get_invoice(iid): return one("SELECT * FROM invoices WHERE id=?", (iid,))
def get_invoice_by_token(tok): return one("SELECT * FROM invoices WHERE token=?", (tok,))
def list_invoices(owner_id, status=""):
    if status:
        return q("SELECT * FROM invoices WHERE owner_id=? AND status=? ORDER BY created DESC", (owner_id, status))
    return q("SELECT * FROM invoices WHERE owner_id=? ORDER BY created DESC", (owner_id,))

def mark_invoice_paid(inv, method, reference="", note=""):
    """Confirm payment on an invoice. If a payment attempt was already recorded
    for this invoice (webhook or manual attempt), confirm THAT row instead of
    inserting a duplicate confirmed payment (duplicate payment events would
    double-count recovered revenue)."""
    existing = one("SELECT * FROM payments WHERE invoice_id=? ORDER BY created DESC LIMIT 1", (inv["id"],))
    if existing:
        pay = dict(existing)
        ex("UPDATE payments SET status='confirmed', method=?, reference=COALESCE(NULLIF(?,''),reference), "
           "note=CASE WHEN ?!='' THEN ? ELSE note END, "
           "confirmed_at=COALESCE(confirmed_at, ?) WHERE id=?",
           (method, reference, note, note, now(), existing["id"]))
        pay.update(status="confirmed", method=method, reference=reference or existing["reference"],
                   confirmed_at=existing["confirmed_at"] or now())
    else:
        pay = {"id": uid(), "owner_id": inv["owner_id"], "invoice_id": inv["id"],
               "customer_id": inv["customer_id"], "amount": inv["amount"], "method": method,
               "status": "confirmed", "reference": reference, "note": note,
               "created": now(), "confirmed_at": now()}
        ex("INSERT INTO payments(id,owner_id,invoice_id,customer_id,amount,method,status,reference,note,created,confirmed_at) "
           "VALUES(:id,:owner_id,:invoice_id,:customer_id,:amount,:method,:status,:reference,:note,:created,:confirmed_at)", pay)
    ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (now(), inv["id"]))
    return pay

def refund_payment(payment_id, note="", gateway="manual"):
    """Record a refund against a confirmed payment. The invoice stays marked
    paid with its history; the payment itself shows refunded and a refund
    event lands in pay_events for reconciliation."""
    pay = one("SELECT * FROM payments WHERE id=?", (payment_id,))
    if not pay or pay["status"] != "confirmed":
        return None
    ex("UPDATE payments SET status='refunded', note=? WHERE id=?",
       ((pay["note"] + " | refunded: " + note).strip(" |") if note else "refunded", payment_id))
    ex("INSERT INTO pay_events(id,owner_id,kind,gateway,customer_id,amount,decline_code,invoice_id,created) "
       "VALUES(?,?,?,?,?,?,?,?,?)",
       (uid(), pay["owner_id"], "refund", gateway, pay["customer_id"], pay["amount"], "", pay["invoice_id"], now()))
    return dict(pay)


def set_verify_token(user_id, token):
    ex("UPDATE users SET verify_token=? WHERE id=?", (token, user_id))

def mark_verified(user_id):
    ex("UPDATE users SET verified_at=?, verify_token=NULL WHERE id=?", (now(), user_id))


def record_payment_attempt(inv, method, status="pending", reference="", note=""):
    pay = {"id": uid(), "owner_id": inv["owner_id"], "invoice_id": inv["id"],
           "customer_id": inv["customer_id"], "amount": inv["amount"], "method": method,
           "status": status, "reference": reference, "note": note, "created": now()}
    ex("INSERT INTO payments(id,owner_id,invoice_id,customer_id,amount,method,status,reference,note,created) "
       "VALUES(:id,:owner_id,:invoice_id,:customer_id,:amount,:method,:status,:reference,:note,:created)", pay)
    return pay

def update_payment(pid, **kw):
    cols = ",".join(f"{k}=:{k}" for k in kw)
    ex(f"UPDATE payments SET {cols} WHERE id=:id", {**kw, "id": pid})

# ---- events (failed payments) ----
def record_event(owner_id, kind, gateway, customer_id, amount, decline_code="", invoice_id=""):
    e = {"id": uid(), "owner_id": owner_id, "kind": kind, "gateway": gateway,
         "customer_id": customer_id, "amount": int(amount or 0),
         "decline_code": decline_code or "", "invoice_id": invoice_id, "created": now()}
    ex("INSERT INTO pay_events(id,owner_id,kind,gateway,customer_id,amount,decline_code,invoice_id,created) "
       "VALUES(:id,:owner_id,:kind,:gateway,:customer_id,:amount,:decline_code,:invoice_id,:created)", e)
    return e

# ---- automations ----
AUTO_KEYS = ["reminder_due", "overdue_1", "overdue_7", "failed_payment", "payment_confirm", "dormant_reactivate", "due_call"]

def get_automations(owner_id):
    rows = {r["key"]: bool(r["enabled"]) for r in q("SELECT * FROM automations WHERE owner_id=?", (owner_id,))}
    return {k: rows.get(k, True) for k in AUTO_KEYS}

def set_automation(owner_id, key, enabled):
    ex("INSERT INTO automations(owner_id,key,enabled,updated) "
       "VALUES(:owner_id,:key,:enabled,:updated) "
       "ON CONFLICT(owner_id,key) DO UPDATE SET enabled=:enabled, updated=:updated",
       {"owner_id": owner_id, "key": key, "enabled": 1 if enabled else 0, "updated": now()})

def get_template(owner_id, key):
    return one("SELECT * FROM etemplates WHERE owner_id=? AND key=?", (owner_id, key))

def set_template(owner_id, key, subject, body):
    ex("INSERT INTO etemplates(owner_id,key,subject,body,updated) "
       "VALUES(:owner_id,:key,:subject,:body,:updated) "
       "ON CONFLICT(owner_id,key) DO UPDATE SET subject=:subject, body=:body, updated=:updated",
       {"owner_id": owner_id, "key": key, "subject": subject, "body": body, "updated": now()})

# ---- outbox ----
def queue(owner_id, channel, dest, tpl, subject, body, dedupe=""):
    if dedupe:
        if one("SELECT id FROM outbox WHERE dedupe=?", (dedupe,)):
            return None
    else:
        dedupe = None   # UNIQUE index allows many NULLs, not many empty strings
    o = {"id": uid(), "owner_id": owner_id, "channel": channel, "dest": dest,
         "tpl": tpl, "dedupe": dedupe, "subject": subject, "body": body,
         "status": "queued", "created": now()}
    ex("INSERT INTO outbox(id,owner_id,channel,dest,tpl,dedupe,subject,body,status,created) "
       "VALUES(:id,:owner_id,:channel,:dest,:tpl,:dedupe,:subject,:body,:status,:created)", o)
    return get_outbox(o["id"])

def get_outbox(oid): return one("SELECT * FROM outbox WHERE id=?", (oid,))
def record_delivery_status(oid, status, detail=""):
    """One row per status transition. Distinct statuses per the founder spec:
    queued, processing, accepted, delivered, read, failed, bounced, unknown,
    plus skipped/deferred outcomes. History is the audit trail for delivery."""
    o = get_outbox(oid)
    if not o:
        return
    ex("INSERT INTO outbox_history(id,outbox_id,owner_id,status,detail,created) VALUES(?,?,?,?,?,?)",
       (uid(), oid, o["owner_id"], status, detail[:500] if isinstance(detail, str) else "", now()))

def set_outbox_status(oid, status, error=""):
    ex("UPDATE outbox SET status=?, error=? WHERE id=?", (status, error, oid))

def list_outbox(owner_id, limit=100):
    return q("SELECT * FROM outbox WHERE owner_id=? ORDER BY created DESC LIMIT ?", (owner_id, limit))

# ---- activity + audit ----
def log_activity(owner_id, kind, message):
    ex("INSERT INTO activities(id,owner_id,kind,message,created) VALUES(?,?,?,?,?)",
       (uid(), owner_id, kind, message, now()))

def log_audit(user_id, action, detail=""):
    ex("INSERT INTO audit(id,user_id,action,detail,created) VALUES(?,?,?,?,?)",
       (uid(), user_id, action, detail, now()))

def list_activities(owner_id, limit=30):
    return q("SELECT * FROM activities WHERE owner_id=? ORDER BY created DESC LIMIT ?", (owner_id, limit))

# ---- support ----
def create_support_message(owner_id, name, email, message):
    s = {"id": uid(), "owner_id": owner_id, "name": name, "email": email,
         "message": message, "created": now()}
    ex("INSERT INTO support(id,owner_id,name,email,message,created) "
       "VALUES(:id,:owner_id,:name,:email,:message,:created)", s)
    return s

def list_support(status=""):
    if status:
        return q("SELECT * FROM support WHERE status=? ORDER BY created DESC", (status,))
    return q("SELECT * FROM support ORDER BY created DESC")

# ---- platform payments ----
def record_plat_pay(owner_id, plan, amount, status, reference=""):
    p = {"id": uid(), "owner_id": owner_id, "plan": plan, "amount": amount,
         "status": status, "reference": reference, "created": now()}
    ex("INSERT INTO plat_pays(id,owner_id,plan,amount,status,reference,created) "
       "VALUES(:id,:owner_id,:plan,:amount,:status,:reference,:created)", p)
    return p

def list_plat_pays(owner_id=""):
    if owner_id:
        return q("SELECT * FROM plat_pays WHERE owner_id=? ORDER BY created DESC", (owner_id,))
    return q("SELECT * FROM plat_pays ORDER BY created DESC")

# ---- owner dashboard metrics ----

def plan_usage(owner_id):
    """Live usage counts for plan limits: customers, invoices, messages, AI ops, workflows, team seats."""
    t = time.localtime()
    ms = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    month = time.strftime("%Y-%m", t)
    cust = q("SELECT COUNT(*) AS c FROM customers WHERE owner_id=?", (owner_id,))[0]["c"]
    inv = q("SELECT COUNT(*) AS c FROM invoices WHERE owner_id=? AND created>=?", (owner_id, ms))[0]["c"]
    wf = q("SELECT COUNT(*) AS c FROM automations WHERE owner_id=? AND enabled=1", (owner_id,))[0]["c"]
    uc = {r["key"]: r["count"] for r in q(
        "SELECT key, count FROM usage_counters WHERE owner_id=? AND month=?", (owner_id, month))}
    return {"customers": cust, "invoices_month": inv, "team": len(list_team(owner_id)),
            "messages_month": uc.get("messages", 0), "ai_ops_month": uc.get("ai_ops", 0),
            "workflows": wf}


def bump_usage(owner_id, key, n=1):
    """Count a billable action (automated messages, AI operations) for this month."""
    month = time.strftime("%Y-%m")
    ex("INSERT INTO usage_counters(owner_id,month,key,count) VALUES(?,?,?,?) "
       "ON CONFLICT(owner_id,month,key) DO UPDATE SET count=count+?",
       (owner_id, month, key, n, n))


def plan_limit_msg(owner_id, kind):
    """Backend plan-limit check. Returns an upgrade message or '' when allowed."""
    from . import config
    u = dict(get_user(owner_id) or {})
    p = config.PLANS.get(config.plan_key(u.get("plan") or "starter")) or config.PLANS["starter"]
    usage = plan_usage(owner_id)
    if kind == "customers" and usage["customers"] >= p["max_customers"]:
        return f"Your {p['name']} plan covers {p['max_customers']:,} customers."
    if kind == "invoices" and usage["invoices_month"] >= p["max_invoices_month"]:
        return f"Your {p['name']} plan covers {p['max_invoices_month']:,} invoices a month."
    if kind == "team" and usage["team"] >= p["max_team"]:
        return f"Your {p['name']} plan includes {p['max_team']} team member(s)."
    if kind == "messages" and usage["messages_month"] >= p.get("max_emails_month", 0):
        return f"Your {p['name']} plan covers {p['max_emails_month']:,} automated messages a month."
    if kind == "ai_ops" and usage["ai_ops_month"] >= p.get("max_ai_ops_month", 0):
        return f"Your {p['name']} plan covers {p['max_ai_ops_month']:,} AI operations a month."
    if kind == "workflows" and usage["workflows"] >= p.get("max_workflows", 0):
        return f"Your {p['name']} plan includes {p['max_workflows']} automation workflows."
    return ""


def owner_metrics(owner_id):
    today = time.strftime("%Y-%m-%d")
    inv = one("""SELECT
        COALESCE(SUM(CASE WHEN status='paid' THEN amount END),0) AS recovered,
        COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN status IN ('pending','overdue') THEN amount END),0) AS outstanding,
        COUNT(CASE WHEN status='paid' THEN 1 END) AS paid_count,
        COUNT(CASE WHEN status IN ('pending','overdue') THEN 1 END) AS open_count,
        COUNT(CASE WHEN status='overdue' OR (status='pending' AND due_date < :today) THEN 1 END) AS overdue_count
        FROM invoices WHERE owner_id=:oid""", {"oid": owner_id, "today": today})
    failed = one("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM pay_events WHERE owner_id=? AND kind='failed'", (owner_id,))
    attempts = one("SELECT COUNT(*) AS n FROM payments WHERE owner_id=?", (owner_id,))
    # success rate: paid / (paid + failed attempts)
    paid_n = inv["paid_count"] or 0
    failed_n = failed["n"] or 0
    rate = round(100.0 * paid_n / (paid_n + failed_n), 1) if (paid_n + failed_n) else 0.0
    inv["success_rate"] = rate
    inv["failed_count"] = failed_n
    inv["failed_amount"] = failed["amt"] or 0
    inv["attempts"] = attempts["n"] or 0
    return inv

def recovery_by_month(owner_id, months=6):
    rows = q("""SELECT strftime('%Y-%m', confirmed_at, 'unixepoch') AS m,
        COALESCE(SUM(amount),0) AS amt FROM payments
        WHERE owner_id=? AND status='confirmed' AND confirmed_at IS NOT NULL
        GROUP BY m ORDER BY m DESC LIMIT ?""", (owner_id, months))
    return list(reversed(rows))

def recent_payments(owner_id, limit=10):
    return q("""SELECT p.*, cu.name AS customer_name, i.number AS invoice_number
        FROM payments p LEFT JOIN customers cu ON cu.id=p.customer_id
        LEFT JOIN invoices i ON i.id=p.invoice_id
        WHERE p.owner_id=? ORDER BY p.created DESC LIMIT ?""", (owner_id, limit))

# ---- radar ----
def radar(owner_id):
    """Recovery Radar data: overdue invoices, dormant repeat customers and
    failed payments, each row annotated with the computed fields the radar
    page needs (days overdue, priority, inactivity, segmentation, retry
    eligibility). All values come from real records only."""
    today = time.strftime("%Y-%m-%d")
    today_t = time.mktime(time.strptime(today, "%Y-%m-%d"))
    overdue = q("""SELECT i.*, cu.name AS customer_name, cu.email, cu.phone FROM invoices i
        JOIN customers cu ON cu.id=i.customer_id
        WHERE i.owner_id=? AND i.status IN ('pending','overdue') AND i.due_date < ?
        ORDER BY i.due_date ASC""", (owner_id, today))
    for i in overdue:
        if i["status"] == "pending":
            ex("UPDATE invoices SET status='overdue' WHERE id=?", (i["id"],))
    overdue = [dict(i) for i in overdue]
    for i in overdue:
        try:
            y, m, d = (int(x) for x in i["due_date"][:10].split("-"))
            days = max(0, round((today_t - time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))) / 86400))
        except Exception:
            days = 0
        i["days_overdue"] = days
        # priority from real data: how late it is and how much is at stake
        score = days * 2 + min(i["amount"] / 100000.0, 10)
        i["priority"] = "high" if score >= 20 else ("medium" if score >= 8 else "low")
    dormant = q("""SELECT cu.id, cu.name, cu.email, cu.phone,
        COALESCE(SUM(p.amount),0) AS lifetime, MAX(p.confirmed_at) AS last_paid_at,
        COUNT(*) AS paid_count
        FROM customers cu JOIN payments p ON p.customer_id=cu.id AND p.status='confirmed'
        WHERE cu.owner_id=? GROUP BY cu.id
        HAVING last_paid_at IS NOT NULL AND last_paid_at < ?
        ORDER BY last_paid_at ASC LIMIT 20""", (owner_id, now() - 45 * 86400))
    dormant = [dict(c) for c in dormant]
    for c in dormant:
        c["days_inactive"] = max(0, round((now() - c["last_paid_at"]) / 86400))
        # segmentation from real spend and repeat behaviour
        c["segment"] = ("VIP" if c["lifetime"] >= 500000 else
                        "Loyal repeat" if c["lifetime"] >= 100000 or c["paid_count"] >= 3 else
                        "Occasional")
    transient = {"insufficient_funds", "insufficient_funds ", "network", "timeout",
                 "temporarily declined", "bank timeout", "issuer unavailable"}
    failed = q("""SELECT e.*, cu.name AS customer_name FROM pay_events e
        LEFT JOIN customers cu ON cu.id=e.customer_id
        WHERE e.owner_id=? AND e.kind='failed' ORDER BY e.created DESC LIMIT 20""", (owner_id,))
    failed = [dict(e) for e in failed]
    for e in failed:
        code = (e["decline_code"] or "").strip().lower()
        age_h = (now() - e["created"]) / 3600
        if code in transient:
            e["retry_eligible"] = "Yes, within the 72h retry window" if age_h < 72                 else "Window closed, needs a fresh attempt"
        elif not code or code == "declined" or "not reported" in code:
            e["retry_eligible"] = "Unknown reason: confirm with the customer first"
        else:
            e["retry_eligible"] = "Needs customer action"
        e["retry_status"] = "Not attempted"
    return {"overdue": overdue, "dormant": dormant, "failed": failed}


def decline_breakdown(owner_id, limit=8):
    return q("SELECT COALESCE(decline_code,'not reported') AS code, COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM pay_events WHERE owner_id=? AND kind='failed' GROUP BY code ORDER BY n DESC LIMIT ?", (owner_id, limit))

def monthly_summary(owner_id, months=6):
    inv = q("SELECT strftime('%Y-%m', created, 'unixepoch') AS m, COUNT(*) AS invoiced, COALESCE(SUM(amount),0) AS invoiced_amt FROM invoices WHERE owner_id=? GROUP BY m ORDER BY m DESC LIMIT ?", (owner_id, months))
    rec = {r["m"]: r["amt"] for r in recovery_by_month(owner_id, months)}
    out = [{"m": r["m"], "invoiced": r["invoiced"], "invoiced_amt": r["invoiced_amt"], "recovered": rec.get(r["m"], 0)} for r in inv]
    out.reverse()
    return out

def recovery_series(owner_id, period="monthly", months=6):
    if period == "weekly":
        rows = q("SELECT strftime('%Y-W%W', confirmed_at, 'unixepoch') AS m, COALESCE(SUM(amount),0) AS amt FROM payments WHERE owner_id=? AND status='confirmed' AND confirmed_at IS NOT NULL GROUP BY m ORDER BY m DESC LIMIT ?", (owner_id, months))
        rows = list(reversed(rows))
        return [dict(r, label="W" + r["m"].split("-W")[1]) for r in rows]
    rows = recovery_by_month(owner_id, months)
    return [dict(r, label=r["m"][5:7] + "/" + r["m"][:4]) for r in rows]


def set_reset_token(email, tok, ttl=3600):
    ex("UPDATE users SET reset_token=?, reset_expires=? WHERE email=?",
       (tok, now() + ttl, email.strip().lower()))

def user_by_reset(tok):
    u = one("SELECT * FROM users WHERE reset_token=?", (tok,))
    if u and u["reset_expires"] and u["reset_expires"] > now():
        return u
    return None

def clear_reset(uid_):
    ex("UPDATE users SET reset_token='', reset_expires=NULL WHERE id=?", (uid_,))

def change_password(uid_, pw_hash):
    ex("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, uid_))


# ---- retailer-owned connection (business email + WhatsApp Business) ----
def get_connect(owner_id):
    return one("SELECT * FROM connect WHERE owner_id=?", (owner_id,))

def save_connect(owner_id, business_name="", logo="", smtp_host="", smtp_port=587,
                 smtp_user="", smtp_pass_enc=None, mail_from_name="", wa_phone_id="",
                 wa_token_enc=None, paused=0):
    cur = get_connect(owner_id)
    row = dict(cur) if cur else {}
    vals = {
        "owner_id": owner_id,
        "business_name": (business_name or row.get("business_name", "")).strip(),
        "logo": logo if logo else row.get("logo", ""),
        "smtp_host": smtp_host if smtp_host else row.get("smtp_host", ""),
        "smtp_port": int(smtp_port or row.get("smtp_port") or 587),
        "smtp_user": (smtp_user or row.get("smtp_user", "")).strip(),
        "smtp_pass_enc": smtp_pass_enc if smtp_pass_enc is not None else row.get("smtp_pass_enc", ""),
        "mail_from_name": mail_from_name if mail_from_name else row.get("mail_from_name", ""),
        "wa_phone_id": (wa_phone_id or row.get("wa_phone_id", "")).strip(),
        "wa_token_enc": wa_token_enc if wa_token_enc is not None else row.get("wa_token_enc", ""),
        "paused": int(row.get("paused", 0)) if paused is None else int(paused),
        "created": row.get("created") or now(),
    }
    ex("INSERT OR REPLACE INTO connect(owner_id,business_name,logo,smtp_host,smtp_port,"
       "smtp_user,smtp_pass_enc,mail_from_name,wa_phone_id,wa_token_enc,paused,created) "
       "VALUES(:owner_id,:business_name,:logo,:smtp_host,:smtp_port,:smtp_user,"
       ":smtp_pass_enc,:mail_from_name,:wa_phone_id,:wa_token_enc,:paused,:created)", vals)
    return get_connect(owner_id)

def set_connect_paused(owner_id, paused):
    ex("INSERT OR IGNORE INTO connect(owner_id,paused,created) VALUES(?,?,?)",
       (owner_id, int(paused), now()))
    ex("UPDATE connect SET paused=? WHERE owner_id=?", (int(paused), owner_id))


# ---- branches (Enterprise: multi-branch management) ----
def list_branches(owner_id):
    return q("SELECT * FROM branches WHERE owner_id=? ORDER BY created", (owner_id,))

def branch_count(owner_id):
    return q("SELECT COUNT(*) AS c FROM branches WHERE owner_id=?", (owner_id,))[0]["c"]

def add_branch(owner_id, name, city=""):
    b = {"id": uid(), "owner_id": owner_id, "name": (name or "").strip(),
         "city": (city or "").strip(), "created": now()}
    if not b["name"]:
        return None
    ex("INSERT INTO branches(id,owner_id,name,city,created) VALUES(:id,:owner_id,:name,:city,:created)", b)
    return get_branch(b["id"], owner_id)

def get_branch(branch_id, owner_id):
    return one("SELECT * FROM branches WHERE id=? AND owner_id=?", (branch_id, owner_id))

def rename_branch(branch_id, owner_id, name, city=""):
    ex("UPDATE branches SET name=?, city=? WHERE id=? AND owner_id=?",
       ((name or "").strip(), (city or "").strip(), branch_id, owner_id))

def delete_branch(branch_id, owner_id):
    # Attached customers/invoices keep their data; they just go back to the main view.
    ex("UPDATE customers SET branch_id='' WHERE owner_id=? AND branch_id=?", (owner_id, branch_id))
    ex("UPDATE invoices SET branch_id='' WHERE owner_id=? AND branch_id=?", (owner_id, branch_id))
    ex("DELETE FROM branches WHERE id=? AND owner_id=?", (branch_id, owner_id))

def assign_branch(owner_id, kind, item_id, branch_id):
    """kind: 'customer' or 'invoice'. Assigning to '' unassigns."""
    if branch_id and not get_branch(branch_id, owner_id):
        return False
    if kind == "customer":
        ex("UPDATE customers SET branch_id=? WHERE id=? AND owner_id=?", (branch_id or "", item_id, owner_id))
    elif kind == "invoice":
        ex("UPDATE invoices SET branch_id=? WHERE id=? AND owner_id=?", (branch_id or "", item_id, owner_id))
    return True

def branch_stats(owner_id):
    """Cross-branch revenue analytics from real invoices and payments."""
    today = time.strftime("%Y-%m-%d")
    out = []
    for b in list_branches(owner_id):
        row = one("""SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt,
            COUNT(CASE WHEN status='paid' THEN 1 END) AS paid_n,
            COALESCE(SUM(CASE WHEN status='paid' THEN amount END),0) AS paid_amt,
            COUNT(CASE WHEN status IN ('pending','overdue') THEN 1 END) AS open_n,
            COUNT(CASE WHEN status='overdue' OR (status='pending' AND due_date < :t) THEN 1 END) AS overdue_n
            FROM invoices WHERE owner_id=:oid AND branch_id=:b""",
            {"oid": owner_id, "b": b["id"], "t": today})
        cust = q("SELECT COUNT(*) AS c FROM customers WHERE owner_id=? AND branch_id=?", (owner_id, b["id"]))[0]["c"]
        out.append({"branch": b, "invoices": row["n"], "invoiced_amt": row["amt"],
                    "paid_n": row["paid_n"], "paid_amt": row["paid_amt"],
                    "open_n": row["open_n"], "overdue_n": row["overdue_n"], "customers": cust})
    return out


# ---- API keys (Enterprise: advanced API integrations) ----
def _hash_key(key):
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()

def list_api_keys(owner_id):
    return q("SELECT id, label, created, last_used, revoked FROM api_keys WHERE owner_id=? ORDER BY created DESC", (owner_id,))

def create_api_key(owner_id, label=""):
    raw = "rsx_" + token(32)
    ex("INSERT INTO api_keys(id,owner_id,key_hash,label,created,revoked) VALUES(?,?,?,?,?,0)",
       (uid(), owner_id, _hash_key(raw), (label or "API key").strip(), now()))
    return raw   # shown once, only its hash is stored

def revoke_api_key(key_id, owner_id):
    ex("UPDATE api_keys SET revoked=1 WHERE id=? AND owner_id=?", (key_id, owner_id))

def owner_for_api_key(raw):
    if not raw:
        return None
    row = one("SELECT owner_id, revoked FROM api_keys WHERE key_hash=?", (_hash_key(raw),))
    if row and not row["revoked"]:
        ex("UPDATE api_keys SET last_used=? WHERE key_hash=?", (now(), _hash_key(raw)))
        return get_user(row["owner_id"])
    return None


# ---- custom automation rules (Enterprise) ----
def list_rules(owner_id):
    return q("SELECT * FROM rules WHERE owner_id=? ORDER BY created DESC", (owner_id,))

def add_rule(owner_id, name, trigger, threshold, action, template=""):
    ok_trigger = trigger in ("invoice_overdue_days", "payment_failed")
    ok_action = action in ("email", "whatsapp", "both")
    if not (name and ok_trigger and ok_action):
        return None
    r = {"id": uid(), "owner_id": owner_id, "name": name.strip(),
         "trigger": trigger, "threshold": float(threshold or 0),
         "action": action, "template": (template or "").strip(),
         "enabled": 1, "created": now()}
    ex("INSERT INTO rules(id,owner_id,name,trigger,threshold,action,template,enabled,created) "
       "VALUES(:id,:owner_id,:name,:trigger,:threshold,:action,:template,:enabled,:created)", r)
    return r

def set_rule(owner_id, rule_id, enabled):
    ex("UPDATE rules SET enabled=? WHERE id=? AND owner_id=?", (1 if enabled else 0, rule_id, owner_id))

def delete_rule(owner_id, rule_id):
    ex("DELETE FROM rules WHERE id=? AND owner_id=?", (rule_id, owner_id))


def rule_matches(owner_id):
    """Rows each rule can act on, from real records only."""
    today = time.strftime("%Y-%m-%d")
    overdue = q("""SELECT i.*, cu.name AS customer_name, cu.email, cu.phone FROM invoices i
        JOIN customers cu ON cu.id=i.customer_id
        WHERE i.owner_id=? AND i.status IN ('pending','overdue') AND i.due_date < ?
        ORDER BY i.due_date ASC""", (owner_id, today))
    for i in overdue:
        try:
            y, mm, d = (int(x) for x in i["due_date"][:10].split("-"))
            i["days_overdue"] = max(0, round((time.mktime(time.strptime(today, "%Y-%m-%d"))
                                              - time.mktime((y, mm, d, 0, 0, 0, 0, 0, -1))) / 86400))
        except Exception:
            i["days_overdue"] = 0
    failed = q("""SELECT e.*, cu.name AS customer_name, cu.email, cu.phone FROM pay_events e
        LEFT JOIN customers cu ON cu.id=e.customer_id
        WHERE e.owner_id=? AND e.kind='failed' AND e.created > ? ORDER BY e.created DESC""",
        (owner_id, now() - 7 * 86400))
    return {"invoice_overdue_days": [dict(i) for i in overdue],
            "payment_failed": [dict(e) for e in failed]}


# ---- enterprise insights + predictive revenue leak ----
def predictive_leak(owner_id):
    """Predictive revenue leak detection: monthly at-risk revenue estimated from
    overdue ageing, failed payments and dormant-customer value. Every input is a
    real record; the forecast is the sum, clearly labelled as an estimate."""
    today_t = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    buckets = {"1_7": 0.0, "8_30": 0.0, "30_plus": 0.0}
    for i in q("""SELECT amount, due_date, status FROM invoices
        WHERE owner_id=? AND status IN ('pending','overdue') AND due_date < ?""",
        (owner_id, time.strftime("%Y-%m-%d"))):
        try:
            y, mm, d = (int(x) for x in i["due_date"][:10].split("-"))
            days = max(0, (today_t - time.mktime((y, mm, d, 0, 0, 0, 0, 0, -1))) / 86400)
        except Exception:
            days = 0
        amt = (i["amount"] or 0) / 100.0
        if days <= 7: buckets["1_7"] += amt
        elif days <= 30: buckets["8_30"] += amt
        else: buckets["30_plus"] += amt
    f30 = one("SELECT COALESCE(SUM(amount),0) AS amt FROM pay_events WHERE owner_id=? AND kind='failed' AND created>?",
              (owner_id, now() - 30 * 86400))
    dormant = one("""SELECT COALESCE(SUM(p.amount),0) AS amt, COUNT(*) AS n FROM customers cu
        JOIN payments p ON p.customer_id=cu.id AND p.status='confirmed'
        WHERE cu.owner_id=? GROUP BY cu.id
        HAVING MAX(p.confirmed_at) < ?""", (owner_id, now() - 45 * 86400))
    # historical recovery ratio: how much of overdue money actually comes back
    rec = one("""SELECT COUNT(*) AS rec_n FROM invoices WHERE owner_id=? AND status='paid'""", (owner_id,))
    total = one("SELECT COUNT(*) AS n FROM invoices WHERE owner_id=?", (owner_id,))
    hist = (rec["rec_n"] / total["n"]) if total["n"] else 0.5
    overdue_sum = sum(buckets.values())
    failed_sum = (f30["amt"] or 0) / 100.0
    dormant_sum = ((dormant["amt"] if dormant else 0) or 0) / 100.0
    at_risk = round(overdue_sum * (1 - min(max(hist, 0.1), 0.9)) + failed_sum * 0.4, 0)
    return {"aging": buckets, "overdue_sum": overdue_sum, "failed_30d": failed_sum,
            "dormant_customers": (dormant["n"] if dormant else 0) or 0, "dormant_value": dormant_sum,
            "historical_recovery_pct": round(hist * 100, 1), "monthly_risk": at_risk}


def daily_insights(owner_id):
    """Advanced AI business assistant: daily insights computed from the real
    database (no invented numbers, no automatic sends)."""
    leak = predictive_leak(owner_id)
    m = owner_metrics(owner_id)
    out = []
    if leak["overdue_sum"] > 0:
        out.append({"title": "Revenue leak forecast",
                    "detail": f"About ₹{leak['monthly_risk']:,.0f} a month is at risk: "
                              f"₹{leak['overdue_sum']:,.0f} overdue (you historically recover "
                              f"{leak['historical_recovery_pct']}%) plus ₹{leak['failed_30d']:,.0f} "
                              f"in failed payments this month."})
    if leak["dormant_customers"] > 0:
        out.append({"title": "Dormant revenue",
                    "detail": f"{leak['dormant_customers']} customer(s) have gone quiet worth "
                              f"₹{leak['dormant_value']:,.0f} in past payments. A reactivation "
                              f"campaign is the usual next step."})
    if m["overdue_count"] > 0:
        top = one("""SELECT i.number, i.amount, cu.name AS cname FROM invoices i
            JOIN customers cu ON cu.id=i.customer_id
            WHERE i.owner_id=? AND i.status IN ('pending','overdue') AND i.due_date < ?
            ORDER BY i.due_date ASC LIMIT 1""", (owner_id, time.strftime("%Y-%m-%d")))
        if top:
            out.append({"title": "Highest-priority recovery",
                        "detail": f"Invoice {top['number']} for {top['cname']} is the oldest overdue "
                                  f"(₹{(top['amount'] or 0)/100:,.0f}). Start there today."})
    if m["recovered"] > 0:
        out.append({"title": "Recovery momentum",
                    "detail": f"₹{m['recovered']/100:,.0f} recovered so far at a "
                              f"{m['success_rate']}% success rate. Keep the follow-up rhythm."})
    return out[:4]


# ---- reliability upgrade: modes, consent, 2FA, verification, export ----

def set_automation_mode(owner_id, mode):
    mode = "approval" if mode == "approval" else "auto"
    ex("UPDATE users SET automation_mode=? WHERE id=?", (mode, owner_id))

def get_automation_mode(owner_id):
    r = one("SELECT automation_mode FROM users WHERE id=?", (owner_id,))
    return (r or {}).get("automation_mode") or "auto"

# ---- TOTP (RFC 6238, stdlib only) ----
import base64, hashlib, hmac as _hmac, secrets as _secrets, struct as _struct

def gen_totp_secret():
    return base64.b32encode(_secrets.token_bytes(20)).decode().rstrip("=")

def totp_code(secret, offset=0):
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret.upper() + pad)
    counter = int(time.time() // 30) + offset
    h = _hmac.new(key, _struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{((_struct.unpack('>I', h[o:o+4])[0] & 0x7FFFFFFF) % 1000000):06d}"

def verify_totp(secret, code):
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != 6:
        return False
    return any(totp_code(secret, off) == code for off in (-1, 0, 1))

def set_totp(owner_id, secret=None, enabled=None):
    if secret is not None:
        ex("UPDATE users SET totp_secret=? WHERE id=?", (secret, owner_id))
    if enabled is not None:
        ex("UPDATE users SET totp_enabled=? WHERE id=?", (1 if enabled else 0, owner_id))

# ---- consent / unsubscribe ----
def ensure_unsub_token(customer_id):
    row = one("SELECT unsub_token FROM customers WHERE id=?", (customer_id,))
    if row and row["unsub_token"]:
        return row["unsub_token"]
    tok = _secrets.token_urlsafe(12)
    ex("UPDATE customers SET unsub_token=? WHERE id=?", (tok, customer_id))
    return tok

def customer_by_unsub_token(token):
    if not token:
        return None
    return one("SELECT * FROM customers WHERE unsub_token=?", (token,))

def set_customer_optout(owner_id, customer_id, email_opt=None, wa_opt=None):
    sets, args = [], []
    if email_opt is not None:
        sets.append("email_opt_out=?"); args.append(1 if email_opt else 0)
    if wa_opt is not None:
        sets.append("wa_opt_out=?"); args.append(1 if wa_opt else 0)
    if not sets:
        return
    args += [owner_id, customer_id]
    ex(f"UPDATE customers SET {', '.join(sets)} WHERE owner_id=? AND id=?", args)

def opt_out_by_token(token):
    """Customer unsubscribes from ALL outreach (their right). Returns customer or None."""
    cu = customer_by_unsub_token(token)
    if not cu:
        return None
    ex("UPDATE customers SET email_opt_out=1, wa_opt_out=1 WHERE id=?", (cu["id"],))
    log_activity(cu["owner_id"], "consent",
                 f"{cu['name']} unsubscribed from all reminders (via unsubscribe link)")
    return dict(cu)

# ---- outbox helpers ----
def list_outbox_status(owner_id, statuses, limit=200):
    marks = ",".join("?" for _ in statuses)
    return q(f"SELECT * FROM outbox WHERE owner_id=? AND status IN ({marks}) ORDER BY created DESC LIMIT ?",
             (owner_id, *statuses, limit))

def outbox_counts(owner_id):
    r = one("""SELECT
      SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
      SUM(CASE WHEN status IN ('queued','pending') THEN 1 ELSE 0 END) AS pending,
      SUM(CASE WHEN status='approval' THEN 1 ELSE 0 END) AS approval,
      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
      SUM(CASE WHEN status='no_channel' THEN 1 ELSE 0 END) AS no_channel
      FROM outbox WHERE owner_id=?""", (owner_id,)) or {}
    return {k: (r.get(k) or 0) for k in ("sent", "pending", "approval", "failed", "no_channel")}

def hold_for_approval(oid, reason="waiting for your approval (Approval Required mode)"):
    ex("UPDATE outbox SET status='approval', error=? WHERE id=?", (reason, oid))
    bind_approval(oid)

def approval_binding(o):
    """The approval is bound to the exact message: channel, recipient, subject,
    body and dedupe key. Any material change to any of them invalidates it."""
    import hashlib
    raw = "|".join([o["channel"] or "", o["dest"] or "", o["subject"] or "",
                    o["body"] or "", o["dedupe"] or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def bind_approval(oid):
    o = get_outbox(oid)
    if o:
        ex("UPDATE outbox SET approval_binding=? WHERE id=?", (approval_binding(o), oid))

def approval_binding_valid(oid):
    o = get_outbox(oid)
    if not o:
        return False
    return bool(o["approval_binding"]) and o["approval_binding"] == approval_binding(o)

def approve_outbox(owner_id, oid):
    o = one("SELECT * FROM outbox WHERE id=? AND owner_id=?", (oid, owner_id))
    if not o or o["status"] != "approval":
        return None
    if not approval_binding_valid(oid):
        ex("UPDATE outbox SET error='approval invalidated: the message changed after it was held' WHERE id=?", (oid,))
        log_activity(owner_id, "automation", f"approval invalidated for {oid}: message changed after it was held")
        return None
    ex("UPDATE outbox SET status='queued', error='' WHERE id=?", (oid,))
    log_activity(owner_id, "automation", f"Approved queued {o['channel']} message to {o['dest']}")
    return dict(o)

def discard_outbox(owner_id, oid):
    o = one("SELECT * FROM outbox WHERE id=? AND owner_id=?", (oid, owner_id))
    if not o or o["status"] != "approval":
        return None
    ex("UPDATE outbox SET status='discarded' WHERE id=?", (oid,))
    log_activity(owner_id, "automation", f"Discarded queued {o['channel']} message to {o['dest']}")
    return dict(o)

# ---- payment claims (verify before reminders stop) ----
def claim_invoice_payment(inv, method="upi", reference="", note=""):
    """Owner reports a payment received; invoice stays unpaid (reminders continue)
    until the claim is verified."""
    pay = {"id": uid(), "owner_id": inv["owner_id"], "invoice_id": inv["id"],
           "customer_id": inv["customer_id"], "amount": inv["amount"], "method": method,
           "status": "claimed", "reference": reference, "note": note,
           "created": now(), "confirmed_at": None}
    ex("INSERT INTO payments(id,owner_id,invoice_id,customer_id,amount,method,status,reference,note,created,confirmed_at) "
       "VALUES(:id,:owner_id,:invoice_id,:customer_id,:amount,:method,:status,:reference,:note,:created,:confirmed_at)", pay)
    return dict(pay)

def verify_claim(payment_id):
    """Verify a claimed payment: only then does the invoice get marked paid and
    reminders stop."""
    pay = one("SELECT * FROM payments WHERE id=? AND status='claimed'", (payment_id,))
    if not pay:
        return None
    inv = one("SELECT * FROM invoices WHERE id=?", (pay["invoice_id"],))
    ex("UPDATE payments SET status='confirmed', confirmed_at=? WHERE id=?", (now(), payment_id))
    if inv and inv["status"] != "paid":
        ex("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (now(), inv["id"]))
    log_activity(pay["owner_id"], "payment",
                 f"Payment verified for invoice {inv['number'] if inv else pay['invoice_id']}; reminders stopped")
    return dict(pay)

def list_claims(owner_id):
    return q("""SELECT p.*, i.number AS inv_number, cu.name AS customer_name
      FROM payments p LEFT JOIN invoices i ON i.id=p.invoice_id
      LEFT JOIN customers cu ON cu.id=p.customer_id
      WHERE p.owner_id=? AND p.status='claimed' ORDER BY p.created DESC LIMIT 50""", (owner_id,))

# ---- alerts (real states only) ----
def owner_alerts(owner_id):
    alerts = []
    t24 = now() - 86400
    f = one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND status='failed' AND created>?", (owner_id, t24))
    if f and f["n"]:
        alerts.append({"kind": "failed", "text": f"{f['n']} message{'s' if f['n']>1 else ''} failed to deliver in the last 24h. Check the automation log."})
    nc = one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND status='no_channel' AND created>?", (owner_id, t24))
    if nc and nc["n"]:
        alerts.append({"kind": "no_channel", "text": f"{nc['n']} message{'s' if nc['n']>1 else ''} are waiting because your email or WhatsApp is not connected (Settings > Your business connection)."})
    ap = one("SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND status='approval'", (owner_id,))
    if ap and ap["n"]:
        alerts.append({"kind": "approval", "text": f"{ap['n']} message{'s' if ap['n']>1 else ''} waiting for your approval."})
    cl = one("SELECT COUNT(*) AS n FROM payments WHERE owner_id=? AND status='claimed'", (owner_id,))
    if cl and cl["n"]:
        alerts.append({"kind": "claim", "text": f"{cl['n']} payment{'s' if cl['n']>1 else ''} reported but not verified yet. Reminders continue until you verify them."})
    if not notify_retailer_email_ready(owner_id):
        alerts.append({"kind": "setup", "text": "Your business email is not connected. Nothing sends until it is (Settings > Your business connection)."})
    return alerts

def notify_retailer_email_ready(owner_id):
    cn = get_connect(owner_id)
    return bool(cn and cn.get("smtp_host") and cn.get("smtp_user") and not cn.get("paused"))

# ---- export / delete ----
def export_owner_data(owner_id):
    u = get_user(owner_id)
    data = {"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "account": {k: u.get(k) for k in ("email","name","business","phone","plan","plan_status","upi_vpa") if u and u.get(k)},
            "customers": q("SELECT * FROM customers WHERE owner_id=?", (owner_id,)),
            "invoices": q("SELECT * FROM invoices WHERE owner_id=?", (owner_id,)),
            "payments": q("SELECT * FROM payments WHERE owner_id=?", (owner_id,)),
            "pay_events": q("SELECT * FROM pay_events WHERE owner_id=?", (owner_id,)),
            "automations": q("SELECT * FROM automations WHERE owner_id=?", (owner_id,)),
            "outbox": q("SELECT * FROM outbox WHERE owner_id=?", (owner_id,)),
            "activities": q("SELECT * FROM activities WHERE owner_id=?", (owner_id,)),
            "branches": q("SELECT * FROM branches WHERE owner_id=?", (owner_id,)),
            "rules": q("SELECT * FROM rules WHERE owner_id=?", (owner_id,)),
            "platform_payments": q("SELECT * FROM plat_pays WHERE owner_id=?", (owner_id,))}
    return data

def delete_owner_data(owner_id):
    for tbl in ("outbox","activities","pay_events","payments","invoices","customers",
                "automations","etemplates","branches","rules","api_keys","plat_pays","connect","support","audit"):
        ex(f"DELETE FROM {tbl} WHERE owner_id=?", (owner_id,))
    ex("DELETE FROM audit WHERE user_id IN (SELECT id FROM users WHERE parent_id=?)", (owner_id,))
    ex("DELETE FROM users WHERE parent_id=?", (owner_id,))
    ex("DELETE FROM usage_counters WHERE owner_id=?", (owner_id,))
    ex("DELETE FROM users WHERE id=?", (owner_id,))

# ---- platform admin overview ----
def admin_overview():
    owners = q("SELECT * FROM users WHERE role='owner' ORDER BY created DESC")
    per_plan = {"starter": 999, "growth": 4999, "enterprise": 5999, "scale": 5999}
    active = [o for o in owners if o.get("plan_status") == "active"]
    mrr = sum(per_plan.get(o.get("plan") or "starter", 0) for o in active)
    rev = one("SELECT COALESCE(SUM(amount),0) AS s FROM plat_pays WHERE status='confirmed'") or {}
    out_today = one("SELECT COUNT(*) AS n FROM outbox WHERE created>? AND status='sent'", (now()-86400,)) or {}
    fails_today = one("SELECT COUNT(*) AS n FROM outbox WHERE created>? AND status='failed'", (now()-86400,)) or {}
    return {"owners": owners, "active_n": len(active), "mrr": mrr,
            "confirmed_revenue": rev.get("s") or 0, "sent_24h": out_today.get("n") or 0,
            "failed_24h": fails_today.get("n") or 0}
