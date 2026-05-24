import os
import re
import json
import time
import hashlib
from io import BytesIO
from html import escape
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"
MAX_DETAIL_PAGES = 300

HEADERS = {
    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            try:
                state = json.load(file)
            except Exception:
                state = {}
    else:
        state = {}

    if "known_pdfs" not in state or not isinstance(state["known_pdfs"], dict):
        state["known_pdfs"] = {}

    if "baseline_done" not in state:
        state["baseline_done"] = False

    if not state["known_pdfs"]:
        state["baseline_done"] = False

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()


def normalise_url(url):
    return urljoin(BASE_URL, url)


def get_page(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            print(f"GET Versuch {attempt}: {url} -> {response.status_code}")
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as exc:
            print(f"GET Fehler Versuch {attempt}: {exc}")
            time.sleep(attempt * 3)

    raise RuntimeError(f"Seite konnte nicht geladen werden: {url}")


def get_soup(url):
    return BeautifulSoup(get_page(url), "html.parser")


def extract_date(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", clean_text(text))
    return match.group(0).replace("..", ".") if match else ""


def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y")


def get_anlass_id(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("anlass", [])
    return values[0] if values else ""


def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked_words = [
        "jung", "nachwuchs", "bueb", "bube", "buben",
        "schüler", "schueler", "knaben",
    ]
    return any(word in text for word in blocked_words)


def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)
    grouped = {}

    for link in soup.find_all("a", href=True):
        href = normalise_url(link["href"])

        if "anlass=" not in href:
            continue

        anlass_id = get_anlass_id(href)
        text = clean_text(link.get_text(" ", strip=True))

        if not anlass_id or not text:
            continue

        if anlass_id not in grouped:
            grouped[anlass_id] = {
                "detail_url": href,
                "parts": [],
            }

        grouped[anlass_id]["parts"].append(text)

    entries = []

    for anlass_id, data in grouped.items():
        parts = data["parts"]

        if len(parts) < 5:
            continue

        date_text = extract_date(parts[0])
        fest_name = clean_text(parts[1])
        category = clean_text(parts[2]).lower()
        location = clean_text(parts[3])
        row_text = clean_text(" ".join(parts))

        if not date_text:
            continue

        if category != "aktiv":
            continue

        if is_jung_or_nachwuchs(row_text):
            continue

        entries.append({
            "anlass_id": anlass_id,
            "detail_url": data["detail_url"],
            "date_text": date_text,
            "fest_name": fest_name,
            "location": location,
        })

    if not entries:
        print("Keine Aktiv-Feste gefunden.")
        return []

    newest_date = max(entries, key=lambda entry: parse_date(entry["date_text"]))["date_text"]

    filtered = [
        entry for entry in entries
        if entry["date_text"] == newest_date
    ]

    print(f"Alle Aktiv-Feste gefunden: {len(entries)}")
    print(f"Neuestes Datum auf der Seite: {newest_date}")
    print(f"Aktiv-Feste mit neuestem Datum: {len(filtered)}")

    for fest in filtered:
        print(
            f"Fest gefunden: {fest['fest_name']} / "
            f"{fest['date_text']} / {fest['location']} / {fest['detail_url']}"
        )

    return filtered[:MAX_DETAIL_PAGES]


def extract_fest_infos(soup):
    page_text = clean_text(soup.get_text(" ", strip=True))

    schwinger = ""
    match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)

    if match:
        schwinger = match.group(1)

    website = ""

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()

        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            website = href
            break

    return {
        "schwinger": schwinger,
        "website": website,
    }


def is_real_pdf_url(href):
    return href.lower().split("?")[0].endswith(".pdf")


def is_blocked_document(href, link_text):
    combined = f"{href} {link_text}".lower()

    blocked_words = [
        "zwischenrangliste",
        "zwischenrang",
        "einteilung",
        "einteilungsliste",
        "startliste",
        "notizblatt",
        "paarung",
    ]

    return any(word in combined for word in blocked_words)


def should_send_document(href, link_text):
    if not is_real_pdf_url(href):
        return False

    if is_blocked_document(href, link_text):
        return False

    combined = f"{href} {link_text}".lower()

    is_statistik = (
        "statistik" in combined
        or "-st.pdf" in combined
        or "_st.pdf" in combined
    )

    is_schlussrangliste = (
        "schlussrangliste" in combined
        or "schlussrang" in combined
        or "-rl.pdf" in combined
        or "_rl.pdf" in combined
    )

    return is_statistik or is_schlussrangliste


def get_gang_number(href, link_text):
    combined = f"{href} {link_text}".lower()

    match = re.search(r"nach\s+(\d+)\s+gäng", combined)
    if match:
        return int(match.group(1))

    match = re.search(r"nach\s+einem\s+gang", combined)
    if match:
        return 1

    match = re.search(r"/zs([1-6])/", combined)
    if match:
        return int(match.group(1))

    return 0


def get_document_title(href, link_text):
    text = clean_text(link_text)
    combined = f"{href} {text}".lower()

    if (
        "schlussrangliste" in combined
        or "schlussrang" in combined
        or "-rl.pdf" in combined
        or "_rl.pdf" in combined
    ):
        return "Schlussrangliste"

    if (
        "statistik" in combined
        or "-st.pdf" in combined
        or "_st.pdf" in combined
    ):
        gang = get_gang_number(href, text)

        if gang == 1:
            return "Statistik nach 1 Gang"

        if gang > 1:
            return f"Statistik nach {gang} Gängen"

        return "Statistik"

    return text if text else "PDF"


def download_pdf(pdf_url):
    response = requests.get(pdf_url, headers=HEADERS, timeout=90)
    print(f"PDF Download: {pdf_url} -> {response.status_code}")
    response.raise_for_status()
    return response.content


def send_document(pdf_bytes, filename, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    data = {
        "chat_id": CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    files = {
        "document": (filename, BytesIO(pdf_bytes), "application/pdf")
    }

    response = requests.post(
        telegram_url,
        data=data,
        files=files,
        timeout=120,
    )

    print(response.text)
    response.raise_for_status()


def build_caption(fest, document_title, infos):
    schwinger = infos.get("schwinger", "")
    website = infos.get("website", "")

    lines = [
        f"📅 Datum: {escape(fest.get('date_text', '-'))}",
        f"🏟 Fest: {escape(fest.get('fest_name', '-'))}",
        f"📍 Ort: {escape(fest.get('location', '-'))}",
        f"🤼 Anzahl Schwinger: {escape(schwinger) if schwinger else '-'}",
        f"🌐 Webseite Fest: {escape(website) if website else '-'}",
        f"📄 Dokument: {escape(document_title)}",
    ]

    return "\n".join(lines)


def process_pdf(pdf_url, document_title, fest, infos, state):
    filename = pdf_url.split("/")[-1].split("?")[0]
    storage_key = pdf_url

    if storage_key in state["known_pdfs"]:
        print(f"Bereits bekannt, wird nicht gesendet: {filename}")
        return

    try:
        pdf_content = download_pdf(pdf_url)
        pdf_hash = hashlib.sha256(pdf_content).hexdigest()
    except Exception as exc:
        print(f"PDF konnte nicht geladen werden: {pdf_url} / {exc}")
        return

    state["known_pdfs"][storage_key] = {
        "hash": pdf_hash,
        "url": pdf_url,
        "filename": filename,
        "title": document_title,
        "fest": fest.get("fest_name", ""),
        "date": fest.get("date_text", ""),
        "location": fest.get("location", ""),
    }

    save_state(state)

    if not state["baseline_done"]:
        print(f"Baseline speichert bestehende PDF ohne Senden: {filename}")
        return

    caption = build_caption(fest, document_title, infos)

    print(f"Sende neue PDF: {filename}")
    send_document(pdf_content, filename, caption)


def process_fest(fest, state):
    try:
        soup = get_soup(fest["detail_url"])
    except Exception as exc:
        print(f"Fest konnte nicht geöffnet werden: {fest['detail_url']} / {exc}")
        return

    infos = extract_fest_infos(soup)

    print(
        f"Aktiv-Fest scannen: {fest['fest_name']} / "
        f"{fest['date_text']} / {fest['location']} / "
        f"Schwinger: {infos.get('schwinger', '-')} / "
        f"Website: {infos.get('website', '-')}"
    )

    found = 0

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if not should_send_document(href, link_text):
            continue

        pdf_url = normalise_url(href)
        document_title = get_document_title(href, link_text)

        process_pdf(
            pdf_url=pdf_url,
            document_title=document_title,
            fest=fest,
            infos=infos,
            state=state,
        )

        found += 1
        time.sleep(1)

    print(f"Relevante PDFs gefunden: {found}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()

    print("Starte Bot: Aktiv-Feste scannen, nur Statistik und Schlussrangliste.")
    print(f"Prüfung gestartet: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    fests = collect_active_fests()

    if not state["baseline_done"]:
        print("SICHERHEITS-BASELINE: Bestehende PDFs werden gespeichert, NICHT gesendet.")

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fertig. Ab jetzt werden nur neue PDFs gesendet.")

    print("Bot-Scan erfolgreich beendet.")


if __name__ == "__main__":
    main()
