import os
import re
import json
import time
import hashlib
from html import escape
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 Schwingen-Live-Bot/1.0",
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

    # ABSOLUTE SICHERHEIT:
    # Wenn keine bekannten PDFs vorhanden sind, darf niemals gesendet werden.
    # Dann wird zuerst eine neue Baseline gemacht.
    if not state["known_pdfs"]:
        state["baseline_done"] = False

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()


def normalise_url(url):
    return urljoin(BASE_URL, url)


def get_page(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    print(f"GET: {url} -> {response.status_code}")
    response.raise_for_status()
    return response.text


def get_soup(url):
    return BeautifulSoup(get_page(url), "html.parser")


def extract_date(text):
    match = re.search(r"\d{2}\.\d{2}\.?\d{4}", text)
    if not match:
        return ""
    return match.group(0).replace("..", ".")


def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y")


def get_anlass_id(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("anlass", [])
    return values[0] if values else ""


def is_jung_or_nachwuchs(text):
    text = text.lower()
    blocked = [
        "jung",
        "nachwuchs",
        "bueb",
        "bube",
        "buben",
        "schüler",
        "schueler",
        "knaben",
    ]
    return any(word in text for word in blocked)


def collect_active_fests():
    soup = get_soup(RANGLISTEN_URL)

    grouped = {}

    for link in soup.find_all("a", href=True):
        href = normalise_url(link["href"])

        if "anlass=" not in href:
            continue

        anlass_id = get_anlass_id(href)
        text = clean_text(link.get_text(" ", strip=True))

        if not anlass_id or not text:
            continue

        if anlass_id not in grouped:
            grouped[anlass_id] = {
                "detail_url": href,
                "parts": [],
            }

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

        if not date_text:
            continue

        if category != "aktiv":
            continue

        if is_jung_or_nachwuchs(row_text):
            continue

        entries.append({
            "detail_url": data["detail_url"],
            "date_text": date_text,
            "fest_name": fest_name,
            "location": location,
        })

    if not entries:
        print("Keine Aktiv-Feste gefunden.")
        return []

    newest_date = max(entries, key=lambda x: parse_date(x["date_text"]))["date_text"]

    newest_entries = [
        entry for entry in entries
        if entry["date_text"] == newest_date
    ]

    print(f"Alle Aktiv-Feste gefunden: {len(entries)}")
    print(f"Neuestes Datum auf der Seite: {newest_date}")
    print(f"Aktiv-Feste mit neuestem Datum: {len(newest_entries)}")

    for fest in newest_entries:
        print(
            f"Fest gefunden: {fest['fest_name']} / "
            f"{fest['date_text']} / "
            f"{fest['location']} / "
            f"{fest['detail_url']}"
        )

    return newest_entries


def extract_number_after(label, text):
    match = re.search(rf"{re.escape(label)}\s+(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def extract_fest_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "arls.esv.ch" not in href and "esv.ch" not in href:
            return href
    return ""


def extract_detail_infos(soup):
    page_text = clean_text(soup.get_text(" ", strip=True))

    return {
        "schwinger": extract_number_after("Anzahl Schwinger", page_text),
        "website": extract_fest_website(soup),
    }


def is_real_pdf(href):
    return href.lower().split("?")[0].endswith(".pdf")


def is_blocked_pdf(href, title):
    combined = f"{href} {title}".lower()

    blocked = [
        "zwischenrangliste",
        "zwischenrang",
        "startliste",
        "einteilung",
        "notizblatt",
        "paarung",
    ]

    return any(word in combined for word in blocked)


def should_track_pdf(href, title):
    if not is_real_pdf(href):
        return False

    if is_blocked_pdf(href, title):
        return False

    combined = f"{href} {title}".lower()

    return (
        "statistik" in combined
        or "-st.pdf" in combined
        or "_st.pdf" in combined
        or "schlussrangliste" in combined
        or "schlussrang" in combined
        or "-rl.pdf" in combined
        or "_rl.pdf" in combined
    )


def get_pdf_title(href, title):
    title = clean_text(title)

    if title:
        return title

    href_lower = href.lower()

    if "-st.pdf" in href_lower or "_st.pdf" in href_lower:
        return "Statistik"

    if "-rl.pdf" in href_lower or "_rl.pdf" in href_lower:
        return "Schlussrangliste"

    return "PDF"


def get_pdf_hash(pdf_url):
    response = requests.get(pdf_url, headers=HEADERS, timeout=90)
    print(f"PDF Download: {pdf_url} -> {response.status_code}")
    response.raise_for_status()
    return hashlib.sha256(response.content).hexdigest()


def build_caption(fest, pdf_title, infos):
    schwinger = infos.get("schwinger", "")
    website = infos.get("website", "")

    lines = [
        f"📅 Datum: {escape(fest.get('date_text', '-'))}",
        f"🏟 Fest: {escape(fest.get('fest_name', '-'))}",
        f"📍 Ort: {escape(fest.get('location', '-'))}",
        f"🤼 Anzahl Schwinger: {escape(schwinger) if schwinger else '-'}",
        f"🌐 Webseite Fest: {escape(website) if website else '-'}",
        f"📄 Dokument: {escape(pdf_title)}",
    ]

    return "\n".join(lines)


def send_document(pdf_url, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    response = requests.post(
        telegram_url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        },
        timeout=120,
    )

    print(response.text)
    response.raise_for_status()


def process_pdf(pdf_url, pdf_title, fest, infos, state):
    pdf_hash = get_pdf_hash(pdf_url)
    old_entry = state["known_pdfs"].get(pdf_url)

    # Neue URL
    if old_entry is None:
        state["known_pdfs"][pdf_url] = {
            "hash": pdf_hash,
            "title": pdf_title,
            "fest": fest.get("fest_name", ""),
            "date": fest.get("date_text", ""),
            "location": fest.get("location", ""),
            "schwinger": infos.get("schwinger", ""),
            "website": infos.get("website", ""),
        }

        save_state(state)

        if not state["baseline_done"]:
            print(f"Baseline speichert bestehende PDF ohne Senden: {pdf_url}")
            return

        print(f"Neue PDF erkannt und wird gesendet: {pdf_url}")

        caption = build_caption(fest, pdf_title, infos)
        send_document(pdf_url, caption)
        return

    # Bekannte URL: NIEMALS nochmals senden
    old_hash = old_entry.get("hash", "") if isinstance(old_entry, dict) else old_entry

    if old_hash != pdf_hash:
        if isinstance(old_entry, dict):
            state["known_pdfs"][pdf_url]["hash"] = pdf_hash
            state["known_pdfs"][pdf_url]["title"] = pdf_title
        else:
            state["known_pdfs"][pdf_url] = {
                "hash": pdf_hash,
                "title": pdf_title,
            }

        save_state(state)
        print(f"Bekannte PDF aktualisiert, aber NICHT erneut gesendet: {pdf_url}")
        return

    print(f"Unverändert: {pdf_url}")


def process_fest(fest, state):
    soup = get_soup(fest["detail_url"])
    infos = extract_detail_infos(soup)

    print(
        f"Aktiv-Fest scannen: {fest['fest_name']} / "
        f"{fest['date_text']} / "
        f"{fest['location']} / "
        f"Schwinger: {infos.get('schwinger', '-')} / "
        f"Website: {infos.get('website', '-')}"
    )

    found = 0

    for link in soup.find_all("a", href=True):
        href = link["href"]
        title = clean_text(link.get_text(" ", strip=True))

        if not should_track_pdf(href, title):
            continue

        pdf_url = normalise_url(href)
        pdf_title = get_pdf_title(href, title)

        try:
            process_pdf(pdf_url, pdf_title, fest, infos, state)
            found += 1
        except Exception as exc:
            print(f"Fehler bei PDF {pdf_url}: {exc}")

        time.sleep(1)

    print(f"Relevante PDFs gefunden: {found}")


def check_ranglisten(state):
    print(f"Prüfung gestartet: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    fests = collect_active_fests()

    if not state["baseline_done"]:
        print("SICHERHEITS-BASELINE: Bestehende PDFs werden gespeichert, NICHT gesendet.")

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fertig. Ab jetzt werden nur neue PDF-URLs gesendet.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    state = load_state()

    print("Starte Bot: prüft einmal und sendet nur komplett neue PDF-URLs.")

    try:
        check_ranglisten(state)
    except Exception as exc:
        print(f"Fehler im Hauptlauf: {exc}")

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
