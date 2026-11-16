"""Metadata currency regression: every pricing figure renders as INR.

Nov 15 2026 (Manthan): the pricing page's SEO meta description said plain
"Rs 999/month" while the visible page showed rupee signs. Fix: the meta uses
INR wording too. Regression guards: no landing page may reintroduce plain
"Rs <digits>", the pricing meta description must carry the rupee figures,
and visible pricing must stay exactly as approved (Rs 999/Rs 4,999/Rs 9,999
monthly; Rs 9,990/Rs 49,990/Rs 99,990 annual; Early Adopter Rs 79,990 first
12 months, Save Rs 20,000). Static file checks only: no network, no
providers, no database.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_meta_currency
"""
import io, os, re, sys, glob

HERE = os.path.dirname(os.path.abspath(__file__))
LANDING = os.path.abspath(os.path.join(HERE, "..", "landing"))
RUPEE = "\u20b9"
results, fails = [], []

def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, (("| " + str(detail)) if not ok else ""))
    if not ok:
        fails.append(name)

pricing_path = os.path.join(LANDING, "pricing.html")
pricing = io.open(pricing_path, encoding="utf-8").read()

# 1. The SEO meta description uses INR wording
meta = re.search(r'<meta name="description" content="([^"]*)"', pricing)
check("pricing page has a meta description", bool(meta))
meta = meta.group(1) if meta else ""
# Wording updated Nov 16, 2026 to the owner-approved SEO meta
# ("Starter ₹999, Growth ₹4,999 and Enterprise ₹9,999 per month...");
# guards stay the same: exact approved figures, per-month wording.
check("meta carries Starter " + RUPEE + "999 per month", "Starter " + RUPEE + "999" in meta and "per month" in meta)
check("meta carries Growth " + RUPEE + "4,999 per month", "Growth " + RUPEE + "4,999" in meta)
check("meta carries Enterprise " + RUPEE + "9,999 per month", "Enterprise " + RUPEE + "9,999" in meta)
check("meta carries " + RUPEE + "79,990 early-adopter annual", RUPEE + "79,990" in meta)
check("meta has no plain-Rs figures", not re.search(r"Rs\.? ?\d", meta), meta[:120])

# 2. Visible pricing unchanged (exact approved figures)
check("visible monthly " + RUPEE + "999", '"price">' + RUPEE + "999<small>" in pricing)
check("visible monthly " + RUPEE + "4,999", '"price">' + RUPEE + "4,999<small>" in pricing)
check("visible monthly " + RUPEE + "9,999", '"price">' + RUPEE + "9,999<small>" in pricing)
check("visible annual " + RUPEE + "9,990/year", "or " + RUPEE + "9,990/year" in pricing)
check("visible annual " + RUPEE + "49,990/year", "or " + RUPEE + "49,990/year" in pricing)
check("visible annual " + RUPEE + "99,990/year", "or " + RUPEE + "99,990/year" in pricing)
check("early-adopter " + RUPEE + "79,990 first 12 months", RUPEE + "79,990" in pricing)
check("early-adopter saves " + RUPEE + "20,000 against " + RUPEE + "99,990",
      "Save " + RUPEE + "20,000 against the regular annual price of " + RUPEE + "99,990" in pricing)

# 3. No landing page may reintroduce plain "Rs <digits>"
bad = []
for f in sorted(glob.glob(os.path.join(LANDING, "*.html"))):
    s = io.open(f, encoding="utf-8").read()
    for m in re.finditer(r"Rs\.? ?\d", s):
        bad.append((os.path.basename(f), s[max(0, m.start() - 30):m.end() + 20]))
check("no landing page uses plain Rs <digits> anywhere", not bad, str(bad[:3]))

print()
print(f"RESULT: {sum(results)}/{len(results)} passed" + ("" if not fails else f"; FAILURES: {fails}"))
sys.exit(0 if not fails else 1)
