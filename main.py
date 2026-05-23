import os
import re
import json
import time
import hashlib
from io import BytesIO
from html import escape
from datetime import datetime
from urllib.parse import urljoin

# WICHTIG: Für den exakten Zeitzonen-Abgleich in der Schweiz
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def load_state():
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Manueller Start: Ignoriere alten Speicher, um heutige Listen sofort zu senden!")
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

def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def extract_date(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    if not match:
        return ""
    return match.group(0).replace("..", ".")

def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben"]
    return any(word in text for word in blocked)

def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)
    entries = []
    
    # Schweizer Uhrzeit erzwingen (Verhindert Server-Zeitverschiebungen bei GitHub)
    tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    today_str = datetime.now(tz_ch).strftime("%d.%m.%Y")
    print(f"Erzwungene Schweizer Server-Zeit für den Abgleich: {today_str}")

    rows = soup.find_all("tr")
    print(f"Scanne {len(rows)} Tabellenzeilen auf der ESV-Seite...")

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
            
        row_text = clean_text(row.get_text(" "))
        date_text = extract_date(row_text)
        
        # 1. Schritt: Datum prüfen (MUSS exakt dem heutigen Schweizer Datum entsprechen)
        if not date_text or date_text != today_str:
            continue
            
        # 2. Schritt: Nachwuchsfeste blockieren
        if is_jung_or_nachwuchs(row_text):
            continue
            
        # 3. Schritt: Kategorie "Aktiv" voraussetzen
        if "aktiv" not in row_text.lower():
            continue

        link = row.find("a", href=True)
        if not link:
            continue
            
        detail_url = urljoin(BASE_URL, link["href"])
        fest_name = clean_text(cells[1].get_text())
        location = clean_text(cells[3].get_text()) if len(cells) > 3 else ""

        entries.append({
            "detail_url": detail_url,
            "date_text": date_text,
            "fest_name": fest_name,
            "location": location
        })

    print(f"Vergleich abgeschlossen. {len(entries)} Aktiv-Feste laufen HEUTE ({today_str}).")
    for f in entries:
        print(f"-> Fest wird geöffnet: {f['fest_name']}")
    return entries

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

    print(f"Scanne Dokumente für das laufende Fest: {fest['fest_name']}")

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

            if old_entry is not None and old_hash == pdf_hash:
                continue

            state["known_pdfs"][pdf_url] = {"hash": pdf_hash}
            save_state(state)

            if not state["baseline_done"]:
                print(f"Baseline sichert bestehende Liste: {filename}")
                continue

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

            send_telegram_document(pdf_bytes, filename, caption, reply_markup)
            print(f"Erfolgreich gepostet: {filename}")
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
            print("Baseline erfolgreich gesetzt.")
            
    except Exception as exc:
        print(f"Fehler im Ablauf: {exc}")
    print("Botlauf beendet.")

if __name__ == "__main__":
    main()
