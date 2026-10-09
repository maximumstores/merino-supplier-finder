# -*- coding: utf-8 -*-
"""Вход через Google + учёт использования Merino Supplier Finder по единому стандарту «Активность дашбордов».

* require_employee()  — пускает только @maximumstores.online. Пока в Secrets нет секции [auth],
                        дашборд открыт как раньше (закрывать сайт до настройки входа нельзя).
* log_login()         — одна строка в supplier_finder.login_log (email, logged_in_at) на каждый новый вход.
* regularity()        — «Регулярность» для Scorecard: среднее по сотрудникам доли будних дней со входом
                        (пн–пт, время Киева; сотрудники = все, кто хоть раз заходил до конца периода).
* render_activity_page() — вкладка «Активность дашборда» с блоком «Для Scorecard».
Запись журнала никогда не ломает дашборд: при ошибке она тихо пропускается.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import streamlit as st

log = logging.getLogger(__name__)

EMPLOYEE_DOMAIN = "maximumstores.online"
KYIV = ZoneInfo("Europe/Kyiv")
SCHEMA = "supplier_finder"
SSLMODE = "require"   # как в get_db() основного приложения


# ---------------------------------------------------------------- вход через Google
def auth_configured() -> bool:
    try:
        return bool(st.secrets.get("auth", {}).get("client_id"))
    except Exception:  # noqa: BLE001
        return False


def _gate(message: str = "") -> None:
    st.markdown(
        "<div style='max-width:420px;margin:12vh auto 0;text-align:center'>"
        "<img src='https://merino.tech/cdn/shop/files/MT_logo_1.png?v=1685099753&width=260' style='max-width:200px'>"
        "<h2 style='margin:8px 0 4px'>Merino Supplier Finder</h2>"
        f"<p style='color:#6e6e73;margin:0 0 18px'>{message or 'Доступ только для сотрудников @' + EMPLOYEE_DOMAIN}</p>"
        "</div>", unsafe_allow_html=True)


def require_employee() -> str:
    """Почта вошедшего сотрудника; пустая строка — вход ещё не настроен, дашборд открыт."""
    if not auth_configured():
        return ""
    user = st.user
    if not user.get("is_logged_in", False):
        _gate()
        _, mid, _ = st.columns([1, 1, 1])
        mid.button("Войти через Google", type="primary", on_click=st.login, use_container_width=True)
        st.stop()
    email = str(user.get("email") or "").strip().lower()
    if not (email.endswith("@" + EMPLOYEE_DOMAIN) and user.get("email_verified", True)):
        _gate(f"Вы вошли как {email or 'неизвестный аккаунт'}. Нужен рабочий аккаунт @{EMPLOYEE_DOMAIN}.")
        _, mid, _ = st.columns([1, 1, 1])
        mid.button("Выйти", on_click=st.logout, use_container_width=True)
        st.stop()
    return email


# ---------------------------------------------------------------- журнал входов
def _ensure(cur) -> None:
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.login_log (
        id BIGSERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        logged_in_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")


def log_login(dsn: str, email: str) -> None:
    """Пишет вход один раз за сессию браузера."""
    if not email or st.session_state.get("_login_logged"):
        return
    st.session_state["_login_logged"] = True
    try:
        with psycopg2.connect(dsn, sslmode=SSLMODE, connect_timeout=10) as conn, conn.cursor() as cur:
            _ensure(cur)
            cur.execute(f"INSERT INTO {SCHEMA}.login_log (email) VALUES (%s);", (email,))
    except Exception as exc:  # noqa: BLE001
        log.warning("login_log: вход %s не записан: %s", email, type(exc).__name__)


def load_logins(dsn: str) -> list[dict]:
    with psycopg2.connect(dsn, sslmode=SSLMODE, connect_timeout=10) as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(f"SELECT email, logged_in_at FROM {SCHEMA}.login_log ORDER BY logged_in_at DESC LIMIT 200000;")
        return [{"email": e, "logged_in_at": t} for e, t in cur.fetchall()]


# ---------------------------------------------------------------- Регулярность
def workdays(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def regularity(logins: list[dict], end: date, days: int) -> dict:
    """Регулярность за `days` дней, оканчивающихся `end` включительно (время Киева)."""
    start = end - timedelta(days=days - 1)
    wd = workdays(start, end)
    empty = {"pct": 0.0, "came": 0, "base": 0, "avg_days": 0.0, "workdays": wd, "start": start, "end": end,
             "per_user": pd.DataFrame(columns=["email", "logins", "days", "last"])}
    rows = [(r["email"], pd.Timestamp(r["logged_in_at"]).tz_convert(KYIV)) for r in logins]
    if not rows:
        return empty
    df = pd.DataFrame(rows, columns=["email", "ts"])
    df["day"] = df["ts"].dt.date
    base = sorted(set(df.loc[df["day"] <= end, "email"]))
    if not base:
        return empty
    win = df[(df["day"] >= start) & (df["day"] <= end)]
    win_wd = win[win["day"].map(lambda d: d.weekday() < 5)]
    days_by = win_wd.groupby("email")["day"].nunique().reindex(base, fill_value=0)
    share = (days_by / wd).clip(upper=1) if wd else days_by * 0
    per_user = pd.DataFrame({
        "email": base,
        "logins": [int((win["email"] == e).sum()) for e in base],
        "days": [int(days_by[e]) for e in base],
        "last": [df.loc[df["email"] == e, "ts"].max() for e in base],
    })
    return {"pct": float(share.mean() * 100), "came": int(win["email"].nunique()), "base": len(base),
            "avg_days": float(days_by.mean()), "workdays": wd, "start": start, "end": end,
            "per_user": per_user.sort_values(["days", "logins"], ascending=False)}


def weekly_table(logins: list[dict], today: date, weeks: int = 8) -> pd.DataFrame:
    monday = today - timedelta(days=today.weekday())
    rows = []
    for i in range(weeks):
        start = monday - timedelta(weeks=i)
        stop = min(start + timedelta(days=6), today)
        r = regularity(logins, stop, (stop - start).days + 1)
        rows.append({"Неделя (пн–вс)": f"{start:%d.%m} – {start + timedelta(days=6):%d.%m}",
                     "Регулярность, %": round(r["pct"]), "Зашли": f"{r['came']} из {r['base']}"})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- вкладка
def render_activity_page(dsn: str) -> None:
    st.markdown("<h2 class='sec'>📈 Активность дашборда</h2>", unsafe_allow_html=True)
    if not auth_configured():
        st.info("Вход через Google ещё не включён, поэтому неизвестно, кто открывает дашборд, и активность "
                "не записывается. Включится сам, когда в Secrets появится секция [auth].")
        return
    days = st.radio("Период", [7, 14, 30, 60], horizontal=True, format_func=lambda d: f"{d} дн.",
                    key="activity_period")
    try:
        logins = load_logins(dsn)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Журнал входов недоступен ({type(exc).__name__}).")
        return

    today = datetime.now(KYIV).date()
    cur = regularity(logins, today, days)
    prev = regularity(logins, today - timedelta(days=days), days)
    with st.container(border=True):
        st.markdown(f"##### 🎯 Для Scorecard — последние {days} дней")
        c1, c2 = st.columns([1, 3])
        c1.metric("Регулярность", f"{cur['pct']:.0f}%", f"{cur['pct'] - prev['pct']:+.0f} п.п.")
        c2.markdown(
            f"**{cur['came']} из {cur['base']}** сотрудников зашли · {cur['start']:%d.%m} – {cur['end']:%d.%m}  \n"
            f"в среднем **{cur['avg_days']:g} из {cur['workdays']}** рабочих дней  \n"
            f"{days} дней до этого — {prev['pct']:.0f}% ({prev['came']} из {prev['base']} заходили)")

    st.markdown("<h2 class='sec' style='margin-top:18px'>👥 Кто пользуется</h2>", unsafe_allow_html=True)
    pu = cur["per_user"]
    if pu.empty:
        st.info("Входов пока не было.")
    else:
        st.dataframe(pd.DataFrame({
            "Сотрудник": pu["email"].str.removesuffix("@" + EMPLOYEE_DOMAIN),
            "Входов": pu["logins"], "Дней с входом": pu["days"],
            "Последний вход": [t.strftime("%d.%m %H:%M") for t in pu["last"]],
        }), use_container_width=True, hide_index=True)

    st.markdown("<h2 class='sec' style='margin-top:18px'>% для Scorecard по неделям</h2>", unsafe_allow_html=True)
    st.dataframe(weekly_table(logins, today), use_container_width=True, hide_index=True)
    st.caption("Для Scorecard бери завершённую неделю: верхняя строка считается за прошедшие дни текущей недели.")
