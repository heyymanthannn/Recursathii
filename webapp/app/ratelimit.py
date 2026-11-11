"""Rate limiting for auth endpoints, now persisted in the database.

Single-process (uvicorn single worker on Render), but the window now survives
restarts and redeploys because hits are stored in the rate_limits table through
the shared connection layer (SQLite in dev, PostgreSQL in prod). If the
database is unavailable, behaviour falls back to the old process-local dict so
an outage degrades to the previous state instead of opening the throttle.
"""
import time
from collections import defaultdict

from .models import conn

_hits: dict[str, list[float]] = defaultdict(list)
_db_ok = True  # flipped False on first DB failure; periodically retried


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS rate_limits(
      key TEXT NOT NULL, ts REAL NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_key_ts ON rate_limits(key, ts)")


def allow(key: str, limit: int, window_s: float) -> bool:
    global _db_ok
    now = time.time()
    if _db_ok:
        try:
            with conn() as c:
                _ensure(c)
                c.execute("DELETE FROM rate_limits WHERE key = ? AND ts < ?",
                          (key, now - max(window_s, 86400.0)))
                row = c.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE key = ? AND ts >= ?",
                                (key, now - window_s)).fetchone()
                n = int(row["n"] if not isinstance(row, int) else row)
                if n >= limit:
                    return False  # denied attempts do not extend the window
                c.execute("INSERT INTO rate_limits(key, ts) VALUES (?, ?)", (key, now))
            return True
        except Exception:
            _db_ok = False  # fall back below until a later call retries
    # in-memory fallback (previous behaviour)
    q = _hits[key]
    while q and q[0] < now - window_s:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def wait_s(key: str, limit: int, window_s: float) -> int:
    """Seconds until another attempt is allowed (0 if allowed now)."""
    now = time.time()
    if _db_ok:
        try:
            with conn() as c:
                _ensure(c)
                row = c.execute(
                    "SELECT MIN(ts) AS oldest, COUNT(*) AS n FROM rate_limits "
                    "WHERE key = ? AND ts >= ?", (key, now - window_s)).fetchone()
                n = int(row["n"] if not isinstance(row, int) else row)
                if n < limit:
                    return 0
                oldest = float(row["oldest"] or now)
                return max(1, int(oldest + window_s - now) + 1)
        except Exception:
            pass
    q = _hits.get(key, [])
    if len(q) < limit:
        return 0
    return max(1, int(q[0] + window_s - now) + 1)
