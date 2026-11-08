import time

"""Owner web app: dashboard, customers, invoices, radar, automations, billing."""
import csv, io, os, secrets, time
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from . import models, auth, vault, payments, notify, automations as auto, config, retention as ret

def _role_gate(request: Request):
    u = auth.current_user(request)
    if not u:
        return
    if u["role"] in ("staff", "viewer") and request.url.path.startswith(("/billing", "/settings", "/team", "/admin")):
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
    t = env.get_template(name)
    return HTMLResponse(t.render(request=request, user=user, active_tab=ctx.pop("tab", ""),
                                 trial_left=trial_left, **ctx))

def inr(p): return f"\u20b9{p/100:,.0f}"


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
    return row

# ---------- auth pages ----------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render("login.html", request, error="")

@router.post("/login")
async def login(request: Request, email: str = Form(""), password: str = Form("")):
    u = models.get_user_by_email(email)
    if not u or not auth.check_pw(password, u["pw_hash"]):
        return render("login.html", request, error="Wrong email or password.")
    resp = RedirectResponse("/dashboard", status_code=303)
    auth.start_session(resp, u["id"])
    models.log_audit(u["id"], "login", u["email"])
    return resp

@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return render("signup.html", request, error="")

@router.post("/signup")
async def signup(request: Request, name: str = Form(""), business: str = Form(""),
                 email: str = Form(""), phone: str = Form(""),
                 upi_vpa: str = Form(""), password: str = Form("")):
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
    for k in models.AUTO_KEYS:
        models.set_automation(u["id"], k, True)
        d = auto.DEFAULTS[k]
        if "email_subject" in d:  # due_call is calls-only: no email template
            models.set_template(u["id"], k, d["email_subject"], d["email_body"])
    models.log_audit(u["id"], "signup", email)
    import uuid as _uuid
    from . import billing as _billing
    mid = "m_" + _uuid.uuid4().hex[:10]
    _m = _billing.onboard(mid, email, u["plan"] or "starter")
    _m.name = name
    _m.phone = phone.strip()
    _billing._persist(_m)
    resp = RedirectResponse("/login?created=1", status_code=303)  # signup lands on the login page; dashboard comes after sign-in
    return resp

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
    return render("dashboard.html", request, user=user, tab="dashboard", m=m, chart=chart,
                  acts=acts, pays=pays, fails=fails, ov=ov, inr=inr, trial_left=trial_left, period=period,
                  channel_note=notify.retailer_email_ready(user["id"]) or notify.retailer_whatsapp_ready(user["id"]))

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
        pay = models.mark_invoice_paid(inv, "upi", reference=reference, note=note)
        cu = models.get_customer(inv["customer_id"])
        models.log_activity(user["id"], "payment", f"Marked invoice {inv['number']} paid ({inr(inv['amount'])}, UPI)")
        models.log_audit(user["id"], "invoice.paid", inv["number"])
        if cu:
            auto.send_confirmation(user, inv, cu, base_url(request))
    return RedirectResponse("/invoices", status_code=303)


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
    if not name or not email or "@" not in email or role not in ("staff", "viewer"):
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
    usage = models.plan_usage(user["id"])
    blocked = request.query_params.get("blocked", "")
    return render("billing.html", request, user=user, tab="subscription", pays=pays,
                  plans=config.PLANS, inr=inr, usage=usage, now_ts=time.time(), blocked=blocked)

@router.post("/billing/change-plan")
async def change_plan(request: Request, plan: str = Form(""), cycle: str = Form("monthly")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if plan in config.PLANS and plan != user["plan"]:
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
    return render("payments.html", request, user=user, tab="payments", m=m, pays=pays, fails=fails, inr=inr)

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
        link = getattr(config, "APP_BASE_URL", "http://localhost:8000") + "/reset/" + tok
        if notify.smtp_ready():
            notify.send_email(email, "Reset your RecurSaathi password",
                f"Hi {u['name'] or 'there'},\n\nReset your password with this link (valid for 1 hour):\n{link}\n\nIf you didn't ask for this, you can ignore this email.")
    return render("forgot_done.html", request, link=("" if notify.smtp_ready() else link))

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
    return RedirectResponse("/settings", status_code=303)
