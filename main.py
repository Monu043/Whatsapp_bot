# main.py
"""
WhatsApp Job Bot - Filter: ONLY (B.Tech ECE) OR (Any Graduate)
Stores seen jobs in SQLite to only send new ones in daily sends.
Deploy with: gunicorn main:app
Requirements: flask, twilio, requests, beautifulsoup4, python-dotenv, gunicorn
"""

import os
import re
import time
import threading
import sqlite3
from datetime import datetime
from typing import List, Dict, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# Optional Twilio REST for outgoing messages
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# ---------- CONFIG ----------
MAX_SHOW = 8
SEND_TIME = os.getenv("DAILY_SEND_TIME", "09:00")  # server time HH:MM
DB_PATH = os.getenv("JOB_DB_PATH", "jobs_seen.db")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # e.g. "whatsapp:+1415..."
RECIPIENT_WHATSAPP = os.getenv("RECIPIENT_WHATSAPP")      # e.g. "whatsapp:+91..."

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TwilioClient:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        print("Twilio init error:", e)
        twilio_client = None

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Minimal list of reliable sources (you can add more)
SOURCES = [
    ("RojgarResult", "https://www.rojgarresult.com/latestjob.php"),
    ("SarkariResult", "https://www.sarkariresult.com/"),
    ("FreeJobAlert", "https://www.freejobalert.com/"),
    ("DRDO", "https://www.drdo.gov.in/careers"),
    ("ISRO", "https://www.isro.gov.in/careers"),
    ("BHEL", "https://www.bhel.com/careers"),
    ("BEL", "https://bel-india.in/career/"),
    ("ECIL", "https://www.ecil.co.in/careers.php"),
    ("NIC", "https://www.nic.in/careers/"),
    ("NIELIT", "https://nielit.gov.in/content/careers"),
]

# ---------- UTILS ----------
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT UNIQUE,
            title TEXT,
            date_added TEXT
        )
    """)
    conn.commit()
    conn.close()

def mark_seen(link: str, title: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO seen_jobs (link, title, date_added) VALUES (?, ?, ?)",
                    (link, title, datetime.utcnow().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def is_seen(link: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen_jobs WHERE link = ? LIMIT 1", (link,))
    r = cur.fetchone()
    conn.close()
    return bool(r)

def fetch(url: str, timeout: int = 12) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print("fetch failed", url, e)
        return None

def normalize_link(base_url: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base_url.rstrip("/") + href
    return base_url.rstrip("/") + "/" + href

def extract_jobs_from_html(html: str, base_url: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    # anchors first
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 8:
            continue
        href = normalize_link(base_url, a["href"])
        combined = (text + " " + href).lower()
        if any(k in combined for k in ["vacancy", "notification", "job", "recruit", "apply", "walk-in", "scientist", "engineer", "trainee", "assistant"]):
            results.append({"title": text.strip(), "link": href})
            continue
        if any(k in combined for k in ["b.tech", "btech", "b.e", "b.e.", "electronics", "ece", "graduate", "any degree", "any graduate", "bachelor"]):
            results.append({"title": text.strip(), "link": href})
    # fallback: list items / table rows
    for li in soup.find_all(["li", "tr"]):
        text = li.get_text(" ", strip=True)
        if not text or len(text) < 20:
            continue
        low = text.lower()
        if any(k in low for k in ["vacancy", "notification", "job", "recruit", "apply", "walk-in", "scientist", "engineer"]):
            a = li.find("a", href=True)
            link = normalize_link(base_url, a["href"]) if a else base_url
            results.append({"title": text.strip(), "link": link})
    # dedupe
    seen = set()
    out = []
    for r in results:
        key = (r["title"].strip(), r["link"].strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

# ---------- FILTER (YOUR EXACT RULE) ----------
def eligible_for_you(text: str) -> Tuple[Optional[bool], List[str]]:
    """
    Returns (True/False/None, reasons)
    True -> Applicable (B.Tech ECE OR Any Graduate)
    False -> Not Applicable
    None -> Unknown
    """
    t = text.lower()
    reasons = []

    # Immediate reject qualifiers (10th/12th/ITI/diploma)
    if any(word in t for word in ["10th", "12th", "iti", "diploma", "polytechnic", "12th pass", "matric"]):
        reasons.append("Requires 10th/12th/ITI/Diploma")
        return False, reasons

    # Accept ANY GRADUATE phrases (priority)
    if any(kw in t for kw in ["any graduate", "any degree", "graduate in any", "bachelor degree in any", "degree in any", "graduation in any"]):
        reasons.append("Open to Any Graduate / Any Degree")
        return True, reasons

    # Accept explicit mentions of 'graduate' or 'bachelor' for degree roles
    if any(kw in t for kw in ["graduate", "bachelor degree", "bachelor's degree", "degree required", "bachelor in"]):
        # but avoid technician roles that explicitly require diploma/iti earlier handled
        reasons.append("Requires Graduate / Bachelor's degree")
        return True, reasons

    # If text contains 'b.tech' or 'btech' -> must ensure ECE/electronics is present
    if "b.tech" in t or "btech" in t or "b.e" in t:
        # if ECE/electronics present => accept
        if any(k in t for k in ["ece", "electronics", "electronics & communication", "electronics and communication"]):
            reasons.append("Specifically mentions ECE / Electronics")
            return True, reasons
        # if 'any branch' explicitly present -> user does NOT want b.tech any-branch
        if any(k in t for k in ["any branch", "all engineering", "all branches"]):
            reasons.append("B.Tech any branch — excluded by preference")
            return False, reasons
        # if other branch explicitly present (mechanical/civil) -> reject
        if any(k in t for k in ["mechanical", "civil", "chemical", "electrical"]):
            reasons.append("Mentions other branch (not ECE)")
            return False, reasons
        # If b.tech present without branch mention -> ambiguous; treat as unknown (user didn't want any-branch)
        reasons.append("B.Tech mentioned without ECE — excluded by preference")
        return False, reasons

    # If ECE/electronics terms appear without degree mention -> accept (likely ECE role)
    if any(k in t for k in ["ece", "electronics", "electronic"]):
        reasons.append("Mentions ECE / Electronics")
        return True, reasons

    # If role is technical (engineer/scientist) and mentions 'degree' or 'graduate'
    if any(k in t for k in ["engineer", "scientist", "scientific", "technical", "technical assistant"]) and any(k in t for k in ["degree", "graduate", "bachelor"]):
        reasons.append("Technical post requiring degree/graduate")
        return True, reasons

    # Technician that requires ITI/diploma was handled above; otherwise unknown
    reasons.append("Could not detect confidently")
    return None, reasons

# ---------- AGGREGATION ----------
def aggregate_jobs() -> List[Dict]:
    all_jobs = []
    for name, url in SOURCES:
        html = fetch(url)
        if not html:
            continue
        items = extract_jobs_from_html(html, base_url=url)
        for it in items[:25]:
            title = f"[{name}] {it['title']}"
            link = it['link']
            all_jobs.append({"title": title, "link": link})
    # dedupe by normalized title+link
    seen = set()
    out = []
    for j in all_jobs:
        key = (re.sub(r"\s+", " ", j["title"].lower()).strip(), j["link"])
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out

# ---------- REPORT & NEW FILTERED ----------
def build_reports(jobs: List[Dict]) -> Tuple[str, Dict]:
    applicable = []
    not_applicable = []
    unknown = []
    new_applicable = []

    for j in jobs:
        combined = f"{j['title']} {j['link']}"
        eligible, reasons = eligible_for_you(combined)
        entry = {"title": j['title'], "link": j['link'], "reasons": reasons}
        if eligible is True:
            applicable.append(entry)
            # if unseen, add to new_applicable
            if not is_seen(j['link']):
                new_applicable.append(entry)
                mark_seen(j['link'], j['title'])
        elif eligible is False:
            not_applicable.append(entry)
            # mark seen to avoid repeat in future if needed
            if not is_seen(j['link']):
                mark_seen(j['link'], j['title'])
        else:
            unknown.append(entry)
            if not is_seen(j['link']):
                mark_seen(j['link'], j['title'])

    now = datetime.utcnow().strftime("%d %b %Y")
    lines = [f"📅 Daily Job Report — {now}", ""]
    lines.append("✅ Applicable (B.Tech ECE OR Any Graduate):")
    if applicable:
        for e in applicable[:MAX_SHOW]:
            lines.append(f"- {e['title']}")
            lines.append(f"  {e['link']}")
            lines.append(f"  Note: {('; ').join(e['reasons'])}")
    else:
        lines.append("None")

    lines.append("")
    lines.append("❌ Not Applicable:")
    if not_applicable:
        for e in not_applicable[:MAX_SHOW]:
            lines.append(f"- {e['title']}")
            lines.append(f"  {e['link']}")
            lines.append(f"  Note: {('; ').join(e['reasons'])}")
    else:
        lines.append("None")

    lines.append("")
    lines.append("⚠️ Unable to detect:")
    if unknown:
        for e in unknown[:MAX_SHOW]:
            lines.append(f"- {e['title']}")
            lines.append(f"  {e['link']}")
            lines.append(f"  Note: {('; ').join(e['reasons'])}")
    else:
        lines.append("None")

    lines.append("")
    lines.append("New applicable jobs (not previously seen):")
    if new_applicable:
        for e in new_applicable[:MAX_SHOW]:
            lines.append(f"- {e['title']}")
            lines.append(f"  {e['link']}")
    else:
        lines.append("None")

    lines.append("")
    lines.append("Reply 'jobs' anytime to get on-demand summary.")
    report_text = "\n".join(lines)
    stats = {"total": len(jobs), "applicable": len(applicable), "not_applicable": len(not_applicable), "unknown": len(unknown), "new_applicable": len(new_applicable)}
    return report_text, stats

# ---------- TWILIO OUTGOING ----------
def send_whatsapp(body: str, to: Optional[str] = None):
    if not twilio_client or not TWILIO_WHATSAPP_FROM:
        print("Twilio not configured, skipping send.")
        return False
    if to is None:
        to = RECIPIENT_WHATSAPP
    try:
        msg = twilio_client.messages.create(body=body, from_=TWILIO_WHATSAPP_FROM, to=to)
        print("Sent message SID:", msg.sid)
        return True
    except Exception as e:
        print("Twilio send error:", e)
        return False

# ---------- DAILY TASK ----------
def daily_task():
    print("[daily] starting aggregation")
    jobs = aggregate_jobs()
    report, stats = build_reports(jobs)
    print("[daily] stats:", stats)
    # Try sending via Twilio to recipient if configured
    sent = send_whatsapp(report) if twilio_client else False
    if not sent:
        print("[daily - fallback] Report:")
        print(report)
    return report, stats

# ---------- FLASK ENDPOINT ----------
@app.route("/bot", methods=["POST"])
def bot_webhook():
    incoming = (request.form.get("Body") or "").strip().lower()
    resp = MessagingResponse()
    if any(k in incoming for k in ["jobs", "daily", "today"]):
        jobs = aggregate_jobs()
        report, stats = build_reports(jobs)
        # Twilio size protections: send first chunk in webhook response
        MAX_CHUNK = 1500
        if len(report) <= MAX_CHUNK:
            resp.message(report)
        else:
            # split by lines
            parts = []
            cur = []
            cur_len = 0
            for line in report.splitlines(True):
                if cur_len + len(line) > MAX_CHUNK and cur:
                    parts.append("".join(cur))
                    cur = [line]
                    cur_len = len(line)
                else:
                    cur.append(line)
                    cur_len += len(line)
            if cur:
                parts.append("".join(cur))
            # respond with first part
            resp.message(parts[0])
            # send remaining parts via Twilio REST to the sender (if available)
            if twilio_client:
                to = request.form.get("From")
                for p in parts[1:]:
                    try:
                        twilio_client.messages.create(body=p, from_=TWILIO_WHATSAPP_FROM, to=to)
                    except Exception as e:
                        print("Failed to send extra chunk:", e)
        return str(resp)
    else:
        resp.message("Send 'jobs' to get today's job summary for B.Tech ECE or Any Graduate.")
        return str(resp)

# ---------- SCHEDULER THREAD ----------
def scheduler_loop():
    last_sent_date = None
    while True:
        try:
            now = datetime.utcnow()
            now_hm = now.strftime("%H:%M")
            if now_hm == SEND_TIME:
                today = now.date()
                if last_sent_date != today:
                    daily_task()
                    last_sent_date = today
            time.sleep(30)
        except Exception as e:
            print("Scheduler error:", e)
            time.sleep(10)

if __name__ == "__main__":
    db_init()
    # start scheduler if twilio configured and recipient provided
    if twilio_client and TWILIO_WHATSAPP_FROM and RECIPIENT_WHATSAPP:
        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()
    else:
        print("Proactive send disabled (missing Twilio config or recipient).")
    # production server will run via gunicorn; for local testing:
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
