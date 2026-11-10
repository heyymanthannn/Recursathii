"""Database safety: encrypted off-box backups of the SQLite database.

Runs automatically: once shortly after every deploy (boot) and on a schedule
(every 6 hours) from the background worker. Uses SQLite's online backup API for
a consistent snapshot, gzips it, encrypts it with Fernet (AES-128-CBC + HMAC),
and uploads to Supabase Storage. Keeps the newest BACKUP_KEEP versions off-box.
Credentials come only from environment variables and never live in the repo:
SUPABASE_URL, SUPABASE_SERVICE_KEY, BACKUP_BUCKET, BACKUP_ENC_KEY.
"""
import gzip, os, sqlite3, tempfile, time
import httpx
from . import db

BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))
BACKUP_INTERVAL_S = int(os.environ.get("BACKUP_INTERVAL_S", str(6 * 3600)))


def _configured():
    return all(os.environ.get(k) for k in
               ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "BACKUP_BUCKET", "BACKUP_ENC_KEY"))


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(os.environ["BACKUP_ENC_KEY"].encode())


def snapshot_gz() -> bytes:
    """Consistent snapshot of the live SQLite db via the online backup API."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        src = sqlite3.connect(db.DB_PATH)
        dst = sqlite3.connect(tmp.name)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with open(tmp.name, "rb") as f:
            raw = f.read()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return gzip.compress(raw)


def _sb():
    return os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_KEY"]


def upload(name: str, enc: bytes) -> str:
    base, key = _sb()
    r = httpx.post(f"{base}/storage/v1/object/{os.environ['BACKUP_BUCKET']}/{name}",
                   content=enc, headers={"Authorization": f"Bearer {key}",
                                         "Content-Type": "application/octet-stream",
                                         "x-upsert": "true"}, timeout=60)
    if r.status_code >= 300:
        return f"error: upload {r.status_code} {r.text[:200]}"
    return "ok"


def list_backups() -> list:
    base, key = _sb()
    r = httpx.post(f"{base}/storage/v1/object/list/{os.environ['BACKUP_BUCKET']}",
                   json={"prefix": "", "limit": 200,
                         "sortBy": {"column": "created_at", "order": "desc"}},
                   headers={"Authorization": f"Bearer {key}"}, timeout=60)
    if r.status_code >= 300:
        return []
    return r.json() or []


def prune(keep: int = BACKUP_KEEP) -> int:
    base, key = _sb()
    items = sorted(list_backups(), key=lambda x: x.get("created_at", ""), reverse=True)
    removed = 0
    for old in items[keep:]:
        name = old.get("name", "")
        if not name:
            continue
        d = httpx.delete(f"{base}/storage/v1/object/{os.environ['BACKUP_BUCKET']}/{name}",
                         headers={"Authorization": f"Bearer {key}"}, timeout=60)
        if d.status_code < 300:
            removed += 1
    return removed


def run_once(tag: str = "sched") -> str:
    """Full cycle: snapshot, encrypt, upload, prune. Returns a status line."""
    if not _configured():
        return "skipped: backup storage not configured"
    try:
        gz = snapshot_gz()
        enc = _fernet().encrypt(gz)
        name = f"recursaathi-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{tag}.sqlite3.gz.enc"
        st = upload(name, enc)
        if st != "ok":
            return f"error: {st}"
        removed = prune()
        return f"ok: {name} ({len(enc)} bytes encrypted, {len(gz)} bytes gzipped, {removed} pruned)"
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
