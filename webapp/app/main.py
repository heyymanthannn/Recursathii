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
    if plan not in PLANS or not email:
        raise HTTPException(status_code=400, detail="plan and email required")
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
    plan_name = {"starter": "Starter", "growth": "Growth", "scale": "Scale"}.get(m.plan, m.plan.title())
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
<input name="code" placeholder="Coupon code" maxlength="20" {'value=' + chr(34) + (row.get('coupon_code') or '') + chr(34) if row and row.get('coupon_code') else ''} {'disabled' if pct else ''}>
<button class="btn ghost" style="width:auto" {'disabled' if pct else ''}>Apply</button></form>
<form method="post" action="/checkout/{merchant_id}/paid-claim" style="margin-top:6px">
<button class="btn">I've paid</button></form>
<p class="sub" style="margin-top:10px;text-align:center;font-size:13px">Your account goes live the moment payment is confirmed. You'll hear from us on {m.email}.</p>
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
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")
    data = await request.json()
    return manual_upi.mark_paid(data.get("merchant_id", ""))

@app.post("/admin/plat-confirm")
async def admin_plat_confirm(request: Request, x_admin_token: str = Header(default="")):
    """Confirm a pending subscription payment -> activates that business's plan."""
    if x_admin_token != config.ADMIN_TOKEN:
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
    if tok != config.ADMIN_TOKEN:
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
        utr_html = f'<div class="utr">UTR claimed: <b>{utr}</b></div>' if utr else '<div class="utr dim">No UTR claimed yet</div>'
        cards.append(f"""<div class="card"><div class="row1"><b>{(m.get('name') or m.get('email') or 'Merchant')}</b><span class="plan">{m.get('plan','')}</span></div>
<div class="meta">{m.get('email','')} · {m.get('phone') or 'no phone'}</div>
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
    if (form.get("token") or "") != config.ADMIN_TOKEN:
        return RedirectResponse("/admin?e=1", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("admin_token", config.ADMIN_TOKEN, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp

@app.post("/admin/confirm")
async def admin_confirm(request: Request):
    from fastapi.responses import RedirectResponse
    if request.cookies.get("admin_token", "") != config.ADMIN_TOKEN:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    manual_upi.mark_paid(form.get("merchant_id", ""))
    return RedirectResponse("/admin", status_code=303)

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


@app.get("/terms", include_in_schema=False)
async def terms_page():
    return FileResponse("landing/terms.html")

@app.get("/upi-autopay-recovery", include_in_schema=False)
async def upi_autopay_recovery_page():
    return FileResponse("landing/upi-autopay-recovery.html")

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
