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
            wait_time = attempt * 5
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
        fest_name = clean_text(parts[1])
        category = clean_text(parts[2]).lower()
        row_text = clean_text(" ".join(parts))

        if category != "aktiv" or is_jung_or_nachwuchs(row_text):
            continue

        entries.append({
            "anlass_id": anlass_id,
            "detail_url": data["detail_url"],
            "fest_name": fest_name,
        })
    return entries[:MAX_DETAIL_PAGES]


def get_gang_nummer(href, link_text):
    combined = f"{href} {link_text}".lower()
    gang_match = re.search(r"\b([1-6])\b\.?\s*(gang|g\b)", combined)
    if gang_match:
        return int(gang_match.group(1))
    zahlen = re.findall(r"\b([1-6])\b", combined)
    if zahlen:
        return int(zahlen[-1])
    return 0


def get_pdf_title(href, link_text, gang_num):
    combined = f"{href} {link_text}".lower()
    if "schluss" in combined or "-rl" in combined or "rangliste" in combined:
        return "Schlussrangliste"
    if gang_num > 0:
        return f"Statistik (nach dem {gang_num}. Gang)"
    return "Statistik"


def download_pdf_for_hash(pdf_url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                pdf_url,
                timeout=60,
                headers={
                    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
            print(f"PDF Download Fehler bei Versuch {attempt}: {exc}. Warte {wait_time}s...")
            time.sleep(wait_time)
    raise RuntimeError(f"PDF konnte nicht geladen werden: {pdf_url}")


def telegram_request_with_retry(url, data, pdf_bytes, filename, timeout=90, retries=3):
    for attempt in range(1, retries + 1):
        try:
            files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
            response = requests.post(url, data=data, files=files, timeout=timeout)
            if response.status_code == 200:
                return response
            if response.status_code == 429:
                try:
                    retry_after = response.json().get("parameters", {}).get("retry_after", 30)
                except Exception:
                    retry_after = 30
                print(f"Telegram Rate Limit. Warte {retry_after} Sekunden...")
                time.sleep(retry_after + 1)
                continue
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
            print(f"Telegram Fehler bei Versuch {attempt}: {exc}. Warte {wait_time}s...")
            time.sleep(wait_time)
    return None


def send_document(pdf_bytes, filename, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {
        "chat_id": CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    telegram_request_with_retry(url=telegram_url, data=data, pdf_bytes=pdf_bytes, filename=filename)


def process_fest(fest, state):
    try:
        soup = get_soup(fest["detail_url"])
    except Exception as e:
        print(f"Fehler beim Laden von {fest['fest_name']}: {e}")
        return

    print(f"Scanne Fest: {fest['fest_name']} (ID: {fest['anlass_id']})")

    website = ""
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            website = href
            break

    page_text = clean_text(soup.get_text(" ", strip=True))
    schwinger = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger_txt = schwinger.group(1) if schwinger else ""

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))
        combined_meta = f"{href} {link_text}".lower()

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        # 🛑 STRIKTE SPERRE: Keine Zwischenranglisten, Startlisten, Einteilungen
        if any(w in combined_meta for w in ["zwischen", "startliste", "einteilung", "notizblatt"]):
            continue

        # Nur echte Statistiken oder Schlussranglisten zulassen
        is_stat = "statistik" in combined_meta or "st_" in combined_meta or "st." in combined_meta
        is_rl = "schluss" in combined_meta or "rangliste" in combined_meta or "-rl" in combined_meta

        if not (is_stat or is_rl):
            continue

        pdf_url = normalise_url(href)
        filename = pdf_url.split("/")[-1].split("?")[0]
        
        # ⚡ TRICK FÜR DEN JETZIGEN 2. GANG: 
        # Wir hängen temporär ein '_live' an den Key an, damit der blockierte 2. Gang JETZT SOFORT gesendet wird.
        storage_key = f"{fest['anlass_id']}_{filename}_live"

        if storage_key in state["known_pdfs"]:
            continue

        try:
            pdf_content = download_pdf_for_hash(pdf_url)
            pdf_hash = hashlib.sha256(pdf_content).hexdigest()
        except Exception as exc:
            print(f"Konnte PDF nicht herunterladen: {filename} / {exc}")
            continue

        # Speichern im Zustand
        state["known_pdfs"][storage_key] = {
            "hash": pdf_hash,
            "filename": filename,
            "fest": fest.get("fest_name", ""),
        }
        save_state(state)

        # Wenn die Baseline steht, wird echt gesendet
        if state["baseline_done"]:
            gang = get_gang_nummer(href, link_text)
            doc_title = get_pdf_title(href, link_text, gang)
            emoji = "🏆" if "Schluss" in doc_title else "📊"

            caption = f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
            if schwinger_txt:
                caption += f"🤼 <b>{escape(schwinger_txt)} Aktivschwinger</b>\n"
            if website:
                caption += f"🌐 <a href='{escape(website)}'>Fest-Webseite</a>\n"
            caption += f"📝 <b>{emoji} {doc_title}</b>"

            print(f"SENDE LIVE-UPDATE: {filename}")
            send_document(pdf_content, filename, caption)
        else:
            print(f"Baseline speichert bestehende PDF ohne Senden: {filename}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()
    fests = collect_active_fests()

    print(f"Anzahl gefundener aktiver Feste: {len(fests)}")

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline erfolgreich fixiert. Ab dem nächsten Durchlauf wird live gesendet.")

    print("Bot-Scan erfolgreich beendet.")


if __name__ == "__main__":
    main()
