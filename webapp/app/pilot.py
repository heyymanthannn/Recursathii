"""Pilot readiness layer (Phase 5, controlled pilot).

Everything a first pilot needs that the core engine does not already enforce:

- activation gate: customer-contacting automations stay OFF for every new
  business until the owner explicitly authorizes them (confirmation word
  required; recorded in the audit log).
- consent and authorization records: durable rows naming who consented, to
  what, on which channel, with an evidence reference.
- data connection: validated imports of customers, invoices and payments.
  Imported payment rows are marked 'imported' - never 'confirmed' - until
  reconciliation verifies them, so no imported number can ever count as
  recovered revenue by accident.
- pilot monitoring: platform-level metrics for the operator (admin-guarded
  endpoint in main.py, never exposed through business API keys).
- backup verification: quick_check plus row counts of the tables a rollback
  would need.
"""
import json, time, os, sqlite3, uuid, hashlib, pathlib
from . import models, permissions
from .auth import check_pw

CONSENT_KINDS = ("customer_messaging_consent", "owner_authorization",
                 "data_import_permission", "channel_authorization")


def _ensure():
    with models.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS pilot_consents(
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, subject TEXT NOT NULL,
          kind TEXT NOT NULL, channel TEXT DEFAULT '', granted_by TEXT DEFAULT '',
          evidence TEXT DEFAULT '', created REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pilot_imports(
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          rows_in INTEGER, created_n INTEGER, updated_n INTEGER, skipped_n INTEGER,
          errors TEXT DEFAULT '[]', created REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pilot_reauth(
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, code TEXT NOT NULL,
          created REAL, expires REAL, used INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS trust_references(
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, reference TEXT UNIQUE NOT NULL,
          amount INTEGER NOT NULL, currency TEXT DEFAULT 'INR', status TEXT NOT NULL,
          created REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS reference_verifications(
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payment_id TEXT NOT NULL,
          reference TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT DEFAULT '',
          created REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pilot_backups(
          id TEXT PRIMARY KEY, label TEXT, path TEXT, sha256 TEXT,
          counts TEXT DEFAULT '{}', created REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pilot_workspaces(
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, api_key TEXT NOT NULL,
          label TEXT, created REAL)""")


# ---- 1. activation gate ----------------------------------------------------

def get_activation(owner_id) -> dict:
    cfg = permissions.get_config(owner_id)
    return {"automations_enabled": bool(cfg.get("automations_enabled")),
            "authorized_channels": list(cfg.get("authorized_channels") or []),
            "quiet_hours": cfg.get("quiet_hours"),
            "caps": {k: cfg[k] for k in
                     ("max_per_customer_per_day", "max_per_customer_week",
                      "max_per_owner_per_day", "max_per_owner_week")},
            "emergency_paused": bool(cfg.get("emergency_paused"))}


def activate(owner_id, confirm: str) -> dict:
    """Turn customer-contacting automations ON. Requires the exact word
    ACTIVATE as confirmation; anything else is rejected without changes."""
    if confirm != "ACTIVATE":
        raise ValueError("activation needs confirm='ACTIVATE' from the owner")
    cfg = permissions.set_config(owner_id, {"automations_enabled": True})
    models.log_audit(owner_id, "automations_activated", "owner authorization recorded")
    return get_activation(owner_id)


def deactivate(owner_id) -> dict:
    cfg = permissions.set_config(owner_id, {"automations_enabled": False})
    models.log_audit(owner_id, "automations_deactivated", "owner pause")
    return get_activation(owner_id)


# ---- 2. consent and authorization records ----------------------------------

def record_consent(owner_id, subject, kind, channel="", granted_by="", evidence=""):
    _ensure()
    if kind not in CONSENT_KINDS:
        raise ValueError(f"kind must be one of {CONSENT_KINDS}")
    if not (subject or "").strip():
        raise ValueError("subject required (who the record is about)")
    row = {"id": models.uid(), "owner_id": owner_id, "subject": subject.strip(),
           "kind": kind, "channel": (channel or "").strip(),
           "granted_by": (granted_by or "").strip(),
           "evidence": (evidence or "").strip(), "created": models.now()}
    models.ex("INSERT INTO pilot_consents(id,owner_id,subject,kind,channel,granted_by,evidence,created) "
              "VALUES(:id,:owner_id,:subject,:kind,:channel,:granted_by,:evidence,:created)", row)
    models.log_audit(owner_id, "pilot_consent_recorded", json.dumps(
        {"kind": kind, "channel": channel, "subject": subject}))
    return row


def list_consents(owner_id):
    _ensure()
    return models.q("SELECT * FROM pilot_consents WHERE owner_id=? ORDER BY created DESC",
                    (owner_id,))


# ---- 3. data connection: validated, deduplicating imports ------------------

def import_data(owner_id, kind, rows):
    """Import a batch of records with validation and dedupe. Payments import
    as status 'imported' and stay OUT of recovered revenue until
    reconcile_payment verifies them. Returns a per-batch receipt."""
    _ensure()
    if kind not in ("customers", "invoices", "payments"):
        raise ValueError("kind must be customers, invoices or payments")
    rows = rows or []
    created = updated = skipped = 0
    errors = []
    for i, r in enumerate(rows):
        try:
            if kind == "customers":
                if not (r.get("name") or "").strip():
                    raise ValueError("name required")
                existing = None
                if (r.get("email") or "").strip():
                    existing = models.one(
                        "SELECT id FROM customers WHERE owner_id=? AND email=?",
                        (owner_id, r["email"].strip().lower()))
                if not existing and (r.get("phone") or "").strip():
                    existing = models.one(
                        "SELECT id FROM customers WHERE owner_id=? AND phone=?",
                        (owner_id, r["phone"].strip()))
                if existing:
                    models.ex("UPDATE customers SET name=COALESCE(NULLIF(?,''),name) WHERE id=?",
                              (r.get("name", ""), existing["id"]))
                    updated += 1
                else:
                    models.create_customer(owner_id, r["name"], email=r.get("email", ""),
                                           phone=r.get("phone", ""), notes=r.get("notes", ""))
                    created += 1
            elif kind == "invoices":
                amount = int(r.get("amount") or 0)
                if amount <= 0:
                    raise ValueError("amount must be positive")
                cu = models.one("SELECT id FROM customers WHERE id=? AND owner_id=?",
                                (r.get("customer_id") or "", owner_id))
                if not cu:
                    raise ValueError("customer_id missing or not in this business")
                number = (r.get("number") or "").strip()
                dup = models.one("SELECT id FROM invoices WHERE owner_id=? AND number=?",
                                 (owner_id, number)) if number else None
                if dup:
                    skipped += 1
                    continue
                due = (r.get("due_date") or "").strip()
                if due and not _is_date(due):
                    raise ValueError("due_date must be YYYY-MM-DD")
                models.create_invoice(owner_id, cu["id"], number or f"IMP-{models.uid()[:8]}",
                                      amount, r.get("description", ""), due)
                created += 1
            else:  # payments
                inv = models.one("SELECT * FROM invoices WHERE id=? AND owner_id=?",
                                 (r.get("invoice_id") or "", owner_id))
                if not inv:
                    raise ValueError("invoice_id missing or not in this business")
                amount = int(r.get("amount") or 0)
                if amount <= 0:
                    raise ValueError("amount must be positive")
                models.ex("INSERT INTO payments(id,owner_id,invoice_id,customer_id,amount,method,"
                          "status,reference,note,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (models.uid(), owner_id, inv["id"], inv["customer_id"], amount,
                           (r.get("method") or "imported")[:20], "imported", "",
                           "imported, pending reconciliation", models.now()))
                created += 1
        except ValueError as e:
            errors.append({"row": i, "error": str(e)})
            skipped += 1
    models.ex("INSERT INTO pilot_imports(id,owner_id,kind,rows_in,created_n,updated_n,skipped_n,errors,created) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (models.uid(), owner_id, kind, len(rows), created, updated, skipped,
               json.dumps(errors)[:4000], models.now()))
    models.log_audit(owner_id, "pilot_import", json.dumps(
        {"kind": kind, "in": len(rows), "created": created, "updated": updated,
         "skipped": skipped, "errors": len(errors)}))
    return {"kind": kind, "rows_in": len(rows), "created": created,
            "updated": updated, "skipped": skipped, "errors": errors}


def _is_date(s: str) -> bool:
    try:
        time.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def reconcile_payment(owner_id, payment_id, verified: bool, reference=""):
    """Reconciliation is the ONLY path from 'imported' to 'confirmed'. A
    reconcile call without a verification reference leaves the row imported."""
    p = models.one("SELECT * FROM payments WHERE id=? AND owner_id=?", (payment_id, owner_id))
    if not p:
        return None
    if p["status"] not in ("imported", "pending"):
        return {"payment": dict(p), "changed": False,
                "reason": f"status '{p['status']}' is not importable"}
    if not verified or not (reference or "").strip():
        models.log_audit(owner_id, "pilot_reconcile_rejected", json.dumps(
            {"payment_id": payment_id, "reason": "no verification reference"}))
        return {"payment": dict(p), "changed": False,
                "reason": "reconciliation needs verified=true and a reference (bank/UTR/ledger)"}
    models.ex("UPDATE payments SET status='confirmed', reference=?, confirmed_at=? WHERE id=?",
              (reference.strip(), models.now(), payment_id))
    models.log_audit(owner_id, "pilot_payment_reconciled", json.dumps(
        {"payment_id": payment_id, "reference": reference.strip()}))
    return {"payment": models.one("SELECT * FROM payments WHERE id=?", (payment_id,)), "changed": True}


# ---- 4. pilot monitoring (operator view) -----------------------------------

def _ensure_outbox_cols():
    """The delivery-status ladder lives on outbox in deployed schemas; a fresh
    test database may predate it. Additive only, never destructive."""
    models.ex("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, owner_id TEXT, channel TEXT, dest TEXT, tpl TEXT, dedupe TEXT, subject TEXT, body TEXT, status TEXT, error TEXT, created REAL, customer_id TEXT)")
    cols = [r["name"] for r in models.q("PRAGMA table_info(outbox)")]
    if "delivery_status" not in cols:
        models.ex("ALTER TABLE outbox ADD COLUMN delivery_status TEXT")
    if "delivery_detail" not in cols:
        models.ex("ALTER TABLE outbox ADD COLUMN delivery_detail TEXT")


def pilot_metrics() -> dict:
    owners = models.q("SELECT COUNT(*) AS n FROM users WHERE role='owner'")
    invs = models.q("SELECT COUNT(*) AS n FROM invoices")
    failed = models.q("SELECT COUNT(*) AS n FROM pay_events WHERE kind='failed'")
    _ensure_outbox_cols()
    ob = models.q("SELECT status, COALESCE(delivery_status,'') AS ds, COUNT(*) AS n "
                  "FROM outbox GROUP BY status, ds")
    accepted = confirmed = failed_out = unknown = 0
    for r in ob:
        n = int(r["n"] or 0)
        if r["status"] == "accepted" or r["ds"] == "accepted":
            accepted += n
        if r["ds"] in ("delivered", "read"):
            confirmed += n
        if r["ds"] == "failed" or r["status"] == "failed":
            failed_out += n
        if r["ds"] == "unknown" or r["status"] == "unknown":
            unknown += n
    rec = models.q("SELECT COALESCE(SUM(amount),0) AS v, COUNT(*) AS n FROM payments WHERE status='confirmed'")
    react = models.q("SELECT COUNT(*) AS n FROM react_messages")
    appr = models.q("SELECT COUNT(*) AS n FROM outbox WHERE status='approval'")
    errs = models.q("SELECT COUNT(*) AS n FROM outbox_history WHERE status IN ('failed','unknown')")
    imp = models.q("SELECT COUNT(*) AS n FROM payments WHERE status='imported'")
    return {"businesses_onboarded": int(owners[0]["n"]),
            "invoices_monitored": int(invs[0]["n"]),
            "failed_payments_detected": int(failed[0]["n"]),
            "reminders_accepted_by_provider": accepted,
            "confirmed_deliveries": confirmed,
            "failed_or_unknown_deliveries": failed_out + unknown,
            "verified_recovered": {"count": int(rec[0]["n"]), "value": int(rec[0]["v"])},
            "reactivation_messages": int(react[0]["n"]),
            "pending_approvals": int(appr[0]["n"]),
            "automation_errors": int(errs[0]["n"]),
            "payments_awaiting_reconciliation": int(imp[0]["n"]),
            "label": "verified_recovered counts only status='confirmed' payments"}


# ---- 5. backup verification -------------------------------------------------

def backup_check() -> dict:
    db = os.environ.get("PAYRECOVERY_DB", "payrecovery.db")
    out = {"db_file": os.path.basename(db), "exists": os.path.exists(db)}
    if out["exists"]:
        out["bytes"] = os.path.getsize(db)
        con = sqlite3.connect(db)
        out["quick_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
        counts = {}
        for t in ("users", "invoices", "payments", "outbox", "outbox_history", "audit"):
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = "missing"
        con.close()
        out["table_counts"] = counts
    out["note"] = ("Run this against a RESTORED copy of the backup, not only the live "
                   "database, to prove the backup itself is restorable.")
    return out


# ---- 5.1 hardened activation: reauthentication ------------------------------

def request_activation_code(owner_id, password):
    """Owner reauthentication. Verifies the owner's account password, then
    issues a one-time activation code valid for 10 minutes, single use.
    Activation itself needs BOTH this code and the word ACTIVATE."""
    u = models.get_user(owner_id)
    if not u or not check_pw(password or "", u["pw_hash"] or ""):
        models.log_audit(owner_id, "pilot_activation_challenge_denied",
                         "wrong owner password at activation challenge")
        return None
    _ensure()
    code = f"{int.from_bytes(os.urandom(4), 'big') % 1000000:06d}"
    now = models.now()
    models.ex("INSERT INTO pilot_reauth(id,owner_id,code,created,expires) VALUES(?,?,?,?,?)",
              (uuid.uuid4().hex, owner_id, code, now, now + 600))
    models.log_audit(owner_id, "pilot_activation_challenge", "reauthentication code issued")
    return {"code": code, "expires_in_seconds": 600}


def _check_reauth(owner_id, code):
    if not (code or "").strip():
        return "activation needs a fresh reauthentication code from the owner"
    r = models.one("SELECT * FROM pilot_reauth WHERE owner_id=? AND code=? AND used=0",
                   (owner_id, (code or "").strip()))
    if not r:
        return "reauthentication code invalid or already used"
    if models.now() > r["expires"]:
        return "reauthentication code expired; request a new one"
    return None


def activate(owner_id, confirm, code=None):
    """Turn customer-contacting automations ON. Requires the exact word
    ACTIVATE AND a fresh, unused owner reauthentication code. Anything
    missing is rejected without any state change."""
    if confirm != "ACTIVATE":
        raise ValueError("activation needs confirm='ACTIVATE' from the owner")
    err = _check_reauth(owner_id, code)
    if err:
        models.log_audit(owner_id, "pilot_activation_denied", err)
        raise ValueError(err)
    models.ex("UPDATE pilot_reauth SET used=1 WHERE owner_id=? AND code=?", (owner_id, (code or "").strip()))
    cfg = permissions.set_config(owner_id, {"automations_enabled": True})
    models.log_audit(owner_id, "automations_activated",
                     "owner reauthenticated; password + one-time code verified")
    return get_activation(owner_id)


# ---- 5.1 payment reference verification ------------------------------------

def seed_trust_reference(provider, reference, amount, status="captured", currency="INR"):
    """Admin-only seed of the trusted source. In the controlled test
    environment this table stands in for the payment gateway's own records
    (what a live Razorpay API call would return); only the platform admin
    can write here, never a business API key."""
    _ensure()
    if not (reference or "").strip():
        raise ValueError("reference required")
    models.ex("INSERT OR REPLACE INTO trust_references(id,provider,reference,amount,currency,status,created) "
              "VALUES(?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, provider, reference.strip(), int(amount), currency, status, models.now()))


def verify_reference(owner_id, payment_id, reference):
    """Check a manually entered payment reference against the trusted source
    BEFORE it can confirm recovered revenue. Verified requires: the payment
    belongs to this owner, the reference exists in the trusted source with
    status 'captured', and the amount matches the payment exactly."""
    _ensure()
    p = models.one("SELECT * FROM payments WHERE id=? AND owner_id=?", (payment_id, owner_id))
    if not p:
        return {"verified": False, "reason": "payment not found for this business"}
    ref = (reference or "").strip()
    if not ref:
        return {"verified": False, "reason": "reference required"}
    t = models.one("SELECT * FROM trust_references WHERE reference=?", (ref,))
    detail = {"payment_amount": p["amount"]}
    if not t:
        _log_verify(owner_id, payment_id, ref, "rejected", "reference not found in trusted source")
        return {"verified": False, "reason": "reference not found in trusted source; imported stays imported"}
    detail["trusted_amount"] = t["amount"]
    detail["trusted_status"] = t["status"]
    if t["status"] != "captured":
        _log_verify(owner_id, payment_id, ref, "rejected", f"trusted status is '{t['status']}', not captured")
        return {"verified": False, "reason": f"trusted source shows status '{t['status']}', not captured"}
    if int(t["amount"]) != int(p["amount"]):
        _log_verify(owner_id, payment_id, ref, "rejected",
                    f"amount mismatch: payment {p['amount']} vs trusted {t['amount']}")
        return {"verified": False, "reason": f"amount mismatch: payment {p['amount']}, trusted source {t['amount']}"}
    _log_verify(owner_id, payment_id, ref, "verified", json.dumps(detail))
    return {"verified": True, "matched": detail}


def _log_verify(owner_id, payment_id, reference, outcome, detail):
    models.ex("INSERT INTO reference_verifications(id,owner_id,payment_id,reference,outcome,detail,created) "
              "VALUES(?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, owner_id, payment_id, reference, outcome, detail, models.now()))
    models.log_audit(owner_id, f"pilot_reference_{outcome}", json.dumps(
        {"payment_id": payment_id, "reference": reference, "detail": detail}))


def reconcile_payment(owner_id, payment_id, verified=None, reference=""):
    """Reconciliation is the ONLY path from 'imported' to 'confirmed', and it
    now REQUIRES the reference to verify against the trusted source first.
    A match confirms once; anything else leaves the row imported."""
    p = models.one("SELECT * FROM payments WHERE id=? AND owner_id=?", (payment_id, owner_id))
    if not p:
        return None
    if p["status"] not in ("imported", "pending"):
        return {"payment": dict(p), "changed": False,
                "reason": f"status '{p['status']}' is not importable"}
    ref = (reference or "").strip()
    if not ref:
        models.log_audit(owner_id, "pilot_reconcile_rejected", json.dumps(
            {"payment_id": payment_id, "reason": "no verification reference"}))
        return {"payment": dict(p), "changed": False,
                "reason": "reconciliation needs a reference checked against the trusted source"}
    v = verify_reference(owner_id, payment_id, ref)
    if not v.get("verified"):
        return {"payment": dict(p), "changed": False, "reason": v["reason"]}
    if p["status"] == "confirmed":
        return {"payment": dict(p), "changed": False, "reason": "already confirmed"}
    models.ex("UPDATE payments SET status='confirmed', reference=?, confirmed_at=? WHERE id=?",
              (ref, models.now(), payment_id))
    models.log_audit(owner_id, "pilot_payment_reconciled", json.dumps(
        {"payment_id": payment_id, "reference": ref, "verified_against": "trusted_source"}))
    return {"payment": models.one("SELECT * FROM payments WHERE id=?", (payment_id,)), "changed": True}


# ---- 5.1 backup + rollback -------------------------------------------------

BACKUP_TABLES = ("users", "customers", "invoices", "payments", "outbox",
                 "pilot_consents", "trust_references", "reference_verifications")


def create_backup(label=""):
    """Full SQLite backup via the online backup API, hashed and counted.
    Pilot records are NEVER dropped; this backup is what rollback restores."""
    _ensure()
    bdir = pathlib.Path(os.environ.get("PILOT_BACKUP_DIR", os.path.join(os.path.dirname(__file__), "..", "backups")))
    bdir.mkdir(parents=True, exist_ok=True)
    bid = uuid.uuid4().hex
    path = str(bdir / f"pilot-backup-{bid}.db")
    src = models.conn()
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    counts = {}
    with models.conn() as c:
        for t in BACKUP_TABLES:
            counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    models.ex("INSERT INTO pilot_backups(id,label,path,sha256,counts,created) VALUES(?,?,?,?,?,?)",
              (bid, label or "manual", path, sha, json.dumps(counts), models.now()))
    models.log_audit("platform", "pilot_backup_created", json.dumps({"id": bid, "sha256": sha[:12]}))
    return {"id": bid, "path": path, "sha256": sha, "counts": counts}


def verify_backup(backup_id):
    """Restore the backup into a scratch copy, run quick_check on it, and
    compare every table count against the live database."""
    _ensure()
    b = models.one("SELECT * FROM pilot_backups WHERE id=?", (backup_id,))
    if not b:
        return None
    ok_sha = hashlib.sha256(open(b["path"], "rb").read()).hexdigest() == b["sha256"]
    tmp = b["path"] + ".verify"
    sqlite3.connect(tmp).close() if False else None
    import shutil
    shutil.copyfile(b["path"], tmp)
    con = sqlite3.connect(tmp)
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    live_counts, bak_counts, diffs = {}, {}, []
    with models.conn() as c:
        for t in BACKUP_TABLES:
            live_counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    for t in BACKUP_TABLES:
        try:
            bak_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            bak_counts[t] = None
        if bak_counts[t] != live_counts[t]:
            diffs.append({"table": t, "backup": bak_counts[t], "live": live_counts[t]})
    con.close()
    os.remove(tmp)
    result = {"id": backup_id, "sha256_ok": ok_sha, "quick_check": quick,
              "counts_match": not diffs, "diffs": diffs, "verified": ok_sha and quick == "ok" and not diffs}
    models.log_audit("platform", "pilot_backup_verified", json.dumps(
        {"id": backup_id, "verified": result["verified"]}))
    return result


def rollback_check():
    """Rollback safety review: no code path drops pilot tables, and the
    procedure preserves every record."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for fn in sorted(os.listdir(app_dir)):
        if fn.endswith(".py"):
            txt = open(os.path.join(app_dir, fn)).read()
            if ("DROP" + " TABLE") in txt:
                offenders.append(fn)
    return {"drop_table_in_app_code": offenders,
            "procedure": ["pause automations (emergency pause or set automations_enabled=false)",
                          "create_backup() with a label",
                          "verify_backup() must report verified=true",
                          "revert code commits only; data tables are never dropped",
                          "re-run backup_check() and record the outcome"]}


def backup_check():
    """Extended readiness check: integrity, row counts, and the latest
    backup's verification status."""
    base = _backup_check_base()
    b = models.one("SELECT * FROM pilot_backups ORDER BY created DESC LIMIT 1")
    base["latest_backup"] = ({"id": b["id"], "sha256": b["sha256"][:12], "created": b["created"],
                              "verified": verify_backup(b["id"])["verified"]} if b else None)
    return base


# ---- 5.1 three isolated workspaces -----------------------------------------

def prepare_pilots(count=3):
    """Prepare isolated pilot workspaces: one owner + API key each,
    automations OFF by default, no shared records."""
    _ensure()
    made = []
    existing = models.q("SELECT COUNT(*) AS n FROM pilot_workspaces")
    start = existing[0]["n"] if existing else 0
    for i in range(start + 1, start + count + 1):
        email = f"pilot{i}@pilot.invalid"
        if models.get_user_by_email(email):
            u = models.get_user_by_email(email)
        else:
            u = models.create_user(email, "x", f"Pilot {i} Owner", f"Pilot Business {i}",
                                   "90000000%02d" % i, role="owner")
        key = models.create_api_key(u["id"], "pilot")
        models.ex("INSERT INTO pilot_workspaces(id,owner_id,api_key,label,created) VALUES(?,?,?,?,?)",
                  (uuid.uuid4().hex, u["id"], key, f"Pilot workspace {i}", models.now()))
        made.append({"workspace": f"Pilot workspace {i}", "owner_id": u["id"],
                     "automations_enabled": get_activation(u["id"])["automations_enabled"]})
    return {"created": len(made), "workspaces": made}


# ---- 5.1 compliance documentation export -----------------------------------

def compliance_export(owner_id):
    """Everything a pilot's consent file needs, in one place: activation
    history, owner authorizations, channel authorizations, consents."""
    _ensure()
    cons = list_consents(owner_id)
    audit_rows = models.q("SELECT action, detail, created FROM audit WHERE user_id=? "
                          "AND action LIKE 'pilot_%' OR (user_id=? AND action LIKE 'automations_%') "
                          "ORDER BY created", (owner_id, owner_id))
    cfg = permissions.get_config(owner_id)
    return {"owner_id": owner_id,
            "activation_state": get_activation(owner_id),
            "authorized_channels": cfg.get("authorized_channels") or [],
            "consents": [dict(c) for c in cons],
            "audit_trail": [dict(a) for a in audit_rows]}


def _backup_check_base():
    out = {"quick_check": None, "tables": {}}
    with models.conn() as c:
        out["quick_check"] = c.execute("PRAGMA quick_check").fetchone()[0]
        for t in BACKUP_TABLES:
            out["tables"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out
