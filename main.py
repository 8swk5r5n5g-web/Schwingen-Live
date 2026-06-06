import os
import re
import json
import hashlib
import time
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

NACHWUCHS_WOERTER = [
    "jung", "nachwuchs", "bueb", "bube", "buben",
    "schüler", "schueler", "knaben", "espoir", "espoirs",
    "jugend", "jahrgang",
]

# ── PDF-Klassifizierung ───────────────────────────────────────────────────────
# SENDEN:   Schlussrangliste | Schlussstatistik | Statistik nach X Gängen
# IGNORIEREN: Zwischenrangliste | Startliste | Einteilung | alles Unklare

def classify_pdf(link_text: str, href: str):
    text     = link_text.lower()
    fname    = href.lower().split("/")[-1].split("?")[0]
    combined = f"{text} {fname}"

    # Immer ignorieren
    ausschluss = [
        "startliste", "einteilung", "paarung",
        "zwischenrangliste", "rangliste nach",
    ]
    if any(a in combined for a in ausschluss):
        return None

    # Schlussrangliste
    if "schlussrangliste" in combined:
        return "schlussrangliste"

    # Schlussstatistik — nur wenn BEIDE Wörter explizit vorhanden
    if "schluss" in combined and "statistik" in combined:
        return "schlussstatistik"

    # Gangstatistik — nur wenn "nach X gang/gängen" explizit im Text
    if "statistik" in combined and re.search(r"nach\s+(\d+|einem)\s+g[aä]ng", combined):
        return "gangstatistik"

    # Alles andere: ignorieren — lieber zu wenig als Spam
    return None


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except Exception:
                state = {}
    else:
        state = {}
    state.setdefault("known_pdfs", {})
    state.setdefault("baseline_done", False)
    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── HTTP ──────────────────────────────────────────────────────────────────────
def get_soup(url):
    time.sleep(2.5)  # Rate-Limit-Schutz
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")

def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()

def fetch_pdf(href):
    if href.startswith("http"):
        candidates = [href]
    else:
        candidates = [
            urljoin("https://arls.esv.ch", href),
            urljoin("https://www.esv.ch", href),
            urljoin("https://esv.ch", href),
        ]
    for url in candidates:
        try:
            time.sleep(0.5)
            res = requests.get(url, headers=HEADERS, timeout=20)
            if res.status_code == 200 and len(res.content) > 500:
                print(f"    ✅ PDF OK: {url}")
                return res.content
            else:
                print(f"    ⚠️  HTTP {res.status_code}: {url}")
        except Exception as e:
            print(f"    ⚠️  Fehler ({url}): {e}")
    print(f"    ❌ Nicht ladbar: {href}")
    return None


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_document(chat_id, pdf_bytes, filename, caption):
    url   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data  = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    files = {"document": (filename, BytesIO(pdf_bytes), "application/pdf")}
    try:
        res = requests.post(url, data=data, files=files, timeout=60)
        res.raise_for_status()
        print(f"    📨 Gesendet → Chat {chat_id}")
    except Exception as e:
        print(f"    ❌ Telegram-Fehler: {e}")


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────
def is_nachwuchs(name):
    n = name.lower()
    return any(w in n for w in NACHWUCHS_WOERTER)

def extract_fests():
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
            "anlass_id":  anlass_id,
            "detail_url": f"https://arls.esv.ch/ranglisten/?anlass={anlass_id}",
            "fest_name":  fest_name,
            "nachwuchs":  is_nachwuchs(fest_name),
        })

    print(f"📋 {len(fests)} Feste gefunden.")
    return fests


# ── Kern-Logik ────────────────────────────────────────────────────────────────
def process_fest(fest, state):
    print(f"\n🏟️  {fest['fest_name']}  (ID {fest['anlass_id']})")

    try:
        soup = get_soup(fest["detail_url"])
    except Exception as e:
        if "429" in str(e):
            print(f"  ⏳ Rate limit — wird beim nächsten Lauf erneut versucht.")
        else:
            print(f"  ❌ Detailseite: {e}")
        return

    page_text = clean_text(soup.get_text(" ", strip=True))

    # Festname vom h1/h2 der Detailseite — zuverlässiger als Link-Text
    fest_name = fest["fest_name"]
    for tag in soup.find_all(["h1", "h2"]):
        kandidat = clean_text(tag.get_text(" ", strip=True))
        if kandidat and len(kandidat) > 10 and not re.match(r"^\d{2}\.\d{2}\.\d{4}$", kandidat):
            fest_name = kandidat
            break

    # Datum
    datum_match = re.search(r"\d{2}\.\d{2}\.\d{4}", page_text)
    datum = datum_match.group(0) if datum_match else "–"

    # Anzahl Schwinger
    schwinger_match = re.search(r"Anzahl Schwinger\s+(\d+)", page_text, re.IGNORECASE)
    schwinger_anz = schwinger_match.group(1) if schwinger_match else ""

    # PDFs durchsuchen
    for link in soup.find_all("a", href=True):
        href      = link["href"].strip()
        link_text = clean_text(link.get_text(" ", strip=True))

        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        pdf_typ = classify_pdf(link_text, href)
        if pdf_typ is None:
            continue

        # Eindeutiger Key pro Fest + Pfad
        clean_path  = href.split("?")[0].strip("/")
        storage_key = f"{fest['anlass_id']}_{clean_path}"

        if storage_key in state["known_pdfs"]:
            continue  # bereits bekannt

        # PDF laden
        pdf_bytes = fetch_pdf(href)
        if not pdf_bytes:
            continue

        # Duplikat via Inhalt-Hash — verhindert gleiche Datei unter verschiedenen Links
        pdf_hash = hashlib.md5(pdf_bytes).hexdigest()
        if pdf_hash in state["known_pdfs"].values():
            print(f"    ⏭️  Inhalt bereits bekannt (Duplikat), überspringe.")
            state["known_pdfs"][storage_key] = pdf_hash
            save_state(state)
            continue

        # Sofort speichern — verhindert Duplikate bei Absturz
        state["known_pdfs"][storage_key] = pdf_hash
        save_state(state)

        # Baseline-Lauf: nur speichern, nicht senden
        if not state["baseline_done"]:
            print(f"  💤 Baseline: [{pdf_typ}] {link_text or clean_path}")
            continue

        # ── Senden ────────────────────────────────────────────────────────────
        filename     = href.split("/")[-1].split("?")[0]
        nachwuchs_pdf = fest["nachwuchs"] or is_nachwuchs(filename)

        if pdf_typ == "schlussrangliste":
            emoji = "🏆"
            titel = "Schlussrangliste"
        elif pdf_typ == "schlussstatistik":
            emoji = "📊"
            titel = "Schlussstatistik"
        else:  # gangstatistik
            emoji = "📈"
            m = re.search(r"nach\s+(\d+|einem)\s+g[aä]ng", link_text.lower())
            if m:
                nr      = m.group(1)
                gang_nr = "1" if nr == "einem" else nr
                einheit = "Gang" if gang_nr == "1" else "Gängen"
                titel   = f"Statistik nach {gang_nr} {einheit}"
            else:
                titel = "Gangstatistik"

        caption = (
            f"🏟️ <b>{escape(fest_name)}</b>\n"
            f"🗓️ {escape(datum)}\n"
            f"{emoji} <b>{escape(titel)}</b>\n"
        )
        if schwinger_anz:
            label    = "Nachwuchsschwinger" if nachwuchs_pdf else "Aktivschwinger"
            caption += f"🤼 {escape(schwinger_anz)} {label}\n"

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
        raise ValueError("BOT_TOKEN fehlt in den GitHub Secrets.")
    if not CHAT_ID_AKTIV:
        raise ValueError("CHAT_ID fehlt in den GitHub Secrets.")

    state = load_state()

    if not state["baseline_done"]:
        print("=" * 50)
        print("🔧 ERSTER LAUF — Baseline wird erstellt.")
        print("   Alle PDFs werden gespeichert, NICHT gesendet.")
        print("   Ab dem nächsten Lauf kommen nur neue PDFs.")
        print("=" * 50)

    fests = extract_fests()
    for fest in fests:
        process_fest(fest, state)

    # Baseline erst NACH allen Festen abschliessen
    if not state["baseline_done"]:
        state["baseline_done"] = True
        save_state(state)
        print("\n✅ Baseline abgeschlossen. Bot ist jetzt aktiv.")

    print("\n🏁 Scan beendet.")

if __name__ == "__main__":
    main()
