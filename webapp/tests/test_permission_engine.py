"""Permission engine + payment-status tests (hybrid AI architecture, Phase 1).

Run:  cd webapp && python3 -m tests.test_permission_engine
Isolated temp DB. No network, no real providers.
"""
import os, sys, tempfile, time, types

os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_perm_", suffix=".sqlite")
os.environ["MANUAL_UPI_ID"] = "testpay@upi"
os.environ.pop("RAZORPAY_KEY_ID", None)
os.environ.pop("RAZORPAY_KEY_SECRET", None)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from types import SimpleNamespace
from app import models, engine, permissions, billing  # noqa: E402
from app import db as _db  # noqa: E402

models.init()
_db.init()
results = []

def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL") + " " + name + ((" | " + detail) if detail else ""))

_u = models.create_user("permowner@example.com", "x", "Perm Owner", "Perm Coaching", "+919000000000", role="owner")
OWNER = _u["id"]
permissions.set_config(OWNER, {"automations_enabled": True})

# ---- 1. Level A is fully automatic ----
r = permissions.resolve(OWNER, "generate_reminder_draft")
record("levelA auto", r["decision"] == "auto" and r["level"] == "A", r["reason"])

# ---- 2. Level C needs approval ----
r = permissions.resolve(OWNER, "issue_refund")
record("levelC needs_approval", r["decision"] == "needs_approval", r["reason"])

# ---- 3. Hard-denied actions are always denied ----
r = permissions.resolve(OWNER, "ai_initiate_refund")
record("hard-denied refund", r["decision"] == "denied", r["reason"])
r = permissions.resolve(OWNER, "override_verified_payment_status")
record("hard-denied override", r["decision"] == "denied", r["reason"])

# ---- 4. Level B blocked until the business authorizes the channel ----
cu = models.create_customer(OWNER, "Ravi Kumar", email="ravi@example.com", phone="+919000000001")
r = permissions.resolve(OWNER, "send_routine_email_reminder", channel="email", customer=cu)
record("levelB unauthorized channel", r["decision"] == "needs_approval", r["reason"])

# authorize -> auto
permissions.authorize_channel(OWNER, "email")
permissions.authorize_channel(OWNER, "whatsapp")
cu = models.get_customer(cu["id"])
r = permissions.resolve(OWNER, "send_routine_email_reminder", channel="email", customer=cu)
record("levelB authorized auto", r["decision"] == "auto", r["reason"])

# ---- 5. Opt-out is a hard stop for that channel ----
models.update_customer(cu["id"], email_opt_out=1)
cu = models.get_customer(cu["id"])
r = permissions.resolve(OWNER, "send_routine_email_reminder", channel="email", customer=cu)
record("opt-out denied", r["decision"] == "denied", r["reason"])
models.ex("UPDATE customers SET email_opt_out=0 WHERE id=?", (cu["id"],))

# ---- 6. Quiet hours defer sends ----
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI
h = _dt.now(_ZI("Asia/Kolkata")).hour  # a window starting this hour contains 'now' in IST
window = f"{h:02d}:00"
permissions.set_config(OWNER, {"automations_enabled": True})
permissions.set_config(OWNER, {"quiet_hours": {"enabled": True, "start": window,
                                               "end": f"{(h+1)%24:02d}:59", "tz": "Asia/Kolkata"}})
r = permissions.resolve(OWNER, "send_routine_whatsapp_reminder", channel="whatsapp", customer=cu)
record("quiet hours deferred", r["decision"] == "deferred", r["reason"])
permissions.set_config(OWNER, {"quiet_hours": {"enabled": False}})
r = permissions.resolve(OWNER, "send_routine_whatsapp_reminder", channel="whatsapp", customer=cu)
record("quiet hours disabled auto", r["decision"] == "auto", r["reason"])

# ---- 7. Per-customer daily cap defers ----
cu = models.get_customer(cu["id"])
permissions.set_config(OWNER, {"max_per_customer_per_day": 1})
models.queue(OWNER, "email", cu["email"], "initial", "s", "b", dedupe="permtest-1")
r = permissions.resolve(OWNER, "send_routine_email_reminder", channel="email", customer=cu)
record("per-customer cap deferred", r["decision"] == "deferred", r["reason"])
permissions.set_config(OWNER, {"max_per_customer_per_day": 2})

# ---- 8. Emergency pause holds Level B ----
permissions.set_emergency_pause(OWNER, True)
r = permissions.resolve(OWNER, "send_routine_email_reminder", channel="email", customer=cu)
record("emergency pause", r["decision"] == "paused", r["reason"])
permissions.set_emergency_pause(OWNER, False)

# ---- 9. Approval Required mode holds Level B ----
models.set_automation_mode(OWNER, "approval")
r = permissions.resolve(OWNER, "send_routine_whatsapp_reminder", channel="whatsapp", customer=cu)
record("approval mode holds B", r["decision"] == "needs_approval", r["reason"])
models.set_automation_mode(OWNER, "auto")

# ---- 10. Unknown action is denied, not executed ----
r = permissions.resolve(OWNER, "totally_unknown_action")
record("unknown denied", r["decision"] == "denied", r["reason"])

# ---- 11. Duplicate webhook events create one case, not two ----
billing.MERCHANTS["m-perm"] = SimpleNamespace(id="m-perm", status="active")
e1 = engine.ingest_normal(event_id="evt-dup-1", merchant_id="m-perm", gateway="razorpay",
                          customer_name="Ravi Kumar", customer_phone="+919000000001",
                          amount=49900, decline_code="insufficient_funds", mandate_id=None)
e2 = engine.ingest_normal(event_id="evt-dup-1", merchant_id="m-perm", gateway="razorpay",
                          customer_name="Ravi Kumar", customer_phone="+919000000001",
                          amount=49900, decline_code="insufficient_funds", mandate_id=None)
from app.db import get_event
n_copies = 1 if get_event("evt-dup-1") else 0
record("one case per event", e1 is not None and e2 is None and n_copies == 1,
       f"first={e1 is not None} dup={e2 is None}")

# ---- 12. Pending never settles; unverified success refused; verified success stops reminders ----
r = engine.set_payment_status("evt-dup-1", "pending")
row = get_event("evt-dup-1")
record("pending is not success", r["ok"] and row["status"] == "pending", r.get("reason", ""))
r = engine.set_payment_status("evt-dup-1", "succeeded")   # no verified flag
record("unverified success refused", not r["ok"], r.get("reason", ""))
row = get_event("evt-dup-1")
record("still not recovered", row["recovered"] == 0, str(row["recovered"]))
r = engine.set_payment_status("evt-dup-1", "succeeded", verified=True)
row = get_event("evt-dup-1")
record("verified success recovers", r["ok"] and row["recovered"] == 1 and row["status"] == "succeeded")
fired = engine.run_scheduler()
record("reminders stopped after payment", fired == 0, f"fired={fired}")
r = engine.set_payment_status("evt-dup-1", "pending")
record("verified status cannot be overridden", not r["ok"], r.get("reason", ""))

# ---- 13. Every resolution is audited ----
n = models.one("SELECT COUNT(*) AS n FROM audit WHERE user_id=? AND action='automation_permission'",
               (OWNER,))
record("permission resolutions audited", int((n or {}).get("n", 0)) >= 10,
       f"entries={(n or {}).get('n', 0)}")

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
