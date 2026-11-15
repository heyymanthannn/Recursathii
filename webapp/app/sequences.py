"""Smart follow-up engine: per-business configurable recovery sequences.

A sequence is an ordered list of steps. Each step fires once per invoice when
the invoice is at least `offset` days past its anchor date (the promised date
when the business recorded one, else the due date), through the channels the
step lists. Offsets may be negative: -7 to -1 schedule a heads-up BEFORE the
due date (validated; a step at day 0 or later still follows the due date). A step flagged `escalate` queues its message but holds it for owner
approval instead of sending (the spec's day-7 escalation). Steps are example
defaults, never mandatory: every business can replace the whole schedule.

Pause conditions live with the caller (automations.run_for_owner): a verified
payment stops everything, opted-out customers are denied by the permission
engine, cancelled/disputed invoices and owner-paused cases are skipped, and a
missing provider shows up as no_channel instead of a fake send.
"""
import json, time
from . import models

DEFAULT_STEPS = [
    {"id": "day0", "offset": 0, "template_key": "reminder_due",
     "channels": ["email", "whatsapp"]},
    {"id": "day2", "offset": 2, "template_key": "overdue_1",
     "channels": ["email", "whatsapp"]},
    {"id": "day5", "offset": 5, "template_key": "overdue_1",
     "channels": ["email", "whatsapp"]},
    {"id": "day7", "offset": 7, "template_key": "overdue_7",
     "channels": ["email", "whatsapp"], "escalate": True},
]

_ALLOWED_TPL = {"reminder_due", "overdue_1", "overdue_7", "failed_payment",
                "payment_confirm", "dormant_reactivate", "due_call"}
_ALLOWED_CHANNELS = {"email", "whatsapp"}


def ensure_schema():
    with models.conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS sequences("
                  "owner_id TEXT PRIMARY KEY, config TEXT, updated REAL)")
        for stmt in (
            "ALTER TABLE outbox ADD COLUMN provider_ref TEXT DEFAULT ''",
            "ALTER TABLE outbox ADD COLUMN delivery_status TEXT DEFAULT ''",
            "ALTER TABLE outbox ADD COLUMN provider_updated REAL",
            "ALTER TABLE invoices ADD COLUMN paused INTEGER DEFAULT 0",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass  # column already exists


def _validate(steps):
    if not isinstance(steps, list) or not (1 <= len(steps) <= 10):
        raise ValueError("sequence must be a list of 1 to 10 steps")
    seen = set()
    last = -8   # offsets may start up to a week before the due date
    out = []
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("each step must be an object")
        sid = str(s.get("id") or "").strip()
        if not sid or not sid.replace("_", "").isalnum() or sid in seen:
            raise ValueError("each step needs a unique alphanumeric id")
        seen.add(sid)
        try:
            off = int(s.get("offset", 0))
        except Exception:
            raise ValueError("step offset must be an integer")
        if not (-7 <= off <= 90) or off < last:
            raise ValueError("step offsets must be between -7 and 90 and non-decreasing")
        last = off
        tk = s.get("template_key") or ("reminder_due" if off <= 0 else
                                       "overdue_7" if off >= 7 else "overdue_1")
        if tk not in _ALLOWED_TPL:
            raise ValueError("unknown template_key: " + str(tk))
        chans = s.get("channels") or ["email", "whatsapp"]
        if (not isinstance(chans, list) or not chans
                or any(c not in _ALLOWED_CHANNELS for c in chans)):
            raise ValueError("channels must be email and/or whatsapp")
        step = {"id": sid, "offset": off, "template_key": tk, "channels": sorted(set(chans))}
        if s.get("escalate"):
            step["escalate"] = True
        if s.get("approval_required"):
            step["approval_required"] = True   # this step always waits for the owner
        try:
            ma = int(s.get("max_attempts", 0))
        except Exception:
            raise ValueError("max_attempts must be an integer")
        if ma:
            if not (1 <= ma <= 10):
                raise ValueError("max_attempts must be 1-10")
            step["max_attempts"] = ma
        out.append(step)
    return out


def get_sequence(owner_id):
    ensure_schema()
    r = models.one("SELECT config FROM sequences WHERE owner_id=?", (owner_id,))
    if r and r.get("config"):
        try:
            return _validate(json.loads(r["config"]))
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STEPS))


def set_sequence(owner_id, steps) -> list:
    ensure_schema()
    if isinstance(steps, list) and not steps:
        # empty list = reset to the defaults
        models.ex("DELETE FROM sequences WHERE owner_id=?", (owner_id,))
        models.log_audit(owner_id, "sequence_updated", "reset to defaults")
        return get_sequence(owner_id)
    clean = _validate(steps)
    models.ex("INSERT INTO sequences(owner_id,config,updated) VALUES(?,?,?) "
              "ON CONFLICT(owner_id) DO UPDATE SET config=excluded.config, "
              "updated=excluded.updated",
              (owner_id, json.dumps(clean), time.time()))
    models.log_audit(owner_id, "sequence_updated",
                     f"{len(clean)} steps: " + ", ".join(
                         s["id"] + "@d" + str(s["offset"]) +
                         ("+escalate" if s.get("escalate") else "") for s in clean))
    return clean


def set_invoice_paused(owner_id, invoice_id, paused: bool) -> bool:
    """Owner pauses or resumes one recovery case. Returns True when changed."""
    ensure_schema()
    inv = models.one("SELECT id FROM invoices WHERE id=? AND owner_id=?",
                     (invoice_id, owner_id))
    if not inv:
        return False
    models.ex("UPDATE invoices SET paused=? WHERE id=?", (1 if paused else 0, invoice_id))
    models.log_audit(owner_id, "invoice_pause" if paused else "invoice_resume", invoice_id)
    return True
