import os
import re
import json
import time
import hashlib
from io import BytesIO
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Die korrekte Hauptdomain des ESV
BASE_URL = "https://esv.ch"
RANGLISTEN_URL = "https://esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cache-Control": "no-cache"
}

def load_state():
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("MANUELLER START: Sende alle aktuellen PDFs sofort raus!")
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

def should_track_pdf(href):
    href_lower = href.lower().split("?")[0]
    if not href_lower.endswith(".pdf"):
        return False

    # Blockiere unwichtige Listen
    blocked = ["startliste", "einteilung", "notizblatt", "paarung", "zwischenrang"]
    if any(b in href_lower for b in blocked):
        return False

    # Nur Statistiken (-ST) oder Schlussranglisten (-RL)
    return any(req in href_lower for req in ["statistik", "-st", "_st", "schluss", "-rl", "_rl"])

def process_fest_page(fest_url, state):
    print(f"Prüfe Fest-Unterseite: {fest_url}")
    try:
        soup = get_soup(fest_url)
    except Exception as e:
        print(f"Fehler beim Laden von {fest_url}: {e}")
        return

    page_text = soup.get_text(" ")
    page_text_lower = page_text.lower()

    # Filter 1: Nur Aktive durchlassen
    if "aktiv" not in page_text_lower:
        return
    # Filter 2: Nachwuchs komplett blockieren
    if any(bad in page_text_lower for bad in ["jung", "nachwuchs", "bueb", "knaben", "schueler", "schüler"]):
        return

    # Festname aus dem Seitentitel ziehen
    title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
    fest_name = title_tag.get_text(strip=True) if title_tag else "Schwingfest"
    fest_name = re.sub(r"(Rangliste|Statistik|Meldungen|ESV).* ", "", fest_name, flags=re.IGNORECASE).strip()

    # Anzahl Schwinger ermitteln
    schwinger_match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger = schwinger_match.group(1) if schwinger_match else ""

    # Fest-Webseite (externe Links) suchen
    website = ""
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            website = href
            break

    # Alle PDF-Links auf dieser Festunterseite verarbeiten
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not should_track_pdf(href):
            continue

        pdf_url = urljoin(BASE_URL, href)
        filename = pdf_url.split("/")[-1].split("?")[0]

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=60)
            res.raise_for_status()
            pdf_hash = hashlib.md5(res.content).hexdigest()

            # Dubletten-Schutz (wurde diese Version schon gesendet?)
            if state["known_pdfs"].get(pdf_url) == pdf_hash:
                continue

            state["known_pdfs"][pdf_url] = pdf_hash
            save_state(state)

            if not state["baseline_done"]:
                print(f"Baseline-Modus speichert im Hintergrund: {filename}")
                continue

            # Zuordnung Dokumenten-Typ
            is_schluss = "schluss" in filename.lower() or "-rl" in filename.lower() or "_rl" in filename.lower()
            doc_emoji = "🏆 Schlussrangliste" if is_schluss else "📊 Statistik"

            # Telegram Nachricht zusammenbauen
            caption = f"🏟 <b>{escape(fest_name)}</b>\n"
            if schwinger:
                caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
            caption += f"📝 <b>{doc_emoji}</b>"

            buttons = []
            if website:
                buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            buttons.append({"text": "🔗 Direktlink ESV", "url": pdf_url})
            reply_markup = {"inline_keyboard": [buttons]}

            print(f"Sende Dokument: {filename}")
            telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
            payload = {
                "chat_id": CHAT_ID, 
                "caption": caption[:1024], 
                "parse_mode": "HTML", 
                "reply_markup": json.dumps(reply_markup)
            }
            files = {"document": (filename, BytesIO(res.content), "application/pdf")}
            
            requests.post(telegram_url, data=payload, files=files, timeout=60).raise_for_status()
            time.sleep(2)

        except Exception as exc:
            print(f"Fehler bei Datei {filename}: {exc}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt in den Umgebungsvariablen!")

    state = load_state()
    
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"Fehler beim Laden der ESV-Übersicht: {e}")
        return

    # Finde alle aktuellen Fest-Links (anlass=XXXX) direkt auf der Hauptseite
    anlass_urls = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "anlass=" in href:
            anlass_urls.add(urljoin(BASE_URL, href))

    print(f"{len(anlass_urls)} Feste auf der Übersicht gefunden. Starte Abruf...")

    # Verarbeite die neuesten Feste (die ersten 10 bis 15 Einträge reichen völlig aus)
    for url in list(anlass_urls)[:15]:
        process_fest_page(url, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline erfolgreich gesetzt.")

    print("Bot-Lauf erfolgreich beendet.")

if __name__ == "__main__":
    main()
