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
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("MANUELLER START: Sende alle aktuellen PDFs sofort raus!")
        return {"known_pdfs": {}, "baseline_done": True}

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
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
            response = requests.get(url, headers=HEADERS, timeout=60)
            print(f"GET: {url} -> {response.status_code}")
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
            print(f"Fehler bei {url} (Versuch {attempt}): {exc}. Warte {wait_time}s...")
            time.sleep(wait_time)
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
    if not match:
        return ""
    return match.group(0).replace("..", ".")

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
    soup = get_soup(RANGLISTEN_URL)
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
    for data in grouped.values():
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
            "detail_url": data["detail_url"],
            "overview_text": row_text,
            "date_text": date_text,
            "fest_name": fest_name,
            "location": location,
        })

    if not entries:
        print("Keine Aktiv-Feste gefunden.")
        return []

    newest_date = max(entries, key=lambda entry: parse_date(entry["date_text"]))["date_text"]
    filtered = [entry for entry in entries if entry["date_text"] == newest_date]

    print(f"Alle Aktiv-Feste gefunden: {len(entries)}")
    print(f"Neuestes Datum auf der Seite: {newest_date}")
    return filtered[:MAX_DETAIL_PAGES]

def extract_number_after(label, text):
    match = re.search(rf"{re.escape(label)}\s+(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""

def extract_fest_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        text = clean_text(link.get_text(" ", strip=True))
        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            return href
        if text.startswith("http") and "arls.esv.ch" not in text and "esv.ch" not in text:
            return text
    return ""

def extract_detail_infos(soup):
    page_text = clean_text(soup.get_text(" ", strip=True))
    return {
        "schwinger": extract_number_after("Anzahl Schwinger", page_text),
        "website": extract_fest_website(soup),
    }

def is_real_pdf_url(href):
    return href.lower().split("?")[0].endswith(".pdf")

def is_blocked_pdf(href, link_text=""):
    combined = f"{href} {link_text}".lower()
    blocked_words = ["zwischenrangliste", "zwischenrang", "startliste", "einteilung", "notizblatt", "paarung"]
    return any(word in combined for word in blocked_words)

def is_statistik(href, link_text=""):
    combined = f"{href} {link_text}".lower()
    if not is_real_pdf_url(href) or is_blocked_pdf(href, link_text):
        return False
    return "statistik" in combined or "-st.pdf" in combined or "_st.pdf" in combined

def is_schlussrangliste(href, link_text=""):
    combined = f"{href} {link_text}".lower()
    if not is_real_pdf_url(href) or is_blocked_pdf(href, link_text):
        return False
    return "schlussrangliste" in combined or "schlussrang" in combined or combined.endswith("-rl.pdf") or "_rl.pdf" in combined

def should_track_pdf(href, link_text=""):
    return is_statistik(href, link_text) or is_schlussrangliste(href, link_text)

def get_pdf_title(href, link_text=""):
    text = clean_text(link_text)
    filename = href.split("/")[-1].split("?")[0].lower()
    
    gang_match = re.search(r"(\d+)\.?\s*(gang|g\b)", text.lower() + " " + filename)
    gang_suffix = f" (nach dem {gang_match.group(1)}. Gang)" if gang_match else ""

    if is_schlussrangliste(href, text):
        return "Schlussrangliste"
        
    if is_statistik(href, text):
        if len(text) > 9 and "statistik" in text.lower():
            return text
        return f"Statistik{gang_suffix}"

    return "PDF"

def get_pdf_bytes(pdf_url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(pdf_url, headers=HEADERS, timeout=90)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
            print(f"Fehler bei PDF-Download {pdf_url} (Versuch {attempt}): {exc}")
            time.sleep(wait_time)
    raise RuntimeError(f"PDF konnte nicht geladen werden: {pdf_url}")

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    res = requests.post(url, data=data, files=files, timeout=90)
    res.raise_for_status()

def process_pdf(pdf_url, pdf_bytes, pdf_hash, href, link_text, fest, detail_infos, state):
    old_entry = state["known_pdfs"].get(pdf_url)
    filename = pdf_url.split("/")[-1].split("?")[0]

    doc_title = get_pdf_title(href, link_text)
    doc_emoji = "🏆 Schlussrangliste" if "Schlussrangliste" in doc_title else f"📊 {doc_title}"
    
    caption = f"🏟 <b>{escape(fest.get('fest_name', 'Schwingfest'))}</b>\n"
    schwinger = detail_infos.get("schwinger", "")
    if schwinger:
        caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
    caption += f"📝 <b>{doc_emoji}</b>"

    # HIER: Direktlink entfernt, nur noch Fest-Webseite falls vorhanden
    buttons = []
    if detail_infos.get("website"):
        buttons.append({"text": "🌐 Fest-Webseite", "url": detail_infos.get("website")})
    
    reply_markup = {"inline_keyboard": [buttons]} if buttons else None

    if old_entry is None:
        state["known_pdfs"][pdf_url] = {
            "hash": pdf_hash,
            "fest": fest.get("fest_name", ""),
            "date": fest.get("date_text", ""),
        }
        save_state(state)

        if not state["baseline_done"]:
            print(f"Baseline sichert: {filename}")
            return

        print(f"Sende neue PDF: {filename}")
        send_telegram_document(pdf_bytes, filename, caption, reply_markup)
        return

    old_hash = old_entry.get("hash", "") if isinstance(old_entry, dict) else old_entry

    if old_hash != pdf_hash:
        if isinstance(old_entry, dict):
            state["known_pdfs"][pdf_url]["hash"] = pdf_hash
        else:
            state["known_pdfs"][pdf_url] = {"hash": pdf_hash}
        
        save_state(state)

        if not state["baseline_done"]:
            return

        print(f"Sende Aktualisierung nach Gang für: {filename}")
        send_telegram_document(pdf_bytes, filename, caption, reply_markup)
        return

    print(f"Unverändert: {filename}")

def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    detail_infos = extract_detail_infos(soup)

    print(f"Scanne: {fest['fest_name']}")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, link_text):
            continue

        pdf_url = normalise_url(href)

        try:
            pdf_bytes = get_pdf_bytes(pdf_url)
            pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
            
            process_pdf(pdf_url, pdf_bytes, pdf_hash, href, link_text, fest, detail_infos, state)
        except Exception as exc:
            print(f"Fehler bei PDF {pdf_url}: {exc}")
            continue

        time.sleep(1)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    fests = collect_active_fests()

    for fest in fests:
        try:
            process_fest(fest, state)
        except Exception as exc:
            print(f"Fehler bei {fest['fest_name']}: {exc}")

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fixiert. System ist live.")

    print("Botlauf erfolgreich beendet.")

if __name__ == "__main__":
    main()
