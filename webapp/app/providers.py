"""Real-protocol provider adapters and the controlled delivery pipeline.

These adapters speak the provider's actual protocol so that provider
ACCEPTANCE, FAILURES and UNKNOWN states are observed, not simulated by a
stub that flips a column. In the controlled test environment they point at
protocol servers we control (a local SMTP server, a local HTTP endpoint
shaped like the WhatsApp Cloud API). No real customers are ever contacted:
the test rig addresses are pilot-test@rig.invalid style.

Status ladder enforced here:
  accepted  - the provider's server accepted the message (SMTP 250 after
              DATA / Cloud API 200 with a message id). This is NOT delivery.
  failed    - the provider's server rejected the message (5xx / 4xx).
  unknown   - timeout, connection drop, or anything unclassifiable. Unknown
              NEVER triggers a blind retry (hybrid-automation rule).
  delivered - ONLY ever set by the provider's own status webhook
              (/webhooks/provider-status), never by this send path.
"""
import smtplib, socket, time, uuid
from email.message import EmailMessage
import httpx
from . import models


def send_email(smtp_host, smtp_port, from_addr, to_addr, subject, body,
               timeout=10.0, starttls=False):
    """Real SMTP conversation. Returns an outcome dict, never raises."""
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as s:
            s.ehlo()
            if starttls:
                s.starttls()
                s.ehlo()
            refused = s.sendmail(from_addr, [to_addr], _rfc822(from_addr, to_addr, subject, body))
            if refused:
                return {"status": "failed", "provider_id": None,
                        "detail": f"recipients refused: {refused}"}
            return {"status": "accepted", "provider_id": None,
                    "detail": "SMTP server accepted DATA (250)"}
    except smtplib.SMTPRecipientsRefused as e:
        return {"status": "failed", "provider_id": None, "detail": f"recipients refused: {e}"}
    except smtplib.SMTPDataError as e:
        return {"status": "failed", "provider_id": None, "detail": f"DATA rejected: {e}"}
    except (smtplib.SMTPResponseException, smtplib.SMTPConnectError,
            smtplib.SMTPServerDisconnected, smtplib.SMTPHeloError) as e:
        return {"status": "unknown", "provider_id": None, "detail": f"SMTP protocol error: {e}"}
    except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        return {"status": "unknown", "provider_id": None, "detail": f"connection problem: {e}"}


def send_whatsapp(base_url, token, phone_number_id, to, body, timeout=10.0):
    """WhatsApp Cloud API shaped POST. base_url is configurable so the
    controlled test rig (a local HTTP server) can stand in for the real API."""
    url = f"{base_url.rstrip('/')}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
               "text": {"body": body}}
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError, ConnectionError, OSError) as e:
        return {"status": "unknown", "provider_id": None, "detail": f"connection problem: {e}"}
    if r.status_code == 200:
        try:
            mid = (r.json().get("messages") or [{}])[0].get("id") or ""
        except Exception:
            mid = ""
        if not mid:
            return {"status": "unknown", "provider_id": None,
                    "detail": "200 without a provider message id"}
        return {"status": "accepted", "provider_id": mid, "detail": f"HTTP 200, id {mid}"}
    if 400 <= r.status_code < 500:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return {"status": "failed", "provider_id": None,
                "detail": f"HTTP {r.status_code}: {msg or 'client error'}"}
    return {"status": "unknown", "provider_id": None,
            "detail": f"HTTP {r.status_code} from provider"}


def _rfc822(from_addr, to_addr, subject, body):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = from_addr, to_addr, subject
    m.set_content(body)
    return m.as_string()


def deliver_outbox(owner_id, config, limit=50):
    """Send every QUEUED outbox row for one owner through the configured
    provider. Records the real status ladder and provider message ids in
    outbox_history. Never sets delivered, never retries unknown."""
    rows = models.q("SELECT * FROM outbox WHERE owner_id=? AND status='queued' "
                    "ORDER BY created LIMIT ?", (owner_id, limit))
    out = {"attempted": 0, "accepted": 0, "failed": 0, "unknown": 0, "items": []}
    for o in rows:
        out["attempted"] += 1
        if (o["channel"] or "").lower() == "email":
            res = send_email(config["smtp_host"], config["smtp_port"],
                             config.get("from_addr", "pilot@recursaathi.test"),
                             o["dest"], o["subject"] or "", o["body"] or "",
                             timeout=config.get("timeout", 10.0))
        elif (o["channel"] or "").lower() in ("whatsapp", "wa"):
            res = send_whatsapp(config["wa_base_url"], config.get("wa_token", ""),
                                config.get("wa_phone_number_id", ""), o["dest"],
                                o["body"] or "", timeout=config.get("timeout", 10.0))
        else:
            res = {"status": "unknown", "provider_id": None,
                   "detail": f"no provider adapter for channel '{o['channel']}'"}
        out[res["status"]] += 1
        models.set_outbox_status(o["id"], res["status"], res["detail"])
        models.ex("INSERT INTO outbox_history(id,outbox_id,owner_id,status,detail,created) "
                  "VALUES(?,?,?,?,?,?)",
                  (uuid.uuid4().hex, o["id"], owner_id, res["status"],
                   (res["provider_id"] and f"provider_id={res['provider_id']}; " or "") + res["detail"],
                   models.now()))
        out["items"].append({"outbox_id": o["id"], "channel": o["channel"],
                             "status": res["status"], "provider_id": res["provider_id"]})
    return out
