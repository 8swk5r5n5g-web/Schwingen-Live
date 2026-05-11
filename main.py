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


def pdf_filename(pdf_url):
    name = unquote(pdf_url.split("/")[-1])
    return name.replace(".pdf", "")


def detect_pdf_type(text):
    t = text.lower()

    if "schlussrangliste" in t or "-rl" in t:
        return "🏁 Schlussrangliste"

    if "statistik" in t or "-st" in t:
        return "📈 Statistik"

    if "zwischenrangliste" in t:
        return "📊 Zwischenrangliste"

    return "📄 PDF"


def is_allowed_pdf(href, link_text):
    combined = f"{href} {link_text}".lower()
    return (
        "schlussrangliste" in combined
        or "zwischenrangliste" in combined
        or "statistik" in combined
        or "-rl.pdf" in combined
        or "-st.pdf" in combined
    )


def extract_list_name(pdf_url, link_text, pdf_type):
    text = clean_text(link_text)

    if text:
        return text

    name = pdf_filename(pdf_url).lower()

    if "zwischenrangliste" in name:
        match = re.search(r"zwischenrangliste[_-]?(\d+)", name)
        if match:
            return f"Zwischenrangliste {match.group(1)}"
        return "Zwischenrangliste"

    if "statistik" in name:
        match = re.search(r"statistik[_-]?(\d+)", name)
        if match:
            return f"Statistik {match.group(1)}"
        return "Statistik"

    if "-st" in name:
        return "Statistik"

    if "-rl" in name:
        return "Schlussrangliste"

    return pdf_type.replace("🏁", "").replace("📈", "").replace("📊", "").replace("📄", "").strip()


def extract_fest_name_from_pdf(pdf_url):
    name = pdf_filename(pdf_url)

    name = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", name)
    name = re.sub(r"[-_](RL|ST)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_]?zwischenrangliste[_-]?\d*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_]?statistik[_-]?\d*$", "", name, flags=re.IGNORECASE)

    name = name.replace("_", " ").replace("-", " ")
    return clean_text(name)


def extract_fest_name(soup, pdf_urls):
    h1 = soup.find("h1")
    if h1:
        text = clean_text(h1.get_text(" ", strip=True))
        if text and "Eidgenössischer Schwingerverband" not in text:
            return text

    for url in pdf_urls:
        name = extract_fest_name_from_pdf(url)
        if name:
            return name

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


def build_caption(pdf_type, list_name, fest_name, festinfo):
    lines = [
        f"<b>{escape(pdf_type)} – {escape(list_name)}</b>",
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

    pdf_entries = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = clean_text(a.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not is_allowed_pdf(href, link_text):
            continue

        pdf_url = normalise_url(href)
        pdf_type = detect_pdf_type(f"{href} {link_text}")
        list_name = extract_list_name(pdf_url, link_text, pdf_type)

        pdf_entries.append({
            "url": pdf_url,
            "type": pdf_type,
            "list_name": list_name,
        })

    if not pdf_entries:
        print("Keine passenden PDFs gefunden.")
        return

    fest_name = extract_fest_name(soup, [p["url"] for p in pdf_entries])
    festinfo = extract_festinfo(soup)

    for pdf in pdf_entries:
        caption = build_caption(
            pdf["type"],
            pdf["list_name"],
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
