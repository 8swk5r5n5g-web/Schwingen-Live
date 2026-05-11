import os
import re
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
RANGLISTEN_URL = "https://esv.ch/ranglisten/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

TIMEZONE = ZoneInfo("Europe/Zurich")
MAX_DETAIL_PAGES = 20


def telegram_request_with_retry(url, data, timeout=60, retries=3):
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

    print("Telegram konnte nach mehreren Versuchen nicht senden.")
    return None


def send_message(text):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    telegram_request_with_retry(
        url=telegram_url,
        data={
            "chat_id": CHAT_ID,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
        retries=3,
    )


def send_document(pdf_url, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    telegram_request_with_retry(
        url=telegram_url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=60,
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


def get_last_weekend_dates():
    today = datetime.now(TIMEZONE).date()

    days_since_sunday = (today.weekday() - 6) % 7
    if days_since_sunday == 0:
        days_since_sunday = 7

    last_sunday = today - timedelta(days=days_since_sunday)
    last_saturday = last_sunday - timedelta(days=1)

    return {last_saturday, last_sunday}


def extract_event_date(soup):
    text = clean_text(soup.get_text(" ", strip=True))

    matches = re.findall(r"\d{2}\.\d{2}\.\d{4}", text)

    for date_text in matches:
        try:
            return datetime.strptime(date_text, "%d.%m.%Y").date()
        except ValueError:
            continue

    return None


def extract_number_after(label, text):
    idx = text.lower().find(label.lower())

    if idx == -1:
        return ""

    snippet = text[idx:idx + 100]
    match = re.search(r"\d+", snippet)

    return match.group(0) if match else ""


def extract_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()

        if href.startswith("http") and "esv.ch" not in href:
            return href

    return ""


def extract_fest_name(soup):
    page_text = clean_text(soup.get_text(" ", strip=True))

    patterns = [
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Kantonales Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwinget)",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            return clean_text(match.group(1))

    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
        if "Eidgenössischer Schwingerverband" not in title:
            return title

    return "Schwingfest"


def extract_festinfos(soup):
    text = clean_text(soup.get_text(" ", strip=True))

    schwinger = extract_number_after("Anzahl Schwinger", text)
    zuschauer = extract_number_after("Anzahl Zuschauer", text)
    website = extract_website(soup)

    lines = []

    if schwinger:
        lines.append(f"🤼 Anzahl Schwinger: {schwinger}")

    if zuschauer:
        lines.append(f"👥 Anzahl Zuschauer: {zuschauer}")

    if website:
        lines.append(f"🌐 Website: {website}")

    return lines


def is_schlussrangliste(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    return (
        "schlussrangliste" in text
        or "-rl.pdf" in text
        or "_rl.pdf" in text
    )


def is_statistik_6(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    patterns = [
        r"statistik nach 6",
        r"statistik nach sechs",
        r"statistik[_\- ]?6",
        r"6\.?\s*gang",
        r"6\.?\s*gängen",
        r"6\.?\s*gaengen",
        r"[-_]st6",
        r"[-_]st_6",
        r"statistik-6",
        r"statistik_6",
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def should_send_pdf(link_text, href):
    if is_schlussrangliste(link_text, href):
        return True

    if is_statistik_6(link_text, href):
        return True

    return False


def get_icon(link_text, href):
    if is_schlussrangliste(link_text, href):
        return "🏁"

    if is_statistik_6(link_text, href):
        return "📈"

    return "📄"


def get_pdf_title(link_text, href):
    title = clean_text(link_text)

    if is_schlussrangliste(link_text, href):
        return "Schlussrangliste"

    if is_statistik_6(link_text, href):
        return "Statistik nach 6 Gängen"

    if title:
        return title

    return "PDF"


def build_pdf_caption(icon, pdf_title, fest_name, event_date, festinfos):
    date_text = event_date.strftime("%d.%m.%Y") if event_date else ""

    lines = [
        f"{icon} <b>{escape(pdf_title)}</b>",
        "",
        f"📍 <b>{escape(fest_name)}</b>",
    ]

    if date_text:
        lines.append(f"🗓 {date_text}")

    if festinfos:
        lines.append("")
        lines.append("ℹ️ <b>Festinfos</b>")
        for info in festinfos:
            lines.append(escape(info))

    return "\n".join(lines)


def collect_ranglisten_detail_links():
    soup = get_soup(RANGLISTEN_URL)

    links = []
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if "/ranglisten/" not in href:
            continue

        full_url = normalise_url(href)

        if full_url.rstrip("/") == RANGLISTEN_URL.rstrip("/"):
            continue

        if "?jahr=" in full_url:
            continue

        if full_url in seen:
            continue

        seen.add(full_url)
        links.append(full_url)

    return links[:MAX_DETAIL_PAGES]


def process_ranglisten_detail_page(detail_url, target_dates):
    soup = get_soup(detail_url)

    event_date = extract_event_date(soup)

    if event_date not in target_dates:
        print(f"Ignoriert, nicht vergangenes Wochenende: {detail_url} / Datum: {event_date}")
        return 0

    fest_name = extract_fest_name(soup)
    festinfos = extract_festinfos(soup)

    print(f"Fest vom vergangenen Wochenende erkannt: {fest_name} / {event_date}")

    sent_count = 0
    sent_urls = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not should_send_pdf(link_text, href):
            print(f"Ignoriert: {link_text} / {href}")
            continue

        pdf_url = normalise_url(href)

        if pdf_url in sent_urls:
            continue

        sent_urls.add(pdf_url)

        icon = get_icon(link_text, href)
        pdf_title = get_pdf_title(link_text, href)

        caption = build_pdf_caption(
            icon=icon,
            pdf_title=pdf_title,
            fest_name=fest_name,
            event_date=event_date,
            festinfos=festinfos,
        )

        print(f"Sende PDF: {pdf_title} -> {pdf_url}")

        send_document(pdf_url, caption)

        sent_count += 1
        time.sleep(2)

    return sent_count


def send_intro_message(target_dates):
    sorted_dates = sorted(target_dates)

    message = f"""
🤼 <b>Schwingen Live ist zurück</b>

Hier folgen nochmals die Schlussranglisten und die Statistik nach 6 Gängen vom vergangenen Wochenende.

📅 <b>Wochenende:</b> {sorted_dates[0].strftime('%d.%m.%Y')} / {sorted_dates[1].strftime('%d.%m.%Y')}

Ab sofort sollte wieder alles wie gewohnt laufen 💪

Danke für eure Geduld und viel Spass im Sägemehl!
""".strip()

    send_message(message)
    time.sleep(2)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    print("Starte einmaligen Nachversand vom vergangenen Wochenende...")

    target_dates = get_last_weekend_dates()

    print(f"Ziel-Daten: {[date.strftime('%Y-%m-%d') for date in sorted(target_dates)]}")

    send_intro_message(target_dates)

    detail_links = collect_ranglisten_detail_links()

    print(f"Gefundene Ranglisten-Detailseiten: {len(detail_links)}")

    total_sent = 0

    for detail_url in detail_links:
        try:
            print(f"Pruefe Rangliste: {detail_url}")
            total_sent += process_ranglisten_detail_page(detail_url, target_dates)
        except Exception as exc:
            print(f"Fehler bei {detail_url}: {exc}")

    print(f"Einmaliger Nachversand beendet. Gesendete PDFs: {total_sent}")


if __name__ == "__main__":
    main()
