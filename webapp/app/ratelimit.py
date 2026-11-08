"""Simple in-memory sliding-window rate limiter for auth endpoints.

Single-process (uvicorn single worker on Render), so a module dict is enough.
Resets on deploy - acceptable for abuse throttling, not for billing.
"""
import time
from collections import defaultdict

_hits: dict[str, list[float]] = defaultdict(list)


def allow(key: str, limit: int, window_s: float) -> bool:
    now = time.time()
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
    q = _hits.get(key, [])
    if len(q) < limit:
        return 0
    return max(1, int(q[0] + window_s - now) + 1)
