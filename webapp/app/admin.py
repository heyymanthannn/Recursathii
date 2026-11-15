"""Platform administrator dashboard: separate role, separate route space."""
import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from . import models, auth, config

router = APIRouter()

def _token_admin(request):
    """ADMIN_TOKEN holders also reach the bank verification screens.

    /admin/login sets an admin_token cookie holding only the SHA-256 digest
    of ADMIN_TOKEN; the raw token never travels. Fails closed when the token
    is unset or the digest is missing or wrong. The synthetic principal is
    audited as "admin-token" so the trail stays truthful about who decided.
    """
    if not config.admin_cookie_ok(request.cookies.get("admin_token", "")):
        return None
    return {"id": "admin-token", "role": "admin", "name": "Platform Admin",
            "email": "admin-token", "business": "RecurSaathi Platform"}

def bank_guard(request):
    """Bank-verification routes accept an admin-role user session OR a valid
    ADMIN_TOKEN cookie. Ordinary business users and anonymous visitors get
    neither and are redirected to /login."""
    u = guard(request)
    return u if u else _token_admin(request)

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
    per_plan = {"starter": 999, "growth": 4999, "enterprise": 9999, "scale": 9999}
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


# ---------- Phase 24: manual bank-transfer verification (admin only) ----------
@router.get("/admin/bank", response_class=HTMLResponse)
async def admin_bank(request: Request):
    """Payment verification screen. Only authorized platform admins reach it;
    every submission sits in PENDING_VERIFICATION until a human decides."""
    user = bank_guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from . import manual_bank
    names = {o["id"]: (o.get("business") or o.get("email") or o["id"])
             for o in models.q("SELECT id, business, email FROM users")}
    return render("admin_bank.html", request, user=user,
                  pending=manual_bank.pending_list(),
                  decided=manual_bank.decided_list(),
                  business_names=names)


@router.post("/admin/bank/{pid}/approve")
async def admin_bank_approve(request: Request, pid: str, note: str = Form("")):
    """Approve a verified transfer. Idempotent: a second approval cannot
    re-activate or double-charge. Every approval is audited."""
    user = bank_guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from . import manual_bank
    try:
        row, decided = manual_bank.approve(pid, user, note)
    except PermissionError:
        return RedirectResponse("/admin/bank?err=not_authorized", status_code=303)
    except LookupError:
        return RedirectResponse("/admin/bank?err=not_found", status_code=303)
    if not decided:
        return RedirectResponse("/admin/bank?already=" + row["reference"], status_code=303)
    return RedirectResponse("/admin/bank?approved=" + row["reference"], status_code=303)


@router.post("/admin/bank/{pid}/reject")
async def admin_bank_reject(request: Request, pid: str, reason: str = Form("")):
    user = bank_guard(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from . import manual_bank
    try:
        row, decided = manual_bank.reject(pid, user, reason or "no reason recorded")
    except PermissionError:
        return RedirectResponse("/admin/bank?err=not_authorized", status_code=303)
    except LookupError:
        return RedirectResponse("/admin/bank?err=not_found", status_code=303)
    if not decided:
        return RedirectResponse("/admin/bank?already=" + row["reference"], status_code=303)
    return RedirectResponse("/admin/bank?rejected=" + row["reference"], status_code=303)
