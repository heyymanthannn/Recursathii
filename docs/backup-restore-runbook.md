# RecurSaathi backup and restore runbook (Phase 9.3, dev)

## What exists in code (app/backup.py)
- `snapshot_gz()`: gzip of the SQLite database file. Snapshot is taken via SQLite's backup API copy, so it is transaction-consistent even while the app is writing (no mid-write torn pages).
- `upload(name, enc)`: Fernet-encrypts the snapshot with `BACKUP_ENC_KEY` and uploads to the Supabase storage bucket `BACKUP_BUCKET`. Nothing unencrypted ever leaves the box.
- `run_once(tag)`: snapshot + upload + verify + `prune(keep=BACKUP_KEEP)`; called 45s after boot and on the backup schedule.
- `_snapshot_gz_pg()` / `restore_pg()`: pre-built Postgres equivalents for a future migration; unused today.

## Where backups live
Supabase storage bucket (env `BACKUP_BUCKET`), encrypted at rest with `BACKUP_ENC_KEY` (Fernet). Off-box by construction: production data lives in the Render disk, backups live in Supabase.

## RPO / RTO (first launch, SQLite, single instance)
- RPO: 24 hours (one scheduled backup per day) plus the boot-time snapshot. For first-customer volume (tens of businesses, hundreds of invoices) a 24h RPO is acceptable; tighten to 6h only if pilot customers report daily bulk edits.
- RTO: about 30 minutes (download latest object, decrypt, decompress, stop service, replace DB file, start service, smoke-test login + one invoice).

## Restore procedure (isolated test before every real restore)
1. `python3 -` with: download newest object from the bucket (Supabase storage API), `Fernet(BACKUP_ENC_KEY).decrypt(...)` -> `gzip.decompress` -> write to a temp file.
2. `sqlite3 tmp.sqlite "PRAGMA integrity_check;"` must return `ok`.
3. Open the temp DB, check the newest `invoices.created` timestamp to confirm which point in time the snapshot represents.
4. Only after integrity passes: stop the Render service, replace the DB, start, verify `/healthz` and one login.

## Verified evidence (dev, no production data touched)
- `tests/test_phase91_security.py` runs `backup.snapshot_gz()` offline, gunzips it, opens the result with SQLite and passes `PRAGMA integrity_check` (objects > 0). Snapshot -> valid-DB roundtrip is proven in the dev environment.

## Why SQLite stays for launch
First-customer volume is single-digit businesses per week, writes are human-scale (invoice edits, outbox inserts), and every write path uses parameterized queries with WAL-style short transactions. A Postgres move before launch would add a paid-tier cost (Supabase larger instance), a new failure surface, and zero user-visible benefit at this volume. Revisit at ~50 active businesses or first sign of `database is locked` in production logs. The migration path is already scaffolded (`_snapshot_gz_pg`, `restore_pg`), so the later move is a documented, rehearsed change, not a rewrite.
