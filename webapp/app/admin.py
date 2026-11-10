"""Platform administrator dashboard: separate role, separate route space."""
import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from . import models, auth

router = APIRouter()

def render(name, request, user=None, **ctx):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    env.filters["inr"] = lambda p: f"\u20b9{p/100:,.0f}"
    env.filters["datetime"] = lambda ts: (time.strftime("%d %b %Y, %H:%M", time.localtime(ts)) if ts else "-")
    env.globals["inr"] = env.filters["inr"]
    env.globals["datetime"] = env.filters["datetime"]
    return env.get_template(name).render(request=request, user=user, **ctx)

def guard(request):
    u = auth.current_user(request)
    if not u or u["role"] != "admin":
        return None
    return u

@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    owners = models.q("SELECT * FROM users WHERE role='owner' ORDER BY created DESC")
    subs_active = [o for o in owners if o["plan_status"] == "active"]
    mrr = sum(1166 for o in subs_active)  # monthly normalized estimate; per-plan below
    per_plan = {"starter": 999, "growth": 4999, "enterprise": 5999, "scale": 5999}
    mrr = sum(per_plan.get(o["plan"], 0) for o in subs_active)
    pays = models.list_plat_pays()
    support = models.list_support()
    audit = models.q("SELECT a.*, u.email AS email FROM audit a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.created DESC LIMIT 50")
    trial = [o for o in owners if o["plan_status"] == "trialing"]
    ovr = models.admin_overview() if hasattr(models, "admin_overview") else {}
    claims = models.q("SELECT p.*, i.number AS inv_number, cu.name AS customer_name FROM payments p "
                      "LEFT JOIN invoices i ON i.id=p.invoice_id LEFT JOIN customers cu ON cu.id=p.customer_id "
                      "WHERE p.status='claimed' ORDER BY p.created DESC LIMIT 50") \
             if "payments" in [r[0] for r in models.q("SELECT name FROM sqlite_master WHERE type='table'")] else []
    obx = models.q("SELECT o.*, u.business AS business FROM outbox o LEFT JOIN users u ON u.id=o.owner_id "
                   "ORDER BY o.created DESC LIMIT 40")
    return render("admin.html", request, user=user, owners=owners, subs_active=subs_active,
                  mrr=mrr, pays=pays, support=support, audit=audit, trial=trial,
                  ovr=ovr, claims=claims, obx=obx)

@router.post("/admin/claims/{pid}/confirm")
async def admin_claim_confirm(request: Request, pid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    p = models.one("SELECT * FROM payments WHERE id=? AND status='claimed'", (pid,))
    if p:
        owner = models.get_user(p["owner_id"])
        inv = models.get_invoice(p["invoice_id"])
        cu = models.get_customer(p["customer_id"])
        if models.verify_claim(pid) and owner and inv and cu:
            from . import automations as auto
            auto.send_confirmation(owner, inv, cu, "")
        models.log_audit(user["id"], "admin.claim.verify", pid)
    return RedirectResponse("/admin", status_code=303)

@router.post("/admin/support/{sid}/resolve")
async def resolve_support(request: Request, sid: str):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    models.ex("UPDATE support SET status='resolved' WHERE id=?", (sid,))
    return RedirectResponse("/admin", status_code=303)

@router.post("/admin/owners/{oid}/plan")
async def owner_plan(request: Request, oid: str, plan_status: str = Form("")):
    user = guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if plan_status in ("active", "trialing", "canceled"):
        kw = {"plan_status": plan_status}
        if plan_status == "active":
            kw["trial_ends"] = None  # paid plan: unlimited, no trial clock
        models.update_user(oid, **kw)
        models.log_audit(user["id"], "admin.plan_status", f"{oid}->{plan_status}")
    return RedirectResponse("/admin", status_code=303)
