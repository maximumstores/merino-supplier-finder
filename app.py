import streamlit as st
import anthropic
import json
import re
from datetime import datetime
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import plotly.express as px

# ── PAGE CONFIG ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Merino Supplier Finder",
    page_icon="🐑",
    layout="wide",
)

# ── CONSTANTS ────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-20250514"
SPREADSHEET_ID = "1kooRXMsREQ6vpyZMz6sLBzB-6ztWpIPUKOieYhAt-ws"

COUNTRIES = [
    # Asia Pacific
    "China", "Vietnam", "Bangladesh", "India", "Thailand",
    "Indonesia", "Cambodia", "Myanmar", "Sri Lanka", "Pakistan", "Nepal",
    "Australia", "New Zealand", "Mongolia",
    # Europe & Middle East
    "Turkey", "Romania", "Bulgaria", "Italy", "Portugal",
    "Poland", "Czech Republic", "Serbia", "Lithuania", "Hungary", "Morocco",
    # Americas
    "Peru", "Argentina", "Uruguay",
    # Africa
    "South Africa", "Ethiopia",
    # Global
    "🌍 Global (best worldwide)", "All countries",
]
PRODUCTS = [
    "base layer / thermal underwear",
    "baby sleeping bag",
    "men boxers / underwear",
    "polo shirt",
    "sweater / knitwear",
    "socks",
    "merino fabric / yarn",
    "any merino clothing",
]
COLS = ["company", "url", "email", "phone", "whatsapp", "wechat",
        "address", "contact_person", "description",
        "products", "certs", "moq", "priority"]
HEADERS = ["Company", "URL", "Email", "Phone", "WhatsApp",
           "Address", "Contact Person", "Description",
           "Products", "Certs", "MOQ", "Priority"]

SYSTEM_PROMPT = """You are a B2B supplier research assistant for merino.tech — an Amazon FBA merino wool clothing brand.

Find real OEM/ODM manufacturers and factories of merino wool clothing.
Search multiple sources: company websites, Alibaba, Made-in-China, GlobalSources, TradeKey, industry directories.

For each supplier return a JSON object with exactly these keys:
  company, url, email, phone, whatsapp, address, contact_person,
  description, products, certs, moq, priority

priority = HIGH  → 100% merino, OEM/ODM, has direct contacts (email or phone)
           MEDIUM → merino blends or limited contact info
           LOW    → unclear merino focus or only platform listing

SEARCH STRATEGY — for each supplier you find, dig deeper:
1. Visit the company website → find /contact page, /about page
2. Check their Alibaba / Made-in-China / GlobalSources profile for direct contacts
3. Search "[company name] email contact" or "[company name] sales manager"
4. Look for patterns like info@, sales@, export@, inquiry@ + their domain

CONTACT FIELDS RULES:
- email: real email only (e.g. "sales@company.com") — never empty placeholders
- phone: real number with country code (e.g. "+86 138 0000 0000")
- whatsapp: WhatsApp number if explicitly mentioned
- contact_person: name + role if found (e.g. "Lisa Wang, Sales Manager")
- If truly not findable → use "" (empty string)
- NEVER write: "Not listed", "N/A", "Contact through website", "Through platform", "Via Alibaba"

CRITICAL OUTPUT RULE:
Your ENTIRE response must be ONLY a JSON array starting with [ and ending with ].
Do NOT write any text before or after the JSON.
Do NOT explain what you found.
Do NOT say "I'll search..." or "Let me search..."
Just output the JSON array directly. Nothing else.
Example of correct response: [{"company":"X","url":"...","email":"..."}]"""

# ── HELPERS ──────────────────────────────────────────────────────────────────
def get_anthropic_client():
    return anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])


# ── POSTGRESQL ───────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(st.secrets["DATABASE_URL"], sslmode="require")

def init_db():
    """Create table if not exists."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS merino_suppliers (
                    id          SERIAL PRIMARY KEY,
                    company     TEXT,
                    url         TEXT,
                    email       TEXT,
                    phone       TEXT,
                    whatsapp    TEXT,
                    address     TEXT,
                    contact_person TEXT,
                    description TEXT,
                    products    TEXT,
                    certs       TEXT,
                    moq         TEXT,
                    priority    TEXT,
                    search_country TEXT,
                    search_product TEXT,
                    status      TEXT DEFAULT 'New',
                    notes       TEXT DEFAULT '',
                    created_at  TIMESTAMP DEFAULT NOW(),
                    UNIQUE(company, url)
                )
            """)
        conn.commit()

def save_to_db(rows: list, country: str, product: str) -> int:
    """Insert rows, skip duplicates. Returns count inserted."""
    if not rows:
        return 0
    values = [
        (
            r.get("company",""), r.get("url",""), r.get("email",""),
            r.get("phone",""), r.get("whatsapp",""), r.get("wechat",""),
            r.get("address",""), r.get("contact_person",""), r.get("description",""),
            r.get("products",""), r.get("certs",""), r.get("moq",""),
            r.get("priority",""), country, product,
        )
        for r in rows
    ]
    with get_db() as conn:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO merino_suppliers
                  (company,url,email,phone,whatsapp,wechat,address,contact_person,
                   description,products,certs,moq,priority,search_country,search_product)
                VALUES %s
                ON CONFLICT (company, url) DO NOTHING
            """, values)
            inserted = cur.rowcount
        conn.commit()
    return inserted

def load_from_db() -> pd.DataFrame:
    """Load all suppliers from DB. Uses session counter to force refresh."""
    _rev = st.session_state.get("db_rev", 0)
    return _load_from_db_cached(_rev)

@st.cache_data(ttl=300)
def _load_from_db_cached(_rev: int) -> pd.DataFrame:
    with get_db() as conn:
        df = pd.read_sql("SELECT * FROM merino_suppliers ORDER BY created_at DESC", conn)
    # filter out archived in Python (handles missing status column gracefully)
    if "status" in df.columns:
        df = df[df["status"].fillna("New") != "🗄️ Archived"]
    return df

def load_archived() -> pd.DataFrame:
    """Load archived suppliers."""
    _rev = st.session_state.get("db_rev", 0)
    return _load_archived_cached(_rev)

@st.cache_data(ttl=300)
def _load_archived_cached(_rev: int) -> pd.DataFrame:
    with get_db() as conn:
        df = pd.read_sql("SELECT * FROM merino_suppliers ORDER BY created_at DESC", conn)
    if "status" in df.columns:
        return df[df["status"].fillna("New") == "🗄️ Archived"]
    return pd.DataFrame()

def region_flag(addr: str) -> str:
    a = (addr or "").lower().strip()
    REGIONS = [
        ("🇨🇳 China",       r"china|shanghai|beijing|guangdong|jiangsu|zhejiang|ningbo|shenzhen|suzhou|guangzhou|hangzhou|changshu|zhangjiagang|jiaxing|cn$"),
        ("🇮🇳 India",       r"india|mumbai|delhi|noida|panipat|bangalore|chennai|surat|jaipur|kolkata"),
        ("🇻🇳 Vietnam",     r"vietnam|viet nam|ho chi minh|hanoi|hcmc|vn$"),
        ("🇹🇷 Turkey",      r"turkey|türkiye|istanbul|bursa|ankara|izmir|tr$"),
        ("🇧🇩 Bangladesh",  r"bangladesh|dhaka|chittagong|bd$"),
        ("🇵🇰 Pakistan",    r"pakistan|karachi|lahore|faisalabad|pk$"),
        ("🇷🇴 Romania",     r"romania|bucharest|cluj|brasov|ro$"),
        ("🇧🇬 Bulgaria",    r"bulgaria|sofia|plovdiv|bg$"),
        ("🇮🇹 Italy",       r"italy|italia|milan|como|florence|it$"),
        ("🇵🇹 Portugal",    r"portugal|lisbon|porto|pt$"),
        ("🇵🇱 Poland",      r"poland|warszawa|krakow|lodz|pl$"),
        ("🇨🇿 Czech Rep",   r"czech|czechia|prague|brno|cz$"),
        ("🇷🇸 Serbia",      r"serbia|belgrade|novi sad|rs$"),
        ("🇦🇺 Australia",   r"australia|sydney|melbourne|au$"),
        ("🇳🇿 New Zealand", r"new zealand|auckland|wellington|nz$"),
        ("🇲🇳 Mongolia",    r"mongolia|ulaanbaatar|mn$"),
        ("🇲🇦 Morocco",     r"morocco|casablanca|rabat|ma$"),
        ("🇿🇦 South Africa",r"south africa|johannesburg|cape town|za$"),
        ("🇪🇹 Ethiopia",    r"ethiopia|addis ababa|et$"),
        ("🇵🇪 Peru",        r"peru|lima|pe$"),
        ("🇦🇷 Argentina",   r"argentina|buenos aires|ar$"),
        ("🇺🇾 Uruguay",     r"uruguay|montevideo|uy$"),
        ("🇹🇭 Thailand",    r"thailand|bangkok|th$"),
        ("🇮🇩 Indonesia",   r"indonesia|jakarta|bandung|id$"),
        ("🇰🇭 Cambodia",    r"cambodia|phnom penh|kh$"),
        ("🇲🇲 Myanmar",     r"myanmar|burma|yangon|mm$"),
        ("🇱🇰 Sri Lanka",   r"sri lanka|colombo|lk$"),
    ]
    for label, pattern in REGIONS:
        if re.search(pattern, a):
            return label
    return "🌐 Other"

def clean_contact(val: str) -> str:
    """Remove placeholder text, keep only real contact values."""
    if not val:
        return ""
    junk = ["not listed","not specified","n/a","na","contact through","through website",
            "through platform","platform","contact form","via website","see website",
            "globalsources","made-in-china","alibaba","contact for details","available on"]
    v = val.strip()
    if any(j in v.lower() for j in junk):
        return ""
    return v

def clean_row(r: dict) -> dict:
    for f in ["email","phone","whatsapp"]:
        r[f] = clean_contact(r.get(f,""))
    return r

def calc_score(r) -> int:
    """Score a supplier row. Max ~20."""
    s = 0
    # contacts
    if r.get("email",""):      s += 3
    if r.get("phone",""):      s += 2
    if r.get("whatsapp",""):   s += 1
    if r.get("contact_person",""): s += 1
    # priority
    pri = str(r.get("priority","")).upper()
    if pri == "HIGH":   s += 3
    elif pri == "MEDIUM": s += 1
    # certs
    certs = str(r.get("certs","")).lower()
    if "woolmark" in certs: s += 2
    if "oeko"     in certs: s += 1
    if "rws"      in certs: s += 1
    if "bsci"     in certs: s += 1
    if "gots"     in certs: s += 1
    # other
    if r.get("moq",""):  s += 1
    if r.get("url",""):  s += 1
    return s

def score_emoji(s: int) -> str:
    if s >= 12: return f"⭐⭐⭐ {s}"
    if s >= 7:  return f"⭐⭐ {s}"
    if s >= 3:  return f"⭐ {s}"
    return f"· {s}"

# ── INIT DB ──────────────────────────────────────────────────────────────────
try:
    init_db()
except Exception as e:
    st.warning(f"DB init warning: {e}")

# explicit migration — runs every startup, safe
try:
    with get_db() as _conn:
        with _conn.cursor() as _cur:
            _cur.execute("ALTER TABLE merino_suppliers ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'New'")
            _cur.execute("ALTER TABLE merino_suppliers ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT ''")
        _conn.commit()
except Exception as _e:
    pass  # table may not exist yet — init_db will create it

# ── SESSION STATE ────────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.session_state.results = []
if "log" not in st.session_state:
    st.session_state.log = []

def add_log(msg: str, level: str = "info"):
    icon = {"info": "✅", "error": "❌", "blue": "📊", "warn": "⚠️"}.get(level, "•")
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log.append(f"`{ts}` {icon} {msg}")

# ── SEARCH FUNCTION ───────────────────────────────────────────────────────────
def domain_to_company(url: str) -> str:
    """Extract readable company name from domain."""
    import re
    try:
        domain = url.split("//")[-1].split("/")[0].lower()
        domain = re.sub(r'^www\.', '', domain)
        name   = domain.split(".")[0]
        # camelCase split + capitalize
        name = re.sub(r'[-_]', ' ', name)
        return name.title()
    except Exception:
        return url

def parse_sd_results_to_json(sd_text: str, country: str, product: str) -> list:
    """Parse ScrapingDog search results into supplier dicts without Claude."""
    import re
    suppliers = []
    skip_kw = ["alibaba.com/trade", "amazon.", "ebay.", "wikipedia",
               "youtube", "news", "blog", "europages", "made-in-china.com",
               "globalsources", "thomasnet", "kompass", "yellowpages"]

    blocks = re.split(r'\n(?=- )', sd_text.strip())
    for block in blocks:
        if not block.strip() or not block.startswith("- "):
            continue
        lines  = block.strip().split("\n")
        header = lines[0][2:]
        parts  = header.split(" | ", 1)
        title  = parts[0].strip() if parts else ""
        url    = parts[1].strip() if len(parts) > 1 else ""
        snippet = " ".join(l.strip() for l in lines[1:])

        if not title or not url:
            continue
        if any(k in url.lower() for k in skip_kw):
            continue

        # better company name: from domain, not page title
        company = domain_to_company(url)

        # extract email from snippet
        email_m = re.search(r'[\w.+-]+@[\w-]+\.[\w.]{2,}', block)
        email   = email_m.group(0) if email_m else ""

        # extract phone
        phone_m = re.search(r'(\+?\d[\d\s\-().]{7,18}\d)', block)
        phone   = phone_m.group(0).strip() if phone_m else ""

        # priority: has email → MEDIUM, else LOW
        priority = "MEDIUM" if email or phone else "LOW"

        suppliers.append({
            "company":        company,
            "url":            url,
            "email":          clean_contact(email),
            "phone":          clean_contact(phone),
            "whatsapp":       "",
            "address":        country,
            "contact_person": "",
            "description":    snippet[:200],
            "products":       product,
            "certs":          "",
            "moq":            "",
            "priority":       priority,
        })
    return suppliers

def was_searched_recently(country: str, product: str, days: int = 7) -> bool:
    """Return True if this country+product combo was searched within N days."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM merino_suppliers
                    WHERE LOWER(search_country) = LOWER(%s)
                      AND LOWER(search_product) = LOWER(%s)
                      AND created_at > NOW() - INTERVAL '%s days'
                """, (country, product, days))
                return cur.fetchone()[0] > 0
    except Exception:
        return False

def scrape_contact_page(url: str) -> str:
    """Fetch a page via ScrapingDog (JS rendered). Returns HTML text or empty string."""
    import urllib.request, urllib.parse
    try:
        api_key = st.secrets.get("SCRAPINGDOG_API_KEY", "")
        if not api_key or not url:
            return ""
        encoded = urllib.parse.quote(url, safe="")
        endpoint = f"https://api.scrapingdog.com/scraper?api_key={api_key}&url={encoded}&dynamic=true"
        req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="ignore")[:15000]
    except Exception:
        return ""

def scrapingdog_search(query: str, num: int = 10) -> str:
    """Search Google via ScrapingDog API. Returns text summary of results."""
    import urllib.request, urllib.parse
    try:
        api_key = st.secrets.get("SCRAPINGDOG_API_KEY", "")
        if not api_key:
            return ""
        params = urllib.parse.urlencode({
            "api_key": api_key,
            "query": query,
            "results": num,
        })
        url = f"https://api.scrapingdog.com/google?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        r = urllib.request.urlopen(req, timeout=20)
        data = json.loads(r.read().decode())
        results = data.get("organic_results", [])
        lines = []
        for item in results:
            title   = item.get("title", "")
            link    = item.get("link", "")          # correct field name
            snippet = item.get("snippet", "")
            if title and link:
                lines.append(f"- {title} | {link}\n  {snippet}")
        return "\n".join(lines)
    except urllib.error.HTTPError as e:
        add_log(f"ScrapingDog HTTP {e.code}: {e.read().decode()[:100]}", "warn")
        return ""
    except Exception as e:
        add_log(f"ScrapingDog error: {e}", "warn")
        return ""

def enrich_with_scrapingdog(company: str, url: str, address: str) -> dict:
    """Use ScrapingDog + Claude to extract contacts from company website."""
    import urllib.parse

    # Build list of URLs to try
    urls_to_try = []
    if url:
        base = url.rstrip("/")
        urls_to_try += [f"{base}/contact", f"{base}/contact-us", f"{base}/about", url]

    html_collected = ""
    for u in urls_to_try[:3]:
        html = scrape_contact_page(u)
        if html:
            html_collected += f"\n\n--- PAGE: {u} ---\n{html[:4000]}"
            if "@" in html or "tel:" in html.lower():
                break  # found something useful

    if not html_collected:
        return {}

    # Ask Claude to extract contacts from HTML
    client = get_anthropic_client()
    extract_prompt = (
        f"Extract contact information from this HTML for company: {company}\n"
        f"Address: {address}\n\n"
        f"HTML content:\n{html_collected[:8000]}\n\n"
        f"Find: email address, phone number, WhatsApp, sales manager name.\n"
        f"Return ONLY JSON: {{\"email\": \"\", \"phone\": \"\", \"whatsapp\": \"\", \"contact_person\": \"\"}}"
        f"Use empty string if not found. NEVER use placeholder text."
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=300,
        messages=[{"role": "user", "content": extract_prompt}]
    )
    txt = " ".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{[^{}]*\}", txt)
    if m:
        found = json.loads(m.group(0))
        return {k: clean_contact(v) for k, v in found.items()}
    return {}

def run_search(country: str, product: str, extra: str, status_box=None, mode: str = "auto"):
    where = "globally, find the best worldwide" if country in ("All countries", "🌍 Global (best worldwide)") else f"in {country}"

    # cache check
    if was_searched_recently(country, product, 7):
        add_log(f"⏭️ Skipped (< 7d): **{product}** / **{country}**", "warn")
        return 0

    add_log(f"Starting search: **{product}** / **{country}**")

    sd_key  = st.secrets.get("SCRAPINGDOG_API_KEY", "")
    use_sd  = bool(sd_key) and mode == "🐕 ScrapingDog"
    use_ant = mode == "🤖 Claude web_search"
    add_log(f"mode={mode} · sd_key={'✅' if sd_key else '❌ MISSING'} · use_sd={use_sd} · use_ant={use_ant}")

    research_text = ""

    # ── STEP 1: gather research ──────────────────────────────────────────────
    if use_sd:
        if status_box: status_box.update(label="🐕 ScrapingDog searching...")
        queries = [
            f"merino wool {product} manufacturer {country} OEM factory",
            f"merino wool {product} supplier {country} contact email wholesale",
            f"merino {country} clothing factory export direct",
        ]
        if extra:
            queries.append(f"merino {product} {country} {extra}")
        parts = []
        for i, q in enumerate(queries):
            if status_box: status_box.update(label=f"🐕 [{i+1}/{len(queries)}] {q[:45]}...")
            r = scrapingdog_search(q, num=10)
            if r: parts.append(r)
        research_text = "\n\n".join(parts)
        add_log(f"🐕 {len(queries)} queries · {len(research_text)} chars")

    if not research_text.strip() and use_ant:
        add_log("🔍 Claude web_search...", "warn")
        if status_box: status_box.update(label="🔍 Claude web_search...")
        client = get_anthropic_client()
        r1 = client.messages.create(
            model=MODEL, max_tokens=3000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                f"Find merino wool {product} manufacturers {where}. "
                f"{('Extra: ' + extra) if extra else ''} "
                f"Search Alibaba, Made-in-China, GlobalSources. Find 8-12 suppliers with contacts."
            }],
        )
        n_searches = sum(1 for b in r1.content if b.type == "tool_use")
        research_text = " ".join(b.text for b in r1.content if hasattr(b,"text") and b.text)
        add_log(f"🔍 {n_searches} searches · {len(research_text)} chars")

    if not research_text.strip():
        add_log("No results from any source", "error")
        return 0

    # ── STEP 2: parse to structured data ─────────────────────────────────────
    if status_box: status_box.update(label="✍️ Formatting results...")

    # if ScrapingDog-only mode and no Claude credits → use Python parser
    ant_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    if mode == "🐕 ScrapingDog" and not ant_key:
        parsed = parse_sd_results_to_json(research_text, country, product)
        add_log(f"🐕 Python parser: {len(parsed)} suppliers")
    else:
        # Claude formats JSON via assistant prefill
        try:
            client = get_anthropic_client()
            r2 = client.messages.create(
                model=MODEL, max_tokens=4000,
                messages=[
                    {"role": "user", "content": (
                        f"Extract suppliers from this research into a JSON array.\n\n"
                        f"{research_text[:6000]}\n\n"
                        f"Each object: company, url, email, phone, whatsapp, address, "
                        f"contact_person, description, products, certs, moq, "
                        f"priority(HIGH/MEDIUM/LOW). Missing = empty string."
                    )},
                    {"role": "assistant", "content": "["},
                ],
            )
            raw = " ".join(b.text for b in r2.content if b.type == "text")
            full_text = "[" + raw
            add_log(f"JSON: {len(full_text)} chars")
            # parse
            m = re.search(r"\[[\s\S]*\]", full_text)
            parsed = json.loads(m.group(0)) if m else []
        except Exception as e:
            add_log(f"Claude formatting failed: {e} → Python parser fallback", "warn")
            parsed = parse_sd_results_to_json(research_text, country, product)

    if not parsed:
        add_log("No suppliers extracted", "error")
        return 0
    if not isinstance(parsed, list):
        parsed = [parsed]

    # ── DEDUP + SAVE ─────────────────────────────────────────────────────────
    existing_session = {(r.get("company","") + r.get("url","")).lower() for r in st.session_state.results}
    try:
        df_ex = load_from_db()
        existing_db = set((df_ex["company"].fillna("") + df_ex["url"].fillna("")).str.lower())
    except Exception:
        existing_db = set()

    already_in_db, fresh = [], []
    for r in parsed:
        key = (r.get("company","") + r.get("url","")).lower()
        if key in existing_session:
            pass
        elif key in existing_db:
            already_in_db.append(r.get("company","?"))
        else:
            fresh.append(clean_row(r))

    st.session_state.results.extend(fresh)
    parts = [f"Found **{len(parsed)}** total"]
    if fresh: parts.append(f"**{len(fresh)}** new")
    if already_in_db: parts.append(f"**{len(already_in_db)}** in DB: {', '.join(already_in_db[:3])}")
    add_log(" · ".join(parts))

    try:
        save_to_db(fresh, country, product)
        st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
    except Exception as e:
        add_log(f"DB: {e}", "warn")

    return len(fresh)




# ── UI ────────────────────────────────────────────────────────────────────────
st.markdown("## 🐑 Merino Supplier Finder")
st.caption(f"merino.tech internal · {MODEL} · web_search")

with st.expander("📖 How it works", expanded=False):
    st.markdown("""
**Workflow в 6 шагов:**

| Шаг | Действие | Где |
|-----|----------|-----|
| 1️⃣ | Выбери режим поиска: **🐕 ScrapingDog** или **🤖 Claude** → страну + продукт → **▶ Search** | Вверху страницы |
| 2️⃣ | Проверь результаты: ⭐ Score, ✅ контакты, фильтры по региону/приоритету | Session results |
| 3️⃣ | Прокрути вниз в Database (all) → нажми **🐕 Enrich All without contacts (N)** — автопоиск email/phone | Database (all) ↓ |
| 4️⃣ | Прокрути вниз → выбери компанию → нажми **✉️ Generate email** → выбери язык EN/CN → скопируй | Database (all) ↓ |
| 5️⃣ | В таблице кликни ячейку **Status** → выбери статус → нажми **🔄 Apply & Refresh** | Database (all) |
| 6️⃣ | Если поставщик не нужен → Status = **🗄️ Archived** → Apply → переходит в таб Archive | Archive |

---

**Все фичи:**

| Функция | Описание |
|---------|----------|
| ⭐ Score | Авто-балл качества: email +3, phone +2, Woolmark +2, сертификаты +1 каждый, HIGH +3 |
| 🐕 ScrapingDog | Поиск через Google без Claude API — работает даже когда кончились кредиты |
| 🤖 Claude web_search | Глубокий поиск с пониманием контекста — точнее, рекомендуется основным |
| 🐕 Enrich All | Одна кнопка — ScrapingDog обходит сайты всех компаний без контактов и ищет email/phone |
| ✉️ Email generator | AI пишет персональное outreach письмо на EN или CN под профиль конкретного поставщика |
| Status | Воронка: New → Contacted → Replied → Negotiating → Deal / Rejected / 🗄️ Archived |
| 🗄️ Archive | Компании которые не подошли — скрыты из основного списка, но хранятся с заметками. Можно восстановить |
| 📝 Notes | Поле для заметок в Archive: причина отказа, дата контакта, что ответили |
| ⬇️ CSV / Excel | Экспорт текущей таблицы с фильтрами в файл |
| 🗑️ Delete | Полное удаление из базы (необратимо) |
| 📊 Charts | Графики: по региону, приоритету, контактам, продуктам |
    """)

st.divider()

# ── SEARCH CONTROLS ───────────────────────────────────────────────────────────
has_sd  = bool(st.secrets.get("SCRAPINGDOG_API_KEY",""))
has_ant = bool(st.secrets.get("ANTHROPIC_API_KEY",""))

mode_options = []
mode_labels  = {}
if has_sd:
    mode_options.append("🐕 ScrapingDog")
    mode_labels["🐕 ScrapingDog"] = "ScrapingDog → Claude formats JSON (дешевле)"
if has_ant:
    mode_options.append("🤖 Claude web_search")
    mode_labels["🤖 Claude web_search"] = "Claude ищет и форматирует (дороже но глубже)"


if not mode_options:
    st.error("Нет API ключей. Добавь ANTHROPIC_API_KEY или SCRAPINGDOG_API_KEY в Secrets.")
    st.stop()

mc1, mc2 = st.columns([2, 4])
with mc1:
    search_mode = st.radio("Search engine", mode_options, horizontal=True,
                           key="search_mode", label_visibility="collapsed")
with mc2:
    st.caption(f"⚙️ {mode_labels.get(search_mode,'')}")

col1, col2, col3, col4, col5 = st.columns([1.4, 1.6, 2.5, 0.9, 0.7])

with col1:
    country_select = st.multiselect("Country", COUNTRIES, default=["China"], key="country",
                                    placeholder="Select or type any country...")
    country_custom = st.text_input("", placeholder="Or type any country not in list (e.g. Fiji, Samoa...)", 
                                   key="country_custom", label_visibility="collapsed")
    country = country_select + ([country_custom.strip()] if country_custom.strip() else [])
with col2:
    product = st.multiselect("Product", PRODUCTS, default=["base layer / thermal underwear"], key="product",
                              placeholder="Select products...")
with col3:
    QUICK_TAGS = [
        "Woolmark certified", "low MOQ", "no mulesing",
        "direct factory", "OCS organic", "GOTS certified",
        "accepts samples", "18.5 micron superfine",
        "OEKO-TEX certified", "RWS certified",
    ]
    tags = st.multiselect("Quick tags", QUICK_TAGS, key="tags", label_visibility="collapsed",
                           placeholder="Quick tags (optional)...")
    extra_custom = st.text_input("Extra requirements", placeholder="Or type custom...", label_visibility="collapsed")
    extra = ", ".join(tags + ([extra_custom] if extra_custom.strip() else []))
with col4:
    st.write("")
    search_btn = st.button("▶ Search", type="primary", use_container_width=True)
with col5:
    st.write("")
    clear_btn = st.button("✕ Clear", use_container_width=True)

if clear_btn:
    st.session_state.results = []
    st.session_state.log = []
    st.rerun()

# ── METRICS ──────────────────────────────────────────────────────────────────
# session stats
_total   = len(st.session_state.results)
_with_c  = sum(1 for r in st.session_state.results if r.get("email") or r.get("phone"))
_high    = sum(1 for r in st.session_state.results if r.get("priority") == "HIGH")
with_contacts = _with_c

# DB stats
try:
    _df_stat = load_from_db()
    _db_total  = len(_df_stat)
    _db_with_c = int((_df_stat["email"].fillna("").str.len() + _df_stat["phone"].fillna("").str.len()) > 0).sum() if _db_total else 0
    _db_no_c   = _db_total - _db_with_c
    _db_high   = int((_df_stat["priority"].fillna("") == "HIGH").sum()) if _db_total else 0
    _db_pct    = f"{round(_db_with_c/_db_total*100)}%" if _db_total else "—"
except Exception:
    _db_total = _db_with_c = _db_no_c = _db_high = 0
    _db_pct = "—"

m1, m2, m3, m4, m5, m6 = st.columns(6)
with m1:
    st.metric("🗄️ DB total", _db_total)
with m2:
    st.metric("✅ With contacts", _db_with_c, delta=_db_pct, delta_color="off")
with m3:
    st.metric("❌ No contacts", _db_no_c)
with m4:
    st.metric("⭐ HIGH", _db_high)
with m5:
    st.metric("🔍 Session", _total, delta=f"+{_total}" if _total else None)
with m6:
    db_refresh = st.button("🔄 Refresh", use_container_width=True)

if db_refresh:
    st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
    st.rerun()

# ── RUN SEARCH ────────────────────────────────────────────────────────────────
if search_btn:
    countries = country if country else ["China"]
    products  = product if product else ["base layer / thermal underwear"]
    total_new = 0
    combos = [(c, p) for c in countries for p in products]

    # show cache preview
    cached = [(c,p) for c,p in combos if was_searched_recently(c, p, 7)]
    fresh  = [(c,p) for c,p in combos if not was_searched_recently(c, p, 7)]
    if cached:
        st.info(f"⏭️ **{len(cached)}** уже искали (< 7 дней) — пропускаем: "
                + ", ".join(f"{p}/{c}" for c,p in cached[:3])
                + ("..." if len(cached)>3 else ""))
    if not fresh:
        st.warning("Все комбинации уже искали недавно. Поиск не запущен.")
        st.stop()
    for i, (c, p) in enumerate(combos):
        label = f"[{i+1}/{len(combos)}] {p} / {c}"
        with st.status(label, expanded=True) as status:
            status.write(f"🚀 Starting search...")
            try:
                n = run_search(c, p, extra, status_box=status, mode=search_mode)
                total_new += n
                status.update(label=f"✅ {label} — found {n} new", state="complete")
            except Exception as e:
                status.update(label=f"❌ {label} — error", state="error")
                st.error(str(e))
                add_log(str(e), "error")
    if total_new:
        st.success(f"✅ Done! Added **{total_new}** new suppliers.")


# ── LOG ───────────────────────────────────────────────────────────────────────
if st.session_state.log:
    with st.expander("📋 Log", expanded=True):
        for line in reversed(st.session_state.log[-15:]):
            st.markdown(line)

# ── RESULTS — tabs: session / database / charts ───────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs(["🔍 Session results", "🗄️ Database (all)", "📊 Charts", "🗄️ Archive", "📥 Import"])

def render_table(df: pd.DataFrame, allow_edit: bool = False):
    """Render filtered supplier table."""
    if df.empty:
        st.info("No data.")
        return

    df = df.copy()
    df["region"] = df.get("address", pd.Series([""] * len(df))).fillna("").apply(region_flag)
    # mark rows with real contacts
    df["✉️"] = df.apply(lambda r: "✅" if (r.get("email","") or r.get("phone","") or r.get("whatsapp","")) else "—", axis=1)
    df["⭐"] = df.apply(lambda r: score_emoji(calc_score(r)), axis=1)
    if "status" not in df.columns:
        df["status"] = "New"

    # ── FILTERS ──
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        regions = ["All"] + sorted(df["region"].unique().tolist())
        f_region = st.selectbox("🌍 Region", regions, key=f"fr_{allow_edit}")
    with fc2:
        pris = ["All"] + [p for p in ["HIGH","MEDIUM","LOW"] if p in df.get("priority", pd.Series()).values]
        f_pri = st.selectbox("⭐ Priority", pris, key=f"fp_{allow_edit}")
    with fc3:
        f_contact = st.selectbox("📬 Contacts", ["All","✅ Has contacts","— No contacts"], key=f"fc_{allow_edit}")

    fc4, fc5, fc6, fc7 = st.columns(4)
    with fc4:
        f_company = st.text_input("🏭 Company", key=f"fco_{allow_edit}", placeholder="Search company...")
    with fc5:
        f_email = st.text_input("✉️ Email", key=f"fe_{allow_edit}", placeholder="sales@, @gmail...")
    with fc6:
        f_phone = st.text_input("📞 Phone", key=f"fph_{allow_edit}", placeholder="+86, +84...")
    with fc7:
        f_url = st.text_input("🌐 Website", key=f"furl_{allow_edit}", placeholder=".com, woolona...")

    # apply filters
    mask = pd.Series([True] * len(df), index=df.index)
    if f_region != "All":
        mask &= df["region"] == f_region
    if f_pri != "All":
        mask &= df["priority"].fillna("") == f_pri
    if f_contact == "✅ Has contacts":
        mask &= df["✉️"] == "✅"
    elif f_contact == "— No contacts":
        mask &= df["✉️"] == "—"
    if f_company:
        mask &= df["company"].fillna("").str.lower().str.contains(f_company.lower(), na=False)
    if f_email:
        mask &= df["email"].fillna("").str.lower().str.contains(f_email.lower(), na=False)
    if f_phone:
        mask &= df["phone"].fillna("").str.lower().str.contains(f_phone.lower(), na=False)
    if f_url:
        mask &= df["url"].fillna("").str.lower().str.contains(f_url.lower(), na=False)
    df_f = df[mask]

    # ── EXPORT COLS (defined before use) ──
    import io
    base_cols = ["region","company","url","email","phone","whatsapp","products","certs","priority"]
    if "status" in df_f.columns:
        base_cols = ["status"] + base_cols
    export_cols = [c for c in base_cols if c in df_f.columns]
    df_export = df_f[export_cols]

    ec1, ec2, ec_gap = st.columns([1, 1, 4])
    with ec1:
        csv_bytes = df_export.to_csv(index=False).encode()
        st.download_button("⬇️ CSV", csv_bytes, "suppliers.csv", "text/csv", use_container_width=True, key=f"csv_{allow_edit}")
    with ec2:
        xlsx_buf = io.BytesIO()
        df_export.to_excel(xlsx_buf, index=False, engine="openpyxl")
        xlsx_buf.seek(0)
        st.download_button("⬇️ Excel", xlsx_buf.read(), "suppliers.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, key=f"xlsx_{allow_edit}")

    # sort by score descending
    if "⭐" in df_f.columns:
        df_f = df_f.copy()
        df_f["_sort"] = df_f["⭐"].str.extract(r"(\d+)").astype(float).fillna(0)
        df_f = df_f.sort_values("_sort", ascending=False).drop(columns=["_sort"])

    # ── DELETE selected ──
    if allow_edit and "id" in df_f.columns:
        with ec_gap:
            del_company = st.selectbox("🗑️ Delete company", ["—"] + df_f["company"].dropna().tolist(),
                                       key=f"del_{allow_edit}", label_visibility="collapsed")
            if del_company != "—":
                if st.button(f"🗑️ Delete: {del_company[:30]}", key=f"delbtn_{allow_edit}", type="secondary"):
                    row_id = df_f[df_f["company"] == del_company]["id"].iloc[0]
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM merino_suppliers WHERE id=%s", (int(row_id),))
                        conn.commit()
                    st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                    st.toast(f"Deleted: {del_company}")
                    st.rerun()

    st.caption(f"Showing **{len(df_f)}** of {len(df)} suppliers")

    show_cols = ["⭐","in_db","✉️","region","company","status","url","email","phone","whatsapp","wechat","products","certs","priority","notes","quotes"]
    if "status" in df_f.columns:
        show_cols = ["⭐","✉️","region","company","status","url","email","phone","whatsapp","wechat","products","certs","priority","notes","quotes"]
    existing = [c for c in show_cols if c in df_f.columns]

    cfg = {
        "url": st.column_config.LinkColumn(),
        "⭐": st.column_config.TextColumn("Score", width="small"),
        "in_db": st.column_config.TextColumn("DB", width="small"),
        "✉️": st.column_config.TextColumn("📬", width="small"),
        "wechat": st.column_config.TextColumn("💬 WeChat", width="small"),
        "notes": st.column_config.TextColumn("📝 Notes", width="medium"),
        "quotes": st.column_config.TextColumn("💰 Quotes (product: price/lead)", width="large"),
        "region": st.column_config.TextColumn("Region", width="small"),
        "company": st.column_config.TextColumn("Company", width="medium"),
        "email": st.column_config.TextColumn("Email", width="medium"),
        "phone": st.column_config.TextColumn("Phone", width="small"),
        "priority": st.column_config.TextColumn("Pri", width="small"),
    }
    if allow_edit and "status" in df_f.columns:
        cfg["status"] = st.column_config.SelectboxColumn(
            "Status", options=["New","Contacted","Replied","Negotiating","Deal","Rejected","🗄️ Archived"],
            width="small"
        )

    if allow_edit:
        edited = st.data_editor(df_f[existing], use_container_width=True, height=520,
                                hide_index=True, column_config=cfg,
                                key=f"editor_{allow_edit}")
        # save status changes back to DB
        if "status" in edited.columns and "id" in df_f.columns:
            changed = edited[edited["status"].fillna("New") != df_f.loc[edited.index,"status"].fillna("New")]
            if not changed.empty:
                try:
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            for idx, row in changed.iterrows():
                                orig_id = df_f.loc[idx,"id"]
                                cur.execute("UPDATE merino_suppliers SET status=%s WHERE id=%s",
                                            (str(row["status"]), int(orig_id)))
                        conn.commit()
                    st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                except Exception as e:
                    st.warning(f"Save error: {e}")

        # apply button to force refresh (needed after Archive)
        save_col, _ = st.columns([1, 5])
        with save_col:
            if st.button("🔄 Apply & Refresh", key=f"apply_{allow_edit}", use_container_width=True):
                st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                st.rerun()
    else:
        st.dataframe(df_f[existing], use_container_width=True, height=520,
                     hide_index=True, column_config=cfg)

with tab1:
    if st.session_state.results:
        df_s = pd.DataFrame(st.session_state.results).copy()
        # mark rows already in DB
        try:
            df_existing = load_from_db()
            db_keys = set((df_existing["company"].fillna("") + df_existing["url"].fillna("")).str.lower())
        except Exception:
            db_keys = set()
        df_s["in_db"] = df_s.apply(
            lambda r: "✅ in DB" if (str(r.get("company","")) + str(r.get("url",""))).lower() in db_keys else "🆕 New",
            axis=1
        )
        render_table(df_s, allow_edit=False)
    else:
        st.info("No results yet — select Country + Product and click **▶ Search**")

with tab2:
    try:
        df_db = load_from_db()
        if df_db.empty:
            st.info("Database is empty — run a search first.")
        else:
            # ── WORKFLOW SUMMARY ──
            if "status" in df_db.columns:
                status_counts = df_db["status"].fillna("New").value_counts()
                STATUS_ORDER = ["New","Contacted","Replied","Negotiating","Deal","Rejected"]
                STATUS_EMOJI = {"New":"🆕","Contacted":"📤","Replied":"📥","Negotiating":"🤝","Deal":"✅","Rejected":"❌"}
                scols = st.columns(len(STATUS_ORDER))
                for i, s in enumerate(STATUS_ORDER):
                    cnt = status_counts.get(s, 0)
                    scols[i].metric(f"{STATUS_EMOJI[s]} {s}", cnt)
                st.divider()

            render_table(df_db, allow_edit=True)

            st.divider()

            # ── ENRICH ALL ──
            no_contacts = df_db[
                df_db["email"].fillna("").str.len() +
                df_db["phone"].fillna("").str.len() == 0
            ]
            has_sd = bool(st.secrets.get("SCRAPINGDOG_API_KEY",""))
            enrich_col1, enrich_col2 = st.columns([2,4])
            with enrich_col1:
                enrich_all_btn = st.button(
                    f"🐕 Enrich All without contacts ({len(no_contacts)})",
                    key="enrich_all_btn",
                    use_container_width=True,
                    disabled=not has_sd or len(no_contacts) == 0
                )
                if not has_sd:
                    st.caption("⚠️ Add SCRAPINGDOG_API_KEY to Secrets")
            with enrich_col2:
                if len(no_contacts) > 0:
                    st.caption(f"Will process: {', '.join(no_contacts['company'].dropna().tolist()[:5])}{'...' if len(no_contacts)>5 else ''}")

            if enrich_all_btn:
                progress = st.progress(0, text="Starting...")
                results_placeholder = st.empty()
                enriched, failed, total = 0, 0, len(no_contacts)

                for i, (_, r) in enumerate(no_contacts.iterrows()):
                    company = r.get("company","?")
                    progress.progress((i+1)/total, text=f"[{i+1}/{total}] 🐕 {company}...")
                    try:
                        found = enrich_with_scrapingdog(company, r.get("url",""), r.get("address",""))
                        # fallback to web_search if ScrapingDog found nothing
                        if not any(found.values()):
                            client = get_anthropic_client()
                            ep = (
                                f"Find direct contact info for: {company}\n"
                                f"Website: {r.get('url','')}\nAddress: {r.get('address','')}\n"
                                f"Return ONLY JSON: {{\"email\":\"\",\"phone\":\"\",\"whatsapp\":\"\",\"contact_person\":\"\"}}"
                            )
                            resp = client.messages.create(
                                model=MODEL, max_tokens=300,
                                tools=[{"type":"web_search_20250305","name":"web_search"}],
                                messages=[{"role":"user","content":ep}]
                            )
                            txt = " ".join(b.text for b in resp.content if b.type=="text")
                            m = re.search(r"\{[^{}]*\}", txt)
                            if m:
                                found = {k: clean_contact(v) for k,v in json.loads(m.group(0)).items()}

                        updates = {k: v for k, v in found.items() if v}
                        if updates and "id" in r:
                            with get_db() as conn:
                                with conn.cursor() as cur:
                                    for col, val in updates.items():
                                        cur.execute(f"UPDATE merino_suppliers SET {col}=%s WHERE id=%s",
                                                    (val, int(r["id"])))
                                conn.commit()
                            enriched += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1

                progress.progress(1.0, text="Done!")
                st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                st.success(f"✅ Enriched {enriched}/{total} · Not found: {failed}")
                st.rerun()

            st.divider()

            # ── ENRICH + EMAIL GENERATOR ──
            company_names = df_db["company"].dropna().tolist()
            sel_col1, sel_col2 = st.columns([3, 1])
            with sel_col1:
                selected_company = st.selectbox("Select company for Enrich / Email", company_names, key="sel_company")
            
            row = df_db[df_db["company"] == selected_company].iloc[0].to_dict() if selected_company else {}

            act1, act2 = st.columns(2)

            # ── ENRICH ──
            with act1:
                st.markdown("**🔍 Enrich contacts**")
                has_sd = bool(st.secrets.get("SCRAPINGDOG_API_KEY",""))
                st.caption(f"{'🐕 ScrapingDog + Claude (JS render)' if has_sd else '⚠️ web_search only — add SCRAPINGDOG_API_KEY for better results'}")
                if st.button("🔍 Find contacts", key="enrich_btn", use_container_width=True):
                    with st.status(f"Enriching {selected_company}...", expanded=True) as enrich_status:
                        try:
                            found = {}
                            company_url = row.get("url","")
                            company_addr = row.get("address","")

                            if has_sd and company_url:
                                enrich_status.write(f"🐕 ScrapingDog → {company_url}/contact ...")
                                found = enrich_with_scrapingdog(selected_company, company_url, company_addr)

                            # fallback: web_search if ScrapingDog found nothing
                            if not any(found.values()):
                                enrich_status.write("🔍 Fallback: web_search...")
                                client = get_anthropic_client()
                                enrich_prompt = (
                                    f"Find direct contact info for: {selected_company}\n"
                                    f"Website: {company_url}\nAddress: {company_addr}\n\n"
                                    f"Search /contact page, Alibaba, Made-in-China, LinkedIn.\n"
                                    f"Return ONLY JSON: {{\"email\":\"\",\"phone\":\"\",\"whatsapp\":\"\",\"contact_person\":\"\"}}"
                                )
                                resp = client.messages.create(
                                    model=MODEL, max_tokens=400,
                                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                                    messages=[{"role": "user", "content": enrich_prompt}]
                                )
                                txt = " ".join(b.text for b in resp.content if b.type == "text")
                                m = re.search(r"\{[^{}]*\}", txt)
                                if m:
                                    found = {k: clean_contact(v) for k, v in json.loads(m.group(0)).items()}

                            updates = {k: v for k, v in found.items() if v}
                            if updates and "id" in row:
                                with get_db() as conn:
                                    with conn.cursor() as cur:
                                        for col, val in updates.items():
                                            cur.execute(f"UPDATE merino_suppliers SET {col}=%s WHERE id=%s",
                                                        (val, int(row["id"])))
                                    conn.commit()
                                st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                                enrich_status.update(label=f"✅ Found: {updates}", state="complete")
                                st.rerun()
                            else:
                                enrich_status.update(label="⚠️ No contacts found", state="complete")
                        except Exception as e:
                            enrich_status.update(label=f"❌ {e}", state="error")

            # ── EMAIL GENERATOR ──
            with act2:
                st.markdown("**✉️ Email generator**")
                st.caption("AI writes outreach email based on supplier profile")
                email_lang = st.selectbox("Language", ["English", "Chinese (中文)"], key="email_lang")
                if st.button("✉️ Generate email", key="email_btn", use_container_width=True):
                    with st.spinner("Writing email..."):
                        try:
                            client = get_anthropic_client()
                            lang_note = "Write in Chinese (Mandarin)" if "Chinese" in email_lang else "Write in English"
                            email_prompt = (
                                f"Write a professional B2B outreach email to a merino wool supplier.\n"
                                f"Our brand: merino.tech — premium merino wool clothing, Amazon FBA, US/EU markets.\n\n"
                                f"Supplier: {selected_company}\n"
                                f"Products: {row.get('products','')}\n"
                                f"Certs: {row.get('certs','')}\n"
                                f"Contact: {row.get('contact_person', 'Sales Team')}\n\n"
                                f"{lang_note}. Keep it concise (150-200 words). Ask about MOQ, pricing, samples.\n"
                                f"Subject line included. Professional but friendly tone."
                            )
                            resp = client.messages.create(
                                model=MODEL, max_tokens=600,
                                messages=[{"role": "user", "content": email_prompt}]
                            )
                            email_text = resp.content[0].text
                            st.session_state["_email_out"] = email_text
                        except Exception as e:
                            st.error(str(e))

                if st.session_state.get("_email_out"):
                    # parse subject + body
                    email_raw = st.session_state["_email_out"]
                    subj_match = re.search(r"Subject[:\s]+(.+)", email_raw, re.IGNORECASE)
                    default_subj = subj_match.group(1).strip() if subj_match else f"Partnership inquiry — merino.tech"
                    body_clean = re.sub(r"Subject[:\s]+.+\n?", "", email_raw, flags=re.IGNORECASE).strip()

                    e1, e2 = st.columns(2)
                    with e1:
                        send_from = st.text_input("From", value=st.secrets.get("SMTP_FROM",""), key="send_from")
                    with e2:
                        supplier_email = row.get("email","")
                        send_to = st.text_input("To", value=supplier_email, key="send_to")

                    send_subj = st.text_input("Subject", value=default_subj, key="send_subj")
                    send_body = st.text_area("Email body", value=body_clean, height=240, key="send_body")

                    sc1, sc2 = st.columns([1,3])
                    with sc1:
                        if st.button("📤 Send email", key="send_btn", type="primary",
                                     use_container_width=True, disabled=not send_to):
                            try:
                                import smtplib
                                from email.mime.text import MIMEText
                                from email.mime.multipart import MIMEMultipart

                                smtp_host = st.secrets.get("SMTP_HOST", "smtp.gmail.com")
                                smtp_port = int(st.secrets.get("SMTP_PORT", 587))
                                smtp_user = st.secrets.get("SMTP_USER", send_from)
                                smtp_pass = st.secrets.get("SMTP_PASS", "")

                                msg = MIMEMultipart()
                                msg["From"]    = send_from
                                msg["To"]      = send_to
                                msg["Subject"] = send_subj
                                msg.attach(MIMEText(send_body, "plain", "utf-8"))

                                with smtplib.SMTP(smtp_host, smtp_port) as server:
                                    server.starttls()
                                    server.login(smtp_user, smtp_pass)
                                    server.sendmail(send_from, send_to.split(","), msg.as_string())

                                # update status to Contacted
                                if "id" in row:
                                    with get_db() as conn:
                                        with conn.cursor() as cur:
                                            cur.execute(
                                                "UPDATE merino_suppliers SET status='Contacted' WHERE id=%s",
                                                (int(row["id"]),)
                                            )
                                        conn.commit()
                                    st.session_state["db_rev"] = st.session_state.get("db_rev",0)+1

                                st.success(f"✅ Email sent to {send_to}! Status → Contacted")
                                st.session_state.pop("_email_out", None)
                                st.rerun()

                            except Exception as e:
                                st.error(f"Send error: {e}")
                    with sc2:
                        st.caption("💡 Add SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM to Streamlit Secrets")

    except Exception as e:
        st.error(f"DB read error: {e}")

with tab4:
    try:
        df_arch = load_archived()
        if df_arch.empty:
            st.info("Archive is empty. Set status **🗄️ Archived** on any supplier to move it here.")
        else:
            st.caption(f"**{len(df_arch)}** archived suppliers")
            # restore button
            ra1, ra2 = st.columns([2, 4])
            with ra1:
                restore_co = st.selectbox("Restore company", ["—"] + df_arch["company"].dropna().tolist(), key="restore_sel")
            if restore_co != "—":
                with ra1:
                    if st.button(f"↩️ Restore: {restore_co[:25]}", key="restore_btn"):
                        rid = df_arch[df_arch["company"] == restore_co]["id"].iloc[0]
                        with get_db() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE merino_suppliers SET status='New' WHERE id=%s", (int(rid),))
                            conn.commit()
                        st.session_state["db_rev"] = st.session_state.get("db_rev",0) + 1
                        st.toast(f"Restored: {restore_co}")
                        st.rerun()
            df_arch2 = df_arch.copy()
            df_arch2["region"] = df_arch2["address"].fillna("").apply(region_flag)
            show = [c for c in ["region","company","url","email","phone","products","certs","priority","created_at"] if c in df_arch2.columns]
            st.dataframe(df_arch2[show], use_container_width=True, height=500, hide_index=True,
                         column_config={"url": st.column_config.LinkColumn()})
    except Exception as e:
        st.error(f"Archive error: {e}")

with tab5:
    st.markdown("**📥 Import from document / text**")
    st.caption("Вставь текст с описанием поставщиков — AI извлечёт компании, email, телефоны и добавит в базу")

    imp_text = st.text_area("Paste text here", height=200, placeholder="Вставь любой текст: отчёт, статью, список поставщиков...", key="import_text")

    imp_col1, imp_col2 = st.columns([1, 4])
    with imp_col1:
        import_btn = st.button("🤖 Extract & Import", type="primary", use_container_width=True, disabled=not imp_text.strip())

    if import_btn and imp_text.strip():
        with st.status("Extracting suppliers from text...", expanded=True) as imp_status:
            try:
                imp_status.write("🤖 Claude reading document...")
                client = get_anthropic_client()
                extract_prompt = f"""Extract all supplier/manufacturer/company information from this text.

Text:
{imp_text[:12000]}

For each company found, return a JSON object with:
company, url, email, phone, whatsapp, address, contact_person, description, products, certs, moq, priority

priority = HIGH (has direct email+phone) | MEDIUM (has email or phone) | LOW (no contacts)
Missing fields = empty string "".

Return ONLY a valid JSON array starting with ["""

                resp = client.messages.create(
                    model=MODEL, max_tokens=4000,
                    messages=[
                        {"role": "user", "content": extract_prompt},
                        {"role": "assistant", "content": "["},
                    ],
                )
                raw = " ".join(b.text for b in resp.content if b.type == "text")
                full = "[" + raw
                m = re.search(r"\[[\s\S]*\]", full)
                if not m:
                    imp_status.update(label="❌ Could not parse response", state="error")
                else:
                    parsed = json.loads(m.group(0))
                    if not isinstance(parsed, list):
                        parsed = [parsed]

                    # dedup against DB
                    try:
                        df_ex = load_from_db()
                        existing_db = set((df_ex["company"].fillna("") + df_ex["url"].fillna("")).str.lower())
                    except Exception:
                        existing_db = set()

                    fresh = [clean_row(r) for r in parsed
                             if (r.get("company","") + r.get("url","")).lower() not in existing_db]

                    if fresh:
                        inserted = save_to_db(fresh, "Import", "manual")
                        st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                        imp_status.update(label=f"✅ Extracted {len(parsed)} · Added {len(fresh)} new to DB", state="complete")
                        st.success(f"✅ Added **{len(fresh)}** suppliers to Database")

                        # preview
                        df_prev = pd.DataFrame(fresh)
                        show = [c for c in ["company","email","phone","address","products","priority"] if c in df_prev.columns]
                        st.dataframe(df_prev[show], use_container_width=True, hide_index=True)
                    else:
                        imp_status.update(label="⚠️ All companies already in DB", state="complete")
                        st.info("All extracted companies already exist in the database.")

            except Exception as e:
                imp_status.update(label=f"❌ {e}", state="error")
                st.error(str(e))

    # ── REVIEW & SELECTIVE IMPORT ──────────────────────────────────────────────
    candidates = st.session_state.get("_import_candidates", [])
    if candidates:
        st.divider()
        st.markdown(f"**Review {len(candidates)} extracted suppliers — select which to import:**")

        df_cand = pd.DataFrame(candidates)
        df_cand.insert(0, "✅ Import", True)

        # filters
        fc1, fc2, fc3 = st.columns([1.5, 1.5, 2])
        with fc1:
            fi_region = st.selectbox("Filter region", ["All"] + sorted(df_cand["address"].fillna("").unique().tolist()), key="fi_region")
        with fc2:
            fi_pri = st.selectbox("Filter priority", ["All","HIGH","MEDIUM","LOW"], key="fi_pri")
        with fc3:
            fi_search = st.text_input("Search company", key="fi_search", placeholder="type to filter...")

        mask = pd.Series([True]*len(df_cand))
        if fi_region != "All": mask &= df_cand["address"].fillna("") == fi_region
        if fi_pri    != "All": mask &= df_cand["priority"].fillna("") == fi_pri
        if fi_search: mask &= df_cand["company"].fillna("").str.lower().str.contains(fi_search.lower())
        df_filtered = df_cand[mask].copy()

        show_imp = [c for c in ["✅ Import","company","email","phone","url","address","products","priority"] if c in df_filtered.columns]
        edited_imp = st.data_editor(
            df_filtered[show_imp], use_container_width=True, height=400, hide_index=True,
            key="import_editor",
            column_config={
                "✅ Import": st.column_config.CheckboxColumn("Import?", width="small"),
                "url": st.column_config.LinkColumn(),
                "priority": st.column_config.TextColumn("Pri", width="small"),
            }
        )

        sel_count = edited_imp["✅ Import"].sum() if "✅ Import" in edited_imp.columns else 0
        ib1, ib2, _ = st.columns([1.5, 1.5, 4])
        with ib1:
            confirm_btn = st.button(f"💾 Import selected ({sel_count})", type="primary", use_container_width=True, disabled=sel_count==0)
        with ib2:
            if st.button("✕ Discard all", use_container_width=True):
                st.session_state.pop("_import_candidates", None)
                st.rerun()

        if confirm_btn:
            to_import = edited_imp[edited_imp["✅ Import"] == True].drop(columns=["✅ Import"]).to_dict("records")
            # merge back full data from candidates
            full_rows = []
            for row in to_import:
                orig = next((c for c in candidates if c.get("company") == row.get("company")), row)
                full_rows.append(clean_row(orig))
            if full_rows:
                inserted = save_to_db(full_rows, "Import", "manual")
                st.session_state["db_rev"] = st.session_state.get("db_rev", 0) + 1
                st.session_state.pop("_import_candidates", None)
                st.success(f"✅ Imported **{len(full_rows)}** suppliers to Database!")
                st.rerun()

with tab3:
    try:
        df_ch = load_from_db()
        if df_ch.empty:
            st.info("No data yet.")
        else:
            df_ch["region"] = df_ch["address"].fillna("").apply(region_flag)
            df_ch["has_contact"] = (
                df_ch["email"].fillna("").str.len() +
                df_ch["phone"].fillna("").str.len() > 0
            ).map({True: "✅ With contacts", False: "❌ No contacts"})

            PIE_COLORS = ["#16a34a","#2563eb","#d97706","#9333ea","#dc2626","#0891b2","#ca8a04","#9ca3af"]

            c1, c2 = st.columns(2)
            with c1:
                reg = df_ch["region"].value_counts().reset_index()
                reg.columns = ["Region", "Count"]
                fig = px.pie(reg, names="Region", values="Count",
                             title=f"By region  ({len(df_ch)} total)",
                             color_discrete_sequence=PIE_COLORS,
                             hole=0.35)
                fig.update_traces(textposition="inside", textinfo="percent+label")
                fig.update_layout(showlegend=False, margin=dict(t=50,b=10,l=10,r=10))
                st.plotly_chart(fig, use_container_width=True)

            with c2:
                pri_colors = {"HIGH":"#16a34a","MEDIUM":"#d97706","LOW":"#9ca3af","UNKNOWN":"#e5e7eb"}
                pri = df_ch["priority"].fillna("UNKNOWN").value_counts().reset_index()
                pri.columns = ["Priority","Count"]
                fig2 = px.pie(pri, names="Priority", values="Count",
                              title="By priority",
                              color="Priority",
                              color_discrete_map=pri_colors,
                              hole=0.35)
                fig2.update_traces(textposition="inside", textinfo="percent+label")
                fig2.update_layout(showlegend=False, margin=dict(t=50,b=10,l=10,r=10))
                st.plotly_chart(fig2, use_container_width=True)

            c3, c4 = st.columns(2)
            with c3:
                contact_df = df_ch["has_contact"].value_counts().reset_index()
                contact_df.columns = ["Status","Count"]
                fig3 = px.pie(contact_df, names="Status", values="Count",
                              title="With contacts vs without",
                              color_discrete_sequence=["#16a34a","#e5e7eb"],
                              hole=0.35)
                fig3.update_traces(textposition="inside", textinfo="percent+label")
                fig3.update_layout(showlegend=False, margin=dict(t=50,b=10,l=10,r=10))
                st.plotly_chart(fig3, use_container_width=True)

            with c4:
                prod = df_ch["search_product"].fillna("unknown").value_counts().reset_index()
                prod.columns = ["Product","Count"]
                fig4 = px.bar(prod, x="Count", y="Product", orientation="h",
                              title="By product searched",
                              color_discrete_sequence=["#2563eb"])
                fig4.update_layout(yaxis=dict(autorange="reversed"),
                                   margin=dict(t=50,b=10,l=10,r=10),
                                   plot_bgcolor="white")
                st.plotly_chart(fig4, use_container_width=True)
    except Exception as e:
        st.error(f"Chart error: {e}")
