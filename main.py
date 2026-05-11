import os
import re
from html import escape
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://esv.ch"
RANGLISTEN_URL = "https://esv.ch/ranglisten/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

STATE_FILE = "state.json"
TEST_MODE = True

MAX_DETAIL_PAGES = 5


def send_document(pdf_url, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    response = requests.post(
        telegram_url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=60,
    )

    print(response.text)
    response.raise_for_status()


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


def is_statistik_1_to_6(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    if "statistik" not in text and "-st.pdf" not in text and "_st.pdf" not in text:
        return False

    patterns = [
        r"statistik nach einem gang",
        r"statistik nach 1 gang",
        r"statistik[_\- ]?1",
        r"statistik nach 2 g",
        r"statistik[_\- ]?2",
        r"statistik nach 3 g",
        r"statistik[_\- ]?3",
        r"statistik nach 4 g",
        r"statistik[_\- ]?4",
        r"statistik nach 5 g",
        r"statistik[_\- ]?5",
        r"statistik nach 6 g",
        r"statistik[_\- ]?6",
        r"[-_]st\.pdf",
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def should_send_pdf(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    if is_statistik_1_to_6(link_text, href):
        return True

    if is_schlussrangliste(link_text, href):
        return True

    return False


def get_icon(link_text, href):
    if is_schlussrangliste(link_text, href):
        return "🏁"

    if is_statistik_1_to_6(link_text, href):
        return "📈"

    return "📄"


def get_pdf_title(link_text, href):
    title = clean_text(link_text)

    if title:
        return title

    filename = href.split("/")[-1].replace(".pdf", "")

    if is_schlussrangliste(link_text, href):
        return "Schlussrangliste"

    if "statistik_1" in filename.lower():
        return "Statistik nach einem Gang"

    if "statistik_2" in filename.lower():
        return "Statistik nach 2 Gängen"

    if "statistik_3" in filename.lower():
        return "Statistik nach 3 Gängen"

    if "statistik_4" in filename.lower():
        return "Statistik nach 4 Gängen"

    if "statistik_5" in filename.lower():
        return "Statistik nach 5 Gängen"

    if "statistik_6" in filename.lower():
        return "Statistik nach 6 Gängen"

    if is_statistik_1_to_6(link_text, href):
        return "Statistik"

    return "PDF"


def build_caption(icon, pdf_title, fest_name, festinfos):
    lines = [
        f"{icon} <b>{escape(pdf_title)}</b>",
        "",
        f"📍 <b>{escape(fest_name)}</b>",
    ]

    if festinfos:
        lines.append("")
        lines.append("ℹ️ <b>Festinfos</b>")
        for info in festinfos:
            lines.append(escape(info))

    return "\n".join(lines)


def collect_detail_links():
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


def process_detail_page(detail_url):
    soup = get_soup(detail_url)

    fest_name = extract_fest_name(soup)
    festinfos = extract_festinfos(soup)

    pdfs = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not should_send_pdf(link_text, href):
            print(f"Ignoriert: {link_text} / {href}")
            continue

        pdf_url = normalise_url(href)
        icon = get_icon(link_text, href)
        pdf_title = get_pdf_title(link_text, href)

        pdfs.append(
            {
                "url": pdf_url,
                "icon": icon,
                "title": pdf_title,
            }
        )

    if not pdfs:
        print(f"Keine passenden PDFs gefunden: {fest_name}")
        return

    for pdf in pdfs:
        caption = build_caption(
            pdf["icon"],
            pdf["title"],
            fest_name,
            festinfos,
        )

        send_document(pdf["url"], caption)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    print("Starte Testlauf...")

    detail_links = collect_detail_links()

    print(f"Gefundene Detailseiten: {len(detail_links)}")

    for detail_url in detail_links:
        try:
            print(f"Pruefe: {detail_url}")
            process_detail_page(detail_url)
        except Exception as exc:
            print(f"Fehler bei {detail_url}: {exc}")

    print("Testlauf beendet.")


if __name__ == "__main__":
    main()
