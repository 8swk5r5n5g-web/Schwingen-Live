import os
import re
import json
import time
import hashlib
from html import escape
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

STATE_FILE = "state.json"
MAX_DETAIL_PAGES = 200


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    if "pdf_hashes" not in state:
        state["pdf_hashes"] = {}

    if "sent_pdfs" in state:
        for url in state["sent_pdfs"]:
            if url not in state["pdf_hashes"]:
                state["pdf_hashes"][url] = ""
        del state["sent_pdfs"]

    if "baseline_done" not in state:
        state["baseline_done"] = False

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def telegram_request_with_retry(url, data, timeout=90, retries=3):
    for attempt in range(1, retries + 1):
        try:
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

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
            print(f"Telegram Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

    print("Telegram konnte nach mehreren Versuchen nicht senden.")
    return None


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
        timeout=90,
        retries=3,
    )


def scraper_url(target_url):
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "false",
    }

    return f"{SCRAPER_API_BASE}?{urlencode(params)}"


def get_page(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            if SCRAPER_API_KEY:
                response = requests.get(scraper_url(url), timeout=90)
                print(f"GET via ScraperAPI Versuch {attempt}: {url} -> {response.status_code}")
            else:
                response = requests.get(
                    url,
                    timeout=90,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                print(f"GET direkt Versuch {attempt}: {url} -> {response.status_code}")

            response.raise_for_status()
            return response.text

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 10
            print(f"Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

    raise RuntimeError(f"Seite konnte nicht geladen werden: {url}")


def get_soup(url):
    return BeautifulSoup(get_page(url), "html.parser")


def normalise_url(url):
    if url.startswith("http"):
        return url
    return requests.compat.urljoin(BASE_URL, url)


def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()


def extract_date_from_text(text):
    match = re.search(r"(\d{2}\.\d{2}\s*\.?\s*\d{4})", text)

    if not match:
        return ""

    date_text = clean_text(match.group(1))
    date_text = date_text.replace(" .", ".")
    date_text = date_text.replace(". ", ".")
    return date_text


def remove_date_from_text(text):
    return clean_text(re.sub(r"\d{2}\.\d{2}\s*\.?\s*\d{4}", "", text))


def is_jung_or_nachwuchs(text):
    text = text.lower()

    blocked_words = [
        "jung",
        "nachwuchs",
        "bueb",
        "bube",
        "buben",
        "schüler",
        "schueler",
        "knaben",
    ]

    return any(word in text for word in blocked_words)


def extract_fest_name_from_overview(overview_text):
    text = clean_text(overview_text)
    text = remove_date_from_text(text)
    text = re.sub(r"\bRangliste\b", "", text, flags=re.IGNORECASE)
    text = clean_text(text)

    parts = text.split()
    lower_parts = [p.lower() for p in parts]

    if "aktiv" in lower_parts:
        aktiv_index = lower_parts.index("aktiv")
        return clean_text(" ".join(parts[:aktiv_index]))

    return ""


def extract_location_from_overview(overview_text):
    text = clean_text(overview_text)
    text = remove_date_from_text(text)
    text = re.sub(r"\bRangliste\b", "", text, flags=re.IGNORECASE)
    text = clean_text(text)

    parts = text.split()
    lower_parts = [p.lower() for p in parts]

    if "aktiv" in lower_parts:
        aktiv_index = lower_parts.index("aktiv")
        return clean_text(" ".join(parts[aktiv_index + 1:]))

    return ""


def collect_all_ranglisten_detail_links():
    soup = get_soup(RANGLISTEN_URL)

    links = []
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if "ranglisten" not in href:
            continue

        full_url = normalise_url(href)

        if full_url.rstrip("/") == RANGLISTEN_URL.rstrip("/"):
            continue

        if "?jahr=" in full_url:
            continue

        if full_url in seen:
            continue

        overview_text = clean_text(link.parent.get_text(" ", strip=True))
        overview_lower = overview_text.lower()

        if "aktiv" not in overview_lower:
            continue

        if is_jung_or_nachwuchs(overview_text):
            continue

        seen.add(full_url)

        links.append(
            {
                "detail_url": full_url,
                "overview_text": overview_text,
                "date_text": extract_date_from_text(overview_text),
                "fest_name": extract_fest_name_from_overview(overview_text),
                "location": extract_location_from_overview(overview_text),
            }
        )

    print(f"Gefundene Aktiv-Ranglisten-Detailseiten total: {len(links)}")

    return links[:MAX_DETAIL_PAGES]


def extract_fest_name_from_detail(soup):
    h1 = soup.find("h1")

    if h1:
        title = clean_text(h1.get_text(" ", strip=True))

        if title and "Eidgenössischer Schwingerverband" not in title:
            return title

    text = clean_text(soup.get_text(" ", strip=True))

    patterns = [
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Kantonales Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwinget)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return clean_text(match.group(1))

    return ""


def extract_location_from_detail(soup):
    text = clean_text(soup.get_text(" ", strip=True))

    patterns = [
        r"\bOrt[:\s]+([A-ZÄÖÜa-zäöü0-9 .'\-]+)",
        r"\bAustragungsort[:\s]+([A-ZÄÖÜa-zäöü0-9 .'\-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            location = clean_text(match.group(1))
            location = re.split(r"\s{2,}| Datum | Rangliste | Anzahl ", location)[0]
            return clean_text(location)

    return ""


def is_blocked_pdf(href, link_text=""):
    combined = f"{href} {link_text}".lower()

    blocked_words = [
        "zwischenrangliste",
        "zwischenrang",
        "/zs",
        "_zs",
        "-zs",
        "gangliste",
        "notizblatt",
        "einteilung",
        "startliste",
        "paarung",
    ]

    return any(word in combined for word in blocked_words)


def is_schlussrangliste(href, link_text=""):
    combined = f"{href} {link_text}".lower()

    if is_blocked_pdf(href, link_text):
        return False

    if "schlussrangliste" in combined:
        return True

    if combined.endswith("-rl.pdf"):
        return True

    if "_rl.pdf" in combined:
        return True

    return False


def is_statistik(href, link_text=""):
    combined = f"{href} {link_text}".lower()

    if is_blocked_pdf(href, link_text):
        return False

    if "statistik" in combined:
        return True

    if combined.endswith("-st.pdf"):
        return True

    if "_st.pdf" in combined:
        return True

    return False


def should_send_pdf(href, link_text=""):
    return is_schlussrangliste(href, link_text) or is_statistik(href, link_text)


def get_pdf_title(href, link_text=""):
    text = clean_text(link_text)

    if is_schlussrangliste(href, text):
        return "Schlussrangliste"

    if is_statistik(href, text):
        if text:
            return text
        return "Statistik"

    return "PDF"


def download_pdf_for_hash(pdf_url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                pdf_url,
                timeout=90,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            print(f"PDF Download Versuch {attempt}: {pdf_url} -> {response.status_code}")

            response.raise_for_status()
            return response.content

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 10
            print(f"PDF Download Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

    raise RuntimeError(f"PDF konnte nicht geladen werden: {pdf_url}")


def get_pdf_hash(pdf_url):
    pdf_content = download_pdf_for_hash(pdf_url)
    return hashlib.sha256(pdf_content).hexdigest()


def pdf_is_new_or_updated(pdf_url, pdf_hash, state):
    old_hash = state["pdf_hashes"].get(pdf_url)

    if old_hash is None:
        state["pdf_hashes"][pdf_url] = pdf_hash
        save_state(state)
        return True

    if old_hash != pdf_hash:
        state["pdf_hashes"][pdf_url] = pdf_hash
        save_state(state)
        return True

    return False


def build_pdf_caption(pdf_title, fest_name, date_text, location, pdf_url):
    lines = [
        f"Datum: {escape(date_text) if date_text else '-'}",
        f"Fest: {escape(fest_name) if fest_name else '-'}",
        f"Ort: {escape(location) if location else '-'}",
        f"Dokument: {escape(pdf_title) if pdf_title else '-'}",
        f"PDF Datei: {escape(pdf_url)}",
    ]

    return "\n".join(lines)


def process_detail_page(entry, state):
    soup = get_soup(entry["detail_url"])

    fest_name = entry.get("fest_name") or extract_fest_name_from_detail(soup) or "Schwingfest"
    date_text = entry.get("date_text")

    if not date_text:
        date_text = extract_date_from_text(clean_text(soup.get_text(" ", strip=True)))

    location = entry.get("location") or extract_location_from_detail(soup)

    print(f"Aktiv-Fest erkannt: {fest_name} / {date_text} / {location}")

    found_relevant_pdf = False

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not should_send_pdf(href, link_text):
            print(f"Ignoriert: {href}")
            continue

        found_relevant_pdf = True

        pdf_url = normalise_url(href)
        pdf_title = get_pdf_title(href, link_text)

        try:
            pdf_hash = get_pdf_hash(pdf_url)
        except Exception as exc:
            print(f"Konnte PDF Hash nicht prüfen: {pdf_url} / {exc}")
            continue

        is_new_or_updated = pdf_is_new_or_updated(pdf_url, pdf_hash, state)

        if not is_new_or_updated:
            print(f"Unverändert: {pdf_url}")
            continue

        if not state["baseline_done"]:
            print(f"Baseline: speichere ohne Senden: {pdf_url}")
            continue

        caption = build_pdf_caption(
            pdf_title=pdf_title,
            fest_name=fest_name,
            date_text=date_text,
            location=location,
            pdf_url=pdf_url,
        )

        print(f"Sende neue oder aktualisierte PDF: {pdf_title} -> {pdf_url}")

        send_document(pdf_url, caption)

        time.sleep(2)

    if not found_relevant_pdf:
        print(f"Keine Statistik oder Schlussrangliste gefunden: {entry['detail_url']}")


def check_ranglisten(state):
    entries = collect_all_ranglisten_detail_links()

    if not state["baseline_done"]:
        print("Baseline-Modus: aktueller Stand wird nur gespeichert, nicht gesendet.")

    for entry in entries:
        try:
            print(f"Pruefe Ranglisten-Fest: {entry['detail_url']}")
            process_detail_page(entry, state)

        except Exception as exc:
            print(f"Fehler bei {entry['detail_url']}: {exc}")

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fertig. Ab dem nächsten Lauf werden neue oder aktualisierte PDFs gesendet.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    state = load_state()

    print("Starte Bot: arls.esv.ch prüfen, nur Aktiv-Feste, Statistik und Schlussrangliste senden...")

    check_ranglisten(state)

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
