"""Phase 16: Early Adopter Enterprise offer (dev-only, sandbox providers).

Offer terms (owner-approved Nov 15, 2026): the first 10 eligible paying
Enterprise businesses get their first 12 months at Rs 79,990 instead of the
regular annual Rs 99,990 (Rs 20,000 off). One offer per business. Renewal
after the first year uses the then-current regular annual price, with
advance notice and an explicit customer action (a new verified payment) -
never a silent charge.

Safety rules enforced here:
- Slot accounting is server-side and database-backed; the frontend can
  never grant, reserve or extend a slot.
- Allocation is atomic (BEGIN IMMEDIATE): two businesses racing for the
  last slot cannot both win.
- A reservation expires (30 minutes) and releasing it frees the slot;
  only a verified payment activates and permanently consumes a slot.
- Abandoned/expired/cancelled checkouts never consume a slot.
- One offer per business, enforced by a UNIQUE owner constraint plus the
  state machine (activated = permanent).
- Activation requires a signature-verified webhook event whose amount
  matches the recorded price exactly; screenshots, UTR strings and
  frontend success messages are never trusted.
- Webhook processing is idempotent (unique event id; duplicates are
  detected and ignored).
- Subscription prices are snapshotted per subscription; later config
  changes never rewrite history.
"""
import hashlib
import json
import time
from datetime import datetime, timezone

from . import models, config

EA_ANNUAL_PAISE = config.EA_OFFER["annual_price"]      # Rs 79,990 first 12 months
EA_MAX_SLOTS = config.EA_OFFER["max_slots"]
EA_MONTHS = config.EA_OFFER["months"]
RESERVATION_TTL_S = config.EA_OFFER["reservation_ttl_s"]  # reservation hold before it expires

SUB_STATES = ("trialing", "pending_payment", "active", "payment_failed",
              "past_due", "canceled", "expired", "refund_pending", "refunded")

RENEWAL_NOTICE_DAYS = 30


def _ensure():
    """Own this module's tables (lazy, additive; same pattern as checkout).
    Safe on a fresh database and on an existing one: CREATE IF NOT EXISTS
    only, nothing renamed or dropped, all existing rows preserved."""
    with models.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS ea_reservations(
          id TEXT PRIMARY KEY, owner_id TEXT UNIQUE NOT NULL, business TEXT,
          status TEXT DEFAULT 'reserved', reserved_at REAL, expires_at REAL,
          activated_at REAL, amount INTEGER, checkout_ref TEXT,
          activated_payment TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS subscriptions(
          id TEXT PRIMARY KEY, owner_id TEXT UNIQUE NOT NULL, plan TEXT,
          cycle TEXT, status TEXT DEFAULT 'pending_payment', price INTEGER,
          regular_renewal_price INTEGER, is_early_adopter INTEGER DEFAULT 0,
          term_start REAL, term_end REAL, renewal_notified_at REAL,
          payment_ref TEXT, provider_sub_id TEXT, created REAL, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS billing_events(
          id TEXT PRIMARY KEY, provider TEXT, event_id TEXT UNIQUE, event TEXT,
          payload TEXT, handled INTEGER DEFAULT 0, result TEXT, created REAL)""")
        _cols = [r[1] for r in c.execute("PRAGMA table_info(ea_reservations)")]
        for _col, _ddl in (("first_year_start", "REAL"), ("first_year_end", "REAL")):
            if _col not in _cols:
                c.execute(f"ALTER TABLE ea_reservations ADD COLUMN {_col} {_ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ea_status ON ea_reservations(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subs_status ON subscriptions(status)")
        c.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                  "VALUES('0003-phase16-early-adopter', ?)", (models.now(),))


def add_one_year(ts):
    """Exactly one calendar year, leap-year safe: Feb 29 -> Feb 28."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        d2 = d.replace(year=d.year + 1)
    except ValueError:            # Feb 29 on a non-leap target year
        d2 = d.replace(year=d.year + 1, day=28)
    return d2.timestamp()


# ---------- availability ----------

def availability():
    """Genuine server-side availability. issued = paid and activated.
    reserved-live holds (unexpired) count against the 10 so two buyers
    cannot both see 'available' for the final slot."""
    _ensure()
    release_expired()
    used = models.one(
        "SELECT COUNT(*) AS n FROM ea_reservations WHERE status='activated' "
        "OR (status='reserved' AND expires_at>?)", (models.now(),))["n"]
    issued = models.one(
        "SELECT COUNT(*) AS n FROM ea_reservations WHERE status='activated'")["n"]
    reserved_live = used - issued
    remaining = max(0, EA_MAX_SLOTS - used)
    return {"max": EA_MAX_SLOTS, "issued": issued, "reserved": reserved_live,
            "used": used, "remaining": remaining, "available": remaining > 0,
            "annual_price": EA_ANNUAL_PAISE,
            "regular_annual_price": config.PLANS["enterprise"]["annual_amount"]}


def release_expired():
    with models.conn() as c:
        c.execute("UPDATE ea_reservations SET status='expired' "
                  "WHERE status='reserved' AND expires_at<=?", (models.now(),))


# ---------- reservation (atomic first-10 claim) ----------

def reserve(owner, checkout_ref=""):
    """Atomically claim one of the 10 slots for this business. Returns
    (ok, reservation_or_reason). Idempotent for the same live reservation.
    An expired or cancelled earlier reservation can be re-reserved (the
    slot it once held was already freed by release_expired)."""
    _ensure()
    release_expired()
    owner_id = owner["id"]
    c = models.conn()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        now = models.now()
        # The caller's own row is checked FIRST: a retry of a live reservation
        # is idempotent (no new slot), and a spent one never re-grants.
        row = c.execute("SELECT * FROM ea_reservations WHERE owner_id=?",
                        (owner_id,)).fetchone()
        if row is not None:
            row = dict(row)
            if row["status"] == "activated":
                c.execute("COMMIT")
                return False, "already_claimed"
            if row["status"] == "reserved" and row["expires_at"] > now:
                c.execute("COMMIT")
                return True, row          # same live reservation, retry-safe
        used = c.execute(
            "SELECT COUNT(*) FROM ea_reservations WHERE status='activated' "
            "OR (status='reserved' AND expires_at>?)", (now,)).fetchone()[0]
        if used >= EA_MAX_SLOTS:
            c.execute("COMMIT")
            return False, "sold_out"
        if row is not None:
            # expired/cancelled: re-reserve the same row (slot was freed)
            c.execute("UPDATE ea_reservations SET status='reserved', "
                      "reserved_at=?, expires_at=?, amount=?, checkout_ref=? "
                      "WHERE id=?", (now, now + RESERVATION_TTL_S,
                                     EA_ANNUAL_PAISE, checkout_ref, row["id"]))
            rid = row["id"]
        else:
            rid = models.uid()
            c.execute("INSERT INTO ea_reservations(id,owner_id,business,status,"
                      "reserved_at,expires_at,amount,checkout_ref) "
                      "VALUES(?,?,?,'reserved',?,?,?,?)",
                      (rid, owner_id, owner.get("business") or "", now,
                       now + RESERVATION_TTL_S, EA_ANNUAL_PAISE, checkout_ref))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        c.close()
    models.log_audit(owner_id, "ea.reserve",
                     f"slot reserved for {EA_MONTHS} months at Rs 79,990 (ref {checkout_ref or '-'})")
    return True, models.one("SELECT * FROM ea_reservations WHERE id=?", (rid,))


def reservation(owner_id):
    _ensure()
    return models.one("SELECT * FROM ea_reservations WHERE owner_id=?", (owner_id,))


# ---------- subscription records ----------

def get_subscription(owner_id):
    _ensure()
    return models.one("SELECT * FROM subscriptions WHERE owner_id=?", (owner_id,))


def _upsert_subscription(owner_id, *, plan, cycle, status, price,
                         regular_renewal_price, is_early_adopter,
                         term_start=None, term_end=None, payment_ref="",
                         provider_sub_id="", update_pricing=True):
    """update_pricing=True is a NEW checkout: it writes the new term's price
    snapshot and clears term dates until activation. Config changes never
    rewrite an existing subscription's recorded price; nothing else calls
    this with update_pricing=True."""
    now = models.now()
    existing = get_subscription(owner_id)
    if existing is None:
        models.ex("INSERT INTO subscriptions(id,owner_id,plan,cycle,status,price,"
                  "regular_renewal_price,is_early_adopter,term_start,term_end,"
                  "payment_ref,provider_sub_id,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (models.uid(), owner_id, plan, cycle, status, price,
                   regular_renewal_price, 1 if is_early_adopter else 0,
                   term_start, term_end, payment_ref, provider_sub_id, now, now))
    elif update_pricing:
        models.ex("UPDATE subscriptions SET plan=?, cycle=?, status=?, price=?, "
                  "regular_renewal_price=?, is_early_adopter=?, term_start=?, "
                  "term_end=?, renewal_notified_at=NULL, payment_ref=?, "
                  "provider_sub_id=?, updated=? WHERE owner_id=?",
                  (plan, cycle, status, price, regular_renewal_price,
                   1 if is_early_adopter else 0, term_start, term_end,
                   payment_ref, provider_sub_id, now, owner_id))
    else:
        models.ex("UPDATE subscriptions SET plan=?, cycle=?, status=?, updated=? "
                  "WHERE owner_id=?",
                  (plan, cycle, status, now, owner_id))
    return get_subscription(owner_id)


def open_checkout(owner, *, plan, cycle, price, regular_renewal_price,
                  is_early_adopter, payment_ref):
    """Record the checkout (pending payment) with the exact price snapshot.
    Activation happens only through a verified payment later."""
    _ensure()
    sub = _upsert_subscription(
        owner["id"], plan=plan, cycle=cycle, status="pending_payment",
        price=price, regular_renewal_price=regular_renewal_price,
        is_early_adopter=is_early_adopter, payment_ref=payment_ref)
    models.log_audit(owner["id"], "billing.checkout_opened",
                     f"{plan}/{cycle}{' early-adopter' if is_early_adopter else ''} "
                     f"price {price} pending payment (ref {payment_ref})")
    return sub


def activate(owner_id, *, amount, payment_ref, provider_sub_id=""):
    """Activate a pending checkout after a VERIFIED payment of exactly the
    recorded price. Sets term dates, and for early adopters freezes the
    first-year price and the regular renewal price separately."""
    _ensure()
    sub = get_subscription(owner_id)
    if sub is None:
        return False, "no_pending_checkout"
    if sub["status"] not in ("pending_payment", "payment_failed"):
        return False, f"not_activatable({sub['status']})"
    if amount != sub["price"]:
        return False, f"amount_mismatch(got {amount}, expected {sub['price']})"
    now = models.now()
    term_end = add_one_year(now) if sub["cycle"] == "annual" else now + 31 * 86400
    models.ex("UPDATE subscriptions SET status='active', term_start=?, term_end=?, "
              "payment_ref=?, provider_sub_id=?, renewal_notified_at=NULL, updated=? "
              "WHERE owner_id=?",
              (now, term_end, payment_ref or sub["payment_ref"],
               provider_sub_id or sub["provider_sub_id"], now, owner_id))
    # The user's plan gate reads plan_status: activation puts the business
    # live, and the verified payment confirms its pending subscription payment.
    models.update_user(owner_id, plan_status="active")
    models.ex("UPDATE plat_pays SET status='confirmed' WHERE owner_id=? "
              "AND status='pending' AND reference=?",
              (owner_id, payment_ref or sub["payment_ref"]))
    if sub["is_early_adopter"]:
        with models.conn() as c:
            c.execute("UPDATE ea_reservations SET status='activated', activated_at=?, "
                      "activated_payment=?, first_year_start=?, first_year_end=? "
                      "WHERE owner_id=? AND status IN ('reserved','expired')",
                      (now, payment_ref, now, term_end, owner_id))
    models.log_audit(owner_id, "billing.activated",
                     f"{sub['plan']}/{sub['cycle']} activated at {sub['price']} "
                     f"(payment {payment_ref or '-'})")
    return True, get_subscription(owner_id)


def set_status(owner_id, status, note=""):
    assert status in SUB_STATES, status
    _ensure()
    models.ex("UPDATE subscriptions SET status=?, updated=? WHERE owner_id=?",
              (status, models.now(), owner_id))
    models.log_audit(owner_id, "billing.status", f"{status}" + (f" ({note})" if note else ""))
    return get_subscription(owner_id)


# ---------- verified webhook processing (idempotent) ----------

def _sub_by_ref(ref):
    """Find a subscription by its checkout reference, or through the payment
    record that reference belongs to (older checkouts correlate there)."""
    if not ref:
        return None
    sub = models.one("SELECT * FROM subscriptions WHERE payment_ref=?", (ref,))
    if sub is None:
        pay = models.one("SELECT * FROM plat_pays WHERE reference=?", (ref,))
        if pay:
            sub = models.one("SELECT * FROM subscriptions WHERE owner_id=?", (pay["owner_id"],))
    return sub

def process_billing_event(payload):
    """Handle a signature-verified platform billing webhook. Idempotent by
    event id (falls back to a stable hash of the payload). Amount comes
    from the event's payment entity only - never from browser state. A
    wrong amount is rejected and audited; it never activates anything."""
    _ensure()
    event = payload.get("event", "")
    sub_entity = ((payload.get("payload") or {}).get("subscription") or {}).get("entity", {}) or {}
    pay_entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity", {}) or {}
    event_id = str(payload.get("id") or hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest())
    with models.conn() as c:
        c.execute("INSERT OR IGNORE INTO billing_events(id,provider,event_id,event,"
                  "payload,handled,created) VALUES(?,?,?,?,?,0,?)",
                  (models.uid(), "razorpay", event_id, event,
                   json.dumps(payload, default=str)[:4000], models.now()))
    prior = models.one("SELECT * FROM billing_events WHERE event_id=?", (event_id,))
    if prior["handled"]:
        return {"duplicate": True, "event": event, "event_id": event_id,
                "result": prior["result"]}

    notes = sub_entity.get("notes") or {}
    ref = notes.get("reference", "")
    result = {"event": event, "event_id": event_id}

    if event in ("subscription.charged", "subscription.activated"):
        sub = None
        owner_id = ""
        if ref:
            sub = models.one("SELECT * FROM subscriptions WHERE payment_ref=?", (ref,)) \
                  or models.one("SELECT * FROM subscriptions WHERE owner_id=?", (ref,))
            if sub is None:
                # older checkouts correlate through their payment record
                pay = models.one("SELECT * FROM plat_pays WHERE reference=?", (ref,))
                if pay:
                    sub = models.one("SELECT * FROM subscriptions WHERE owner_id=?", (pay["owner_id"],))
        if sub is None:
            result["handled"] = False
            result["reason"] = "no_matching_subscription"
        else:
            amount = pay_entity.get("amount")
            if amount is None:
                result["handled"] = False
                result["reason"] = "no_payment_amount_in_event"
            else:
                amount = int(amount)
                expected = sub["price"] if sub["status"] in ("pending_payment", "payment_failed") \
                    else sub["regular_renewal_price"]
                if sub["status"] == "active" and sub["is_early_adopter"] and amount == sub["price"]:
                    # First-year charge replay for an already-active EA term:
                    # idempotent, extends nothing.
                    result["handled"] = True
                    result["reason"] = "already_active_first_year"
                elif sub["status"] == "active" and amount == sub["regular_renewal_price"]:
                    # A renewal payment for a still-active regular term is
                    # recorded and audited; term extension happens only through
                    # the explicit renewal checkout, never silently.
                    result["handled"] = True
                    result["reason"] = "renewal_payment_recorded_for_active_subscription"
                    models.log_audit(sub["owner_id"], "billing.renewal_payment",
                                     f"renewal payment {amount} recorded for active subscription")
                elif amount != expected:
                    result["handled"] = False
                    result["reason"] = (f"amount_mismatch(got {amount}, "
                                        f"expected {expected}); not activated")
                    models.log_audit(sub["owner_id"], "billing.webhook_rejected", result["reason"])
                else:
                    ok, activated = activate(sub["owner_id"], amount=amount,
                                             payment_ref=ref,
                                             provider_sub_id=sub_entity.get("id", ""))
                    result["handled"] = bool(ok)
                    result["reason"] = "activated" if ok else str(activated)
    elif event == "subscription.halted":
        sub = _sub_by_ref(ref)
        if sub and sub["status"] == "active":
            set_status(sub["owner_id"], "past_due", "provider halt")
            result.update(handled=True, reason="past_due")
        else:
            result.update(handled=False, reason="no_active_subscription")
    elif event == "subscription.cancelled":
        sub = _sub_by_ref(ref)
        if sub:
            set_status(sub["owner_id"], "canceled", "provider cancellation")
            result.update(handled=True, reason="canceled")
        else:
            result.update(handled=False, reason="no_matching_subscription")
    elif event in ("refund.processed", "subscription.charged.refunded"):
        sub = _sub_by_ref(ref)
        if sub:
            set_status(sub["owner_id"], "refunded", "refund processed")
            result.update(handled=True, reason="refunded")
        else:
            result.update(handled=False, reason="no_matching_subscription")
    elif event == "refund.pending":
        sub = _sub_by_ref(ref)
        if sub:
            set_status(sub["owner_id"], "refund_pending", "refund initiated")
            result.update(handled=True, reason="refund_pending")
        else:
            result.update(handled=False, reason="no_matching_subscription")
    else:
        result.update(handled=False, reason="unhandled_event_type")

    with models.conn() as c:
        c.execute("UPDATE billing_events SET handled=?, result=? WHERE event_id=?",
                  (1 if result.get("handled") else 0,
                   json.dumps(result, default=str)[:1000], event_id))
    return result


# ---------- renewal ----------

def renewal_notice_due(owner_id):
    """True when an active early-adopter subscription is within 30 days of
    its first-year end and the advance notice has not gone out yet."""
    sub = get_subscription(owner_id)
    if not sub or not sub["is_early_adopter"] or sub["status"] != "active":
        return False
    if not sub["term_end"]:
        return False
    if models.now() < sub["term_end"] - RENEWAL_NOTICE_DAYS * 86400:
        return False
    return not sub["renewal_notified_at"]


def queue_renewal_notice(owner):
    """Advance renewal notice to the BUSINESS OWNER (not a customer):
    states the first-year price paid, the renewal price, the renewal date,
    and that renewal needs their explicit action. Deduped, dev-only."""
    sub = get_subscription(owner["id"])
    if not sub:
        return None
    dedupe = f"ea-renewal-notice:{sub['id']}:{int(sub['term_end'])}"
    when = time.strftime("%d %b %Y", time.gmtime(sub["term_end"] + 19800))  # IST
    body = (f"Hi {owner.get('name') or 'there'},\n\n"
            f"Your RecurSaathi Enterprise early-adopter year ends on {when}.\n"
            f"You paid Rs 79,990 for the first 12 months.\n"
            f"Renewal after that runs at the then-current regular annual price "
            f"(currently Rs {sub['regular_renewal_price'] // 100:,}).\n"
            f"Nothing is charged automatically: renewing needs your explicit "
            f"action from the billing page before {when}.\n\n"
            f"The RecurSaathi team")
    mid = models.queue(owner["id"], "email", owner.get("email") or "", "",
                       "Your RecurSaathi Enterprise renewal is coming up",
                       body, dedupe=dedupe)
    if mid is not None:
        models.ex("UPDATE subscriptions SET renewal_notified_at=? WHERE owner_id=?",
                  (models.now(), owner["id"]))
        models.log_audit(owner["id"], "billing.renewal_notice_queued", dedupe)
    return mid


def check_expiries_and_notices():
    """Maintenance pass (dev scheduler calls it): queue due renewal notices
    and mark lapsed subscriptions expired. Never charges anyone."""
    _ensure()
    release_expired()
    notices = 0
    for o in models.q("SELECT u.*, s.id AS sub_id FROM users u JOIN subscriptions s "
                      "ON s.owner_id=u.id WHERE s.status='active' AND s.is_early_adopter=1"):
        if renewal_notice_due(o["id"]):
            if queue_renewal_notice(o):
                notices += 1
    models.ex("UPDATE subscriptions SET status='expired', updated=? "
              "WHERE status='active' AND term_end IS NOT NULL AND term_end<?",
              (models.now(), models.now()))
    return {"renewal_notices_queued": notices}


# ---------- admin stats ----------

def admin_stats():
    _ensure()
    av = availability()
    pending = models.one("SELECT COUNT(*) AS n FROM ea_reservations "
                         "WHERE status='reserved' AND expires_at>?", (models.now(),))["n"]
    expired = models.one("SELECT COUNT(*) AS n FROM ea_reservations WHERE status='expired'")["n"]
    failed = models.one("SELECT COUNT(*) AS n FROM plat_pays WHERE status='failed'")["n"]
    rev_by_plan = {r["plan"]: r["s"] for r in models.q(
        "SELECT plan, COALESCE(SUM(amount),0) AS s FROM plat_pays WHERE status='confirmed' GROUP BY plan")}
    ea_rev = models.one("SELECT COALESCE(SUM(price),0) AS s FROM subscriptions "
                        "WHERE is_early_adopter=1 AND status IN ('active','expired','canceled')")["s"]
    reg_enterprise_rev = models.one(
        "SELECT COALESCE(SUM(price),0) AS s FROM subscriptions "
        "WHERE is_early_adopter=0 AND plan='enterprise' AND status IN ('active','expired','canceled')")["s"]
    mrr = sum({"starter": 999, "growth": 4999, "enterprise": 9999}.get(
        o["plan"], 0) for o in models.q("SELECT plan FROM users WHERE role='owner' AND plan_status='active'"))
    upcoming = models.q("SELECT owner_id, plan, price, term_end FROM subscriptions "
                        "WHERE status='active' AND term_end IS NOT NULL "
                        "ORDER BY term_end ASC LIMIT 10")
    failed_renewals = models.q("SELECT owner_id, plan, price, updated FROM subscriptions "
                               "WHERE status IN ('payment_failed','past_due') ORDER BY updated DESC LIMIT 10")
    return {"availability": av, "pending_reservations": pending,
            "expired_reservations": expired, "failed_checkouts": failed,
            "revenue_by_plan": rev_by_plan,
            "mrr": mrr, "arr": mrr * 12,
            "ea_revenue": ea_rev, "regular_enterprise_revenue": reg_enterprise_rev,
            "upcoming_renewals": upcoming, "failed_renewals": failed_renewals}
