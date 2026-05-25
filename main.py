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

RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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

    if "known_pdfs" not in state:
        state["known_pdfs"] = {}
    if "baseline_done" not in state:
        state["baseline_done"] = False

    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

def get_soup(url):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")

def clean_text(text):
    return " ".join(text.replace("\xa0", " ").replace(" .", ".").replace(". ", ".").split()).strip()

def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked_words = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben"]
    return any(word in text for word in blocked_words)

def extract_date_from_text(text):
    text = clean_text(text)
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    return match.group(0).replace("..", ".") if match else ""

def collect_today_active_fests():
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"Fehler Übersicht: {e}")
        return []

    heute_str = datetime.now().strftime("%d.%m.%Y")
    print(f"Suche Feste für das heutige Datum: {heute_str}")

    grouped = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        
        if "anlass=" not in href:
            continue
        
        parsed_url = urlparse(href)
        queries = parse_qs(parsed_url.query)
        anlass_id = queries.get("anlass", [""])[0]
        
        if not anlass_id:
            continue
            
        text = clean_text(link.get_text(" ", strip=True))
        if not text:
            continue
            
        if anlass_id not in grouped:
            grouped[anlass_id] = {"detail_url": f"https://arls.esv.ch/ranglisten/?anlass={anlass_id}", "parts": []}
        grouped[anlass_id]["parts"].append(text)

    entries = []
    for anlass_id, data in grouped.items():
        parts = data["parts"]
        if len(parts) < 3:
            continue
            
        date_text = extract_date_from_text(parts[0])
        fest_name = clean_text(parts[1])
        row_text = clean_text(" ".join(parts))

        if date_text != heute_str:
            continue

        if "aktiv" not in row_text.lower() or is_jung_or_nachwuchs(row_text):
            continue

        print(f"Heutiges Aktiv-Fest gefunden: {fest_name} ({date_text}) ID: {anlass_id}")
        entries.append({"anlass_id": anlass_id, "detail_url": data["detail_url"], "fest_name": fest_name})
    
    return entries

def send_telegram_document(pdf_bytes, filename, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
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

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        link_text = clean_text(link.get_text(" ", strip=True))
        combined_meta = f"{href} {link_text}".lower()

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        # Harter Negativ-Filter für unerwünschte Dokumente
        is_blockiert = "zwischen" in combined_meta or "startliste" in combined_meta or "einteilung" in combined_meta
        if is_blockiert:
            continue

        # Dateinamen sauber extrahieren
        filename = href.split("/")[-1].split("?")[0]
        storage_key = f"{fest['anlass_id']}_{filename}"

        if storage_key in state["known_pdfs"]:
            continue

        # 🎯 FIX: Wir versuchen das PDF über beide möglichen Server-URLs zu laden
        pdf_bytes = None
        for domain in ["https://esv.ch", "https://arls.esv.ch"]:
            try:
                pdf_url = requests.compat.urljoin(domain, href)
                res = requests.get(pdf_url, headers=HEADERS, timeout=15)
                if res.status_code == 200 and len(res.content) > 1000: # Muss eine echte Datei sein
                    pdf_bytes = res.content
                    break
            except Exception:
                continue

        # Wenn die Datei von keinem Server geladen werden konnte, überspringen
        if not pdf_bytes:
            print(f"Konnte PDF nicht laden: {filename}")
            continue

        doc_title = link_text if link_text else "Dokument"
        emoji = "🏆" if "schluss" in doc_title.lower() else "📊"

        if state["baseline_done"]:
            caption = (
                f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                f"{emoji} <b>{escape(doc_title)}</b>\n"
            )
            if schwinger_txt:
                caption += f"🤼 {escape(schwinger_txt)} Aktivschwinger\n"

            print(f"... SENDE AN TELEGRAM: {doc_title}")
            send_telegram_document(pdf_bytes, filename, caption)
        else:
            print(f"Baseline speichert lautlos: {doc_title}")

        state["known_pdfs"][storage_key] = hashlib.md5(pdf_bytes).hexdigest()
        save_state(state)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    fests = collect_today_active_fests()

    print(f"Anzahl heutiger Aktiv-Feste: {len(fests)}")

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fixiert. Ab dem nächsten Durchlauf wird scharf gesendet.")

    print("Bot-Scan erfolgreich beendet.")

if __name__ == "__main__":
    main()
