import os
import re
from html import escape
from urllib.parse import urlencode, unquote
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
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
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


def detect_pdf_type(text):
    t = text.lower()

    if "schlussrangliste" in t or "-rl" in t:
        return "🏁 Schlussrangliste"

    if "zwischenrangliste" in t or "zwischenrangliste" in t:
        return "📊 Zwischenrangliste"

    if "statistik" in t or "-st" in t:
        return "📈 Statistik"

    return "📄 PDF"


def is_allowed_pdf(href, link_text):
    combined = f"{href} {link_text}".lower()
    return any(k in combined for k in ALLOWED_PDF_KEYWORDS)


def extract_list_name(pdf_type, link_text):
    text = clean_text(link_text)

    if text:
        return text

    if "Schlussrangliste" in pdf_type:
        return "Schlussrangliste"

    if "Statistik" in pdf_type:
        return "Statistik"

    if "Zwischenrangliste" in pdf_type:
        return "Zwischenrangliste"

    return "PDF"


def event_name_from_pdf_url(pdf_url):
    filename = unquote(pdf_url.split("/")[-1])
    filename = filename.replace(".pdf", "")

    filename = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", filename)
    filename = re.sub(r"[-_](RL|ST)$", "", filename, flags=re.IGNORECASE)
    filename = re.sub(r"[-_]?zwischenrangliste[_-]?\d*$", "", filename, flags=re.IGNORECASE)

    filename = filename.replace("_", " ").replace("-", " ")
    filename = clean_text(filename)

    return filename


def extract_event_name(soup, pdf_urls):
    page_text = clean_text(soup.get_text(" ", strip=True))

    bad_titles = [
        "Eidgenössischer Schwingerverband",
        "Association fédérale de lutte suisse",
        "ESV",
    ]

    h1 = soup.find("h1")
    if h1:
        h1_text = clean_text(h1.get_text(" ", strip=True))
        if h1_text and not any(bad in h1_text for bad in bad_titles):
            return h1_text

    for pdf_url in pdf_urls:
        name = event_name_from_pdf_url(pdf_url)
        if name and len(name) > 5:
            return name

    marker = "Anzahl Schwinger"
    if marker in page_text:
        before = page_text.split(marker)[0]
        before = before.replace("Eidgenössischer Schwingerverband", "")
        before = before.replace("Association fédérale de lutte suisse", "")
        before = before.replace("zurück zur Übersicht", "")
        before = clean_text(before)
        parts = before.split(".")
        if parts:
            return clean_text(parts[-1])

    return "Schwingfest"


def extract_number_after(label, text):
    idx = text.lower().find(label.lower())
    if idx == -1:
        return ""

    snippet = text[idx:idx + 80]
    match = re.search(r"\d+", snippet)
    if match:
        return match.group(0)

    return ""


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


def build_caption(pdf_type, fest_name, list_name, festinfo=None):
    lines = [
        f"<b>{escape(pdf_type)}</b>",
        "",
        f"📍 <b>{escape(fest_name)}</b>",
        f"📄 {escape(list_name)}",
    ]

    if festinfo:
        lines.append("")
        lines.append("ℹ️ <b>Festinfo</b>")
        for item in festinfo:
            lines.append(escape(item))

    return "\n".join(lines)


def process_detail_page(detail_url):
    soup = get_soup(detail_url)

    pdf_entries = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(" ", strip=True)

        if ".pdf" not in href.lower():
            continue

        if not is_allowed_pdf(href, link_text):
            continue

        pdf_url = normalise_url(href)
        pdf_type = detect_pdf_type(f"{href} {link_text}")
        list_name = extract_list_name(pdf_type, link_text)

        pdf_entries.append(
            {
                "url": pdf_url,
                "type": pdf_type,
                "list_name": list_name,
            }
        )

    if not pdf_entries:
        print("Keine passenden PDFs gefunden.")
        return

    fest_name = extract_event_name(soup, [p["url"] for p in pdf_entries])
    festinfo = extract_festinfo(soup)

    for pdf in pdf_entries:
        include_festinfo = "Schlussrangliste" in pdf["type"]

        caption = build_caption(
            pdf["type"],
            fest_name,
            pdf["list_name"],
            festinfo if include_festinfo else None,
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
