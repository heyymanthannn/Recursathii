"""Retention agent: chases merchants who signed up but never paid.

Within RETENTION_DELAY_MIN minutes of a signup with no payment, the agent
reaches out: an AI voice call when a voice provider is connected
(RETENTION_CALL_URL + RETENTION_CALL_TOKEN), and always a WhatsApp + email
follow-up asking what stopped them. If the answer is price, a 20% off coupon
is issued automatically and the code goes out on WhatsApp. A coupon can also
be entered on the checkout page before any of this: valid codes discount the
QR amount on the spot.
"""
import os, time
import httpx
from . import db, notify, config

RETENTION_DELAY_MIN = int(os.environ.get("RETENTION_DELAY_MIN", "5"))
RETENTION_COUPON_PCT = int(os.environ.get("RETENTION_COUPON_PCT", "20"))

CALL_URL = os.environ.get("RETENTION_CALL_URL", "")
CALL_TOKEN = os.environ.get("RETENTION_CALL_TOKEN", "")
OMNIDIM_AGENT_ID = os.environ.get("OMNIDIM_AGENT_ID", "")


def place_provider_call(num: str, script: str, metadata: dict) -> str:
    """Fire an AI voice call through the configured provider. Omnidimension
    (omnidim.io) uses its own dispatch shape: agent_id + to_number + call_context
    (the call's task/prompt travels as variables the agent reads)."""
    try:
        with httpx.Client(timeout=25) as cl:
            headers = {"Authorization": f"Bearer {CALL_TOKEN}", "Content-Type": "application/json"}
            if OMNIDIM_AGENT_ID or "omnidim" in CALL_URL:
                payload = {
                    "agent_id": int(OMNIDIM_AGENT_ID),
                    "to_number": num,
                    "call_context": {"task": script},
                    "metadata": metadata,
                }
                url = CALL_URL or "https://omnidim.io/api/v1/calls/dispatch"
            else:
                payload = {"to": num, "task": script, "metadata": metadata}
                url = CALL_URL
            r = cl.post(url, headers=headers, json=payload)
        return "call_placed" if r.status_code < 400 else f"call_failed:{r.status_code}"
    except Exception as e:
        return f"call_failed:{str(e)[:120]}"

PRICE_WORDS = ("price", "expensive", "cost", "costly", "mehenga", "mahenga", "paisa", "budget", "afford")


def voice_ready() -> bool:
    return bool(CALL_URL and CALL_TOKEN)


def _checkout_link(m: dict) -> str:
    return f"{config.BASE_URL}/checkout/{m['id']}"


def _norm_phone(p: str) -> str:
    num = (p or "").replace(" ", "")
    if num and not num.startswith("+"):
        num = "+91" + num.lstrip("0")
    return num


def _ask_bodies(m: dict) -> tuple[str, str]:
    """(whatsapp body, email body) asking why they left."""
    name = m.get("name") or "there"
    link = _checkout_link(m)
    wa = (f"Hi {name}, you set up RecurSaathi but didn't finish. "
          f"What stopped you - price, setup, or just busy? "
          f"If price is the issue, tell us and we'll do something about it. "
          f"Pick up where you left off: {link}")
    em = (f"Hi {name},\n\nYou started setting up RecurSaathi but didn't finish. "
          f"What stopped you? If price is the issue, just reply and say so - "
          f"we can do something about it.\n\n"
          f"Pick up where you left off: {link}\n\nTeam RecurSaathi")
    return wa, em


def _coupon_bodies(m: dict, code: str, pct: int) -> tuple[str, str]:
    link = _checkout_link(m)
    wa = (f"Totally fair. Here's {pct}% off your first month of RecurSaathi: "
          f"code {code}. Enter it on the checkout page and the QR updates to "
          f"the discounted amount: {link}")
    em = (f"Hi,\n\nNo problem - here's {pct}% off your first month of RecurSaathi.\n\n"
          f"Your coupon code: {code}\n\nEnter it on the checkout page and the "
          f"amount updates on the spot: {link}\n\nTeam RecurSaathi")
    return wa, em


def _place_call(m: dict, script: str) -> str:
    """Fire the AI voice call through the configured provider. Returns status."""
    num = _norm_phone(m.get("phone"))
    if not voice_ready() or not num:
        return "no_voice_provider" if not voice_ready() else "no_phone"
    return place_provider_call(num, script, {"merchant_id": m["id"]})


def _first_touch(m: dict, counts: dict):
    name = m.get("name") or "there"
    wa_body, em_body = _ask_bodies(m)
    script = (f"Hi {name}, this is the RecurSaathi assistant. You tried RecurSaathi "
              f"but didn't finish setting up. May I ask what stopped you? "
              f"If it is the price, tell the caller price, and they will arrange "
              f"a discount for you right away.")
    call_status = _place_call(m, script)
    st_wa = st_em = ""
    if m.get("phone") and not m.get("retention_wa_sent"):
        st_wa, _err = notify.send_whatsapp(m.get("phone"), wa_body)
    if m.get("email") and not m.get("retention_email_sent"):
        st_em, _err = notify.send_email(m.get("email"), "What stopped you from finishing your RecurSaathi setup?", em_body)
    ok_call = call_status == "call_placed"
    ok_any = ok_call or st_wa == "sent" or st_em == "sent"
    if ok_any:
        db.save_merchant({**m, "retention_state": f"contacted:{call_status}",
                          "retention_at": time.time(),
                          "retention_wa_sent": 1 if st_wa == "sent" else 0,
                          "retention_email_sent": 1 if st_em == "sent" else 0})
        counts["contacted"] += 1
    else:
        # No channel connected yet: leave state empty so this merchant is
        # retried once a channel goes live, instead of being lost.
        counts["waiting_channel"] = counts.get("waiting_channel", 0) + 1
    counts["calls"] += 1 if call_status == "call_placed" else 0
    counts["wa_sent"] += 1 if st_wa == "sent" else 0
    counts["emails"] += 1 if st_em == "sent" else 0


def _coupon_touch(m: dict, counts: dict):
    pct = RETENTION_COUPON_PCT
    code = db.issue_coupon(m["id"], pct)
    if m.get("coupon_code") == code and m.get("retention_state", "").startswith("coupon_sent"):
        return  # already sent
    wa_body, em_body = _coupon_bodies(m, code, pct)
    st_wa = st_em = ""
    if m.get("phone"):
        st_wa, _err = notify.send_whatsapp(m.get("phone"), wa_body)
    if m.get("email"):
        st_em, _err = notify.send_email(m.get("email"),
                                        f"Your {pct}% off code for RecurSaathi", em_body)
    if st_wa == "sent" or st_em == "sent":
        db.save_merchant({**m, "coupon_code": code, "retention_state": "coupon_sent",
                          "retention_at": time.time()})
    counts["coupons"] += 1
    counts["wa_sent"] += 1 if st_wa == "sent" else 0
    counts["emails"] += 1 if st_em == "sent" else 0


def run_retention() -> dict:
    """One pass over unpaid signups. Cheap enough to run every minute."""
    counts = {"contacted": 0, "calls": 0, "wa_sent": 0, "emails": 0, "coupons": 0}
    now = time.time()
    for m in db.load_merchants().values():
        try:
            if m.get("status") != "trial":
                continue
            created = m.get("created") or 0
            if not created or now - created < RETENTION_DELAY_MIN * 60:
                continue
            if not m.get("retention_state"):
                _first_touch(m, counts)
            if m.get("retention_outcome") == "price":
                _coupon_touch(m, counts)
        except Exception:
            continue
    return counts


def record_call_outcome(merchant_id: str, reason: str) -> dict:
    """Webhook body for the voice provider (or manual): the merchant's reason."""
    m = db.load_merchants().get(merchant_id)
    if not m:
        return {"ok": False, "reason": "unknown merchant"}
    r = (reason or "").lower()
    outcome = "price" if any(w in r for w in PRICE_WORDS) else "other"
    db.save_merchant({**m, "retention_outcome": outcome})
    if outcome == "price":
        _coupon_touch(m, {"coupons": 0, "wa_sent": 0, "emails": 0})
        return {"ok": True, "outcome": outcome, "coupon": db.load_merchants()[merchant_id].get("coupon_code")}
    return {"ok": True, "outcome": outcome}


def apply_coupon(merchant_id: str, code: str) -> dict:
    """Validate a coupon on the checkout page; on success the QR discounts."""
    m = db.load_merchants().get(merchant_id)
    if not m:
        return {"ok": False, "error": "unknown merchant"}
    if m.get("status") == "active":
        return {"ok": False, "error": "account already active"}
    cu = db.find_coupon(code)
    if not cu or not cu.get("active") or cu.get("redeemed"):
        return {"ok": False, "error": "invalid code"}
    if cu.get("merchant_id") and cu["merchant_id"] != merchant_id:
        return {"ok": False, "error": "invalid code"}
    db.save_merchant({**m, "discount_pct": cu["pct"], "coupon_code": cu["code"]})
    return {"ok": True, "pct": cu["pct"]}
