"""Recovery engine: intake, playbook routing, retry scheduling."""
import time
from dataclasses import dataclass, field
from . import playbooks
from .billing import is_billable
from . import db

db.init()

@dataclass
class FailureEvent:
    id: str
    merchant_id: str
    gateway: str
    customer_name: str
    customer_phone: str
    amount: int                  # paise
    decline_code: str
    mandate_id: str | None
    playbook: str
    retries_due: list = field(default_factory=list)
    retries_done: int = 0
    recovered: bool = False
    status: str = "failed"

EVENTS: list[FailureEvent] = []
SENT_MESSAGES: list[dict] = []   # whatsapp outbox (swap for real provider)

def ingest_normal(*, event_id: str, merchant_id: str, gateway: str,
                  customer_name: str, customer_phone: str, amount: int,
                  decline_code: str, mandate_id: str | None) -> FailureEvent | None:
    """Shared intake for any gateway, already normalized."""
    if not is_billable(merchant_id):
        return None
    if db.get_event(event_id):
        return None  # duplicate webhook event: one recovery case per verified event
    pb = playbooks.playbook_for(decline_code)
    fe = FailureEvent(
        id=event_id, merchant_id=merchant_id, gateway=gateway,
        customer_name=customer_name, customer_phone=customer_phone,
        amount=amount, decline_code=decline_code,
        mandate_id=mandate_id, playbook=pb,
        retries_due=playbooks.next_retry_at(pb),
    )
    EVENTS.append(fe)
    db.save_event(fe.__dict__)
    send_nudge(fe)
    return fe


def ingest(payload: dict, gateway: str = "razorpay") -> FailureEvent | None:
    """Normalize a Razorpay-shaped webhook into a FailureEvent and act on it."""
    if payload.get("event") != "payment.failed":
        return None
    e = payload["payload"]["payment"]["entity"]
    notes = e.get("notes") or {}
    return ingest_normal(
        event_id=e.get("id", f"evt_{len(EVENTS)}"),
        merchant_id=notes.get("merchant_id", "demo"),
        gateway=gateway,
        customer_name=notes.get("name", "there"),
        customer_phone=(e.get("contact") or "").lstrip("+"),
        amount=e.get("amount", 0),
        decline_code=(e.get("error_reason") or "UNKNOWN").upper().replace(" ", "_"),
        mandate_id=e.get("token_id"))

def send_nudge(fe: FailureEvent, link_override: str | None = None):
    from .config import PAY_LINK_BASE
    msg = playbooks.render_message(
        fe.playbook, name=fe.customer_name, brand="your-subscription",
        amount=fe.amount / 100, link=link_override or f"{PAY_LINK_BASE}/{fe.id}")
    msg_row = {"to": fe.customer_phone, "event_id": fe.id, "playbook": fe.playbook,
               "body": msg, "at": time.time()}
    SENT_MESSAGES.append(msg_row)
    db.save_message(msg_row)

def hydrate():
    """Re-attach persisted events so retries survive a restart."""
    known = {e.id for e in EVENTS}
    for row in db.load_events():
        if row["id"] in known:
            continue
        EVENTS.append(FailureEvent(
            id=row["id"], merchant_id=row["merchant_id"], gateway=row["gateway"],
            customer_name=row["customer_name"], customer_phone=row["customer_phone"],
            amount=row["amount"], decline_code=row["decline_code"], mandate_id=row["mandate_id"],
            playbook=row["playbook"], retries_due=list(row["retries_due"]),
            retries_done=row["retries_done"], recovered=row["recovered"],
            status=(row["status"] if "status" in row.keys() else "failed") or "failed"))

def run_scheduler(now: float | None = None) -> int:
    """Process retries that are due. In production this is a Redis-backed worker."""
    hydrate()
    now = now if now is not None else time.time()
    fired = 0
    for fe in EVENTS:
        if fe.recovered or not fe.retries_due:
            continue
        due = [t for t in fe.retries_due if t <= now]
        if due:
            fe.retries_done += len(due)
            fe.retries_due = [t for t in fe.retries_due if t > now]
            db.update_event(fe.id, retries_due=",".join(str(t) for t in fe.retries_due),
                            retries_done=fe.retries_done)
            send_nudge(fe, link_override=f"https://pay.example/retry/{fe.id}")
            fired += len(due)
    return fired

VERIFIED_STATUSES = {"pending", "failed", "succeeded", "refunded", "disputed",
                     "expired", "cancelled"}


def set_payment_status(event_id: str, status: str, *, verified: bool = False) -> dict:
    """Record a payment lifecycle status on a recovery case.

    'succeeded' is accepted only when verified (payment.captured webhook,
    verified checkout signature, or owner confirmation). A pending webhook or
    a bare API accept never settles a payment. A verified success is final:
    nothing may override it afterwards, and it stops all follow-ups.
    """
    row = db.get_event(event_id)
    if not row:
        return {"ok": False, "reason": "unknown event"}
    if status not in VERIFIED_STATUSES:
        return {"ok": False, "reason": "unknown status: " + str(status)}
    if status == "succeeded" and not verified:
        return {"ok": False, "reason": "unverified: accepted or pending is not a settled payment"}
    if (row["status"] or "failed") == "succeeded" and status != "succeeded":
        return {"ok": False, "reason": "verified payment status cannot be overridden"}
    db.set_event_status(event_id, status)
    for fe in EVENTS:
        if fe.id == event_id:
            fe.status = status
    if status == "succeeded":
        mark_recovered(event_id)
    return {"ok": True, "event_id": event_id, "status": status}


def mark_recovered(payment_id: str):
    for fe in EVENTS:
        if fe.id == payment_id:
            fe.recovered = True
            fe.retries_due = []
            db.update_event(fe.id, recovered=1, retries_due="")

def stats(merchant_id: str | None = None) -> dict:
    rows = [e for e in EVENTS if merchant_id is None or e.merchant_id == merchant_id]
    failed = len(rows)
    recovered = sum(1 for e in rows if e.recovered)
    by_code: dict[str, int] = {}
    for e in rows:
        by_code[e.decline_code] = by_code.get(e.decline_code, 0) + 1
    return {"failed": failed, "recovered": recovered,
            "recovery_rate": round(recovered / failed, 3) if failed else 0.0,
            "rupees_saved": sum(e.amount for e in rows if e.recovered) / 100,
            "by_decline_code": by_code}
