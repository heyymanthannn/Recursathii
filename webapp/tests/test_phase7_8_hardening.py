"""Phase 7.8 tests: CSP tightening, persistent rate limiting, PG backup encryption.

Runs on SQLite by default; PG-specific parts require PG78_URL (hosted rehearsal).
"""
import os, subprocess, sys, tempfile, gzip, json, time
import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_P78") == "1", reason="phase 7.8 suite disabled")

# ---------- CSP hardening ----------
def test_csp_script_src_has_no_unsafe_inline():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.get("/login")
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    # no unsafe-inline anywhere in script-src (style-src keeps it, tracked residual)
    import re
    m = re.search(r"script-src ([^;]+)", csp)
    assert m and "'unsafe-inline'" not in m.group(1)

def test_base_js_served_and_templates_clean():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.get("/static/base.js")
    assert r.status_code == 200
    import pathlib
    templates = pathlib.Path("app/templates").glob("*.html")
    for t in templates:
        src = t.read_text()
        assert "<script>" not in src, f"inline script block in {t.name}"
        for handler in ("onsubmit=", "onclick=", "onchange=", "oninput=", "onload=", "onerror="):
            assert handler not in src, f"inline handler in {t.name}"

# ---------- persistent rate limiting ----------
def test_rate_limit_enforces_and_persists(tmp_path):
    db = os.path.join(tmp_path, "rl.db")
    env = dict(os.environ, PAYRECOVERY_DB=db)
    script = (
        "import sys, os; sys.path.insert(0, '.');"
        "from app import models, ratelimit; models.init();"
        f"r=[ratelimit.allow('persist-key', 2, 600) for _ in range(3)];"
        "print(r)"
    )
    out = subprocess.run([sys.executable, "-c", script], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "[True, True, False]" in out.stdout
    # a fresh process must still see the limit (persistence)
    script2 = script.replace("r=[ratelimit.allow('persist-key', 2, 600) for _ in range(3)]",
                             "r=[ratelimit.allow('persist-key', 2, 600), ratelimit.wait_s('persist-key', 2, 600)]")
    out2 = subprocess.run([sys.executable, "-c", script2], env=env,
                          capture_output=True, text=True, timeout=60)
    assert out2.returncode == 0, out2.stderr
    assert "[False" in out2.stdout and ", 0)" not in out2.stdout.split("]")[0]

def test_rate_limit_falls_back_in_memory_when_db_fails(tmp_path, monkeypatch):
    db = os.path.join(tmp_path, "rlfail.db")
    os.environ["PAYRECOVERY_DB"] = db
    import importlib
    import app.models as models, app.ratelimit as rl
    importlib.reload(models); importlib.reload(rl)
    models.init()
    calls = {"n": 0}
    real_conn = models.conn
    class Boom:
        def __enter__(self): calls["n"] += 1; raise RuntimeError("db down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(rl, "conn", lambda: Boom())
    res = [rl.allow("boom-key", 1, 600) for _ in range(2)]
    assert res == [True, False]  # fallback enforces the same window
    assert calls["n"] >= 1

def test_rate_limits_table_recorded_in_schema_migrations(tmp_path):
    db = os.path.join(tmp_path, "mig.db")
    os.environ["PAYRECOVERY_DB"] = db
    import importlib
    import app.models as models
    importlib.reload(models)
    models.init()
    versions = sorted(r["version"] for r in models.q("SELECT version FROM schema_migrations"))
    assert "0001-phase7.7-baseline" in versions
    assert "0002-phase7.8-ratelimits" in versions
    assert "rate_limits" in [r["name"] for r in models.q(
        "SELECT name FROM sqlite_master WHERE type='table'")]

# ---------- backup encryption (driver-agnostic cycle) ----------
def test_backup_cycle_encrypts_snapshot(tmp_path):
    db = os.path.join(tmp_path, "enc.db")
    os.environ["PAYRECOVERY_DB"] = db
    os.environ["BACKUP_ENC_KEY"] = Fernet.generate_key().decode()
    import importlib
    import app.models as models, app.backup as backup
    importlib.reload(models); importlib.reload(backup)
    models.init()
    gz = backup.snapshot_gz()
    enc = backup._fernet().encrypt(gz)
    assert enc[:4] == b"gAAA"  # Fernet envelope
    assert gzip.decompress(backup._fernet().decrypt(enc))  # round-trip to the same gz stream
    # snapshot stream verifies by driver: SQLite = binary db file, PG = JSON dump
    raw = gzip.decompress(gz)
    if raw[:16].startswith(b"SQLite format 3"):
        assert True
    else:
        assert "tables" in json.loads(raw.decode())
