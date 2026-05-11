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

MAX_DETAIL_PAGES = 5


def send_document(pdf_url, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
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


def clean_text(text):
    return " ".join(text.split()).strip()


def extract_fest_name(soup):
    text = clean_text(soup.get_text(" ", strip=True))

    match = re.search(
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwingfest)",
        text
    )

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

    snippet = text[idx:idx + 80]
    match = re.search(r"\d+", snippet)
    return match.group(0) if match else ""


def extract_website(soup):
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            return href
    return ""


def extract_festinfo(soup):
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


def is_statistik_1_to_6(link_text, href):
    text = f"{link_text} {href}".lower()

    if "statistik" not in text:
        return False

    if "zwischenrangliste" in text:
        return False

    patterns = [
        r"statistik[_\- ]?([1-6])",
        r"statistik nach einem gang",
        r"statistik nach ([2-6]) g",
    ]

    for pattern in patterns:
        if re.search(pattern, text):
            return True

    return False


def is_schlussrangliste(link_text, href):
    text = f"{link_text} {href}".lower()

    if "schlussrangliste" in text:
        return True

    if re.search(r"[-_]?rl\.pdf$", href.lower()):
        return True

    return False


def get_pdf_category(link_text, href):
    if is_schlussrangliste(link_text, href):
        return "🏁"

    if is_statistik_1_to_6(link_text, href):
        return "📈"

    return ""


def should_send_pdf(link_text, href):
    return is_statistik_1_to_6(link_text, href) or is_schlussrangliste(link_text, href)


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


def build_caption(icon, pdf_title, fest_name, festinfo):
    lines = [
        f"{icon} <b>{escape(pdf_title)}</b>",
        "",
        f"📍 <b>{escape(fest_name)}</b>",
    ]

    if festinfo:
        lines.append("")
        lines.append("ℹ️ <b>Festinfos</b>")
        for item in festinfo:
            lines.append(escape(item))

    return "\n".join(lines)


def process_detail_page(detail_url):
    soup = get_soup(detail_url)

    fest_name = extract_fest_name(soup)
    festinfo = extract_festinfo(soup)

    pdf_entries = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = clean_text(a.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not should_send_pdf(link_text, href):
            print(f"Ignoriert: {link_text} / {href}")
            continue

        pdf_url = normalise_url(href)
        icon = get_pdf_category(link_text, href)

        pdf_title = link_text if link_text else "PDF"

        pdf_entries.append({
            "url": pdf_url,
            "icon": icon,
            "title": pdf_title,
        })

    if not pdf_entries:
        print("Keine passenden PDFs gefunden.")
        return

    for pdf in pdf_entries:
        caption = build_caption(
            pdf["icon"],
            pdf["title"],
            fest_name,
            festinfo,
        )

        send_document(pdf["url"], caption)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    detail_links = collect_detail_links()

    print(f"Gefundene Detailseiten: {len(detail_links)}")

    for detail_url in detail_links:
        try:
            print(f"Pruefe: {detail_url}")
            process_detail_page(detail_url)
        except Exception as exc:
            print(f"Fehler bei {detail_url}: {exc}")


if __name__ == "__main__":
    main()
