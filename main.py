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
        print("MANUELLER START: Sende die aktuellsten PDFs sofort raus!")
        return {"known_pdfs": {}, "fests": {}, "baseline_done": True}

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    if "known_pdfs" not in state:
        state["known_pdfs"] = {}
    if "fests" not in state:
        state["fests"] = {}
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
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
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
    for anlass_id, data in grouped.items():
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
            "anlass_id": anlass_id,
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

def get_gang_nummer(href, link_text):
    combined = f"{href} {link_text}".lower()
    gang_match = re.search(r"(\d+)\.?\s*(gang|g\b)", combined)
    return int(gang_match.group(1)) if gang_match else 0

def get_pdf_title(href, link_text, gang_num):
    text = clean_text(link_text)
    if is_schlussrangliste(href, text):
        return "Schlussrangliste"
    if is_statistik(href, text):
        if len(text) > 9 and "statistik" in text.lower():
            return text
        if gang_num > 0:
            return f"Statistik (nach dem {gang_num}. Gang)"
        return "Statistik"
    return "PDF"

def get_pdf_bytes(pdf_url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(pdf_url, headers=HEADERS, timeout=90)
            response.raise_for_status()
            return response.content
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"PDF konnte nicht geladen werden: {pdf_url}")

def send_telegram_document(pdf_bytes, filename, caption, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    requests.post(url, data=data, files=files, timeout=90).raise_for_status()

def process_fest(fest, state):
    anlass_id = fest["anlass_id"]
    soup = get_soup(fest["detail_url"])
    detail_infos = extract_detail_infos(soup)

    print(f"Scanne: {fest['fest_name']} (ID: {anlass_id})")
    
    # Fest-Speicher initialisieren, falls neu
    if anlass_id not in state["fests"]:
        state["fests"][anlass_id] = {"last_gang": 0, "has_schlussrangliste": False}

    statistiken = []
    schlussrangliste = None

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if not is_real_pdf_url(href) or is_blocked_pdf(href, link_text):
            continue

        pdf_url = normalise_url(href)

        if is_schlussrangliste(href, link_text):
            schlussrangliste = {"url": pdf_url, "href": href, "link_text": link_text}
        elif is_statistik(href, link_text):
            gang = get_gang_nummer(href, link_text)
            statistiken.append({"url": pdf_url, "href": href, "link_text": link_text, "gang": gang})

    # BUTTONS BAUEN
    buttons = []
    if detail_infos.get("website"):
        buttons.append({"text": "🌐 Fest-Webseite", "url": detail_infos.get("website")})
    reply_markup = {"inline_keyboard": [buttons]} if buttons else None

    # 1. VERARBEITUNG DER STATISTIK
    if statistiken:
        neueste_statistik = max(statistiken, key=lambda x: x["gang"])
        aktuelle_gang_num = neueste_statistik["gang"]
        letzte_gesendete_gang_num = state["fests"][anlass_id]["last_gang"]

        print(f"-> Gefundener Gang auf ESV: {aktuelle_gang_num} | Zuletzt gesendet: {letzte_gesendete_gang_num}")

        # Nur senden, wenn die Gangnummer ECHT HÖHER ist als die zuvor gesendete!
        if aktuelle_gang_num > letzte_gesendete_gang_num:
            try:
                pdf_bytes = get_pdf_bytes(neueste_statistik["url"])
                filename = neueste_statistik["url"].split("/")[-1].split("?")[0]
                doc_title = get_pdf_title(neueste_statistik["href"], neueste_statistik["link_text"], aktuelle_gang_num)
                
                caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                if detail_infos.get("schwinger"):
                    caption += f"🤼 <b>{escape(detail_infos['schwinger'])} Aktivschwinger</b>\n"
                caption += f"📝 <b>📊 {doc_title}</b>"

                state["fests"][anlass_id]["last_gang"] = aktuelle_gang_num
                save_state(state)

                if state["baseline_done"]:
                    print(f"Sende neue Statistik (Gang {aktuelle_gang_num}) für {fest['fest_name']}")
                    send_telegram_document(pdf_bytes, filename, caption, reply_markup)
            except Exception as exc:
                print(f"Fehler bei Statistik-Verarbeitung: {exc}")
        else:
            print(f"-> Überspringe Statistik. Keine neuere Gangnummer (aktuell: {aktuelle_gang_num} <= gesendet: {letzte_gesendete_gang_num})")

    # 2. VERARBEITUNG DER SCHLUSSRANGLISTE
    if schlussrangliste and not state["fests"][anlass_id]["has_schlussrangliste"]:
        try:
            pdf_bytes = get_pdf_bytes(schlussrangliste["url"])
            filename = schlussrangliste["url"].split("/")[-1].split("?")[0]
            
            caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
            if detail_infos.get("schwinger"):
                caption += f"🤼 <b>{escape(detail_infos['schwinger'])} Aktivschwinger</b>\n"
            caption += f"📝 <b>🏆 Schlussrangliste</b>"

            state["fests"][anlass_id]["has_schlussrangliste"] = True
            save_state(state)

            if state["baseline_done"]:
                print(f"Sende Schlussrangliste für {fest['fest_name']}")
                send_telegram_document(pdf_bytes, filename, caption, reply_markup)
        except Exception as exc:
            print(f"Fehler bei Schlussrangliste: {exc}")

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
