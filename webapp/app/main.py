"""Payment recovery SaaS - MVP API.

Merchant gateway webhooks -> /webhooks/razorpay
Our own SaaS billing webhooks -> /webhooks/billing
Onboarding + checkout links  -> /onboard, /checkout/{plan}
Recovery dashboard           -> /dashboard/{merchant_id} (JSON + /dashboard.html)
"""
import hmac, hashlib, os
from fastapi import FastAPI, Request, HTTPException, Header, Form
from fastapi.responses import FileResponse, JSONResponse
from . import engine, billing, playbooks, youpi, config, manual_upi
from .config import WEBHOOK_SECRET, BILLING_WEBHOOK_SECRET, PLANS
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="payment-recovery-mvp", version="0.2")

# Cross-origin access from the hosted marketing site (GitHub Pages)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://heyymanthannn.github.io",
        "https://recursaathi.netlify.app",
        "http://localhost:8000", "http://127.0.0.1:8000",
    ],
    allow_methods=["*"], allow_headers=["*"],
)

def _verify(body: bytes, signature: str | None, secret: bytes):
    if not signature or not hmac.compare_digest(
            hmac.new(secret, body, hashlib.sha256).hexdigest(), signature):
        raise HTTPException(status_code=400, detail="bad signature")

@app.post("/webhooks/razorpay")
async def merchant_webhook(request: Request,
                           x_razorpay_signature: str | None = Header(default=None)):
    body = await request.body()
    _verify(body, x_razorpay_signature, WEBHOOK_SECRET)
    payload = await request.json()
    fe = engine.ingest(payload)
    return {"ok": True, "processed": fe is not None,
            "playbook": fe.playbook if fe else None}

@app.post("/webhooks/billing")
async def billing_webhook(request: Request,
                          x_razorpay_signature: str | None = Header(default=None)):
    body = await request.body()
    _verify(body, x_razorpay_signature, BILLING_WEBHOOK_SECRET)
    payload = await request.json()
    event = payload.get("event", "")
    sub = (payload.get("payload") or {}).get("subscription", {}).get("entity", {})
    return billing.handle_billing_event(event, sub)

@app.post("/api/signup")
async def api_signup(request: Request):
    """Landing-page signup: creates a trial merchant and returns a checkout link.

    Provider is chosen by what is configured: Razorpay (real keys), YouPI-PG
    sandbox (YOUPI_API_URL), else a stub checkout so the flow runs end to end.
    """
    import uuid as _uuid, os as _os
    data = await request.json()
    plan, email = data.get("plan"), data.get("email") or ""
    if plan not in PLANS or not email:
        raise HTTPException(status_code=400, detail="plan and email required")
    mid = "m_" + _uuid.uuid4().hex[:10]
    m = billing.onboard(mid, email, plan)
    m.name = data.get("name") or ""
    m.phone = (data.get("phone") or "").lstrip("+")
    billing._persist(m)
    provider, url = "stub", ""
    if config.MANUAL_UPI_ID:
        provider, url = "manual", f"/checkout/{mid}"
    elif _os.environ.get("RAZORPAY_KEY_ID"):
        provider = "razorpay"
        try:
            url = billing.subscription_link(plan, mid)
        except Exception:
            provider, url = "error", ""
    elif _os.environ.get("YOUPI_API_URL") and _os.environ.get("YOUPI_MERCHANT_ID"):
        provider = "youpi"
        try:
            pay = youpi.create_payment(_os.environ["YOUPI_API_URL"], _os.environ["YOUPI_MERCHANT_ID"],
                                       f"SAAS-{mid}", f"{PLANS[plan]['amount'] / 100:.2f}",
                                       _os.environ.get("APP_BASE_URL", "http://localhost:8000/thanks"))
            url = pay.get("checkoutUrl", "")
        except Exception:
            provider, url = "error", ""
    if provider in ("stub", "error"):
        url = f"/checkout/{plan}?merchant_id={mid}"
    return {"merchant_id": mid, "status": m.status, "plan": plan,
            "provider": provider, "checkout_url": url}

def _merchant_row(merchant_id: str) -> dict | None:
    from . import db
    return db.load_merchants().get(merchant_id)


def _pay_amount(plan: str, row: dict | None) -> float:
    """Full plan price, minus any coupon discount already applied."""
    full = PLANS[plan]["amount"] / 100
    pct = (row or {}).get("discount_pct") or 0
    return round(full * (100 - pct) / 100, 2)


@app.get("/checkout/{merchant_id}")
async def checkout_page(merchant_id: str):
    """Checkout: plan summary, coupon entry, live-updating UPI QR."""
    from . import manual_upi, db
    from fastapi.responses import HTMLResponse
    m = billing.MERCHANTS.get(merchant_id)
    if not m or not config.MANUAL_UPI_ID:
        raise HTTPException(status_code=404, detail="checkout page not found")
    row = _merchant_row(merchant_id)
    full = PLANS[m.plan]["amount"] / 100
    amt = _pay_amount(m.plan, row)
    pct = (row or {}).get("discount_pct") or 0
    coupon_msg = ""
    if pct:
        coupon_msg = f'<div class="ok" style="color:#00d4a0;font-weight:600">Coupon applied: {pct}% off. You pay &#8377;{amt:,.0f} instead of &#8377;{full:,.0f}.</div>'
    if m.status == "active":
        coupon_msg = '<div class="ok" style="color:#00d4a0;font-weight:600">Your account is already active - no payment needed.</div>'
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Checkout - RecurSaathi</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>body{{background:#0a0e1a;color:#e8ecf6;font-family:Inter,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px;margin:0}}
.c{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:38px 32px;max-width:440px;width:100%;text-align:center}}
h1{{font-family:'Space Grotesk';font-size:24px;letter-spacing:-.02em;margin:0 0 6px}}
.amt{{font-size:34px;font-family:'Space Grotesk';font-weight:700;color:#00d4a0;margin:14px 0 4px}}
img{{width:200px;height:200px;border-radius:14px;background:#fff;padding:10px;margin:14px 0}}
.vpa{{font-family:monospace;font-size:14px;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:9px 13px;display:inline-block}}
p{{color:#9aa3b8;font-size:14px;line-height:1.6}}
.st{{color:#ffd166;font-size:13px;margin-top:18px}}
form{{margin-top:16px;display:flex;gap:8px;justify-content:center}}
input{{background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.18);border-radius:10px;padding:10px 12px;color:#e8ecf6;font-size:14px;width:150px;text-transform:uppercase}}
button{{background:#00d4a0;border:0;border-radius:10px;padding:10px 16px;font-weight:600;color:#06251d;cursor:pointer}}
.strike{{text-decoration:line-through;color:#9aa3b8}}</style></head><body>
<div class="c"><h1>Checkout</h1>
<p>RecurSaathi {m.plan} plan &middot; 14-day free trial of the platform, then monthly. Money goes straight to the bank, no middleman.</p>
{coupon_msg}
<div class="amt">&#8377;{amt:,.0f}{f' <span class="strike" style="font-size:16px">&#8377;{full:,.0f}</span>' if pct else ''}<span style="font-size:15px;color:#9aa3b8">/month &middot; {m.plan} plan</span></div>
<img src="/pay-qr/{merchant_id}.png" alt="UPI QR code">
<div class="vpa">{config.MANUAL_UPI_ID}</div>
<form method="post" action="/checkout/{merchant_id}/coupon">
<input name="code" placeholder="Coupon code" maxlength="20" {'value=' + chr(34) + (row.get('coupon_code') or '') + chr(34) if row and row.get('coupon_code') else ''} {'disabled' if pct else ''}>
<button {'disabled' if pct else ''}>Apply</button></form>
<p style="margin-top:14px">Scan the QR with Google Pay or any UPI app. The amount is locked to the exact total shown.</p>
<p style="margin-top:6px">Your account activates as soon as we confirm the payment (usually within a few hours).</p>
<div class="st">You'll hear from us on {m.email} once it's live.</div></div></body></html>"""
    return HTMLResponse(html)


@app.post("/checkout/{merchant_id}/coupon")
async def checkout_coupon(merchant_id: str, code: str = Form("")):
    from fastapi.responses import RedirectResponse
    from . import retention
    res = retention.apply_coupon(merchant_id, code)
    if res.get("ok"):
        return RedirectResponse(f"/checkout/{merchant_id}?applied=1", status_code=303)
    return RedirectResponse(f"/checkout/{merchant_id}?error=invalid", status_code=303)


@app.post("/retention/call-outcome")
async def retention_call_outcome(request: Request):
    """Voice provider reports the call outcome: merchant's stated reason."""
    from . import retention
    import json as _json
    data = await request.json()
    mid = data.get("merchant_id") or ""
    return retention.record_call_outcome(mid, data.get("reason") or "")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pay with Google Pay - RecurSaathi</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>body{{background:#0a0e1a;color:#e8ecf6;font-family:Inter,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px;margin:0}}
.c{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:38px 32px;max-width:420px;width:100%;text-align:center}}
h1{{font-family:'Space Grotesk';font-size:24px;letter-spacing:-.02em;margin:0 0 6px}}
.amt{{font-size:34px;font-family:'Space Grotesk';font-weight:700;color:#00d4a0;margin:14px 0 4px}}
img{{width:220px;height:220px;border-radius:14px;background:#fff;padding:10px;margin:18px 0}}
.vpa{{font-family:monospace;font-size:15px;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:10px 14px;display:inline-block}}
p{{color:#9aa3b8;font-size:14px;line-height:1.6}}
.st{{color:#ffd166;font-size:13px;margin-top:18px}}</style></head><body>
<div class="c"><h1>Pay with Google Pay</h1>
<p>Scan this QR with Google Pay (or any UPI app). The money goes straight to our bank account - no third party in between.</p>
<div class="amt">&#8377;{amt:,.0f}<span style="font-size:15px;color:#9aa3b8">/month · {m.plan} plan</span></div>
<img src="/pay-qr/{merchant_id}.png" alt="UPI QR code">
<div class="vpa">{config.MANUAL_UPI_ID}</div>
<p style="margin-top:14px;color:#00d4a0;font-weight:600">Amount locked: the QR carries the exact full Rs {amt:,.0f}. Scan and pay it in one go - partial or smaller payments won't activate the account.</p>
<p style="margin-top:14px">Your account activates as soon as we confirm the payment (usually within a few hours).</p>
<div class="st">You'll hear from us on {m.email} once it's live.</div></div></body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)

@app.get("/pay-qr/{merchant_id}.png")
async def pay_qr(merchant_id: str):
    from . import manual_upi
    from fastapi.responses import Response
    m = billing.MERCHANTS.get(merchant_id)
    if not m or not config.MANUAL_UPI_ID:
        raise HTTPException(status_code=404, detail="qr not found")
    row = _merchant_row(merchant_id)
    return Response(content=manual_upi.qr_png(m.plan, merchant_id, _pay_amount(m.plan, row)), media_type="image/png")

@app.post("/admin/mark-paid")
async def admin_mark_paid(request: Request, x_admin_token: str = Header(default="")):
    """Owner confirms an incoming UPI payment; activates the merchant account."""
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")
    data = await request.json()
    return manual_upi.mark_paid(data.get("merchant_id", ""))

@app.get("/admin/pending")
async def admin_pending(x_admin_token: str = Header(default="")):
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")
    return {"pending": manual_upi.pending(), "activations": manual_upi.activations()}

@app.post("/onboard")
async def onboard(request: Request):
    data = await request.json()
    m = billing.onboard(data["merchant_id"], data["email"], data["plan"])
    return {"merchant_id": m.id, "plan": m.plan, "status": m.status}

@app.get("/checkout/{plan}")
async def checkout(plan: str, merchant_id: str = "demo"):
    if plan not in PLANS:
        raise HTTPException(status_code=404, detail="unknown plan")
    billing.onboard(merchant_id, f"{merchant_id}@example.com", plan)
    return {"plan": plan, "checkout_url": billing.subscription_link(plan, merchant_id)}

@app.get("/dashboard/{merchant_id}")
async def dashboard(merchant_id: str):
    m = billing.MERCHANTS.get(merchant_id)
    return {"merchant": merchant_id,
            "subscription_status": m.status if m else "none",
            "stats": engine.stats(merchant_id)}

@app.post("/scheduler/tick")
async def scheduler_tick():
    return {"retries_fired": engine.run_scheduler()}

@app.get("/plans")
async def plans():
    return PLANS

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse("landing/index.html")

@app.get("/privacy", include_in_schema=False)
async def privacy_page():
    return FileResponse("landing/privacy.html")


@app.get("/terms", include_in_schema=False)
async def terms_page():
    return FileResponse("landing/terms.html")

@app.get("/guide", include_in_schema=False)
async def guide_page():
    return FileResponse("landing/guide.html")

@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return FileResponse("landing/robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    return FileResponse("landing/sitemap.xml", media_type="application/xml")

@app.get("/indexnow-key.txt", include_in_schema=False)
async def indexnow_key():
    return FileResponse("landing/indexnow-key.txt", media_type="text/plain")


@app.post("/webhooks/youpi")
async def youpi_webhook(request: Request):
    """YouPI-PG sandbox gateway webhook (HMAC-SHA256 over raw body)."""
    raw = await request.body()
    if not youpi.verify_signature(raw, request.headers.get("x-gateway-signature"), youpi.youpi_secret()):
        return JSONResponse({"error": "Invalid signature"}, status_code=400)
    import json as _json
    payload = _json.loads(raw.decode("utf-8") or "{}")
    return youpi.ingest_outcome(payload)



# ---------------- Automatic recovery scheduler ----------------
# On Render there is no external cron, so a background thread in the app
# process runs the recovery workflows every 5 minutes: reminders/overdue/
# failed-payment nudges are queued and delivered, and anything that queued
# while a channel (SMTP / WhatsApp) was not configured is retried so it
# flushes the moment the channel is connected.
import threading as _threading
import time as _time
from . import automations as _auto, notify as _notify, models as _models, retention as _retention

def _automation_worker():
    n = 0
    while True:
        _time.sleep(60)
        n += 1
        try:
            # Retention agent: chase unpaid signups 5 minutes after they appear.
            try:
                _retention.run_retention()
            except Exception:
                pass
        except Exception:
            pass
        if n % 5:
            continue
        try:
            owners = _models.q("SELECT * FROM users WHERE role='owner' AND plan_status IN ('active','trialing')")
            for o in owners:
                try:
                    _auto.run_for_owner(o, config.BASE_URL)
                except Exception:
                    pass
            # flush messages that queued while a channel was missing
            try:
                pending = _models.q("SELECT * FROM outbox WHERE status IN ('pending','no_channel') ORDER BY created LIMIT 50")
                for row in pending:
                    try:
                        _notify.deliver(row)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

@app.on_event("startup")
def _start_automation_worker():
    t = _threading.Thread(target=_automation_worker, name="automation-worker", daemon=True)
    t.start()

@app.get("/health/channels")
async def health_channels():
    """Which outbound channels are connected (no secrets returned)."""
    return {"email": _notify.smtp_ready(), "whatsapp": _notify.whatsapp_ready()}

# ---------------- RecurSaathi platform web app ----------------
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import HTTPException as _HTTPException
from . import webhook_ext
from .webui import router as webui_router
from .portal import router as portal_router
from .admin import router as admin_router

app.include_router(webui_router)
app.include_router(portal_router)
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.post("/webhooks/razorpay/platform")
async def platform_webhook_extended(request: Request,
                                    x_razorpay_signature: str | None = Header(default=None)):
    """Invoice-level webhook: signature verified against the PLATFORM secret."""
    body = await request.body()
    _verify(body, x_razorpay_signature, WEBHOOK_SECRET)
    payload = await request.json()
    base = str(request.base_url).rstrip("/")
    fe = engine.ingest(payload)
    extra = webhook_ext.handle(payload, base)
    return {"ok": True, "engine": fe is not None, "invoice": extra}
