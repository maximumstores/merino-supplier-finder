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
def run_search(country: str, product: str, extra: str, status_box=None):
    where = "globally" if country == "All countries" else f"in {country}"
    user_msg = (
        f"Find merino wool {product} manufacturers {where}. "
        f"{('Extra requirements: ' + extra) if extra else ''} "
        f"Need OEM/ODM factories with direct contacts (email, phone, WhatsApp). "
        f"Search Alibaba, Made-in-China, GlobalSources, company websites. "
        f"For each supplier: visit their website contact page and find direct email/phone. "
        f"Return minimum 8–12 suppliers with as many direct contacts as possible."
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
                    cb = getattr(event, "content_block", None)
                    if cb and cb.type == "tool_use":
                        search_count += 1
                        if status_box:
                            status_box.update(label=f"🔍 Web search #{search_count}...")
                        add_log(f"🔍 Web search #{search_count}")
                    elif cb and cb.type == "text":
                        if status_box:
                            status_box.update(label="✍️ Generating results...")
        msg = stream.get_final_message()

    if search_count:
        add_log(f"Web searches performed: **{search_count}**")

    # collect text
    for block in msg.content:
        if block.type == "text":
            full_text += block.text

    # parse JSON — multiple fallback strategies
    add_log(f"Response length: {len(full_text)} chars")

    parsed = None

    # strategy 1: find JSON array directly
    m = re.search(r"\[[\s\S]*\]", full_text)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            pass

    # strategy 2: strip markdown fences
    if parsed is None:
        cleaned = re.sub(r"```(?:json)?\s*", "", full_text).replace("```", "").strip()
        m2 = re.search(r"\[[\s\S]*\]", cleaned)
        if m2:
            try:
                parsed = json.loads(m2.group(0))
            except Exception:
                pass

    # strategy 3: find first { ... } object and wrap in array
    if parsed is None:
        m3 = re.search(r"(\{[\s\S]*\})", full_text)
        if m3:
            try:
                parsed = [json.loads(m3.group(0))]
            except Exception:
                pass

    if not parsed:
        snippet = full_text[:300].replace("\n", " ")
        add_log(f"No JSON found. Response preview: {snippet}", "error")
        return 0

    if not isinstance(parsed, list):
        parsed = [parsed]

    # dedup vs session
    existing_session = {(r.get("company", "") + r.get("url", "")).lower()
                        for r in st.session_state.results}
    # dedup vs DB
    try:
        df_existing = load_from_db()
        existing_db = set((df_existing["company"].fillna("") + df_existing["url"].fillna("")).str.lower())
    except Exception:
        existing_db = set()

    already_in_db = []
    fresh = []
    for r in parsed:
        key = (r.get("company","") + r.get("url","")).lower()
        if key in existing_session:
            pass  # skip session dup
        elif key in existing_db:
            already_in_db.append(r.get("company","?"))
        else:
            fresh.append(clean_row(r))

    st.session_state.results.extend(fresh)
    msg_parts = [f"Found **{len(parsed)}** total"]
    if fresh:
        msg_parts.append(f"**{len(fresh)}** new added")
    if already_in_db:
        msg_parts.append(f"**{len(already_in_db)}** already in DB: {', '.join(already_in_db[:5])}")
    add_log(" · ".join(msg_parts))

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
    country = st.multiselect("Country", COUNTRIES[:-1], default=["China"], key="country",
                              placeholder="Select countries...")
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
    countries = country if country else ["China"]
    products  = product if product else ["base layer / thermal underwear"]
    total_new = 0
    combos = [(c, p) for c in countries for p in products]
    for i, (c, p) in enumerate(combos):
        label = f"[{i+1}/{len(combos)}] {p} / {c}"
        with st.status(label, expanded=True) as status:
            status.write(f"🚀 Starting search...")
            try:
                n = run_search(c, p, extra, status_box=status)
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
tab1, tab2, tab3 = st.tabs(["🔍 Session results", "🗄️ Database (all)", "📊 Charts"])

def render_table(df: pd.DataFrame, allow_edit: bool = False):
    """Render filtered supplier table."""
    if df.empty:
        st.info("No data.")
        return

    df = df.copy()
    df["region"] = df.get("address", pd.Series([""] * len(df))).fillna("").apply(region_flag)
    # mark rows with real contacts
    df["✉️"] = df.apply(lambda r: "✅" if (r.get("email","") or r.get("phone","") or r.get("whatsapp","")) else "—", axis=1)

    # ── FILTERS ──
    fc1, fc2, fc3, fc4 = st.columns([1.5, 1.5, 1.5, 2])
    with fc1:
        regions = ["All"] + sorted(df["region"].unique().tolist())
        f_region = st.selectbox("Region", regions, key=f"fr_{allow_edit}")
    with fc2:
        pris = ["All"] + [p for p in ["HIGH","MEDIUM","LOW"] if p in df.get("priority", pd.Series()).values]
        f_pri = st.selectbox("Priority", pris, key=f"fp_{allow_edit}")
    with fc3:
        f_contact = st.selectbox("Contacts", ["All","✅ Has contacts","— No contacts"], key=f"fc_{allow_edit}")
    with fc4:
        f_search = st.text_input("🔍 Search company / product", key=f"fs_{allow_edit}")

    # apply filters
    mask = pd.Series([True] * len(df), index=df.index)
    if f_region != "All":
        mask &= df["region"] == f_region
    if f_pri != "All":
        mask &= df.get("priority", pd.Series([""] * len(df), index=df.index)).fillna("") == f_pri
    if f_contact == "✅ Has contacts":
        mask &= df["✉️"] == "✅"
    elif f_contact == "— No contacts":
        mask &= df["✉️"] == "—"
    if f_search:
        q = f_search.lower()
        mask &= (df.get("company","").fillna("").str.lower().str.contains(q) |
                 df.get("products","").fillna("").str.lower().str.contains(q))
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

    st.caption(f"Showing **{len(df_f)}** of {len(df)} suppliers")

    show_cols = ["in_db","✉️","region","company","url","email","phone","whatsapp","products","certs","priority"]
    if "status" in df_f.columns:
        show_cols = ["✉️","region","company","status","url","email","phone","whatsapp","products","certs","priority"]
    existing = [c for c in show_cols if c in df_f.columns]

    cfg = {
        "url": st.column_config.LinkColumn(),
        "in_db": st.column_config.TextColumn("DB", width="small"),
        "✉️": st.column_config.TextColumn("📬", width="small"),
        "region": st.column_config.TextColumn("Region", width="small"),
        "company": st.column_config.TextColumn("Company", width="medium"),
        "email": st.column_config.TextColumn("Email", width="medium"),
        "phone": st.column_config.TextColumn("Phone", width="small"),
        "priority": st.column_config.TextColumn("Pri", width="small"),
    }
    if allow_edit and "status" in df_f.columns:
        cfg["status"] = st.column_config.SelectboxColumn(
            "Status", options=["New","Contacted","Replied","In progress","Rejected","Done"],
            width="small"
        )

    if allow_edit:
        edited = st.data_editor(df_f[existing], use_container_width=True, height=520,
                                hide_index=True, column_config=cfg)
        # save status changes back to DB
        if "status" in edited.columns and "id" in df_f.columns:
            changed = edited[edited["status"] != df_f.loc[edited.index,"status"]]
            if not changed.empty:
                try:
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            for idx, row in changed.iterrows():
                                orig_id = df_f.loc[idx,"id"]
                                cur.execute("UPDATE merino_suppliers SET status=%s WHERE id=%s",
                                            (row["status"], orig_id))
                        conn.commit()
                    load_from_db.clear()
                    st.toast("Status saved ✅")
                except Exception as e:
                    st.warning(f"Save error: {e}")
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
            render_table(df_db, allow_edit=True)
    except Exception as e:
        st.error(f"DB read error: {e}")

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
