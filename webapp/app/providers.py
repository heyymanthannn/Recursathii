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
               timeout=10.0, starttls=False, smtp_user="", smtp_password=""):
    """Real SMTP conversation. Returns an outcome dict, never raises."""
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as s:
            s.ehlo()
            if starttls or smtp_user:
                try:
                    s.starttls()
                    s.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass
            if smtp_user:
                s.login(smtp_user, smtp_password or "")
            refused = s.sendmail(from_addr, [to_addr], _rfc822(from_addr, to_addr, subject, body))
            if refused:
                return {"status": "failed", "provider_id": None,
                        "detail": f"recipients refused: {refused}"}
            return {"status": "accepted", "provider_id": None,
                    "detail": "SMTP server accepted DATA (250)"}
    except smtplib.SMTPRecipientsRefused as e:
        return {"status": "failed", "provider_id": None, "detail": f"recipients refused: {e}"}
    except smtplib.SMTPAuthenticationError as e:
        return {"status": "failed", "provider_id": None,
                "detail": f"authentication rejected by provider: {e}"}
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
            if config.get("email_api_base_url"):
                res = send_email_api(config["email_api_base_url"],
                                     config.get("email_api_token", ""),
                                     config.get("from_addr", "pilot@recursaathi.test"),
                                     o["dest"], o["subject"] or "", o["body"] or "",
                                     timeout=config.get("timeout", 10.0))
            else:
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
        if res.get("provider_id"):
            models.ex("UPDATE outbox SET provider_ref=?, accepted_at=? WHERE id=?",
                      (res["provider_id"], models.now(), o["id"]))
        models.ex("INSERT INTO outbox_history(id,outbox_id,owner_id,status,detail,created) "
                  "VALUES(?,?,?,?,?,?)",
                  (uuid.uuid4().hex, o["id"], owner_id, res["status"],
                   (res["provider_id"] and f"provider_id={res['provider_id']}; " or "") + res["detail"],
                   models.now()))
        out["items"].append({"outbox_id": o["id"], "channel": o["channel"],
                             "status": res["status"], "provider_id": res["provider_id"]})
    return out


# ---- Phase 6.1: sandbox provider credentials, API adapters, signatures -----

import base64, hashlib, hmac as _hmac, json as _json, os as _os
from cryptography.fernet import Fernet, InvalidToken

_SECRET_FIELDS = ("token", "password", "api_key", "wa_token", "smtp_password",
                  "email_api_token", "app_secret")

def _enc():
    raw = _os.environ.get("PROVIDER_ENC_KEY", "")
    if not raw:
        raw = "recursaathi-derived::" + str(models.secret_seed())
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))

def _ensure_creds():
    with models.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS provider_credentials(
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
          kind TEXT NOT NULL, config_enc TEXT NOT NULL, sandbox INTEGER DEFAULT 1,
          created REAL, updated REAL)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS pc_owner_channel ON provider_credentials(owner_id, channel)")

def _mask(cfg):
    out = {}
    for k, v in (cfg or {}).items():
        if k in _SECRET_FIELDS and v:
            sv = str(v)
            out[k] = (sv[:3] + "***") if len(sv) > 3 else "***"
        else:
            out[k] = v
    return out

def configure_provider(owner_id, channel, kind, config, sandbox=True):
    """Store sandbox provider credentials for one business, encrypted at rest.

    Live (non-sandbox) credentials are refused unless the platform-level
    ALLOW_LIVE_PROVIDERS flag is explicitly set - separate founder approval.
    Secrets never appear in audit logs or API responses."""
    _ensure_creds()
    if not sandbox and _os.environ.get("ALLOW_LIVE_PROVIDERS") != "1":
        raise ValueError("live provider credentials are not approved; sandbox only")
    cfg = dict(config or {})
    blob = _enc().encrypt(_json.dumps(cfg).encode()).decode()
    now = models.now()
    with models.conn() as c:
        c.execute("""INSERT INTO provider_credentials(id,owner_id,channel,kind,config_enc,sandbox,created,updated)
                     VALUES(?,?,?,?,?,?,?,?)
                     ON CONFLICT(owner_id,channel) DO UPDATE SET
                       kind=excluded.kind, config_enc=excluded.config_enc,
                       sandbox=excluded.sandbox, updated=excluded.updated""",
                  (models.uid(), owner_id, channel, kind, blob, 1 if sandbox else 0, now, now))
    models.log_audit(owner_id, "provider_configured",
                     _json.dumps({"channel": channel, "kind": kind, "sandbox": bool(sandbox),
                                  "fields": sorted(_mask(cfg).keys())}))
    return {"owner_id": owner_id, "channel": channel, "kind": kind,
            "sandbox": bool(sandbox), "config_masked": _mask(cfg)}

def get_provider_config(owner_id, channel):
    _ensure_creds()
    row = models.one("SELECT * FROM provider_credentials WHERE owner_id=? AND channel=?",
                     (owner_id, channel))
    if not row:
        return None
    try:
        cfg = _json.loads(_enc().decrypt(row["config_enc"].encode()).decode())
    except (InvalidToken, ValueError):
        return None
    cfg["_kind"] = row["kind"]
    cfg["_sandbox"] = bool(row["sandbox"])
    return cfg

def masked_provider_config(owner_id, channel):
    cfg = get_provider_config(owner_id, channel)
    if not cfg:
        return None
    m = _mask({k: v for k, v in cfg.items() if not k.startswith("_")})
    m["kind"], m["sandbox"] = cfg["_kind"], cfg["_sandbox"]
    return m

def verify_hub_signature(body_bytes, header, app_secret):
    """WhatsApp Cloud API X-Hub-Signature-256 check: 'sha256=' + HMAC-SHA256."""
    if not header or not str(header).startswith("sha256="):
        return False
    digest = _hmac.new(str(app_secret).encode(), body_bytes or b"", hashlib.sha256).hexdigest()
    return _hmac.compare_digest(str(header)[7:], digest)


def send_email_api(base_url, token, from_addr, to, subject, body, timeout=10.0):
    """HTTP email API adapter (Resend-style POST /emails). Returns the same
    outcome dict as the SMTP adapter; never claims delivery."""
    url = f"{base_url.rstrip('/')}/emails"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"from": from_addr, "to": [to], "subject": subject, "text": body}
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError, ConnectionError, OSError) as e:
        return {"status": "unknown", "provider_id": None, "detail": f"connection problem: {e}"}
    if r.status_code in (200, 201):
        try:
            mid = r.json().get("id") or ""
        except Exception:
            mid = ""
        if not mid:
            return {"status": "unknown", "provider_id": None,
                    "detail": "2xx without a provider message id"}
        return {"status": "accepted", "provider_id": mid, "detail": f"HTTP {r.status_code}, id {mid}"}
    if 400 <= r.status_code < 500:
        return {"status": "failed", "provider_id": None, "detail": f"HTTP {r.status_code}: client error"}
    return {"status": "unknown", "provider_id": None, "detail": f"HTTP {r.status_code} from provider"}


def handle_bounce(owner_id, dest, channel, reason=""):
    """A provider-reported bounce: the address is unreachable. Marks the
    message bounced, flags the customer so no future sends go there, and
    audits the event. Never a silent drop."""
    _ensure_creds()
    ch = (channel or "email").lower()
    models.ex("UPDATE outbox SET delivery_status='bounced', provider_updated=? "
              "WHERE owner_id=? AND dest=? AND channel=?",
              (models.now(), owner_id, dest, "email" if ch == "email" else "whatsapp"))
    flag = "email_bounced" if ch == "email" else "wa_bounced"
    _ensure_customer_flags()
    models.ex(f"UPDATE customers SET {flag}=1 WHERE owner_id=? AND (email=? OR phone=?)",
              (owner_id, dest, dest))
    models.log_audit(owner_id, "provider_bounce",
                     _json.dumps({"channel": ch, "dest_domain": (dest or "").split("@")[-1],
                                  "reason": (reason or "")[:200]}))
    models.log_activity(owner_id, "delivery", f"provider reported bounce for a {ch} address: {(reason or '')[:120]}")
    return {"ok": True, "dest_suppressed": True}


def _ensure_customer_flags():
    with models.conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(customers)")}
        for f in ("email_bounced", "wa_bounced"):
            if f not in cols:
                c.execute(f"ALTER TABLE customers ADD COLUMN {f} INTEGER DEFAULT 0")


def resolve_owner_config(owner_id):
    """Build the deliver_outbox config for one owner from its stored sandbox
    credentials. Never returns secrets to a caller outside this process."""
    cfg = {}
    email = get_provider_config(owner_id, "email")
    if email:
        if email.get("_kind") == "email_api":
            cfg["email_api_base_url"] = email.get("base_url", "")
            cfg["email_api_token"] = email.get("token", "")
        else:
            cfg["smtp_host"] = email.get("smtp_host", "")
            cfg["smtp_port"] = email.get("smtp_port", 587)
            cfg["smtp_user"] = email.get("smtp_user", "")
            cfg["smtp_password"] = email.get("smtp_password", "")
            cfg["from_addr"] = email.get("from_addr", "")
        cfg["_email_sandbox"] = email.get("_sandbox", True)
    wa = get_provider_config(owner_id, "whatsapp")
    if wa:
        cfg["wa_base_url"] = wa.get("base_url", "")
        cfg["wa_token"] = wa.get("token", "")
        cfg["wa_phone_number_id"] = wa.get("wa_phone_number_id", "")
        cfg["_wa_sandbox"] = wa.get("_sandbox", True)
    return cfg
