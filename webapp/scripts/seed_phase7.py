"""Phase 7 synthetic-business seeder (development only).

Creates three fully isolated synthetic businesses with owner accounts, one
staff member each, and synthetic customers/invoices/payment events. Nothing
here touches production data: run it against a dev database only.

Usage:  cd webapp && PYTHONPATH=. python3 scripts/seed_phase7.py
Idempotent: businesses are keyed by email, so re-running updates rather than
duplicates. Writes data/phase7_seed.json with the created account ids.
"""
import json, os, sys, time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app import models, auth, sequences  # noqa: E402

BUSINESSES = [
    {"email": "owner1@synthetic.test", "name": "Asha Verma", "business": "Bloom Yoga Studio",
     "phone": "9000000001", "plan": "starter"},
    {"email": "owner2@synthetic.test", "name": "Rohit Iyer", "business": "PeakFit Gym",
     "phone": "9000000002", "plan": "growth"},
    {"email": "owner3@synthetic.test", "name": "Neha Kulkarni", "business": "CodeSprint Coaching",
     "phone": "9000000003", "plan": "enterprise"},
]
CUSTOMERS = ["Ananya Rao", "Vikram Shah", "Divya Menon", "Karan Patel", "Meera Joshi"]
PASSWORD = "synthetic-only-do-not-login"  # dev-only synthetic accounts


def seed(password=PASSWORD):
    out = []
    now = time.time()
    for bi, b in enumerate(BUSINESSES):
        u = models.get_user_by_email(b["email"])
        if not u:
            u = models.create_user(b["email"], auth.hash_pw(password), b["name"],
                                   b["business"], b["phone"], plan=b["plan"])
        models.update_user(u["id"], onboarded=1, plan_status="active")
        # staff member inside this business's workspace
        staff_email = b["email"].replace("owner", "staff")
        staff = models.get_user_by_email(staff_email)
        if not staff:
            staff = models.create_user(staff_email, auth.hash_pw(password),
                                       b["name"].split()[0] + " Staff", b["business"],
                                       b["phone"], role="staff", parent_id=u["id"])
        # customer-contacting automations OFF by default (Phase 7 rule)
        for k in models.AUTO_KEYS:
            models.set_automation(u["id"], k, False)
        ids = []
        for ci, cname in enumerate(CUSTOMERS):
            c = models.create_customer(u["id"], cname,
                                       email=f"{cname.split()[0].lower()}{bi}{ci}@synthetic.test",
                                       phone=f"98765432{bi}{ci}0")
            ids.append(c["id"])
            for inv_n in range(2):
                overdue = (ci + inv_n) % 3 == 0
                amount = 149900 + 10000 * (ci + inv_n)
                inv = models.create_invoice(
                    u["id"], c["id"], f"INV-{bi+1}-{ci}-{inv_n}", amount,
                    "Synthetic monthly subscription", 
                    time.strftime("%Y-%m-%d", time.gmtime(now - (10 if overdue else -7) * 86400)))
                if (ci + inv_n) % 2 == 0:
                    # a failed payment attempt with a decline event (synthetic)
                    models.record_payment_attempt(inv, "upi", status="failed", reference="synthetic")
                    models.record_event(u["id"], "payment.failed", "upi", c["id"], amount,
                                        decline_code="insufficient_funds", invoice_id=inv["id"])
        out.append({"owner_id": u["id"], "staff_id": staff["id"], "email": b["email"],
                    "business": b["business"], "plan": b["plan"]})
    os.makedirs("data", exist_ok=True)
    with open(os.path.join("data", "phase7_seed.json"), "w") as f:
        json.dump({"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "synthetic": True, "businesses": out}, f, indent=2)
    return out


if __name__ == "__main__":
    rows = seed()
    print(f"seeded {len(rows)} synthetic businesses:")
    for r in rows:
        print(" ", r["business"], r["email"], "owner", r["owner_id"], "staff", r["staff_id"])
    print("manifest: data/phase7_seed.json")
