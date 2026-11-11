"""Sessions and password hashing (stdlib + itsdangerous cookie signing)."""
import hashlib, os
from itsdangerous import URLSafeTimedSerializer, BadSignature
from . import models, vault

SESSION_COOKIE = "rs_session"
SESSION_TTL = 30 * 86400
_s = URLSafeTimedSerializer(vault.SECRET, salt="rs-session")

def hash_pw(pw: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"

def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, want = stored.split("$", 1)
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
        return hmac.compare_digest(got, want)
    except Exception:
        return False

import hmac  # noqa: E402  (used by check_pw)

def _epoch(user_id: str) -> int:
    u = models.get_user(user_id)
    try:
        return int((u or {}).get("session_epoch") or 0)
    except Exception:
        return 0

def start_session(response, user_id: str):
    # session epoch: bumped on every password change, so all cookies issued
    # before a rotation stop validating (server-side revocation without a
    # session store; pre-epoch cookies stay valid only for epoch-0 accounts)
    response.set_cookie(SESSION_COOKIE, _s.dumps({"u": user_id, "e": _epoch(user_id)}),
                        httponly=True, samesite="lax", max_age=SESSION_TTL)
    # Secure flag is added per-request by the security middleware when the
    # connection is actually https (incl. X-Forwarded-Proto behind Render),
    # so localhost dev and the http test client keep working.

def end_session(response):
    response.delete_cookie(SESSION_COOKIE)

def current_user(request):
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        payload = _s.loads(cookie, max_age=SESSION_TTL)
    except BadSignature:
        return None
    if isinstance(payload, dict):
        user_id, cookie_epoch = str(payload.get("u") or ""), int(payload.get("e") or 0)
    else:
        user_id, cookie_epoch = str(payload), 0
    user = models.get_user(user_id)
    if not user or int(user.get("session_epoch") or 0) != cookie_epoch:
        return None
    return user
