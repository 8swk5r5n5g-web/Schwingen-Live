import os
import json
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://esv.ch"
RANGLISTEN_URL = f"{BASE_URL}/ranglisten/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

STATE_FILE = "state.json"

MAX_DETAIL_PAGES = 5

ALLOWED_PDF_KEYWORDS = [
    "zwischenrangliste",
    "schlussrangliste",
    "statistik",
]


def send_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
        },
        timeout=30,
    )

    print(response.text)
    response.raise_for_status()


def send_document(file_url: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "document": file_url,
            "caption": caption[:1024],
        },
        timeout=60,
    )

    print(response.text)
    response.raise_for_status()


def build_scraperapi_url(target_url: str) -> str:
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "true",
    }

    return f"{SCRAPER_API_BASE}?{urlencode(params)}"


def get_soup(url: str) -> BeautifulSoup:
    scraper_url = build_scraperapi_url(url)

    response = requests.get(scraper_url, timeout=90)

    print(f"GET {url} -> {response.status_code}")

    response.raise_for_status()

    return BeautifulSoup(response.text, "html.parser")


def normalise_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url

    return requests.compat.urljoin(BASE_URL, url)


def detect_pdf_type(name_or_url: str) -> str:
    value = name_or_url.lower()

    if "schlussrangliste" in value:
        return "🏁 Schlussrangliste"

    if "zwischenrangliste" in value:
        return "📊 Zwischenrangliste"

    if "statistik" in value:
        return "📈 Statistik"

    return "📄 PDF"


def is_allowed_pdf(href: str, link_text: str) -> bool:
    combined = f"{href} {link_text}".lower()

    return any(
        keyword in combined
        for keyword in ALLOWED_PDF_KEYWORDS
    )


def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")

    if h1:
        return h1.get_text(" ", strip=True)

    return "Schwingfest"


def collect_ranglisten_detail_links() -> list[str]:
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

        if full_url in seen:
            continue

        seen.add(full_url)
        links.append(full_url)

    return links


def process_detail_page(detail_url: str) -> None:
    soup = get_soup(detail_url)

    title = extract_title(soup)

    send_message(f"🧪 TESTE:\n{title}")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = link.get_text(" ", strip=True)

        if ".pdf" not in href.lower():
            continue

        if not is_allowed_pdf(href, link_text):
            print(f"IGNORIERT: {link_text}")
            continue

        pdf_url = normalise_url(href)

        pdf_type = detect_pdf_type(f"{href} {link_text}")

        caption = (
            f"{pdf_type}\n\n"
            f"📍 {title}\n"
            f"📝 {link_text}"
        )

        send_document(pdf_url, caption)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    send_message("🚀 Testlauf gestartet")

    detail_links = collect_ranglisten_detail_links()

    detail_links = detail_links[:MAX_DETAIL_PAGES]

    print(detail_links)

    for detail_url in detail_links:
        try:
            process_detail_page(detail_url)
        except Exception as exc:
            print(f"Fehler bei {detail_url}: {exc}")

    send_message("✅ Testlauf beendet")


if __name__ == "__main__":
    main()
