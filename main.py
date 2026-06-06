import os
import re
import json
import hashlib
from io import BytesIO
from html import escape
from urllib.parse import urlparse, parse_qs, urljoin
import requests
from bs4 import BeautifulSoup

# ── Konfiguration ─────────────────────────────────────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN")
CHAT_ID_AKTIV     = os.getenv("CHAT_ID")
CHAT_ID_NACHWUCHS = os.getenv("CHAT_ID_NACHWUCHS")

RANGLISTEN_URL = "https://arls.esv.ch/ranglisten/"
STATE_FILE     = "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Schlüsselwörter für Nachwuchs-Erkennung (Festname + PDF-Dateiname)
NACHWUCHS_WOERTER = [
    "jung", "nachwuchs", "bueb", "bube", "buben",
    "schüler", "schueler", "knaben", "espoir", "espoirs",
    "jugend", "jahrgang",
]

# ── Was soll gesendet werden? ─────────────────────────────────────────────────
#
# SENDEN:
#   ✅ Statistik nach X Gängen      → "statistik" im Text, OHNE "schluss"
#   ✅ Schlussstatistik              → "statistik" + "schluss" im Text
#   ✅ Schlussrangliste              → "schlussrangliste" im Text
#
# IGNORIEREN:
#   ❌ Zwischenrangliste nach X Gängen → "zwischenrangliste" oder "rangliste nach"
#   ❌ Startliste / Einteilung / etc.

def classify_pdf(link_text: str, href: str) -> str | None:
    """
    Gibt zurück:
      'schlussrangliste'  → Offizielle Schlussrangliste
      'schlussstatistik'  → Statistik am Ende
      'gangstatistik'     → Statistik nach einem Gang
      None                → ignorieren
    """
    text  = link_text.lower()
    fname = href.lower().split("/")[-1].split("?")[0]
    combined = f"{text} {fname}"

    # Harte Ausschlüsse zuerst
    ausschluss = [
        "startliste", "einteilung", "paarung",
        "zwischenrangliste", "rangliste nach",
    ]
    if any(a in combined for a in ausschluss):
        return None

    # Offizielle Schlussrangliste
    if "schlussrangliste" in combined:
        return "schlussrangliste"

    # Statistik — unterscheide Schluss vs. Gang
    if "statistik" in combined:
        if "schluss" in combined:
            return "schlussstatistik"
        # "nach einem gang", "nach 2 gängen" etc. → Gangstatistik
        if re.search(r"nach\s+(\d+|einem)\s+gang", combined):
            return "gangstatistik"
        # Dateiname endet auf -st.pdf → vermutlich Schlussstatistik
        if fname.endswith("-st.pdf"):
            return "schlussstatistik"
        # Generische Statistik ohne Gang-Nummer → Schlussstatistik
        return "schlussstatistik"

    return None


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except Exception:
                state = {}
    else:
        state = {}

    state.setdefault("known_pdfs", {})   # key → md5-hash
    state.setdefault("baseline_done", False)
    return state

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── HTTP ──────────────────────────────────────────────────────────────────────
def get_soup(url: str) -> BeautifulSoup:
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")

def clean_text(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split()).strip()

def fetch_pdf(href: str) -> bytes | None:
    """Lädt PDF — unterstützt absolute und relative URLs."""
    if href.startswith("http"):
        candidates = [href]
    else:
        candidates = [
            urljoin("https://arls.esv.ch", href),
            urljoin("https://esv.ch", href),
        ]
    for url in candidates:
        try:
            res = requests.get(url, headers=HEADERS, timeout=20)
            if res.status_code == 200 and len(res.content) > 500:
                print(f"    ✅ PDF OK: {url}")
                return res.content
        except Exception as e:
            print(f"    ⚠️  Fehler ({url}): {e}")
    print(f"    ❌ PDF nicht ladbar: {href}")
    return None


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_document(chat_id: str, pdf_bytes: bytes, filename: str, caption: str):
    url   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data  = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    try:
        res = requests.post(url, data=data, files=files, timeout=60)
        res.raise_for_status()
        print(f"    📨 Gesendet → {chat_id}")
    except Exception as e:
        print(f"    ❌ Telegram-Fehler: {e}")


# ── Fest-Verarbeitung ─────────────────────────────────────────────────────────
def is_nachwuchs(name: str) -> bool:
    n = name.lower()
    return any(w in n for w in NACHWUCHS_WOERTER)

def extract_fests() -> list[dict]:
    try:
        soup = get_soup(RANGLISTEN_URL)
    except Exception as e:
        print(f"❌ Hauptseite nicht ladbar: {e}")
        return []

    fests, seen = [], set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "anlass=" not in href:
            continue
        anlass_id = parse_qs(urlparse(href).query).get("anlass", [""])[0]
        if not anlass_id or anlass_id in seen:
            continue
        fest_name = clean_text(link.get_text(" ", strip=True))
        if not fest_name or fest_name.isdigit():
            continue
        seen.add(anlass_id)
        fests.append({
            "anlass_id":   anlass_id,
            "detail_url":  f"https://arls.esv.ch/ranglisten/?anlass={anlass_id}",
            "fest_name":   fest_name,
            "nachwuchs":   is_nachwuchs(fest_name),
        })

    print(f"📋 {len(fests)} Feste auf Seite gefunden.")
    return fests

def process_fest(fest: dict, state: dict):
    print(f"\n🏟️  {fest['fest_name']}  (ID {fest['anlass_id']})")

    try:
        soup = get_soup(fest["detail_url"])
    except Exception as e:
        print(f"  ❌ Detailseite: {e}")
        return

    page_text = clean_text(soup.get_text(" ", strip=True))

    datum_match = re.search(r"\d{2}\.\d{2}\.\d{4}", page_text)
    datum = datum_match.group(0) if datum_match else "–"

    schwinger_match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, re.IGNORECASE)
    schwinger_anz = schwinger_match.group(1) if schwinger_match else ""

    for link in soup.find_all("a", href=True):
        href      = link["href"].strip()
        link_text = clean_text(link.get_text(" ", strip=True))

        # Nur PDF-Links
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        pdf_typ = classify_pdf(link_text, href)
        if pdf_typ is None:
            continue  # ignorieren

        # Eindeutiger Schlüssel
        clean_path  = href.split("?")[0].strip("/")
        storage_key = f"{fest['anlass_id']}_{clean_path}"

        if storage_key in state["known_pdfs"]:
            continue  # bereits bekannt

        # PDF laden
        pdf_bytes = fetch_pdf(href)
        if not pdf_bytes:
            continue

        # Sofort in State speichern → kein Duplikat bei Absturz
        state["known_pdfs"][storage_key] = hashlib.md5(pdf_bytes).hexdigest()
        save_state(state)

        # Beim ersten Run nur Baseline bauen, nicht senden
        if not state["baseline_done"]:
            print(f"  💤 Baseline: [{pdf_typ}] {link_text or clean_path}")
            continue

        # ── Ab hier: wirklich senden ──────────────────────────────────────
        filename = href.split("/")[-1].split("?")[0]

        # Nachwuchs-Check: Festname ODER Dateiname
        nachwuchs_pdf = fest["nachwuchs"] or is_nachwuchs(filename)

        # Emoji + Titel je nach Typ
        if pdf_typ == "schlussrangliste":
            emoji = "🏆"
            titel = "Schlussrangliste"
        elif pdf_typ == "schlussstatistik":
            emoji = "📊"
            titel = "Schlussstatistik"
        else:  # gangstatistik
            emoji = "📈"
            # Gang-Nummer aus Link-Text extrahieren
            gang_match = re.search(r"nach\s+(\d+|einem)\s+gang", link_text.lower())
            gang_nr = gang_match.group(1) if gang_match else "?"
            titel = f"Statistik nach {gang_nr} Gang/Gängen"

        caption = (
            f"🏟️ <b>{escape(fest['fest_name'])}</b>\n"
            f"🗓️ {escape(datum)}\n"
            f"{emoji} <b>{escape(titel)}</b>\n"
        )
        if schwinger_anz:
            typ_label = "Nachwuchsschwinger" if nachwuchs_pdf else "Aktivschwinger"
            caption += f"🤼 {escape(schwinger_anz)} {typ_label}\n"

        # Zielkanal bestimmen
        if nachwuchs_pdf:
            if CHAT_ID_NACHWUCHS:
                print(f"  🚀 [{pdf_typ}] → Nachwuchs-Kanal")
                send_document(CHAT_ID_NACHWUCHS, pdf_bytes, filename, caption)
            else:
                print(f"  ⚠️  CHAT_ID_NACHWUCHS nicht gesetzt!")
        else:
            print(f"  🚀 [{pdf_typ}] → Aktiv-Kanal")
            send_document(CHAT_ID_AKTIV, pdf_bytes, filename, caption)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("❌ BOT_TOKEN fehlt in den GitHub Secrets.")
    if not CHAT_ID_AKTIV:
        raise ValueError("❌ CHAT_ID fehlt in den GitHub Secrets.")

    state = load_state()

    if not state["baseline_done"]:
        print("=" * 50)
        print("🔧 ERSTER LAUF — Baseline wird erstellt.")
        print("   Alle vorhandenen PDFs werden gespeichert,")
        print("   aber NICHT gesendet. Ab dem nächsten Lauf")
        print("   kommen nur neue PDFs in den Kanal.")
        print("=" * 50)

    fests = extract_fests()

    for fest in fests:
        process_fest(fest, state)

    # Baseline erst nach ALLEN Festen als fertig markieren
    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("\n✅ Baseline abgeschlossen. Bot ist ab jetzt aktiv.")

    print("\n🏁 Scan beendet.")

if __name__ == "__main__":
    main()
