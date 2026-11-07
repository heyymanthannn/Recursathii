"""Customer payment portal: public pages where customers view and pay their
outstanding invoices without any business-owner account."""
import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from . import models, payments, notify, automations as auto, config

router = APIRouter()

def render(name, request, **ctx):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    env.filters["inr"] = lambda p: f"\u20b9{p/100:,.0f}"
    env.filters["datetime"] = lambda ts: (time.strftime("%d %b %Y, %H:%M", time.localtime(ts)) if ts else "-")
    env.globals["inr"] = env.filters["inr"]
    env.globals["datetime"] = env.filters["datetime"]
    return env.get_template(name).render(request=request, **ctx)

def base_url(request):
    return str(request.base_url).rstrip("/")

def _invoice_ctx(tok):
    inv = models.get_invoice_by_token(tok)
    if not inv:
        return None
    cu = models.get_customer(inv["customer_id"])
    owner = models.get_user(inv["owner_id"])
    pays = models.q("SELECT * FROM payments WHERE invoice_id=? ORDER BY created DESC", (inv["id"],))
    return inv, cu, owner, pays

@router.get("/invoice/{tok}", response_class=HTMLResponse)
async def pay_page(request: Request, tok: str):
    ctx = _invoice_ctx(tok)
    if not ctx:
        return render("portal_404.html", request)
    inv, cu, owner, pays = ctx
    creds = payments.owner_creds(owner)
    rzp_link = None
    if creds and inv["status"] in ("pending", "overdue"):
        r = payments.create_payment_link(creds, inv, cu, base_url(request))
        rzp_link = r.get("short_url") if not r.get("error") else None
    vpa = owner.get("upi_vpa") or ""
    return render("portal_pay.html", request, inv=inv, cu=cu, owner=owner, pays=pays,
                  rzp_link=rzp_link, vpa=vpa, payee=owner.get("payee_name") or owner.get("business"),
                  has_rzp=bool(creds), qr=f"/portal/qr/{tok}.png")

@router.get("/portal/qr/{tok}.png")
async def portal_qr(request: Request, tok: str):
    import qrcode, io as _io
    ctx = _invoice_ctx(tok)
    if not ctx:
        return Response(status_code=404)
    inv, cu, owner, pays = ctx
    vpa = owner.get("upi_vpa") or ""
    if not vpa:
        return Response(status_code=404)
    payee = (owner.get("payee_name") or owner.get("business") or "").replace(" ", "%20")
    uri = f"upi://pay?pa={vpa}&pn={payee}&am={inv['amount']/100:.2f}&cu=INR&tn=Invoice%20{inv['number']}"
    img = qrcode.make(uri)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

@router.post("/invoice/{tok}/sent")
async def pay_sent(request: Request, tok: str, reference: str = Form("")):
    """Customer says they paid by UPI. Records the claim as pending; the
    business owner confirms receipt in their dashboard. Nothing is faked."""
    ctx = _invoice_ctx(tok)
    if not ctx:
        return render("portal_404.html", request)
    inv, cu, owner, pays = ctx
    if inv["status"] != "paid":
        p = models.record_payment_attempt(inv, "upi", status="pending", reference=reference,
                                          note="customer reported UPI payment, awaiting business confirmation")
        models.log_activity(owner["id"], "payment",
                            f"{cu['name']} reported a UPI payment of {inv['amount']/100:,.0f} for invoice {inv['number']} (pending confirmation)")
        models.log_audit(owner["id"], "payment.customer_claim", inv["number"])
    return RedirectResponse(f"/invoice/{tok}?sent=1", status_code=303)

@router.get("/receipt/{payment_id}", response_class=HTMLResponse)
async def receipt(request: Request, payment_id: str):
    pay = models.one("SELECT * FROM payments WHERE id=? AND status='confirmed'", (payment_id,))
    if not pay:
        return render("portal_404.html", request)
    inv = models.get_invoice(pay["invoice_id"]) if pay["invoice_id"] else None
    cu = models.get_customer(pay["customer_id"]) if pay["customer_id"] else None
    owner = models.get_user(pay["owner_id"]) if pay["owner_id"] else None
    return render("receipt.html", request, pay=pay, inv=inv, cu=cu, owner=owner)

@router.post("/portal/opt-out")
async def opt_out(request: Request, phone: str = Form("")):
    if phone:
        models.ex("UPDATE customers SET wa_opt_out=1 WHERE phone=?", (phone.strip(),))
        return render("optout_done.html", request)
    return render("optout_done.html", request, error="Enter the phone number you want to opt out.")
