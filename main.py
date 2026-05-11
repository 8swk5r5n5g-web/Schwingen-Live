import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://esv.ch"
RANGLISTEN_URL = "https://esv.ch/ranglisten/"

STATE_FILE = "state.json"

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return []


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_telegram_document(file_url, caption):
    filename = file_url.split("/")[-1]

    response = requests.get(file_url, headers=HEADERS)
    response.raise_for_status()

    files = {
        "document": (filename, response.content)
    }

    data = {
        "chat_id": CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML"
    }

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    r = requests.post(url, data=data, files=files)
    print(r.text)


def get_soup(url):
    if SCRAPER_API_KEY:
        payload = {
            "api_key": SCRAPER_API_KEY,
            "url": url,
            "render": "true"
        }

        response = requests.get(
            "https://api.scraperapi.com/",
            params=payload,
            timeout=60
        )

    else:
        response = requests.get(url, headers=HEADERS, timeout=60)

    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def extract_festinfos(text):
    schwinger = "-"
    zuschauer = "-"
    website = "-"

    lines = text.split("\n")

    for line in lines:
        l = line.strip()

        if "Anzahl Schwinger" in l:
            schwinger = l.split(":")[-1].strip()

        if "Anzahl Zuschauer" in l:
            zuschauer = l.split(":")[-1].strip()

        if "Website" in l:
            website = l.split(":")[-1].strip()

    return schwinger, zuschauer, website


def is_statistik_1_to_6(link_text, href):
    text = f"{link_text} {href}".lower()

    return (
        "statistik" in text and
        any(f"_{i}" in text or f" {i}" in text for i in range(1, 7))
    )


def is_schlussrangliste(link_text, href):
    text = f"{link_text} {href}".lower()

    return "schlussrangliste" in text


def should_send_pdf(link_text, href):
    text = f"{link_text} {href}".lower()

    # Zwischenranglisten komplett ignorieren
    if "zwischenrangliste" in text:
        return False

    # Statistik 1-6 erlauben
    if is_statistik_1_to_6(link_text, href):
        return True

    # Schlussrangliste erlauben
    if is_schlussrangliste(link_text, href):
        return True

    return False


def build_caption(title, fest_name, schwinger, zuschauer, website):
    icon = "📄"

    lower = title.lower()

    if "schlussrangliste" in lower:
        icon = "🏁"

    elif "statistik" in lower:
        icon = "📈"

    caption = (
        f"{icon} <b>{title}</b>\n\n"
        f"📍 {fest_name}\n\n"
        f"ℹ️ <b>Festinfos</b>\n"
        f"🤼 Anzahl Schwinger: {schwinger}\n"
        f"👥 Anzahl Zuschauer: {zuschauer}\n"
        f"🌐 Website: {website}"
    )

    return caption


def check_ranglisten():
    print("Prüfe Ranglisten...")

    sent_files = load_state()

    soup = get_soup(RANGLISTEN_URL)

    text = soup.get_text("\n")

    schwinger, zuschauer, website = extract_festinfos(text)

    links = soup.find_all("a")

    for link in links:
        href = link.get("href")

        if not href:
            continue

        if ".pdf" not in href.lower():
            continue

        link_text = link.get_text(" ", strip=True)

        if not should_send_pdf(link_text, href):
            continue

        full_url = urljoin(BASE_URL, href)

        if full_url in sent_files:
            continue

        title = link_text.strip()

        fest_name = "Schwingfest"

        h1 = soup.find("h1")
        if h1:
            fest_name = h1.get_text(strip=True)

        caption = build_caption(
            title,
            fest_name,
            schwinger,
            zuschauer,
            website
        )

        try:
            send_telegram_document(full_url, caption)

            sent_files.append(full_url)
            save_state(sent_files)

            print(f"Gesendet: {title}")

        except Exception as e:
            print(f"Fehler beim Senden: {e}")


if __name__ == "__main__":
    check_ranglisten()
