import os
import re
import json
import time
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

HEADERS = {"User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.4"}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {"known_pdfs": {}, "baseline_done": False}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def send_telegram_document(pdf_bytes, filename, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    requests.post(url, data=data, files=files, timeout=60).raise_for_status()

def should_track_pdf(href, title):
    combined = f"{href} {title}".lower()
    if not href.lower().split("?")[0].endswith(".pdf"): return False
    return any(word in combined for word in ["statistik", "-st.pdf", "_st.pdf", "schlussrang", "-rl.pdf", "_rl.pdf"])

def main():
    state = load_state()
    soup = get_soup(RANGLISTEN_URL)
    
    # 1. Alle Feste sammeln
    grouped = {}
    for link in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, link["href"])
        if "anlass=" not in href: continue
        anlass_id = parse_qs(urlparse(href).query).get("anlass", [""])[0]
        text = " ".join(link.get_text().split())
        if anlass_id and text:
            if anlass_id not in grouped: grouped[anlass_id] = {"url": href, "parts": []}
            grouped[anlass_id]["parts"].append(text)

    # 2. Feste prüfen und PDFs verarbeiten
    for data in grouped.values():
        parts = data["parts"]
        if len(parts) < 5 or "aktiv" not in parts[2].lower(): continue
        
        detail_soup = get_soup(data["url"])
        page_text = " ".join(detail_soup.get_text().split())
        schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, re.I)
        schwinger = schwinger.group(1) if schwinger else "Unbekannt"

        for link in detail_soup.find_all("a", href=True):
            if should_track_pdf(link["href"], link.get_text()):
                pdf_url = urljoin(BASE_URL, link["href"])
                if pdf_url in state["known_pdfs"]: continue
                
                # Datei laden
                res = requests.get(pdf_url, headers=HEADERS, timeout=60)
                pdf_bytes = res.content
                
                if state["baseline_done"]:
                    doc_type = "🏆 Schlussrangliste" if "rl" in pdf_url.lower() else "📊 Statistik"
                    caption = f"🏟 <b>{parts[1]}</b>\n📍 {parts[3]}\n🤼 <b>{schwinger} Aktivschwinger</b>\n📝 {doc_type}"
                    send_telegram_document(pdf_bytes, pdf_url.split("/")[-1], caption)
                
                state["known_pdfs"][pdf_url] = {"fest": parts[1]}
                save_state(state)
                time.sleep(2)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Live-Ticker bereit und Baseline gesetzt.")

if __name__ == "__main__":
    main()
