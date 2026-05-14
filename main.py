import os
import re
import json
import time
from html import escape
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BASE_URL = "https://esv.ch"
RANGLISTEN_URL = "https://esv.ch/ranglisten/"
SCRAPER_API_BASE = "https://api.scraperapi.com"

STATE_FILE = "state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    if "sent_pdfs" not in state:
        state["sent_pdfs"] = []

    if "baseline_done" not in state:
        state["baseline_done"] = False

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


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
        timeout=90,
        retries=3,
    )


def scraper_url(target_url):
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "false",
    }

    return f"{SCRAPER_API_BASE}?{urlencode(params)}"


def get_soup(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(scraper_url(url), timeout=90)

            print(f"GET via ScraperAPI Versuch {attempt}: {url} -> {response.status_code}")

            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")

        except requests.exceptions.ReadTimeout:
            wait_time = attempt * 10
            print(f"ScraperAPI Timeout bei Versuch {attempt}. Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

        except requests.exceptions.RequestException as exc:
            wait_time = attempt * 10
            print(f"ScraperAPI Fehler bei Versuch {attempt}: {exc}")
            print(f"Warte {wait_time} Sekunden...")
            time.sleep(wait_time)

    raise RuntimeError(f"ScraperAPI konnte Seite nach {retries} Versuchen nicht laden: {url}")


def normalise_url(url):
    if url.startswith("http"):
        return url

    return requests.compat.urljoin(BASE_URL, url)


def clean_text(text):
    return " ".join(text.split()).strip()


def extract_date_from_text(text):
    match = re.search(r"(\d{2}\.\d{2}\s*\.?\s*\d{4})", text)

    if not match:
        return ""

    return clean_text(match.group(1).replace(" ", ""))


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


def is_aktiv_text(text):
    text = text.lower()
    return "aktiv" in text and not is_jung_or_nachwuchs(text)


def extract_number_after(label, text):
    idx = text.lower().find(label.lower())

    if idx == -1:
        return ""

    snippet = text[idx:idx + 100]
    match = re.search(r"\d+", snippet)

    return match.group(0) if match else ""


def extract_website(soup):
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()

        if href.startswith("http") and "esv.ch" not in href:
            return href

    return ""


def extract_festinfos(soup):
    text = clean_text(soup.get_text(" ", strip=True))

    schwinger = extract_number_after("Anzahl Schwinger", text)
    zuschauer = extract_number_after("Anzahl Zuschauer", text)
    website = extract_website(soup)

    lines = []

    if schwinger:
        lines.append(f"🤼 Anzahl Schwinger: {schwinger}")

    if zuschauer:
        lines.append(f"👥 Anzahl Zuschauer: {zuschauer}")

    if website:
        lines.append(f"🌐 Website: {website}")

    return lines


def extract_fest_name_from_text(text):
    text = clean_text(text)

    text = re.sub(r"\d{2}\.\d{2}\s*\.?\s*\d{4}", "", text)
    text = re.sub(r"\baktiv\b", "", text, flags=re.IGNORECASE)
    text = clean_text(text)

    patterns = [
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Kantonales Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwinget)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return clean_text(match.group(1))

    return text if text else "Schwingfest"


def collect_active_ranglisten_entries():
    soup = get_soup(RANGLISTEN_URL)

    entries = []
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if "/ranglisten/" not in href:
            continue

        full_url = normalise_url(href)

        if full_url.rstrip("/") == RANGLISTEN_URL.rstrip("/"):
            continue

        if "?jahr=" in full_url:
            continue

        parent = link.parent
        parent_text = clean_text(parent.get_text(" ", strip=True))

        if not is_aktiv_text(parent_text):
            continue

        if full_url in seen:
            continue

        seen.add(full_url)

        event_date = extract_date_from_text(parent_text)
        fest_name = extract_fest_name_from_text(parent_text)

        entries.append(
            {
                "detail_url": full_url,
                "fest_name": fest_name,
                "date_text": event_date,
            }
        )

    print(f"Aktiv-Ranglisten gefunden: {len(entries)}")

    for entry in entries:
        print(f"{entry['date_text']} | {entry['fest_name']} | {entry['detail_url']}")

    return entries


def is_schlussrangliste(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    return (
        "schlussrangliste" in text
        or "-rl.pdf" in text
        or "_rl.pdf" in text
    )


def is_statistik_1_to_6(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    if "statistik" not in text and "-st" not in text and "_st" not in text:
        return False

    patterns = [
        r"statistik nach einem gang",
        r"statistik nach 1 gang",
        r"statistik[_\- ]?1",
        r"statistik nach 2 g",
        r"statistik[_\- ]?2",
        r"statistik nach 3 g",
        r"statistik[_\- ]?3",
        r"statistik nach 4 g",
        r"statistik[_\- ]?4",
        r"statistik nach 5 g",
        r"statistik[_\- ]?5",
        r"statistik nach 6 g",
        r"statistik[_\- ]?6",
        r"[-_]st\.pdf",
        r"[-_]st\d",
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def should_send_pdf(link_text, href):
    text = f"{link_text} {href}".lower()

    if "zwischenrangliste" in text:
        return False

    if is_statistik_1_to_6(link_text, href):
        return True

    if is_schlussrangliste(link_text, href):
        return True

    return False


def get_icon(link_text, href):
    if is_schlussrangliste(link_text, href):
        return "🏁"

    if is_statistik_1_to_6(link_text, href):
        return "📈"

    return "📄"


def get_pdf_title(link_text, href):
    title = clean_text(link_text)
    filename = href.split("/")[-1].replace(".pdf", "")
    filename_lower = filename.lower()

    if title:
        return title

    if is_schlussrangliste(link_text, href):
        return "Schlussrangliste"

    if "statistik_1" in filename_lower or "st1" in filename_lower:
        return "Statistik nach einem Gang"

    if "statistik_2" in filename_lower or "st2" in filename_lower:
        return "Statistik nach 2 Gängen"

    if "statistik_3" in filename_lower or "st3" in filename_lower:
        return "Statistik nach 3 Gängen"

    if "statistik_4" in filename_lower or "st4" in filename_lower:
        return "Statistik nach 4 Gängen"

    if "statistik_5" in filename_lower or "st5" in filename_lower:
        return "Statistik nach 5 Gängen"

    if "statistik_6" in filename_lower or "st6" in filename_lower:
        return "Statistik nach 6 Gängen"

    if is_statistik_1_to_6(link_text, href):
        return "Statistik"

    return "PDF"


def build_pdf_caption(icon, pdf_title, fest_name, date_text, festinfos):
    lines = [
        f"{icon} <b>{escape(pdf_title)}</b>",
        "",
        f"🏟 <b>{escape(fest_name)}</b>",
    ]

    if date_text:
        lines.append(f"🗓 {escape(date_text)}")

    if festinfos:
        lines.append("")
        lines.append("ℹ️ <b>Festinfos</b>")

        for info in festinfos:
            lines.append(escape(info))

    return "\n".join(lines)


def process_detail_page(entry, state):
    soup = get_soup(entry["detail_url"])

    festinfos = extract_festinfos(soup)

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = clean_text(link.get_text(" ", strip=True))

        if ".pdf" not in href.lower():
            continue

        if not should_send_pdf(link_text, href):
            print(f"Ignoriert: {link_text} / {href}")
            continue

        pdf_url = normalise_url(href)

        if pdf_url in state["sent_pdfs"]:
            print(f"Bereits bekannt: {pdf_url}")
            continue

        if not state["baseline_done"]:
            print(f"Baseline: speichere vorhandene PDF ohne Senden: {pdf_url}")
            state["sent_pdfs"].append(pdf_url)
            save_state(state)
            continue

        icon = get_icon(link_text, href)
        pdf_title = get_pdf_title(link_text, href)

        caption = build_pdf_caption(
            icon=icon,
            pdf_title=pdf_title,
            fest_name=entry["fest_name"],
            date_text=entry["date_text"],
            festinfos=festinfos,
        )

        print(f"Sende neue Aktiv-PDF: {pdf_title} -> {pdf_url}")

        send_document(pdf_url, caption)

        state["sent_pdfs"].append(pdf_url)
        save_state(state)

        time.sleep(2)


def check_ranglisten(state):
    entries = collect_active_ranglisten_entries()

    if not state["baseline_done"]:
        print("Baseline-Modus aktiv: vorhandene Aktiv-PDFs werden nur gespeichert, nicht gesendet.")

    for entry in entries:
        try:
            print(f"Pruefe Aktiv-Fest: {entry['fest_name']} / {entry['detail_url']}")
            process_detail_page(entry, state)

        except Exception as exc:
            print(f"Fehler bei {entry['detail_url']}: {exc}")

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline abgeschlossen. Ab dem nächsten Lauf werden nur neue PDFs gesendet.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    state = load_state()

    print("Starte Live-Bot für neue Aktiv-Ranglisten und Statistiken...")

    check_ranglisten(state)

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
