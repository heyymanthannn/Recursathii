"""Payment recovery SaaS - MVP API.

Merchant gateway webhooks -> /webhooks/razorpay
Our own SaaS billing webhooks -> /webhooks/billing
Onboarding + checkout links  -> /onboard, /checkout/{plan}
Recovery dashboard           -> /dashboard/{merchant_id} (JSON + /dashboard.html)
"""
import hmac, hashlib, os
from fastapi import FastAPI, Request, HTTPException, Header
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
        provider, url = "manual", f"/pay/{mid}"
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

@app.get("/pay/{merchant_id}")
async def pay_page(merchant_id: str):
    """Manual UPI payment page: QR + amount, direct to the owner's bank."""
    from . import manual_upi
    m = billing.MERCHANTS.get(merchant_id)
    if not m or not config.MANUAL_UPI_ID:
        raise HTTPException(status_code=404, detail="payment page not found")
    amt = PLANS[m.plan]["amount"] / 100
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
<p style="margin-top:18px">Your account activates as soon as we confirm the payment (usually within a few hours).</p>
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
    return Response(content=manual_upi.qr_png(m.plan, merchant_id), media_type="image/png")

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
    return {"pending": manual_upi.pending()}

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


@app.post("/webhooks/youpi")
async def youpi_webhook(request: Request):
    """YouPI-PG sandbox gateway webhook (HMAC-SHA256 over raw body)."""
    raw = await request.body()
    if not youpi.verify_signature(raw, request.headers.get("x-gateway-signature"), youpi.youpi_secret()):
        return JSONResponse({"error": "Invalid signature"}, status_code=400)
    import json as _json
    payload = _json.loads(raw.decode("utf-8") or "{}")
    return youpi.ingest_outcome(payload)


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
