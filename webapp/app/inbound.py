"""Inbound customer replies on the business's own WhatsApp number.

Meta's Cloud API webhook delivers customer messages to /webhooks/whatsapp.
RecurSaathi records them as conversations (the business owns the number and
the relationship; RecurSaathi never auto-replies), classifies the intent with
deterministic rules on the message text only (no paid AI services), and lets
the existing engine react: a "paid" reply becomes a PENDING claim only the
owner can verify (a customer never settles an invoice by saying so), a
"more time" reply pauses the case and opens a request for the owner, a
dispute flips the invoice to disputed (reminders stop). Everything else is
recorded for the owner inbox.
"""
import re
from . import models

_PAID_WORDS = ("paid", "payment done", "done the payment", "payment sent",
               "sent the payment", "transfer kar diya", "kar diya", "bhej diya",
               "upi kar diya", "ho gaya", "\u092a\u0947\u092e\u0947\u0902",
               "\u092d\u0947\u091c \u0926\u093f\u092f\u093e",
               "\u0915\u0930 \u0926\u093f\u092f\u093e")
_MORE_WORDS = ("more time", "some time", "next week", "next month", "month end",
               "salary", "extend", "give me time", "wait", "thoda", "kal",
               "monday", "friday", "by tomorrow", "few days",
               "\u0915\u0932", "\u0935\u0947\u0924\u0928")
_DISPUTE_WORDS = ("dispute", "wrong", "not mine", "didn't order", "did not order",
                  "never ordered", "already paid", "fraud", "scam", "cancel",
                  "refund", "duplicate", "galat")

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def classify_intent(body: str):
    """Deterministic rules on the message text. Order matters: a dispute
    claim wins over a payment claim, because "already paid" needs the owner's
    eyes, not an automated pending claim."""
    t = " " + (body or "").lower() + " "
    if any(w in t for w in _DISPUTE_WORDS):
        return "dispute"
    if any(w in t for w in _PAID_WORDS):
        return "paid"
    if any(w in t for w in _MORE_WORDS):
        return "more_time"
    return "other"


def _norm_phone(p):
    p = re.sub(r"[^0-9+]", "", p or "").lstrip("+")
    if p.startswith("91") and len(p) > 10:
        p = p[2:]
    return p


def find_owner_by_phone_id(phone_number_id):
    """The business whose own WhatsApp number received the message."""
    if not phone_number_id:
        return None
    return models.one("SELECT owner_id FROM connect WHERE wa_phone_id=?",
                      (str(phone_number_id).strip(),))


def find_customer(owner_id, phone):
    np = _norm_phone(phone)
    if not np:
        return None
    for cu in models.q("SELECT * FROM customers WHERE owner_id=?", (owner_id,)):
        if _norm_phone(cu.get("phone")) == np:
            return cu
    return None


def latest_open_invoice(owner_id, customer_id):
    if not customer_id:
        return None
    return models.one("""SELECT i.* FROM invoices i WHERE i.owner_id=? AND i.customer_id=?
                         AND i.status NOT IN ('paid','cancelled')
                         ORDER BY i.created DESC LIMIT 1""", (owner_id, customer_id))


def _inbound_text(msg):
    t = msg.get("type") or ""
    if t == "text":
        return ((msg.get("text") or {}).get("body")) or ""
    if t == "button":
        return ((msg.get("button") or {}).get("text")) or ""
    if t == "interactive":
        br = ((msg.get("interactive") or {}).get("button_reply") or {})
        return br.get("title") or ""
    return ""


def _apply_intent(owner_id, cu, inv, conv, intent, text, out):
    """Rule-based reaction. Every reaction stays within the existing
    permission model: no customer is ever contacted automatically here."""
    if intent == "paid" and inv:
        models.record_payment_attempt(inv, "upi", status="pending",
                                      note="customer reported payment on WhatsApp; pending business verification")
        models.log_activity(owner_id, "payment",
                            f"{cu['name']} replied on WhatsApp that they paid; pending claim recorded "
                            f"for invoice {inv['number']} (nothing is confirmed until you verify)")
        out["actions"].append({"invoice": inv["number"], "action": "pending_claim"})
    elif intent == "more_time" and inv:
        m = _DATE_RE.search(text or "")
        req = models.create_customer_request(owner_id, customer_id=cu["id"],
                                             invoice_id=inv["id"], kind="more_time",
                                             note=text or "",
                                             requested_date=m.group(1) if m else "")
        models.ex("UPDATE invoices SET paused=1 WHERE id=?", (inv["id"],))
        models.log_activity(owner_id, "automation",
                            f"{cu['name']} asked for more time on invoice {inv['number']}; "
                            "reminders paused pending your decision")
        out["actions"].append({"invoice": inv["number"], "action": "more_time_request",
                               "request_id": req["id"]})
    elif intent == "dispute" and inv:
        req = models.create_customer_request(owner_id, customer_id=cu["id"],
                                             invoice_id=inv["id"], kind="dispute",
                                             note=text or "")
        if inv["status"] != "disputed":
            models.ex("UPDATE invoices SET status='disputed' WHERE id=?", (inv["id"],))
        models.log_activity(owner_id, "automation",
                            f"{cu['name']} disputed invoice {inv['number']}; "
                            "reminders stopped until you review")
        out["actions"].append({"invoice": inv["number"], "action": "dispute",
                               "request_id": req["id"]})


def handle_inbound(payload: dict):
    """Process one Meta webhook payload. Never raises to the caller: an
    unreadable message is skipped, the webhook still returns ok."""
    out = {"recorded": 0, "duplicates": 0, "actions": []}
    for entry in (payload.get("entry") or []):
        for change in (entry.get("changes") or []):
            value = change.get("value") or {}
            phone_number_id = ((value.get("metadata") or {}).get("phone_number_id")) or ""
            om = find_owner_by_phone_id(phone_number_id)
            if not om:
                if value.get("messages"):
                    out["actions"].append({"skipped": "no business connected to this WhatsApp number"})
                continue
            owner_id = om["owner_id"]
            for msg in (value.get("messages") or []):
                try:
                    mid = msg.get("id") or ""
                    text = _inbound_text(msg)
                    intent = classify_intent(text)
                    cu = find_customer(owner_id, msg.get("from") or "")
                    inv = latest_open_invoice(owner_id, cu["id"]) if cu else None
                    conv, dup = models.record_conversation(
                        owner_id, customer_id=(cu or {}).get("id", ""),
                        invoice_id=(inv or {}).get("id", "") if inv else "",
                        channel="whatsapp", direction="in", body=text,
                        provider_message_id=mid, intent=intent)
                    if dup:
                        out["duplicates"] += 1
                        continue
                    out["recorded"] += 1
                    if cu:
                        _apply_intent(owner_id, cu, inv, conv, intent, text, out)
                    else:
                        out["actions"].append({"note": "reply from an unknown number recorded without a customer link"})
                except Exception as e:  # one malformed message never kills the batch
                    out["actions"].append({"skipped": f"message processing error: {str(e)[:120]}"})
    return out
