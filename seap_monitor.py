#!/usr/bin/env python3
"""Monitor public pentru anunțurile de participare SEAP."""

import json
import logging
import os
import re
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
import requests

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
        message TEXT, seap_found_count INTEGER NOT NULL DEFAULT 0,
        datadriven_found_count INTEGER NOT NULL DEFAULT 0
    )""")
    run_columns = [row[1] for row in con.execute("PRAGMA table_info(runs)")]
    if "seap_found_count" not in run_columns:
        con.execute("ALTER TABLE runs ADD COLUMN seap_found_count INTEGER NOT NULL DEFAULT 0")
        # Rulările existente erau exclusiv SEAP.
        con.execute("UPDATE runs SET seap_found_count = found_count")
    if "datadriven_found_count" not in run_columns:
        con.execute("ALTER TABLE runs ADD COLUMN datadriven_found_count INTEGER NOT NULL DEFAULT 0")
    con.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL, notice_no TEXT NOT NULL,
        title TEXT NOT NULL, cpv TEXT NOT NULL, recipients TEXT NOT NULL, sent_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'SEAP', batch_id INTEGER
    )""")
    delivery_columns = [row[1] for row in con.execute("PRAGMA table_info(deliveries)")]
    if "source" not in delivery_columns:
        con.execute("ALTER TABLE deliveries ADD COLUMN source TEXT NOT NULL DEFAULT 'SEAP'")
    if "batch_id" not in delivery_columns:
        con.execute("ALTER TABLE deliveries ADD COLUMN batch_id INTEGER")
    con.execute("""CREATE TABLE IF NOT EXISTS email_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recipients TEXT NOT NULL, subject TEXT NOT NULL,
        sent_at TEXT NOT NULL, html_body TEXT NOT NULL, text_body TEXT NOT NULL
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


def fetch_datadriven_notices():
    """Citește licitațiile monitorizate din contul DataDriven al clientului."""
    secret_path = Path(os.getenv("DATADRIVEN_SECRET", "/home/licitatii/seap-monitor/datadriven.secret"))
    secret = dict(line.rstrip("\n").split("=", 1) for line in secret_path.open() if "=" in line)
    session = requests.Session()
    login_page = session.get("https://www.datadriven.ro/app/", timeout=45).text
    def value(name):
        return re.search(r'"%s"\s*:\s*"([^"]+)"' % name, login_page).group(1)
    csrf, transaction, policy, api = (value(x) for x in ("csrf", "transId", "policy", "api"))
    base = "https://dmbidiq.b2clogin.com/datadriven.ro/" + policy
    response = session.post(base + "/SelfAsserted", params={"tx": transaction, "p": policy}, data={
        "request_type": "RESPONSE", "signInName": secret["USERNAME"], "password": secret["PASSWORD"],
    }, headers={"X-CSRF-TOKEN": csrf}, timeout=45)
    answer = response.json()
    if answer.get("message"):
        raise RuntimeError("DataDriven a respins autentificarea.")
    session.get(base + "/api/" + api + "/confirmed", params={"rememberMe": "false", "csrf_token": csrf, "tx": transaction, "p": policy}, headers={"X-CSRF-TOKEN": csrf}, timeout=45)
    projects = session.get("https://www.datadriven.ro/api/ProjectsGet", timeout=45).json()["projects"]
    if not projects:
        return []
    project_id = projects[0]["id"]
    # Aceeași vedere ca „Monitor > Relevante” din aplicația DataDriven.
    query = {
        "skip": 0, "top": 100, "monitoringCriteria": ["main"], "authorities": [],
        "searchText": "*", "sortOrder": "pub", "authorityFilter": [], "cpvFilter": [],
        "areaFilter": [], "domainFilter": [], "categoryFilter": [], "typeFilter": [],
        "noticeTypeFilter": [], "visibilityFilter": ["showVisible"], "statusFilter": ["open"],
        "relevanceFilter": ["High", "Medium"], "primaryCategoriesFilter": False,
    }
    notices = session.post("https://www.datadriven.ro/api/ProjectNotices/" + project_id, json=query, timeout=45).json()["notices"]
    return [{
        "noticeId": "datadriven:" + item["id"], "noticeNo": item.get("noticeNo", "DataDriven"),
        "contractTitle": item.get("title") or item.get("fullTitle") or "—",
        "contractingAuthorityNameAndFN": item.get("authorityName", "—"),
        "cpvCodeAndName": "%s - %s" % (item.get("cpv", ""), item.get("cpvName", "")),
        "tenderReceiptDeadlineExport": item.get("deadline") or "nespecificat",
        "_source": "DataDriven",
        "_url": "https://www.datadriven.ro/app/?id=%s&monitor=true&AiRelevance=Relevante&noticeId=%s" % (project_id, item["id"]),
    } for item in notices]


def cpv_for(notice):
    value = notice.get("cpvCodeAndName", "")
    return next((code for code in CPV_CODES if value.startswith(code)), None)


def notice_url(notice):
    if notice.get("_url"):
        return notice["_url"]
    # SCN/CN sunt anunțuri de participare; pagina publică folosește identificatorul intern.
    return f"https://e-licitatie.ro/pub/notices/c-notice/v2/view/{notice['noticeId']}"


def send_email(notices, recipients):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    security = os.getenv("SMTP_SECURITY", "starttls").lower()
    sender = os.environ["SMTP_FROM"]
    sender_name = os.getenv("SMTP_FROM_NAME", "Monitorizare Licitatii")
    password = os.environ["SMTP_PASSWORD"]
    text_sections = []
    html_sections = []
    for source in ("SEAP", "DataDriven"):
        source_items = [item for item in notices if item.get("_source", "SEAP") == source]
        if not source_items:
            continue
        rows = []
        text_rows = []
        for item in source_items:
            cpv = cpv_for(item)
            title, authority = item.get("contractTitle", "—"), item.get("contractingAuthorityNameAndFN", "—")
            deadline = item.get("tenderReceiptDeadlineExport") or "nespecificat"
            url = notice_url(item)
            rows.append(f"<tr><td><a href='{escape(url)}'>{escape(item.get('noticeNo', source))}</a></td>"
                        f"<td>{escape(title)}</td><td>{escape(authority)}</td><td>{escape(cpv)}</td><td>{escape(deadline)}</td></tr>")
            text_rows.append(f"{item.get('noticeNo')} | {title}\nAutoritate: {authority}\nCPV: {cpv}\nTermen: {deadline}\n{url}")
        text_sections.append(source + "\n" + "\n\n".join(text_rows))
        html_sections.append("<h2>" + escape(source) + "</h2><table border='1' cellpadding='7' cellspacing='0'>"
                             "<thead><tr><th>Anunț</th><th>Obiect</th><th>Autoritate</th><th>CPV</th><th>Termen</th></tr></thead><tbody>"
                             + "".join(rows) + "</tbody></table>")
    message = EmailMessage()
    summary = ", ".join("%s %s" % (sum(1 for x in notices if x.get("_source", "SEAP") == source), source) for source in ("SEAP", "DataDriven") if any(x.get("_source", "SEAP") == source for x in notices))
    is_single = len(notices) == 1
    subject = ("Licitație nouă: " if is_single else "Licitații noi: ") + summary
    introduction = "A apărut o licitație nouă" if is_single else "Au apărut licitații noi"
    html_body = "<html><body><p>" + introduction + ", cu CPV-urile urmărite.</p>" + "".join(html_sections) + "</body></html>"
    text_body = "\n\n".join(text_sections)
    message["Subject"] = subject
    # Destinatarii sunt puși doar în plicul SMTP, nu unul în câmpul To al altuia.
    message["From"], message["To"] = "%s <%s>" % (sender_name, sender), "Destinatari ascunsi:;"
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    smtp_class = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with smtp_class(host, port, timeout=45, context=context) if security == "ssl" else smtp_class(host, port, timeout=45) as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        smtp.login(sender, password)
        smtp.send_message(message, from_addr=sender, to_addrs=recipients)
    return {"subject": subject, "html_body": html_body, "text_body": text_body}


def run_once():
    con = database()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = con.execute("INSERT INTO runs(started_at, status) VALUES (?, 'running')", (started_at,)).lastrowid
    try:
        seap_items = fetch_notices()
        for item in seap_items:
            item["_source"] = "SEAP"
        matched = [item for item in seap_items + fetch_datadriven_notices() if cpv_for(item)]
        unseen = [item for item in matched if not con.execute("SELECT 1 FROM notices WHERE notice_id = ?", (str(item["noticeId"]),)).fetchone()]
        initialized_sources = set()
        for source in ("SEAP", "DataDriven"):
            source_unseen = [item for item in unseen if item.get("_source", "SEAP") == source]
            source_count = con.execute("SELECT COUNT(*) FROM notices WHERE notice_id LIKE ?", ("datadriven:%" if source == "DataDriven" else "datadriven:%",)).fetchone()[0] if source == "DataDriven" else con.execute("SELECT COUNT(*) FROM notices WHERE notice_id NOT LIKE 'datadriven:%'").fetchone()[0]
            if source_unseen and source_count == 0:
                initialized_sources.add(source)
        alertable = [item for item in unseen if item.get("_source", "SEAP") not in initialized_sources]
        seap_found_count = len([item for item in alertable if item.get("_source", "SEAP") == "SEAP"])
        datadriven_found_count = len([item for item in alertable if item.get("_source", "SEAP") == "DataDriven"])
        sent_count = 0
        if unseen:
            # Fiecare sursă își stabilește propriul reper, fără alerte retrospective.
            if alertable:
                recipients = get_recipients(con)
                if not recipients:
                    raise RuntimeError("Nu este configurat niciun destinatar de e-mail.")
                email = send_email(alertable, recipients)
                sent_count = len(alertable)
                now = datetime.now(timezone.utc).isoformat()
                with con:
                    batch_id = con.execute(
                        "INSERT INTO email_batches(recipients, subject, sent_at, html_body, text_body) VALUES (?, ?, ?, ?, ?)",
                        (", ".join(recipients), email["subject"], now, email["html_body"], email["text_body"]),
                    ).lastrowid
                    con.executemany(
                        "INSERT INTO deliveries(notice_id, notice_no, title, cpv, recipients, sent_at, source, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [(str(x["noticeId"]), x.get("noticeNo", "SEAP"), x.get("contractTitle", "—"), cpv_for(x), ", ".join(recipients), now, x.get("_source", "SEAP"), batch_id) for x in alertable],
                    )
                logging.info("Trimis e-mail pentru %s.", "o licitație nouă" if len(alertable) == 1 else "%s licitații noi" % len(alertable))
            else:
                logging.info("Inițializare %s: %s licitații curente marcate, fără e-mail.", ", ".join(sorted(initialized_sources)), len(unseen))
            with con:
                con.executemany("INSERT OR IGNORE INTO notices(notice_id, seen_at) VALUES (?, ?)", [(str(x["noticeId"]), datetime.now(timezone.utc).isoformat()) for x in unseen])
        else:
            logging.info("Nicio licitație nouă relevantă.")
        with con:
            message = "Nicio noutate" if not unseen else ("Reper inițial stabilit: " + ", ".join(sorted(initialized_sources)) if initialized_sources else "Procesare reușită")
            con.execute("UPDATE runs SET completed_at = ?, status = 'ok', found_count = ?, sent_count = ?, message = ?, seap_found_count = ?, datadriven_found_count = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), len(alertable), sent_count, message, seap_found_count, datadriven_found_count, run_id))
        return {"found": len(alertable), "sent": sent_count}
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
