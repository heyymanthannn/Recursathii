"""AI revenue intelligence agent (hybrid automation architecture, Phase 4).

Read-only analysis of one business's own records. Every number here is
computed from the payments, pay_events and invoices tables - nothing is
invented, and anything that is not a verified fact (a forecast, a recovery
"opportunity") is labelled an estimate in the output itself.

Verified recovery rate counts an invoice as recovered only when a CONFIRMED
payment exists for it. Pending, attempted or promised payments never count.

Reports are Level A (internal, no customer contact) and every generation is
written to the audit log. Tenant isolation is absolute: every query is bound
to the owner's id.
"""
import json, time

from . import models

TRANSIENT_CODES = {"insufficient_funds", "insufficient_funds ", "network", "timeout",
                   "temporarily declined", "bank timeout", "issuer unavailable"}


def _overdue_rows(owner_id):
    today = time.strftime("%Y-%m-%d")
    rows = models.q("""SELECT i.id, i.number, i.amount, i.due_date, i.status, cu.name AS customer_name
        FROM invoices i LEFT JOIN customers cu ON cu.id=i.customer_id
        WHERE i.owner_id=? AND i.status IN ('pending','overdue')""", (owner_id,))
    out = []
    for r in rows:
        i = dict(r)
        # financial accuracy: an overdue invoice counts for what is still
        # UNPAID. Partial payments shrink it; once fully paid it is not
        # overdue at all, whatever its status field still says.
        i["paid_amount"] = _net_confirmed(i["id"])
        i["outstanding"] = max(0, (i["amount"] or 0) - i["paid_amount"])
        if i["outstanding"] <= 0:
            continue
        try:
            y, m, d = (int(x) for x in i["due_date"][:10].split("-"))
            days = max(0, round((time.mktime(time.strptime(today, "%Y-%m-%d")) -
                                 time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))) / 86400))
        except Exception:
            days = 0
        i["days_overdue"] = days
        out.append(i)
    return out


def _net_confirmed(invoice_id):
    """Money actually received and kept for one invoice: sum of CONFIRMED
    payments only. Refunded payments are excluded (their status changes to
    refunded, and a refund event is recorded separately). Pending, attempted
    and promised payments never count. This is what makes partial payments
    and duplicate charges read correctly."""
    row = models.one("SELECT COALESCE(SUM(amount),0) AS n FROM payments "
                     "WHERE invoice_id=? AND status='confirmed' AND confirmed_at IS NOT NULL",
                     (invoice_id,))
    return int((row or {}).get("n", 0) or 0)


def _bucket(days):
    if days <= 7:
        return "1-7 days"
    if days <= 30:
        return "8-30 days"
    return "30+ days"


def failed_payment_analysis(owner_id, weeks=4):
    """Recurring failure patterns from provider decline codes on real events."""
    since = models.now() - weeks * 7 * 86400
    events = models.q("SELECT * FROM pay_events WHERE owner_id=? AND kind='failed' AND created>=? "
                      "ORDER BY created DESC", (owner_id, since))
    by_code, by_customer = {}, {}
    retryable_value = 0
    for e in events:
        code = (e["decline_code"] or "unreported").strip().lower() or "unreported"
        by_code.setdefault(code, {"count": 0, "amount": 0})
        by_code[code]["count"] += 1
        by_code[code]["amount"] += e["amount"] or 0
        if code in TRANSIENT_CODES:
            retryable_value += e["amount"] or 0
        by_customer.setdefault(e["customer_id"], 0)
        by_customer[e["customer_id"]] += 1
    repeat_offenders = [{"customer_id": cid, "failures": n}
                        for cid, n in sorted(by_customer.items(), key=lambda kv: -kv[1]) if n >= 2]
    top = sorted(by_code.items(), key=lambda kv: -kv[1]["count"])[:5]
    return {"window_weeks": weeks, "total_failures": len(events),
            "value_at_risk": sum(e["amount"] or 0 for e in events),
            "top_decline_codes": [{"code": c, **v} for c, v in top],
            "retryable_within_72h_value": retryable_value,
            "customers_with_repeated_failures": repeat_offenders[:10]}


def overdue_analysis(owner_id):
    rows = _overdue_rows(owner_id)
    buckets, priority_value = {}, {}
    total = 0
    for i in rows:
        b = _bucket(i["days_overdue"])
        buckets.setdefault(b, {"count": 0, "amount": 0})
        buckets[b]["count"] += 1
        buckets[b]["amount"] += i["outstanding"]
        total += i["outstanding"]
        score = i["days_overdue"] * 2 + min(i["outstanding"] / 100000.0, 10)
        pri = "high" if score >= 20 else ("medium" if score >= 8 else "low")
        priority_value.setdefault(pri, {"count": 0, "amount": 0})
        priority_value[pri]["count"] += 1
        priority_value[pri]["amount"] += i["outstanding"]
    return {"total_overdue_invoices": len(rows), "total_overdue_amount": total,
            "aging_buckets": buckets, "by_priority": priority_value}


def verified_recovery_rate(owner_id, days=90):
    """Share of past-due/failed cases that have a VERIFIED (confirmed) payment.
    Pending and attempted never count as recovered."""
    since = models.now() - days * 86400
    cases = models.q("""SELECT i.id, i.amount FROM invoices i
        WHERE i.owner_id=? AND i.created>=?
        AND (i.status IN ('overdue','paid') OR i.due_date < ?)""",
        (owner_id, since, time.strftime("%Y-%m-%d")))
    recovered = 0
    recovered_value = 0
    partial = 0
    for c in cases:
        net = _net_confirmed(c["id"])
        if net > 0:
            recovered += 1
            # never credit more than the invoice was worth, however many
            # confirmed payments (including duplicates) landed on it
            recovered_value += min(net, c["amount"])
            if net < c["amount"]:
                partial += 1
    failed_events = models.one("SELECT COUNT(*) n FROM pay_events WHERE owner_id=? AND kind='failed' "
                               "AND created>=?", (owner_id, since))
    disputed = models.one("SELECT COUNT(*) n FROM invoices WHERE owner_id=? AND status='disputed' "
                          "AND created>=?", (owner_id, since))
    return {"window_days": days, "past_due_cases": len(cases), "verified_recovered": recovered,
            "verified_recovery_rate": round(recovered / len(cases), 3) if cases else None,
            "verified_recovered_value": recovered_value,
            "partially_recovered_cases": partial,
            "disputed_cases": disputed["n"],
            "failed_payment_events": failed_events["n"]}


def collection_forecast(owner_id):
    """ESTIMATE, clearly labelled: overdue amount x the business's own verified
    recovery rate. The rate is None when there is not enough history, and then
    no forecast number is produced at all - never a made-up one."""
    ov = overdue_analysis(owner_id)
    rr = verified_recovery_rate(owner_id)
    if rr["verified_recovery_rate"] is None:
        return {"estimate": False,
                "reason": "not enough verified history yet to forecast from real data",
                "overdue_amount": ov["total_overdue_amount"]}
    expected = round(ov["total_overdue_amount"] * rr["verified_recovery_rate"])
    return {"estimate": True, "label": "ESTIMATE from your own verified recovery rate",
            "recovery_rate_used": rr["verified_recovery_rate"],
            "overdue_amount": ov["total_overdue_amount"],
            "expected_collections": expected,
            "at_risk": ov["total_overdue_amount"] - expected}


def next_actions(owner_id):
    """Highest-impact next actions, ranked by verified money at stake. Each
    action states which automation level runs it."""
    acts = []
    fpa = failed_payment_analysis(owner_id)
    if fpa["retryable_within_72h_value"] > 0:
        acts.append({"action": "retry transiently-declined payments inside the 72-hour window",
                     "money_at_stake": fpa["retryable_within_72h_value"],
                     "level": "A/B (pre-authorized retries per your recovery rules)"})
    ov = overdue_analysis(owner_id)
    early = ov["aging_buckets"].get("1-7 days", {})
    if early.get("amount", 0) > 0:
        acts.append({"action": f"send day-0 reminders to {early.get('count', 0)} fresh overdue invoice(s)",
                     "money_at_stake": early["amount"],
                     "level": "B (automatic if the channel is authorized)"})
    late = ov["aging_buckets"].get("30+ days", {})
    if late.get("amount", 0) > 0:
        acts.append({"action": f"review {late.get('count', 0)} invoice(s) 30+ days overdue",
                     "money_at_stake": late["amount"],
                     "level": "C (your approval before escalation messages)"})
    if fpa["customers_with_repeated_failures"]:
        acts.append({"action": "update saved cards/UPI mandates for customers with repeated failures",
                     "money_at_stake": sum(e for e in [fpa["value_at_risk"]] if e),
                     "level": "B (routine reminders) / C (collection setting changes)"})
    acts.sort(key=lambda a: -a["money_at_stake"])
    return acts[:5]


def intelligence_snapshot(owner_id):
    snap = {"failed_payments": failed_payment_analysis(owner_id),
            "overdue": overdue_analysis(owner_id),
            "recovery": verified_recovery_rate(owner_id),
            "forecast": collection_forecast(owner_id),
            "next_actions": next_actions(owner_id)}
    models.log_audit(owner_id, "intelligence_snapshot_generated",
                     json.dumps({"overdue": snap["overdue"]["total_overdue_amount"]}))
    return snap


def daily_report(owner_id):
    snap = intelligence_snapshot(owner_id)
    ov = snap["overdue"]
    fpa = snap["failed_payments"]
    rec = snap["recovery"]
    lines = [f"Daily revenue intelligence - {time.strftime('%d %b %Y')}",
             f"- Overdue: {ov['total_overdue_invoices']} invoice(s), Rs {ov['total_overdue_amount']:,} at stake",
             f"- Failed payments (last 4 weeks): {fpa['total_failures']} event(s), Rs {fpa['value_at_risk']:,} at risk",
             f"- Verified recovery rate (last 90 days): "
             f"{rec['verified_recovery_rate'] if rec['verified_recovery_rate'] is not None else 'not enough history yet'}"]
    top = snap["failed_payments"]["top_decline_codes"][:1]
    if top:
        lines.append(f"- Most common decline: {top[0]['code']} ({top[0]['count']} time(s))")
    if snap["forecast"].get("estimate"):
        lines.append(f"- Forecast (ESTIMATE, not a promise): Rs {snap['forecast']['expected_collections']:,}")
    report = "\n".join(lines)
    models.log_activity(owner_id, "report", "daily revenue intelligence report generated")
    return report


def weekly_report(owner_id):
    snap = intelligence_snapshot(owner_id)
    ov = snap["overdue"]
    rec = snap["recovery"]
    lines = [f"Weekly revenue intelligence - week of {time.strftime('%d %b %Y')}",
             f"- Overdue inventory: {ov['total_overdue_invoices']} invoice(s), Rs {ov['total_overdue_amount']:,}",
             "- Aging: " + ", ".join(f"{k}: {v['count']} (Rs {v['amount']:,})"
                                     for k, v in sorted(ov["aging_buckets"].items())),
             f"- Verified recoveries (last 90 days): {rec['verified_recovered']} case(s), "
             f"Rs {rec['verified_recovered_value']:,} - verified payments only"]
    acts = snap["next_actions"][:3]
    if acts:
        lines.append("- Highest-impact next actions:")
        for a in acts:
            lines.append(f"  1. {a['action']} (Rs {a['money_at_stake']:,})")
    report = "\n".join(lines)
    models.log_activity(owner_id, "report", "weekly revenue intelligence report generated")
    return report


def assistant_answer(owner_id, question):
    """Read-only business assistant. It answers ONLY from this business's own
    records, via rule-based lookups - no invented figures, no customer-facing
    action, no writes. Anything it cannot compute from real data, it says so."""
    q = (question or "").strip().lower()
    if not q:
        return "Ask me about overdue invoices, failed payments, recovery rates or forecasts."
    # read-only guard: this assistant never writes, cancels, refunds, sends or
    # triggers automations. Write requests are refused here, at the door.
    if any(w in q for w in ("cancel", "refund", "send ", "send a", "mark ", "delete", "update ",
                            "write", "create ", "trigger", "run ", "pause ", "approve ", "pay ")):
        return ("I only read your records and answer questions - I cannot cancel, refund, send, "
                "or trigger anything. Those actions live in the automation control centre "
                "and in your hands.")
    if any(w in q for w in ("overdue", "pending", "unpaid")):
        ov = overdue_analysis(owner_id)
        if not ov["total_overdue_invoices"]:
            return "No overdue invoices in your records right now."
        parts = [f"{v['count']} invoice(s) {k} overdue, Rs {v['amount']:,}"
                 for k, v in sorted(ov["aging_buckets"].items())]
        return f"You have {ov['total_overdue_invoices']} overdue invoice(s), Rs {ov['total_overdue_amount']:,} total. " + \
               "; ".join(parts) + "."
    if "fail" in q or "declin" in q or "retry" in q:
        fpa = failed_payment_analysis(owner_id)
        if not fpa["total_failures"]:
            return "No failed payment events in your records for the last 4 weeks."
        top = fpa["top_decline_codes"][:2]
        codes = ", ".join(f"{t['code']} ({t['count']})" for t in top)
        return (f"{fpa['total_failures']} failed payment(s) in the last 4 weeks, Rs {fpa['value_at_risk']:,} at risk. "
                f"Most common: {codes}. Rs {fpa['retryable_within_72h_value']:,} is retryable inside the 72-hour window.")
    if "recover" in q or "success" in q or "rate" in q:
        rec = verified_recovery_rate(owner_id)
        if rec["verified_recovery_rate"] is None:
            return "Not enough verified history yet to compute a recovery rate - I only count confirmed payments."
        return (f"Verified recovery rate (last 90 days): {rec['verified_recovery_rate']:.0%} - "
                f"{rec['verified_recovered']} of {rec['past_due_cases']} past-due cases paid and confirmed, "
                f"Rs {rec['verified_recovered_value']:,}.")
    if "forecast" in q or "expect" in q or "predict" in q or "next month" in q:
        fc = collection_forecast(owner_id)
        if not fc.get("estimate"):
            return fc["reason"]
        return (f"ESTIMATE, not a promise: at your verified recovery rate of {fc['recovery_rate_used']:.0%}, "
                f"about Rs {fc['expected_collections']:,} of the Rs {fc['overdue_amount']:,} overdue could come in.")
    if "action" in q or "should i" in q or "what next" in q or "priority" in q:
        acts = next_actions(owner_id)
        if not acts:
            return "Nothing needs attention in your records right now."
        return "Highest-impact next actions: " + "; ".join(
            f"{a['action']} (Rs {a['money_at_stake']:,})" for a in acts[:3])
    return ("I only answer from your own RecurSaathi records (overdue invoices, failed payments, "
            "recovery rates, forecasts and next actions) and I never make up numbers. Try one of those.")
