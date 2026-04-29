import streamlit as st
import anthropic
import json
import re
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pandas as pd

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

def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(creds)

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
    return len(fresh)


# ── WRITE TO SHEETS ───────────────────────────────────────────────────────────
def write_to_sheets():
    if not st.session_state.results:
        add_log("No data to write", "warn")
        return

    tab_name = datetime.now().strftime("Search %d.%m %H:%M")
    add_log(f'Creating sheet tab "{tab_name}"...', "blue")

    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    # create new worksheet
    ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(COLS))

    # write header + data
    rows = [HEADERS]
    for r in st.session_state.results:
        rows.append([str(r.get(c, "") or "") for c in COLS])

    ws.update("A1", rows)

    # format header row: bold + green background
    ws.format("A1:L1", {
        "backgroundColor": {"red": 0.94, "green": 0.99, "blue": 0.95},
        "textFormat": {"bold": True},
    })

    # freeze header
    spreadsheet.batch_update({
        "requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        }]
    })

    add_log(
        f'**{len(st.session_state.results)}** rows written → '
        f'[Open sheet](https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID})',
        "blue"
    )


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
    sheets_btn = st.button("📊 Save to Google Sheets → new tab", use_container_width=True)

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

# ── WRITE TO SHEETS ───────────────────────────────────────────────────────────
if sheets_btn:
    with st.spinner("Writing to Google Sheets..."):
        try:
            write_to_sheets()
            st.success("Done! Data written to new sheet tab.")
        except Exception as e:
            st.error(f"Sheets error: {e}")
            add_log(str(e), "error")

# ── LOG ───────────────────────────────────────────────────────────────────────
if st.session_state.log:
    with st.expander("📋 Log", expanded=True):
        for line in reversed(st.session_state.log[-15:]):
            st.markdown(line)

# ── RESULTS TABLE ─────────────────────────────────────────────────────────────
if st.session_state.results:
    df = pd.DataFrame(st.session_state.results)

    # add region column
    df.insert(0, "region", df.get("address", pd.Series([""] * len(df))).apply(region_flag))

    # reorder + rename
    display_cols = ["region"] + [c for c in COLS if c in df.columns]
    df = df[display_cols]
    df.columns = ["Region"] + [h for h, c in zip(HEADERS, COLS) if c in st.session_state.results[0]]

    st.dataframe(
        df,
        use_container_width=True,
        height=600,
        column_config={
            "Region": st.column_config.TextColumn(width="small"),
            "Company": st.column_config.TextColumn(width="medium"),
            "URL": st.column_config.LinkColumn(width="medium"),
            "Email": st.column_config.TextColumn(width="medium"),
            "Phone": st.column_config.TextColumn(width="small"),
            "WhatsApp": st.column_config.TextColumn(width="small"),
            "Priority": st.column_config.TextColumn(width="small"),
            "Description": st.column_config.TextColumn(width="large"),
        },
        hide_index=True,
    )
else:
    st.info("No results yet — select Country + Product and click **▶ Search**")
