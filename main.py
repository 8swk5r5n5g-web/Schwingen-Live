import os
import re
import json
import hashlib
from io import BytesIO
from html import escape
from urllib.parse import urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 🎯 DIE NEUE, SAUBERE HAUPTSEITE
TARGET_URL = "https://esv.ch/ranglisten/"
BASE_URL = "https://arls.esv.ch"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
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
    if "abgeschlossene_feste" not in state:
        state["abgeschlossene_feste"] = {}
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

def collect_live_fests(state):
    try:
        soup = get_soup(TARGET_URL)
    except Exception as e:
        print(f"Fehler beim Laden der ESV-Hauptseite: {e}")
        return []

    entries = []
    
    # Die neue Seite listet Feste meistens in Tabellenzeilen (tr) oder Listen-Blöcken auf
    for row in soup.find_all(["tr", "div", "li"]):
        row_text = clean_text(row.get_text(" ", strip=True))
        
        # 🎯 BEDINGUNG B: FILTER NACH "AKTIV" (Nachwuchs fliegt sofort raus)
        if "aktiv" not in row_text.lower() or is_jung_or_nachwuchs(row_text):
            continue
            
        link_tag = row.find("a", href=True)
        if not link_tag:
            continue
            
        href = link_tag["href"].strip()
        fest_name = clean_text(link_tag.get_text(" ", strip=True))
        
        # Anlass ID extrahieren
        anlass_id = ""
        if "anlass=" in href:
            anlass_id = parse_qs(urlparse(href).query).get("anlass", [""])[0]
        else:
            # Falls der Link keine ID hat, nutzen wir einen Hash des Namens als ID
            anlass_id = hashlib.md5(fest_name.encode("utf-8")).hexdigest()[:8]

        # 🛑 ABSOLUTER SCHUTZ: Bereits beendete Feste ignorieren
        if anlass_id in state["abgeschlossene_feste"]:
            continue

        # Externe Links erkennen (Regionalverbände wie isv.ch, bksv.ch etc.)
        is_external = not ("esv.ch" in href or href.startswith("/"))

        entries.append({
            "anlass_id": anlass_id,
            "detail_url": requests.compat.urljoin(TARGET_URL, href) if href.startswith("/") else href,
            "fest_name": fest_name,
            "is_external": is_external
        })
        
    # Duplikate filtern
    seen = set()
    unique_entries = []
    for e in entries:
        if e["anlass_id"] not in seen:
            seen.add(e["anlass_id"])
            unique_entries.append(e)
            
    return unique_entries

def get_gang_nummer(href, link_text):
    combined = f"{href} {link_text}".lower()
    gang_match = re.search(r"\b([1-6])\b\.?\s*(gang|g\b)", combined)
    if gang_match:
        return int(gang_match.group(1))
    zahlen = re.findall(r"\b([1-6])\b", combined)
    return int(zahlen[-1]) if zahlen else 0

def get_pdf_title(href, link_text, gang_num):
    combined = f"{href} {link_text}".lower()
    if "schluss" in combined or "-rl" in combined or "rangliste" in combined:
        return "Schlussrangliste"
    if gang_num > 0:
        return f"Statistik (nach dem {gang_num}. Gang)"
    return "Statistik"

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    requests.post(url, data=data, files=files, timeout=60).raise_for_status()

def process_fest(fest, state):
    # Wenn es ein externer Link ist, rufen wir ihn nicht auf, bieten aber den Button an
    if fest["is_external"]:
        print(f"Externes Fest erkannt (wird verlinkt): {fest['fest_name']}")
        return

    try:
        soup = get_soup(fest["detail_url"])
    except Exception:
        return

    page_text = clean_text(soup.get_text(" ", strip=True))
    schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger_txt = schwinger.group(1) if schwinger else ""

    website = ""
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            website = href
            break

    hat_schlussrangliste = False
    hat_finale_statistik = False

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))
        combined_meta = f"{href} {link_text}".lower()

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        # 🛑 HÄRTESTER FILTER GEGEN ZWISCHENRANGLISTEN & STARTLISTEN
        if any(w in combined_meta for w in ["zwischen", "startliste", "einteilung", "notizblatt", "paarung"]):
            continue

        is_stat = "statistik" in combined_meta or "st_" in combined_meta or "st." in combined_meta
        is_rl = "schluss" in combined_meta or "rangliste" in combined_meta or "-rl" in combined_meta

        if not (is_stat or is_rl):
            continue

        gang = get_gang_nummer(href, link_text)
        doc_title = get_pdf_title(href, link_text, gang)

        if doc_title == "Schlussrangliste":
            hat_schlussrangliste = True
        if doc_title == "Statistik" and gang == 0:
            hat_finale_statistik = True

        pdf_url = requests.compat.urljoin(BASE_URL, href) if href.startswith("/") else href
        filename = pdf_url.split("/")[-1].split("?")[0]
        storage_key = f"{fest['anlass_id']}_{filename}"

        if storage_key in state["known_pdfs"]:
            continue

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=30)
            pdf_bytes = res.content
        except Exception:
            continue

        # Speichern in der state.json
        state["known_pdfs"][storage_key] = hashlib.md5(pdf_bytes).hexdigest()
        save_state(state)

        # 🎯 CONDITION C: Nur senden, wenn die Baseline (Vergangenheit) fixiert ist
        if state["baseline_done"]:
            emoji = "🏆" if "Schluss" in doc_title else "📊"
            caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
            if schwinger_txt:
                caption += f"🤼 <b>{escape(schwinger_txt)} Aktivschwinger</b>\n"
            
            buttons = []
            if website:
                buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            
            reply_markup = {"inline_keyboard": [buttons]} if buttons else None

            print(f"SENDE LIVE-UPDATE: {filename}")
            send_telegram_document(pdf_bytes, filename, caption, reply_markup)
        else:
            print(f"Baseline blockiert vergangenheits-PDF stumm: {filename}")

    # 🏁 WENN SCHLUSSRANGLISTE UND FINALE STATISTIK GELESEN SIND -> ABSCHLIESSEN
    if hat_schlussrangliste and hat_finale_statistik:
        print(f"🏆 Fest {fest['fest_name']} ist komplett fertig. Wird für immer archiviert.")
        state["abgeschlossene_feste"][fest["anlass_id"]] = True
        save_state(state)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    fests = collect_live_fests(state)

    print(f"Anzahl aktuell laufender Feste auf Hauptseite: {len(fests)}")

    for fest in fests:
        process_fest(fest, state)

    # Nach dem allerersten Durchlauf die Live-Leitung freischalten
    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Vergangenheit erfolgreich blockiert. Ab der nächsten Minute wird scharf gesendet!")

    print("Bot-Scan erfolgreich beendet.")

if __name__ == "__main__":
    main()
