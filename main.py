import os
import re
import time
from html import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

AGENDA_URL = "https://arls.esv.ch/agenda/"
TIMEZONE = ZoneInfo("Europe/Zurich")


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


def get_soup(url):
    response = requests.get(url, timeout=45)
    print(f"GET: {url} -> {response.status_code}")
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def clean_text(text):
    return " ".join(text.split()).strip()


def get_next_friday(today):
    days_until_friday = (4 - today.weekday()) % 7

    if days_until_friday == 0:
        days_until_friday = 7

    return today + timedelta(days=days_until_friday)


def weekend_dates_after_friday(friday):
    saturday = friday + timedelta(days=1)
    sunday = friday + timedelta(days=2)

    return {saturday.date(), sunday.date()}


def weekday_de(date_value):
    weekdays = {
        0: "Montag",
        1: "Dienstag",
        2: "Mittwoch",
        3: "Donnerstag",
        4: "Freitag",
        5: "Samstag",
        6: "Sonntag",
    }

    return weekdays[date_value.weekday()]


def parse_agenda_line(line):
    match = re.match(
        r"^(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+aktiv\s+(.+)$",
        line,
        re.IGNORECASE,
    )

    if not match:
        return None

    date_text = match.group(1)
    name = clean_text(match.group(2))
    location = clean_text(match.group(3))

    try:
        event_date = datetime.strptime(date_text, "%d.%m.%Y").date()
    except ValueError:
        return None

    return {
        "date": event_date,
        "name": name,
        "location": location,
        "website": "",
    }


def collect_active_agenda_events_for_dates(target_dates):
    soup = get_soup(AGENDA_URL)

    main_text = soup.get_text("\n", strip=True)
    lines = [clean_text(line) for line in main_text.splitlines() if clean_text(line)]

    events = []
    seen = set()

    website_links = {}

    for link in soup.find_all("a", href=True):
        link_text = clean_text(link.get_text(" ", strip=True))
        href = link["href"].strip()

        if link_text.lower() == "website":
            website_links[href] = href

    for line in lines:
        event = parse_agenda_line(line)

        if not event:
            continue

        if event["date"] not in target_dates:
            continue

        key = f"{event['date']}-{event['name']}-{event['location']}"

        if key in seen:
            continue

        seen.add(key)

        events.append(event)

    events.sort(key=lambda item: (item["date"], item["name"]))

    return events


def build_agenda_message(events, friday, target_dates):
    sorted_dates = sorted(target_dates)

    lines = [
        "🤼 <b>Schwingfeste am kommenden Wochenende</b>",
        "",
        f"📆 Vorschau vom Freitag, {friday.strftime('%d.%m.%Y')}",
        f"🗓 Wochenende: {sorted_dates[0].strftime('%d.%m.%Y')} bis {sorted_dates[1].strftime('%d.%m.%Y')}",
        "",
    ]

    if not events:
        lines.append("Für dieses Wochenende wurden keine Aktiv-Schwingfeste gefunden.")
        return "\n".join(lines)

    current_date = None

    for event in events:
        if event["date"] != current_date:
            current_date = event["date"]
            lines.append(f"📌 <b>{weekday_de(event['date'])}, {event['date'].strftime('%d.%m.%Y')}</b>")
            lines.append("")

        lines.append(f"🏟 <b>{escape(event['name'])}</b>")
        lines.append(f"📍 {escape(event['location'])}")

        if event["website"]:
            lines.append(f"🌐 {escape(event['website'])}")

        lines.append("")

    lines.append("Viel Spass im Sägemehl! 💪")

    return "\n".join(lines).strip()


def check_agenda_testlauf():
    now = datetime.now(TIMEZONE)

    next_friday = get_next_friday(now)
    target_dates = weekend_dates_after_friday(next_friday)

    print(f"Testlauf für kommenden Freitag: {next_friday.strftime('%Y-%m-%d')}")
    print(f"Ziel-Daten: {[date.strftime('%Y-%m-%d') for date in sorted(target_dates)]}")

    events = collect_active_agenda_events_for_dates(target_dates)

    message = build_agenda_message(
        events=events,
        friday=next_friday,
        target_dates=target_dates,
    )

    print(message)

    send_message(message)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    print("Starte Testlauf für kommenden Freitag...")

    check_agenda_testlauf()

    print("Testlauf beendet.")


if __name__ == "__main__":
    main()
