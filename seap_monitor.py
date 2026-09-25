#!/usr/bin/env python3
"""Monitor public pentru anunțurile de participare SEAP."""

import json
import logging
import os
import smtplib
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen

SEAP_URL = "https://e-licitatie.ro/api-pub/NoticeCommon/GetCNoticeList/"
SEAP_REFERER = "https://e-licitatie.ro/pub/notices/c-notices"
CPV_CODES = {
    "90910000-9": "Servicii de curățenie",
    "90911200-8": "Servicii de curățenie a clădirilor",
    "90919200-4": "Servicii de curățenie a birourilor",
    "90919300-5": "Servicii de curățenie a școlilor",
    "90610000-6": "Servicii de curățare și măturare a străzilor",
    "90611000-3": "Servicii de curățare a străzilor",
    "90612000-0": "Servicii de măturare a străzilor",
    "90511000-2": "Servicii de colectare a deșeurilor",
    "90511100-3": "Servicii de colectare a deșeurilor menajere",
    "90511200-4": "Servicii de colectare a deșeurilor menajere",
    "90921000-9": "Servicii de dezinfecție și exterminare",
    "90922000-6": "Servicii de combatere a dăunătorilor",
    "90923000-3": "Servicii de deratizare",
    "90924000-0": "Servicii de fumigație",
    "90670000-4": "Servicii de dezinfecție și exterminare în zone urbane sau rurale",
    "79713000-5": "Servicii de pază",
}

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "seap_monitor.sqlite3"
# Destinatarul se configurează exclusiv în .env sau din dashboard.
DEFAULT_RECIPIENT = os.getenv("RECIPIENT", "")
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
PAGE_SIZE = 100


def database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Python 3.6 folosește un modul sqlite care nu acceptă obiecte Path.
    con = sqlite3.connect(str(DB_PATH))
    con.execute("CREATE TABLE IF NOT EXISTS notices (notice_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, completed_at TEXT,
        status TEXT NOT NULL, found_count INTEGER NOT NULL DEFAULT 0, sent_count INTEGER NOT NULL DEFAULT 0,
        message TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL, notice_no TEXT NOT NULL,
        title TEXT NOT NULL, cpv TEXT NOT NULL, recipients TEXT NOT NULL, sent_at TEXT NOT NULL
    )""")
    con.commit()
    return con


def get_recipients(con):
    saved = con.execute("SELECT value FROM settings WHERE key = 'recipients'").fetchone()
    value = saved[0] if saved else DEFAULT_RECIPIENT
    return [address.strip() for address in value.replace(";", ",").split(",") if address.strip()]


def set_recipients(con, recipients):
    value = ", ".join(recipients)
    with con:
        # Varianta explicită este compatibilă și cu SQLite-ul disponibil pe Ubuntu 18.04.
        cursor = con.execute("UPDATE settings SET value = ? WHERE key = 'recipients'", (value,))
        if cursor.rowcount == 0:
            con.execute("INSERT INTO settings(key, value) VALUES ('recipients', ?)", (value,))


def fetch_notices():
    # Fereastra de suprapunere face serviciul rezistent la întârzieri sau reporniri.
    start = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    payload = {
        "sysNoticeTypeIds": [], "sortProperties": [], "pageSize": PAGE_SIZE,
        "pageIndex": 0, "startPublicationDate": start.isoformat(),
    }
    def get_page(page_index):
        payload["pageIndex"] = page_index
        request = Request(
            SEAP_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Referer": SEAP_REFERER,
                     "Origin": "https://e-licitatie.ro", "User-Agent": "RomGreen-SEAP-monitor/1.0"},
        )
        with urlopen(request, timeout=45) as response:
            return json.load(response)

    first_page = get_page(0)
    total = first_page.get("total", 0)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    items = first_page.get("items", [])
    # SEAP limitează fiecare răspuns la 100 de rânduri; parcurgem toate paginile
    # din fereastra de siguranță, nu doar prima pagină.
    for page_index in range(1, pages):
        items.extend(get_page(page_index).get("items", []))
    return items


def cpv_for(notice):
    value = notice.get("cpvCodeAndName", "")
    return next((code for code in CPV_CODES if value.startswith(code)), None)


def notice_url(notice):
    # SCN/CN sunt anunțuri de participare; pagina publică folosește identificatorul intern.
    return f"https://e-licitatie.ro/pub/notices/c-notice/v2/view/{notice['noticeId']}"


def send_email(notices, recipients):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    security = os.getenv("SMTP_SECURITY", "starttls").lower()
    sender = os.environ["SMTP_FROM"]
    password = os.environ["SMTP_PASSWORD"]
    rows = []
    text_rows = []
    for item in notices:
        cpv = cpv_for(item)
        title, authority = item.get("contractTitle", "—"), item.get("contractingAuthorityNameAndFN", "—")
        deadline = item.get("tenderReceiptDeadlineExport") or "nespecificat"
        url = notice_url(item)
        rows.append(f"<tr><td><a href='{escape(url)}'>{escape(item.get('noticeNo', 'SEAP'))}</a></td>"
                    f"<td>{escape(title)}</td><td>{escape(authority)}</td><td>{escape(cpv)}</td><td>{escape(deadline)}</td></tr>")
        text_rows.append(f"{item.get('noticeNo')} | {title}\nAutoritate: {authority}\nCPV: {cpv}\nTermen: {deadline}\n{url}")
    message = EmailMessage()
    message["Subject"] = f"SEAP: {len(notices)} licitații noi relevante"
    message["From"], message["To"] = sender, ", ".join(recipients)
    message.set_content("\n\n".join(text_rows))
    message.add_alternative("""<html><body><p>Au apărut licitații SEAP noi, cu CPV-urile urmărite.</p>
      <table border='1' cellpadding='7' cellspacing='0'><thead><tr><th>Anunț</th><th>Obiect</th><th>Autoritate</th><th>CPV</th><th>Termen</th></tr></thead>
      <tbody>""" + "".join(rows) + "</tbody></table><p>Sursă: SEAP.</p></body></html>", subtype="html")
    context = ssl.create_default_context()
    smtp_class = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with smtp_class(host, port, timeout=45, context=context) if security == "ssl" else smtp_class(host, port, timeout=45) as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        smtp.login(sender, password)
        smtp.send_message(message)


def run_once():
    con = database()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = con.execute("INSERT INTO runs(started_at, status) VALUES (?, 'running')", (started_at,)).lastrowid
    try:
        matched = [item for item in fetch_notices() if cpv_for(item)]
        unseen = [item for item in matched if not con.execute("SELECT 1 FROM notices WHERE notice_id = ?", (str(item["noticeId"]),)).fetchone()]
        sent_count = 0
        if unseen:
            # Prima rulare stabilește doar reperul, fără să trimită retrospectiv un val de alerte.
            known_count = con.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
            if known_count:
                recipients = get_recipients(con)
                if not recipients:
                    raise RuntimeError("Nu este configurat niciun destinatar de e-mail.")
                send_email(unseen, recipients)
                sent_count = len(unseen)
                now = datetime.now(timezone.utc).isoformat()
                with con:
                    con.executemany(
                        "INSERT INTO deliveries(notice_id, notice_no, title, cpv, recipients, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [(str(x["noticeId"]), x.get("noticeNo", "SEAP"), x.get("contractTitle", "—"), cpv_for(x), ", ".join(recipients), now) for x in unseen],
                    )
                logging.info("Trimis e-mail pentru %s licitații noi.", len(unseen))
            else:
                logging.info("Inițializare: %s licitații curente marcate, fără e-mail.", len(unseen))
            with con:
                con.executemany("INSERT OR IGNORE INTO notices(notice_id, seen_at) VALUES (?, ?)", [(str(x["noticeId"]), datetime.now(timezone.utc).isoformat()) for x in unseen])
        else:
            logging.info("Nicio licitație nouă relevantă.")
        with con:
            con.execute("UPDATE runs SET completed_at = ?, status = 'ok', found_count = ?, sent_count = ?, message = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), len(unseen), sent_count, "Nicio noutate" if not unseen else "Procesare reușită", run_id))
        return {"found": len(unseen), "sent": sent_count}
    except Exception as exc:
        with con:
            con.execute("UPDATE runs SET completed_at = ?, status = 'error', message = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), str(exc), run_id))
        raise
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        run_once()
    except Exception as exc:
        logging.exception("Rularea monitorului a eșuat: %s", exc)
        sys.exit(1)
