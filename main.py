import os
import re
import json
import time
import hashlib
from io import BytesIO
from html import escape
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Cache-Control": "no-cache"
}

def load_state():
    # Bei manuellem Start ("Run workflow") wird der Speicher ignoriert -> Er sendet SOFORT alles Aktuelle.
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Manueller Start: Speicher wird zurückgesetzt. Sende jetzt die Live-Dokumente!")
        return {"known_pdfs": {}, "baseline_done": True}

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"known_pdfs": {}, "baseline_done": False}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def find_active_fests():
    soup = get_soup(RANGLISTEN_URL)
    fests = {}

    print("Suche nach Festen auf der ESV-Hauptseite...")
    
    # Gehe durch alle Links, die zu einem Anlass führen
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "anlass=" not in href:
            continue

        # Den Container (z.B. Tabellenzeile) finden, in dem der Link steht
        container = link.find_parent("tr") or link.find_parent("li") or link.parent
        text = container.get_text(" ", strip=True)
        text_lower = text.lower()

        # 1. FILTER: Muss Aktiv sein, darf kein Nachwuchs sein
        if "aktiv" not in text_lower:
            continue
        if any(bad in text_lower for bad in ["jung", "nachwuchs", "bueb", "knaben", "schueler", "schüler"]):
            continue

        # 2. FILTER: Muss ein Datum enthalten
        match = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
        if not match:
            continue
        date_str = match.group(0)

        # 3. FESTNAME herausfiltern (alles was nicht Datum oder "Aktiv" ist)
        clean_name = re.sub(r"\d{2}\.\d{2}\.\d{4}", "", text, flags=re.IGNORECASE)
        clean_name = re.sub(r"\baktiv\b", "", clean_name, flags=re.IGNORECASE).strip()
        
        # Falls der Name zu kurz ist, nehmen wir den Linktext
        fest_name = clean_name if len(clean_name) > 5 else link.get_text(strip=True)

        url = urljoin(BASE_URL, href)
        fests[url] = {
            "url": url,
            "name": fest_name[:60], # Begrenzung auf sauberen Titel
            "date_str": date_str,
            "date_obj": datetime.strptime(date_str, "%d.%m.%Y")
        }

    if not fests:
        print("Keine Aktiv-Feste gefunden.")
        return []

    # UNFEHLBARER DATUMS-FILTER: Finde das absolut aktuellste Datum auf der Seite!
    all_fests = list(fests.values())
    newest_date = max(f["date_obj"] for f in all_fests)
    
    # Nimm nur Feste, die am selben Wochenende (max 2 Tage Abweichung) stattfinden
    live_fests = [f for f in all_fests if abs((f["date_obj"] - newest_date).days) <= 2]
    
    print(f"Treffer! {len(live_fests)} Feste für das aktuelle Wochenende gefunden:")
    for f in live_fests:
        print(f"- {f['name']}")
        
    return live_fests

def process_live_fest(fest, state):
    print(f"Öffne Fest: {fest['name']}")
    try:
        soup = get_soup(fest["url"])
    except Exception as e:
        print(f"Fehler beim Öffnen von {fest['url']}: {e}")
        return

    # Schwingeranzahl auslesen
    page_text = soup.get_text(" ")
    schwinger_match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger = schwinger_match.group(1) if schwinger_match else ""

    # Fest-Webseite suchen
    website = ""
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("http") and "esv.ch" not in a["href"]:
            website = a["href"]
            break

    # Alle PDF-Links prüfen
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        title = a.get_text(" ", strip=True).lower()
        combined = href.lower() + " " + title

        # Unwichtiges blockieren
        if any(b in combined for b in ["startliste", "einteilung", "notizblatt", "paarung", "zwischenrang"]):
            continue

        # Nur Statistik & Schlussrangliste zulassen
        if not any(req in combined for req in ["statistik", "-st", "_st", "schluss", "-rl", "_rl"]):
            continue

        pdf_url = urljoin(BASE_URL, href)
        filename = pdf_url.split("/")[-1].split("?")[0]

        try:
            # Dokument herunterladen und auf neuen Inhalt prüfen (Hash)
            res = requests.get(pdf_url, headers=HEADERS, timeout=60)
            res.raise_for_status()
            pdf_hash = hashlib.md5(res.content).hexdigest()

            # Wurde EXAKT diese Datei schon gesendet?
            if state["known_pdfs"].get(pdf_url) == pdf_hash:
                continue

            # In Speicher eintragen
            state["known_pdfs"][pdf_url] = pdf_hash
            save_state(state)

            if not state["baseline_done"]:
                print(f"Stumme Sicherung (Baseline): {filename}")
                continue

            # Telegram-Nachricht vorbereiten
            is_schluss = "schluss" in combined or "rl" in href.lower()
            doc_emoji = "🏆 Schlussrangliste" if is_schluss else "📊 Statistik"

            caption = f"🏟 <b>{escape(fest['name'])}</b>\n"
            if schwinger:
                caption += f"🤼 <b>{escape(schwinger)} Aktivschwinger</b>\n"
            caption += f"📝 <b>{doc_emoji}</b>"

            buttons = []
            if website:
                buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            buttons.append({"text": "🔗 Direktlink ESV", "url": pdf_url})
            reply_markup = {"inline_keyboard": [buttons]}

            # Direktes Senden des PDFs
            print(f"Sende neue Datei an Telegram: {filename}")
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
            data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML", "reply_markup": json.dumps(reply_markup)}
            files = {"document": (filename, BytesIO(res.content), "application/pdf")}
            
            post_res = requests.post(url, data=data, files=files, timeout=60)
            post_res.raise_for_status()
            time.sleep(2)

        except Exception as exc:
            print(f"Fehler bei PDF {filename}: {exc}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlen in den Einstellungen!")

    state = load_state()
    fests = find_active_fests()
    
    for fest in fests:
        process_live_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fixiert. Das System ist jetzt scharf.")

    print("Durchlauf erfolgreich beendet.")

if __name__ == "__main__":
    main()
