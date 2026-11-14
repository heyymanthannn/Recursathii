"""Light symmetric encryption for stored gateway credentials.

HMAC-CTR keystream XOR with a random 16-byte prefix per value, keyed on APP_SECRET.
Keeps Razorpay keys out of plaintext in the database file. APP_SECRET must be set
in production; the dev default exists only so tests run.
"""
import base64, hashlib, hmac, os

SECRET = os.environ.get("APP_SECRET", "dev-secret-change-me").encode()

def _keystream(nonce, n):
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(SECRET, nonce + str(counter).encode(), hashlib.sha256).digest()
        counter += 1
    return out[:n]

def encrypt(s: str) -> str:
    if not s:
        return ""
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(s.encode(), _keystream(nonce, len(s))))
    return base64.b64encode(nonce + ct).decode()

def decrypt(b64: str) -> str:
    if not b64:
        return ""
    raw = base64.b64decode(b64)
    nonce, ct = raw[:16], raw[16:]
    return bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct)))).decode()


def require_production_secrets(env=None):
    """Fail fast when a production-like process would boot without the secrets
    the platform relies on. Dev and test runs are unaffected: the check only
    engages when a production marker is present, and it never touches the
    existing dev fallback used by the test suites."""
    env = env if env is not None else os.environ
    if not (env.get("RENDER") or env.get("RENDER_EXTERNAL_URL")):
        return []
    missing = [k for k in ("APP_SECRET", "BACKUP_ENC_KEY", "BILLING_WEBHOOK_SECRET",
                           "YOUPI_WEBHOOK_SECRET") if not (env.get(k) or "").strip()]
    if missing:
        raise RuntimeError("missing required secrets in production: " + ", ".join(missing)
                           + " (set them in the Render environment before deploying)")
    app_secret = (env.get("APP_SECRET") or "").strip()
    if len(app_secret) < 32:
        raise RuntimeError("APP_SECRET is too short for production (need at least 32 chars); "
                           "rotation must follow the vault re-encryption runbook")
    weak = (app_secret.lower() in ("dev-secret-change-me", "secret", "changeme", "change-me")
            or app_secret.lower().startswith("test"))
    if weak:
        raise RuntimeError("APP_SECRET looks like a development default; refusing to boot in production")
    warnings = [k for k in ("PROVIDER_STATUS_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
                            "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "BACKUP_BUCKET",
                            "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RZP_WEBHOOK_SECRET",
                            "RZP_PLAN_STARTER", "RZP_PLAN_GROWTH", "RZP_PLAN_SCALE")
                if not (env.get(k) or "").strip()]
    if warnings:
        print("[startup] not configured (feature stays off until set): " + ", ".join(warnings), flush=True)
    return warnings
