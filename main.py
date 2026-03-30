import requests
from bs4 import BeautifulSoup
import os
import json
from datetime import datetime, timedelta

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://esv.ch"
STATE_FILE = "state.json"


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text})


def send_pdf(url_pdf, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "document": url_pdf,
        "caption": caption
    })


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"pdfs": [], "results": []}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


# 🔥 Prüfen ob heute oder morgen ein Event ist
def is_event_today_or_tomorrow():
    today = datetime.today()
    tomorrow = today + timedelta(days=1)

    r = requests.get("https://esv.ch/agenda/")
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()

        if "aktiv" not in text:
            continue

        for d in [today, tomorrow]:
            if d.strftime("%d.%m.%Y") in text:
                return True

    return False


# 📄 PDF Typ erkennen
def detect_type(url):
    u = url.lower()
    if "zwischen" in u:
        return "📊 Zwischenrangliste"
    if "schluss" in u:
        return "🏁 Schlussrangliste"
    if "statistik" in u:
        return "📈 Statistik"
    return "📄 Rangliste"


# 🏁 Resultat erkennen
def parse_result(text):
    if "gewinnt" not in text:
        return None

    parts = text.split("gewinnt")
    before = parts[0][-50:].strip()
    after = parts[1][:80].strip()

    return f"{before} gewinnt {after}"


# 🔍 Ranglisten prüfen
def check_ranglisten():
    state = load_state()

    r = requests.get("https://esv.ch/ranglisten/")
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.find_all("a", href=True):
        if "/ranglisten/" in a["href"] and a["href"].count("/") > 3:

            detail_url = BASE_URL + a["href"]
            detail = requests.get(detail_url)
            dsoup = BeautifulSoup(detail.text, "html.parser")

            title = dsoup.find("h1").get_text(strip=True)
            text = dsoup.get_text(" ", strip=True)

            # Ergebnis posten
            if detail_url not in state["results"]:
                result = parse_result(text)
                if result:
                    zuschauer = "?"
                    if "Zuschauer" in text:
                        try:
                            zuschauer = text.split("Zuschauer")[1].split()[0]
                        except:
                            pass

                    msg = f"""🏆 {title}

🥇 {result}

👀 Zuschauer: {zuschauer}
"""
                    send_message(msg)
                    state["results"].append(detail_url)

            # PDFs posten
            for link in dsoup.find_all("a", href=True):
                if ".pdf" in link["href"]:
                    pdf_url = link["href"]

                    if pdf_url not in state["pdfs"]:
                        typ = detect_type(pdf_url)

                        msg = f"""{typ}

📍 {title}

👉 {pdf_url}
"""
                        send_pdf(pdf_url, msg)
                        state["pdfs"].append(pdf_url)

    save_state(state)


# 📅 Agenda (Freitag)
def check_agenda():
    r = requests.get("https://esv.ch/agenda/")
    soup = BeautifulSoup(r.text, "html.parser")

    today = datetime.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)

    events = {"Samstag": [], "Sonntag": []}

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)

        if "aktiv" not in text.lower():
            continue

        for day, name in [(saturday, "Samstag"), (sunday, "Sonntag")]:
            if day.strftime("%d.%m.%Y") in text:

                detail_url = BASE_URL + a["href"]
                detail = requests.get(detail_url)
                dsoup = BeautifulSoup(detail.text, "html.parser")

                website = ""
                for link in dsoup.find_all("a", href=True):
                    if "http" in link["href"] and "esv.ch" not in link["href"]:
                        website = link["href"]
                        break

                events[name].append((text, website))

    msg = "📅 Schwingfeste dieses Wochenende\n\n"

    for day in ["Samstag", "Sonntag"]:
        if events[day]:
            msg += f"🟢 {day}\n"
            for e, w in events[day]:
                msg += f"• {e}\n"
                if w:
                    msg += f"🔗 {w}\n"
            msg += "\n"

    send_message(msg)


# MAIN
if __name__ == "__main__":
    today = datetime.today()

    if is_event_today_or_tomorrow():
        check_ranglisten()

    if today.weekday() == 4:
        check_agenda()
