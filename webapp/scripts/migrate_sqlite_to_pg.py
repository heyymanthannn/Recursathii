#!/usr/bin/env python3
"""Migrate a RecurSaathi SQLite database to PostgreSQL.

Exact-value migration: every row is copied with its SQLite value unchanged
(INTEGER amounts stay integers, unix-epoch REAL timestamps stay the same
floats). Runs inside ONE PostgreSQL transaction; if any per-table count or
row-set digest mismatches, the whole migration rolls back and the script
exits non-zero. Records the applied version in schema_migrations.

Usage:
  python3 scripts/migrate_sqlite_to_pg.py --sqlite data/payrecovery.db \
      --pg "postgresql://user:pass@host/db"
"""
import argparse, hashlib, json, os, sqlite3, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

p = argparse.ArgumentParser()
p.add_argument("--sqlite", required=True)
p.add_argument("--pg", required=True)
a = p.parse_args()

os.environ["PAYRECOVERY_DB"] = a.pg
from app import models, pgcompat  # noqa: E402  (after env)


def digest(rows):
    canon = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()[:16]


def main():
    src = sqlite3.connect(a.sqlite)
    src.row_factory = sqlite3.Row
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    ddl = {r[0]: r[1] for r in src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    models.init()  # creates the full PG schema (+ schema_migrations)
    c = pgcompat.conn()
    report, ok = {}, True
    try:
        pg_tables = {r["table_name"] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
        for t in tables:
            if t == "schema_migrations":
                continue
            if t not in pg_tables:
                c.execute(pgcompat.translate_ddl(ddl[t]))
        for t in tables:
            if t == "schema_migrations":
                continue
            rows = [dict(r) for r in src.execute(f'SELECT * FROM "{t}"')]
            if rows:
                cols = list(rows[0].keys())
                for r in rows:
                    c.execute(f'INSERT INTO "{t}" ({", ".join(cols)}) VALUES ({", ".join(["?"] * len(cols))})',
                              [r[k] for k in cols])
            report[t] = len(rows)
        # verify inside the same transaction, before commit
        for t in tables:
            if t == "schema_migrations":
                continue
            src_rows = [dict(r) for r in src.execute(f'SELECT * FROM "{t}"')]
            pg_rows = [dict(r) for r in c.execute(f'SELECT * FROM "{t}"').fetchall()]
            if len(src_rows) != len(pg_rows) or digest(src_rows) != digest(pg_rows):
                print(f"MISMATCH {t}: sqlite {len(src_rows)} rows ({digest(src_rows)}) vs pg {len(pg_rows)} rows ({digest(pg_rows)})")
                ok = False
        if not ok:
            raise SystemExit("verification failed; rolling back")
        c.execute("DELETE FROM schema_migrations WHERE version='0001-phase7.7-sqlite-to-pg'")
        c.execute("INSERT INTO schema_migrations(version, applied_at) VALUES('0001-phase7.7-sqlite-to-pg', ?)",
                  (time.time(),))
        c.commit()
    except SystemExit:
        c.rollback(); raise
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    total = sum(report.values())
    print(f"MIGRATION OK: {len(tables)} tables, {total} rows, all row-set digests identical")
    for t, n in sorted(report.items()):
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
