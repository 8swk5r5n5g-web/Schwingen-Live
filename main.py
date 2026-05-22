import os
import re
import json
import time
from io import BytesIO
from html import escape
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
AGENDA_URL = "https://arls.esv.ch/agenda/"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    if "known_pdfs" not in state or not isinstance(state["known_pdfs"], dict):
        state["known_pdfs"] = {}

    if "baseline_done" not in state:
        state["baseline_done"] = False

    if "last_agenda_sent" not in state:
        state["last_agenda_sent"] = ""

    if not state["known_pdfs"]:
        state["baseline_done"] = False

    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()

def normalise_url(url):
    return urljoin(BASE_URL, url) if not url.startswith("http") else url

def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def extract_date(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    if not match:
        return ""
    return match.group(0).replace("..", ".")

def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y")

def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben"]
    return any(word in text for word in blocked)

def send_telegram_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(url, data=data, timeout=30)
    response.raise_for_status()

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    response = requests.post(url, data=data, files=files, timeout=60)
    response.raise_for_status()

# --- Strategische Freitags-Agenda (Auslesung über den gesamten Seiten-Text) ---
def check_and_send_agenda(state, is_manual=False):
    now = datetime.now()
    current_calendar_week = now.strftime("%Y-%V")
    
    if not is_manual:
        if now.weekday() != 4 or now.hour != 12:
            return
        if state["last_agenda_sent"] == current_calendar_week:
            return

    print("Starte Agenda-Lauf: Generiere Vorschau aus arls.esv.ch/agenda/ (Volltext-Modus)...")
    soup = get_soup(AGENDA_URL)
    
    # Gesamten Text der Seite holen und in Zeilen zerlegen
    page_lines = [clean_text(line) for line in soup.get_text("\n").split("\n") if line.strip()]
    
    agenda_entries = []
    friday_date = now.date()
    saturday = friday_date + timedelta(days=1)
    sunday = friday_date + timedelta(days=2)
    
    allowed_dates = [saturday, sunday]
    if is_manual:
        allowed_dates = [friday_date + timedelta(days=i) for i in range(-2, 4)]

    # Auch gezielt nach Links suchen, falls wir URLs brauchen
    links_dict = {}
    for link in soup.find_all("a", href=True):
        link_text = clean_text(link.get_text())
        if link_text and "anlass=" in link["href"]:
            links_dict[link_text] = normalise_url(link["href"])

    for line in page_lines:
        # Prüfen, ob ein Datum in der Zeile existiert
        date_str = extract_date(line)
        if not date_str:
            continue
            
        try:
            fest_date = parse_date(date_str)
            if fest_date.date() in allowed_dates:
                # Nur Aktivfeste beachten, Nachwuchs blockieren
                if "aktiv" in line.lower() and not is_jung_or_nachwuchs(line):
                    
                    # Text bereinigen, um den reinen Festnamen zu isolieren
                    clean_line = line.replace(date_str, "")
                    clean_line = re.sub(r"\baktiv\b", "", clean_line, flags=re.IGNORECASE)
                    
                    # Eventuelle Schwingeranzahl am Ende der Zeile heraussuchen
                    schwinger_count = ""
                    match_s = re.search(r"(\d+)\s*$", clean_line)
                    if match_s and int(match_s.group(1)) > 20:
                        schwinger_count = match_s.group(1)
                        clean_line = clean_line.replace(schwinger_count, "")
                    
                    fest_name = clean_text(re.sub(r"\s+", " ", clean_line))
                    
                    # Dem Namen die URL zuordnen falls auffindbar
                    detail_url = AGENDA_URL
                    for k, v in links_dict.items():
                        if k in line or fest_name in k:
                            detail_url = v
                            break

                    if len(fest_name) > 3:
                        agenda_entries.append({
                            "name": fest_name,
                            "date": date_str,
                            "url": detail_url,
                            "schwinger": schwinger_count
                        })
        except Exception:
            continue

    if agenda_entries:
        msg = "📅 <b>VORSCHAU: Aktiv-Schwingfeste an diesem Wochenende</b>\n\n"
        inline_buttons = []
        
        seen = set()
        unique_entries = []
        for f in agenda_entries:
            if f["name"] not in seen:
                seen.add(f["name"])
                unique_entries.append(f)

        for f in unique_entries:
            msg += f"🏟 <b>{escape(f['name'])}</b>\n📅 {escape(f['date'])}\n"
            if f["schwinger"]:
                msg += f"🤼 Gemeldete Aktivschwinger: <b>{escape(f['schwinger'])}</b>\n"
            msg += "\n"
            inline_buttons.append([{"text": f"🔗 Details: {f['name'][:22]}", "url": f["url"]}])
            
        msg += "💪 <i>Allen Schwingern ein erfolgreiches und verletzungsfreies Wochenende!</i>"
        
        send_telegram_message(msg, {"inline_keyboard": inline_buttons})
        print("Agenda-Vorschau erfolgreich im Telegram-Kanal gepostet.")
    else:
        print("Keine anstehenden Aktivfeste für das Wochenende in der Agenda gefunden. Kanal bleibt stumm.")
        
    if not is_manual:
        state["last_agenda_sent"] = current_calendar_week
        save_state(state)

# --- Ranglisten-Überwachung ---
def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)
    grouped = {}

    for link in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, link["href"])
        if "anlass=" not in href:
            continue

        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        values = query.get("anlass", [])
        anlass_id = values[0] if values else ""
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
        return []

    newest_date = max(entries, key=lambda x: parse_date(x["date_text"]))["date_text"]
    return [e for e in entries if e["date_text"] == newest_date]

def should_track_pdf(href, title):
    combined = f"{href} {title}".lower()
    if not href.lower().split("?")[0].endswith(".pdf"):
        return False
    
    blocked = ["startliste", "einteilung", "notizblatt", "paarung", "zwischenrang"]
    if any(word in combined for word in blocked):
        return False

    return (
        "statistik" in combined or "-st.pdf" in combined or "_st.pdf" in combined or
        "schlussrangliste" in combined or "schlussrang" in combined or 
        "-rl.pdf" in combined or "_rl.pdf" in combined
    )

def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    page_text = clean_text(soup.get_text(" ", strip=True))
    
    match_schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger = match_schwinger.group(1) if match_schwinger else ""
    
    website = ""
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            website = href
            break

    for link in soup.find_all("a", href=True):
        href = link["href"]
        title = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, title):
            continue

        pdf_url = urljoin(BASE_URL, href)
        
        if pdf_url in state["known_pdfs"]:
            continue

        try:
            res = requests.get(pdf_url, headers=HEADERS, timeout=60)
            res.raise_for_status()
            pdf_bytes = res.content
            
            state["known_pdfs"][pdf_url] = {
                "fest": fest["fest_name"],
                "date": fest["date_text"]
            }
            save_state(state)

            if not state["baseline_done"]:
                print(f"Stille Sicherung: {pdf_url}")
                continue

            if "schluss" in pdf_url.lower() or "rl" in pdf_url.lower():
                doc_type = "🏆 Schlussrangliste"
            else:
                doc_type = "📊 Statistik (Gänge 1–6)"
            
            caption = (
                f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                f"📍 {escape(fest['location'])}  —  📅 {escape(fest['date_text'])}\n\n"
                f"🤼 <b>{escape(schwinger)} Aktivschwinger</b> im Einsatz\n"
                f"📝 Dokument: <b>{doc_type}</b>" if schwinger else
                f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                f"📍 {escape(fest['location'])}  —  📅 {escape(fest['date_text'])}\n\n"
                f"📝 Dokument: <b>{doc_type}</b>"
            )

            inline_buttons = []
            if website:
                inline_buttons.append({"text": "🌐 Fest-Webseite", "url": website})
            inline_buttons.append({"text": "🔗 ESV Direktlink", "url": pdf_url})

            filename = pdf_url.split("/")[-1].split("?")[0]
            send_telegram_document(pdf_bytes, filename, caption, {"inline_keyboard": [inline_buttons]})
            print(f"Erfolgreich im Premium-Design gepostet: {filename}")
            
            time.sleep(2)
        except Exception as exc:
            print(f"Fehler bei {pdf_url}: {exc}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt in GitHub Secrets.")
        
    state = load_state()
    
    is_manual_run = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    
    try:
        check_and_send_agenda(state, is_manual=is_manual_run)
    except Exception as exc:
        print(f"Fehler bei Agenda: {exc}")
        
    try:
        fests = collect_active_fests()
        for fest in fests:
            process_fest(fest, state)
            
        if not state["baseline_done"]:
            state["baseline_done"] = True
            save_state(state)
            print("Baseline für Live-Kanal fixiert!")
    except Exception as exc:
        print(f"Fehler bei Ranglisten: {exc}")

if __name__ == "__main__":
    main()
