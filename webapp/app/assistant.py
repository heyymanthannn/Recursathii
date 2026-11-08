"""AI Recovery Assistant: reads the merchant's own records and turns them into
an analysis the owner can act on - overdue priorities, next-best actions,
dormant customers, draft messages and a weekly summary. Every number comes
from the real database; the assistant never invents figures or sends anything
on its own (drafts queue only when the owner clicks send)."""
import time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from . import models, automations as auto

router = APIRouter()


def _inr(p):
    return f"\u20b9{p/100:,.0f}"


def _days_between(d_iso, now_ts):
    try:
        y, m, d = (int(x) for x in d_iso[:10].split("-"))
        return max(0, round((now_ts - time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))) / 86400))
    except Exception:
        return 0


def next_best_action(row):
    """Rule-based next step from the row's real state."""
    d = row.get("days_overdue", 0)
    if d == 0:
        return "Send today's gentle nudge - the invoice is due now, a soft reminder lands best."
    if d <= 3:
        return "Send a friendly reminder with the payment link; most customers pay on the first nudge."
    if d <= 7:
        return "Send a firmer follow-up with the QR and offer to split the amount if money is tight."
    if d <= 14:
        return "Call the customer today, then log the call outcome. Follow with a written reminder."
    return "Escalate: call, then pause their service per your policy and send a final notice."


def weekly_summary(owner_id, now_ts):
    """Real numbers from the last 7 days."""
    wk = now_ts - 7 * 86400
    recovered = models.one(
        "SELECT COALESCE(SUM(amount),0) AS amt, COUNT(*) AS n FROM payments "
        "WHERE owner_id=? AND status='confirmed' AND confirmed_at>=?", (owner_id, wk))
    failed = models.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM pay_events "
        "WHERE owner_id=? AND kind='failed' AND created>=?", (owner_id, wk))
    queued = models.one(
        "SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND created>=?", (owner_id, wk))
    sent = models.one(
        "SELECT COUNT(*) AS n FROM outbox WHERE owner_id=? AND created>=? AND status='sent'",
        (owner_id, wk))
    outstanding = models.one(
        "SELECT COALESCE(SUM(amount),0) AS amt, COUNT(*) AS n FROM invoices "
        "WHERE owner_id=? AND status IN ('pending','overdue')", (owner_id,))
    return {
        "recovered_amt": recovered["amt"], "recovered_n": recovered["n"],
        "failed_n": failed["n"], "failed_amt": failed["amt"],
        "messages_sent": sent["n"], "messages_queued": queued["n"],
        "outstanding_amt": outstanding["amt"], "outstanding_n": outstanding["n"],
    }


def priorities(owner_id):
    """Overdue invoices ranked: amount at stake x how late x whether retries failed."""
    rad = models.radar(owner_id)
    rows = []
    for i in rad["overdue"]:
        score = i["days_overdue"] * 2 + min(i["amount"] / 100000.0, 10)
        rows.append({"inv": i, "score": score, "action": next_best_action(i)})
    rows.sort(key=lambda r: (r["score"], r["inv"]["amount"]), reverse=True)
    return rows[:8], rad


def draft_message(owner, inv, cu, stage, base_url):
    """Generate the draft reminder text for one invoice (email + WhatsApp).
    Rendered from the merchant's real invoice data and editable templates."""
    tpl = models.get_template(owner["id"], stage)
    ctx = auto._ctx(owner, inv, cu, base_url)
    sub = auto._render(tpl["subject"] if tpl else auto.DEFAULTS[stage]["email_subject"], **ctx)
    ebody = auto._render(tpl["body"] if tpl else auto.DEFAULTS[stage]["email_body"], **ctx)
    wbody = auto._render(auto.DEFAULTS[stage]["wa_body"], **ctx)
    return sub, ebody, wbody, stage


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request):
    from .webui import guard, render
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    now_ts = time.time()
    today = time.strftime("%Y-%m-%d")
    tops, rad = priorities(user["id"])
    rows = []
    for r in tops:
        i = r["inv"]
        cu = models.get_customer(i["customer_id"])
        stage = auto._stage(i, today) or "reminder_due"
        sub, ebody, wbody, stage = draft_message(user, i, cu, stage, str(request.base_url).rstrip("/"))
        rows.append({"inv": i, "cu": cu, "score": r["score"], "action": r["action"],
                     "stage": stage, "sub": sub, "ebody": ebody, "wbody": wbody})
    summary = weekly_summary(user["id"], now_ts)
    # retry-eligible failures from real pay_events
    transient = {"insufficient_funds", "insufficient_funds ", "network", "timeout",
                 "temporarily declined", "bank timeout", "issuer unavailable"}
    fails = models.q(
        "SELECT e.*, cu.name AS customer_name FROM pay_events e "
        "LEFT JOIN customers cu ON cu.id=e.customer_id "
        "WHERE e.owner_id=? AND e.kind='failed' ORDER BY e.created DESC LIMIT 6", (user["id"],))
    fails = [dict(f) for f in fails]
    for f in fails:
        f["retry_ok"] = (f.get("decline_code") or "").strip().lower() in transient
    return render("assistant.html", request, user=user, tab="assistant", rows=rows,
                  summary=summary, dormant=rad["dormant"][:6], fails=fails, inr=_inr,
                  base=str(request.base_url).rstrip("/"), today=today)
