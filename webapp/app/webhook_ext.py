"""Extends the Razorpay webhook: beyond failure ingestion, it settles invoices
confirmed by payment links / checkout and records failure events per owner."""
from . import models, automations as auto

def handle(payload: dict, base_url: str):
    event = payload.get("event", "")
    pay_entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    notes = pay_entity.get("notes") or {}
    tok = notes.get("token")
    inv = models.get_invoice_by_token(tok) if tok else None
    if not inv:
        return {"ok": True, "matched": False}
    owner = models.get_user(inv["owner_id"])
    cu = models.get_customer(inv["customer_id"])
    if event in ("payment.captured", "payment_link.paid", "order.paid"):
        if inv["status"] != "paid":
            models.mark_invoice_paid(inv, "razorpay",
                                     reference=pay_entity.get("id", ""),
                                     note=f"webhook {event}")
            models.log_activity(owner["id"], "payment",
                                f"Razorpay confirmed {inv['amount']/100:,.0f} for invoice {inv['number']}")
            if owner and cu:
                auto.send_confirmation(owner, inv, cu, base_url)
        return {"ok": True, "matched": True, "invoice": inv["number"], "status": "paid"}
    if event == "payment.refunded":
        ref = pay_entity.get("id", "")
        pay = models.one("SELECT * FROM payments WHERE reference=? AND status='confirmed'", (ref,))
        if pay:
            models.refund_payment(pay["id"], note=f"gateway refund {ref}", gateway="razorpay")
            models.log_activity(inv["owner_id"], "refund",
                                f"Refund recorded on invoice {inv['number']} ({pay['amount']/100:,.0f})")
            return {"ok": True, "matched": True, "invoice": inv["number"], "status": "refunded"}
        return {"ok": True, "matched": True, "invoice": inv["number"], "status": "refund: no matching payment"}
    if event == "payment.failed":
        models.record_event(owner["id"], "failed", "razorpay", inv["customer_id"],
                            pay_entity.get("amount", inv["amount"]),
                            decline_code=pay_entity.get("error_description", "declined"),
                            invoice_id=inv["id"])
        if owner and cu:
            auto.send_failed_payment_notice(owner, inv, cu, pay_entity.get("error_description", ""), base_url)
        return {"ok": True, "matched": True, "invoice": inv["number"], "status": "failed recorded"}
    return {"ok": True, "matched": False}
