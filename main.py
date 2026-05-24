import os
import re
import json
import time
import hashlib
from io import BytesIO
from html import escape
import requests
from bs4 import BeautifulSoup

# HIER deine festen Daten eintragen (oder als Umgebungsvariable übergeben)
BOT_TOKEN = "DEIN_TELEGRAM_BOT_TOKEN"
CHAT_ID = "DEIN_TELEGRAM_CHAT_ID"

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "/root/tradingbot/state_schwingen.json" # Pfad anpassen

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {"known_pdfs": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

def get_soup(url):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")

def normalise_url(url):
    if url.startswith("http"):
        return url
    return requests.compat.urljoin(BASE_URL, url)

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
        full_url = normalise_url(href)
        if "anlass=" not in full_url:
            continue
        
        # Anlass ID extrahieren
        from urllib.parse import urlparse, parse_qs
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

        if category != "aktiv" or is_jung_or_nachwuchs(row_text):
            continue

        entries.append({"anlass_id": anlass_id, "detail_url": data["detail_url"], "fest_name": fest_name})
    return entries

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

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    requests.post(url, data=data, files=files, timeout=60).raise_for_status()

def main():
    print("Schwingbot Aktiv-Dienst gestartet...")
    state = load_state()

    while True:
        try:
            fests = collect_active_fests()
            for fest in fests:
                try:
                    soup = get_soup(fest["detail_url"])
                except Exception:
                    continue

                # Webseite extrahieren
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

                    is_stat = "statistik" in href.lower() or "statistik" in link_text.lower()
                    is_rl = "schluss" in href.lower() or "schluss" in link_text.lower()
                    if not (is_stat or is_rl):
                        continue

                    pdf_url = normalise_url(href)
                    filename = pdf_url.split("/")[-1].split("?")[0]
                    storage_key = f"{fest['anlass_id']}_{filename}"

                    if storage_key in state["known_pdfs"]:
                        continue

                    # Download und Senden
                    res = requests.get(pdf_url, headers=HEADERS, timeout=30)
                    pdf_bytes = res.content
                    
                    state["known_pdfs"][storage_key] = hashlib.md5(pdf_bytes).hexdigest()
                    save_state(state)

                    gang = get_gang_nummer(href, link_text)
                    title = "Schlussrangliste" if is_rl else ("Statistik" if gang == 99 else f"Statistik (nach dem {gang}. Gang)")
                    emoji = "🏆" if is_rl else "📊"

                    caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                    if schwinger_txt:
                        caption += f"🤼 <b>{escape(schwinger_txt)} Aktivschwinger</b>\n"
                    caption += f"📝 <b>{emoji} {title}</b>"

                    print(f"Sende: {filename}")
                    send_telegram_document(pdf_bytes, filename, caption, reply_markup)

        except Exception as e:
            print(f"Fehler im Hauptzyklus: {e}")

        # ⏱ Präzise 5 Minuten warten bis zum nächsten Scan
        time.sleep(300)

if __name__ == "__main__":
    main()
