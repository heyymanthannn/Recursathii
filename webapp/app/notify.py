"""Email + WhatsApp delivery. Without credentials, messages queue in the
outbox marked no_channel instead of pretending to send."""
import smtplib, os
from email.message import EmailMessage
import httpx
from . import models, config

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "RecurSaathi <no-reply@recursaathi.in>")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")

def smtp_ready(): return bool(SMTP_HOST and SMTP_USER)
def whatsapp_ready(): return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)

def send_email(to, subject, body):
    if not smtp_ready():
        return "no_channel", "SMTP not configured"
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return "sent", ""
    except Exception as e:
        return "failed", str(e)[:300]

def send_whatsapp(phone_e164, body):
    if not whatsapp_ready():
        return "no_channel", "WhatsApp not configured"
    num = (phone_e164 or "").replace(" ", "")
    if not num.startswith("+"):
        num = "+91" + num.lstrip("0")
    try:
        with httpx.Client(timeout=15) as cl:
            r = cl.post(
                f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_ID}/messages",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                json={"messaging_product": "whatsapp",
                      "to": num.lstrip("+"),
                      "type": "text", "text": {"preview_url": False, "body": body}})
        if r.status_code >= 400:
            return "failed", r.text[:300]
        return "sent", ""
    except Exception as e:
        return "failed", str(e)[:300]

def deliver(outbox_row):
    """Deliver one queued outbox message. Opt-out is honored for WhatsApp."""
    if outbox_row["channel"] == "whatsapp":
        cu = models.one("SELECT * FROM customers WHERE phone=? ", (outbox_row["dest"],)) \
            if outbox_row["dest"] else None
        if cu and cu.get("wa_opt_out"):
            models.set_outbox_status(outbox_row["id"], "skipped", "customer opted out")
            return "skipped"
        st, err = send_whatsapp(outbox_row["dest"], outbox_row["body"])
    else:
        st, err = send_email(outbox_row["dest"], outbox_row["subject"], outbox_row["body"])
    models.set_outbox_status(outbox_row["id"], st, err)
    if st == "sent":
        models.ex("UPDATE outbox SET sent=? WHERE id=?", (models.now(), outbox_row["id"]))
    return st
