"""Automation permission engine (hybrid AI architecture, Phase 1).

Every automated action resolves through exactly one of three levels before it
runs, and every resolution is written to the audit log.

  Level A - fully automatic. Internal, non-communicating work: detect failed
    payments and overdue invoices, sync payment status, categorize and
    prioritize recovery cases, generate reminder drafts, schedule authorized
    reminders, stop follow-ups after a verified payment, build reports, write
    activity logs.
  Level B - automatic after business authorization. Anything that contacts a
    customer: routine email/WhatsApp reminders, approved reminder sequences,
    reactivation campaigns within configured limits. Requires the channel to
    be connected and authorized by the business, messages inside frequency
    limits and quiet hours, and the customer not opted out.
  Level C - owner approval required. Unusual or sensitive communications,
    bulk campaigns outside an approved policy, refunds, cancellations,
    financial adjustments, payment-collection setting changes, permission
    changes, recovery rule changes, disputed or ambiguous transactions.

Hard-denied for AI whatever the settings say: initiating refunds, transferring
funds, changing financial records, or overriding a verified payment status.
Delivery honesty lives in notify.py: a successful API request alone never
counts as a delivered message.
"""
import json, time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import models

LEVEL_A, LEVEL_B, LEVEL_C = "A", "B", "C"

ACTIONS = {
    # ---- Level A: internal, fully automatic ----
    "detect_failed_payment": LEVEL_A,
    "detect_overdue_invoice": LEVEL_A,
    "sync_payment_status": LEVEL_A,
    "categorize_case": LEVEL_A,
    "prioritize_case": LEVEL_A,
    "generate_reminder_draft": LEVEL_A,
    "schedule_authorized_reminder": LEVEL_A,
    "stop_after_verified_payment": LEVEL_A,
    "generate_report": LEVEL_A,
    "log_activity": LEVEL_A,
    # ---- Level B: automatic once the business authorized it ----
    "send_routine_email_reminder": LEVEL_B,
    "send_routine_whatsapp_reminder": LEVEL_B,
    "execute_approved_sequence": LEVEL_B,
    "run_approved_reactivation": LEVEL_B,
    # ---- Level C: owner approval required ----
    "send_sensitive_message": LEVEL_C,
    "run_bulk_campaign_outside_policy": LEVEL_C,
    "issue_refund": LEVEL_C,
    "cancel_invoice": LEVEL_C,
    "financial_adjustment": LEVEL_C,
    "change_collection_settings": LEVEL_C,
    "change_permissions": LEVEL_C,
    "change_recovery_rules": LEVEL_C,
    "act_on_disputed_transaction": LEVEL_C,
}

# The AI can never do these on its own, whatever any settings say.
HARD_DENIED = {
    "ai_initiate_refund",
    "transfer_funds",
    "change_financial_records",
    "override_verified_payment_status",
}

DEFAULTS = {
    "quiet_hours": {"enabled": True, "start": "21:00", "end": "08:00",
                    "tz": "Asia/Kolkata"},
    "max_per_customer_per_day": 2,
    "max_per_owner_per_day": 100,
    "authorized_channels": [],   # business must connect AND authorize each channel
    "ai_variation": False,       # owner authorized AI wording variations of approved templates
    "reactivation_max_per_run": 25,
    "emergency_paused": False,   # global emergency pause: holds every customer send
}


def _ensure():
    with models.conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS permissions("
                  "owner_id TEXT PRIMARY KEY, config TEXT, updated REAL)")


def get_config(owner_id) -> dict:
    """Effective permission config: defaults merged with the owner's overrides."""
    _ensure()
    r = models.one("SELECT config FROM permissions WHERE owner_id=?", (owner_id,))
    stored = {}
    if r and r.get("config"):
        try:
            stored = json.loads(r["config"])
        except Exception:
            stored = {}
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in (stored or {}).items():
        if k in cfg and isinstance(cfg[k], dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


_ALLOWED = set(DEFAULTS)


def set_config(owner_id, patch: dict) -> dict:
    """Merge an owner's permission overrides. Unknown keys are rejected."""
    _ensure()
    patch = patch or {}
    bad = [k for k in patch if k not in _ALLOWED]
    if bad:
        raise ValueError("unknown permission keys: " + ", ".join(sorted(bad)))
    cfg = get_config(owner_id)
    for k, v in patch.items():
        if isinstance(cfg.get(k), dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    models.ex("INSERT INTO permissions(owner_id,config,updated) VALUES(?,?,?) "
              "ON CONFLICT(owner_id) DO UPDATE SET config=excluded.config, "
              "updated=excluded.updated",
              (owner_id, json.dumps(cfg), time.time()))
    models.log_audit(owner_id, "permissions_updated", json.dumps(patch))
    return cfg


def authorize_channel(owner_id, channel: str) -> dict:
    """Record that the business connected and authorized a channel for Level B."""
    cfg = get_config(owner_id)
    chans = list(cfg["authorized_channels"])
    if channel not in chans:
        chans.append(channel)
    return set_config(owner_id, {"authorized_channels": chans})


def set_emergency_pause(owner_id, paused: bool) -> dict:
    return set_config(owner_id, {"emergency_paused": bool(paused)})


def _tz_of(cfg):
    try:
        return ZoneInfo(cfg["quiet_hours"].get("tz") or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _hhmm(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def in_quiet_hours(cfg, ts=None) -> bool:
    q = cfg["quiet_hours"]
    if not q.get("enabled"):
        return False
    tz = _tz_of(cfg)
    now = datetime.fromtimestamp(ts if ts is not None else time.time(), tz)
    cur = now.hour * 60 + now.minute
    start, end = _hhmm(q.get("start", "21:00")), _hhmm(q.get("end", "08:00"))
    if start is None or end is None:
        return False
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # window crosses midnight


def _local_day_start(cfg) -> float:
    tz = _tz_of(cfg)
    now = datetime.now(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _opted_out(customer, channel) -> bool:
    if not customer:
        return False
    if channel == "email":
        return bool(customer.get("email_opt_out"))
    if channel == "whatsapp":
        return bool(customer.get("wa_opt_out"))
    return False


def _count_outbox(owner_id, since, dest=None) -> int:
    if dest:
        row = models.one("SELECT COUNT(*) AS n FROM outbox "
                         "WHERE owner_id=? AND dest=? AND created>=?",
                         (owner_id, dest, since))
    else:
        row = models.one("SELECT COUNT(*) AS n FROM outbox "
                         "WHERE owner_id=? AND created>=?", (owner_id, since))
    return int((row or {}).get("n", 0) or 0)


def resolve(owner_id, action, *, channel="", customer=None, ts=None) -> dict:
    """Resolve one action against the business's permissions. Never raises on
    an unknown action: unknown resolves denied, and every resolution is
    audited."""
    cfg = get_config(owner_id)
    level = ACTIONS.get(action)
    out = {"action": action, "level": level, "decision": "", "reason": ""}

    if action in HARD_DENIED or level is None:
        out.update(decision="denied",
                   reason="hard-denied: AI may never do this on its own")
    elif level == LEVEL_A:
        out.update(decision="auto", reason="level A: internal, no customer contact")
    elif level == LEVEL_C:
        out.update(decision="needs_approval",
                   reason="level C: owner approval required before this runs")
    else:  # LEVEL_B
        if cfg.get("emergency_paused"):
            out.update(decision="paused", reason="owner emergency pause is active")
        elif _opted_out(customer, channel):
            out.update(decision="denied", reason="customer opted out of this channel")
        elif channel and channel not in cfg["authorized_channels"]:
            out.update(decision="needs_approval",
                       reason="channel '" + (channel or "?") +
                              "' is not connected and authorized by the business")
        elif in_quiet_hours(cfg, ts):
            out.update(decision="deferred", reason="inside configured quiet hours")
        elif customer is not None and cfg.get("max_per_customer_per_day", 0) > 0:
            since = _local_day_start(cfg)
            n = 0
            for d in filter(None, [customer.get("email") or "", customer.get("phone") or ""]):
                n += _count_outbox(owner_id, since, dest=d)
            if n >= cfg["max_per_customer_per_day"]:
                out.update(decision="deferred",
                           reason="per-customer daily message limit reached")
        if not out["decision"] and cfg.get("max_per_owner_per_day", 0) > 0:
            if _count_outbox(owner_id, _local_day_start(cfg)) >= cfg["max_per_owner_per_day"]:
                out.update(decision="deferred",
                           reason="business daily message limit reached")
        if not out["decision"] and models.get_automation_mode(owner_id) == "approval":
            out.update(decision="needs_approval",
                       reason="business runs in Approval Required mode")
        if not out["decision"]:
            out.update(decision="auto", reason="level B: authorized, within limits")

    models.log_audit(owner_id, "automation_permission",
                     action + " -> " + out["decision"] + " (" + out["reason"] + ")")
    return out
