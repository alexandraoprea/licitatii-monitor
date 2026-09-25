#!/usr/bin/env python3
"""Dashboard local protejat pentru monitorul SEAP."""

import base64
import hmac
import os
import re
import threading
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs

import seap_monitor as monitor

PORT = int(os.getenv("PORT", "8080"))
USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
INTERVAL_SECONDS = 3600
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Toate momentele din dashboard sunt afișate în fusul orar al României.
os.environ["TZ"] = "Europe/Bucharest"
if hasattr(time, "tzset"):
    time.tzset()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Echivalentul ThreadingHTTPServer pentru Python 3.6."""
    daemon_threads = True


def human_time(value):
    """Transformă momentul UTC stocat în SQLite într-un format românesc lizibil."""
    if not value:
        return "—"
    try:
        normalized = str(value).replace("+00:00", "+0000").replace("Z", "+0000")
        try:
            parsed = datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            parsed = datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S%z")
        return parsed.astimezone().strftime("%d.%m.%Y, %H:%M")
    except ValueError:
        return str(value)


def authorized(header):
    if not PASSWORD or not header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    return hmac.compare_digest(supplied, f"{USERNAME}:{PASSWORD}")


def scheduler():
    while True:
        try:
            monitor.run_once()
        except Exception:
            monitor.logging.exception("Rularea orară a eșuat")
        time.sleep(INTERVAL_SECONDS)


class Dashboard(BaseHTTPRequestHandler):
    def guard(self):
        if authorized(self.headers.get("Authorization", "")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SEAP Monitor"')
        self.end_headers()
        return False

    def do_GET(self):
        if not self.guard():
            return
        if self.path != "/":
            self.send_error(404)
            return
        con = monitor.database()
        recipients = ", ".join(monitor.get_recipients(con))
        runs = con.execute("SELECT started_at, completed_at, status, seap_found_count, datadriven_found_count, sent_count, message FROM runs ORDER BY id DESC LIMIT 12").fetchall()
        emails = con.execute("SELECT subject, recipients, sent_at, html_body FROM email_batches ORDER BY id DESC LIMIT 50").fetchall()
        con.close()
        run_rows = "".join(f"<tr><td>{escape(human_time(x[0]))}</td><td>{escape(str(x[2]))}</td><td>{x[3]}</td><td>{x[4]}</td><td>{x[5]}</td><td>{escape(str(x[6] or ''))}</td></tr>" for x in runs) or "<tr><td colspan='6'>Încă nu există rulări.</td></tr>"
        email_rows = "".join(f"<tr><td>{escape(human_time(x[2]))}</td><td>{escape(str(x[1]))}</td><td>{escape(str(x[0]))}</td><td><details><summary>Vezi conținut</summary><div class='mail-preview'>{x[3]}</div></details></td></tr>" for x in emails) or "<tr><td colspan='4'>Încă nu s-a trimis niciun e-mail.</td></tr>"
        page = f"""<!doctype html><html lang='ro'><meta charset='utf-8'><title>Monitor licitații</title><style>
body{{font:15px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#172033}}h1{{margin-bottom:4px}}section{{background:#f7f9fc;border:1px solid #d9e1ed;border-radius:10px;padding:20px;margin:22px 0}}input{{width:min(600px,100%);padding:9px;margin:6px 0}}button{{padding:9px 15px;background:#1769aa;color:#fff;border:0;border-radius:5px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;text-align:left;border-bottom:1px solid #d9e1ed;vertical-align:top}}th{{color:#536174}}small{{color:#536174}}summary{{cursor:pointer;color:#1769aa}}.mail-preview{{margin-top:12px;background:#fff;padding:12px;overflow:auto}}.mail-preview table{{min-width:700px}}</style>
<h1>Monitor licitații</h1>
<section><h2>Destinatari alerte</h2><form method='post' action='/settings'><input name='recipients' required value='{escape(recipients)}' aria-label='Destinatari'><br><small>Mai multe adrese se separă prin virgulă.</small><p><button>Salvează destinatarii</button></p></form></section>
<section><h2>Istoric rulări</h2><table><tr><th>Pornită</th><th>Stare</th><th>Noi SEAP</th><th>Noi DataDriven</th><th>Trimise</th><th>Detalii</th></tr>{run_rows}</table></section>
<section><h2>E-mailuri trimise</h2><table><tr><th>Moment</th><th>Destinatari</th><th>Subiect</th><th>Conținut</th></tr>{email_rows}</table></section></html>"""
        body = page.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if not self.guard():
            return
        if self.path != "/settings":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(size).decode("utf-8"))
        recipients = [x.strip() for x in values.get("recipients", [""])[0].replace(";", ",").split(",") if x.strip()]
        if not recipients or any(not EMAIL.fullmatch(x) for x in recipients):
            self.send_error(400, "Introduceți cel puțin o adresă de e-mail validă.")
            return
        con = monitor.database(); monitor.set_recipients(con, recipients); con.close()
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()

    def log_message(self, format, *args):
        monitor.logging.info("Dashboard: " + format, *args)


if __name__ == "__main__":
    if not PASSWORD:
        raise SystemExit("Setează DASHBOARD_PASSWORD înainte de pornire.")
    monitor.logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Dashboard disponibil pe portul {PORT}.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Dashboard).serve_forever()
