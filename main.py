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
MAX_DETAIL_PAGES = 200


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


def is_active_fest(soup, overview_text=""):
    text = clean_text(soup.get_text(" ", strip=True))
    combined = f"{overview_text} {text}".lower()

    if is_jung_or_nachwuchs(combined):
        return False

    if "aktiv" in combined:
        return True

    return False


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


def extract_fest_name(soup, overview_text=""):
    h1 = soup.find("h1")

    if h1:
        title = clean_text(h1.get_text(" ", strip=True))

        if title and "Eidgenössischer Schwingerverband" not in title:
            return title

    text = clean_text(soup.get_text(" ", strip=True))

    patterns = [
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Kantonales Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwingfest)",
        r"([A-ZÄÖÜa-zäöü0-9 .'\-]+Schwinget)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return clean_text(match.group(1))

    overview_text = clean_text(overview_text)
    overview_text = re.sub(r"\d{2}\.\d{2}\s*\.?\s*\d{4}", "", overview_text)
    overview_text = re.sub(r"\baktiv\b", "", overview_text, flags=re.IGNORECASE)
    overview_text = clean_text(overview_text)

    return overview_text if overview_text else "Schwingfest"


def collect_all_ranglisten_detail_links():
    soup = get_soup(RANGLISTEN_URL)

    links = []
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

        if full_url in seen:
            continue

        seen.add(full_url)

        overview_text = clean_text(link.parent.get_text(" ", strip=True))

        links.append(
            {
                "detail_url": full_url,
                "overview_text": overview_text,
                "date_text": extract_date_from_text(overview_text),
            }
        )

    print(f"Gefundene Ranglisten-Detailseiten total: {len(links)}")

    return links[:MAX_DETAIL_PAGES]


def is_main_schlussrangliste(href):
    href_lower = href.lower()

    if "/zs" in href_lower:
        return False

    if "zwischenrangliste" in href_lower:
        return False

    return href_lower.endswith("-rl.pdf") or "_rl.pdf" in href_lower


def is_main_statistik(href):
    href_lower = href.lower()

    if "/zs" in href_lower:
        return False

    if "zwischenrangliste" in href_lower:
        return False

    return href_lower.endswith("-st.pdf") or "_st.pdf" in href_lower


def should_send_pdf(href):
    return is_main_schlussrangliste(href) or is_main_statistik(href)


def get_icon(href):
    if is_main_schlussrangliste(href):
        return "🏁"

    if is_main_statistik(href):
        return "📈"

    return "📄"


def get_pdf_title(href):
    if is_main_schlussrangliste(href):
        return "Schlussrangliste"

    if is_main_statistik(href):
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

    if not is_active_fest(soup, entry["overview_text"]):
        print(f"Ignoriert, kein Aktiv-Fest: {entry['detail_url']}")
        return

    fest_name = extract_fest_name(soup, entry["overview_text"])
    date_text = entry["date_text"]

    if not date_text:
        date_text = extract_date_from_text(clean_text(soup.get_text(" ", strip=True)))

    festinfos = extract_festinfos(soup)

    print(f"Aktiv-Fest erkannt: {fest_name} / {date_text}")

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if ".pdf" not in href.lower():
            continue

        if not should_send_pdf(href):
            print(f"Ignoriert: {href}")
            continue

        pdf_url = normalise_url(href)

        if pdf_url in state["sent_pdfs"]:
            print(f"Bereits bekannt: {pdf_url}")
            continue

        if not state["baseline_done"]:
            print(f"Baseline: speichere ohne Senden: {pdf_url}")
            state["sent_pdfs"].append(pdf_url)
            save_state(state)
            continue

        icon = get_icon(href)
        pdf_title = get_pdf_title(href)

        caption = build_pdf_caption(
            icon=icon,
            pdf_title=pdf_title,
            fest_name=fest_name,
            date_text=date_text,
            festinfos=festinfos,
        )

        print(f"Sende neue PDF: {pdf_title} -> {pdf_url}")

        send_document(pdf_url, caption)

        state["sent_pdfs"].append(pdf_url)
        save_state(state)

        time.sleep(2)


def check_ranglisten(state):
    entries = collect_all_ranglisten_detail_links()

    if not state["baseline_done"]:
        print("Baseline-Modus: aktueller Stand wird nur gespeichert, nicht gesendet.")

    for entry in entries:
        try:
            print(f"Pruefe Ranglisten-Fest: {entry['detail_url']}")
            process_detail_page(entry, state)

        except Exception as exc:
            print(f"Fehler bei {entry['detail_url']}: {exc}")

    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("Baseline fertig. Ab dem nächsten Lauf werden nur neue PDFs gesendet.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN fehlt.")

    if not CHAT_ID:
        raise ValueError("CHAT_ID fehlt.")

    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY fehlt.")

    state = load_state()

    print("Starte Bot: alle Aktiv-Feste prüfen, nur neue Statistik und Schlussrangliste senden...")

    check_ranglisten(state)

    print("Botlauf beendet.")


if __name__ == "__main__":
    main()
