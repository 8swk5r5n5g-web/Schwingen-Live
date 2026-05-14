import os
import re
import json
import time
import hashlib
from html import escape
from datetime import datetime

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
            state = json.load(file)
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

            print(f"GET direkt Versuch {attempt}: {url} -> {response.status_code}")
            response.raise_for_status()
            return response.text

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 10
            print(f"Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
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
    return " ".join(text.replace("\xa0", " ").replace(" .", ".").split()).strip()


def extract_date_from_text(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)

    if not match:
        return ""

    date_text = clean_text(match.group(0))
    date_text = date_text.replace(" .", ".")
    date_text = date_text.replace(". ", ".")
    return date_text


def is_jung_or_nachwuchs(text):
    text = text.lower()

    blocked_words = [
        "jung",
        "nachwuchs",
        "bueb",
        "bube",
        "buben",
        "schüler",
        "schueler",
        "knaben",
    ]

    return any(word in text for word in blocked_words)


def parse_overview_text(text):
    text = clean_text(text)

    date_text = extract_date_from_text(text)

    if not date_text:
        return None

    without_date = clean_text(text.replace(date_text, ""))
    without_date = without_date.replace("Rangliste", "")
    without_date = clean_text(without_date)

    parts = without_date.split()
    lower_parts = [part.lower() for part in parts]

    if "aktiv" not in lower_parts:
        return None

    aktiv_index = lower_parts.index("aktiv")

    fest_name = clean_text(" ".join(parts[:aktiv_index]))
    location = clean_text(" ".join(parts[aktiv_index + 1:]))

    if not fest_name:
        return None

    return {
        "date_text": date_text,
        "fest_name": fest_name,
        "location": location,
    }


def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)

    entries = []
    seen_urls = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        detail_url = normalise_url(href)

        if "ranglisten" not in detail_url:
            continue

        if "anlass=" not in detail_url:
            continue

        if detail_url in seen_urls:
            continue

        parent_text = clean_text(link.parent.get_text(" ", strip=True))

        if "aktiv" not in parent_text.lower():
            continue

        if is_jung_or_nachwuchs(parent_text):
            continue

        parsed = parse_overview_text(parent_text)

        if not parsed:
            continue

        seen_urls.add(detail_url)

        entries.append({
            "detail_url": detail_url,
            "overview_text": parent_text,
            "date_text": parsed["date_text"],
            "fest_name": parsed["fest_name"],
            "location": parsed["location"],
        })

    if not entries:
        print("Keine Aktiv-Feste gefunden.")
        return []

    newest_date = max(
        entries,
        key=lambda entry: datetime.strptime(entry["date_text"], "%d.%m.%Y")
    )["date_text"]

    filtered = [
        entry
        for entry in entries
        if entry["date_text"] == newest_date
    ]

    print(f"Neuestes Datum auf der Seite: {newest_date}")
    print(f"Gefundene Aktiv-Feste mit neuestem Datum: {len(filtered)}")

    for fest in filtered:
        print(
            f"Fest gefunden: "
            f"{fest['fest_name']} / "
            f"{fest['date_text']} / "
            f"{fest['location']}"
        )

    return filtered[:MAX_DETAIL_PAGES]


def is_blocked_pdf(href, link_text=""):
    combined = f"{href} {link_text}".lower()

    blocked_words = [
        "zwischenrangliste",
        "zwischenrang",
        "/zs",
        "_zs",
        "-zs",
        "gangliste",
        "notizblatt",
        "einteilung",
        "startliste",
        "paarung",
    ]

    return any(word in combined for word in blocked_words)


def is_schlussrangliste(href, link_text=""):
    combined = f"{href} {link_text}".lower()

    if is_blocked_pdf(href, link_text):
        return False

    return (
        "schlussrangliste" in combined
        or combined.endswith("-rl.pdf")
        or "_rl.pdf" in combined
    )


def is_statistik(href, link_text=""):
    combined = f"{href} {link_text}".lower()

    if is_blocked_pdf(href, link_text):
        return False

    return (
        "statistik" in combined
        or combined.endswith("-st.pdf")
        or "_st.pdf" in combined
    )


def should_track_pdf(href, link_text=""):
    return is_schlussrangliste(href, link_text) or is_statistik(href, link_text)


def get_pdf_title(href, link_text=""):
    text = clean_text(link_text)

    if is_schlussrangliste(href, text):
        return "Schlussrangliste"

    if is_statistik(href, text):
        if text:
            return text

        return "Statistik"

    return "PDF"


def download_pdf_for_hash(pdf_url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                pdf_url,
                timeout=90,
                headers={
                    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )

            print(f"PDF Download Versuch {attempt}: {pdf_url} -> {response.status_code}")
            response.raise_for_status()
            return response.content

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 10
            print(f"PDF Download Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

    raise RuntimeError(f"PDF konnte nicht geladen werden: {pdf_url}")


def get_pdf_hash(pdf_url):
    pdf_content = download_pdf_for_hash(pdf_url)
    return hashlib.sha256(pdf_content).hexdigest()


def telegram_request_with_retry(url, data, timeout=90, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, data=data, timeout=timeout)

            print(response.text)

            if response.status_code == 200:
                return response

            if response.status_code == 429:
                try:
                    retry_after = response.json().get("parameters", {}).get("retry_after", 30)
                except Exception:
                    retry_after = 30

                print(f"Telegram Rate Limit erreicht. Warte {retry_after} Sekunden...")
                time.sleep(retry_after + 1)
                continue

            response.raise_for_status()

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 5
            print(f"Telegram Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

    print("Telegram konnte nach mehreren Versuchen nicht senden.")
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


def build_pdf_caption(pdf_title, fest_name, date_text, location, pdf_url):
    lines = [
        f"Datum: {escape(date_text)}",
        f"Fest: {escape(fest_name)}",
        f"Ort: {escape(location)}",
        f"Dokument: {escape(pdf_title)}",
        f"PDF Datei: {escape(pdf_url)}",
    ]

    return "\n".join(lines)


def process_pdf(pdf_url, pdf_hash, pdf_title, fest, state):
    old_entry = state["known_pdfs"].get(pdf_url)

    if old_entry is None:
        state["known_pdfs"][pdf_url] = {
            "hash": pdf_hash,
            "title": pdf_title,
            "fest": fest.get("fest_name", ""),
            "date": fest.get("date_text", ""),
            "location": fest.get("location", ""),
        }

        save_state(state)

        if state["baseline_done"]:
            print(f"Neue PDF erkannt: {pdf_url}")

            caption = build_pdf_caption(
                pdf_title=pdf_title,
                fest_name=fest.get("fest_name", ""),
                date_text=fest.get("date_text", ""),
                location=fest.get("location", ""),
                pdf_url=pdf_url,
            )

            send_document(pdf_url, caption)
        else:
            print(f"Baseline speichert PDF ohne Senden: {pdf_url}")

        return

    old_hash = old_entry.get("hash", "")

    if old_hash != pdf_hash:
        state["known_pdfs"][pdf_url]["hash"] = pdf_hash
        save_state(state)

        if state["baseline_done"]:
            print(f"Aktualisierte PDF erkannt: {pdf_url}")

            caption = build_pdf_caption(
                pdf_title=pdf_title,
                fest_name=fest.get("fest_name", ""),
                date_text=fest.get("date_text", ""),
                location=fest.get("location", ""),
                pdf_url=pdf_url,
            )

            send_document(pdf_url, caption)

        return

    print(f"Unverändert: {pdf_url}")


def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])

    print(
        f"Aktiv-Fest scannen: "
        f"{fest['fest_name']} / "
        f"{fest['date_text']} / "
        f"{fest['location']}"
    )

    found = 0

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not should_track_pdf(href, link_text):
            continue

        pdf_url = normalise_url(href)
        pdf_title = get_pdf_title(href, link_text)

        try:
            pdf_hash = get_pdf_hash(pdf_url)
        except Exception as exc:
            print(f"Konnte PDF nicht prüfen: {pdf_url} / {exc}")
            continue

        found += 1

        process_pdf(
            pdf_url=pdf_url,
            pdf_hash=pdf_hash,
            pdf_title=pdf_title,
            fest=fest,
            state=state,
        )

        time.sleep(1)

    print(f"Relevante PDFs gefunden: {found}")


def check_ranglisten(state):
    fests = collect_active_fests()

    if not state["baseline_done"]:
        print("ERSTER LAUF: Bestehende PDFs werden nur gespeichert, NICHT gesendet.")

    for fest in fests:
        try:
            process_fest(fest, state)
        except Exception as exc:
            print(f"Fehler bei {fest['detail_url']}: {exc}")

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fertig. Ab jetzt werden nur neue oder aktualisierte PDFs gesendet.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    state = load_state()

    print("Starte Bot: Neueste Aktiv-Feste scannen, nur Statistik und Schlussranglisten senden.")

    check_ranglisten(state)

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
