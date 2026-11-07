"""Recovery automations: reminders, overdue notices, failed-payment nudges,
confirmations. Each workflow can be switched on/off per owner. Dedupe keys
keep every stage from firing twice for the same invoice on the same day."""
import time
from . import models, notify, config

DEFAULTS = {
    "reminder_due": {
        "email_subject": "Payment reminder: invoice {number} from {business}",
        "email_body": ("Hi {customer_name},\n\nThis is a friendly reminder that invoice {number} "
                       "for Rs {amount} is due on {due_date}.\n\nPay here: {pay_link}\n\nThank you,\n{business}"),
        "wa_body": ("Hi {customer_name}, gentle reminder: invoice {number} of Rs {amount} is due on "
                    "{due_date}. Pay here: {pay_link} - {business}"),
    },
    "overdue_1": {
        "email_subject": "Overdue: invoice {number} from {business}",
        "email_body": ("Hi {customer_name},\n\nInvoice {number} for Rs {amount} was due on {due_date} "
                       "and is still unpaid.\n\nPay here: {pay_link}\n\nThank you,\n{business}"),
        "wa_body": ("Hi {customer_name}, invoice {number} (Rs {amount}) is overdue since {due_date}. "
                    "Pay here: {pay_link} - {business}"),
    },
    "overdue_7": {
        "email_subject": "Final reminder: invoice {number} from {business}",
        "email_body": ("Hi {customer_name},\n\nInvoice {number} for Rs {amount} has been overdue "
                       "since {due_date}. Please settle it to avoid service interruption.\n\n"
                       "Pay here: {pay_link}\n\nThank you,\n{business}"),
        "wa_body": ("Hi {customer_name}, final reminder for invoice {number} (Rs {amount}, overdue "
                    "since {due_date}). Pay here: {pay_link} - {business}"),
    },
    "failed_payment": {
        "email_subject": "Your payment to {business} did not go through",
        "email_body": ("Hi {customer_name},\n\nYour recent payment of Rs {amount} to {business} could "
                       "not be processed ({reason}). You can retry here: {pay_link}\n\nThank you,\n{business}"),
        "wa_body": ("Hi {customer_name}, your payment of Rs {amount} to {business} failed ({reason}). "
                    "Retry here: {pay_link}"),
    },
    "dormant_reactivate": {
        "email_subject": "We miss you, {customer_name}! - {business}",
        "email_body": ("Hi {customer_name},\n\nIt has been a while since your last order. "
                       "You have paid us {lifetime} across your orders, last one on {last_paid}.\n\n"
                       "Whenever you are ready, we are here.\n\n{business}"),
        "wa_body": ("Hi {customer_name}! It has been a while - your last order was on {last_paid}. "
                    "We would love to see you again. - {business}"),
    },
    "payment_confirm": {
        "email_subject": "Payment received: invoice {number}",
        "email_body": ("Hi {customer_name},\n\nWe have received your payment of Rs {amount} for invoice "
                       "{number}. Thank you!\n\n{business}"),
        "wa_body": ("Hi {customer_name}, payment of Rs {amount} received for invoice {number}. "
                    "Thank you! - {business}"),
    },
}

def _render(text, **ctx):
    class _D(dict):
        def __missing__(self, k): return ""
    return text.format_map(_D(**ctx))

def _ctx(owner, invoice, customer, base_url):
    return {
        "customer_name": customer["name"], "business": owner["business"],
        "number": invoice["number"], "amount": f"{invoice['amount']/100:,.0f}",
        "due_date": invoice["due_date"], "pay_link": f"{base_url}/invoice/{invoice['token']}",
        "reason": "bank declined the payment",
    }

def _stage(invoice, today):
    if invoice["status"] == "paid":
        return None
    if not invoice["due_date"]:
        return None
    try:
        y, m, d = (int(x) for x in invoice["due_date"][:10].split("-"))
        due = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    except Exception:
        return None
    today_t = time.mktime(time.strptime(today, "%Y-%m-%d"))
    days = round((today_t - due) / 86400)
    if days < -1:
        return None          # too early
    if -1 <= days <= 0:
        return "reminder_due"
    if days <= 7:
        return "overdue_1"
    return "overdue_7"

def run_dormant_reactivation(owner):
    """Queue reactivation notes for dormant repeat customers. Returns counts."""
    counts = {"queued": 0, "sent": 0, "no_channel": 0, "skipped": 0, "failed": 0}
    today = time.strftime("%Y-%m-%d")
    for cu in models.radar(owner["id"])["dormant"]:
        ctx = {"customer_name": cu["name"], "business": owner["business"] or owner["name"] or "",
               "lifetime": f"Rs {cu['lifetime']/100:,.0f}",
               "last_paid": time.strftime("%d %b %Y", time.localtime(cu["last_paid_at"]))}
        tpl = models.get_template(owner["id"], "dormant_reactivate")
        sub = _render(tpl["subject"] if tpl else DEFAULTS["dormant_reactivate"]["email_subject"], **ctx)
        ebody = _render(tpl["body"] if tpl else DEFAULTS["dormant_reactivate"]["email_body"], **ctx)
        wbody = _render(DEFAULTS["dormant_reactivate"]["wa_body"], **ctx)
        outs = []
        if cu.get("email"):
            o = models.queue(owner["id"], "email", cu["email"], "dormant_reactivate", sub, ebody,
                             dedupe=f"dormant_reactivate:{cu['id']}:{today}:email")
            if o: outs.append(o)
        if cu.get("phone") and not cu.get("wa_opt_out"):
            o = models.queue(owner["id"], "whatsapp", cu["phone"], "dormant_reactivate", sub, wbody,
                             dedupe=f"dormant_reactivate:{cu['id']}:{today}:wa")
            if o: outs.append(o)
        for o in outs:
            st = notify.deliver(o)
            counts[st if st in counts else "failed"] += 1
            if st == "sent":
                models.log_activity(owner["id"], "automation",
                                    f"Reactivation note sent to {cu['name']} ({o['channel']})")
        if outs:
            counts["queued"] += len(outs)
    return counts

def run_for_owner(owner, base_url):
    """Queue + attempt delivery for everything due. Returns counts."""
    keys = models.get_automations(owner["id"])
    counts = {"queued": 0, "sent": 0, "no_channel": 0, "skipped": 0, "failed": 0}
    today = time.strftime("%Y-%m-%d")
    if keys.get("dormant_reactivate", True):
        d = run_dormant_reactivation(owner)
        for k, v in d.items():
            counts[k] = counts.get(k, 0) + v
    for inv in models.list_invoices(owner["id"]):
        stage = _stage(inv, today)
        if not stage or not keys.get(stage, True):
            continue
        cu = models.get_customer(inv["customer_id"])
        if not cu:
            continue
        ctx = _ctx(owner, inv, cu, base_url)
        tpl = models.get_template(owner["id"], stage)
        sub = _render(tpl["subject"] if tpl else DEFAULTS[stage]["email_subject"], **ctx)
        ebody = _render(tpl["body"] if tpl else DEFAULTS[stage]["email_body"], **ctx)
        wbody = _render(DEFAULTS[stage]["wa_body"], **ctx)
        day = today
        outs = []
        if cu.get("email"):
            o = models.queue(owner["id"], "email", cu["email"], stage, sub, ebody,
                             dedupe=f"{stage}:{inv['id']}:{day}:email")
            if o: outs.append(o)
        if cu.get("phone") and not cu.get("wa_opt_out"):
            o = models.queue(owner["id"], "whatsapp", cu["phone"], stage, sub, wbody,
                             dedupe=f"{stage}:{inv['id']}:{day}:wa")
            if o: outs.append(o)
        for o in outs:
            st = notify.deliver(o)
            counts[st if st in counts else "failed"] += 1
            if st == "sent":
                models.log_activity(owner["id"], "automation",
                                    f"{stage} message sent to {cu['name']} ({o['channel']}) for invoice {inv['number']}")
    return counts

def send_confirmation(owner, invoice, customer, base_url):
    keys = models.get_automations(owner["id"])
    if not keys.get("payment_confirm", True):
        return None
    ctx = _ctx(owner, invoice, customer, base_url)
    tpl = models.get_template(owner["id"], "payment_confirm")
    outs = []
    if customer.get("email"):
        o = models.queue(owner["id"], "email", customer["email"], "payment_confirm",
                         _render(tpl["subject"] if tpl else DEFAULTS["payment_confirm"]["email_subject"], **ctx),
                         _render(tpl["body"] if tpl else DEFAULTS["payment_confirm"]["email_body"], **ctx))
        if o: outs.append(o)
    if customer.get("phone") and not customer.get("wa_opt_out"):
        o = models.queue(owner["id"], "whatsapp", customer["phone"], "payment_confirm",
                         _render(tpl["subject"] if tpl else DEFAULTS["payment_confirm"]["email_subject"], **ctx),
                         _render(DEFAULTS["payment_confirm"]["wa_body"], **ctx))
        if o: outs.append(o)
    results = [notify.deliver(o) for o in outs]
    return results

def send_failed_payment_notice(owner, invoice, customer, reason, base_url):
    keys = models.get_automations(owner["id"])
    if not keys.get("failed_payment", True) or not invoice:
        return []
    ctx = _ctx(owner, invoice, customer, base_url)
    ctx["reason"] = reason or "the bank declined the payment"
    tpl = models.get_template(owner["id"], "failed_payment")
    outs = []
    if customer.get("email"):
        o = models.queue(owner["id"], "email", customer["email"], "failed_payment",
                         _render(tpl["subject"] if tpl else DEFAULTS["failed_payment"]["email_subject"], **ctx),
                         _render(tpl["body"] if tpl else DEFAULTS["failed_payment"]["email_body"], **ctx))
        if o: outs.append(o)
    if customer.get("phone") and not customer.get("wa_opt_out"):
        o = models.queue(owner["id"], "whatsapp", customer["phone"], "failed_payment",
                         _render(tpl["subject"] if tpl else DEFAULTS["failed_payment"]["email_subject"], **ctx),
                         _render(DEFAULTS["failed_payment"]["wa_body"], **ctx))
        if o: outs.append(o)
    return [notify.deliver(o) for o in outs]
