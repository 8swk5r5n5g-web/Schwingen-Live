import os
import re
import json
import time
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {"known_pdfs": {}, "last_baseline_date": ""}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

def get_page(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException:
            time.sleep(3)
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
        row_text = clean_text(" ".join(parts))

        if category != "aktiv" or is_jung_or_nachwuchs(row_text):
            continue

        entries.append({
            "anlass_id": anlass_id,
            "detail_url": data["detail_url"],
            "fest_name": fest_name,
        })

    return entries[:MAX_DETAIL_PAGES]

def get_gang_nummer(href, link_text):
    combined = f"{href} {link_text}".lower()
    gang_match = re.search(r"\b([1-5])\b\.?\s*(gang|g\b)", combined)
    if gang_match:
        return int(gang_match.group(1))
    zahlen = re.findall(r"\b([1-5])\b", combined)
    if zahlen:
        return int(zahlen[-1])
    if "statistik" in combined:
        return 99
    return 0

def get_pdf_title(href, link_text, gang_num):
    text = clean_text(link_text)
    if "schluss" in text.lower() or "schluss" in href.lower() or "-rl" in href.lower():
        return "Schlussrangliste"
    if gang_num == 99:
        return "Statistik"
    if gang_num > 0:
        return f"Statistik (nach dem {gang_num}. Gang)"
    return "Statistik"

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    requests.post(url, data=data, files=files, timeout=90).raise_for_status()

def process_fest(fest, state, is_baseline_run):
    try:
        soup = get_soup(fest["detail_url"])
    except Exception:
        return

    website = ""
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            website = href
            break

    page_text = clean_text(soup.get_text(" ", strip=True))
    schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger_txt = schwinger.group(1) if schwinger else ""

    buttons = [{"text": "🌐 Fest-Webseite", "url": website}] if website else []
    reply_markup = {"inline_keyboard": [buttons]} if buttons else None

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        if any(w in f"{href} {link_text}".lower() for w in ["zwischen", "startliste", "einteilung", "notizblatt"]):
            continue

        is_stat = "statistik" in href.lower() or "statistik" in link_text.lower() or "-st.pdf" in href.lower()
        is_rl = "schluss" in href.lower() or "schluss" in link_text.lower() or "-rl.pdf" in href.lower()
        
        if not (is_stat or is_rl):
            continue

        pdf_url = normalise_url(href)
        filename = pdf_url.split("/")[-1].split("?")[0]
        storage_key = f"{fest['anlass_id']}_{filename}"

        gang = get_gang_nummer(href, link_text)
        verarbeite_dokument(pdf_url, storage_key, filename, href, link_text, gang, fest, state, reply_markup, schwinger_txt, is_baseline_run)

def verarbeite_dokument(pdf_url, storage_key, filename, href, link_text, gang, fest, state, reply_markup, schwinger_txt, is_baseline_run):
    if storage_key in state["known_pdfs"]:
        return

    try:
        res = requests.get(pdf_url, headers=HEADERS, timeout=60)
        res.raise_for_status()
        pdf_bytes = res.content
        pdf_hash = hashlib.md5(pdf_bytes).hexdigest()

        state["known_pdfs"][storage_key] = pdf_hash

        if is_baseline_run:
            print(f"Baseline-Erfassung: {filename} lautlos registriert.")
            return

        doc_title = get_pdf_title(href, link_text, gang)
        emoji = "🏆" if "Schluss" in doc_title else "📊"

        caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
        if schwinger_txt:
            caption += f"🤼 <b>{escape(schwinger_txt)} Aktivschwinger</b>\n"
        caption += f"📝 <b>{emoji} {doc_title}</b>"

        print(f"SENDE LIVE-UPDATE: {filename}")
        send_telegram_document(pdf_bytes, filename, caption, reply_markup)

    except Exception as e:
        print(f"Fehler bei Dokument {filename}: {e}")

def run_ticker_cycle(state):
    fests = collect_active_fests()
    heute_str = datetime.now().strftime("%d.%m.%Y")
    is_baseline_run = (state.get("last_baseline_date", "") != heute_str)

    if is_baseline_run:
        print(f"Erster Lauf am {heute_str}: Starte lautlose Baseline-Initialisierung...")
    
    for fest in fests:
        process_fest(fest, state, is_baseline_run)

    if is_baseline_run:
        state["last_baseline_date"] = heute_str
        print(f"Baseline für den {heute_str} erfolgreich fixiert.")
    
    save_state(state)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    
    # 🔁 DIE DAUERSCHLEIFE: Läuft für 60 Runden à 5 Minuten (= 5 Stunden Überwachung am Stück)
    # Vollkommen unabhängig von der GitHub-Uhr!
    for i in range(1, 61):
        print(f"----------------------------------------")
        print(f"START TICKER-SCAN NR. {i} von 60...")
        try:
            run_ticker_cycle(state)
        except Exception as e:
            print(f"Kritischer Fehler im Schleifendurchlauf: {e}")
        
        print("Scan beendet. Warte exakt 5 Minuten (300 Sekunden)...")
        time.sleep(300)

if __name__ == "__main__":
    main()
