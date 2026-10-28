"""
=====================================================================
 KARIGAR LEDGER  -  Garment Workshop Tracker  (Streamlit + SQLite)
=====================================================================
 Tabs:
   1. Daily Entry         -> log pieces (no rates here) + view daily log
   2. Log Kharcha         -> advance cash given to a Karigar
   3. Mamu's Hisaab       -> PIN protected: 2-week settlement, PDF /
                             WhatsApp export, and Item Rate Master
   4. Karigar Master      -> add / rename / deactivate karigars + backup

 Run locally :  pip install -r requirements.txt
                streamlit run app.py
 Data file   :  workshop_ledger.db  (auto-created next to this file)

 Mamu PIN    :  default is 1234.  Change it by adding  MAMU_PIN = "5678"
                in Streamlit secrets (see the setup guide).
=====================================================================
"""

import os
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from fpdf import FPDF
from fpdf.enums import XPos, YPos

# ---------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------
DB_PATH = "workshop_ledger.db"
DEFAULT_PIN = "1234"
NEW_ITEM_LABEL = "➕ Naya item (type karo)"
SEED_ITEMS = ["Jacket Astar", "Cotton", "Feeding", "Smoking"]  # first-run starter items
DATE_FMT = "DD/MM/YYYY"

# India time (UTC+5:30, no daylight saving). Streamlit Cloud servers run on UTC,
# so we never use date.today() directly - otherwise "today" would be wrong early morning.
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------
def today_ist() -> date:
    return datetime.now(IST).date()


def now_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def clean_name(s) -> str:
    """Trim and collapse extra spaces:  '  Jacket   Astar ' -> 'Jacket Astar'."""
    return " ".join(str(s or "").split())


def fmt(x) -> str:
    """Money / number format: 1234.0 -> '1,234'   1234.5 -> '1,234.50'."""
    s = f"{round(float(x), 2):,.2f}"
    return s[:-3] if s.endswith(".00") else s


def _as_date(d) -> date:
    return date.fromisoformat(d) if isinstance(d, str) else d


def fmt_d(d) -> str:
    return _as_date(d).strftime("%d %b %Y")


def fmt_short(d) -> str:
    return _as_date(d).strftime("%d %b")


def flash(key: str, kind: str, msg: str):
    """Store a one-time message that show_flash() displays on the next run."""
    st.session_state[key] = (kind, msg)


def show_flash(key: str):
    item = st.session_state.pop(key, None)
    if item:
        kind, msg = item
        (st.success if kind == "ok" else st.error)(msg)


# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------
@contextmanager
def db():
    """Open a short-lived connection; commit on success, rollback on error."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(sql: str, params=()) -> list:
    """Run a SELECT and return a list of dicts."""
    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def run(sql: str, params=()) -> int:
    """Run INSERT / UPDATE / DELETE and return the last inserted row id."""
    with db() as c:
        return c.execute(sql, params).lastrowid


def init_db():
    """Create all tables on first run (safe to call on every run)."""
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS karigars (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name   TEXT NOT NULL UNIQUE COLLATE NOCASE,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS items (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                rate REAL NOT NULL DEFAULT 0          -- set by Mamu only
            );
            CREATE TABLE IF NOT EXISTS work_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                work_date  TEXT NOT NULL,             -- ISO: YYYY-MM-DD
                karigar_id INTEGER NOT NULL REFERENCES karigars(id),
                item_id    INTEGER NOT NULL REFERENCES items(id),
                qty        INTEGER NOT NULL CHECK (qty > 0),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kharcha (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kharcha_date TEXT NOT NULL,
                karigar_id   INTEGER NOT NULL REFERENCES karigars(id),
                amount       REAL NOT NULL CHECK (amount > 0),
                note         TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_work_date ON work_log(work_date);
            CREATE INDEX IF NOT EXISTS idx_kh_date   ON kharcha(kharcha_date);
            """
        )
        # Seed starter items once (PRAGMA user_version is our "already seeded" flag)
        if c.execute("PRAGMA user_version").fetchone()[0] < 1:
            c.executemany(
                "INSERT OR IGNORE INTO items(name, rate) VALUES (?, 0)",
                [(n,) for n in SEED_ITEMS],
            )
            c.execute("PRAGMA user_version = 1")


def get_or_create_item(name: str):
    """Return (item_id, canonical_name). New items start with rate = 0 (Mamu sets it later)."""
    rows = q("SELECT id, name FROM items WHERE name = ?", (name,))  # case-insensitive
    if rows:
        return rows[0]["id"], rows[0]["name"]
    return run("INSERT INTO items(name, rate) VALUES (?, 0)", (name,)), name


def get_item_names() -> list:
    """Item names, most-used first (so the daily dropdown is quick)."""
    rows = q(
        """
        SELECT i.name FROM items i
        LEFT JOIN (SELECT item_id, COUNT(*) AS c FROM work_log GROUP BY item_id) u
               ON u.item_id = i.id
        ORDER BY COALESCE(u.c, 0) DESC, i.name
        """
    )
    return [r["name"] for r in rows]


def get_pin() -> str:
    try:
        return str(st.secrets["MAMU_PIN"])
    except Exception:
        return DEFAULT_PIN


# ---------------------------------------------------------------------
# SETTLEMENT LOGIC (pure data, no UI)
# ---------------------------------------------------------------------
def settlement_data(start_iso: str, end_iso: str) -> list:
    """
    For each karigar who has work or kharcha between start & end (inclusive):
      lines    -> item-wise qty x current rate = amount
      gross    -> total earnings
      kharcha  -> total advance taken
      net      -> gross - kharcha   (Baki Paisa; negative = karigar took extra)
    NOTE: uses the CURRENT item rates.
    """
    work = q(
        """
        SELECT k.id AS kid, k.name AS karigar, i.name AS item, i.rate AS rate,
               SUM(w.qty) AS qty
        FROM work_log w
        JOIN karigars k ON k.id = w.karigar_id
        JOIN items i    ON i.id = w.item_id
        WHERE w.work_date BETWEEN ? AND ?
        GROUP BY k.id, i.id
        ORDER BY k.name, i.name
        """,
        (start_iso, end_iso),
    )
    adv = q(
        """
        SELECT k.id AS kid, k.name AS karigar, h.kharcha_date AS d,
               h.amount AS amount, h.note AS note
        FROM kharcha h
        JOIN karigars k ON k.id = h.karigar_id
        WHERE h.kharcha_date BETWEEN ? AND ?
        ORDER BY h.kharcha_date, h.id
        """,
        (start_iso, end_iso),
    )

    out = {}

    def entry(kid, name):
        return out.setdefault(
            kid,
            {"name": name, "lines": [], "kh_lines": [], "pieces": 0,
             "gross": 0.0, "kharcha": 0.0, "net": 0.0},
        )

    for r in work:
        e = entry(r["kid"], r["karigar"])
        amount = r["qty"] * r["rate"]
        e["lines"].append({"item": r["item"], "qty": r["qty"],
                           "rate": r["rate"], "amount": amount})
        e["pieces"] += r["qty"]
        e["gross"] += amount
    for r in adv:
        e = entry(r["kid"], r["karigar"])
        e["kh_lines"].append({"date": r["d"], "amount": r["amount"], "note": r["note"]})
        e["kharcha"] += r["amount"]
    for e in out.values():
        e["net"] = round(e["gross"] - e["kharcha"], 2)
    return sorted(out.values(), key=lambda e: e["name"].lower())


def whatsapp_text(e: dict, start, end) -> str:
    """Plain-text summary (WhatsApp formatting: *bold*)."""
    L = [f"*Hisaab - {e['name']}*", f"Period: {fmt_d(start)} to {fmt_d(end)}", ""]
    for l in e["lines"]:
        L.append(f"{l['item']}: {l['qty']} x ₹{fmt(l['rate'])} = ₹{fmt(l['amount'])}")
    L += ["", f"Total pieces: {e['pieces']}", f"Gross earning: ₹{fmt(e['gross'])}"]
    if e["kh_lines"]:
        L += ["", "Kharcha / Advance liya:"]
        for k in e["kh_lines"]:
            note = f" ({k['note']})" if k["note"] else ""
            L.append(f"• {fmt_short(k['date'])}: ₹{fmt(k['amount'])}{note}")
    L.append(f"Total kharcha: -₹{fmt(e['kharcha'])}")
    L.append("")
    if e["net"] >= 0:
        L.append(f"*NET PAYABLE (Baki): ₹{fmt(e['net'])}*")
    else:
        L.append(f"*Zyada advance liya: ₹{fmt(-e['net'])} (agle hisaab me adjust)*")
    return "\n".join(L)


def wa_url(text: str) -> str:
    """wa.me link - opens WhatsApp with the text ready; you pick the contact."""
    return "https://wa.me/?text=" + urllib.parse.quote(text)


# ---------------------------------------------------------------------
# PDF EXPORT (fpdf2).  Built-in PDF fonts only support Latin characters,
# so the rupee sign becomes "Rs." and Hindi (Devanagari) names show as "?".
# Tip: type Karigar names in English letters (Ramesh, Imran...).
# ---------------------------------------------------------------------
def pdf_txt(s) -> str:
    return str(s).replace("₹", "Rs.").encode("latin-1", "replace").decode("latin-1")


def _pdf_row(pdf, widths, cells, aligns, bold=False, fill=None, size=11):
    pdf.set_font("Helvetica", "B" if bold else "", size)
    if fill:
        pdf.set_fill_color(*fill)
    last = len(cells) - 1
    for i, (w, txt) in enumerate(zip(widths, cells)):
        pdf.cell(
            w, 8, pdf_txt(txt), border=1, fill=bool(fill), align=aligns[i],
            new_x=XPos.LMARGIN if i == last else XPos.RIGHT,
            new_y=YPos.NEXT if i == last else YPos.TOP,
        )


def _pdf_line(pdf, text, size=11, bold=False, align="L", gap=0):
    pdf.set_font("Helvetica", "B" if bold else "", size)
    pdf.cell(0, 8, pdf_txt(text), align=align, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if gap:
        pdf.ln(gap)


def build_pdf(entries: list, start, end) -> bytes:
    """One A4 page (or more) per karigar."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title("Karigar Hisaab")
    W = [95, 25, 30, 40]  # item table (total 190 mm)

    for e in entries:
        pdf.add_page()
        _pdf_line(pdf, "Workshop Hisaab", size=18, bold=True, align="C")
        _pdf_line(pdf, f"Period: {fmt_d(start)} to {fmt_d(end)}", align="C", gap=4)
        _pdf_line(pdf, f"Karigar: {e['name']}", size=14, bold=True, gap=2)

        # --- Work table
        _pdf_row(pdf, W, ["Item", "Qty", "Rate (Rs.)", "Amount (Rs.)"], "LRRR",
                 bold=True, fill=(225, 225, 225))
        for l in e["lines"]:
            _pdf_row(pdf, W, [l["item"], l["qty"], fmt(l["rate"]), fmt(l["amount"])], "LRRR")
        _pdf_row(pdf, W, ["TOTAL", e["pieces"], "", fmt(e["gross"])], "LRRR",
                 bold=True, fill=(240, 240, 240))
        pdf.ln(5)

        # --- Kharcha table
        if e["kh_lines"]:
            _pdf_line(pdf, "Kharcha / Advance given", size=12, bold=True, gap=1)
            KW = [35, 115, 40]
            _pdf_row(pdf, KW, ["Date", "Note", "Amount (Rs.)"], "LLR",
                     bold=True, fill=(225, 225, 225))
            for k in e["kh_lines"]:
                _pdf_row(pdf, KW, [fmt_d(k["date"]), k["note"], fmt(k["amount"])], "LLR")
            _pdf_row(pdf, KW, ["", "TOTAL KHARCHA", fmt(e["kharcha"])], "LLR",
                     bold=True, fill=(240, 240, 240))
            pdf.ln(5)

        # --- Final settlement
        S = [150, 40]
        _pdf_row(pdf, S, ["Gross Earnings", fmt(e["gross"])], "LR", bold=True)
        _pdf_row(pdf, S, ["Less: Kharcha / Advance", "- " + fmt(e["kharcha"])], "LR", bold=True)
        if e["net"] >= 0:
            _pdf_row(pdf, S, ["NET PAYABLE (Baki Paisa)", fmt(e["net"])], "LR",
                     bold=True, fill=(200, 235, 200), size=13)
        else:
            _pdf_row(pdf, S, ["EXTRA ADVANCE TAKEN (adjust next time)", fmt(-e["net"])], "LR",
                     bold=True, fill=(245, 200, 200), size=13)
        pdf.ln(6)
        _pdf_line(pdf, f"Generated on {fmt_d(today_ist())}", size=9)

    return bytes(pdf.output())


# ---------------------------------------------------------------------
# CALLBACKS (run BEFORE the page redraws, so we can safely reset inputs)
# ---------------------------------------------------------------------
def save_work():
    s = st.session_state
    karigar, choice = s.get("w_karigar"), s.get("w_item")
    qty = int(s.get("w_qty") or 0)

    if not karigar:
        flash("flash_work", "err", "⚠️ Pehle Karigar select karo.")
        return
    if not choice:
        flash("flash_work", "err", "⚠️ Item select karo ya naya item type karo.")
        return
    item_name = clean_name(s.get("w_new_item")) if choice == NEW_ITEM_LABEL else choice
    if not item_name:
        flash("flash_work", "err", "⚠️ Naye item ka naam likho.")
        return
    if qty <= 0:
        flash("flash_work", "err", "⚠️ Piece quantity 0 se zyada honi chahiye.")
        return

    kid = q("SELECT id FROM karigars WHERE name = ?", (karigar,))[0]["id"]
    item_id, item_name = get_or_create_item(item_name)
    run(
        "INSERT INTO work_log(work_date, karigar_id, item_id, qty, created_at) VALUES (?,?,?,?,?)",
        (s["w_date"].isoformat(), kid, item_id, qty, now_str()),
    )
    # keep Date + Karigar selected (you usually enter many items in a row); reset the rest
    s["w_item"] = item_name
    s["w_new_item"] = ""
    s["w_qty"] = 0
    flash("flash_work", "ok", f"✅ Saved: {karigar} - {item_name} x {qty}")


def save_kharcha():
    s = st.session_state
    karigar = s.get("k_karigar")
    amount = float(s.get("k_amount") or 0)
    if not karigar:
        flash("flash_kh", "err", "⚠️ Pehle Karigar select karo.")
        return
    if amount <= 0:
        flash("flash_kh", "err", "⚠️ Amount 0 se zyada hona chahiye.")
        return
    kid = q("SELECT id FROM karigars WHERE name = ?", (karigar,))[0]["id"]
    run(
        "INSERT INTO kharcha(kharcha_date, karigar_id, amount, note, created_at) VALUES (?,?,?,?,?)",
        (s["k_date"].isoformat(), kid, amount, clean_name(s.get("k_note")), now_str()),
    )
    s["k_amount"] = 0
    s["k_note"] = ""
    flash("flash_kh", "ok", f"✅ Kharcha saved: {karigar} - ₹{fmt(amount)}")


def delete_entry(table: str, state_key: str):
    assert table in ("work_log", "kharcha")
    label = st.session_state.get(state_key)
    if not label:
        flash("flash_log", "err", "⚠️ Pehle delete karne wali entry chuno.")
        return
    entry_id = int(label.split()[0].lstrip("#"))  # labels look like "#12 • Ramesh • ..."
    run(f"DELETE FROM {table} WHERE id = ?", (entry_id,))
    st.session_state[state_key] = None
    flash("flash_log", "ok", "🗑️ Entry delete ho gayi.")


def add_karigar():
    name = clean_name(st.session_state.get("km_new"))
    if not name:
        flash("flash_km", "err", "⚠️ Karigar ka naam likho.")
        return
    try:
        run("INSERT INTO karigars(name) VALUES (?)", (name,))
        st.session_state["km_new"] = ""
        flash("flash_km", "ok", f"✅ '{name}' add ho gaya.")
    except sqlite3.IntegrityError:
        flash("flash_km", "err", f"⚠️ '{name}' pehle se list me hai.")


def try_unlock():
    if st.session_state.get("pin_input", "") == get_pin():
        st.session_state["mamu_ok"] = True
    else:
        flash("flash_pin", "err", "❌ Galat PIN.")
    st.session_state["pin_input"] = ""


def lock_mamu():
    st.session_state["mamu_ok"] = False


# ---------------------------------------------------------------------
# TAB 1: DAILY ENTRY  (+ daily log)
# ---------------------------------------------------------------------
def render_daily_entry():
    st.subheader("📝 Daily Kaam Entry")
    karigars = [r["name"] for r in q("SELECT name FROM karigars WHERE active = 1 ORDER BY name")]

    if not karigars:
        st.info("Pehle **Karigar Master** tab me karigar add karo.")
    else:
        show_flash("flash_work")
        st.date_input("📅 Date", value=today_ist(), key="w_date", format=DATE_FMT)
        st.selectbox("👷 Karigar", karigars, index=None,
                     placeholder="Karigar chuno...", key="w_karigar")
        st.selectbox("🧵 Item / Kaam  (type karke search kar sakte ho)",
                     get_item_names() + [NEW_ITEM_LABEL], index=None,
                     placeholder="Item chuno...", key="w_item")
        if st.session_state.get("w_item") == NEW_ITEM_LABEL:
            st.text_input("Naye item ka naam", key="w_new_item",
                          placeholder="e.g. Jacket Astar")
        st.number_input("🔢 Piece Quantity", min_value=0, step=1, key="w_qty")
        st.button("✅ Save Entry", type="primary", on_click=save_work, key="btn_save_work")

    st.divider()
    render_daily_log()


def render_daily_log():
    st.subheader("📋 Daily Log")
    show_flash("flash_log")
    day = st.date_input("Kis din ka log dekhna hai?", value=today_ist(),
                        key="log_date", format=DATE_FMT)
    iso = day.isoformat()

    work = q(
        """
        SELECT w.id AS id, k.name AS Karigar, i.name AS Item, w.qty AS Qty
        FROM work_log w JOIN karigars k ON k.id = w.karigar_id
                        JOIN items i    ON i.id = w.item_id
        WHERE w.work_date = ? ORDER BY k.name, i.name, w.id
        """, (iso,))
    kh = q(
        """
        SELECT h.id AS id, k.name AS Karigar, h.amount AS Amount, h.note AS Note
        FROM kharcha h JOIN karigars k ON k.id = h.karigar_id
        WHERE h.kharcha_date = ? ORDER BY k.name, h.id
        """, (iso,))

    c1, c2 = st.columns(2)
    c1.metric("Total Pieces", sum(r["Qty"] for r in work))
    c2.metric("Total Kharcha", f"₹{fmt(sum(r['Amount'] for r in kh))}")

    if not work and not kh:
        st.info("Is date par koi entry nahi hai.")
    else:
        # Karigar-wise summary for the day
        by_k = {}
        for r in work:
            by_k.setdefault(r["Karigar"], [0, 0.0])[0] += r["Qty"]
        for r in kh:
            by_k.setdefault(r["Karigar"], [0, 0.0])[1] += r["Amount"]
        st.markdown("**Karigar-wise summary**")
        st.dataframe(
            pd.DataFrame([{"Karigar": k, "Pieces": v[0], "Kharcha ₹": fmt(v[1])}
                          for k, v in sorted(by_k.items())]),
            hide_index=True)

        if work:
            st.markdown("**Kaam entries**")
            st.dataframe(pd.DataFrame(work, columns=["id", "Karigar", "Item", "Qty"])
                         .drop(columns="id"), hide_index=True)
        if kh:
            st.markdown("**Kharcha entries**")
            st.dataframe(pd.DataFrame([{"Karigar": r["Karigar"], "Amount ₹": fmt(r["Amount"]),
                                        "Note": r["Note"]} for r in kh]), hide_index=True)

        # Delete a wrong entry
        with st.expander("🗑️ Galat entry delete karo"):
            if work:
                wk = f"del_work_{iso}"
                st.selectbox(
                    "Kaam entry", [f"#{r['id']} • {r['Karigar']} • {r['Item']} • {r['Qty']} pcs"
                                   for r in work],
                    index=None, placeholder="Entry chuno...", key=wk)
                st.button("Delete kaam entry", key="btn_del_work",
                          on_click=delete_entry, args=("work_log", wk))
            if kh:
                kk = f"del_kh_{iso}"
                st.selectbox(
                    "Kharcha entry", [f"#{r['id']} • {r['Karigar']} • ₹{fmt(r['Amount'])}"
                                      for r in kh],
                    index=None, placeholder="Entry chuno...", key=kk)
                st.button("Delete kharcha entry", key="btn_del_kh",
                          on_click=delete_entry, args=("kharcha", kk))

    with st.expander("📅 Last 14 din ka date-wise summary"):
        start = (today_ist() - timedelta(days=13)).isoformat()
        rows = q(
            """
            SELECT d, SUM(pcs) AS pcs, SUM(kh) AS kh FROM (
                SELECT work_date AS d, qty AS pcs, 0 AS kh FROM work_log WHERE work_date >= ?
                UNION ALL
                SELECT kharcha_date AS d, 0 AS pcs, amount AS kh FROM kharcha WHERE kharcha_date >= ?
            ) GROUP BY d ORDER BY d DESC
            """, (start, start))
        if rows:
            st.dataframe(pd.DataFrame([{"Date": fmt_short(r["d"]), "Pieces": r["pcs"],
                                        "Kharcha ₹": fmt(r["kh"])} for r in rows]),
                         hide_index=True)
        else:
            st.caption("Abhi koi entry nahi.")


# ---------------------------------------------------------------------
# TAB 2: LOG KHARCHA
# ---------------------------------------------------------------------
def render_kharcha():
    st.subheader("💸 Kharcha (Advance Cash)")
    karigars = [r["name"] for r in q("SELECT name FROM karigars WHERE active = 1 ORDER BY name")]
    if not karigars:
        st.info("Pehle **Karigar Master** tab me karigar add karo.")
        return

    show_flash("flash_kh")
    st.date_input("📅 Date", value=today_ist(), key="k_date", format=DATE_FMT)
    st.selectbox("👷 Karigar", karigars, index=None,
                 placeholder="Karigar chuno...", key="k_karigar")
    st.number_input("💰 Amount (₹)", min_value=0, step=10, key="k_amount")
    st.text_input("📝 Note (optional)", key="k_note", placeholder="e.g. chai-nashta, ghar bhejna")
    st.button("✅ Save Kharcha", type="primary", on_click=save_kharcha, key="btn_save_kh")

    st.divider()
    st.markdown("**Haal ke 10 kharcha**")
    rows = q(
        """
        SELECT h.kharcha_date AS d, k.name AS karigar, h.amount AS amount, h.note AS note
        FROM kharcha h JOIN karigars k ON k.id = h.karigar_id
        ORDER BY h.id DESC LIMIT 10
        """)
    if rows:
        st.dataframe(pd.DataFrame([{"Date": fmt_short(r["d"]), "Karigar": r["karigar"],
                                    "Amount ₹": fmt(r["amount"]), "Note": r["note"]}
                                   for r in rows]), hide_index=True)
    else:
        st.caption("Abhi koi kharcha entry nahi.")


# ---------------------------------------------------------------------
# TAB 3: MAMU'S HISAAB & RATES  (PIN protected)
# ---------------------------------------------------------------------
def render_mamu():
    st.subheader("🧮 Mamu's Hisaab & Rates")

    if not st.session_state.get("mamu_ok"):
        show_flash("flash_pin")
        st.text_input("🔑 Mamu PIN", type="password", key="pin_input")
        st.button("🔓 Unlock", type="primary", on_click=try_unlock, key="btn_unlock")
        return

    st.button("🔒 Lock", on_click=lock_mamu, key="btn_lock")
    if get_pin() == DEFAULT_PIN:
        st.warning("Default PIN (1234) chal raha hai. Secrets me `MAMU_PIN` set karke badal do.")

    t_settle, t_rates = st.tabs(["📊 Settlement", "💰 Item Rates"])
    with t_settle:
        render_settlement()
    with t_rates:
        render_rates()


def render_settlement():
    mode = st.radio("Period", ["Last 14 days", "Last 7 days", "Custom"],
                    horizontal=True, key="s_mode")
    today = today_ist()
    if mode == "Last 14 days":
        start, end = today - timedelta(days=13), today
    elif mode == "Last 7 days":
        start, end = today - timedelta(days=6), today
    else:
        c1, c2 = st.columns(2)
        start = c1.date_input("From", value=today - timedelta(days=13),
                              key="s_from", format=DATE_FMT)
        end = c2.date_input("To", value=today, key="s_to", format=DATE_FMT)

    st.caption(f"📆 {fmt_d(start)}  →  {fmt_d(end)}")
    if start > end:
        st.error("'From' date 'To' date se pehle honi chahiye.")
        return

    entries = settlement_data(start.isoformat(), end.isoformat())
    if not entries:
        st.info("Is period me koi kaam ya kharcha entry nahi mili.")
        return

    pick = st.selectbox("👷 Karigar", ["All Karigars"] + [e["name"] for e in entries])
    shown = entries if pick == "All Karigars" else [e for e in entries if e["name"] == pick]

    missing = sorted({l["item"] for e in shown for l in e["lines"] if l["rate"] <= 0})
    if missing:
        st.warning("⚠️ In items ka rate set nahi hai (amount 0 ginega): **"
                   + ", ".join(missing) + "**. 'Item Rates' tab me rate daalo.")

    # Overview when more than one karigar is shown
    if len(shown) > 1:
        st.markdown("#### Overview")
        st.dataframe(pd.DataFrame([
            {"Karigar": e["name"], "Gross ₹": fmt(e["gross"]),
             "Kharcha ₹": fmt(e["kharcha"]), "Net Payable ₹": fmt(e["net"])}
            for e in shown]), hide_index=True)
        st.markdown(
            f"**Total Gross:** ₹{fmt(sum(e['gross'] for e in shown))}  \n"
            f"**Total Kharcha:** ₹{fmt(sum(e['kharcha'] for e in shown))}  \n"
            f"**Total Net Payable:** ₹{fmt(sum(e['net'] for e in shown))}")

    # Karigar-wise cards
    for e in shown:
        with st.container(border=True):
            st.markdown(f"### 👷 {e['name']}")
            if e["lines"]:
                st.dataframe(pd.DataFrame([
                    {"Item": l["item"], "Qty": l["qty"], "Rate ₹": fmt(l["rate"]),
                     "Amount ₹": fmt(l["amount"])} for l in e["lines"]]), hide_index=True)
            else:
                st.caption("Is period me koi kaam entry nahi.")
            st.markdown(
                f"Total pieces: **{e['pieces']}**  \n"
                f"Gross Earnings: **₹{fmt(e['gross'])}**  \n"
                f"Kharcha / Advance: **− ₹{fmt(e['kharcha'])}**")
            if e["kh_lines"]:
                with st.expander("Kharcha details"):
                    st.dataframe(pd.DataFrame([
                        {"Date": fmt_short(k["date"]), "Amount ₹": fmt(k["amount"]),
                         "Note": k["note"]} for k in e["kh_lines"]]), hide_index=True)
            if e["net"] >= 0:
                st.success(f"### Net Payable (Baki): ₹{fmt(e['net'])}")
            else:
                st.error(f"### Zyada advance: ₹{fmt(-e['net'])}\n"
                         "Agle hisaab me adjust hoga.")

            msg = whatsapp_text(e, start, end)
            st.link_button("📲 WhatsApp pe bhejo", wa_url(msg))
            with st.expander("📝 Message text (copy karo)"):
                st.code(msg, language=None)

    # Export
    st.divider()
    st.markdown("#### 📤 Export")
    pdf_name = f"hisaab_{start.isoformat()}_to_{end.isoformat()}"
    if len(shown) == 1:
        pdf_name = f"{clean_name(shown[0]['name']).replace(' ', '_')}_{pdf_name}"
    st.download_button("📄 PDF Download karo", data=build_pdf(shown, start, end),
                       file_name=pdf_name + ".pdf", mime="application/pdf", key="dl_pdf")
    if len(shown) > 1:
        all_msg = "\n\n━━━━━━━━━━\n\n".join(whatsapp_text(e, start, end) for e in shown)
        st.link_button("📲 Sab ka hisaab WhatsApp pe bhejo", wa_url(all_msg))
    st.caption("PDF download hone ke baad WhatsApp me 'Attach → Document' se bhej sakte ho.")


def render_rates():
    show_flash("flash_rates")
    st.caption("Rate = ₹ per piece. Settlement hamesha **current rate** se banta hai, "
               "isliye rate badalne se pehle purana hisaab settle kar lo.")

    rows = q("SELECT id, name, rate FROM items ORDER BY name")
    if rows:
        df = pd.DataFrame(rows).set_index("id").rename(columns={"name": "Item", "rate": "Rate"})
        zero = int((df["Rate"] <= 0).sum())
        if zero:
            st.warning(f"{zero} item ka rate abhi 0 hai - neeche table me daalo.")
        with st.form("rates_form"):
            edited = st.data_editor(
                df, hide_index=True, disabled=["Item"], key="rates_editor",
                column_config={
                    "Item": st.column_config.TextColumn("Item"),
                    "Rate": st.column_config.NumberColumn(
                        "Rate ₹ / piece", min_value=0.0, step=0.5, format="%.2f"),
                })
            if st.form_submit_button("💾 Save Rates", type="primary"):
                changed = 0
                for item_id, new_rate in edited["Rate"].items():
                    new_rate = 0.0 if pd.isna(new_rate) else float(new_rate)
                    if abs(new_rate - float(df.loc[item_id, "Rate"])) > 1e-9:
                        run("UPDATE items SET rate = ? WHERE id = ?", (new_rate, int(item_id)))
                        changed += 1
                flash("flash_rates", "ok", f"✅ {changed} rate update hua.")
                st.rerun()
    else:
        st.info("Abhi koi item nahi hai.")

    with st.expander("➕ Naya item + rate add karo"):
        with st.form("new_item_form", clear_on_submit=True):
            name = st.text_input("Item ka naam")
            rate = st.number_input("Rate (₹ per piece)", min_value=0.0, step=0.5)
            if st.form_submit_button("Add Item"):
                name = clean_name(name)
                if not name:
                    flash("flash_rates", "err", "⚠️ Item ka naam likho.")
                else:
                    item_id, name = get_or_create_item(name)
                    run("UPDATE items SET rate = ? WHERE id = ?", (float(rate), item_id))
                    flash("flash_rates", "ok", f"✅ '{name}' ka rate ₹{fmt(rate)} set ho gaya.")
                st.rerun()

    unused = q("SELECT id, name FROM items WHERE id NOT IN "
               "(SELECT DISTINCT item_id FROM work_log) ORDER BY name")
    with st.expander("🗑️ Unused item delete karo (galti se bana item)"):
        if not unused:
            st.caption("Koi unused item nahi hai.")
        else:
            victim = st.selectbox("Item", [u["name"] for u in unused])
            if st.button("Delete item", key="btn_del_item"):
                run("DELETE FROM items WHERE name = ?", (victim,))
                flash("flash_rates", "ok", f"🗑️ '{victim}' delete ho gaya.")
                st.rerun()


# ---------------------------------------------------------------------
# TAB 4: KARIGAR MASTER (+ backup)
# ---------------------------------------------------------------------
def render_karigar_master():
    st.subheader("👷 Karigar Master")
    show_flash("flash_km")
    st.text_input("Naya Karigar", key="km_new", placeholder="Karigar ka naam")
    st.button("➕ Add Karigar", type="primary", on_click=add_karigar, key="btn_add_k")

    rows = q("SELECT id, name, active FROM karigars ORDER BY active DESC, name")
    if rows:
        st.dataframe(pd.DataFrame([{"Karigar": r["name"],
                                    "Status": "✅ Active" if r["active"] else "⏸️ Inactive"}
                                   for r in rows]), hide_index=True)

        with st.expander("✏️ Rename / Active-Inactive"):
            pick = st.selectbox("Karigar chuno", [r["name"] for r in rows])
            cur = next(r for r in rows if r["name"] == pick)
            new_name = st.text_input("Naya naam", value=cur["name"], key=f"km_rename_{cur['id']}")
            if st.button("💾 Rename save karo", key="btn_rename"):
                new_name = clean_name(new_name)
                try:
                    if not new_name:
                        raise ValueError
                    run("UPDATE karigars SET name = ? WHERE id = ?", (new_name, cur["id"]))
                    flash("flash_km", "ok", f"✅ Naam badal gaya: {new_name}")
                except (sqlite3.IntegrityError, ValueError):
                    flash("flash_km", "err", "⚠️ Ye naam khaali hai ya pehle se hai.")
                st.rerun()
            toggle = ("⏸️ Deactivate (dropdown se hatao)" if cur["active"]
                      else "▶️ Activate karo")
            if st.button(toggle, key="btn_toggle"):
                run("UPDATE karigars SET active = ? WHERE id = ?",
                    (0 if cur["active"] else 1, cur["id"]))
                flash("flash_km", "ok", f"✅ {cur['name']} ka status badal gaya.")
                st.rerun()
            st.caption("Inactive karigar ka purana data aur hisaab safe rehta hai.")
    else:
        st.info("Abhi koi karigar nahi hai. Upar naam likh kar add karo.")

    render_backup()


def render_backup():
    with st.expander("💾 Data Backup / Restore (zaroor padho)"):
        st.caption("Free cloud hosting par data kabhi-kabhi reset ho sakta hai. "
                   "Har hafte backup download karke phone me rakho.")
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as f:
                st.download_button("⬇️ Backup Download karo", data=f.read(),
                                   file_name=f"workshop_ledger_backup_{today_ist().isoformat()}.db",
                                   mime="application/octet-stream", key="dl_backup")

        st.markdown("**Restore (purana backup wapas lao)**")
        up = st.file_uploader("Backup file (.db)", type=["db", "sqlite"], key="restore_file")
        ok = st.checkbox("Haan, abhi ka data replace karna hai", key="restore_ok")
        if st.button("♻️ Restore karo", key="btn_restore"):
            if up is None or not ok:
                st.error("File chuno aur confirm tick karo.")
            else:
                tmp = DB_PATH + ".restore"
                with open(tmp, "wb") as f:
                    f.write(up.getvalue())
                try:
                    con = sqlite3.connect(tmp)
                    tables = {r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    con.close()
                    valid = {"karigars", "items", "work_log", "kharcha"} <= tables
                except sqlite3.DatabaseError:
                    valid = False
                if valid:
                    os.replace(tmp, DB_PATH)
                    st.success("✅ Restore ho gaya. Page refresh karo.")
                else:
                    os.remove(tmp)
                    st.error("Ye valid ledger backup file nahi lagti.")


# ---------------------------------------------------------------------
# PAGE SETUP & MOBILE STYLING
# ---------------------------------------------------------------------
def inject_css():
    st.markdown(
        """
        <style>
        .block-container {padding: 1rem 0.8rem 4rem 0.8rem; max-width: 720px;}
        /* big touch-friendly buttons */
        .stButton > button, .stDownloadButton > button,
        .stFormSubmitButton > button, .stLinkButton > a {
            width: 100%; min-height: 3.2rem; font-size: 1.1rem;
            font-weight: 600; border-radius: 12px;
        }
        /* bigger dropdowns / inputs */
        div[data-baseweb="select"] > div {min-height: 3rem; font-size: 1.05rem;}
        .stTextInput input, .stNumberInput input {font-size: 1.1rem;}
        /* tabs scroll sideways on small screens instead of getting cut */
        .stTabs [data-baseweb="tab-list"] {gap: 0.2rem; overflow-x: auto;}
        .stTabs [data-baseweb="tab"] {padding: 0.7rem 0.8rem; white-space: nowrap;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(page_title="Karigar Ledger", page_icon="🧵",
                       layout="centered", initial_sidebar_state="collapsed")
    init_db()
    inject_css()
    st.title("🧵 Karigar Ledger")

    t1, t2, t3, t4 = st.tabs(["📝 Daily Entry", "💸 Log Kharcha",
                              "🧮 Mamu's Hisaab", "👷 Karigar Master"])
    with t1:
        render_daily_entry()
    with t2:
        render_kharcha()
    with t3:
        render_mamu()
    with t4:
        render_karigar_master()


main()
