import os
import re
import time
from html import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
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


def normalise_url(url):
    return urljoin(BASE_URL, url)


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


def get_next_friday(today):
    days_until_friday = (4 - today.weekday()) % 7

    if days_until_friday == 0:
        days_until_friday = 7

    return today + timedelta(days=days_until_friday)


def weekend_dates_after_friday(friday):
    saturday = friday + timedelta(days=1)
    sunday = friday + timedelta(days=2)

    return {saturday.date(), sunday.date()}


def parse_event_from_segment(segment_html):
    segment_soup = BeautifulSoup(segment_html, "html.parser")
    text = clean_text(segment_soup.get_text(" ", strip=True))

    date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)

    if not date_match:
        return None

    date_text = date_match.group(1)

    try:
        event_date = datetime.strptime(date_text, "%d.%m.%Y").date()
    except ValueError:
        return None

    after_date = clean_text(text[date_match.end():])

    aktiv_match = re.search(r"aktiv", after_date, re.IGNORECASE)

    if not aktiv_match:
        return None

    name = clean_text(after_date[:aktiv_match.start()])
    rest = clean_text(after_date[aktiv_match.end():])

    rest = rest.replace("Website", "")
    rest = clean_text(rest)

    website = ""

    for link in segment_soup.find_all("a", href=True):
        link_text = clean_text(link.get_text(" ", strip=True))

        if "website" in link_text.lower():
            website = normalise_url(link["href"].strip())
            break

    if not name:
        return None

    return {
        "date": event_date,
        "name": name,
        "location": rest,
        "website": website,
    }


def collect_active_agenda_events_for_dates(target_dates):
    soup = get_soup(AGENDA_URL)

    html = str(soup)

    segments = re.split(r"(?=\d{2}\.\d{2}\.\d{4})", html)

    events = []
    seen = set()

    print(f"Agenda-Segmente gefunden: {len(segments)}")

    for segment in segments:
        if not re.search(r"\d{2}\.\d{2}\.\d{4}", segment):
            continue

        event = parse_event_from_segment(segment)

        if not event:
            continue

        print(
            f"Agenda erkannt: {event['date']} | {event['name']} | {event['location']} | {event['website']}"
        )

        if event["date"] not in target_dates:
            continue

        key = f"{event['date']}-{event['name']}-{event['location']}"

        if key in seen:
            continue

        seen.add(key)
        events.append(event)

    events.sort(key=lambda item: (item["date"], item["name"]))

    print(f"Aktiv-Schwingfeste für Ziel-Wochenende gefunden: {len(events)}")

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
            lines.append(
                f"📌 <b>{weekday_de(event['date'])}, {event['date'].strftime('%d.%m.%Y')}</b>"
            )
            lines.append("")

        lines.append(f"🏟 <b>{escape(event['name'])}</b>")

        if event["location"]:
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

    print(f"Heutiges Datum: {now.strftime('%Y-%m-%d')}")
    print(f"Testlauf für kommenden Freitag: {next_friday.strftime('%Y-%m-%d')}")
    print(f"Ziel-Daten: {[date.strftime('%Y-%m-%d') for date in sorted(target_dates)]}")

    events = collect_active_agenda_events_for_dates(target_dates)

    message = build_agenda_message(
        events=events,
        friday=next_friday,
        target_dates=target_dates,
    )

    print("Telegram-Nachricht:")
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
