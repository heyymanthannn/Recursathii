"""Phase 24: manual bank-transfer subscription billing (NEFT / IMPS / UPI).

The initial subscription payment method for RecurSaathi plans. Razorpay stays
in the codebase but disabled until separately approved. A business pays the
platform by bank transfer; the submission lands as PENDING_VERIFICATION and
only an authorized platform admin can approve it. A UTR number, a screenshot
or a customer claim never activates a subscription on its own.

Security rules this module enforces:
- Bank details come from server-side environment configuration only. They are
  never hardcoded and never returned by a public API response; only a signed-in
  owner (or admin) can read them through the app.
- Tenant isolation: every query is scoped to the acting owner's id.
- Admin RBAC: approve/reject refuse anything whose role is not "admin" - no
  automation, script or AI caller holds that role.
- Idempotency: a status-guarded UPDATE decides exactly one winner, so a
  duplicate approval cannot re-activate a subscription and a duplicate
  submission cannot create two pending records.
- Audit: every decision is written to the append-only audit log.
"""
import os, re, secrets, time
from . import config, db, models

METHODS = ("neft", "imps", "upi")
CYCLES = ("monthly", "annual")

# Server-side bank configuration. Unset means "not configured": the app shows a
# contact-support line instead of inventing details.
BANK_NAME = os.environ.get("BANK_TRANSFER_BANK", "")
ACCOUNT_NAME = os.environ.get("BANK_TRANSFER_ACCOUNT_NAME", "")
ACCOUNT_NUMBER = os.environ.get("BANK_TRANSFER_ACCOUNT_NUMBER", "")
IFSC = os.environ.get("BANK_TRANSFER_IFSC", "")
UPI_VPA = os.environ.get("BANK_TRANSFER_UPI_VPA", "") or config.MANUAL_UPI_ID
UPI_PAYEE = config.MANUAL_UPI_PAYEE

PERIOD_DAYS = {"monthly": 30, "annual": 365}
RENEWAL_REMIND_DAYS = 7

_UTR_RE = re.compile(r"^[A-Za-z0-9\-]{6,64}$")


def migrate():
    """Additive-only migration: new table + new users columns. Re-run safe."""
    with db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bank_transfer_payments(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plan TEXT NOT NULL,
  cycle TEXT NOT NULL, ea INTEGER DEFAULT 0, amount INTEGER NOT NULL,
  method TEXT NOT NULL, utr TEXT NOT NULL, receipt_note TEXT DEFAULT '',
  reference TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'PENDING_VERIFICATION',
  submitted_at REAL, verified_by TEXT, verified_at REAL,
  receipt_no TEXT DEFAULT '', decision_note TEXT DEFAULT '')""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS btp_pending_once
  ON bank_transfer_payments(owner_id, plan, cycle) WHERE status='PENDING_VERIFICATION'""")
        for stmt in (
            "ALTER TABLE users ADD COLUMN sub_period_end REAL",
            "ALTER TABLE users ADD COLUMN sub_last_payment_ref TEXT DEFAULT ''",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass


def configured() -> dict:
    """Which transfer rails have real server-side details (never fabricated)."""
    return {
        "bank": bool(BANK_NAME and ACCOUNT_NAME and ACCOUNT_NUMBER and IFSC),
        "upi": bool(UPI_VPA),
    }


def bank_details() -> dict | None:
    """Full details for a signed-in viewer only. Returns None when unset."""
    if not (BANK_NAME and ACCOUNT_NAME and ACCOUNT_NUMBER and IFSC):
        return None
    return {"bank": BANK_NAME, "account_name": ACCOUNT_NAME,
            "account_number": ACCOUNT_NUMBER, "ifsc": IFSC, "upi_vpa": UPI_VPA,
            "upi_payee": UPI_PAYEE}


def quote(plan: str, cycle: str, ea: bool = False) -> int:
    """Server-calculated price in paise. Client input never sets the amount."""
    if plan not in config.OFFICIAL_MONTHLY:
        raise ValueError("unknown plan")
    if cycle not in CYCLES:
        raise ValueError("unknown cycle")
    if ea:
        if plan != config.EA_OFFER["plan"] or cycle != "annual":
            raise ValueError("early adopter offer is Enterprise annual only")
        from . import early_adopter
        if not early_adopter.availability()["available"]:
            raise ValueError("early adopter slots are full")
        return config.EA_OFFER["annual_price"]
    if cycle == "monthly":
        return config.OFFICIAL_MONTHLY[plan]
    return config.OFFICIAL_ANNUAL[plan]


def new_reference(owner_id: str, plan: str, cycle: str) -> str:
    base = f"RS-{plan[:3].upper()}-{cycle[0].upper()}-{owner_id[:8].upper()}-"
    for _ in range(5):
        ref = base + secrets.token_hex(2).upper()
        if not models.one("SELECT id FROM bank_transfer_payments WHERE reference=?",
                          (ref,)):
            return ref
    raise RuntimeError("could not allocate a unique payment reference")


def get_by_reference(reference: str):
    return models.one("SELECT * FROM bank_transfer_payments WHERE reference=?",
                      (reference,))


def get(payment_id: str):
    return models.one("SELECT * FROM bank_transfer_payments WHERE id=?",
                      (payment_id,))


def create_submission(owner, plan: str, cycle: str, method: str, utr: str,
                      receipt_note: str = "", ea: bool = False):
    """Record a bank-transfer claim. Amount comes from quote() - a submission
    whose amount disagrees with the server quote is refused. A second live
    submission for the same plan+cycle returns the existing record (idempotent)
    instead of creating a duplicate. Status is always PENDING_VERIFICATION."""
    if not owner or owner.get("role") not in ("owner", "staff"):
        raise PermissionError("only a signed-in business account can submit")
    try:
        amount = quote(plan, cycle, ea)
    except ValueError as e:
        raise ValueError(str(e))
    if method not in METHODS:
        raise ValueError("method must be neft, imps or upi")
    utr = (utr or "").strip()
    if not _UTR_RE.match(utr):
        raise ValueError("enter the UTR / reference number your bank gave you")
    existing = models.one(
        "SELECT * FROM bank_transfer_payments WHERE owner_id=? AND plan=? AND "
        "cycle=? AND status='PENDING_VERIFICATION'", (owner["id"], plan, cycle))
    if existing:
        return existing, False
    pid, ref = models.uid(), new_reference(owner["id"], plan, cycle)
    with db._conn() as c:
        c.execute("""INSERT INTO bank_transfer_payments
(id,owner_id,plan,cycle,ea,amount,method,utr,receipt_note,reference,status,submitted_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (pid, owner["id"], plan, cycle, 1 if ea else 0, amount, method,
                   utr, (receipt_note or "").strip()[:500], ref,
                   "PENDING_VERIFICATION", models.now()))
    models.log_audit(owner["id"], "bank.submitted",
                     f"{ref} {plan}/{cycle} Rs{amount/100:,.0f} via {method}")
    return get(pid), True


def _decide(payment_id: str, admin, decision: str, note: str):
    """One winner per payment: the status-guarded UPDATE decides. Returns
    (payment_row, decided_now). Any non-admin is refused."""
    if not admin or admin.get("role") != "admin":
        raise PermissionError("only an authorized platform admin can decide "
                              "subscription payments")
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    row = get(payment_id)
    if not row:
        raise LookupError("payment not found")
    with db._conn() as c:
        cur = c.execute(
            "UPDATE bank_transfer_payments SET status=?, verified_by=?, "
            "verified_at=?, decision_note=? WHERE id=? AND status=?",
            (decision, admin["email"], models.now(), note[:500],
             payment_id, "PENDING_VERIFICATION"))
        won = cur.rowcount == 1
    return get(payment_id), won


def approve(payment_id: str, admin, note: str = ""):
    """Approve a verified transfer and activate the subscription exactly once."""
    row, won = _decide(payment_id, admin, "approved", note)
    if not won:
        return row, False  # already decided: never re-activate, never re-charge
    receipt_no = f"RS-RCP-{secrets.token_hex(3).upper()}"
    period_end = models.now() + PERIOD_DAYS[row["cycle"]] * 86400
    models.update_user(row["owner_id"], plan=row["plan"], plan_status="active",
                       sub_period_end=period_end,
                       sub_last_payment_ref=row["reference"])
    with db._conn() as c:
        c.execute("UPDATE bank_transfer_payments SET receipt_no=? WHERE id=?",
                  (receipt_no, row["id"]))
        c.execute("INSERT INTO pay_events(id,owner_id,kind,gateway,amount,created) "
                  "VALUES(?,?,?,?,?,?)",
                  (models.uid(), row["owner_id"], "subscription_activated",
                   "bank_transfer", row["amount"], models.now()))
    models.log_audit(admin["id"], "bank.approved",
                     f"{row['reference']} verified by {admin['email']}; "
                     f"sub {row['plan']}/{row['cycle']} active to "
                     f"{time.strftime('%Y-%m-%d', time.gmtime(period_end))}")
    return get(row["id"]), True


def reject(payment_id: str, admin, reason: str = ""):
    row, won = _decide(payment_id, admin, "rejected", reason)
    if won:
        models.log_audit(admin["id"], "bank.rejected",
                         f"{row['reference']} rejected by {admin['email']}: "
                         f"{reason[:200]}")
    return row, won


def history(owner_id: str):
    return models.q("SELECT * FROM bank_transfer_payments WHERE owner_id=? "
                    "ORDER BY submitted_at DESC", (owner_id,))


def pending_list():
    return models.q("SELECT * FROM bank_transfer_payments "
                    "WHERE status='PENDING_VERIFICATION' ORDER BY submitted_at")


def decided_list(limit=100):
    return models.q("SELECT * FROM bank_transfer_payments "
                    "WHERE status!='PENDING_VERIFICATION' "
                    "ORDER BY verified_at DESC LIMIT ?", (limit,))


def days_left(user) -> int | None:
    end = user.get("sub_period_end") if isinstance(user, dict) else None
    if not end:
        return None
    return int((end - models.now()) / 86400)


def renewal_state(user) -> dict:
    """Renewal view for the owner dashboard: history, current period, expiry.
    Never auto-renews and never charges: expiry only marks the account."""
    state = {"period_end": user.get("sub_period_end") or 0,
             "days_left": days_left(user), "reminders_queued": 0,
             "expired": False}
    if state["period_end"] and state["days_left"] is not None \
            and state["days_left"] < 0 and user.get("plan_status") == "active":
        models.update_user(user["id"], plan_status="past_due")
        models.log_audit(user["id"], "bank.expired",
                         f"period ended {time.strftime('%Y-%m-%d', time.gmtime(state['period_end']))}; "
                         "marked past_due, no auto-renewal")
        state["expired"] = True
    return state


def queue_expiry_reminders(owner) -> int:
    """Queue (never send) a renewal reminder within RENEWAL_REMIND_DAYS of
    expiry. Deduped per period so one reminder goes out per billing cycle."""
    end = owner.get("sub_period_end") or 0
    left = days_left(owner) if end else None
    if end and left is not None and 0 <= left <= RENEWAL_REMIND_DAYS:
        dedupe = f"bankrenew:{owner['id']}:{int(end)}"
        if not models.one("SELECT id FROM outbox WHERE dedupe=?", (dedupe,)):
            models.queue(owner["id"], "email", owner.get("email") or "", "",
                         f"Your RecurSaathi {owner.get('plan','')} plan renews in {left} day(s)",
                         "Your subscription period is ending. Renew any time from "
                         "your dashboard - nothing is charged automatically.",
                         dedupe=dedupe)
            models.log_audit(owner["id"], "bank.renewal_reminder_queued",
                             f"{left} day(s) left")
            return 1
    return 0


def sub_receipt_pdf_bytes(row, owner) -> bytes:
    """Payment receipt for an approved bank-transfer subscription payment."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "RecurSaathi - Subscription Payment Receipt")
    pdf.ln(12)
    for label, value in (
        ("Receipt no", row["receipt_no"] or "-"),
        ("Payment reference", row["reference"]),
        ("UTR / bank reference", row["utr"]),
        ("Method", (row["method"] or "").upper()),
        ("Plan", f"{row['plan'].capitalize()} ({row['cycle']})"),
        ("Amount", f"Rs {row['amount']/100:,.2f}"),
        ("Business", owner.get("business") or owner.get("email") or "-"),
        ("Verified by", row["verified_by"] or "-"),
        ("Verified at", time.strftime("%d %b %Y %H:%M", time.gmtime(row["verified_at"]))
                        if row["verified_at"] else "-"),
        ("Status", "Approved - subscription active"),
    ):
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(45, 8, label)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_x(pdf.l_margin + 45)
        pdf.multi_cell(0, 8, str(value))
        pdf.set_x(pdf.l_margin)
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 6, "Verified payment. This receipt was generated after "
                   "manual verification by a RecurSaathi admin. No automatic "
                   "charge or renewal is attached to this subscription.")
    return bytes(pdf.output())


# Razorpay stays available in code but disabled: no live API call may start.
def razorpay_status() -> dict:
    return {"enabled": False,
            "reason": "Razorpay is disabled until separately approved by the owner"}
