"""Sessions and password hashing (stdlib + itsdangerous cookie signing)."""
import hashlib, os, secrets, time
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

def start_session(response, user_id: str, ttl: int | None = None):
    # session epoch: bumped on every password change, so all cookies issued
    # before a rotation stop validating (server-side revocation without a
    # session store; pre-epoch cookies stay valid only for epoch-0 accounts)
    # since 0003-user-sessions: every session also gets a server-side row, so
    # logout revokes the exact token (a copied token dies immediately).
    ttl = ttl or SESSION_TTL
    sid = secrets.token_urlsafe(32)
    models.ex("INSERT INTO user_sessions(sid, user_id, expires_at, revoked, created_at) VALUES(?,?,?,0,?)",
              (sid, user_id, time.time() + ttl, time.time()))
    models.ex("DELETE FROM user_sessions WHERE expires_at < ?", (time.time() - 86400,))
    response.set_cookie(SESSION_COOKIE, _s.dumps({"u": user_id, "e": _epoch(user_id), "sid": sid}),
                        httponly=True, samesite="lax", max_age=ttl)
    # Secure flag is added per-request by the security middleware when the
    # connection is actually https (incl. X-Forwarded-Proto behind Render),
    # so localhost dev and the http test client keep working.

def end_session(response):
    response.delete_cookie(SESSION_COOKIE)

def revoke_session(request) -> bool:
    """Mark the cookie's server-side session row revoked. Logout calls this."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        payload = _s.loads(cookie, max_age=SESSION_TTL)
    except BadSignature:
        return False
    sid = str(payload.get("sid") or "") if isinstance(payload, dict) else ""
    if not sid:
        return False
    models.ex("UPDATE user_sessions SET revoked=1 WHERE sid=? AND revoked=0", (sid,))
    return True

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
        sid = str(payload.get("sid") or "")
    else:
        user_id, cookie_epoch, sid = str(payload), 0, ""
    if not sid:
        return None  # pre-revocation cookies carry no session id: reject
    row = models.one("SELECT user_id, expires_at, revoked FROM user_sessions WHERE sid=?", (sid,))
    if not row or row["user_id"] != user_id or int(row.get("revoked") or 0) \
            or float(row["expires_at"]) < time.time():
        return None
    user = models.get_user(user_id)
    if not user or int(user.get("session_epoch") or 0) != cookie_epoch:
        return None
    return user
