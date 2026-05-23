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

HEADERS = {
    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/4.0",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def load_state():
    # Wenn manuell auf "Run workflow" geklickt wird -> Speicher ignorieren und ALLES von heute rausballern!
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Manueller Start: Speicher wird ignoriert! Alle aktuellen PDFs werden jetzt gesendet.")
        return {"known_pdfs": {}, "baseline_done": True}

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    if "known_pdfs" not in state or not isinstance(state["known_pdfs"], dict):
        state["known_pdfs"] = {}

    if "baseline_done" not in state:
        state["baseline_done"] = False

    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()

def normalise_url(url):
    return urljoin(BASE_URL, url)

def get_page(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response.text

def get_soup(url):
    return BeautifulSoup(get_page(url), "html.parser")

def extract_date(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    if not match:
        return ""
    return match.group(0).replace("..", ".")

def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y")

def get_anlass_id(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("anlass", [])
    return values[0] if values else ""

def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben"]
    return any(word in text for word in blocked)

def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)
    grouped = {}

    # ORIGINALER PARSER: Gruppiert stur alle Links, die zum selben Fest gehören
    for link in soup.find_all("a", href=True):
        href = normalise_url(link["href"])
        if "anlass=" not in href:
            continue

        anlass_id = get_anlass_id(href)
        text = clean_text(link.get_text(" ", strip=True))

        if not anlass_id or not text:
            continue

        if anlass_id not in grouped:
            grouped[anlass_id] = {"detail_url": href, "parts": []}
        grouped[anlass_id]["parts"].append(text)

    entries = []
    
    for data in grouped.values():
        parts = data["parts"]
        row_text = " ".join(parts).lower()
        date_text = extract_date(" ".join(parts))

        if not date_text:
            continue
            
        # Nachwuchs blockieren und 'Aktiv' voraussetzen
        if is_jung_or_nachwuchs(row_text):
            continue
        if "aktiv" not in row_text:
            continue

        # Sichere Festnamen-Erkennung (nimmt den längsten Textteil, der kein Datum/Kategorie ist)
        fest_name = "Schwingfest"
        for p in parts:
            cleaned = clean_text(p)
            if len(cleaned) > 5 and not extract_date(cleaned) and cleaned.lower() != "aktiv":
                fest_name = cleaned
                break

        entries.append({
            "detail_url": data["detail_url"],
            "date_text": date_text,
            "fest_name": fest_name,
            "parsed_date": parse_date(date_text)
        })

    if not entries:
        print("Keine Aktiv-Feste auf der ESV-Seite gefunden.")
        return []

    # UNFEHLBARE DATUMS-LOGIK: Wir suchen das absolut neueste Datum AUF DER SEITE.
    # Egal ob 2024 oder 2026, der Bot nimmt automatisch die Feste vom aktuellsten Wochenende!
    max_date = max(e["parsed_date"] for e in entries)
    
    current_weekend_fests = []
    for e in entries:
        # Alles was max 2 Tage vom neuesten Datum entfernt ist (fängt SA & SO ein)
        if abs((e["parsed_date"] - max_date).days) <= 2:
            current_weekend_fests.append(e)

    print(f"Laufendes Wochenende erkannt: Rund um den {max_date.strftime('%d.%m.%Y')}")
    print(f"Treffer! {len(current_weekend_fests)} Aktiv-Feste gefunden:")
    for f in current_weekend_fests:
        print(f"-> {f['fest_name']}")
        
    return current_weekend_fests

def extract_number_after(label, text):
    match = re.search(rf"{re.escape(label)}\s+(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""

def extract_fest_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            return href
    return ""

def should_track_pdf(href, title):
    combined = f"{href} {title}".lower()
    if not href.lower().split("?")[0].endswith(".pdf"):
        return False

    blocked = ["zwischenrangliste", "zwischenrang", "startliste", "einteilung", "notizblatt", "paarung"]
    if any(word in combined for word in blocked):
        return False

    return (
        "statistik" in combined or "-st.pdf" in combined or "_st.pdf" in combined or
        "schlussrangliste" in combined or "schlussrang" in combined or 
        "-rl.pdf" in combined or "_rl.pdf" in combined
    )

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    response = requests.post(url, data=data, files=files, timeout=120)
    response.raise_for_status()

def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    page_text = clean_text(soup.get_text(" "))
    
    schwinger = extract_number_after("Anzahl Schwinger", page_text)
    website = extract_fest_website(soup)

    print(f"Prüfe Unterseite auf PDFs: {fest['fest_name']}")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        title = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, title):
            continue

        pdf_url = urljoin(BASE_URL, href)
        filename = pdf_url.split("/")[-1].split("?")[0]

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=90)
            res.raise_for_status()
            pdf_bytes = res.content
            pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()

            old_entry = state["known_pdfs"].get(pdf_url)
            old_hash = old_entry.get("hash", "") if isinstance(old_entry, dict) else old_entry

            # Wurde EXAKT dieser Hash (Dateiversion) schon mal gesendet? -> Ignorieren!
            if old_entry is not None and old_hash == pdf_hash:
                continue

            # Neuen Hash in den Speicher eintragen
            state["known_pdfs"][pdf_url] = {"hash": pdf_hash}
            save_state(state)

            if not state["baseline_done"]:
                print(f"Stille Sicherung (Baseline): {filename}")
                continue

            # Nachricht absenden (Das kompakte Design!)
            doc_emoji = "🏆 Schlussrangliste" if "schluss" in filename.lower() or "rl" in filename.lower() else "📊 Statistik"
            
            caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
            if schwinger:
                caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
            caption += f"📝 <b>{doc_emoji}</b>"

            buttons = []
            if website:
                buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            buttons.append({"text": "🔗 Direktlink ESV", "url": pdf_url})
            reply_markup = {"inline_keyboard": [buttons]}

            print(f"Sende PDF an Telegram: {filename}")
            send_telegram_document(pdf_bytes, filename, caption, reply_markup)
            time.sleep(2)

        except Exception as exc:
            print(f"Fehler bei PDF {filename}: {exc}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    try:
        fests = collect_active_fests()
        for fest in fests:
            process_fest(fest, state)
            
        if not state["baseline_done"]:
            state["baseline_done"] = True
            save_state(state)
            print("Baseline gesetzt.")
            
    except Exception as exc:
        print(f"Kritischer Fehler im Ablauf: {exc}")
    print("Botlauf beendet.")

if __name__ == "__main__":
    main()
