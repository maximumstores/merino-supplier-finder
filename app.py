import streamlit as st
import anthropic
import json
import re
from datetime import datetime
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

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
    "China", "Vietnam", "Turkey", "India",
    "Romania", "Bulgaria", "Italy", "Portugal",
    "Poland", "Czech Republic", "Serbia", "Bangladesh",
    "Australia", "New Zealand", "All countries",
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
COLS = ["company", "url", "email", "phone", "whatsapp",
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

Return ONLY a valid JSON array. No markdown. No explanation. No code fences."""

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
            r.get("phone",""), r.get("whatsapp",""), r.get("address",""),
            r.get("contact_person",""), r.get("description",""),
            r.get("products",""), r.get("certs",""), r.get("moq",""),
            r.get("priority",""), country, product,
        )
        for r in rows
    ]
    with get_db() as conn:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO merino_suppliers
                  (company,url,email,phone,whatsapp,address,contact_person,
                   description,products,certs,moq,priority,search_country,search_product)
                VALUES %s
                ON CONFLICT (company, url) DO NOTHING
            """, values)
            inserted = cur.rowcount
        conn.commit()
    return inserted

@st.cache_data(ttl=60)
def load_from_db() -> pd.DataFrame:
    """Load all suppliers from DB."""
    with get_db() as conn:
        return pd.read_sql(
            "SELECT * FROM merino_suppliers ORDER BY created_at DESC",
            conn
        )

def region_flag(addr: str) -> str:
    a = (addr or "").lower()
    if re.search(r"china|shanghai|beijing|guangdong|jiangsu|zhejiang|ningbo|shenzhen|suzhou|guangzhou", a):
        return "🇨🇳 China"
    if re.search(r"india|mumbai|delhi|noida|panipat|bangalore|chennai", a):
        return "🇮🇳 India"
    if re.search(r"vietnam|ho chi minh|hanoi", a):
        return "🇻🇳 Vietnam"
    if re.search(r"turkey|istanbul|bursa|ankara", a):
        return "🇹🇷 Turkey"
    if re.search(r"romania|bucharest|cluj|brasov", a):
        return "🇷🇴 Romania"
    if re.search(r"bulgaria|sofia|plovdiv", a):
        return "🇧🇬 Bulgaria"
    if re.search(r"italy|italia|milan|como", a):
        return "🇮🇹 Italy"
    if re.search(r"portugal|lisbon|porto", a):
        return "🇵🇹 Portugal"
    if re.search(r"poland|warszawa|krakow|lodz", a):
        return "🇵🇱 Poland"
    if re.search(r"australia|sydney|melbourne", a):
        return "🇦🇺 Australia"
    return "🌐 Other"

# ── INIT DB ──────────────────────────────────────────────────────────────────
try:
    init_db()
except Exception as e:
    st.warning(f"DB init warning: {e}")

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
def run_search(country: str, product: str, extra: str):
    where = "globally" if country == "All countries" else f"in {country}"
    user_msg = (
        f"Find merino wool {product} manufacturers {where}. "
        f"{('Extra requirements: ' + extra) if extra else ''} "
        f"Need OEM/ODM factories with direct contacts (email, phone, WhatsApp). "
        f"Search Alibaba, Made-in-China, GlobalSources, company websites. "
        f"Return minimum 8–12 suppliers."
    )

    client = get_anthropic_client()
    add_log(f"Starting search: **{product}** / **{country}**")

    # stream with web_search tool
    search_count = 0
    full_text = ""

    with client.messages.stream(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        for event in stream:
            if hasattr(event, "type"):
                if event.type == "content_block_start":
                    if hasattr(event, "content_block") and event.content_block.type == "tool_use":
                        search_count += 1
        # get final message
        msg = stream.get_final_message()

    if search_count:
        add_log(f"Web searches performed: **{search_count}**")

    # collect text
    for block in msg.content:
        if block.type == "text":
            full_text += block.text

    # parse JSON
    m = re.search(r"\[[\s\S]*\]", full_text)
    if not m:
        add_log("No JSON array found in response", "error")
        return 0

    parsed = json.loads(m.group(0))

    # dedup
    existing = {(r.get("company", "") + r.get("url", "")).lower()
                for r in st.session_state.results}
    fresh = [r for r in parsed
             if (r.get("company", "") + r.get("url", "")).lower() not in existing]

    st.session_state.results.extend(fresh)
    add_log(f"Added **{len(fresh)}** new suppliers (duplicates skipped: {len(parsed) - len(fresh)})")

    # save to postgres
    try:
        inserted = save_to_db(fresh, country, product)
        add_log(f"Saved **{inserted}** rows → PostgreSQL")
        load_from_db.clear()
    except Exception as e:
        add_log(f"DB write warning: {e}", "warn")

    return len(fresh)




# ── UI ────────────────────────────────────────────────────────────────────────
st.markdown("## 🐑 Merino Supplier Finder")
st.caption(f"merino.tech internal · {MODEL} · web_search")
st.divider()

# ── SEARCH CONTROLS ───────────────────────────────────────────────────────────
col1, col2, col3, col4, col5 = st.columns([1.4, 1.6, 2.5, 0.9, 0.7])

with col1:
    country = st.selectbox("Country", COUNTRIES, key="country")
with col2:
    product = st.selectbox("Product", PRODUCTS, key="product")
with col3:
    extra = st.text_input("Extra requirements", placeholder="Woolmark certified, low MOQ, no mulesing...")
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
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric("Total found", len(st.session_state.results))
with m2:
    with_contacts = sum(1 for r in st.session_state.results if r.get("email") or r.get("phone"))
    st.metric("With contacts", with_contacts)
with m3:
    high = sum(1 for r in st.session_state.results if r.get("priority") == "HIGH")
    st.metric("HIGH priority", high)
with m4:
    db_refresh = st.button("🔄 Refresh DB", use_container_width=True)

if db_refresh:
    load_from_db.clear()
    st.rerun()

# ── RUN SEARCH ────────────────────────────────────────────────────────────────
if search_btn:
    with st.spinner(f"Searching for **{product}** in **{country}**..."):
        try:
            n = run_search(country, product, extra)
            if n:
                st.success(f"Found {n} new suppliers!")
        except Exception as e:
            st.error(f"Error: {e}")
            add_log(str(e), "error")


# ── LOG ───────────────────────────────────────────────────────────────────────
if st.session_state.log:
    with st.expander("📋 Log", expanded=True):
        for line in reversed(st.session_state.log[-15:]):
            st.markdown(line)

# ── RESULTS — tabs: session / database / charts ───────────────────────────────
tab1, tab2, tab3 = st.tabs(["🔍 Session results", "🗄️ Database (all)", "📊 Charts"])

with tab1:
    if st.session_state.results:
        df_s = pd.DataFrame(st.session_state.results)
        df_s.insert(0, "region", df_s.get("address", pd.Series([""] * len(df_s))).apply(region_flag))
        st.dataframe(df_s, use_container_width=True, height=500, hide_index=True,
            column_config={"url": st.column_config.LinkColumn()})
    else:
        st.info("No results yet — select Country + Product and click **▶ Search**")

with tab2:
    try:
        df_db = load_from_db()
        if df_db.empty:
            st.info("Database is empty — run a search first.")
        else:
            df_db.insert(0, "region", df_db["address"].fillna("").apply(region_flag))
            st.caption(f"Total in DB: **{len(df_db)}** suppliers")
            st.dataframe(df_db, use_container_width=True, height=500, hide_index=True,
                column_config={"url": st.column_config.LinkColumn()})
    except Exception as e:
        st.error(f"DB read error: {e}")

with tab3:
    try:
        df_ch = load_from_db()
        if df_ch.empty:
            st.info("No data yet.")
        else:
            df_ch["region"] = df_ch["address"].fillna("").apply(region_flag)
            c1, c2 = st.columns(2)

            with c1:
                st.subheader("By region")
                reg = df_ch["region"].value_counts().reset_index()
                reg.columns = ["Region", "Count"]
                st.bar_chart(reg.set_index("Region"), color="#16a34a")

            with c2:
                st.subheader("By priority")
                pri = df_ch["priority"].fillna("UNKNOWN").value_counts().reset_index()
                pri.columns = ["Priority", "Count"]
                colors = {"HIGH": "#16a34a", "MEDIUM": "#d97706", "LOW": "#9ca3af"}
                st.bar_chart(pri.set_index("Priority"))

            c3, c4 = st.columns(2)
            with c3:
                st.subheader("By product searched")
                prod = df_ch["search_product"].fillna("unknown").value_counts().reset_index()
                prod.columns = ["Product", "Count"]
                st.bar_chart(prod.set_index("Product"))

            with c4:
                st.subheader("With contacts vs without")
                df_ch["has_contact"] = (
                    df_ch["email"].fillna("").str.len() +
                    df_ch["phone"].fillna("").str.len() > 0
                )
                contact_data = df_ch["has_contact"].value_counts().reset_index()
                contact_data.columns = ["Has contact", "Count"]
                contact_data["Has contact"] = contact_data["Has contact"].map({True: "✅ Yes", False: "❌ No"})
                st.bar_chart(contact_data.set_index("Has contact"))
    except Exception as e:
        st.error(f"Chart error: {e}")
