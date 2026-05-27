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

RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
    return " ".join(text.replace("\xa0", " ").split()).strip()

def extract_fests_from_main_page():
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"❌ Fehler beim Laden der Ranglisten-Hauptseite: {e}")
        return []

    fests = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "anlass=" not in href:
            continue

        parsed_url = urlparse(href)
        anlass_id = parse_qs(parsed_url.query).get("anlass", [""])[0]
        if not anlass_id:
            continue

        fest_name = clean_text(link.get_text(" ", strip=True))
        if not fest_name or fest_name.isdigit():
            continue

        if not any(f["anlass_id"] == anlass_id for f in fests):
            detail_url = f"https://arls.esv.ch/ranglisten/?anlass={anlass_id}"
            fests.append({"anlass_id": anlass_id, "detail_url": detail_url, "fest_name": fest_name})

    return fests

def send_telegram_document(pdf_bytes, filename, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    requests.post(url, data=data, files=files, timeout=60).raise_for_status()

def process_fest(fest, state):
    try:
        soup = get_soup(fest["detail_url"])
    except Exception as e:
        print(f"❌ Fehler bei Detailseite von Fest {fest['anlass_id']}: {e}")
        return

    page_text = clean_text(soup.get_text(" ", strip=True))
    
    schwinger_match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, flags=re.IGNORECASE)
    schwinger_txt = schwinger_match.group(1) if schwinger_match else ""

    fest_website = ""
    for external_link in soup.find_all("a", href=True):
        ext_href = external_link["href"].strip()
        ext_text = external_link.get_text().lower()
        if "festobjekt" in ext_href or "festseite" in ext_text or "fest-website" in ext_text:
            fest_website = ext_href
            if not fest_website.startswith("http"):
                fest_website = requests.compat.urljoin("https://esv.ch", fest_website)

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        link_text = clean_text(link.get_text(" ", strip=True))
        combined_meta = f"{href} {link_text}".lower()

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        is_blockiert = "zwischen" in combined_meta or "startliste" in combined_meta or "einteilung" in combined_meta
        if is_blockiert:
            continue

        clean_path = href.split("?")[0].strip("/")
        storage_key = f"{fest['anlass_id']}_{clean_path}"

        if storage_key in state["known_pdfs"]:
            continue

        pdf_bytes = None
        for domain in ["https://esv.ch", "https://arls.esv.ch"]:
            try:
                pdf_url = requests.compat.urljoin(domain, href)
                res = requests.get(pdf_url, headers=HEADERS, timeout=15)
                if res.status_code == 200 and len(res.content) > 1000:
                    pdf_bytes = res.content
                    break
            except Exception:
                continue

        if not pdf_bytes:
            continue

        doc_title = link_text if link_text else "Dokument"
        emoji = "🏆" if "schluss" in doc_title.lower() else "📊"
        filename_to_send = href.split("/")[-1].split("?")[0]

        if state["baseline_done"]:
            caption = (
                f"🏟 <b>{escape(fest['fest_name'])}</b>\n"
                f"{emoji} <b>{escape(doc_title)}</b>\n"
            )
            if schwinger_txt:
                caption += f"🤼 {escape(schwinger_txt)} Aktivschwinger\n"
            if fest_website:
                caption += f"🌐 <a href='{escape(fest_website)}'>Zur Festwebseite</a>\n"

            print(f"🚀 SENDE AN TELEGRAM: {fest['fest_name']} -> {doc_title}")
            send_telegram_document(pdf_bytes, filename_to_send, caption)
        else:
            print(f"💤 Baseline-Modus speichert im Hintergrund: {doc_title}")

        state["known_pdfs"][storage_key] = hashlib.md5(pdf_bytes).hexdigest()
        save_state(state)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt in den GitHub Secrets.")

    state = load_state()
    fests = extract_fests_from_main_page()

    print(f"Anzahl überwachter Feste auf der Ranglisten-Seite: {len(fests)}")

    for fest in fests:
        process_fest(fest, state)

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("✅ Baseline erfolgreich fixiert. Ab dem nächsten Durchlauf wird scharf gesendet.")

    print("🏁 Bot-Scan erfolgreich beendet.")

if __name__ == "__main__":
    main()
