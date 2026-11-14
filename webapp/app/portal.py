"""Customer payment portal: public pages where customers view and pay their
outstanding invoices without any business-owner account."""
import time
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from . import models, payments, notify, automations as auto, config
from .ratelimit import allow as rl_allow

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

@router.post("/invoice/{tok}/razorpay-order")
async def invoice_razorpay_order(tok: str):
    """Create the Razorpay order for paying this invoice. Money settles to the
    invoice owner's OWN Razorpay account (never the platform's), and the
    server-side binding is stored before Checkout.js ever loads."""
    from . import checkout as _chk
    inv = models.get_invoice_by_token(tok)
    if not inv:
        raise HTTPException(status_code=404, detail="invoice not found")
    if inv["status"] == "paid":
        raise HTTPException(status_code=409, detail="invoice already paid")
    owner = models.get_user(inv["owner_id"])
    creds = payments.owner_creds(owner) if owner else None
    if not creds:
        raise HTTPException(status_code=503, detail="razorpay is not configured for this business")
    out = _chk.create_order(creds, purpose="invoice", ref=inv["token"], amount=inv["amount"],
                            owner_id=inv["owner_id"], receipt=inv["number"],
                            notes={"invoice_id": inv["id"]})
    if not out.get("ok"):
        raise HTTPException(status_code=502, detail=out.get("error") or "order creation failed")
    return out


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


# ---------- customer portal accounts (separate login, strict isolation) ----------
import hashlib, os
from itsdangerous import URLSafeTimedSerializer, BadSignature
from . import vault as _vault

PORTAL_COOKIE = "rs_portal_session"
PORTAL_TTL = 14 * 86400
_ps = URLSafeTimedSerializer(_vault.SECRET, salt="rs-portal-session")


def _portal_hash(pw: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def _portal_check(pw: str, stored: str) -> bool:
    try:
        salt, want = stored.split("$", 1)
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
        return got == want
    except Exception:
        return False


def portal_customer(request):
    """The logged-in portal customer's email (lowercased), or None."""
    cookie = request.cookies.get(PORTAL_COOKIE)
    if not cookie:
        return None
    try:
        return _ps.loads(cookie, max_age=PORTAL_TTL)
    except BadSignature:
        return None


def _portal_session(response, email: str):
    response.set_cookie(PORTAL_COOKIE, _ps.dumps(email.lower()),
                        httponly=True, samesite="lax", max_age=PORTAL_TTL,
                        secure=(config.BASE_URL or "").startswith("https"))


def _customer_invoices(email):
    """Only this customer's invoices, matched on their email. Nothing else."""
    rows = models.q(
        """SELECT i.*, o.business AS business_name, o.name AS owner_name,
                  o.upi_vpa AS owner_vpa, o.payee_name AS owner_payee
           FROM invoices i JOIN customers cu ON cu.id=i.customer_id
           JOIN users o ON o.id=i.owner_id
           WHERE LOWER(cu.email)=? ORDER BY i.created DESC""", (email.lower(),))
    return [dict(r) for r in rows]


@router.get("/portal", response_class=HTMLResponse)
async def portal_login_page(request: Request):
    email = portal_customer(request)
    if email:
        return RedirectResponse("/portal/home", status_code=303)
    return render("portal_login.html", request, err="",
                  created=request.query_params.get("created") == "1")


@router.post("/portal/login")
async def portal_login(request: Request, email: str = Form(""), password: str = Form("")):
    _ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    _rk = "portal_login:" + _ip + ":" + (email or "").lower()
    if not rl_allow(_rk, 10, 600):
        return render("portal_login.html", request, err="Too many attempts. Try again in a few minutes.", created=False)
    email = (email or "").strip().lower()
    row = models.one("SELECT * FROM portal_users WHERE email=?", (email,))
    if not row or not _portal_check(password or "", row["pw_hash"]):
        return render("portal_login.html", request, err="Wrong email or password.", created=False)
    resp = RedirectResponse("/portal/home", status_code=303)
    _portal_session(resp, email)
    return resp


@router.post("/portal/register")
async def portal_register(request: Request, email: str = Form(""), password: str = Form("")):
    _ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    _rk = "portal_register:" + _ip + ":" + (email or "").lower()
    if not rl_allow(_rk, 10, 600):
        return render("portal_login.html", request, err="Too many attempts. Try again in a few minutes.", created=False)
    """Set a portal password for this email. Only allowed when at least one
    invoice already exists for that email (they got a link from a business)."""
    email = (email or "").strip().lower()
    if len(password or "") < 8:
        return render("portal_login.html", request,
                      err="Password needs at least 8 characters.", created=False)
    has_inv = models.one(
        "SELECT 1 AS ok FROM invoices i JOIN customers cu ON cu.id=i.customer_id "
        "WHERE LOWER(cu.email)=?", (email,))
    if not has_inv:
        return render("portal_login.html", request,
                      err="No invoices found for this email yet. Ask the business to send one.", created=False)
    if models.one("SELECT 1 AS ok FROM portal_users WHERE email=?", (email,)):
        return render("portal_login.html", request,
                      err="Account exists. Log in instead.", created=False)
    models.ex("INSERT INTO portal_users(id, email, pw_hash, created) VALUES(?,?,?,?)",
              (models.uid(), email, _portal_hash(password), models.now()))
    models.log_audit("", "portal.register", email)
    resp = RedirectResponse("/portal/home", status_code=303)
    _portal_session(resp, email)
    return resp


@router.get("/portal/logout")
async def portal_logout():
    resp = RedirectResponse("/portal", status_code=303)
    resp.delete_cookie(PORTAL_COOKIE)
    return resp


@router.get("/portal/home", response_class=HTMLResponse)
async def portal_home(request: Request):
    email = portal_customer(request)
    if not email:
        return RedirectResponse("/portal", status_code=303)
    invs = _customer_invoices(email)
    pays = models.q(
        """SELECT p.*, i.number AS inv_number, i.id AS inv_id FROM payments p
           JOIN invoices i ON i.id=p.invoice_id
           JOIN customers cu ON cu.id=p.customer_id
           WHERE LOWER(cu.email)=? ORDER BY p.created DESC LIMIT 50""", (email,))
    return render("portal_home.html", request, invs=invs, pays=pays, email=email)


@router.get("/portal/invoice/{iid}/pdf")
async def portal_invoice_pdf(request: Request, iid: str):
    email = portal_customer(request)
    if not email:
        return RedirectResponse("/portal", status_code=303)
    inv = models.one(
        """SELECT i.* FROM invoices i JOIN customers cu ON cu.id=i.customer_id
           WHERE i.id=? AND LOWER(cu.email)=?""", (iid, email.lower()))
    if not inv:
        return Response(status_code=404)
    from . import pdfgen
    inv = dict(inv)
    cu = models.get_customer(inv["customer_id"])
    owner = models.get_user(inv["owner_id"])
    pdf = pdfgen.invoice_pdf_bytes(inv, cu, owner, pay_url="")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{inv["number"]}.pdf"'})
