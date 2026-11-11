"""Payment recovery SaaS - MVP API.

Merchant gateway webhooks -> /webhooks/razorpay
Our own SaaS billing webhooks -> /webhooks/billing
Onboarding + checkout links  -> /onboard, /checkout/{plan}
Recovery dashboard           -> /dashboard/{merchant_id} (JSON + /dashboard.html)
"""
import hmac, hashlib, os
from fastapi import FastAPI, Request, HTTPException, Header, Form
from fastapi.responses import FileResponse, JSONResponse
from . import engine, billing, playbooks, youpi, config, manual_upi, auth
from .ratelimit import allow as rl_allow
from .config import WEBHOOK_SECRET, BILLING_WEBHOOK_SECRET, PLANS
from fastapi.responses import HTMLResponse, Response
import html as _html
import re as _re

def _esc(v) -> str:
    """HTML-escape anything user-supplied before it lands in a page."""
    return _html.escape(str(v or ""), quote=True)

def _admin_digest(tok: str) -> str:
    # Cookie never carries the raw admin token, only its SHA-256 digest.
    return hashlib.sha256((tok or "").encode()).hexdigest()

_ADMIN_COOKIE = _admin_digest(config.ADMIN_TOKEN)

def _cookie_secure() -> bool:
    # Secure flag only where we actually serve https (Render), so localhost dev keeps working.
    return (config.BASE_URL or "").startswith("https")

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

# ---------------- Security headers + CSRF guard (Phase 7.6) ----------------
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: https:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

def _origin_allowed(request: Request) -> bool:
    """CSRF defense: browsers attach ambient cookies to cross-site
    state-changing requests. If Origin/Referer names another site, reject.
    No Origin/Referer means a non-browser client (curl, gateway webhooks,
    cron, API keys) which cannot carry a CSRF'd browser session, so it stays
    allowed."""
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    if not src:
        return True
    from urllib.parse import urlparse
    host = (urlparse(src).netloc or "").lower()
    if not host:
        return True
    allowed = {(urlparse(config.BASE_URL or "").netloc or "").lower(),
               (request.headers.get("host") or "").lower()}
    return (host in allowed
            or host.startswith("localhost") or host.startswith("127.0.0.1"))

@app.middleware("http")
async def security_hardening(request: Request, call_next):
    if (request.method in _CSRF_UNSAFE_METHODS
            and request.cookies.get(auth.SESSION_COOKIE)
            and not _origin_allowed(request)):
        return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
    resp = await call_next(request)
    _https = (request.url.scheme == "https"
              or (request.headers.get("x-forwarded-proto") or "").lower() == "https")
    if _https:
        _cookies = resp.headers.getlist("set-cookie")
        if any(auth.SESSION_COOKIE in c and "secure" not in c.lower() for c in _cookies):
            del resp.headers["set-cookie"]
            for _c in _cookies:
                if auth.SESSION_COOKIE in _c and "secure" not in _c.lower():
                    _c = _c + "; Secure"
                resp.headers.append("set-cookie", _c)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Strict-Transport-Security",
                            "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    return resp

def _verify(body: bytes, signature: str | None, secret: bytes):
    if not secret:
        # Phase 7.6: no secret configured -> accept nothing, ever.
        raise HTTPException(status_code=503, detail="webhook secret not configured")
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
    _ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    if not rl_allow("api_signup:" + _ip, 5, 3600):
        raise HTTPException(status_code=429, detail="Too many signups from this connection. Try again in an hour.")
    """Landing-page signup: creates a trial merchant and returns a checkout link.

    Provider is chosen by what is configured: Razorpay (real keys), YouPI-PG
    sandbox (YOUPI_API_URL), else a stub checkout so the flow runs end to end.
    """
    import uuid as _uuid, os as _os
    data = await request.json()
    plan, email = data.get("plan"), data.get("email") or ""
    plan = {"scale": "enterprise"}.get(plan, plan)
    if plan not in PLANS or not email:
        raise HTTPException(status_code=400, detail="plan and email required")
    email = email.strip().lower()
    if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if not (data.get("phone") or "").strip():
        raise HTTPException(status_code=400, detail="phone number required to start your trial")
    pw = (data.get("password") or "")
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="Password needs at least 8 characters.")
    from . import models
    if models.get_user_by_email(email):
        raise HTTPException(status_code=400, detail="That email already has an account. Sign in at /login with your password to continue.")
    u = models.create_user(email, auth.hash_pw(pw), data.get("name") or "",
                           data.get("name") or "", (data.get("phone") or "").lstrip("+"),
                           plan=plan)
    import time as _time
    models.update_user(u["id"], trial_started_at=_time.time(),
                       trial_ends=_time.time() + 14 * 86400)  # trial starts at signup, regardless of plan
    # email verification (soft): login is never blocked, a banner reminds until verified
    import uuid as _uuid3
    _vtok = _uuid3.uuid4().hex
    models.set_verify_token(u["id"], _vtok)
    try:
        from . import notify as _notify
        if _notify.smtp_ready():
            _notify.send_email(email, "Verify your email - RecurSaathi",
                f"Welcome to RecurSaathi, {data.get('name') or ''}!\n\nConfirm your email address (takes one click):\n"
                f"{config.BASE_URL}/verify/{_vtok}\n\n"
                "If you didn't create this account you can ignore this email.")
    except Exception:
        pass
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
            "provider": provider, "checkout_url": url, "account_created": True}

def _merchant_row(merchant_id: str) -> dict | None:
    from . import db
    return db.load_merchants().get(merchant_id)


def _pay_amount(plan: str, row: dict | None) -> float:
    """Full plan price, minus any coupon discount already applied."""
    full = PLANS[plan]["amount"] / 100
    pct = (row or {}).get("discount_pct") or 0
    return round(full * (100 - pct) / 100, 2)


@app.get("/checkout/{merchant_id}")
async def checkout_page(merchant_id: str, claimed: int = 0):
    """Premium checkout: plan summary, coupon entry, live-updating UPI QR."""
    from . import manual_upi, db
    from fastapi.responses import HTMLResponse
    m = billing.MERCHANTS.get(merchant_id)
    row = _merchant_row(merchant_id)
    if not m and row:
        m = billing.Merchant(merchant_id, row.get("email", ""), row.get("plan") or "starter")
        billing.MERCHANTS[merchant_id] = m
    if not m or not config.MANUAL_UPI_ID:
        raise HTTPException(status_code=404, detail="checkout page not found")
    if row:
        m.status = row.get("status") or m.status
        m.name = row.get("name") or m.name
        m.phone = row.get("phone") or m.phone
    full = PLANS[m.plan]["amount"] / 100
    amt = _pay_amount(m.plan, row)
    pct = (row or {}).get("discount_pct") or 0
    plan_name = {"starter": "Starter", "growth": "Growth", "enterprise": "Enterprise",
                 "scale": "Enterprise"}.get(m.plan, m.plan.title())
    note = ""
    if pct:
        note = f'<div class="ok">Coupon applied: {pct}% off. You pay &#8377;{amt:,.0f} instead of &#8377;{full:,.0f}.</div>'
    if m.status == "active":
        note = '<div class="ok">Your account is already active - no payment needed.</div>'
    if claimed and m.status == "trial":
        note = '<div class="ok">Payment submitted. You\'re all set - your account goes live the moment it\'s confirmed.</div>'
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Checkout · RecurSaathi</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%237c6cff'/%3E%3Cstop offset='1' stop-color='%2346d8ff'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='32' height='32' rx='9' fill='url(%23g)'/%3E%3Cpath d='M16 9a7 7 0 1 0 7 7' stroke='white' stroke-width='3' fill='none' stroke-linecap='round'/%3E%3Cpath d='M23 12v4h-4' stroke='white' stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<meta name="theme-color" content="#faf8f3">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,400..700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box}}
body{{background:#faf8f3;color:#23233b;font-family:Inter,sans-serif;margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:28px 16px}}
.w{{width:100%;max-width:520px}}
.top{{display:flex;align-items:center;gap:10px;margin-bottom:22px}}
.top svg{{width:30px;height:30px}}
.brand{{font-family:Fraunces,serif;font-size:20px;color:#20264a}}
.brand b{{color:#7c6cff}}
.card{{background:#fff;border:1px solid #ece8df;border-radius:18px;padding:28px;box-shadow:0 10px 30px rgba(32,38,74,.06);margin-bottom:16px}}
.pill{{display:inline-block;background:#f0edff;color:#5b4bd6;font-size:12px;font-weight:600;padding:4px 12px;border-radius:99px;letter-spacing:.02em}}
h1{{font-family:Fraunces,serif;font-size:26px;color:#20264a;margin:12px 0 4px}}
.sub{{color:#6b6f8e;font-size:14px;line-height:1.55;margin:0 0 14px}}
.amt{{font-size:38px;font-weight:700;color:#20264a;letter-spacing:-.02em}}
.amt small{{font-size:15px;color:#6b6f8e;font-weight:500}}
.strike{{text-decoration:line-through;color:#9aa0bd;font-size:18px;font-weight:500;margin-left:8px}}
.line{{display:flex;justify-content:space-between;font-size:14px;color:#4a4e6d;padding:9px 0;border-bottom:1px solid #f2efe8}}
.line b{{color:#20264a}}
.qr{{text-align:center;padding:18px 0 6px}}
.qr img{{width:190px;height:190px;border-radius:14px;background:#fff;padding:8px;border:1px solid #ece8df}}
.vpa{{font-family:ui-monospace,monospace;font-size:14px;background:#f5f3ec;border:1px solid #ece8df;border-radius:10px;padding:9px 14px;display:inline-block;color:#3a3f63}}
.qr p{{color:#6b6f8e;font-size:13px;margin:8px 0 0}}
form{{display:flex;gap:8px;margin-top:14px}}
input{{flex:1;border:1px solid #e2ddcf;border-radius:10px;padding:11px 12px;font-size:14px;background:#fff;color:#23233b;text-transform:uppercase}}
.btn{{background:linear-gradient(135deg,#7c6cff,#5a8dff);border:0;border-radius:12px;padding:13px 18px;font-weight:600;color:#fff;font-size:15px;cursor:pointer;width:100%}}
.btn:hover{{filter:brightness(1.05)}}
.btn.ghost{{background:#fff;color:#5b4bd6;border:1px solid #d8d2ff}}
.ok{{background:#eefaf3;border:1px solid #bfead4;color:#0d7a4a;font-size:13.5px;font-weight:600;border-radius:10px;padding:10px 14px;margin-bottom:14px}}
.fine{{color:#8a8fae;font-size:12px;line-height:1.6;margin:12px 0 0}}
a{{color:#5b4bd6}}
.foot{{text-align:center;color:#9aa0bd;font-size:12px;margin-top:6px}}
@media(max-width:480px){{.card{{padding:20px}}.amt{{font-size:32px}}}}
</style></head><body>
<div class="w">
<div class="top">
<svg viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7c6cff"/><stop offset="1" stop-color="#46d8ff"/></linearGradient></defs><rect width="32" height="32" rx="9" fill="url(#g)"/><path d="M16 9a7 7 0 1 0 7 7" stroke="white" stroke-width="3" fill="none" stroke-linecap="round"/><path d="M23 12v4h-4" stroke="white" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
<span class="brand">Recur<b>Saathi</b></span></div>
<div class="card">
<span class="pill">{plan_name} plan</span>
<h1>Your subscription</h1>
<p class="sub">14-day free trial of the full platform, then billed monthly. Cancel any time from your billing page.</p>
{note}
<div class="amt">&#8377;{amt:,.0f}{f'<span class="strike">&#8377;{full:,.0f}</span>' if pct else ''}<small> /month</small></div>
<div style="margin:12px 0 4px">
<div class="line"><span>Plan</span><b>{plan_name}</b></div>
<div class="line"><span>Billing</span><b>Monthly</b></div>
<div class="line"><span>Account</span><b>{m.email}</b></div>
</div>
<div class="qr">
<img src="/pay-qr/{merchant_id}.png" alt="UPI QR code">
<div style="margin-top:10px"><span class="vpa">{config.MANUAL_UPI_ID}</span></div>
<p>Scan with Google Pay or any UPI app. The amount is locked to the exact total shown.</p>
</div>
<form method="post" action="/checkout/{merchant_id}/coupon">
<input name="code" placeholder="Coupon code" maxlength="20" {'value=' + chr(34) + _esc(row.get('coupon_code') or '') + chr(34) if row and row.get('coupon_code') else ''} {'disabled' if pct else ''}>
<button class="btn ghost" style="width:auto" {'disabled' if pct else ''}>Apply</button></form>
<form method="post" action="/checkout/{merchant_id}/paid-claim" style="margin-top:6px">
<button class="btn">I've paid</button></form>
<p class="sub" style="margin-top:10px;text-align:center;font-size:13px">Your account goes live the moment payment is confirmed. You'll hear from us on {_esc(m.email)}.</p>
<p class="fine">Prices exclude 18% GST; payment provider charges may apply. Your customers' invoice payments never come here - they go straight to your own bank via your UPI ID or your own gateway. Subscription payments go to RecurSaathi.</p>
</div>
<div class="foot">Already have an account? <a href="/login">Sign in</a> &middot; <a href="/">recursathii.onrender.com</a></div>
</div></body></html>"""
    return HTMLResponse(html)


@app.post("/checkout/{merchant_id}/paid-claim")
async def checkout_paid_claim(merchant_id: str, utr: str = Form("")):
    """Customer self-reports payment with UTR; instant verify pipeline input."""
    from fastapi.responses import RedirectResponse
    from . import db
    import time as _time
    m = billing.MERCHANTS.get(merchant_id)
    row = _merchant_row(merchant_id)
    if not m and row:
        m = billing.Merchant(merchant_id, row.get("email", ""), row.get("plan") or "starter")
        billing.MERCHANTS[merchant_id] = m
        m.status = row.get("status") or "trial"
    if not m or not config.MANUAL_UPI_ID:
        raise HTTPException(status_code=404, detail="checkout page not found")
    row = _merchant_row(merchant_id) or {}
    row["claimed_utr"] = (utr or "").strip().upper()
    row["claimed_at"] = _time.time()
    db.save_merchant(row)
    return RedirectResponse(f"/checkout/{merchant_id}?claimed=1", status_code=303)

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
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    data = await request.json()
    return manual_upi.mark_paid(data.get("merchant_id", ""))

@app.post("/admin/plat-confirm")
async def admin_plat_confirm(request: Request, x_admin_token: str = Header(default="")):
    """Confirm a pending subscription payment -> activates that business's plan."""
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    from . import models
    data = await request.json()
    pay = models.one("SELECT * FROM plat_pays WHERE id=?", (data.get("payment_id", ""),))
    if not pay or pay["status"] not in ("pending", "pending_upgrade"):
        return {"ok": False, "error": "payment not pending"}
    models.ex("UPDATE plat_pays SET status='paid' WHERE id=?", (pay["id"],))
    models.update_user(pay["owner_id"], plan=pay["plan"], plan_status="active", trial_ends=None)
    return {"ok": True, "owner": pay["owner_id"], "plan": pay["plan"]}


@app.get("/admin")
async def admin_panel(request: Request):
    """Owner admin panel: token login, pending payments, one-click confirm."""
    from fastapi.responses import HTMLResponse, RedirectResponse
    tok = request.cookies.get("admin_token", "")
    if not hmac.compare_digest(tok, _ADMIN_COOKIE):
        html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin - RecurSaathi</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>body{background:#0a0e1a;color:#e8ecf6;font-family:Inter,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px;margin:0}
.c{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:38px 32px;max-width:420px;width:100%;text-align:center}
h1{font-family:'Space Grotesk';font-size:24px;letter-spacing:-.02em;margin:0 0 8px}p{color:#9aa3b8;font-size:14px;line-height:1.6}
input{width:100%;box-sizing:border-box;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.14);border-radius:10px;padding:12px 14px;color:#e8ecf6;font-size:15px;margin:16px 0 4px}
button{width:100%;background:linear-gradient(135deg,#7c6cf8,#5ac8fa);border:0;border-radius:10px;padding:12px 0;color:#fff;font-size:15px;font-weight:600;margin-top:10px;cursor:pointer}
.err{color:#ff6b6b;font-size:13px;margin-top:10px}</style></head><body><div class="c">
<h1>RecurSaathi Admin</h1><p>Enter your admin token to see pending payments and confirm them with one tap.</p>
<form method="post" action="/admin/login"><input type="password" name="token" placeholder="Admin token" autocomplete="off" required autofocus>
<button type="submit">Open admin panel</button></form>{err}</div></body></html>"""
        err = '<div class="err">Wrong token, try again.</div>' if request.query_params.get("e") == "1" else ""
        return HTMLResponse(html.replace("{err}", err))

    rows = [m for m in manual_upi.pending()]
    cards = []
    for m in rows:
        amt = _pay_amount(m.get("plan", ""), m)
        utr = (m.get("claimed_utr") or "").strip()
        utr_html = f'<div class="utr">UTR claimed: <b>{_esc(utr)}</b></div>' if utr else '<div class="utr dim">No UTR claimed yet</div>'
        cards.append(f"""<div class="card"><div class="row1"><b>{_esc(m.get('name') or m.get('email') or 'Merchant')}</b><span class="plan">{_esc(m.get('plan',''))}</span></div>
<div class="meta">{_esc(m.get('email',''))} · {_esc(m.get('phone') or 'no phone')}</div>
<div class="amt">&#8377;{amt:,.0f}<span> / {m.get('plan','')} · monthly</span></div>{utr_html}
<form method="post" action="/admin/confirm"><input type="hidden" name="merchant_id" value="{m.get('id','')}"><button type="submit">Confirm paid &amp; activate</button></form></div>""")
    if not cards:
        cards = ['<div class="empty">No pending payments right now. New trial signups land here the moment they say "I\'ve paid".</div>']
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin - RecurSaathi</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>body{background:#0a0e1a;color:#e8ecf6;font-family:Inter,sans-serif;margin:0;padding:32px 16px}
.wrap{max-width:640px;margin:0 auto}h1{font-family:'Space Grotesk';font-size:26px;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:#9aa3b8;font-size:14px;margin:0 0 24px}
.card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:20px 22px;margin-bottom:14px}
.row1{display:flex;justify-content:space-between;align-items:center;font-size:16px}
.plan{background:rgba(124,108,248,.16);border:1px solid rgba(124,108,248,.3);color:#b9b1ff;border-radius:20px;padding:3px 12px;font-size:12px;font-weight:600}
.meta{color:#9aa3b8;font-size:13px;margin:6px 0 10px}
.amt{font-size:26px;font-family:'Space Grotesk';font-weight:700;color:#00d4a0}.amt span{font-size:13px;color:#9aa3b8;font-weight:400;font-family:Inter}
.utr{font-size:13px;color:#ffd166;margin:8px 0 14px}.utr.dim{color:#9aa3b8}
button{width:100%;background:linear-gradient(135deg,#00d4a0,#00b389);border:0;border-radius:10px;padding:12px 0;color:#04121e;font-size:15px;font-weight:700;cursor:pointer}
.empty{background:rgba(255,255,255,.04);border:1px dashed rgba(255,255,255,.16);border-radius:16px;padding:28px 22px;color:#9aa3b8;font-size:14px;text-align:center}</style></head><body>
<div class="wrap"><h1>RecurSaathi Admin</h1><p class="sub">Pending payments waiting for your confirmation</p>{CARDS}</div></body></html>"""
    return HTMLResponse(html.replace("{CARDS}", "".join(cards)))

@app.post("/admin/login")
async def admin_login(request: Request):
    from fastapi.responses import RedirectResponse
    form = await request.form()
    if not hmac.compare_digest(form.get("token") or "", config.ADMIN_TOKEN):
        return RedirectResponse("/admin?e=1", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("admin_token", _ADMIN_COOKIE, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=_cookie_secure())
    return resp

@app.post("/admin/confirm")
async def admin_confirm(request: Request):
    from fastapi.responses import RedirectResponse
    if not hmac.compare_digest(request.cookies.get("admin_token", ""), _ADMIN_COOKIE):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    manual_upi.mark_paid(form.get("merchant_id", ""))
    return RedirectResponse("/admin", status_code=303)

@app.get("/admin/pending")
async def admin_pending(x_admin_token: str = Header(default="")):
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return {"pending": manual_upi.pending(), "activations": manual_upi.activations()}

@app.post("/onboard")
async def onboard(request: Request):
    data = await request.json()
    _em = (data.get("email") or "").strip().lower()
    if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", _em):
        raise HTTPException(status_code=400, detail="valid email required")
    m = billing.onboard(data["merchant_id"], _em, data["plan"])
    return {"merchant_id": m.id, "plan": m.plan, "status": m.status}

@app.get("/checkout/{plan}")
async def checkout(plan: str, merchant_id: str = "demo"):
    plan = {"scale": "enterprise"}.get(plan, plan)
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
async def scheduler_tick(request: Request, x_scheduler_token: str = Header(default="")):
    # Security fix: this endpoint mutates account state (trials, outbox). It now
    # requires the shared secret from SCHEDULER_TICK_TOKEN (header X-Scheduler-Token)
    # and fails closed when the env var is not set. The Render cron that calls it
    # must be updated in the same deploy: set SCHEDULER_TICK_TOKEN + send the header.
    _tick_token = os.environ.get("SCHEDULER_TICK_TOKEN", "")
    if not _tick_token or not hmac.compare_digest(x_scheduler_token, _tick_token):
        raise HTTPException(status_code=403, detail="scheduler token required")
    import time
    from . import models
    now = time.time()
    expired = queued = 0
    base = config.BASE_URL.rstrip("/")
    for u in models.q("SELECT * FROM users WHERE plan_status='trialing' AND trial_ends IS NOT NULL", ()):
        if u["trial_ends"] < now:
            models.update_user(u["id"], plan_status="expired")
            expired += 1
            continue
        started = u["trial_started_at"] or (u["trial_ends"] - 14 * 86400)
        days_in = (now - started) / 86400.0
        notes = (
            (7, "Your RecurSaathi trial: 7 days left",
             "Hi there,\n\nYou are 7 days into your RecurSaathi free trial. Recovery Radar, automations and analytics are live on your dashboard.\n\nSubscribe any time at " + base + "/billing - we never charge you automatically.\n\n- Team RecurSaathi"),
            (12, "2 days left in your RecurSaathi trial",
             "Hi there,\n\nYour free trial ends in 2 days. Pick a plan at " + base + "/billing to keep recovering revenue without interruption.\n\n- Team RecurSaathi"),
            (14, "Your RecurSaathi trial ends today",
             "Hi there,\n\nYour free trial ends today. Your data is safe, but recovery features pause until you subscribe. Choose a plan at " + base + "/billing.\n\n- Team RecurSaathi"),
        )
        for d, subj, body in notes:
            dk = f"trial-d{d}-{u['id']}"
            if days_in >= d and not models.one("SELECT 1 FROM outbox WHERE dedupe=?", (dk,)):
                models.queue(u["id"], "email", u["email"], "trial_reminder", subj, body, dedupe=dk)
                queued += 1
    return {"retries_fired": engine.run_scheduler(),
            "trial_notifications_queued": queued, "trials_expired": expired}

@app.get("/plans")
async def plans():
    return PLANS

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse("landing/index.html")

@app.get("/privacy", include_in_schema=False)
async def privacy_page():
    return FileResponse("landing/privacy.html")

@app.get("/config.js")
def _config_js():
    return FileResponse("landing/config.js", media_type="application/javascript")

@app.get("/site.css")
def _site_css():
    return FileResponse("landing/site.css", media_type="text/css")


@app.get("/terms", include_in_schema=False)
async def terms_page():
    return FileResponse("landing/terms.html")

@app.get("/upi-autopay-recovery", include_in_schema=False)
async def upi_autopay_recovery_page():
    return FileResponse("landing/upi-autopay-recovery.html")

@app.get("/guide", include_in_schema=False)
async def guide_page():
    return FileResponse("landing/guide.html")

@app.get("/failed-payment-calculator", include_in_schema=False)
async def calculator_page():
    return FileResponse("landing/calculator.html")

@app.get("/about", include_in_schema=False)
async def about_page():
    return FileResponse("landing/about.html")

@app.get("/rbi-retry-window-checker", include_in_schema=False)
async def retry_checker_page():
    return FileResponse("landing/retry-checker.html")

@app.get("/upi-autopay-failure-decoder", include_in_schema=False)
async def failure_decoder_page():
    return FileResponse("landing/failure-decoder.html")

@app.get("/payment-failed-money-deducted", include_in_schema=False)
async def money_deducted_page():
    return FileResponse("landing/money-deducted.html")

@app.get("/upi-autopay-mandate-failed-reasons", include_in_schema=False)
async def mandate_failures_page():
    return FileResponse("landing/mandate-failures.html")

@app.get("/razorpay-autopay-failed-payment-retry", include_in_schema=False)
async def razorpay_retry_page():
    return FileResponse("landing/razorpay-retry.html")

@app.get("/ai-invoice-reminder-software", include_in_schema=False)
async def ai_invoice_reminder_page():
    return FileResponse("landing/ai-invoice-reminder.html")

@app.get("/overdue-invoice-recovery", include_in_schema=False)
async def overdue_invoice_page():
    return FileResponse("landing/overdue-invoice-recovery.html")

@app.get("/customer-reactivation", include_in_schema=False)
async def customer_reactivation_page():
    return FileResponse("landing/customer-reactivation.html")

@app.get("/for/gyms", include_in_schema=False)
async def for_gyms_page():
    return FileResponse("landing/for-gyms.html")

@app.get("/for/coaching-institutes", include_in_schema=False)
async def for_coaching_page():
    return FileResponse("landing/for-coaching.html")

@app.get("/for/d2c-subscriptions", include_in_schema=False)
async def for_d2c_page():
    return FileResponse("landing/for-d2c.html")

@app.get("/vs/razorpay-failed-payments-recovery", include_in_schema=False)
async def vs_razorpay_page():
    return FileResponse("landing/vs-razorpay.html")

@app.get("/pricing", include_in_schema=False)
async def pricing_page():
    return FileResponse("landing/pricing.html")

@app.get("/contact", include_in_schema=False)
async def contact_page():
    return FileResponse("landing/contact-page.html")

@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return FileResponse("landing/robots.txt", media_type="text/plain")

@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    return FileResponse("landing/llms.txt", media_type="text/markdown")


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
from . import automations as _auto, notify as _notify, models as _models, retention as _retention, backup as _backup

def _automation_worker():
    n = 0
    last_backup = _time.time()
    while True:
        _time.sleep(60)
        n += 1
        try:
            if _time.time() - last_backup >= _backup.BACKUP_INTERVAL_S:
                st = _backup.run_once("sched")
                print(f"[backup] scheduled: {st}", flush=True)
                last_backup = _time.time()  # set even on error: retry next interval, not every minute
        except Exception:
            pass
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
                try:
                    from .webui import run_custom_rules
                    run_custom_rules(o["id"], config.BASE_URL)
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
    def _boot_backup():
        _time.sleep(45)  # let the app finish serving first requests
        st = _backup.run_once("boot")
        print(f"[backup] boot: {st}", flush=True)
    t = _threading.Thread(target=_automation_worker, name="automation-worker", daemon=True)
    tb = _threading.Thread(target=_boot_backup, name="boot-backup", daemon=True)
    t.start()
    tb.start()

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
from .assistant import router as assistant_router
from .admin import router as admin_router

app.include_router(webui_router)
app.include_router(portal_router)
app.include_router(assistant_router)
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


# ---------------- Enterprise REST API (API-key authenticated) ----------------
from fastapi import Header as _Header

def _api_owner(x_api_key: str | None):
    owner = _models.owner_for_api_key(x_api_key or "")
    if not owner:
        raise _HTTPException(status_code=401, detail="Invalid or missing API key")
    if not config.has_feature(owner.get("plan") or "starter", "api_access"):
        raise _HTTPException(status_code=403, detail="API access is an Enterprise feature")
    return owner

@app.get("/api/v1/customers")
async def api_v1_customers(request: Request, x_api_key: str | None = _Header(default=None),
                           limit: int = 100, offset: int = 0):
    """List customers (Enterprise API). Auth: X-API-Key header."""
    owner = _api_owner(x_api_key)
    limit = max(1, min(limit, 500))
    rows = _models.q("SELECT id, name, email, phone, notes, created FROM customers "
                     "WHERE owner_id=? ORDER BY created DESC LIMIT ? OFFSET ?",
                     (owner["id"], limit, max(0, offset)))
    total = _models.q("SELECT COUNT(*) AS c FROM customers WHERE owner_id=?", (owner["id"],))[0]["c"]
    return {"count": len(rows), "total": total, "customers": rows}

@app.get("/api/v1/invoices")
async def api_v1_invoices(request: Request, x_api_key: str | None = _Header(default=None),
                          status: str = "", limit: int = 100, offset: int = 0):
    """List invoices (Enterprise API). Auth: X-API-Key header."""
    owner = _api_owner(x_api_key)
    limit = max(1, min(limit, 500))
    if status not in ("", "pending", "overdue", "paid"):
        status = ""
    where, args = "owner_id=?", [owner["id"]]
    if status:
        where += " AND status=?"
        args.append(status)
    rows = _models.q(f"SELECT id, number, amount, due_date, status, created, paid_at "
                     f"FROM invoices WHERE {where} ORDER BY created DESC LIMIT ? OFFSET ?",
                     args + [limit, max(0, offset)])
    total = _models.q(f"SELECT COUNT(*) AS c FROM invoices WHERE {where}", args)[0]["c"]
    return {"count": len(rows), "total": total, "invoices": rows}

@app.get("/api/v1/summary")
async def api_v1_summary(request: Request, x_api_key: str | None = _Header(default=None)):
    """Key recovery metrics (Enterprise API). Auth: X-API-Key header."""
    owner = _api_owner(x_api_key)
    return {"metrics": _models.owner_metrics(owner["id"]), "usage": _models.plan_usage(owner["id"])}

# ---- hybrid automation architecture, Phase 2: automation control centre ----
from . import permissions as _permissions, sequences as _sequences

def _control_owner(x_api_key: str | None):
    """Resolve the business owner for control-centre endpoints (X-API-Key,
    the owner's own key). No plan gate here: every business controls its own
    automations; the Enterprise gate only applies to the metrics API."""
    owner = _models.owner_for_api_key(x_api_key or "")
    if not owner:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return owner


@app.get("/api/automations/control")
async def automations_control(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    cfg = _permissions.get_config(u["id"])
    cn = _models.get_connect(u["id"]) or {}
    return {
        "permissions": cfg,
        "sequence": _sequences.get_sequence(u["id"]),
        "mode": _models.get_automation_mode(u["id"]),
        "paused": bool(cn.get("paused")) or bool(cfg.get("emergency_paused")),
        "channels": {"email": _notify.retailer_email_ready(u["id"]),
                     "whatsapp": _notify.retailer_whatsapp_ready(u["id"])},
        "activity": _models.list_activities(u["id"], 30),
    }


@app.put("/api/automations/config")
async def automations_config(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    data = await request.json()
    changed = []
    if isinstance(data.get("permissions"), dict):
        _permissions.set_config(u["id"], data["permissions"])
        changed.append("permissions")
    if isinstance(data.get("sequence"), list):
        _sequences.set_sequence(u["id"], data["sequence"])
        changed.append("sequence")
    if data.get("mode") in ("auto", "approval"):
        _models.set_automation_mode(u["id"], data["mode"])
        changed.append("mode")
    if not changed:
        raise HTTPException(status_code=400, detail="nothing recognizable to update")
    return {"ok": True, "updated": changed}


@app.post("/api/automations/pause")
async def automations_pause(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    data = await request.json()
    paused = bool(data.get("paused"))
    _permissions.set_emergency_pause(u["id"], paused)
    _models.ex("UPDATE connect SET paused=? WHERE owner_id=?",
               (1 if paused else 0, u["id"]))
    _models.log_audit(u["id"], "automation_" + ("paused" if paused else "resumed"), "")
    return {"ok": True, "paused": paused}


@app.get("/api/automations/audit")
async def automations_audit(request: Request, x_api_key: str | None = Header(default=None),
                            limit: int = 50):
    u = _control_owner(x_api_key)
    limit = max(1, min(int(limit), 200))
    audit = _models.q("SELECT created, action, detail FROM audit WHERE user_id=? "
                      "ORDER BY created DESC LIMIT ?", (u["id"], limit))
    return {"audit": audit, "activity": _models.list_activities(u["id"], limit)}


@app.get("/api/automations/approvals")
async def automations_approvals(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    rows = _models.q("SELECT id, channel, dest, tpl, subject, status, created "
                     "FROM outbox WHERE owner_id=? AND status='approval' "
                     "ORDER BY created DESC LIMIT 100", (u["id"],))
    return {"approvals": rows}


@app.post("/api/automations/approvals/{oid}")
async def automations_approval_decide(oid: str, request: Request,
                                      x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    data = await request.json()
    o = _models.get_outbox(oid)
    if not o or o["owner_id"] != u["id"]:
        raise HTTPException(status_code=404, detail="no such pending message")
    if o["status"] != "approval":
        raise HTTPException(status_code=409, detail="message is not awaiting approval")
    decision = (data.get("decision") or "").lower()
    if decision == "approve":
        out = _models.approve_outbox(u["id"], oid)
        if not out:
            raise HTTPException(status_code=409, detail="message is not awaiting approval")
        st = _notify.deliver(out)
        return {"ok": True, "delivery_status": st}
    if decision == "reject":
        _models.discard_outbox(u["id"], oid)
        return {"ok": True, "rejected": True}
    raise HTTPException(status_code=400, detail="decision must be approve or reject")


@app.post("/api/automations/invoices/{iid}/pause")
async def automations_invoice_pause(iid: str, request: Request,
                                    x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    data = await request.json()
    ok = _sequences.set_invoice_paused(u["id"], iid, bool(data.get("paused", True)))
    if not ok:
        raise HTTPException(status_code=404, detail="no such invoice")
    return {"ok": True, "paused": bool(data.get("paused", True))}


# ---- Phase 6.1: sandbox provider credential management + WhatsApp webhook ----
from . import providers as _providers61

@app.get("/api/provider/config")
async def provider_config_get(x_api_key: str | None = Header(default=None)):
    """Masked view of this business's provider configuration. Secrets are
    never returned: masked values only, sandbox state included."""
    u = _control_owner(x_api_key)
    out = {}
    for ch in ("email", "whatsapp"):
        m = _providers61.masked_provider_config(u["id"], ch)
        if m:
            out[ch] = m
    return {"ok": True, "providers": out}


@app.post("/api/provider/config")
async def provider_config_set(request: Request,
                              x_api_key: str | None = Header(default=None)):
    """Store sandbox provider credentials for this business, encrypted at
    rest. Live credentials are refused unless the platform-level live flag is
    explicitly set (separate approval). Secrets never appear in responses."""
    u = _control_owner(x_api_key)
    data = await request.json()
    channel = (data.get("channel") or "").lower()
    kind = (data.get("kind") or ("email_api" if channel == "email" else "whatsapp_cloud")).lower()
    cfg = data.get("config") or {}
    sandbox = data.get("sandbox", True)
    if channel not in ("email", "whatsapp"):
        raise HTTPException(status_code=400, detail="channel must be email or whatsapp")
    if not cfg:
        raise HTTPException(status_code=400, detail="config required")
    try:
        res = _providers61.configure_provider(u["id"], channel, kind, cfg, sandbox=bool(sandbox))
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True, **res}


@app.post("/api/provider/test")
async def provider_test(request: Request, x_api_key: str | None = Header(default=None)):
    """Send a canary through this business's configured sandbox provider and
    return the real ladder result: accepted / failed / unknown. Never claims
    delivery: only the provider webhook can move a message to delivered."""
    u = _control_owner(x_api_key)
    data = await request.json()
    channel = (data.get("channel") or "email").lower()
    to = (data.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="test address required")
    cfg = _providers61.get_provider_config(u["id"], channel)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"no provider configured for {channel}")
    if channel == "email":
        if cfg.get("_kind") == "email_api":
            res = _providers61.send_email_api(cfg["base_url"], cfg.get("token", ""),
                                              cfg.get("from_addr", "pilot@recursaathi.test"),
                                              to, "RecurSaathi sandbox test", "canary", 10.0)
        else:
            res = _providers61.send_email(cfg["smtp_host"], int(cfg.get("smtp_port", 587)),
                                          cfg.get("from_addr", "pilot@recursaathi.test"), to,
                                          "RecurSaathi sandbox test", "canary", 10.0,
                                          smtp_user=cfg.get("smtp_user", ""),
                                          smtp_password=cfg.get("smtp_password", ""))
    else:
        res = _providers61.send_whatsapp(cfg["base_url"], cfg.get("token", ""),
                                         cfg.get("wa_phone_number_id", ""), to, "canary", 10.0)
    import json as _pj
    _models.log_audit(u["id"], "provider_test",
                      _pj.dumps({"channel": channel, "result": res["status"],
                                  "provider_id": res.get("provider_id")}))
    return {"ok": True, "channel": channel, "result": res["status"],
            "provider_id": res.get("provider_id"), "detail": res["detail"],
            "note": "accepted is NOT delivery; only the provider webhook confirms delivery"}


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request,
                           x_hub_signature_256: str | None = Header(default=None)):
    """WhatsApp Cloud API status webhook. Signature verified with the app
    secret (X-Hub-Signature-256). Maps status events onto the delivery
    ladder and handles bounces. Never sets delivered from a send result."""
    body = await request.body()
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if secret and not _providers61.verify_hub_signature(body, x_hub_signature_256, secret):
        raise HTTPException(status_code=401, detail="bad signature")
    data = await request.json()
    entry = ((data.get("entry") or [{}])[0].get("changes") or [{}])[0].get("value", {})
    for st in (entry.get("statuses") or []):
        ref = st.get("id") or ""
        state = (st.get("status") or "").lower()
        if state not in ("delivered", "read", "failed", "bounced"):
            continue
        row = _models.one("SELECT id, owner_id, dest, channel FROM outbox WHERE provider_ref=?", (ref,))
        if not row:
            continue
        if state == "bounced":
            _providers61.handle_bounce(row["owner_id"], row["dest"], row["channel"],
                                       str((st.get("errors") or [{}])[0].get("title", "bounce")))
        else:
            _models.ex("UPDATE outbox SET delivery_status=?, provider_updated=? WHERE id=?",
                       (state, _models.now(), row["id"]))
            _models.log_activity(row["owner_id"], "delivery",
                                 f"WhatsApp provider reported {ref} as {state}")
    return {"ok": True}


@app.post("/webhooks/provider-status")
async def provider_status(request: Request, x_provider_token: str = Header(default="")):
    """Provider delivery confirmations. A provider ACCEPT is never a delivery:
    only this webhook moves a message to delivered/read/failed/bounced."""
    import os as _os
    expected = _os.environ.get("PROVIDER_STATUS_TOKEN", "")
    if expected and not hmac.compare_digest(x_provider_token, expected):
        raise HTTPException(status_code=401, detail="invalid provider token")
    data = await request.json()
    ref = (data.get("provider_ref") or "").strip()
    status = (data.get("status") or "").strip().lower()
    if status not in ("delivered", "read", "failed", "bounced"):
        raise HTTPException(status_code=400, detail="unknown delivery status")
    if not ref:
        raise HTTPException(status_code=400, detail="provider_ref required")
    row = _models.one("SELECT id, owner_id FROM outbox WHERE provider_ref=?", (ref,))
    if not row:
        raise HTTPException(status_code=404, detail="unknown message reference")
    if status == "bounced":
        rowfull = _models.one("SELECT id, owner_id, dest, channel FROM outbox WHERE provider_ref=?", (ref,))
        _providers61.handle_bounce(rowfull["owner_id"], rowfull["dest"], rowfull["channel"],
                                   f"provider reported {ref} bounced")
    else:
        _models.ex("UPDATE outbox SET delivery_status=?, provider_updated=? WHERE id=?",
                   (status, _models.now(), row["id"]))
    _models.log_activity(row["owner_id"], "delivery",
                         f"provider reported message {ref} as {status}")
    return {"ok": True, "outbox_id": row["id"], "delivery_status": status}


# ---- hybrid automation architecture, Phase 3: customer reactivation agent ----
from . import reactivation as _reactivation


@app.get("/api/reactivation/dormant")
async def reactivation_dormant(x_api_key: str | None = Header(default=None),
                               days_inactive: int = 90):
    """Level A: internal analysis of the business's own verified history."""
    u = _control_owner(x_api_key)
    members = _reactivation.dormant_customers(u["id"], max(7, min(int(days_inactive), 365)))
    by_seg = {}
    for m in members:
        by_seg.setdefault(m["segment"], []).append(m["name"])
    return {"dormant": members, "segments": {k: len(v) for k, v in by_seg.items()}}


@app.post("/api/reactivation/campaigns")
async def reactivation_create(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    d = await request.json()
    try:
        c = _reactivation.create_campaign(
            u, str(d.get("name") or "reactivation campaign")[:80],
            d.get("segment", "all"), d.get("channels") or [],
            str(d.get("email_subject") or "")[:200], str(d.get("email_body") or "")[:4000],
            str(d.get("wa_body") or "")[:2000],
            max_per_run=int(d.get("max_per_run", 50)),
            days_inactive=int(d.get("days_inactive", 90)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "campaign": c}


@app.get("/api/reactivation/campaigns")
async def reactivation_list(x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    out = []
    for c in _reactivation.list_campaigns(u["id"]):
        s = _reactivation.campaign_summary(u["id"], c["id"])
        out.append({"campaign": c, "summary": s})
    return {"campaigns": out}


@app.post("/api/reactivation/campaigns/{cid}/approve")
async def reactivation_approve(cid: str, request: Request,
                               x_api_key: str | None = Header(default=None)):
    """Level C gate: the owner's approval launches the campaign."""
    u = _control_owner(x_api_key)
    c = _reactivation.approve_campaign(u, cid, approved_by=u.get("email") or "owner")
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": c["status"] == "active", "campaign": c}


@app.post("/api/reactivation/campaigns/{cid}/cancel")
async def reactivation_cancel(cid: str, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    c = _reactivation.cancel_campaign(u["id"], cid)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": True, "campaign": c}


@app.post("/api/reactivation/campaigns/{cid}/run")
async def reactivation_run(cid: str, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    c = _reactivation.get_campaign(cid)
    if not c or c["owner_id"] != u["id"]:
        raise HTTPException(status_code=404, detail="campaign not found")
    counts = _reactivation.run_campaign(u, c)
    return {"ok": True, "counts": counts}


@app.post("/api/reactivation/response")
async def reactivation_response(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    d = await request.json()
    try:
        r = _reactivation.record_response(u["id"], d.get("campaign_id", ""),
                                          d.get("customer_id", ""), d.get("kind", ""),
                                          str(d.get("note") or "")[:500])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not r:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": True, "response": r}


# ---- hybrid automation architecture, Phase 4: revenue intelligence (read-only) ----
from . import intelligence as _intelligence


@app.get("/api/intelligence/overview")
async def intelligence_overview(x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    return _intelligence.intelligence_snapshot(u["id"])


@app.get("/api/intelligence/report")
async def intelligence_report(x_api_key: str | None = Header(default=None), kind: str = "daily"):
    u = _control_owner(x_api_key)
    if kind == "daily":
        return {"kind": "daily", "report": _intelligence.daily_report(u["id"])}
    if kind == "weekly":
        return {"kind": "weekly", "report": _intelligence.weekly_report(u["id"])}
    raise HTTPException(status_code=400, detail="kind must be daily or weekly")


@app.post("/api/intelligence/assistant")
async def intelligence_assistant(request: Request, x_api_key: str | None = Header(default=None)):
    """Read-only business assistant: answers only from this tenant's own records."""
    u = _control_owner(x_api_key)
    d = await request.json()
    return {"answer": _intelligence.assistant_answer(u["id"], str(d.get("question") or "")[:500])}


# ---- hybrid automation architecture, Phase 5: controlled pilot readiness ----
from . import pilot as _pilot
from . import providers as _providers


@app.get("/api/pilot/activation")
async def pilot_activation_get(x_api_key: str | None = Header(default=None)):
    """What is on for this business: automation state, authorized channels,
    quiet hours and caps. This is the 'display permissions and active channels'
    view the pilot needs."""
    u = _control_owner(x_api_key)
    return _pilot.get_activation(u["id"])


@app.post("/api/pilot/activation")
async def pilot_activation(request: Request, x_api_key: str | None = Header(default=None)):
    """Owner-controlled activation. automations stay OFF for every new
    business until this is called with confirm='ACTIVATE'."""
    u = _control_owner(x_api_key)
    d = await request.json()
    try:
        if d.get("enabled"):
            return {"ok": True, "activation": _pilot.activate(u["id"], str(d.get("confirm") or ""), str(d.get("code") or ""))}
        return {"ok": True, "activation": _pilot.deactivate(u["id"])}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/pilot/consents")
async def pilot_consent_add(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    d = await request.json()
    try:
        row = _pilot.record_consent(u["id"], str(d.get("subject") or ""),
                                    str(d.get("kind") or ""),
                                    str(d.get("channel") or ""),
                                    str(d.get("granted_by") or ""),
                                    str(d.get("evidence") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "consent": row}


@app.get("/api/pilot/consents")
async def pilot_consents(x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    return {"consents": _pilot.list_consents(u["id"])}


@app.post("/api/pilot/import")
async def pilot_import(request: Request, x_api_key: str | None = Header(default=None)):
    """Validated, deduplicating import. Payments arrive as status 'imported'
    and NEVER count as recovered revenue until reconciliation confirms them."""
    u = _control_owner(x_api_key)
    d = await request.json()
    try:
        return {"ok": True, "result": _pilot.import_data(u["id"], str(d.get("kind") or ""),
                                                         d.get("rows") or [])}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/pilot/reconcile")
async def pilot_reconcile(request: Request, x_api_key: str | None = Header(default=None)):
    u = _control_owner(x_api_key)
    d = await request.json()
    r = _pilot.reconcile_payment(u["id"], str(d.get("payment_id") or ""),
                                 bool(d.get("verified")), str(d.get("reference") or ""))
    if not r:
        raise HTTPException(status_code=404, detail="payment not found")
    return r


@app.get("/pilot/metrics")
async def pilot_metrics(x_admin_token: str = Header(default="")):
    """Operator-level pilot monitoring across every business. Admin token
    only, never a business API key: tenant isolation is not weakened."""
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    _models.log_audit("platform", "pilot_metrics_access", "restricted pilot monitoring read")
    return _pilot.pilot_metrics()


@app.get("/pilot/backup-check")
async def pilot_backup(x_admin_token: str = Header(default="")):
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return _pilot.backup_check()


# ---- Phase 5.1: pilot safety hardening endpoints ----------------------------

@app.post("/api/pilot/activation/challenge")
async def pilot_activation_challenge(request: Request, x_api_key: str | None = Header(default=None)):
    """Owner reauthentication: verify the owner's password, issue a one-time
    activation code (10 minutes, single use). The activation POST then needs
    both the code and confirm=ACTIVATE."""
    u = _control_owner(x_api_key)
    d = await request.json()
    code = _pilot.request_activation_code(u["id"], str(d.get("password") or ""))
    if not code:
        raise HTTPException(status_code=401, detail="owner password verification failed")
    return {"ok": True, **code}


@app.post("/pilot/trust-source")
async def pilot_trust_source(request: Request, x_admin_token: str = Header(default="")):
    """Platform admin only. Seeds the trusted reference source (what a live
    gateway API export would show). Business API keys can never write here."""
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")
    d = await request.json()
    try:
        _pilot.seed_trust_reference(str(d.get("provider") or "razorpay"),
                                    str(d.get("reference") or ""),
                                    int(d.get("amount") or 0),
                                    str(d.get("status") or "captured"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/pilot/backup")
async def pilot_backup_create(request: Request, x_admin_token: str = Header(default="")):
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")
    d = await request.json() if request.headers.get("content-length", "0") not in ("0", "") else {}
    return {"ok": True, "backup": _pilot.create_backup(str((d or {}).get("label") or "pre-rollback"))}


@app.get("/pilot/backup")
async def pilot_backup_verify(x_admin_token: str = Header(default=""), backup_id: str = ""):
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")
    if not backup_id:
        return {"ok": True, "backups": _models.q("SELECT id,label,sha256,created FROM pilot_backups ORDER BY created DESC LIMIT 20")}
    v = _pilot.verify_backup(backup_id)
    if v is None:
        raise HTTPException(status_code=404, detail="backup not found")
    return {"ok": True, "verification": v}


@app.post("/pilot/prepare")
async def pilot_prepare(request: Request, x_admin_token: str = Header(default="")):
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")
    d = await request.json() if request.headers.get("content-length", "0") not in ("0", "") else {}
    return {"ok": True, **_pilot.prepare_pilots(int((d or {}).get("count") or 3))}


@app.get("/api/pilot/compliance")
async def pilot_compliance(x_api_key: str | None = Header(default=None)):
    """Consent, owner authorization and channel authorization records for
    this business, plus its activation audit trail. Tenant-scoped."""
    u = _control_owner(x_api_key)
    return _pilot.compliance_export(u["id"])


@app.post("/pilot/deliver")
async def pilot_deliver(request: Request, x_admin_token: str = Header(default="")):
    """Platform admin only: run the controlled delivery pipeline against the
    configured providers (SMTP host/port, WhatsApp Cloud API base). Used in
    the controlled test environment; never contacts real customers."""
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")
    d = await request.json()
    out = _providers.deliver_outbox(str(d.get("owner_id") or ""), d.get("config") or {})
    return {"ok": True, "delivery": out}
