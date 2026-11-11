import time
import fastapi
import hmac, hashlib, secrets, pathlib, csv, io, json

"""Owner web app: dashboard, customers, invoices, radar, automations, billing."""
import csv, io, os, secrets, time
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from . import models
from . import auth, vault, payments, notify, automations as auto, config, retention as ret
from .ratelimit import allow as rl_allow, wait_s as rl_wait

def _role_gate(request: Request):
    u = auth.current_user(request)
    if not u:
        return
    if u["role"] in ("staff", "viewer", "manager") and request.url.path.startswith(("/billing", "/settings", "/team", "/admin")):
        raise HTTPException(status_code=403, detail="Owner access only")
    if request.method == "POST" and u["role"] == "viewer" and request.url.path not in ("/login", "/logout"):
        raise HTTPException(status_code=403, detail="Viewers have read-only access")

router = APIRouter(dependencies=[Depends(_role_gate)])
models.init()
TPL = "app/templates"

def render(name, request, user=None, **ctx):
    from fastapi.templating import Jinja2Templates
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(TPL), autoescape=True)
    env.filters["inr"] = lambda p: f"\u20b9{p/100:,.0f}"
    env.filters["datetime"] = lambda ts: (time.strftime("%d %b %Y, %H:%M", time.localtime(ts)) if ts else "-")
    env.globals["inr"] = env.filters["inr"]
    env.globals["datetime"] = env.filters["datetime"]
    # days left on the 14-day trial, for the daily subscription popup
    # (a route that passes its own trial_left keeps its value)
    trial_left = ctx.pop("trial_left", None)
    if trial_left is None and user and user.get("plan_status") == "trialing" and user.get("trial_ends"):
        trial_left = max(0, int((user["trial_ends"] - time.time()) / 86400) + 1)
    code = ctx.pop("code", None)
    t = env.get_template(name)
    body = t.render(request=request, user=user, active_tab=ctx.pop("tab", ""),
                    trial_left=trial_left, code=code, **ctx)
    return HTMLResponse(body, status_code=code or 200)

def inr(p): return f"\u20b9{p/100:,.0f}"

def feature_gate(user, key):
    """Enterprise feature entitlement check. Returns a redirect when the plan
    does not include the feature, else None."""
    if not user or not config.has_feature(user.get("plan") or "starter", key):
        return RedirectResponse("/billing?blocked=" + key, status_code=303)
    return None


def _wa_link(o):
    """One-tap WhatsApp deep link for a queued message, so the owner can send
    it from their own WhatsApp until a WhatsApp API token is connected."""
    if o.get("channel") != "whatsapp" or o.get("status") in ("sent", "skipped"):
        return ""
    num = (o.get("dest") or "").replace(" ", "")
    if not num:
        return ""
    if not num.startswith("+"):
        num = "+91" + num.lstrip("0")
    from urllib.parse import quote as _q
    return f"https://wa.me/{num.lstrip('+')}?text={_q(o.get('body') or '')}"

def base_url(request):
    return str(request.base_url).rstrip("/")

def guard(request):
    """Owner row for the acting user: team members (staff/viewer) act inside
    their owner's workspace, tagged with their own role."""
    u = auth.current_user(request)
    if not u or u["role"] not in ("owner", "staff", "viewer"):
        return None
    owner = models.get_user(u["parent_id"]) if u["parent_id"] else u
    if not owner:
        return None
    row = dict(owner)
    row["role"] = u["role"]
    if row["plan_status"] == "trialing" and not row.get("trial_ends"):
        started = time.time()
        models.update_user(owner["id"], trial_started_at=started,
                           trial_ends=started + 14 * 86400)
        row["trial_started_at"] = started
        row["trial_ends"] = started + 14 * 86400
    if row["plan_status"] == "trialing" and row.get("trial_ends") and row["trial_ends"] < time.time():
        models.update_user(owner["id"], plan_status="expired")
        row["plan_status"] = "expired"
    row["read_only"] = row["plan_status"] == "expired"
    row["features"] = config.PLAN_FEATURES.get(config.plan_key(row.get("plan") or "starter"), [])
    return row

# ---------- auth pages ----------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render("login.html", request, error="")

@router.post("/login")

async def login(request: Request, email: str = Form(""), password: str = Form("")):
    _ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    _rl_key = "login:" + _ip + ":" + (email or "").lower()
    if not rl_allow(_rl_key, 10, 600):
        return render("login.html", request, error=f"Too many attempts. Try again in {rl_wait(_rl_key, 10, 600)}s.")
    u = models.get_user_by_email(email)
    if not u or not auth.check_pw(password, u["pw_hash"]):
        return render("login.html", request, error="Wrong email or password.")
    if u.get("totp_enabled") and u.get("totp_secret"):
        tok = _sign_challenge(u["email"])
        return render("login2.html", request, error="", challenge=tok, email=u["email"])
    resp = RedirectResponse("/dashboard" if u.get("onboarded") else "/onboarding", status_code=303)
    auth.start_session(resp, u["id"])
    models.log_audit(u["id"], "login", u["email"])
    return resp

def _sign_key():
    import os as _os
    k = _os.environ.get("WAJO_SIGN_KEY", "")
    if k:
        return k.encode()
    p = pathlib.Path("data/.sign_key")
    p.parent.mkdir(exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_hex(32))
    return p.read_bytes()

def _sign_challenge(email, ttl=300):
    exp = int(time.time()) + ttl
    msg = f"{email}|{exp}".encode()
    sig = hmac.new(_sign_key(), msg, hashlib.sha256).hexdigest()
    return f"{exp}:{sig}"

def _check_challenge(email, tok):
    try:
        exp, sig = (tok or "").split(":", 1)
        if int(exp) < time.time():
            return False
        msg = f"{email}|{exp}".encode()
        return hmac.compare_digest(hmac.new(_sign_key(), msg, hashlib.sha256).hexdigest(), sig)
    except Exception:
        return False

@router.post("/login/2fa")
async def login_2fa(request: Request, email: str = Form(""), challenge: str = Form(""),
                    code: str = Form("")):
    if not _check_challenge(email, challenge):
        return render("login2.html", request, error="This step expired. Start again from the login page.",
                      challenge="", email=email)
    u = models.get_user_by_email(email)
    if not u or not u.get("totp_enabled"):
        return RedirectResponse("/login", status_code=303)
    if not models.verify_totp(u["totp_secret"], code):
        return render("login2.html", request, error="That code is not right. Codes refresh every 30 seconds.",
                      challenge=challenge, email=email)
    resp = RedirectResponse("/dashboard" if u.get("onboarded") else "/onboarding", status_code=303)
    auth.start_session(resp, u["id"])
    models.log_audit(u["id"], "login", u["email"] + " (2fa)")
    return resp

@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return render("signup.html", request, error="")

@router.post("/signup")
async def signup(request: Request, name: str = Form(""), business: str = Form(""),
                 email: str = Form(""), phone: str = Form(""),
                 upi_vpa: str = Form(""), password: str = Form("")):
    _ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    _rl_key = "signup:" + _ip
    if not rl_allow(_rl_key, 5, 3600):
        return render("signup.html", request, error=f"Too many signups from this connection. Try again in {rl_wait(_rl_key, 5, 3600)}s.")
    if not all([name, business, email, password]):
        return render("signup.html", request, error="Name, business, email and password are required.")
    if not phone.strip():
        return render("signup.html", request, error="Phone number is required to start your trial.")
    if len(password) < 8:
        return render("signup.html", request, error="Password must be at least 8 characters.")
    if models.get_user_by_email(email):
        return render("signup.html", request, error="That email is already registered. Try logging in.")
    u = models.create_user(email, auth.hash_pw(password), name, business, phone,
                           upi_vpa=upi_vpa, payee_name=name)
    _t = time.time()
    models.update_user(u["id"], trial_started_at=_t,
                       trial_ends=_t + 14 * 86400)  # trial starts at signup, regardless of plan
    # Phase 7: new businesses start with every customer-contacting automation OFF
    for k in models.AUTO_KEYS:
        models.set_automation(u["id"], k, False)
        d = auto.DEFAULTS[k]
        if "email_subject" in d:  # due_call is calls-only: no email template
            models.set_template(u["id"], k, d["email_subject"], d["email_body"])
    models.log_audit(u["id"], "signup", email)
    # email verification (soft): login is never blocked, a banner reminds until verified
    import uuid as _uuid2
    _vtok = _uuid2.uuid4().hex
    models.set_verify_token(u["id"], _vtok)
    try:
        from . import notify as _notify
        from .config import BASE_URL as _BU
        if _notify.smtp_ready():
            _notify.send_email(email, "Verify your email - RecurSaathi",
                f"Welcome to RecurSaathi, {name or business}!\n\nConfirm your email address (takes one click):\n"
                f"{_BU}/verify/{_vtok}\n\n"
                "Until then you can use the dashboard - we just remind you on sign-in.\n\n- Team RecurSaathi")
            models.log_audit(u["id"], "verify.sent", email)
    except Exception as _e:
        models.log_audit(u["id"], "verify.send_failed", str(_e)[:120])
    # mark rl key comment to keep replace idempotent
    # rl_signup marker
    import uuid as _uuid
    from . import billing as _billing
    mid = "m_" + _uuid.uuid4().hex[:10]
    _m = _billing.onboard(mid, email, u["plan"] or "starter")
    _m.name = name
    _m.phone = phone.strip()
    _billing._persist(_m)
    resp = RedirectResponse("/login?created=1", status_code=303)  # signup lands on the login page; dashboard comes after sign-in
    return resp

@router.get("/verify/{token}")
async def verify_email(request: Request, token: str):
    u = models.q("SELECT id FROM users WHERE verify_token=?", (token,))
    if not u:
        return render("login.html", request, error="That verification link is invalid or already used.")
    models.mark_verified(u[0]["id"])
    models.log_audit(u[0]["id"], "verify.ok", "")
    return render("login.html", request, ok="Email verified. You are all set.")

@router.post("/resend-verification")
async def resend_verification(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("verified_at"):
        return RedirectResponse("/dashboard", status_code=303)
    import uuid as _uuid3
    from . import notify as _notify
    from .config import BASE_URL as _BU
    _tok = _uuid3.uuid4().hex
    models.set_verify_token(user["id"], _tok)
    sent = False
    if _notify.smtp_ready():
        try:
            _notify.send_email(user["email"], "Verify your email - RecurSaathi",
                f"Confirm your email address (one click):\n{_BU}/verify/{_tok}\n\n- Team RecurSaathi")
            sent = True
        except Exception:
            sent = False
    return RedirectResponse(f"/dashboard?verify={'sent' if sent else 'failed'}", status_code=303)

@router.get("/logout")

async def logout():
    resp = RedirectResponse("/login", status_code=303)
    auth.end_session(resp)
    return resp

# ---------- dashboard ----------
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") == "owner" and not user.get("onboarded"):
        return RedirectResponse("/onboarding", status_code=303)
    _vmsg = None
    if not user.get("verified_at"):
        _vs = request.query_params.get("verify", "")
        _vmsg = ("A fresh verification link is on its way to your inbox." if _vs == "sent"
                 else ("Could not send the verification email just now - try again shortly." if _vs == "failed"
                       else "One quick step: verify your email so we can reach you about your account."))
    m = models.owner_metrics(user["id"])
    period = request.query_params.get("period", "monthly")
    if period not in ("monthly", "weekly"):
        period = "monthly"
    chart = models.recovery_series(user["id"], period)
    acts = models.list_activities(user["id"])
    pays = models.recent_payments(user["id"])
    fails = models.q("SELECT e.*, cu.name AS customer_name FROM pay_events e LEFT JOIN customers cu ON cu.id=e.customer_id WHERE e.owner_id=? AND e.kind='failed' ORDER BY e.created DESC LIMIT 8", (user["id"],))
    ov = models.q("SELECT i.*, cu.name AS customer_name FROM invoices i LEFT JOIN customers cu ON cu.id=i.customer_id WHERE i.owner_id=? AND i.status='overdue' ORDER BY i.due_date ASC LIMIT 8", (user["id"],))
    trial_left = ""
    if user["plan_status"] == "trialing" and user["trial_ends"]:
        days = int((user["trial_ends"] - time.time()) // 86400)
        trial_left = f"{max(days,0)} days left in your free trial" if days >= 0 else "Your trial has ended"
    leak = models.predictive_leak(user["id"]) if "predictive_leaks" in user.get("features", []) else None
    insights = models.daily_insights(user["id"]) if "advanced_assistant" in user.get("features", []) else []
    bstats = models.branch_stats(user["id"]) if "multi_branch" in user.get("features", []) else []
    return render("dashboard.html", request, user=user, tab="dashboard", m=m, chart=chart,
                  acts=acts, pays=pays, fails=fails, ov=ov, inr=inr, trial_left=trial_left, period=period,
                  leak=leak, insights=insights, bstats=bstats,
                  channel_note=notify.retailer_email_ready(user["id"]) or notify.retailer_whatsapp_ready(user["id"]),
                  alerts=models.owner_alerts(user["id"]), obc=models.outbox_counts(user["id"]),
                  claims=models.list_claims(user["id"]), mode=models.get_automation_mode(user["id"]))

# ---------- customers ----------
@router.get("/customers", response_class=HTMLResponse)
async def customers_page(request: Request, q: str = ""):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    seg = request.query_params.get("seg", "all")
    if seg not in models.SEGMENTS:
        seg = "all"
    rows = models.segment_rows(user["id"], seg)
    if q:
        s = q.lower()
        rows = [r for r in rows if s in (r["name"] or "").lower() or s in (r["email"] or "") or s in (r["phone"] or "")]
    return render("customers.html", request, user=user, tab="customers", rows=rows, q=q, seg=seg,
                  segs=models.SEGMENTS, inr=inr)

@router.post("/customers")
async def customers_add(request: Request, name: str = Form(""), email: str = Form(""),
                        phone: str = Form(""), notes: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("read_only") or models.plan_limit_msg(user["id"], "customers"):
        return RedirectResponse("/billing?blocked=customers", status_code=303)
    if name:
        c = models.create_customer(user["id"], name, email, phone, notes)
        models.log_activity(user["id"], "customer", f"Added customer {name}")
        models.log_audit(user["id"], "customer.create", name)
    return RedirectResponse("/customers", status_code=303)

@router.get("/customers/export")
async def customers_export(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "phone", "outstanding_paise", "paid_total_paise", "invoices", "whatsapp_opt_out", "created_at"])
    for r in models.customer_balances(user["id"]):
        w.writerow([r["name"], r["email"], r["phone"], r["outstanding"], r["paid_total"],
                    r["invoices"], r["wa_opt_out"], time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"]))])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=customers.csv"})


@router.get("/customers/{cid}", response_class=HTMLResponse)
async def customer_detail(request: Request, cid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    cu = models.one("SELECT * FROM customers WHERE id=? AND owner_id=?", (cid, user["id"]))
    if not cu:
        return render("error.html", request, user=user, code=404, message="Customer not found")
    invs = models.q("SELECT * FROM invoices WHERE customer_id=? ORDER BY created DESC", (cid,))
    pays = models.q("SELECT * FROM payments WHERE customer_id=? ORDER BY created DESC", (cid,))
    events = models.q("SELECT * FROM pay_events WHERE customer_id=? ORDER BY created DESC", (cid,))
    outstanding = sum(i["amount"] for i in invs if i["status"] in ("pending", "overdue"))
    return render("customer_detail.html", request, user=user, cu=cu, invs=invs, pays=pays,
                  events=events, outstanding=outstanding, inr=inr, tab="customers")

@router.post("/customers/{cid}/edit")
async def customer_edit(request: Request, cid: str, name: str = Form(""), email: str = Form(""),
                        phone: str = Form(""), notes: str = Form(""), wa_opt_out: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("read_only") or models.plan_limit_msg(user["id"], "customers"):
        return RedirectResponse("/billing?blocked=customers", status_code=303)
    cu = models.one("SELECT id FROM customers WHERE id=? AND owner_id=?", (cid, user["id"]))
    if cu:
        models.update_customer(cid, name=name, email=email, phone=phone, notes=notes,
                               wa_opt_out=1 if wa_opt_out == "on" else 0)
        models.log_audit(user["id"], "customer.edit", name)
    return RedirectResponse(f"/customers/{cid}", status_code=303)

# ---------- invoices ----------
@router.post("/customers/{cid}/delete")
async def delete_customer(request: Request, cid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    cu = models.get_customer(cid)
    if cu and cu["owner_id"] == user["id"]:
        n = models.one("SELECT COUNT(*) AS n FROM invoices WHERE customer_id=?", (cid,))["n"]
        if n:
            return RedirectResponse("/customers?err=hasinv", status_code=303)
        models.ex("DELETE FROM customers WHERE id=?", (cid,))
        models.log_audit(user["id"], "customer.delete", cid)
        models.log_activity(user["id"], "customer", f"Deleted customer {cu['name']}")
    return RedirectResponse("/customers", status_code=303)

@router.get("/invoices", response_class=HTMLResponse)
async def invoices_page(request: Request, status: str = ""):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    rows = models.list_invoices(user["id"], status)
    custs = {c["id"]: c for c in models.list_customers(user["id"])}
    for r in rows:
        cu = custs.get(r["customer_id"], {})
        r["customer_name"] = cu.get("name", "")
        r["pay_link"] = f"{base_url(request)}/pay/{r['token']}"
    return render("invoices.html", request, user=user, tab="invoices", rows=rows,
                  custs=list(custs.values()), status=status, inr=inr)

@router.post("/invoices")
async def invoices_add(request: Request, customer_id: str = Form(""), number: str = Form(""),
                       amount: str = Form(""), description: str = Form(""), due_date: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("read_only") or models.plan_limit_msg(user["id"], "invoices"):
        return RedirectResponse("/billing?blocked=invoices", status_code=303)
    try:
        amt = int(round(float(amount) * 100))
        assert amt > 0
    except Exception:
        return RedirectResponse("/invoices?status=invalid_amount", status_code=303)
    if customer_id:
        inv = models.create_invoice(user["id"], customer_id, number, amt, description, due_date)
        models.log_activity(user["id"], "invoice", f"Created invoice {inv['number']} for {inr(amt)}")
        models.log_audit(user["id"], "invoice.create", inv["number"])
    return RedirectResponse("/invoices", status_code=303)

@router.post("/invoices/{iid}/remind")
async def invoice_remind(iid: str, request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    inv = models.one("SELECT * FROM invoices WHERE id=? AND owner_id=?", (iid, user["id"]))
    if not inv or inv["status"] == "paid":
        return RedirectResponse("/radar", status_code=303)
    cu = models.get_customer(inv["customer_id"])
    if not cu:
        return RedirectResponse("/radar", status_code=303)
    today = time.strftime("%Y-%m-%d")
    stage = auto._stage(inv, today) or "reminder_due"
    tpl = models.get_template(user["id"], stage)
    ctx = auto._ctx(user, inv, cu, base_url(request))
    sub = auto._render(tpl["subject"] if tpl else auto.DEFAULTS[stage]["email_subject"], **ctx)
    ebody = auto._render(tpl["body"] if tpl else auto.DEFAULTS[stage]["email_body"], **ctx)
    wbody = auto._render(auto.DEFAULTS[stage]["wa_body"], **ctx)
    dedupe = f"manual_remind:{iid}:{stage}"
    if not models.one("SELECT id FROM outbox WHERE dedupe=?", (dedupe,)):
        if cu.get("email"):
            o = models.queue(user["id"], "email", cu["email"], stage, sub, ebody, dedupe=dedupe + ":email")
            if o:
                notify.deliver(o)
        if cu.get("phone") and not cu.get("wa_opt_out"):
            o = models.queue(user["id"], "whatsapp", cu["phone"], stage, sub, wbody, dedupe=dedupe + ":wa")
            if o:
                notify.deliver(o)
        models.log_activity(user["id"], "manual",
                            f"Reminder queued for {cu['name']} on {inv['number']} ({stage})")
        models.log_audit(user["id"], "invoice.remind", f"{inv['number']}:{stage}")
    return RedirectResponse("/radar", status_code=303)

@router.post("/invoices/{iid}/mark-paid")
async def invoice_mark_paid(request: Request, iid: str, reference: str = Form(""), note: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    inv = models.one("SELECT * FROM invoices WHERE id=? AND owner_id=?", (iid, user["id"]))
    if inv and inv["status"] != "paid":
        cu = models.get_customer(inv["customer_id"])
        creds = payments.owner_creds(user)
        verified = False
        ref = (reference or "").strip()
        # Verify through the connected payment provider before stopping reminders.
        if creds and ref.startswith("pay_"):
            r = payments.api(creds, "GET", f"/payments/{ref}")
            if isinstance(r, dict) and not r.get("error") and (r.get("status") == "captured"):
                verified = int(r.get("amount") or 0) >= inv["amount"]
        if verified:
            pay = models.mark_invoice_paid(inv, "razorpay", reference=ref, note=note)
            models.log_activity(user["id"], "payment",
                                f"Invoice {inv['number']} paid ({inr(inv['amount'])}) - verified with Razorpay payment {ref}")
            models.log_audit(user["id"], "invoice.paid", f"{inv['number']} rzp-verified")
            if cu:
                auto.send_confirmation(user, inv, cu, base_url(request))
        else:
            # No provider verification available: record the claim, keep the
            # invoice open and keep reminding until it is verified.
            pay = models.claim_invoice_payment(inv, "upi", reference=ref, note=note)
            models.log_activity(user["id"], "payment",
                                f"Payment reported for invoice {inv['number']} ({inr(inv['amount'])}) - awaiting verification; reminders continue")
            models.log_audit(user["id"], "invoice.claim", inv["number"])
    return RedirectResponse("/payments", status_code=303)


@router.get("/invoices/{iid}/pdf")
async def invoice_pdf(request: Request, iid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    inv = models.get_invoice(iid)
    if not inv or inv["owner_id"] != user["id"]:
        return RedirectResponse("/invoices", status_code=303)
    from . import pdfgen
    cu = models.get_customer(inv["customer_id"])
    pay_url = f"{base_url(request)}/invoice/{inv['token']}"
    data = pdfgen.invoice_pdf_bytes(inv, cu, user, pay_url=pay_url)
    models.log_audit(user["id"], "invoice.pdf", iid)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{inv["number"] or "invoice"}.pdf"'})

# ---------- radar ----------
@router.get("/radar", response_class=HTMLResponse)
async def radar_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    r = models.radar(user["id"])
    return render("radar.html", request, user=user, tab="radar", r=r, inr=inr,
                  base=base_url(request))

# ---------- automations ----------
@router.get("/automations", response_class=HTMLResponse)
async def automations_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    keys = models.get_automations(user["id"])
    tpls = {k: models.get_template(user["id"], k) for k in models.AUTO_KEYS}
    out = models.list_outbox(user["id"])
    out = [{**o, "wa_link": _wa_link(o)} for o in out]
    return render("automations.html", request, user=user, tab="automations", keys=keys,
                  tpls=tpls, out=out, defaults=auto.DEFAULTS, inr=inr,
                  email_ready=notify.retailer_email_ready(user["id"]), wa_ready=notify.retailer_whatsapp_ready(user["id"]))

@router.post("/automations/toggle")
async def automations_toggle(request: Request, key: str = Form(""), enabled: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if key in models.AUTO_KEYS:
        models.set_automation(user["id"], key, enabled == "1")
        models.log_audit(user["id"], "automation.toggle", f"{key}={'on' if enabled=='1' else 'off'}")
    return RedirectResponse("/automations", status_code=303)

@router.post("/automations/template")
async def automations_template(request: Request, key: str = Form(""), subject: str = Form(""),
                               body: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if key in models.AUTO_KEYS and subject and body:
        models.set_template(user["id"], key, subject, body)
        models.log_audit(user["id"], "automation.template", key)
    return RedirectResponse("/automations", status_code=303)

@router.post("/automations/run")
async def automations_run(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    counts = auto.run_for_owner(user, base_url(request))
    models.log_audit(user["id"], "automation.run", str(counts))
    return RedirectResponse("/automations", status_code=303)

# ---------- billing / subscription ----------
# ---------- team ----------
@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    user = guard(request)
    if not user or user["role"] != "owner":
        return RedirectResponse("/login", status_code=303)
    return render("team.html", request, user=user, tab="team",
                  team=models.list_team(user["id"]), new_pw="", new_err="")

@router.post("/team/add")
async def team_add(request: Request, name: str = Form(""), email: str = Form(""),
                   role: str = Form("staff")):
    user = guard(request)
    if not user or user["role"] != "owner":
        return RedirectResponse("/login", status_code=303)
    if user.get("read_only") or models.plan_limit_msg(user["id"], "team"):
        return RedirectResponse("/billing?blocked=team", status_code=303)
    team = models.list_team(user["id"])
    if role == "manager" and not config.has_feature(user["plan"], "advanced_permissions"):
        role = "staff"
    if not name or not email or "@" not in email or role not in ("staff", "viewer", "manager"):
        return render("team.html", request, user=user, tab="team", team=team,
                      new_pw="", new_err="Name, a valid email and a role are required.")
    if models.get_user_by_email(email):
        return render("team.html", request, user=user, tab="team", team=team,
                      new_pw="", new_err="That email already has an account.")
    pw = secrets.token_urlsafe(9)
    models.create_user(email, auth.hash_pw(pw), name, user["business"], "",
                       role=role, parent_id=user["id"])
    models.log_activity(user["id"], "team", f"Added team member {name} as {role}")
    return render("team.html", request, user=user, tab="team",
                  team=models.list_team(user["id"]), new_pw=pw, new_err="")

@router.post("/team/{mid}/remove")
async def team_remove(request: Request, mid: str):
    user = guard(request)
    if not user or user["role"] != "owner":
        return RedirectResponse("/login", status_code=303)
    models.remove_team_member(mid, user["id"])
    models.log_activity(user["id"], "team", "Removed a team member")
    return RedirectResponse("/team", status_code=303)

@router.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    pays = models.list_plat_pays(user["id"])
    user["plan"] = config.plan_key(user.get("plan") or "starter")
    usage = models.plan_usage(user["id"])
    blocked = request.query_params.get("blocked", "")
    return render("billing.html", request, user=user, tab="subscription", pays=pays,
                  plans=config.PLANS, inr=inr, usage=usage, now_ts=time.time(), blocked=blocked)

@router.post("/billing/change-plan")
async def change_plan(request: Request, plan: str = Form(""), cycle: str = Form("monthly")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    plan = config.plan_key(plan)
    if plan in config.PLANS and plan != config.plan_key(user["plan"]):
        p = config.PLANS[plan]
        if user["plan_status"] == "trialing":
            # Plan changes during a free trial are free; the trial keeps running.
            models.update_user(user["id"], plan=plan, plan_status="trialing")
            models.log_audit(user["id"], "billing.plan_change", f"{plan} during trial (no charge)")
        else:
            # No verified payment -> no activation. The plan sits in pending_payment
            # until the payment is confirmed (admin panel or billing webhook).
            cyc = "annual" if cycle == "annual" else "monthly"
            amount = p["annual_amount"] if cyc == "annual" else p["amount"]
            models.update_user(user["id"], plan=plan, plan_status="pending_payment",
                               billing_cycle=cyc)
            models.record_plat_pay(user["id"], plan, amount, "pending",
                                   reference=f"plan-change-{int(time.time())}")
            models.log_audit(user["id"], "billing.plan_change", f"{plan}/{cyc} pending payment")
    return RedirectResponse("/billing", status_code=303)

@router.post("/billing/cancel")
async def cancel_sub(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.update_user(user["id"], plan_status="canceled")
    models.log_audit(user["id"], "billing.cancel", user["plan"])
    return RedirectResponse("/billing", status_code=303)


# ---------- payments ----------
@router.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    m = models.owner_metrics(user["id"])
    pays = models.recent_payments(user["id"], limit=50)
    fails = models.q("SELECT e.*, cu.name AS customer_name FROM pay_events e LEFT JOIN customers cu ON cu.id=e.customer_id WHERE e.owner_id=? AND e.kind='failed' ORDER BY e.created DESC LIMIT 50", (user["id"],))
    recon = {
        "confirmed_n": 0, "confirmed_amt": 0, "refunded_n": 0, "refunded_amt": 0,
        "net_amt": 0, "gateway_n": 0}
    for r in models.q("SELECT status, method, COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM payments WHERE owner_id=? GROUP BY status", (user["id"],)):
        if r["status"] == "confirmed":
            recon["confirmed_n"] = r["n"]; recon["confirmed_amt"] = r["amt"]
            recon["net_amt"] = r["amt"]
            recon["gateway_n"] = models.one("SELECT COUNT(*) AS n FROM payments WHERE owner_id=? AND status='confirmed' AND method='razorpay'", (user["id"],))["n"]
        elif r["status"] == "refunded":
            recon["refunded_n"] = r["n"]; recon["refunded_amt"] = r["amt"]
            recon["net_amt"] -= r["amt"]
    return render("payments.html", request, user=user, tab="payments", m=m, pays=pays, fails=fails, recon=recon, inr=inr)

@router.post("/payments/{pid}/refund")
async def payments_refund(request: Request, pid: str, note: str = Form("")):
    """Owner records a refund they processed at their own gateway."""
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    pay = models.one("SELECT * FROM payments WHERE id=? AND owner_id=?", (pid, user["id"]))
    if pay:
        r = models.refund_payment(pid, note=note or "", gateway="manual")
        if r:
            models.log_activity(user["id"], "refund", f"Refund recorded on {r['reference'] or r['id']}")
            models.log_audit(user["id"], "payment.refund", pid)
    return RedirectResponse("/payments", status_code=303)


# ---------- analytics ----------
@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    m = models.owner_metrics(user["id"])
    chart = models.recovery_series(user["id"], "monthly", 12)
    declines = models.decline_breakdown(user["id"])
    monthly = models.monthly_summary(user["id"], 6)
    astats = models.automation_stats(user["id"])
    react = models.reactivation_outcomes(user["id"])
    dormant_n = len(models.segment_rows(user["id"], "dormant"))
    return render("analytics.html", request, user=user, tab="analytics", m=m, chart=chart,
                  declines=declines, monthly=monthly, astats=astats, react=react,
                  dormant_n=dormant_n, inr=inr)

@router.get("/analytics/export")
async def analytics_export(request: Request, report: str = "recovery"):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if report == "failed":
        rows = models.q("SELECT e.created, cu.name AS customer, e.amount, COALESCE(e.decline_code,'') AS reason, e.gateway FROM pay_events e LEFT JOIN customers cu ON cu.id=e.customer_id WHERE e.owner_id=? AND e.kind='failed' ORDER BY e.created DESC", (user["id"],))
        head = ["when", "customer", "amount", "decline_reason", "gateway"]
        fname = "failed-payments-report.csv"
    else:
        rows = models.recovery_by_month(user["id"], 12)
        head = ["month", "recovered_amount"]
        fname = "recovery-report.csv"
    lines = [",".join(head)]
    for r in rows:
        d = dict(r)
        vals = []
        for h in head:
            v = d.get(h, "")
            if h == "when" and v:
                v = time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
            vals.append(str(v).replace(",", " "))
        lines.append(",".join(vals))
    if request.query_params.get("format") == "pdf":
        from . import pdfgen
        from fpdf import FPDF
        pdf = FPDF(format="A4")
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 20)
        title = "Failed payments report" if report == "failed" else "Recovery report"
        pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(90, 90, 100)
        pdf.cell(0, 7, user.get("business") or user.get("name") or "", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 7, "Generated " + time.strftime("%d %b %Y, %H:%M"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "B", 9)
        for h in head:
            pdf.cell(min(90, 180 / len(head)), 8, h[:20], border=1)
        pdf.ln(8)
        pdf.set_font("Helvetica", "", 9)
        for r in rows:
            d = dict(r)
            for h in head:
                v = str(d.get(h, ""))
                if h == "when" and d.get(h):
                    v = time.strftime("%Y-%m-%d", time.localtime(d[h]))
                pdf.cell(min(90, 180 / len(head)), 8, v[:26], border=1)
            pdf.ln(8)
        return Response(bytes(pdf.output()), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{fname.replace(".csv", ".pdf")}"'})
    return Response("\n".join(lines) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

# ---------- notifications ----------

# ---------- campaigns ----------
@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    rad = models.radar(user["id"])
    camp = [o for o in models.list_outbox(user["id"]) if o["tpl"] == "dormant_reactivate"]
    return render("campaigns.html", request, user=user, tab="campaigns",
                  dormant=rad["dormant"], camp=camp, inr=inr)

@router.post("/campaigns/run")
async def campaigns_run(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    counts = auto.run_dormant_reactivation(user)
    models.log_audit(user["id"], "campaign.run", str(counts))
    return RedirectResponse("/campaigns", status_code=303)

# ---------- integrations ----------
@router.get("/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render("integrations.html", request, user=user, tab="integrations",
                  rzp_connected=bool(payments.owner_creds(user)),
                  email_ready=notify.retailer_email_ready(user["id"]), wa_ready=notify.retailer_whatsapp_ready(user["id"]),
                  voice_ready=bool(ret.CALL_URL and ret.CALL_TOKEN),
                  upi_ready=bool((user.get("upi_vpa") or "").strip()))

@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    acts = models.list_activities(user["id"], limit=50)
    outbox = models.list_outbox(user["id"], limit=50)
    outbox = [{**o, "wa_link": _wa_link(o)} for o in outbox]
    return render("notifications.html", request, user=user, tab="notifications", acts=acts, outbox=outbox)

# ---------- subscription (alias of billing) ----------
@router.get("/subscription", response_class=HTMLResponse)
async def subscription_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    pays = models.list_plat_pays(user["id"])
    return render("billing.html", request, user=user, tab="subscription", pays=pays,
                  plans=config.PLANS, inr=inr)

# ---------- settings ----------
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    has_rzp = bool(payments.owner_creds(user))
    cn = models.get_connect(user["id"]) or {}
    return render("settings.html", request, user=user, tab="settings", has_rzp=has_rzp, inr=inr, cn=cn)

@router.post("/settings/profile")
async def settings_profile(request: Request, name: str = Form(""), business: str = Form(""),
                           phone: str = Form(""), upi_vpa: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.update_user(user["id"], name=name, business=business, phone=phone, upi_vpa=upi_vpa)
    models.log_audit(user["id"], "settings.profile", "updated")
    return RedirectResponse("/settings", status_code=303)

@router.post("/settings/razorpay")
async def settings_razorpay(request: Request, key_id: str = Form(""), key_secret: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if key_id and key_secret:
        models.set_user_secret(user["id"], "rzp_key_enc", vault.encrypt(key_id.strip()))
        models.set_user_secret(user["id"], "rzp_secret_enc", vault.encrypt(key_secret.strip()))
        creds = payments.owner_creds(user)
        ok = bool(creds) and not payments.api(creds, "GET", "/payments?count=1").get("error")
        models.log_audit(user["id"], "settings.razorpay", "verified" if ok else "saved-not-verified")
    return RedirectResponse("/settings", status_code=303)

# ---------- retailer-owned connection (customer messages go out as the retailer) ----------
@router.post("/settings/connect")
async def settings_connect(request: Request, business_name: str = Form(""),
                           smtp_host: str = Form(""), smtp_port: str = Form("587"),
                           smtp_user: str = Form(""), smtp_pass: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "owner":
        return RedirectResponse("/settings", status_code=303)
    models.save_connect(
        user["id"],
        business_name=(business_name or user.get("business") or user.get("name") or "").strip(),
        smtp_host=smtp_host.strip(), smtp_port=int(smtp_port or 587),
        smtp_user=smtp_user.strip(),
        smtp_pass_enc=vault.encrypt(smtp_pass) if smtp_pass else None)
    models.log_audit(user["id"], "settings.connect", "email")
    return RedirectResponse("/settings", status_code=303)

@router.post("/settings/connect/wa")
async def settings_connect_wa(request: Request, wa_phone_id: str = Form(""), wa_token: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "owner":
        return RedirectResponse("/settings", status_code=303)
    models.save_connect(user["id"], wa_phone_id=wa_phone_id.strip(),
                        wa_token_enc=vault.encrypt(wa_token) if wa_token else None)
    models.log_audit(user["id"], "settings.connect", "whatsapp")
    return RedirectResponse("/settings", status_code=303)

@router.post("/settings/connect/test")
async def settings_connect_test(request: Request, channel: str = Form("email"), dest: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if channel == "email":
        to = dest.strip() or (models.get_connect(user["id"]) or {}).get("smtp_user", "")
        st, err = notify.send_email(
            to, "Test email from your business connection",
            "This is a test message sent from your own business email through RecurSaathi. "
            "Your customer reminders will arrive from this same address.",
            owner_id=user["id"])
        msg = {"sent": "test email sent", "no_channel": "email not connected yet"}.get(st, f"test failed: {err}")
    else:
        ok, err = notify.verify_retailer_whatsapp(user["id"])
        msg = "whatsapp connection verified" if ok else f"whatsapp not verified: {err}" if err != "not configured" else "whatsapp not configured yet"
    from urllib.parse import quote
    return RedirectResponse(f"/settings?ctest={quote(msg)}", status_code=303)

@router.post("/settings/connect/pause")
async def settings_connect_pause(request: Request, paused: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "owner":
        return RedirectResponse("/settings", status_code=303)
    models.set_connect_paused(user["id"], 1 if paused == "1" else 0)
    models.log_audit(user["id"], "settings.connect.pause", paused)
    return RedirectResponse("/settings", status_code=303)

@router.get("/settings/backup")
async def settings_backup(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.log_audit(user["id"], "data.backup", models.DB_PATH)
    from fastapi.responses import FileResponse
    return FileResponse(models.DB_PATH, media_type="application/octet-stream",
                        filename="recursaathi-backup.sqlite3")

@router.post("/support")
async def support_send(request: Request, message: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if message:
        models.create_support_message(user["id"], user["name"], user["email"], message)
    return RedirectResponse("/settings", status_code=303)

# ---------- payment page QR (manual UPI, owner's own VPA) ----------
@router.get("/upi-qr/{iid}.png")
async def upi_qr(request: Request, iid: str):
    import qrcode, io as _io
    user = guard(request)
    if not user:
        return Response(status_code=401)
    inv = models.one("SELECT * FROM invoices WHERE id=? AND owner_id=?", (iid, user["id"]))
    if not inv:
        return Response(status_code=404)
    vpa = (user.get("upi_vpa") or "").strip()
    if not vpa:
        # Retailer-owned rule: a customer invoice QR goes to the retailer's own UPI,
        # never to RecurSaathi's. No VPA on file -> show a setup prompt, not a payment.
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (480, 140), "white")
        d = ImageDraw.Draw(img)
        d.text((20, 52), "Add your UPI ID in Settings first.", fill=(23, 32, 64))
        buf = _io.BytesIO(); img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    payee = (user.get("payee_name") or user.get("business") or user.get("name") or "").replace(" ", "%20")
    uri = f"upi://pay?pa={vpa}&pn={payee}&am={inv['amount']/100:.2f}&cu=INR&tn=Invoice%20{inv['number']}"
    img = qrcode.make(uri)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

# ---------- public legal + contact ----------
@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return render("privacy.html", request)

@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return render("terms.html", request)

@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return render("contact.html", request)

@router.post("/contact-send")
async def contact_send(request: Request, name: str = Form(""), email: str = Form(""), message: str = Form("")):
    if name and email and message:
        models.create_support_message(None, name, email, message)
    return render("contact_done.html", request)

# ---------- scheduler tick (call from cron / manually) ----------
@router.get("/cron/run")
async def cron_run(request: Request, x_admin_token: str = ""):
    if x_admin_token != config.ADMIN_TOKEN:
        return HTMLResponse("unauthorized", status_code=401)
    owners = models.q("SELECT * FROM users WHERE role='owner' AND plan_status IN ('active','trialing')")
    base = base_url(request)
    results = {}
    for o in owners:
        try:
            results[o["email"]] = auto.run_for_owner(o, base)
        except Exception as e:
            results[o["email"]] = {"error": str(e)[:200]}
    return results


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    return render("forgot.html", request, error="")

@router.post("/forgot")
async def forgot_send(request: Request, email: str = Form("")):
    u = models.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    link = ""
    if u:
        tok = models.token(32)
        models.set_reset_token(email, tok)
        base = getattr(config, "APP_BASE_URL", "http://localhost:8000")
        full = base + "/reset/" + tok
        if notify.smtp_ready():
            notify.send_email(email, "Reset your RecurSaathi password",
                f"Hi {u['name'] or 'there'},\n\nReset your password with this link (valid for 1 hour):\n{full}\n\nIf you didn't ask for this, you can ignore this email.")
        elif (request.url.hostname or "") in ("localhost", "127.0.0.1"):
            # local-dev convenience only: a deployed host must never render a
            # reset link in the page (email enumeration + account takeover)
            link = full
    return render("forgot_done.html", request, link=link)

@router.get("/reset/{tok}", response_class=HTMLResponse)
async def reset_page(request: Request, tok: str):
    u = models.user_by_reset(tok)
    if not u:
        return render("reset.html", request, valid=False, token=tok, error="")
    return render("reset.html", request, valid=True, token=tok, error="")

@router.post("/reset/{tok}")
async def reset_pw(request: Request, tok: str, password: str = Form("")):
    u = models.user_by_reset(tok)
    if not u:
        return render("reset.html", request, valid=False, token=tok, error="This reset link has expired.")
    if len(password) < 8:
        return render("reset.html", request, valid=True, token=tok, error="Password must be at least 8 characters.")
    models.change_password(u["id"], auth.hash_pw(password))
    models.clear_reset(u["id"])
    r = RedirectResponse("/dashboard", status_code=303)
    auth.start_session(r, u["id"])
    return r

@router.post("/settings/password")
async def settings_password(request: Request, current_password: str = Form(""),
                            new_password: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not auth.check_pw(current_password, user["pw_hash"]) or len(new_password) < 8:
        return RedirectResponse("/settings?pw=err", status_code=303)
    models.change_password(user["id"], auth.hash_pw(new_password))
    models.log_audit(user["id"], "settings.password", "changed")
    r = RedirectResponse("/settings", status_code=303)
    auth.start_session(r, user["id"])  # re-issue this browser's session; all older sessions are revoked
    return r


# ---------- Enterprise: multi-branch management ----------
@router.get("/branches", response_class=HTMLResponse)
async def branches_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "multi_branch")
    if blocked:
        return blocked
    stats = models.branch_stats(user["id"])
    customers = models.q("SELECT id, name FROM customers WHERE owner_id=? ORDER BY name LIMIT 200", (user["id"],))
    return render("branches.html", request, user=user, tab="branches", stats=stats,
                  branches=models.list_branches(user["id"]), customers=customers,
                  max_branches=config.PLANS[config.plan_key(user["plan"])]["branches"],
                  err="", inr=inr)

@router.post("/branches/add")
async def branches_add(request: Request, name: str = Form(""), city: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "multi_branch")
    if blocked:
        return blocked
    stats = models.branch_stats(user["id"])
    if len(stats) >= config.PLANS[config.plan_key(user["plan"])]["branches"]:
        return render("branches.html", request, user=user, tab="branches", stats=stats,
                      branches=models.list_branches(user["id"]),
                      customers=models.q("SELECT id, name FROM customers WHERE owner_id=? ORDER BY name LIMIT 200", (user["id"],)),
                      max_branches=config.PLANS[config.plan_key(user["plan"])]["branches"],
                      err="Your plan covers 5 branches. Talk to us about more.", inr=inr)
    models.add_branch(user["id"], name, city)
    models.log_activity(user["id"], "branches", f"Added branch {name}")
    return RedirectResponse("/branches", status_code=303)

@router.post("/branches/rename")
async def branches_rename(request: Request, bid: str = Form(""), name: str = Form(""), city: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "multi_branch")
    if blocked:
        return blocked
    if bid and models.get_branch(bid, user["id"]):
        models.rename_branch(bid, user["id"], name, city)
    return RedirectResponse("/branches", status_code=303)

@router.post("/branches/delete")
async def branches_delete(request: Request, bid: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "multi_branch")
    if blocked:
        return blocked
    if bid:
        models.delete_branch(bid, user["id"])
        models.log_activity(user["id"], "branches", "Removed a branch (its records stay)")
    return RedirectResponse("/branches", status_code=303)

@router.post("/branches/assign")
async def branches_assign(request: Request, kind: str = Form("customer"),
                          item_id: str = Form(""), branch_id: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "multi_branch")
    if blocked:
        return blocked
    if kind in ("customer", "invoice") and item_id:
        models.assign_branch(user["id"], kind, item_id, branch_id)
    return RedirectResponse("/branches", status_code=303)


# ---------- Enterprise: custom automation rules ----------
@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_rules")
    if blocked:
        return blocked
    return render("rules.html", request, user=user, tab="rules", rules=models.list_rules(user["id"]),
                  usage=models.plan_usage(user["id"]), err="", note="")

@router.post("/rules/add")
async def rules_add(request: Request, name: str = Form(""), trigger: str = Form(""),
                    threshold: str = Form(""), action: str = Form(""), template: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_rules")
    if blocked:
        return blocked
    if not models.add_rule(user["id"], name, trigger, threshold, action, template):
        return render("rules.html", request, user=user, tab="rules", rules=models.list_rules(user["id"]),
                      usage=models.plan_usage(user["id"]),
                      err="Pick a name, a trigger and an action.", note="")
    if models.plan_limit_msg(user["id"], "workflows"):
        return render("rules.html", request, user=user, tab="rules", rules=models.list_rules(user["id"]),
                      usage=models.plan_usage(user["id"]),
                      err="Workflow limit reached on your plan.", note="")
    models.log_activity(user["id"], "rules", f"Added automation rule {name}")
    return RedirectResponse("/rules", status_code=303)

@router.post("/rules/toggle")
async def rules_toggle(request: Request, rid: str = Form(""), enabled: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_rules")
    if blocked:
        return blocked
    models.set_rule(user["id"], rid, enabled == "1")
    return RedirectResponse("/rules", status_code=303)

@router.post("/rules/delete")
async def rules_delete(request: Request, rid: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_rules")
    if blocked:
        return blocked
    models.delete_rule(user["id"], rid)
    return RedirectResponse("/rules", status_code=303)

@router.post("/rules/run")
async def rules_run(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_rules")
    if blocked:
        return blocked
    if user.get("read_only"):
        return RedirectResponse("/billing", status_code=303)
    n = run_custom_rules(user["id"], base_url(request))
    return render("rules.html", request, user=user, tab="rules", rules=models.list_rules(user["id"]),
                  usage=models.plan_usage(user["id"]), err="",
                  note=f"Rules ran: {n} message(s) queued." if n else "Rules ran: nothing matched right now.")


def run_custom_rules(owner_id, base=""):
    """Run enabled custom rules against real records; queue messages in the
    outbox (deduped, so a customer never gets the same rule twice per invoice)."""
    from . import automations as auto
    u = models.get_user(owner_id)
    if not u:
        return 0
    matches = models.rule_matches(owner_id)
    n = 0
    for r in models.list_rules(owner_id):
        if not r["enabled"]:
            continue
        thr = float(r["threshold"] or 0)
        tpl = r["template"]
        if r["trigger"] == "invoice_overdue_days":
            rows = [i for i in matches["invoice_overdue_days"] if i["days_overdue"] >= thr]
        else:
            rows = [e for e in matches["payment_failed"] if (e.get("amount") or 0) >= thr * 100]
        for row in rows:
            cu = models.get_customer(row.get("customer_id")) or {}
            inv = models.get_invoice(row.get("invoice_id") or row.get("id")) or {}
            if not cu.get("email") and not cu.get("phone"):
                continue
            ctx = auto._ctx(u, inv or {"number": "-", "amount": row.get("amount") or 0,
                                       "due_date": row.get("due_date", ""), "token": ""},
                            cu, base)
            subject = auto._render("Payment reminder from {{business}}", **ctx)
            body = auto._render(tpl or ("Hi {{customer_name}}, a quick automated reminder about "
                                        "invoice {{invoice_number}} for {{amount}}. "
                                        "Pay here: {{payment_link}}"), **ctx)
            wb = auto._render(tpl or ("Hi {{customer_name}}, a gentle reminder about invoice "
                                      "{{invoice_number}} for {{amount}}. Pay here: {{payment_link}}"), **ctx)
            sent = False
            body = body + auto._unsub_footer(base, cu)
            if r["action"] in ("email", "both") and cu.get("email") and not cu.get("email_opt_out"):
                dedupe = f"rule:{r['id']}:{row.get('id')}:email"
                o = models.queue(owner_id, "email", cu["email"], "custom_rule", subject, body, dedupe=dedupe)
                if o:
                    if not auto._post_queue(u, o):
                        notify.deliver(o)
                        sent = True
            if r["action"] in ("whatsapp", "both") and cu.get("phone") and not cu.get("wa_opt_out"):
                dedupe = f"rule:{r['id']}:{row.get('id')}:wa"
                o = models.queue(owner_id, "whatsapp", cu["phone"], "custom_rule", subject, wb, dedupe=dedupe)
                if o:
                    if not auto._post_queue(u, o):
                        notify.deliver(o)
                        sent = True
            if sent:
                n += 1
                models.bump_usage(owner_id, "rules_fires")
    if n:
        models.log_activity(owner_id, "rules", f"Custom rules queued {n} message(s)")
    return n


# ---------- Enterprise: custom business reports ----------
@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_reports")
    if blocked:
        return blocked
    return render("reports.html", request, user=user, tab="reports", done="", inr=inr)

@router.post("/reports/generate")
async def reports_generate(request: Request, title: str = Form("Business report"),
                           sections: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "custom_reports")
    if blocked:
        return blocked
    from . import pdfgen
    picked = [s_ for s_ in (sections or "").split(",") if s_]
    m = models.owner_metrics(user["id"])
    today = time.strftime("%Y-%m-%d")
    out = []
    if not picked or "overview" in picked:
        out.append(("Overview", [
            ["Metric", "Value"],
            ["Total invoices", str(m["total"])],
            ["Recovered", inr(m["recovered"])],
            ["Outstanding", inr(m["outstanding"])],
            ["Overdue invoices", str(m["overdue_count"])],
            ["Recovery rate", f"{m['success_rate']}%"],
            ["Failed payments", f"{m['failed_count']} ({inr(m['failed_amount'])})"],
        ], [110, 90]))
    if "monthly" in picked:
        rows = [[r["m"], str(r["invoiced"]), inr(r["invoiced_amt"]), inr(r["recovered"])]
                for r in models.monthly_summary(user["id"], 6)]
        out.append(("Monthly invoiced vs recovered", [["Month", "Invoices", "Invoiced", "Recovered"]] + rows, [40, 40, 60, 60]))
    if "aging" in picked:
        leak = models.predictive_leak(user["id"])
        out.append(("Overdue ageing (at-risk estimate)", [
            ["Ageing bucket", "Amount"],
            ["1-7 days", f"Rs {leak['aging']['1_7']:,.0f}"],
            ["8-30 days", f"Rs {leak['aging']['8_30']:,.0f}"],
            ["30+ days", f"Rs {leak['aging']['30_plus']:,.0f}"],
            ["Estimated monthly revenue at risk", f"Rs {leak['monthly_risk']:,.0f}"],
        ], [110, 90]))
    if "customers" in picked:
        rows = models.q("""SELECT cu.name, COUNT(i.id) AS n, COALESCE(SUM(i.amount),0) AS amt
            FROM customers cu JOIN invoices i ON i.customer_id=cu.id
            WHERE cu.owner_id=? GROUP BY cu.id ORDER BY amt DESC LIMIT 10""", (user["id"],))
        out.append(("Top customers by invoiced value", [["Customer", "Invoices", "Value"]] +
                    [[r["name"], str(r["n"]), inr(r["amt"])] for r in rows], [90, 40, 70]))
    if "failed" in picked:
        rows = [[r["code"], str(r["n"]), inr(r["amt"])] for r in models.decline_breakdown(user["id"])]
        out.append(("Failed payment reasons", [["Reason", "Count", "Amount"]] + rows, [90, 40, 70]))
    data = pdfgen.business_report_pdf_bytes(user, title or "Business report", out)
    models.log_audit(user["id"], "reports.generate", ",".join(picked))
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="business-report.pdf"'})


# ---------- Enterprise: API keys (settings) ----------
@router.post("/settings/apikey/generate")
async def settings_apikey_generate(request: Request, label: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "api_access")
    if blocked:
        return blocked
    raw = models.create_api_key(user["id"], label)
    models.log_audit(user["id"], "apikey.generate", label or "")
    has_rzp = bool(payments.owner_creds(user))
    cn = models.get_connect(user["id"]) or {}
    return render("settings.html", request, user=user, tab="settings", has_rzp=has_rzp, inr=inr, cn=cn,
                  api_keys=models.list_api_keys(user["id"]), new_api_key=raw)

@router.post("/settings/apikey/revoke")
async def settings_apikey_revoke(request: Request, kid: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    blocked = feature_gate(user, "api_access")
    if blocked:
        return blocked
    models.revoke_api_key(kid, user["id"])
    models.log_audit(user["id"], "apikey.revoke", kid)
    return RedirectResponse("/settings", status_code=303)


# ---------- reliability upgrade routes ----------

def _owner_only(user):
    return user and user.get("role") == "owner"

# ---- onboarding wizard ----
@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, step: str = "1"):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("onboarded"):
        return RedirectResponse("/dashboard", status_code=303)
    step = step if step in ("1", "2", "3") else "1"
    cn = models.get_connect(user["id"]) or {}
    return render("onboarding.html", request, user=user, tab="", step=step, cn=cn)

@router.post("/onboarding/step1")
async def onboarding_step1(request: Request, name: str = Form(""), business: str = Form(""),
                           phone: str = Form(""), upi_vpa: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.update_user(user["id"], name=name, business=business, phone=phone, upi_vpa=upi_vpa)
    return RedirectResponse("/onboarding?step=2", status_code=303)

@router.post("/onboarding/step2")
async def onboarding_step2(request: Request, business_name: str = Form(""), smtp_host: str = Form(""),
                           smtp_port: str = Form("587"), smtp_user: str = Form(""),
                           smtp_pass: str = Form(""), skip: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not skip:
        models.save_connect(user["id"], business_name=business_name or user.get("business", ""),
                            smtp_host=smtp_host.strip(), smtp_port=int(smtp_port or 587),
                            smtp_user=smtp_user.strip(),
                            smtp_pass_enc=vault.encrypt(smtp_pass) if smtp_pass else None)
    return RedirectResponse("/onboarding?step=3", status_code=303)

@router.post("/onboarding/finish")
async def onboarding_finish(request: Request, mode: str = Form("auto"),
                            cname: str = Form(""), cemail: str = Form(""), cphone: str = Form(""),
                            skip: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not skip and cname.strip():
        models.create_customer(user["id"], cname.strip(), cemail.strip(), cphone.strip())
    models.set_automation_mode(user["id"], mode)
    models.update_user(user["id"], onboarded=1)
    models.log_activity(user["id"], "onboarding", "Setup completed")
    models.log_audit(user["id"], "onboarding.finish", f"mode={mode}")
    return RedirectResponse("/dashboard", status_code=303)

# ---- approval queue ----
@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    items = models.list_outbox_status(user["id"], ["approval"], limit=100)
    items = [{**o, "wa_link": _wa_link(o)} for o in items]
    return render("approvals.html", request, user=user, tab="approvals", items=items,
                  mode=models.get_automation_mode(user["id"]), inr=inr)

@router.post("/approvals/{oid}/approve")
async def approval_approve(request: Request, oid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    o = models.approve_outbox(user["id"], oid)
    if o:
        st = notify.deliver(o)
        models.log_audit(user["id"], "approval.approve", f"{oid}:{st}")
    return RedirectResponse("/approvals", status_code=303)

@router.post("/approvals/{oid}/discard")
async def approval_discard(request: Request, oid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.discard_outbox(user["id"], oid)
    models.log_audit(user["id"], "approval.discard", oid)
    return RedirectResponse("/approvals", status_code=303)

# ---- automation activity log ----
@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, status: str = "all"):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    flt = status if status in ("all", "sent", "pending", "failed", "approval") else "all"
    if flt == "all":
        outbox = models.list_outbox(user["id"], limit=150)
    elif flt == "sent":
        outbox = models.list_outbox_status(user["id"], ["sent"])
    elif flt == "failed":
        outbox = models.list_outbox_status(user["id"], ["failed", "no_channel"])
    elif flt == "approval":
        outbox = models.list_outbox_status(user["id"], ["approval"])
    else:
        outbox = models.list_outbox_status(user["id"], ["queued", "pending"])
    acts = models.list_activities(user["id"], limit=60)
    outbox = [{**o, "wa_link": _wa_link(o)} for o in outbox]
    return render("activity.html", request, user=user, tab="activity", outbox=outbox,
                  acts=acts, flt=flt, obc=models.outbox_counts(user["id"]), inr=inr)

# ---- customer unsubscribe (public) ----
@router.get("/u/{token}", response_class=HTMLResponse)
async def unsubscribe_page(request: Request, token: str):
    cu = models.customer_by_unsub_token(token)
    return render("unsubscribe.html", request, user=None, cu=cu, token=token, done=False)

@router.post("/u/{token}")
async def unsubscribe_confirm(request: Request, token: str):
    cu = models.opt_out_by_token(token)
    return render("unsubscribe.html", request, user=None, cu=cu, token=token, done=bool(cu))

# ---- settings: automation mode, 2FA, data export, account deletion ----
@router.post("/settings/mode")
async def settings_mode(request: Request, mode: str = Form("auto")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if _owner_only(user):
        models.set_automation_mode(user["id"], mode)
        models.log_audit(user["id"], "settings.mode", mode)
    return RedirectResponse("/settings", status_code=303)

@router.get("/settings/2fa", response_class=HTMLResponse)
async def settings_2fa(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user.get("totp_secret"):
        models.set_totp(user["id"], secret=models.gen_totp_secret())
        user = models.get_user(user["id"])
    import urllib.parse as _up
    label = _up.quote(f"RecurSaathi:{user.get('email', '')}")
    uri = (f"otpauth://totp/{label}?secret={user['totp_secret']}"
           f"&issuer=RecurSaathi&algorithm=SHA1&digits=6&period=30")
    return render("twofa.html", request, user=user, tab="settings", uri=uri)

@router.post("/settings/2fa/enable")
async def settings_2fa_enable(request: Request, code: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ok = models.verify_totp(user.get("totp_secret", ""), code)
    models.set_totp(user["id"], enabled=ok)
    models.log_audit(user["id"], "settings.2fa", "enabled" if ok else "enable-failed")
    from urllib.parse import quote
    msg = "Two-factor authentication is on. Keep your authenticator app safe." if ok else "That code did not match. Try again."
    return RedirectResponse(f"/settings?ctest={quote(msg)}", status_code=303)

@router.post("/settings/2fa/disable")
async def settings_2fa_disable(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.set_totp(user["id"], secret="", enabled=0)
    models.log_audit(user["id"], "settings.2fa", "disabled")
    return RedirectResponse("/settings", status_code=303)

@router.get("/settings/export-data")
async def settings_export_data(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from fastapi.responses import Response
    data = json.dumps(models.export_owner_data(user["id"]), indent=2, default=str)
    models.log_audit(user["id"], "data.export", "json")
    return Response(content=data, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="recursaathi-data.json"'})

@router.post("/settings/delete-account")
async def settings_delete_account(request: Request, confirm: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if confirm.strip().upper() != "DELETE" or not _owner_only(user):
        from urllib.parse import quote
        return RedirectResponse(f"/settings?ctest={quote('Type DELETE exactly to confirm account deletion.')}", status_code=303)
    oid = user["id"]
    models.delete_owner_data(oid)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp

# ---- customers: CSV import + consent toggles ----
@router.post("/customers/import")
async def customers_import(request: Request, file: "fastapi.UploadFile" = None):
    import fastapi
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    created, skipped = 0, 0
    if file is not None:
        raw = (await file.read()).decode("utf-8-sig", errors="replace")
        for i, row in enumerate(csv.reader(io.StringIO(raw))):
            if not row or (i == 0 and "name" in [c.strip().lower() for c in row]):
                continue
            name = (row[0] if len(row) > 0 else "").strip()
            email = (row[1] if len(row) > 1 else "").strip()
            phone = (row[2] if len(row) > 2 else "").strip()
            if not name:
                skipped += 1
                continue
            try:
                models.create_customer(user["id"], name, email, phone)
                created += 1
            except Exception:
                skipped += 1
    models.log_activity(user["id"], "import", f"CSV import: {created} customers added, {skipped} skipped")
    return RedirectResponse(f"/customers", status_code=303)

@router.post("/customers/{cid}/consent")
async def customers_consent(request: Request, cid: str, field: str = Form(""), value: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    val = 1 if value == "1" else 0
    if field == "email":
        models.set_customer_optout(user["id"], cid, email_opt=val)
    elif field == "wa":
        models.set_customer_optout(user["id"], cid, wa_opt=val)
    return RedirectResponse(f"/customers/{cid}", status_code=303)

# ---- payment verification ----
@router.post("/payments/{pid}/confirm")
async def payments_confirm(request: Request, pid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    pay = models.one("SELECT * FROM payments WHERE id=? AND owner_id=? AND status='claimed'", (pid, user["id"]))
    if not pay:
        return RedirectResponse("/payments", status_code=303)
    inv = models.get_invoice(pay["invoice_id"])
    cu = models.get_customer(pay["customer_id"])
    vp = models.verify_claim(pid)
    if vp:
        models.log_audit(user["id"], "payment.verified", pid)
        if inv and cu:
            auto.send_confirmation(user, inv, cu, base_url(request))
    return RedirectResponse("/payments", status_code=303)

@router.get("/receipt/{pid}", response_class=HTMLResponse)
async def receipt_page(request: Request, pid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    pay = models.one("SELECT * FROM plat_pays WHERE id=? AND owner_id=?", (pid, user["id"]))
    if not pay:
        return RedirectResponse("/billing", status_code=303)
    return render("receipt.html", request, user=user, pay=pay, inr=inr)


# ---------- Phase 7: follow-up sequences module ----------
@router.get("/sequences", response_class=HTMLResponse)
async def sequences_page(request: Request, rows: int = 0):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from . import sequences as _seq
    steps = _seq.get_sequence(user["id"])
    n = max(len(steps), rows if 0 < rows <= 10 else 0)
    rows_out = []
    for i in range(n):
        s = steps[i] if i < len(steps) else {}
        rows_out.append({
            "id": s.get("id", f"step{i+1}"),
            "offset": s.get("offset", i * 7),
            "email": "email" in (s.get("channels") or ["email", "whatsapp"]),
            "whatsapp": "whatsapp" in (s.get("channels") or ["email", "whatsapp"]),
            "escalate": bool(s.get("escalate")),
            "approval": bool(s.get("approval_required")),
            "max_attempts": s.get("max_attempts", 0),
        })
    return render("sequences.html", request, user=user, tab="sequences",
                  rows=rows_out, n=n, msg=request.query_params.get("msg", ""))

@router.post("/sequences/save")
async def sequences_save(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from . import sequences as _seq
    form = await request.form()
    try:
        n = int(form.get("n") or "0")
    except Exception:
        n = 0
    n = max(1, min(n, 10))
    steps = []
    for i in range(n):
        sid = (form.get(f"sid_{i}") or "").strip()
        if not sid:
            continue
        try:
            off = int(form.get(f"off_{i}") or "0")
        except Exception:
            off = 0
        chans = [c for c in ("email", "whatsapp") if form.get(f"ch_{i}_{c}")]
        step = {"id": sid, "offset": off, "channels": chans or ["email", "whatsapp"]}
        if form.get(f"esc_{i}"):
            step["escalate"] = True
        if form.get(f"appr_{i}"):
            step["approval_required"] = True
        try:
            ma = int(form.get(f"ma_{i}") or "0")
        except Exception:
            ma = 0
        if ma:
            step["max_attempts"] = ma
        steps.append(step)
    if user.get("read_only"):
        return RedirectResponse("/sequences?msg=Subscription+expired+-+read-only", status_code=303)
    if user["role"] != "owner":
        return RedirectResponse("/sequences?msg=Only+the+owner+can+edit+sequences", status_code=303)
    try:
        saved = _seq.set_sequence(user["id"], steps)
        models.log_audit(user["id"], "sequence_saved_ui", f"{len(saved)} steps")
        return RedirectResponse("/sequences?msg=Sequence+saved", status_code=303)
    except ValueError as e:
        steps_now = _seq.get_sequence(user["id"])
        n2 = max(len(steps_now), n)
        rows_out = []
        for i in range(n2):
            s = steps_now[i] if i < len(steps_now) else {}
            rows_out.append({
                "id": s.get("id", f"step{i+1}"),
                "offset": s.get("offset", i * 7),
                "email": "email" in (s.get("channels") or ["email", "whatsapp"]),
                "whatsapp": "whatsapp" in (s.get("channels") or ["email", "whatsapp"]),
                "escalate": bool(s.get("escalate")),
                "approval": bool(s.get("approval_required")),
                "max_attempts": s.get("max_attempts", 0),
            })
        return render("sequences.html", request, user=user, tab="sequences",
                      rows=rows_out, n=n2, msg=f"Could not save: {e}")

# ---------- Phase 7: help & support module ----------
@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from . import providers as _providers
    channels = []
    for ch in ("email", "whatsapp"):
        cfg = _providers.masked_provider_config(user["id"], ch)
        channels.append({"channel": ch, "connected": bool(cfg)})
    return render("help.html", request, user=user, tab="help", channels=channels,
                  msg=request.query_params.get("msg", ""))