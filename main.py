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
    "Cache-Control": "no-cache"
}

def load_state():
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("MANUELLER START: Speicher gelöscht. Sende alle heutigen PDFs JETZT SOFORT!")
        return {"known_pdfs": {}, "baseline_done": True}

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"known_pdfs": {}, "baseline_done": False}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_soup(url):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")

def should_track_pdf(href, title):
    combined = f"{href} {title}".lower()
    if not href.lower().split("?")[0].endswith(".pdf"):
        return False

    blocked = ["startliste", "einteilung", "notizblatt", "paarung", "zwischenrang"]
    if any(b in combined for b in blocked):
        return False

    return any(req in combined for req in ["statistik", "-st", "_st", "schluss", "-rl", "_rl"])

def process_single_fest(fest_url, state):
    try:
        soup = get_soup(fest_url)
    except Exception as e:
        print(f"Fehler beim Öffnen von {fest_url}: {e}")
        return

    page_text = soup.get_text(" ")
    page_text_lower = page_text.lower()

    # 1. SICHERHEIT: MUSS AKTIV SEIN & DARF KEIN NACHWUCHS SEIN
    if "aktiv" not in page_text_lower:
        return
    if any(bad in page_text_lower for bad in ["jung", "nachwuchs", "bueb", "knaben", "schueler", "schüler"]):
        return

    # 2. DETAILS HOCHPRÄZISE EXTRAHIEREN
    # Festname aus dem h1- oder h2-Titel holen
    title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
    fest_name = title_tag.get_text(strip=True) if title_tag else "Schwingfest"
    fest_name = re.sub(r"(Rangliste|Statistik|Meldungen|ESV|ARLS).*", "", fest_name, flags=re.IGNORECASE).strip()

    # Anzahl Schwinger
    schwinger_match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger = schwinger_match.group(1) if schwinger_match else ""

    # Fest-Webseite
    website = ""
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("http") and "esv.ch" not in a["href"]:
            website = a["href"]
            break

    # 3. ALLE PDFS DURCHSUCHEN
    pdf_found = False
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not should_track_pdf(href, a.get_text()):
            continue

        pdf_url = urljoin(BASE_URL, href)
        filename = pdf_url.split("/")[-1].split("?")[0]

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=60)
            res.raise_for_status()
            pdf_hash = hashlib.md5(res.content).hexdigest()

            # Double-Post Schutz
            if state["known_pdfs"].get(pdf_url) == pdf_hash:
                continue

            state["known_pdfs"][pdf_url] = pdf_hash
            save_state(state)

            if not state["baseline_done"]:
                print(f"Baseline sichert im Hintergrund: {filename}")
                continue

            is_schluss = "schluss" in filename.lower() or "rl" in filename.lower()
            doc_emoji = "🏆 Schlussrangliste" if is_schluss else "📊 Statistik"

            caption = f"🏟 <b>{escape(fest_name)}</b>\n"
            if schwinger:
                caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
            caption += f"📝 <b>{doc_emoji}</b>"

            buttons = []
            if website:
                buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            buttons.append({"text": "🔗 Direktlink ESV", "url": pdf_url})
            reply_markup = {"inline_keyboard": [buttons]}

            print(f"POSTET JETZT: {filename} für Fest: {fest_name}")
            telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
            payload = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML", "reply_markup": json.dumps(reply_markup)}
            files = {"document": (filename, BytesIO(res.content), "application/pdf")}
            
            requests.post(telegram_url, data=payload, files=files, timeout=60).raise_for_status()
            pdf_found = True
            time.sleep(2)

        except Exception as exc:
            print(f"Fehler bei PDF {filename}: {exc}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt!")

    state = load_state()
    
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"Fehler beim Laden der ESV-Übersicht: {e}")
        return

    # Alle Anlass-IDs sammeln (Vollkommen ohne Filter!)
    anlass_urls = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "anlass=" in href:
            anlass_urls.add(urljoin(BASE_URL, href))

    print(f"{len(anlass_urls)} Feste auf der ESV-Seite gefunden. Starte Tiefenprüfung...")

    # Jedes einzelne Fest öffnen und prüfen
    for url in list(anlass_urls)[:15]:  # Die obersten 15 Feste reichen völlig aus fürs Wochenende
        process_single_fest(url, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline erfolgreich fixiert.")

    print("Durchlauf beendet.")

if __name__ == "__main__":
    main()
