import os
import re
import json
import hashlib
from io import BytesIO
from html import escape
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"
MAX_DETAIL_PAGES = 300


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            try:
                state = json.load(file)
            except Exception:
                state = {}
    else:
        state = {}

    if "known_pdfs" not in state:
        state["known_pdfs"] = {}

    if "baseline_done" not in state:
        state["baseline_done"] = False

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def get_page(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException:
            import time
            time.sleep(2)
    raise RuntimeError(f"Seite konnte nicht geladen werden: {url}")


def get_soup(url):
    html = get_page(url)
    return BeautifulSoup(html, "html.parser")


def normalise_url(url):
    if url.startswith("http"):
        return url
    return requests.compat.urljoin(BASE_URL, url)


def clean_text(text):
    return " ".join(text.replace("\xa0", " ").replace(" .", ".").replace(". ", ".").split()).strip()


def extract_date_from_text(text):
    text = clean_text(text)
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    return match.group(0).replace("..", ".") if match else ""


def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y")


def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked_words = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben"]
    return any(word in text for word in blocked_words)


def get_anlass_id(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("anlass", [])
    return values[0] if values else ""


def collect_active_fests():
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"Fehler Übersicht: {e}")
        return []

    grouped = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        full_url = normalise_url(href)
        if "anlass=" not in full_url:
            continue
        anlass_id = get_anlass_id(full_url)
        if not anlass_id:
            continue
        text = clean_text(link.get_text(" ", strip=True))
        if not text:
            continue
        if anlass_id not in grouped:
            grouped[anlass_id] = {"detail_url": full_url, "parts": []}
        grouped[anlass_id]["parts"].append(text)

    entries = []
    for anlass_id, data in grouped.items():
        parts = data["parts"]
        if len(parts) < 5:
            continue
        date_text = extract_date_from_text(parts[0])
        fest_name = clean_text(parts[1])
        category = clean_text(parts[2]).lower()
        location = clean_text(parts[3])
        row_text = clean_text(" ".join(parts))

        if not date_text or category != "aktiv" or is_jung_or_nachwuchs(row_text):
            continue

        entries.append({
            "anlass_id": anlass_id,
            "detail_url": data["detail_url"],
            "fest_name": fest_name,
            "date_text": date_text,
            "location": location,
        })

    if not entries:
        return []

    newest_date = max(entries, key=lambda entry: parse_date(entry["date_text"]))["date_text"]
    filtered = [entry for entry in entries if entry["date_text"] == newest_date]
    return filtered[:MAX_DETAIL_PAGES]


def get_gang_nummer(href, link_text):
    combined = f"{href} {link_text}".lower()
    gang_match = re.search(r"\b([1-6])\b\.?\s*(gang|g\b)", combined)
    if gang_match:
        return int(gang_match.group(1))
    zahlen = re.findall(r"\b([1-6])\b", combined)
    return int(zahlen[-1]) if zahlen else 0


def get_pdf_title(href, link_text, gang_num):
    combined = f"{href} {link_text}".lower()
    if "schluss" in combined or "-rl" in combined or "rangliste" in combined:
        return "Schlussrangliste"
    if gang_num > 0:
        return f"Statistik (nach dem {gang_num}. Gang)"
    return "Statistik"


def download_pdf(pdf_url):
    response = requests.get(
        pdf_url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    response.raise_for_status()
    return response.content


def send_document(pdf_bytes, filename, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {
        "chat_id": CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    requests.post(url, data=data, files=files, timeout=60).raise_for_status()


def process_fest(fest, state):
    try:
        soup = get_soup(fest["detail_url"])
    except Exception:
        return

    page_text = clean_text(soup.get_text(" ", strip=True))
    schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger_txt = schwinger.group(1) if schwinger else ""

    # Fest-Webseite suchen
    website = ""
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            website = href
            break

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))
        combined_meta = f"{href} {link_text}".lower()

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        # Rigoroser Filter gegen Zwischenranglisten
        if any(w in combined_meta for w in ["zwischen", "startliste", "einteilung", "notizblatt", "paarung"]):
            continue

        is_stat = "statistik" in combined_meta or "st_" in combined_meta or "st." in combined_meta
        is_rl = "schluss" in combined_meta or "rangliste" in combined_meta or "-rl" in combined_meta

        if not (is_stat or is_rl):
            continue

        pdf_url = normalise_url(href)
        filename = pdf_url.split("/")[-1].split("?")[0]
        storage_key = f"{fest['anlass_id']}_{filename}"

        if storage_key in state["known_pdfs"]:
            continue

        try:
            pdf_content = download_pdf(pdf_url)
            pdf_hash = hashlib.sha256(pdf_content).hexdigest()
        except Exception:
            continue

        state["known_pdfs"][storage_key] = {
            "hash": pdf_hash,
            "filename": filename,
            "fest": fest.get("fest_name", ""),
        }
        save_state(state)

        # Baseline-Check: Verhindert jeglichen vergangenheits-Spam
        if state["baseline_done"]:
            gang = get_gang_nummer(href, link_text)
            doc_title = get_pdf_title(href, link_text, gang)
            emoji = "🏆" if "Schluss" in doc_title else "📊"

            caption = (
                f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                f"📅 Datum: {escape(fest['date_text'])}\n"
                f"📍 Ort: {escape(fest['location'])}\n"
            )
            if schwinger_txt:
                caption += f"🤼 Anzahl Schwinger: {escape(schwinger_txt)}\n"
            if website:
                caption += f"🌐 <a href='{escape(website)}'>Fest-Webseite</a>\n"
            caption += f"📝 <b>{emoji} {doc_title}</b>"

            print(f"SENDE LIVE-UPDATE: {filename}")
            send_document(pdf_content, filename, caption)
        else:
            print(f"Baseline blockiert alten Stand: {filename}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    fests = collect_active_fests()

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fixiert. Ab dem nächsten automatischen Lauf wird live gesendet.")

    print("Bot-Scan erfolgreich beendet.")


if __name__ == "__main__":
    main()
