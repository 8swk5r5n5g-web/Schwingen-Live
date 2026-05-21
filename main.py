import os
import re
import json
import time
import hashlib
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://arls.esv.ch"
RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"

STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    return {
        "known_pdfs": {},
        "baseline_done": False
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)

    print(f"GET Versuch 1: {url} -> {response.status_code}")

    response.raise_for_status()

    return BeautifulSoup(response.text, "html.parser")


def clean_text(text):
    return " ".join(text.split()).strip()


def get_pdf_hash(pdf_url):
    try:
        response = requests.get(pdf_url, headers=HEADERS, timeout=60)

        print(f"PDF Download Versuch 1: {pdf_url} -> {response.status_code}")

        response.raise_for_status()

        return hashlib.md5(response.content).hexdigest()

    except Exception as exc:
        print(f"PDF Hash Fehler: {exc}")
        return None


def send_document(pdf_url, caption):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    response = requests.post(
        telegram_url,
        data={
            "chat_id": CHAT_ID,
            "document": pdf_url,
            "caption": caption,
            "parse_mode": "HTML",
        },
        timeout=120,
    )

    print(response.text)


def extract_latest_active_feste():
    soup = get_soup(RANGLISTEN_URL)

    rows = soup.find_all("tr")

    feste = []

    for row in rows:
        text = clean_text(row.get_text(" ", strip=True))

        if "aktiv" not in text.lower():
            continue

        link = row.find("a", href=True)

        if not link:
            continue

        href = link["href"]

        if "?anlass=" not in href:
            continue

        cols = row.find_all("td")

        if len(cols) < 4:
            continue

        datum = clean_text(cols[0].get_text())
        name = clean_text(cols[1].get_text())
        ort = clean_text(cols[3].get_text())

        feste.append({
            "datum": datum,
            "name": name,
            "ort": ort,
            "url": BASE_URL + href
        })

    print(f"Alle Aktiv-Feste gefunden: {len(feste)}")

    if not feste:
        return []

    newest_date = max(f["datum"] for f in feste)

    print(f"Neuestes Datum auf der Seite: {newest_date}")

    newest = [f for f in feste if f["datum"] == newest_date]

    print(f"Aktiv-Feste mit neuestem Datum: {len(newest)}")

    for fest in newest:
        print(
            f"Fest gefunden: "
            f"{fest['name']} / "
            f"{fest['datum']} / "
            f"{fest['ort']} / "
            f"{fest['url']}"
        )

    return newest


def extract_fest_infos(soup):
    text = clean_text(soup.get_text(" ", strip=True))

    schwinger = ""

    match = re.search(r"Anzahl Schwinger\s+(\d+)", text)

    if match:
        schwinger = match.group(1)

    website = ""

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if href.startswith("http") and "arls.esv.ch" not in href:
            website = href
            break

    return schwinger, website


def extract_relevant_pdfs(soup):
    pdfs = []

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")

        if not href.lower().endswith(".pdf"):
            continue

        title = clean_text(link.get_text(" ", strip=True))

        title_lower = title.lower()

        if "statistik" not in title_lower and "schlussrangliste" not in title_lower:
            continue

        if "zwischenrangliste" in title_lower:
            continue

        pdf_url = href

        if href.startswith("/"):
            pdf_url = BASE_URL + href

        pdfs.append({
            "title": title,
            "url": pdf_url
        })

    return pdfs


def build_caption(fest, title, schwinger, website):
    lines = [
        "🤼 <b>Neue Rangliste verfügbar</b>",
        "",
        f"🏟 <b>{fest['name']}</b>",
        f"📍 {fest['ort']}",
        f"📅 {fest['datum']}",
        f"📄 <b>{title}</b>",
    ]

    if schwinger:
        lines.append(f"👥 Schwinger: {schwinger}")

    if website:
        lines.append(f"🌐 {website}")

    return "\n".join(lines)


def process_fest(fest, state):
    soup = get_soup(fest["url"])

    schwinger, website = extract_fest_infos(soup)

    print(
        f"Aktiv-Fest scannen: "
        f"{fest['name']} / "
        f"{fest['datum']} / "
        f"{fest['ort']} / "
        f"Schwinger: {schwinger} / "
        f"Website: {website}"
    )

    pdfs = extract_relevant_pdfs(soup)

    for pdf in pdfs:
        pdf_url = pdf["url"]
        title = pdf["title"]

        pdf_hash = get_pdf_hash(pdf_url)

        if not pdf_hash:
            continue

        old_hash = state["known_pdfs"].get(pdf_url)

        if old_hash == pdf_hash:
            print(f"Unverändert: {pdf_url}")
            continue

        if not state["baseline_done"]:
            print(f"Baseline speichert PDF ohne Senden: {pdf_url}")

            state["known_pdfs"][pdf_url] = pdf_hash
            save_state(state)

            continue

        print(f"NEUES PDF erkannt: {pdf_url}")

        caption = build_caption(
            fest=fest,
            title=title,
            schwinger=schwinger,
            website=website
        )

        send_document(pdf_url, caption)

        state["known_pdfs"][pdf_url] = pdf_hash

        save_state(state)

    print(f"Relevante PDFs gefunden: {len(pdfs)}")


def check_ranglisten(state):
    feste = extract_latest_active_feste()

    if not feste:
        print("Keine Aktiv-Feste gefunden.")
        return

    if not state["baseline_done"]:
        print("ERSTER LAUF: Bestehende PDFs werden nur gespeichert, NICHT gesendet.")

    for fest in feste:
        try:
            process_fest(fest, state)

        except Exception as exc:
            print(f"Fehler bei Fest {fest['name']}: {exc}")

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)

        print("Baseline fertig. Ab jetzt werden nur neue PDFs gesendet.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    state = load_state()

    print("Starte Bot: Neue Aktiv-PDFs prüfen.")

    print(
        f"Prüfung gestartet: "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    try:
        check_ranglisten(state)

    except Exception as exc:
        print(f"Fehler im Hauptlauf: {exc}")

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
