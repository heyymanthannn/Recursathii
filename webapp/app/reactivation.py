"""AI customer reactivation agent (hybrid AI architecture, Phase 3).

Everything here is rule-based arithmetic on the business's own records - no
external AI service. Dormant customers are detected from verified payment
history (confirmed payments only, never pending ones), segmented by real
lifetime value and repeat behaviour, and campaign drafts are built from those
segments.

Launching a campaign is a Level C action: a draft does nothing until the
business owner approves it through the owner API. After approval the actual
sends are Level B routine reactivation and still pass the permission engine
(authorized channel, quiet hours, frequency caps, opt-outs, audit trail).

Honest reporting: recovered revenue is counted ONLY from confirmed payments a
customer made after the campaign contacted them. Everything else (the value
estimate on a draft) is labelled an estimate and kept separate.
"""
import json, time

from . import models, permissions

# ---- schema ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS react_campaigns(
  id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, segment TEXT,
  channels TEXT, email_subject TEXT, email_body TEXT, wa_body TEXT,
  status TEXT DEFAULT 'draft', max_per_run INTEGER DEFAULT 50,
  estimated_value INTEGER DEFAULT 0, customers_targeted INTEGER DEFAULT 0,
  created REAL, approved_at REAL, approved_by TEXT, closed_at REAL
);
CREATE TABLE IF NOT EXISTS react_messages(
  id TEXT PRIMARY KEY, owner_id TEXT, campaign_id TEXT, customer_id TEXT,
  channel TEXT, outbox_id TEXT, status TEXT DEFAULT 'queued',
  created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS react_responses(
  id TEXT PRIMARY KEY, owner_id TEXT, campaign_id TEXT, customer_id TEXT,
  kind TEXT, note TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS react_outcomes(
  id TEXT PRIMARY KEY, owner_id TEXT, campaign_id TEXT, customer_id TEXT,
  invoice_id TEXT, amount INTEGER, created REAL, UNIQUE(campaign_id, invoice_id)
);
"""

SEGMENTS = ("all", "VIP", "Loyal repeat", "Occasional", "One-time")
DEFAULT_INACTIVE_DAYS = 90


def ensure_schema():
    with models.conn() as c:
        c.executescript(SCHEMA)


ensure_schema()


# ---- dormant detection from real transaction history ----
def dormant_customers(owner_id, days_inactive=DEFAULT_INACTIVE_DAYS):
    """Customers with at least one VERIFIED payment, none of it recent, and
    no open invoice (open invoices belong to recovery, not reactivation)."""
    now = models.now()
    cutoff = now - days_inactive * 86400
    rows = models.q("""
        SELECT cu.id, cu.name, cu.email, cu.phone, cu.email_opt_out, cu.wa_opt_out,
               COALESCE(SUM(p.amount),0) AS lifetime,
               COUNT(*) AS paid_count,
               MAX(p.confirmed_at) AS last_paid_at
        FROM customers cu
        JOIN payments p ON p.customer_id = cu.id AND p.status = 'confirmed'
        WHERE cu.owner_id = ?
        GROUP BY cu.id
        HAVING last_paid_at IS NOT NULL AND last_paid_at < ?
        ORDER BY last_paid_at ASC""", (owner_id, cutoff))
    out = []
    for r in rows:
        c = dict(r)
        # only real history counts: confirmed payments, nothing pending
        open_inv = models.one(
            "SELECT id FROM invoices WHERE customer_id=? AND status IN ('pending','overdue') LIMIT 1",
            (c["id"],))
        if open_inv:
            continue   # they have an open invoice: that is recovery work, not reactivation
        c["avg_order"] = round(c["lifetime"] / max(c["paid_count"], 1))
        c["days_inactive"] = max(0, round((now - c["last_paid_at"]) / 86400))
        c["segment"] = _segment(c["lifetime"], c["paid_count"])
        out.append(c)
    return out


def _segment(lifetime, paid_count):
    if lifetime >= 500000 or paid_count >= 10:
        return "VIP"
    if paid_count == 1:
        return "One-time"   # real money, but no repeat behaviour yet
    if paid_count >= 3 or lifetime >= 100000:
        return "Loyal repeat"
    return "Occasional"


# ---- campaigns ----
def _segment_members(owner_id, segment):
    return [c for c in dormant_customers(owner_id)
            if segment == "all" or c["segment"] == segment]


def create_campaign(owner, name, segment, channels, email_subject="", email_body="",
                    wa_body="", max_per_run=50, days_inactive=DEFAULT_INACTIVE_DAYS):
    """Create a DRAFT. A draft contacts nobody and runs nothing."""
    if segment not in SEGMENTS:
        raise ValueError(f"segment must be one of {SEGMENTS}")
    chans = [c for c in (channels or []) if c in ("email", "whatsapp")]
    if not chans:
        raise ValueError("campaign needs at least one channel: email or whatsapp")
    members = _segment_members(owner["id"], segment) if segment != "all" else \
        dormant_customers(owner["id"], days_inactive)
    # estimate only: average historical order value x customers. Never counted
    # as revenue until a verified payment actually lands.
    estimated = sum(m["avg_order"] for m in members)
    camp = {"id": models.uid(), "owner_id": owner["id"], "name": name,
            "segment": segment, "channels": json.dumps(chans),
            "email_subject": email_subject, "email_body": email_body,
            "wa_body": wa_body, "status": "draft", "max_per_run": int(max_per_run),
            "estimated_value": estimated, "customers_targeted": len(members),
            "created": models.now(), "approved_at": None, "approved_by": None,
            "closed_at": None}
    models.ex("INSERT INTO react_campaigns(id,owner_id,name,segment,channels,email_subject,"
              "email_body,wa_body,status,max_per_run,estimated_value,customers_targeted,"
              "created,approved_at,approved_by,closed_at) VALUES(:id,:owner_id,:name,:segment,"
              ":channels,:email_subject,:email_body,:wa_body,:status,:max_per_run,"
              ":estimated_value,:customers_targeted,:created,:approved_at,:approved_by,:closed_at)", camp)
    models.log_activity(owner["id"], "campaign",
                        f"reactivation campaign drafted: {name} ({segment}, {len(members)} customers, "
                        f"estimated value {estimated} - estimate only")
    models.log_audit(owner["id"], "campaign_draft_created",
                     json.dumps({"campaign_id": camp["id"], "name": name, "segment": segment}))
    return get_campaign(camp["id"])


def get_campaign(cid):
    r = models.one("SELECT * FROM react_campaigns WHERE id=?", (cid,))
    if not r:
        return None
    c = dict(r)
    c["channels"] = json.loads(c["channels"] or "[]")
    return c


def list_campaigns(owner_id):
    return [get_campaign(r["id"]) for r in
            models.q("SELECT id FROM react_campaigns WHERE owner_id=? ORDER BY created DESC", (owner_id,))]


def approve_campaign(owner, campaign_id, approved_by):
    """Level C: the owner's explicit approval is what turns a draft into an
    active campaign. Nothing sends before this."""
    c = get_campaign(campaign_id)
    if not c or c["owner_id"] != owner["id"]:
        return None
    if c["status"] != "draft":
        return c
    res = permissions.resolve(owner["id"], "launch_reactivation_campaign")
    if res["decision"] == "denied":
        models.log_audit(owner["id"], "campaign_launch_denied",
                         json.dumps({"campaign_id": campaign_id, "reason": res["reason"]}))
        return c
    models.ex("UPDATE react_campaigns SET status='active', approved_at=?, approved_by=? WHERE id=?",
              (models.now(), approved_by or "owner", campaign_id))
    models.log_activity(owner["id"], "campaign", f"reactivation campaign approved: {c['name']}")
    models.log_audit(owner["id"], "campaign_approved",
                     json.dumps({"campaign_id": campaign_id, "approved_by": approved_by or "owner"}))
    return get_campaign(campaign_id)


def cancel_campaign(owner_id, campaign_id):
    c = get_campaign(campaign_id)
    if not c or c["owner_id"] != owner_id or c["status"] in ("cancelled", "done"):
        return c
    models.ex("UPDATE react_campaigns SET status='cancelled', closed_at=? WHERE id=?",
              (models.now(), campaign_id))
    models.log_audit(owner_id, "campaign_cancelled", json.dumps({"campaign_id": campaign_id}))
    return get_campaign(campaign_id)


def run_campaign(owner, campaign, counts=None):
    """Queue one reactivation message per eligible customer per channel.
    Every send still resolves through the permission engine (Level B)."""
    ensure_schema()
    counts = counts if counts is not None else {}
    if campaign["status"] != "active":
        counts["campaign_draft_not_active"] = counts.get("campaign_draft_not_active", 0) + 1
        return counts
    members = _segment_members(owner["id"], campaign["segment"]) if campaign["segment"] != "all" \
        else dormant_customers(owner["id"])
    queued = held = deferred = 0
    for m in members[:max(int(campaign["max_per_run"]), 0)]:
        for channel in campaign["channels"]:
            dest = m.get("email") if channel == "email" else m.get("phone")
            if not dest:
                continue
            if models.one("SELECT id FROM react_messages WHERE campaign_id=? AND customer_id=? AND channel=?",
                          (campaign["id"], m["id"], channel)):
                continue   # this campaign already contacted them on this channel
            # cap check and queue insert stay under one lock so concurrent
            # jobs cannot bypass frequency caps between counting and inserting
            with models.QUEUE_LOCK:
                res = permissions.resolve(owner["id"], "run_approved_reactivation",
                                          channel=channel, customer=m)
                if res["decision"] not in ("auto", "needs_approval"):
                    deferred += 1
                    continue
                body = campaign["email_body"] if channel == "email" else campaign["wa_body"]
            sub = campaign["email_subject"] if channel == "email" else ""
            o = models.queue(owner["id"], channel, dest, "reactivation", sub, body,
                             dedupe=f"react:{campaign['id']}:{m['id']}:{channel}")
            st = "held_for_approval" if res["decision"] == "needs_approval" else "queued"
            if o:
                if res["decision"] == "needs_approval":
                    # a held message sits in approval; the delivery loop must
                    # never pick it up until the owner approves that exact message
                    models.ex("UPDATE outbox SET status='approval', error=? WHERE id=?",
                              (res.get("reason") or "waiting for owner approval", o["id"]))
                    held += 1
                else:
                    queued += 1
                models.ex("INSERT INTO react_messages(id,owner_id,campaign_id,customer_id,channel,"
                          "outbox_id,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                          (models.uid(), owner["id"], campaign["id"], m["id"], channel,
                           o["id"], st, models.now(), models.now()))
    if queued:
        models.log_activity(owner["id"], "campaign",
                            f"campaign {campaign['name']}: {queued} message(s) queued for this run")
    counts.update({"reactivation_queued": queued, "reactivation_held": held,
                   "reactivation_deferred": deferred})
    return counts


def run_active(owner_id, counts=None):
    counts = counts if counts is not None else {}
    owner = models.get_user(owner_id)
    if not owner:
        return counts
    for c in models.q("SELECT id FROM react_campaigns WHERE owner_id=? AND status='active'", (owner_id,)):
        run_campaign(owner, get_campaign(c["id"]), counts)
    return counts


# ---- responses and verified revenue ----
RESPONSE_KINDS = ("reply", "interested", "not_interested", "opt_out")


def record_response(owner_id, campaign_id, customer_id, kind, note=""):
    if kind not in RESPONSE_KINDS:
        raise ValueError(f"kind must be one of {RESPONSE_KINDS}")
    c = get_campaign(campaign_id)
    if not c or c["owner_id"] != owner_id:
        return None
    models.ex("INSERT INTO react_responses(id,owner_id,campaign_id,customer_id,kind,note,created) "
              "VALUES(?,?,?,?,?,?,?)",
              (models.uid(), owner_id, campaign_id, customer_id, kind, note, models.now()))
    if kind == "opt_out":
        cu = models.get_customer(customer_id)
        if cu:
            models.set_customer_optout(owner_id, customer_id, email_opt=True, wa_opt=True)
    models.log_audit(owner_id, "campaign_response",
                     json.dumps({"campaign_id": campaign_id, "customer_id": customer_id, "kind": kind}))
    return models.one("SELECT * FROM react_responses WHERE campaign_id=? AND customer_id=? "
                      "ORDER BY created DESC LIMIT 1", (campaign_id, customer_id))


def campaign_summary(owner_id, campaign_id):
    """Actual vs estimated, kept separate by design:
    verified_recovered counts only confirmed payments made AFTER the campaign
    contacted the customer. estimated_value is a guess and says so."""
    c = get_campaign(campaign_id)
    if not c or c["owner_id"] != owner_id:
        return None
    msgs = models.q("SELECT * FROM react_messages WHERE campaign_id=?", (campaign_id,))
    sent = delivered = 0
    for m in msgs:
        if m["outbox_id"]:
            o = models.get_outbox(m["outbox_id"])
            if o:
                if o["status"] in ("sent", "delivered"):
                    sent += 1
                if o["status"] == "delivered":
                    delivered += 1
    first_msg = {}
    for m in msgs:
        t = first_msg.get(m["customer_id"])
        if t is None or m["created"] < t:
            first_msg[m["customer_id"]] = m["created"]
    verified = 0
    for cid, since in first_msg.items():
        for p in models.q("SELECT id, invoice_id, amount FROM payments WHERE owner_id=? AND "
                          "customer_id=? AND status='confirmed' AND confirmed_at>=? AND confirmed_at IS NOT NULL",
                          (owner_id, cid, since)):
            if not models.one("SELECT id FROM react_outcomes WHERE campaign_id=? AND invoice_id=?",
                              (campaign_id, p["invoice_id"])):
                models.ex("INSERT OR IGNORE INTO react_outcomes(id,owner_id,campaign_id,customer_id,"
                          "invoice_id,amount,created) VALUES(?,?,?,?,?,?,?)",
                          (models.uid(), owner_id, campaign_id, cid, p["invoice_id"], p["amount"],
                           models.now()))
                verified += p["amount"]
    responses = models.q("SELECT kind, COUNT(*) n FROM react_responses WHERE campaign_id=? GROUP BY kind",
                         (campaign_id,))
    resp = {r["kind"]: r["n"] for r in responses}
    return {"campaign": c,
            "targeted": c["customers_targeted"],
            "messaged": len(set(m["customer_id"] for m in msgs)),
            "sent": sent, "delivered": delivered,
            "replies": resp.get("reply", 0) + resp.get("interested", 0),
            "opt_outs": resp.get("opt_out", 0),
            "verified_recovered": verified,          # actual money, verified payments only
            "estimated_value": c["estimated_value"]}  # estimate, clearly not actual
