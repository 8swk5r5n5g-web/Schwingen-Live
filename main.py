import os
import json
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_URL = "https://esv.ch"
AGENDA_URL = f"{BASE_URL}/agenda/"
RANGLISTEN_URL = f"{BASE_URL}/ranglisten/"

STATE_FILE = "state.json"


def send_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    print("send_message:", response.text)
    response.raise_for_status()


def send_document(file_url: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "document": file_url,
            "caption": caption[:1024],
            "disable_web_page_preview": True,
        },
        timeout=60,
    )
    print("send_document:", response.text)
    response.raise_for_status()


def get_soup(url: str) -> BeautifulSoup:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        "Referer": "https://esv.ch/",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    session = requests.Session()
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "sent_pdfs": [],
        "sent_results": [],
        "sent_agenda_dates": [],
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalise_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return requests.compat.urljoin(BASE_URL, url)


def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    title = soup.find("title")
    if title:
        return title.get_text(" ", strip=True)
    return "Schwingfest"


def extract_page_text(soup: BeautifulSoup) -> str:
    return soup.get_text(" ", strip=True)


def find_first_number_after(label: str, text: str) -> str:
    idx = text.lower().find(label.lower())
    if idx == -1:
        return ""

    snippet = text[idx:idx + 120]
    digits = "".join(ch if ch.isdigit() else " " for ch in snippet).split()
    return digits[0] if digits else ""


def parse_result_sentence(text: str) -> str:
    if " gewinnt " not in text:
        return ""

    idx = text.find(" gewinnt ")
    start = max(0, idx - 100)
    end = min(len(text), idx + 180)
    snippet = text[start:end].strip()

    parts = snippet.split(".")
    for part in parts:
        part = part.strip()
        if " gewinnt " in part:
            return part

    return snippet


def detect_pdf_type(name_or_url: str) -> str:
    value = name_or_url.lower()

    if "schlussrangliste" in value:
        return "🏁 Schlussrangliste"
    if "zwischenrangliste" in value:
        return "📊 Zwischenrangliste"
    if "statistik" in value:
        return "📈 Statistik"
    return "📄 PDF"


def extract_external_website(soup: BeautifulSoup) -> str:
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("http") and "esv.ch" not in href:
            return href
    return ""


def get_relevant_weekend_dates(today: datetime) -> list[str]:
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    return [saturday.strftime("%d.%m.%Y"), sunday.strftime("%d.%m.%Y")]


def is_event_today_or_tomorrow() -> bool:
    soup = get_soup(AGENDA_URL)
    page_text = soup.get_text(" ", strip=True).lower()

    today = datetime.today()
    tomorrow = today + timedelta(days=1)

    for date_value in [today, tomorrow]:
        date_str = date_value.strftime("%d.%m.%Y")
        if date_str in page_text and "aktiv" in page_text:
            return True

    return False


def collect_agenda_events_for_weekend() -> list[dict]:
    soup = get_soup(AGENDA_URL)
    weekend_dates = get_relevant_weekend_dates(datetime.today())

    events = []
    seen_links = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(" ", strip=True)

        if "/agenda/" not in href:
            continue
        if "aktiv" not in text.lower():
            continue
        if not any(date_str in text for date_str in weekend_dates):
            continue

        detail_url = normalise_url(href)
        if detail_url in seen_links:
            continue
        seen_links.add(detail_url)

        try:
            detail_soup = get_soup(detail_url)
            website = extract_external_website(detail_soup)
        except Exception as exc:
            print(f"Fehler bei Agenda-Detailseite {detail_url}: {exc}")
            website = ""

        day_name = ""
        for date_str in weekend_dates:
            if date_str in text:
                dt = datetime.strptime(date_str, "%d.%m.%Y")
                day_name = "Samstag" if dt.weekday() == 5 else "Sonntag"
                break

        events.append(
            {
                "day": day_name,
                "text": text,
                "detail_url": detail_url,
                "website": website,
            }
        )

    return events


def send_weekend_agenda_if_needed(state: dict) -> None:
    today = datetime.today()

    if today.weekday() != 4:
        return

    key = today.strftime("%Y-%m-%d")
    if key in state["sent_agenda_dates"]:
        print("Agenda fuer heute bereits gesendet.")
        return

    events = collect_agenda_events_for_weekend()
    if not events:
        print("Keine Wochenend-Events gefunden.")
        return

    saturday_events = [e for e in events if e["day"] == "Samstag"]
    sunday_events = [e for e in events if e["day"] == "Sonntag"]

    lines = ["📅 Schwingfeste dieses Wochenende", ""]

    if saturday_events:
        lines.append("🟢 Samstag")
        for event in saturday_events:
            lines.append(f"• {event['text']}")
            if event["website"]:
                lines.append(f"🔗 {event['website']}")
        lines.append("")

    if sunday_events:
        lines.append("🟢 Sonntag")
        for event in sunday_events:
            lines.append(f"• {event['text']}")
            if event["website"]:
                lines.append(f"🔗 {event['website']}")
        lines.append("")

    send_message("\n".join(lines).strip())
    state["sent_agenda_dates"].append(key)
    save_state(state)


def collect_ranglisten_detail_links() -> list[str]:
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

        if full_url in seen:
            continue

        seen.add(full_url)
        links.append(full_url)

    return links


def process_detail_page(detail_url: str, state: dict) -> None:
    soup = get_soup(detail_url)
    page_text = extract_page_text(soup)
    title = extract_title(soup)

    result_text = parse_result_sentence(page_text)
    schwinger = find_first_number_after("Anzahl Schwinger", page_text)
    zuschauer = find_first_number_after("Anzahl Zuschauer", page_text)

    if result_text and detail_url not in state["sent_results"]:
        lines = [f"🏆 {title}", "", f"🥇 {result_text}"]

        if schwinger:
            lines.append("")
            lines.append(f"👥 Schwinger: {schwinger}")
        if zuschauer:
            lines.append(f"👀 Zuschauer: {zuschauer}")

        send_message("\n".join(lines))
        state["sent_results"].append(detail_url)
        save_state(state)

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if ".pdf" not in href.lower():
            continue

        pdf_url = normalise_url(href)
        if pdf_url in state["sent_pdfs"]:
            continue

        link_text = link.get_text(" ", strip=True)
        pdf_type = detect_pdf_type(link_text or pdf_url)

        caption_lines = [
            pdf_type,
            "",
            f"📍 {title}",
        ]

        if link_text:
            caption_lines.append(f"📝 {link_text}")

        send_document(pdf_url, "\n".join(caption_lines))
        state["sent_pdfs"].append(pdf_url)
        save_state(state)


def check_ranglisten(state: dict) -> None:
    detail_links = collect_ranglisten_detail_links()
    print(f"Gefundene Detailseiten: {len(detail_links)}")

    for detail_url in detail_links:
        try:
            process_detail_page(detail_url, state)
        except Exception as exc:
            print(f"Fehler bei {detail_url}: {exc}")


if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("BOT_TOKEN oder CHAT_ID fehlt.")

    state = load_state()

    try:
        if is_event_today_or_tomorrow():
            print("Hot Mode aktiv.")
            check_ranglisten(state)
        else:
            print("Kein Event heute oder morgen.")
    except Exception as exc:
        print(f"Fehler bei Event-Pruefung: {exc}")

    try:
        send_weekend_agenda_if_needed(state)
    except Exception as exc:
        print(f"Fehler bei Agenda-Post: {exc}")
