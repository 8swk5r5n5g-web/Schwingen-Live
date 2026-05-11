import os
import re
import json
import time
from html import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://esv.ch"
AGENDA_URL = "https://esv.ch/agenda/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

STATE_FILE = "state.json"

AGENDA_POST_HOUR = 12
AGENDA_POST_MINUTE = 30
TIMEZONE = ZoneInfo("Europe/Zurich")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    return {
        "sent_agenda_dates": [],
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def telegram_request_with_retry(url, data, timeout=30, retries=3):
    for attempt in range(1, retries + 1):
        response = requests.post(url, data=data, timeout=timeout)

        print(response.text)

        if response.status_code == 200:
            return response

        if response.status_code == 429:
            try:
                retry_after = response.json().get("parameters", {}).get("retry_after", 30)
            except Exception:
                retry_after = 30

            print(f"Telegram Rate Limit erreicht. Warte {retry_after} Sekunden...")
            time.sleep(retry_after + 1)
            continue

        response.raise_for_status()

    print("Telegram-Nachricht konnte nach mehreren Versuchen nicht gesendet werden.")
    return None


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    telegram_request_with_retry(
        url=url,
        data={
            "chat_id": CHAT_ID,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
        retries=3,
    )


def scraper_url(target_url):
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "false",
    }

    return f"{SCRAPER_API_BASE}?{urlencode(params)}"


def get_soup(url):
    response = requests.get(scraper_url(url), timeout=45)

    print(f"GET via ScraperAPI: {url} -> {response.status_code}")

    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def normalise_url(url):
    if url.startswith("http"):
        return url

    return requests.compat.urljoin(BASE_URL, url)


def clean_text(text):
    return " ".join(text.split()).strip()


def extract_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()

        if href.startswith("http") and "esv.ch" not in href:
            return href

    return ""


def parse_agenda_date(text):
    match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)

    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date()
    except ValueError:
        return None


def weekend_dates_for_next_weekend(today):
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)

    return {saturday.date(), sunday.date()}


def collect_active_agenda_events():
    soup = get_soup(AGENDA_URL)

    now = datetime.now(TIMEZONE)
    target_dates = weekend_dates_for_next_weekend(now)

    events = []
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = clean_text(link.get_text(" ", strip=True))

        if "/agenda/" not in href:
            continue

        if "aktiv" not in text.lower():
            continue

        event_date = parse_agenda_date(text)

        if not event_date:
            continue

        if event_date not in target_dates:
            continue

        detail_url = normalise_url(href)

        if detail_url in seen:
            continue

        seen.add(detail_url)

        website = ""

        try:
            detail_soup = get_soup(detail_url)
            website = extract_website(detail_soup)
        except Exception as exc:
            print(f"Fehler bei Agenda Detailseite: {detail_url}: {exc}")

        clean_event_text = text.replace("aktiv", "")
        clean_event_text = clean_event_text.replace("Aktiv", "")
        clean_event_text = clean_text(clean_event_text)

        events.append(
            {
                "date": event_date,
                "text": clean_event_text,
                "website": website,
            }
        )

    events.sort(key=lambda item: item["date"])

    return events


def build_agenda_message(events):
    lines = [
        "📅 <b>Schwingfeste dieses Wochenende</b>",
        "",
    ]

    if not events:
        lines.append(
            "Aktuell wurden keine Schwingfeste der Aktiven für dieses Wochenende gefunden."
        )
        return "\n".join(lines)

    for event in events:
        date_text = event["date"].strftime("%d.%m.%Y")

        lines.append(f"📍 <b>{escape(event['text'])}</b>")
        lines.append(f"🗓 {date_text}")

        if event["website"]:
            lines.append(f"🌐 {escape(event['website'])}")

        lines.append("")

    return "\n".join(lines).strip()


def should_send_agenda_today(state):
    now = datetime.now(TIMEZONE)
    today_key = now.strftime("%Y-%m-%d")

    if now.weekday() != 4:
        print("Heute ist nicht Freitag. Agenda wird nicht gesendet.")
        return False

    if now.hour != AGENDA_POST_HOUR:
        print("Aktuelle Stunde passt nicht. Agenda wird nicht gesendet.")
        return False

    if now.minute < AGENDA_POST_MINUTE or now.minute >= AGENDA_POST_MINUTE + 30:
        print("Aktuelles Zeitfenster passt nicht. Agenda wird nicht gesendet.")
        return False

    if today_key in state["sent_agenda_dates"]:
        print("Agenda wurde heute bereits gesendet.")
        return False

    return True


def mark_agenda_as_sent(state):
    now = datetime.now(TIMEZONE)
    today_key = now.strftime("%Y-%m-%d")

    if today_key not in state["sent_agenda_dates"]:
        state["sent_agenda_dates"].append(today_key)
        save_state(state)


def check_agenda(state):
    if not should_send_agenda_today(state):
        return

    events = collect_active_agenda_events()
    message = build_agenda_message(events)

    send_message(message)
    mark_agenda_as_sent(state)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    state = load_state()

    print("Starte Wochenend-Vorschau...")

    check_agenda(state)

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
