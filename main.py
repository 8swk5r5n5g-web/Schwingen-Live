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

# Die erreichbare Datenbankseite ohne 403-Sperren
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
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

def collect_active_fests():
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"Fehler Übersicht: {e}")
        return []

    grouped = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        full_url = requests.compat.urljoin(BASE_URL, href)
        if "anlass=" not in full_url:
            continue
        
        anlass_id = parse_qs(urlparse(full_url).query).get("anlass", [""])[0]
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
        fest_name = clean_text(parts[1])
        category = clean_text(parts[2]).lower()
        row_text = clean_text(" ".join(parts))

        # Nur Aktiv-Feste berücksichtigen
        if category != "aktiv" or is_jung_or_nachwuchs(row_text):
            continue

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
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))
        combined_meta = f"{href} {link_text}".lower()

        # Es muss zwingend eine PDF-Datei sein
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        # 🎯 MESSERSCHARFE LOGIK NACH DEINEM SCREENSHOT:
        # Es MUSS das Wort "rangliste" enthalten sein, aber DARF NICHT "zwischen" enthalten!
        # Statistiken, Einteilungen und Zwischenranglisten fliegen hier eiskalt raus.
        is_rangliste = "rangliste" in combined_meta or "classement" in combined_meta or "classifica" in combined_meta
        is_zwischen = "zwischen" in combined_meta

        if not is_rangliste or is_zwischen:
            continue

        pdf_url = requests.compat.urljoin(BASE_URL, href)
        filename = pdf_url.split("/")[-1].split("?")[0]
        storage_key = f"{fest['anlass_id']}_{filename}"

        if storage_key in state["known_pdfs"]:
            continue

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=30)
            pdf_bytes = res.content
        except Exception:
            continue

        # Registrieren in der state.json
        state["known_pdfs"][storage_key] = hashlib.md5(pdf_bytes).hexdigest()
        save_state(state)

        # Erst senden, wenn die Baseline (Vergangenheit) stumm eingelesen wurde
        if state["baseline_done"]:
            caption = (
                f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                f"🏆 <b>Offizielle Festrangliste verfügbar!</b>\n"
            )
            if schwinger_txt:
                caption += f"🤼 {escape(schwinger_txt)} Aktivschwinger\n"

            print(f"SENDE RANGLISTE: {filename}")
            send_telegram_document(pdf_bytes, filename, caption)
        else:
            print(f"Baseline speichert alte Rangliste stumm ab: {filename}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    fests = collect_active_fests()

    print(f"Anzahl gefundener Aktiv-Feste: {len(fests)}")

    for fest in fests:
        process_fest(fest, state)

    # Schaltet ab dem zweiten Durchlauf die Live-Benachrichtigungen scharf
    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Sicherheitsnetz aktiv: Alle alten Ranglisten wurden stumm importiert.")

    print("Bot-Scan erfolgreich beendet.")

if __name__ == "__main__":
    main()
