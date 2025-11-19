from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
from bs4 import BeautifulSoup
import schedule
import time
import threading

app = Flask(__name__)

YOUR_DEGREE = "btech"
YOUR_BRANCH = "ece"

def scrape_jobs():
    url = "https://www.rojgarresult.com/latestjob.php"
    html = requests.get(url).text
    soup = BeautifulSoup(html, "html.parser")
    
    jobs = []

    for row in soup.select("tr"):
        cols = row.text.lower()
        if "apply" in cols:
            jobs.append(cols)

    return jobs

def check_eligibility(text):
    text = text.lower()

    # degree filters
    if "10th" in text or "12th" in text or "iti" in text or "diploma" in text:
        return False

    if "b.tech" in text or "engineering" in text or "graduate" in text:
        pass

    # branch filters
    if "ece" in text or "electronics" in text:
        return True
    
    if "any branch" in text or "all engineering" in text:
        return True

    if "mechanical" in text or "civil" in text or "electrical" in text:
        return False

    return None  # unknown eligibility


def generate_daily_report():
    jobs = scrape_jobs()

    applicable = []
    not_applicable = []
    unknown = []

    for job in jobs:
        result = check_eligibility(job)
        
        if result is True:
            applicable.append(job)
        elif result is False:
            not_applicable.append(job)
        else:
            unknown.append(job)

    report = "📅 *Daily Job Report*\n\n"

    report += "✅ *Applicable for You*\n"
    report += "\n".join(applicable[:5]) if applicable else "None\n"

    report += "\n\n❌ *Not Applicable*\n"
    report += "\n".join(not_applicable[:5]) if not_applicable else "None\n"

    report += "\n\n⚠ *Unable to detect*\n"
    report += "\n".join(unknown[:5]) if unknown else "None\n"

    return report


@app.route("/bot", methods=["POST"])
def bot():
    incoming = request.form.get("Body").lower()
    resp = MessagingResponse()

    if "jobs" in incoming or "daily" in incoming:
        resp.message(generate_daily_report())
    else:
        resp.message("Send *jobs* to get today's job summary.")

    return str(resp)


def run_daily_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(1)


# SCHEDULE AUTOMATIC DAILY REPORT TO YOUR WHATSAPP
def schedule_daily():
    schedule.every().day.at("09:00").do(lambda: print("Daily job check triggered"))


if __name__ == "__main__":
    schedule_daily()
    threading.Thread(target=run_daily_scheduler).start()
    app.run()
