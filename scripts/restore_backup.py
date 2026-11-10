#!/usr/bin/env python3
"""Restore test / restore tool (separate environment only; never run against prod).

Downloads the newest (or named) encrypted backup from Supabase Storage, decrypts
it, gunzips it, and verifies the SQLite file opens and the core tables hold
record counts. Credentials from env: SUPABASE_URL, SUPABASE_SERVICE_KEY,
BACKUP_BUCKET, BACKUP_ENC_KEY.

Usage: python scripts/restore_backup.py [--name FILENAME] [--out restored.db]
"""
import argparse, gzip, os, sqlite3, sys, tempfile
import httpx
from cryptography.fernet import Fernet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    bucket = os.environ["BACKUP_BUCKET"]
    f = Fernet(os.environ["BACKUP_ENC_KEY"].encode())

    if a.name:
        name = a.name
    else:
        r = httpx.post(f"{base}/storage/v1/object/list/{bucket}",
                       json={"prefix": "", "limit": 200,
                             "sortBy": {"column": "created_at", "order": "desc"}},
                       headers={"Authorization": f"Bearer {key}"}, timeout=60)
        r.raise_for_status()
        items = [x for x in (r.json() or []) if x.get("name")]
        if not items:
            print("FAIL: no backups found in bucket"); sys.exit(1)
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        name = items[0]["name"]
    print(f"restoring: {name}")

    d = httpx.get(f"{base}/storage/v1/object/{bucket}/{name}",
                  headers={"Authorization": f"Bearer {key}"}, timeout=120)
    d.raise_for_status()
    raw = f.decrypt(d.content)
    db_bytes = gzip.decompress(raw)

    tmp = a.out or tempfile.mktemp(suffix=".db")
    with open(tmp, "wb") as fh:
        fh.write(db_bytes)

    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    tables = [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    counts = {}
    for t in ("users", "customers", "invoices", "payments", "pay_events",
              "outbox", "activities", "audit", "plat_pays", "api_keys", "branches"):
        if t in tables:
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    con.close()
    print(f"decrypted + gunzipped OK ({len(db_bytes)} bytes)")
    print(f"sqlite opens OK; {len(tables)} tables")
    print("row counts:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    ok = all(t in tables for t in ("users", "customers", "invoices", "payments", "audit"))
    print("VERIFICATION:", "PASS" if ok else "FAIL: core tables missing")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
