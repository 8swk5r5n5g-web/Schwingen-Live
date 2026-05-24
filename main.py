import os
import re
import json
import time
import hashlib
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

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))
LOOP_MINUTES = int(os.getenv("LOOP_MINUTES", "55"))


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

    # Der digitale Schutzschild: Wenn der Bot neu startet, weiß er, ob er die Vergangenheit schon kennt
    if "baseline_done" not in state:
        state["baseline_done"] = False

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def get_page(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=60,
                headers={
                    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 10
            print(f"Fehler bei Get-Versuch {attempt}: {exc}. Warte {wait_time}s...")
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


def should_track_pdf(href, link_text=""):
    return is_statistik(href, link_text) or is_schlussrangliste(href, link_text)


def get_pdf_title(href, link_text=""):
    text = clean_text(link_text)
    if is_statistik(href, text):
        return text if text else "Statistik"
    if is_schlussrangliste(href, text):
        return text if text else "Schlussrangliste"
    return "PDF"


def telegram_request_with_retry(url, data, timeout=90, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, data=data, timeout=timeout)
            if response.status_code == 200:
                return response
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 30)
                time.sleep(retry_after + 1)
                continue
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            time.sleep(attempt * 5)
    return None


def send_document(pdf_url, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    telegram_request_with_retry(
        url=telegram_url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def build_pdf_caption(pdf_title, fest, detail_infos):
    schwinger = detail_infos.get("schwinger", "")
    website = detail_infos.get("website", "")
    lines = [
        f"📅 Datum: {escape(fest.get('date_text', '-'))}",
        f"🏟 Fest: {escape(fest.get('fest_name', '-'))}",
        f"📍 Ort: {escape(fest.get('location', '-'))}",
        f"🤼 Anzahl Schwinger: {escape(schwinger) if schwinger else '-'}",
        f"🌐 Webseite Fest: {escape(website) if website else '-'}",
        f"📄 Dokument: {escape(pdf_title)}",
    ]
    return "\n".join(lines)


def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    
    page_text = clean_text(soup.get_text(" ", strip=True))
    schwinger = extract_number_after("Anzahl Schwinger", page_text)
    website = extract_fest_website(soup)
    detail_infos = {"schwinger": schwinger, "website": website}

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, link_text):
            continue

        pdf_url = normalise_url(href)
        pdf_title = get_pdf_title(href, link_text)
        
        # Sendersicherer Speicher-Schlüssel aus Anlass-ID und PDF-Name
        filename = pdf_url.split("/")[-1].split("?")[0]
        storage_key = f"{fest['anlass_id']}_{filename}"

        if storage_key in state["known_pdfs"]:
            continue

        # Als bekannt markieren
        state["known_pdfs"][storage_key] = {
            "title": pdf_title,
            "fest": fest.get("fest_name", ""),
            "date": fest.get("date_text", ""),
        }
        save_state(state)

        # 🛑 DIE ENTSCHEIDENDE BARRIERE: 
        # Nur wenn im vorherigen Lauf alle alten PDFs weggespeichert wurden, wird JETZT echt gesendet!
        if state["baseline_done"]:
            print(f"Echtzeit-Neuheit erkannt: {pdf_url}")
            caption = build_pdf_caption(pdf_title, fest, detail_infos)
            send_document(pdf_url, caption)
        else:
            print(f"VERGANGENHEIT GEBLOCKT (Stumm gespeichert): {filename}")


def check_ranglisten(state):
    print(f"Prüfung gestartet: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    fests = collect_active_fests()

    for fest in fests:
        try:
            process_fest(fest, state)
        except Exception as exc:
            print(f"Fehler bei {fest['detail_url']}: {exc}")

    # Wenn der allererste Durchlauf fertig ist, schalten wir die Live-Leitung frei
    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("--- VERGANGENHEIT ERFOLGREICH IMPORTIERT. AB JETZT WIRD LIVE GESENDET! ---")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    end_time = time.time() + (LOOP_MINUTES * 60)

    while time.time() < end_time:
        check_ranglisten(state)
        remaining_seconds = int(end_time - time.time())
        if remaining_seconds <= CHECK_INTERVAL_SECONDS:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
