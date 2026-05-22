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
PUBLIC_AGENDA_URL = "https://esv.ch/agenda/"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en-US;q=0.8,en;q=0.7",
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
    blocked = ["jung", "nachwuchs", "bueb", "bube", "buben", "schüler", "schueler", "knaben", "jahrg"]
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

# --- Strategische Freitags-Agenda (Auslesung der stabilen öffentlichen ESV-Agenda) ---
def check_and_send_agenda(state, is_manual=False):
    now = datetime.now()
    current_calendar_week = now.strftime("%Y-%V")
    
    if not is_manual:
        if now.weekday() != 4 or now.hour != 12:
            return
        if state["last_agenda_sent"] == current_calendar_week:
            return

    print("Starte Agenda-Lauf: Generiere Vorschau aus esv.ch/agenda/ ...")
    soup = get_soup(PUBLIC_AGENDA_URL)
    
    agenda_entries = []
    friday_date = now.date()
    saturday = friday_date + timedelta(days=1)
    sunday = friday_date + timedelta(days=2)
    
    allowed_dates = [saturday, sunday]
    if is_manual:
        # Bei manuellem Start erweitertes Zeitfenster um das aktuelle Datum herum
        allowed_dates = [friday_date + timedelta(days=i) for i in range(-2, 4)]

    # Jedes Event auf der neuen ESV-Seite durchgehen
    for event in soup.find_all(["tr", "div", "article"]):
        text_content = clean_text(event.get_text(" "))
        
        date_str = extract_date(text_content)
        if not date_str:
            continue
            
        try:
            fest_date = parse_date(date_str)
            if fest_date.date() in allowed_dates:
                # Nachwuchs aussortieren
                if is_jung_or_nachwuchs(text_content):
                    continue
                
                # Wir suchen nach typischen Merkmalen von Aktivfesten
                # (Entweder das Wort 'aktiv', Schwingfest, Schwinget, oder Verbandstypen)
                is_aktiv_fest = any(word in text_content.lower() for word in [
                    "aktiv", "schwingfest", "schwinget", "kantonal", "gau", "bergkranz", "regional"
                ])
                
                if is_aktiv_fest:
                    link_el = event.find("a", href=True)
                    detail_url = urljoin(PUBLIC_AGENDA_URL, link_el["href"]) if link_el else PUBLIC_AGENDA_URL
                    
                    # Text bereinigen, um einen sauberen Namen zu bekommen
                    name_raw = text_content.replace(date_str, "")
                    for word in ["aktiv", "details", "info", "agenda"]:
                        name_raw = re.sub(r"\b" + word + r"\b", "", name_raw, flags=re.IGNORECASE)
                    
                    # Versuche verbleibende Zahlen (wie Schwingeranzahlen) zu extrahieren
                    schwinger_count = ""
                    match_s = re.search(r"\b(\d{2,3})\b", name_raw)
                    if match_s:
                        val = match_s.group(1)
                        # Wenn die Zahl im Bereich realistischer Schwingerzahlen liegt
                        if 30 <= int(val) <= 300 and val not in date_str:
                            schwinger_count = val
                            name_raw = name_raw.replace(val, "")

                    fest_name = clean_text(re.sub(r"\s+", " ", name_raw))
                    
                    # Kürzen falls Reste von Orten/Kategorien am Ende hängen
                    if len(fest_name) > 80:
                        fest_name = fest_name[:77] + "..."

                    if len(fest_name) > 5:
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
            # Duplikate anhand des bereinigten Namens aussortieren
            short_name = f["name"][:30]
            if short_name not in seen:
                seen.add(short_name)
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

# --- Ranglisten-Überwachung (Unveränderte Live-Funktion für das Wochenende) ---
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
