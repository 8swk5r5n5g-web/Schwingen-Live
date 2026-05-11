import os
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://esv.ch"
RANGLISTEN_URL = "https://esv.ch/ranglisten/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

MAX_DETAIL_PAGES = 5

ALLOWED_PDF_KEYWORDS = [
    "zwischenrangliste",
    "schlussrangliste",
    "statistik",
]


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    print(r.text)
    r.raise_for_status()


def send_document(pdf_url, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption[:1024],
        },
        timeout=60,
    )
    print(r.text)
    r.raise_for_status()


def scraper_url(target_url):
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "false",
    }
    return f"{SCRAPER_API_BASE}?{urlencode(params)}"


def get_soup(url):
    r = requests.get(scraper_url(url), timeout=45)
    print(f"GET via ScraperAPI: {url} -> {r.status_code}")
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def normalise_url(url):
    if url.startswith("http"):
        return url
    return requests.compat.urljoin(BASE_URL, url)


def extract_title(soup):
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return "Schwingfest"


def detect_pdf_type(text):
    t = text.lower()
    if "schlussrangliste" in t:
        return "🏁 Schlussrangliste"
    if "zwischenrangliste" in t:
        return "📊 Zwischenrangliste"
    if "statistik" in t:
        return "📈 Statistik"
    return "📄 PDF"


def is_allowed_pdf(href, link_text):
    combined = f"{href} {link_text}".lower()
    return any(k in combined for k in ALLOWED_PDF_KEYWORDS)


def collect_detail_links():
    soup = get_soup(RANGLISTEN_URL)

    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]

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


def process_detail_page(detail_url):
    soup = get_soup(detail_url)
    title = extract_title(soup)

    send_message(f"🧪 Teste Ranglisten:\n{title}")

    found = 0

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(" ", strip=True)

        if ".pdf" not in href.lower():
            continue

        if not is_allowed_pdf(href, link_text):
            print(f"Ignoriert: {link_text} / {href}")
            continue

        pdf_url = normalise_url(href)
        pdf_type = detect_pdf_type(f"{href} {link_text}")

        caption = f"{pdf_type}\n\n📍 {title}"
        if link_text:
            caption += f"\n📝 {link_text}"

        send_document(pdf_url, caption)
        found += 1

    if found == 0:
        print(f"Keine passenden PDFs gefunden: {title}")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")
    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")
    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    send_message("🚀 Testlauf gestartet")

    detail_links = collect_detail_links()
    print(f"Gefundene Detailseiten: {len(detail_links)}")

    for detail_url in detail_links:
        try:
            process_detail_page(detail_url)
        except Exception as exc:
            print(f"Fehler bei {detail_url}: {exc}")

    send_message("✅ Testlauf beendet")


if __name__ == "__main__":
    main()
