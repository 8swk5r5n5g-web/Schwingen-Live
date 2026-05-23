import os
import re
import json
import time
import hashlib
from io import BytesIO
from html import escape
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def get_file_hash(content):
    return hashlib.md5(content).hexdigest()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    if "sent_hashes" not in state or not isinstance(state["sent_hashes"], list):
        state["sent_hashes"] = []

    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()

def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def extract_date(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    if not match:
        return ""
    return match.group(0).replace("..", ".")

def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y")

def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben"]
    return any(word in text for word in blocked)

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    response = requests.post(url, data=data, files=files, timeout=60)
    response.raise_for_status()

def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)
    grouped = {}

    for link in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, link["href"])
        if "anlass=" not in href:
            continue

        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        values = query.get("anlass", [])
        anlass_id = values[0] if values else ""
        text = clean_text(link.get_text(" ", strip=True))

        if not anlass_id or not text:
            continue

        if anlass_id not in grouped:
            grouped[anlass_id] = {"detail_url": href, "parts": []}
        grouped[anlass_id]["parts"].append(text)

    entries = []
    for data in grouped.values():
        parts = data["parts"]
        if len(parts) < 5:
            continue

        date_text = extract_date(parts[0])
        fest_name = clean_text(parts[1])
        category = clean_text(parts[2]).lower()
        location = clean_text(parts[3])
        row_text = clean_text(" ".join(parts))

        if not date_text or category != "aktiv" or is_jung_or_nachwuchs(row_text):
            continue

        entries.append({
            "detail_url": data["detail_url"],
            "date_text": date_text,
            "fest_name": fest_name,
            "location": location,
        })

    if not entries:
        return []

    # Wir filtern stur auf das aktuellste Datum (heute)
    newest_date = max(entries, key=lambda x: parse_date(x["date_text"]))["date_text"]
    return [e for e in entries if e["date_text"] == newest_date]

def should_track_pdf(href, title):
    combined = f"{href} {title}".lower()
    if not href.lower().split("?")[0].endswith(".pdf"):
        return False

    blocked = ["startliste", "einteilung", "notizblatt", "paarung", "zwischenrang"]
    if any(word in combined for word in blocked):
        return False

    return (
        "statistik" in combined or "-st.pdf" in combined or "_st.pdf" in combined or
        "schlussrangliste" in combined or "schlussrang" in combined or 
        "-rl.pdf" in combined or "_rl.pdf" in combined
    )

def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    page_text = clean_text(soup.get_text(" ", strip=True))
    
    match_schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger = match_schwinger.group(1) if match_schwinger else ""
    
    website = ""
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            website = href
            break

    for link in soup.find_all("a", href=True):
        href = link["href"]
        title = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, title):
            continue

        pdf_url = urljoin(BASE_URL, href)

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=60)
            res.raise_for_status()
            pdf_bytes = res.content
            pdf_hash = get_file_hash(pdf_bytes)
            
            # EISERNE REGEL: Wenn dieser Inhalt noch NIE gesendet wurde, wird er JETZT gesendet!
            if pdf_hash in state["sent_hashes"]:
                continue

            # Sofort speichern, damit es nie doppelt kommt
            state["sent_hashes"].append(pdf_hash)
            save_state(state)

            # Dokumententyp bestimmen
            doc_type = "🏆 Schlussrangliste" if "schluss" in pdf_url.lower() or "rl" in pdf_url.lower() else "📊 Statistik"
            
            # Post erstellen
            caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
            if schwinger:
                caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
            caption += f"📝 <b>{doc_type}</b>"

            buttons = []
            if website:
                buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            buttons.append({"text": "🔗 Direktlink ESV", "url": pdf_url})

            filename = pdf_url.split("/")[-1].split("?")[0]
            
            # Direkt rausballern in den Kanal!
            send_telegram_document(pdf_bytes, filename, caption, {"inline_keyboard": [buttons]})
            print(f"Erfolgreich gepostet: {filename}")
            
            time.sleep(2)
        except Exception as exc:
            print(f"Fehler bei {pdf_url}: {exc}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")
        
    state = load_state()
    
    try:
        fests = collect_active_fests()
        for fest in fests:
            process_fest(fest, state)
            
        print("Durchlauf beendet. Alle neuen Listen von heute wurden, falls vorhanden, gepostet.")
    except Exception as exc:
        print(f"Fehler im Ablauf: {exc}")

if __name__ == "__main__":
    main()
