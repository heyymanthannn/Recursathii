"""Email + WhatsApp delivery, retailer-owned.

Every customer-facing message goes out from the retailer's own business email
and their own WhatsApp Business number -- never from RecurSaathi. Until a
retailer connects their channels in Settings, messages queue instead of
pretending to send. Platform messages TO the retailer (password reset, welcome
mail) may use the platform SMTP when it is configured.
"""
import smtplib, os, json
from email.message import EmailMessage
import httpx
from . import models, config, vault

# Platform SMTP: used ONLY for messages TO the retailer, never to their customers.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "RecurSaathi <no-reply@recursaathi.in>")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")

# Outbox templates that are RecurSaathi -> retailer (platform mail), not retailer -> customer.
SYSTEM_TPLS = {"welcome"}

def smtp_ready(): return bool(SMTP_HOST and SMTP_USER)
def whatsapp_ready(): return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)

def retailer_email_ready(owner_id) -> bool:
    cn = models.get_connect(owner_id) if owner_id else None
    return bool(cn and cn.get("smtp_host") and cn.get("smtp_user") and not cn.get("paused"))

def retailer_whatsapp_ready(owner_id) -> bool:
    cn = models.get_connect(owner_id) if owner_id else None
    return bool(cn and cn.get("wa_phone_id") and cn.get("wa_token_enc") and not cn.get("paused"))

def _retailer(owner_id):
    """Return (connect_row, reason). reason is plain-language when unusable."""
    cn = models.get_connect(owner_id) if owner_id else None
    if not cn:
        return None, "your business email is not connected yet (Settings > Your business connection)"
    if cn.get("paused"):
        return None, "you paused automations in Settings"
    return cn, ""

def _smtp_send(host, port, user, password, mail_from, to, subject, body):
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = mail_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(host, int(port), timeout=15) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return "sent", ""
    except Exception as e:
        return "failed", str(e)[:300]

def _send_email_raw(to, subject, body, owner_id=None):
    if owner_id:
        cn, err = _retailer(owner_id)
        if not cn:
            return "no_channel", err
        sender = (cn.get("business_name") or "").strip() or cn.get("smtp_user", "")
        return _smtp_send(cn["smtp_host"], cn.get("smtp_port") or 587, cn["smtp_user"],
                          vault.decrypt(cn.get("smtp_pass_enc") or ""),
                          f"{sender} <{cn['smtp_user']}>", to, subject, body)
    if not smtp_ready():
        return "no_channel", "SMTP not configured"
    return _smtp_send(SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, to, subject, body)

def _wa_send(phone_id, token, num, body):
    """Free-form WhatsApp text. Returns (status, err, provider_ref): the ref is
    captured per send, never in a shared global (no cross-send races)."""
    ref = ""
    try:
        with httpx.Client(timeout=15) as cl:
            r = cl.post(
                f"https://graph.facebook.com/v20.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product": "whatsapp",
                      "to": num.lstrip("+"),
                      "type": "text", "text": {"preview_url": False, "body": body}})
        if r.status_code >= 400:
            return "failed", r.text[:300], ref
        try:
            ref = ((r.json().get("messages") or [{}])[0].get("id") or "")
        except Exception:
            ref = ""
        return "sent", "", ref
    except Exception as e:
        return "failed", str(e)[:300], ref


def _wa_send_template(phone_id, token, num, meta_name, lang, params):
    """Approved Meta template send (business-initiated messages outside the
    24h window REQUIRE an approved template; free-form text is rejected).
    Returns (status, err, provider_ref) like _wa_send."""
    ref = ""
    try:
        payload = {"messaging_product": "whatsapp", "to": num.lstrip("+"),
                   "type": "template",
                   "template": {"name": meta_name, "language": {"code": lang or "en"},
                                "components": [{"type": "body",
                                                "parameters": [{"type": "text",
                                                                "text": str(p)[:800]}
                                                               for p in (params or [])]}]}}
        with httpx.Client(timeout=15) as cl:
            r = cl.post(f"https://graph.facebook.com/v20.0/{phone_id}/messages",
                        headers={"Authorization": f"Bearer {token}"}, json=payload)
        if r.status_code >= 400:
            return "failed", r.text[:300], ref
        try:
            ref = ((r.json().get("messages") or [{}])[0].get("id") or "")
        except Exception:
            ref = ""
        return "sent", "", ref
    except Exception as e:
        return "failed", str(e)[:300], ref


def _send_whatsapp_raw(phone_e164, body, owner_id=None):
    num = (phone_e164 or "").replace(" ", "")
    if not num.startswith("+"):
        num = "+91" + num.lstrip("0")
    if owner_id:
        cn, err = _retailer(owner_id)
        if not cn:
            return "no_channel", err, ""
        if not (cn.get("wa_phone_id") and cn.get("wa_token_enc")):
            return "no_channel", "your WhatsApp Business number is not connected yet (Settings > Your business connection)", ""
        return _wa_send(cn["wa_phone_id"], vault.decrypt(cn.get("wa_token_enc") or ""), num, body)
    if not whatsapp_ready():
        return "no_channel", "WhatsApp not configured", ""
    return _wa_send(WHATSAPP_PHONE_ID, WHATSAPP_TOKEN, num, body)


def send_email(to, subject, body, owner_id=None):
    """Plan-aware wrapper: enforces the message quota and counts successful sends."""
    from . import models
    if owner_id:
        lim = models.plan_limit_msg(owner_id, "messages")
        if lim:
            return "limit_reached", lim
    res = _send_email_raw(to, subject, body, owner_id)
    if owner_id and res[0] == "sent":
        models.bump_usage(owner_id, "messages")
    return res


def send_whatsapp(phone_e164, body, owner_id=None):
    """Plan-aware wrapper: enforces the message quota and counts successful sends."""
    from . import models
    if owner_id:
        lim = models.plan_limit_msg(owner_id, "messages")
        if lim:
            return "limit_reached", lim
    res = _send_whatsapp_raw(phone_e164, body, owner_id)
    if owner_id and res[0] == "sent":
        models.bump_usage(owner_id, "messages")
    return res[0], res[1], res[2]


def send_whatsapp_template(phone_e164, meta_name, lang, params, owner_id=None):
    """Send through an APPROVED Meta template. Returns (status, err, ref)."""
    num = (phone_e164 or "").replace(" ", "")
    if not num.startswith("+"):
        num = "+91" + num.lstrip("0")
    if owner_id:
        cn, err = _retailer(owner_id)
        if not cn:
            return "no_channel", err, ""
        if not (cn.get("wa_phone_id") and cn.get("wa_token_enc")):
            return "no_channel", "your WhatsApp Business number is not connected yet", ""
        return _wa_send_template(cn["wa_phone_id"], vault.decrypt(cn.get("wa_token_enc") or ""),
                                 num, meta_name, lang, params)
    if not whatsapp_ready():
        return "no_channel", "WhatsApp not configured", ""
    return _wa_send_template(WHATSAPP_PHONE_ID, WHATSAPP_TOKEN, num, meta_name, lang, params)


def verify_retailer_whatsapp(owner_id):
    """Ping the WhatsApp Cloud API with the retailer's stored creds. (ok, err)."""
    cn = models.get_connect(owner_id) if owner_id else None
    if not cn or not cn.get("wa_phone_id") or not cn.get("wa_token_enc"):
        return False, "not configured"
    try:
        with httpx.Client(timeout=15) as cl:
            r = cl.get(f"https://graph.facebook.com/v20.0/{cn['wa_phone_id']}",
                       headers={"Authorization": "Bearer " + vault.decrypt(cn["wa_token_enc"])})
        return r.status_code < 400, "" if r.status_code < 400 else r.text[:200]
    except Exception as e:
        return False, str(e)[:200]

def deliver(outbox_row):
    """Deliver one queued outbox message.

    Delivery status honesty (founder requirement B): every status transition
    is recorded in outbox_history, a provider ACCEPT is recorded as accepted
    (never delivered), and quiet hours are rechecked immediately before the
    send, not only when the message was scheduled. A provider that cannot be
    reached leaves the message UNKNOWN - unknown never triggers a retry.
    """
    from . import permissions as _perms
    from . import sequences as _seq
    _seq.ensure_schema()
    models.record_delivery_status(outbox_row["id"], outbox_row["status"], "picked up for delivery")
    models.ex("UPDATE outbox SET status='processing' WHERE id=?", (outbox_row["id"],))
    if outbox_row["channel"] not in ("email", "whatsapp"):
        models.set_outbox_status(outbox_row["id"], "no_channel",
                                 f"{outbox_row['channel']} channel not configured yet")
        models.record_delivery_status(outbox_row["id"], "no_channel", "channel not configured")
        return "no_channel"
    owner_id = outbox_row["owner_id"]
    if (outbox_row["tpl"] or "") in SYSTEM_TPLS:
        owner_id = None  # RecurSaathi -> the retailer, not their customer
    if owner_id:
        cn = models.get_connect(owner_id)
        if cn and cn.get("paused"):
            models.ex("UPDATE outbox SET status='queued' WHERE id=?", (outbox_row["id"],))
            models.record_delivery_status(outbox_row["id"], "queued", "owner paused automations")
            return "skipped"  # stays queued; owner paused automations
        # quiet hours, checked immediately before the send (not just at schedule time)
        cfg = _perms.get_config(owner_id)
        if _perms.in_quiet_hours(cfg):
            models.ex("UPDATE outbox SET status='queued' WHERE id=?", (outbox_row["id"],))
            models.record_delivery_status(outbox_row["id"], "queued",
                                          "deferred to the next permitted sending window (quiet hours)")
            return "deferred_quiet_hours"
    if outbox_row["channel"] == "whatsapp":
        cu = models.one("SELECT * FROM customers WHERE phone=? ", (outbox_row["dest"],)) \
            if outbox_row["dest"] else None
        if cu and cu.get("wa_opt_out"):
            models.set_outbox_status(outbox_row["id"], "skipped", "customer opted out")
            models.record_delivery_status(outbox_row["id"], "skipped", "customer opted out")
            return "skipped"
        tmap = models.get_wa_template_map(owner_id, outbox_row["tpl"] or "") if owner_id else None
        params = []
        if tmap:
            try:
                params = [str(p) for p in json.loads(outbox_row.get("tpl_params") or "[]")]
            except Exception:
                params = []
        try:
            if tmap and params:
                st, err, ref = send_whatsapp_template(outbox_row["dest"], tmap["meta_name"],
                                                      tmap.get("lang") or "en", params,
                                                      owner_id=owner_id)
            else:
                st, err, ref = send_whatsapp(outbox_row["dest"], outbox_row["body"], owner_id=owner_id)
                ref = ref if isinstance(ref, str) else ""
        except Exception as e:
            models.set_outbox_status(outbox_row["id"], "unknown",
                                     f"provider unreachable, no retry until it confirms: {e}")
            models.record_delivery_status(outbox_row["id"], "unknown", "provider unreachable")
            return "unknown"
    else:
        try:
            st, err = send_email(outbox_row["dest"], outbox_row["subject"], outbox_row["body"], owner_id=owner_id)
        except Exception as e:
            models.set_outbox_status(outbox_row["id"], "unknown",
                                     f"provider unreachable, no retry until it confirms: {e}")
            models.record_delivery_status(outbox_row["id"], "unknown", "provider unreachable")
            return "unknown"
    models.set_outbox_status(outbox_row["id"], st, err)
    models.record_delivery_status(outbox_row["id"], st, err or "")
    if st == "sent":
        # "sent" means the provider ACCEPTED the message. It is never treated
        # as delivered: delivery_status moves to delivered/read/failed only
        # when the provider itself reports back via /webhooks/provider-status.
        models.ex("UPDATE outbox SET sent=?, provider_ref=?, provider_message_id=?, "
                  "accepted_at=?, delivery_status='accepted', provider_updated=? WHERE id=?",
                  (models.now(), ref, ref, models.now(),
                   models.now(), outbox_row["id"]))
        models.record_delivery_status(outbox_row["id"], "accepted",
                                      f"provider accepted, ref {ref}")
    return st
