"""Gateway layer. Customer invoice money NEVER flows through the platform's
own account: it either goes via the business owner's own Razorpay keys or
straight to the owner's UPI VPA (manual mode). Platform subscription charges
use the platform's own env credentials only."""
import base64, hmac, hashlib, time
import httpx
from fastapi import HTTPException
from . import models, vault, config

def owner_creds(user):
    kid, ksec = user.get("rzp_key_enc"), user.get("rzp_secret_enc")
    if not kid or not ksec:
        return None
    try:
        return {"key_id": vault.decrypt(kid), "key_secret": vault.decrypt(ksec)}
    except Exception:
        return None

def platform_creds():
    if config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET:
        return {"key_id": config.RAZORPAY_KEY_ID, "key_secret": config.RAZORPAY_KEY_SECRET}
    return None

def _auth(creds):
    raw = f"{creds['key_id']}:{creds['key_secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

def api(creds, method, path, json_body=None):
    url = f"https://api.razorpay.com/v1{path}"
    try:
        with httpx.Client(timeout=20) as cl:
            r = cl.request(method, url, headers=_auth(creds), json=json_body)
        if r.status_code >= 400:
            return {"error": True, "status": r.status_code, "detail": r.text[:300]}
        return r.json()
    except Exception as e:
        return {"error": True, "status": 0, "detail": str(e)}

def create_payment_link(creds, invoice, customer, base_url):
    """Unique payment link for one invoice, settled to the OWNER's account."""
    payload = {
        "amount": invoice["amount"],
        "currency": "INR",
        "accept_partial": False,
        "description": f"Invoice {invoice['number']}",
        "customer": {"name": customer["name"] or "Customer"},
        "notes": {"invoice_id": invoice["id"], "token": invoice["token"]},
        "callback_url": f"{base_url}/pay/{invoice['token']}",
        "callback_method": "get",
    }
    if customer.get("email"): payload["customer"]["email"] = customer["email"]
    if customer.get("phone"): payload["customer"]["contact"] = customer["phone"]
    return api(creds, "POST", "/payment_links", payload)

def create_order(creds, amount, receipt, notes=None):
    return api(creds, "POST", "/orders", {
        "amount": int(amount), "currency": "INR", "receipt": receipt,
        "notes": notes or {}})

def verify_checkout_signature(order_id, payment_id, signature, key_secret):
    msg = f"{order_id}|{payment_id}".encode()
    want = hmac.new(key_secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature)

def verify_webhook_sig(body: bytes, signature: str, secret: str) -> bool:
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature or "")
