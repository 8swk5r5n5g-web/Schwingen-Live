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
    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.5",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def load_state():
    # Wenn manuell gestartet wird (Workflow Dispatch), ignorieren wir den alten Zustand,
    # damit heute alles sofort und garantiert gesendet wird!
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Manueller Start erkannt: Erzwinge frischen Scan für die heutigen Listen!")
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

def get_page(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    print(f"GET: {url} -> {response.status_code}")
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
        print("Keine Aktiv-Feste gefunden.")
        return []

    newest_date = max(entries, key=lambda x: parse_date(x["date_text"]))["date_text"]
    newest_entries = [entry for entry in entries if entry["date_text"] == newest_date]

    print(f"Alle Aktiv-Feste gefunden: {len(entries)}")
    print(f"Neuestes Datum auf der Seite: {newest_date}")
    
    return newest_entries

def extract_number_after(label, text):
    match = re.search(rf"{re.escape(label)}\s+(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""

def extract_fest_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            return href
    return ""

def extract_detail_infos(soup):
    page_text = clean_text(soup.get_text(" ", strip=True))
    return {
        "schwinger": extract_number_after("Anzahl Schwinger", page_text),
        "website": extract_fest_website(soup),
    }

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

def get_pdf_title(href, title):
    title = clean_text(title)
    if title:
        return title
    href_lower = href.lower()
    if "-st.pdf" in href_lower or "_st.pdf" in href_lower:
        return "Statistik"
    if "-rl.pdf" in href_lower or "_rl.pdf" in href_lower:
        return "Schlussrangliste"
    return "PDF"

def get_pdf_bytes_and_hash(pdf_url):
    response = requests.get(pdf_url, headers=HEADERS, timeout=90)
    print(f"PDF Download: {pdf_url} -> {response.status_code}")
    response.raise_for_status()
    return response.content, hashlib.sha256(response.content).hexdigest()

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    response = requests.post(url, data=data, files=files, timeout=120)
    response.raise_for_status()

def process_pdf(pdf_url, pdf_title, fest, infos, state):
    pdf_bytes, pdf_hash = get_pdf_bytes_and_hash(pdf_url)
    old_entry = state["known_pdfs"].get(pdf_url)

    # Bestimme Emoji für Dokument
    doc_emoji = "🏆 Schlussrangliste" if "schluss" in pdf_title.lower() or "rl" in pdf_url.lower() else "📊 Statistik"
    
    # Nachricht gemäss Wunsch ultrakompakt aufbauen
    caption = f"🏟 <b>{escape(fest.get('fest_name', 'Schwingfest'))}</b>\n"
    schwinger = infos.get("schwinger", "")
    if schwinger:
        caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
    caption += f"📝 <b>{doc_emoji}</b>"

    # Buttons vorbereiten
    buttons = []
    if infos.get("website"):
        buttons.append({"text": "🌐 Fest-Webseite", "url": infos.get("website")})
    buttons.append({"text": "🔗 Direktlink ESV", "url": pdf_url})
    reply_markup = {"inline_keyboard": [buttons]}

    filename = pdf_url.split("/")[-1].split("?")[0]

    # FALL 1: Komplett neue URL
    if old_entry is None:
        state["known_pdfs"][pdf_url] = {"hash": pdf_hash, "title": pdf_title}
        save_state(state)

        if not state["baseline_done"]:
            print(f"Baseline speichert bestehende PDF ohne Senden: {pdf_url}")
            return

        print(f"Neue PDF erkannt und wird gesendet: {pdf_url}")
        send_telegram_document(pdf_bytes, filename, caption, reply_markup)
        return

    # FALL 2: Bekannte URL, aber der INHALT (Hash) hat sich verändert -> NACH JEDEM GANG!
    old_hash = old_entry.get("hash", "") if isinstance(old_entry, dict) else old_entry

    if old_hash != pdf_hash:
        if isinstance(old_entry, dict):
            state["known_pdfs"][pdf_url]["hash"] = pdf_hash
        else:
            state["known_pdfs"][pdf_url] = {"hash": pdf_hash, "title": pdf_title}

        save_state(state)
        
        if not state["baseline_done"]:
            return

        print(f"Update für Gang-Statistik erkannt! Sende Aktualisierung: {pdf_url}")
        send_telegram_document(pdf_bytes, filename, caption, reply_markup)
        return

    print(f"Unverändert: {pdf_url}")

def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    infos = extract_detail_infos(soup)

    print(f"Aktiv-Fest scannen: {fest['fest_name']}")
    found = 0

    for link in soup.find_all("a", href=True):
        href = link["href"]
        title = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, title):
            continue

        pdf_url = normalise_url(href)
        pdf_title = get_pdf_title(href, title)

        try:
            process_pdf(pdf_url, pdf_title, fest, infos, state)
            found += 1
        except Exception as exc:
            print(f"Fehler bei PDF {pdf_url}: {exc}")

        time.sleep(1)
    print(f"Relevante PDFs für dieses Fest geprüft: {found}")

def check_ranglisten(state):
    print(f"Prüfung gestartet: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    fests = collect_active_fests()

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fertig. Ab jetzt werden nur noch echte Inhalts-Updates gesendet.")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    try:
        check_ranglisten(state)
    except Exception as exc:
        print(f"Fehler im Hauptlauf: {exc}")
    print("Botlauf beendet.")

if __name__ == "__main__":
    main()
