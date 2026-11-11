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
import json, time, os, sqlite3
from . import models, permissions

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

def pilot_metrics() -> dict:
    owners = models.q("SELECT COUNT(*) AS n FROM users WHERE role='owner'")
    invs = models.q("SELECT COUNT(*) AS n FROM invoices")
    failed = models.q("SELECT COUNT(*) AS n FROM pay_events WHERE kind='failed'")
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
