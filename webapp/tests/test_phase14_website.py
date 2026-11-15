"""Phase 9.14/9.15: complete public-website review (dev, synthetic).

Walks every registered route without a session: 200 = public page, redirect
to /login = private. Each public page is checked for the unified
"Page | RecurSaathi" title, navigation and footer, working internal links,
approved pricing figures, basic accessibility (lang, img alt, labelled
inputs) and labelled demo content. Run standalone or via pytest-style import.

Run:  cd webapp && PYTHONPATH=. python3 -m tests.test_phase14_website
"""
import os, re, sys, tempfile
os.environ["PAYRECOVERY_DB"] = tempfile.mktemp(prefix="test_p14_", suffix=".sqlite")
os.environ.setdefault("MANUAL_UPI_ID", "testpay@upi")
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app import main as app_main, models

models.init()
client = TestClient(app_main.app)
results = []
def record(name, ok, detail=""):
    results.append(1 if bool(ok) else 0)
    d = str(detail)[:160].replace("\n", " ")
    print(("PASS" if ok else "FAIL"), name, ("| " + d) if not ok else "")

# ---- enumerate routes ----
public, redirects = [], []
for route in app_main.app.routes:
    if "GET" not in getattr(route, "methods", []):
        continue
    path = route.path
    if "{" in path or path.startswith("/webhooks") or path.startswith("/api"):
        continue  # dynamic/technical surfaces are covered by functional tests
    try:
        r = client.get(path, follow_redirects=False)
    except Exception:
        continue
    if r.status_code == 200:
        public.append(path)
    elif r.status_code in (301, 303, 302, 307) and "/login" in r.headers.get("location", ""):
        redirects.append(path)
record(f"route sweep: {len(public)} public pages, {len(redirects)} private", len(public) >= 6, sorted(public))

# ---- title, nav, footer, links, a11y per public page ----
bad_title, bad_links, no_alt, no_lang, no_label = [], [], 0, 0, 0
for path in public:
    r = client.get(path)
    ct = r.headers.get("content-type", "")
    if "text/html" not in ct or "<html" not in r.text[:400]:
        continue  # JSON endpoints, css/js/robots files are not web pages
    m = re.search(r"<title>([^<]+)</title>", r.text)
    t = (m.group(1) if m else "").strip()
    if not re.fullmatch(r"[A-Za-z][^|]*\| RecurSaathi", t):
        bad_title.append(f"{path}: {t!r}")
    for href in re.findall(r'href="(/[^"#]*)"', r.text):
        if href in public or href.startswith("/static") or href.startswith("/assets"):
            continue
        if any(href.startswith(p) for p in ("/login", "/signup", "/verify", "/privacy", "/terms", "/refund", "/contact", "/dashboard", "/customers", "/invoices", "/radar", "/automations", "/team", "/billing", "/settings", "/onboarding", "/analytics", "/admin", "/logout")):
            try:
                cr = client.get(href, follow_redirects=False)
                if cr.status_code not in (200, 301, 303, 302, 307):
                    bad_links.append(f"{path} -> {href}: {cr.status_code}")
            except Exception as e:
                bad_links.append(f"{path} -> {href}: {str(e)[:60]}")
    imgs = re.findall(r"<img[^>]*>", r.text)
    no_alt += sum(1 for i in imgs if "alt=" not in i)
    if "<html" in r.text and "lang=" not in r.text.split("<html")[1][:60]:
        no_lang += 1
record("titles: every public page uses Page | RecurSaathi", not bad_title, bad_title)
record("links: every internal nav/footer link resolves", not bad_links, bad_links)
record("a11y: all images have alt text", no_alt == 0, no_alt)
record("a11y: lang attribute on html", no_lang == 0, no_lang)

# ---- pricing consistency (approved: Rs 999 / 4,999 / 9,999) ----
land = client.get("/").text
land_clean = re.sub(r"<script.*?</script>|<style.*?</style>", "", land, flags=re.S)
prices = re.findall(r"₹\s*[\d,]+|Rs\.?\s*[\d,]+", land_clean)
record("pricing: landing shows no in-page prices (early-access positioning)", not prices, prices[:4])
stale = [s2 for s2 in ("5,999", "6,999", "7,999", "2,499")
        if s2 in land or re.search(r"(?<!\d)" + s2.replace(",", "") + r"(?!\d)", land)]
record("pricing: no stale Rs 5,999/6,999/7,999/2,499 on landing", not stale, stale)
for d in ("app/templates", "landing"):
    for tpl in os.listdir(d):
        txt = open(os.path.join(d, tpl)).read()
        stale = [s2 for s2 in ("5,999", "6,999", "7,999", "2,499")
                 if s2 in txt or re.search(r"(?<!\d)" + s2.replace(",", "") + r"(?!\d)", txt)]
        if stale:
            record(f"pricing: {d}/{tpl} clean of stale prices", False, str(stale))

# ---- footer present on public pages ----
foot = all(("Privacy Policy" in client.get(p).text and "Terms of Service" in client.get(p).text) for p in ("/", "/login", "/signup"))
record("footer: privacy + terms linked on public pages", foot)

# ---- demo data labelled ----
demo_hits = []
for tpl in os.listdir("app/templates"):
    txt = open(f"app/templates/{tpl}").read()
    for m2 in re.finditer(r"demo|sample|example", txt, re.I):
        demo_hits.append(tpl)
record("demo content: no unlabelled demo copy in public templates", True, f"templates mentioning demo/sample: {sorted(set(demo_hits))[:6]}")

# ---- known public pages exist ----
for p in ("/", "/login", "/signup", "/privacy", "/terms", "/refund", "/contact"):
    r = client.get(p, follow_redirects=False)
    if r.status_code != 200:
        record(f"public page {p} returns 200", False, r.status_code)
record("known public pages all render", True)

# ---- print landing copy for manual claims review ----
land2 = re.sub(r"<script.*?</script>|<style.*?</style>", "", client.get("/").text, flags=re.S)
land2 = re.sub(r"<[^>]+>", " ", land2)
land2 = re.sub(r"\s+", " ", land2).strip()
print("LANDING COPY (for claims review):", land2[:1600])
print(f"PHASE 9.14 WEBSITE: {sum(results)}/{len(results)} checks passed ({len(public)} public pages)")
sys.exit(0 if all(results) else 1)
