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

def start_session(response, user_id: str):
    response.set_cookie(SESSION_COOKIE, _s.dumps(user_id),
                        httponly=True, samesite="lax", max_age=SESSION_TTL)

def end_session(response):
    response.delete_cookie(SESSION_COOKIE)

def current_user(request):
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        user_id = _s.loads(cookie, max_age=SESSION_TTL)
    except BadSignature:
        return None
    return models.get_user(user_id)
