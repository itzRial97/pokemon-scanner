#!/usr/bin/env python3
"""
Pokémon-Karten-Scanner v2: findet günstige Karten mit Wertsteigerungs-Potenzial.
Quelle: pokemontcg.io (Cardmarket-EUR + TCGplayer-USD).

Installation:  pip install requests
Start:         python pokemon_scanner.py
Optional (Umgebungsvariablen):
  POKEMON_TCG_API_KEY   kostenloser Key von dev.pokemontcg.io (höhere Limits)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   -> Top-Liste täglich aufs Handy

Täglich automatisch:
  Windows:  Aufgabenplanung -> täglich -> "python C:\\Pfad\\pokemon_scanner.py"
  Mac/Linux: crontab -e ->  0 8 * * * cd /pfad && python3 pokemon_scanner.py

WICHTIG: Keine Anlageberatung. Preise können auch fallen.
"""
import os, sqlite3, csv, sys, time, html as _html
from datetime import date
from urllib.parse import quote_plus
import requests

# ---------------- Einstellungen ----------------
MIN_PREIS = 1.0
MAX_PREIS = 40.0
TOP_N = 50
DB_DATEI = "pokemon_preise.db"
REPORT_DATEI = f"report_{date.today()}.csv"
HTML_DATEI = f"report_{date.today()}.html"
JAHR_JETZT = date.today().year

BELIEBT = {"charizard", "pikachu", "mewtwo", "mew", "umbreon", "espeon",
           "gengar", "eevee", "rayquaza", "lugia", "blastoise", "venusaur",
           "gyarados", "dragonite", "lucario", "greninja", "sylveon",
           "charmander", "snorlax", "arcanine", "gardevoir", "mimikyu",
           "giratina", "garchomp", "leafeon", "glaceon", "vaporeon",
           "jolteon", "flareon", "zoroark", "ho-oh", "suicune", "raikou"}

RARITAET_PUNKTE = {
    "Rare Holo": 1, "Rare Holo EX": 2, "Rare Holo GX": 2, "Rare Holo V": 2,
    "Rare Holo VMAX": 2, "Rare Holo VSTAR": 2, "Rare Ultra": 2,
    "Rare Secret": 3, "Illustration Rare": 3, "Special Illustration Rare": 4,
    "Hyper Rare": 3, "Rare Rainbow": 3, "Amazing Rare": 2, "Rare Shiny": 2,
    "Rare Holo LV.X": 2, "Rare Prime": 2, "LEGEND": 2, "Rare BREAK": 2,
}
API = "https://api.pokemontcg.io/v2/cards"
# -----------------------------------------------


def hole_karten():
    headers = {}
    if os.getenv("POKEMON_TCG_API_KEY"):
        headers["X-Api-Key"] = os.environ["POKEMON_TCG_API_KEY"]
    seite = 1
    while True:
        params = {"page": seite, "pageSize": 250,
                  "q": f"cardmarket.prices.trendPrice:[{MIN_PREIS} TO {MAX_PREIS}]",
                  "select": "id,name,rarity,number,set,cardmarket,tcgplayer,images"}
        r = None
        for versuch in range(4):
            try:
                r = requests.get(API, params=params, headers=headers, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"Fehler Seite {seite} (Versuch {versuch+1}): {e}")
                time.sleep(5 * (versuch + 1))
                r = None
        if r is None:
            return
        daten = r.json().get("data", [])
        if not daten:
            return
        yield from daten
        if len(daten) < 250:
            return
        seite += 1


def init_db():
    db = sqlite3.connect(DB_DATEI)
    db.execute("""CREATE TABLE IF NOT EXISTS preise (
        tag TEXT, id TEXT, name TEXT, trend REAL, PRIMARY KEY (tag, id))""")
    try:
        db.execute("ALTER TABLE preise ADD COLUMN avg30 REAL")
    except sqlite3.OperationalError:
        pass  # Spalte existiert schon
    db.execute("""CREATE TABLE IF NOT EXISTS tops (tag TEXT, id TEXT, PRIMARY KEY (tag, id))""")
    return db


def eigene_historie(db, karten_id):
    def preis_vor(tage):
        row = db.execute(
            "SELECT trend FROM preise WHERE id=? AND tag<=date('now', ?) "
            "ORDER BY tag DESC LIMIT 1", (karten_id, f"-{tage} day")).fetchone()
        return row[0] if row else None
    return preis_vor(7), preis_vor(30)


def _tcg_preis(k):
    prices = (k.get("tcgplayer") or {}).get("prices") or {}
    for variante in prices.values():
        if variante.get("market"):
            return variante["market"]
    return None


def fmt(v, w="EUR"):
    return f"{v:.2f} {w}" if v else "-"


def bewerte(k, db):
    cm = (k.get("cardmarket") or {}).get("prices") or {}
    trend, avg30, avg7, avg1, low = (cm.get("trendPrice"), cm.get("avg30"),
                                     cm.get("avg7"), cm.get("avg1"),
                                     cm.get("lowPrice"))
    if not trend or not (MIN_PREIS <= trend <= MAX_PREIS):
        return None

    punkte, gruende = 0.0, []

    # 1) Dip unter 30-Tage-Schnitt = Einstiegschance
    if avg30 and trend < avg30:
        dip = (avg30 - trend) / avg30
        punkte += min(dip * 40, 15)
        gruende.append(f"{dip:.0%} unter 30-Tage-Schnitt")

    # 2) Liquider Markt: günstigstes Angebot nahe am Trend
    if low and low / trend > 0.85:
        punkte += 1
    elif low and low / trend < 0.5:
        punkte -= 2; gruende.append("Ausreißer-Angebot (unsicher)")

    # 3) Beliebtes Pokémon
    if any(n in k["name"].lower() for n in BELIEBT):
        punkte += 4; gruende.append("beliebtes Pokémon")

    # 4) Seltenheit
    rp = RARITAET_PUNKTE.get(k.get("rarity", ""), 0)
    if rp:
        punkte += rp * 1.5; gruende.append(k["rarity"])

    # 5) Set-Alter: Vintage + Sweet-Spot (Sets, die 2-6 Jahre alt / ausverkauft sind)
    jahr = int((k["set"].get("releaseDate") or "2099")[:4])
    alter = JAHR_JETZT - jahr
    if jahr <= 2005:
        punkte += 5; gruende.append("Vintage-Set (<=2005)")
    elif jahr <= 2015:
        punkte += 2; gruende.append("älteres Set")
    elif 2 <= alter <= 6:
        punkte += 1.5; gruende.append("Set nicht mehr im Druck (Sweet-Spot)")

    # 6) Erholung beginnt: kurzfristig steigt, 30-Tage-Schnitt höher
    if avg7 and avg30 and avg7 > trend * 0.98 and avg30 > trend:
        punkte += 1.5; gruende.append("Erholung beginnt")

    # 7) Fallendes Messer: alles fällt weiter -> Abzug
    if avg1 and avg7 and avg30 and avg1 < avg7 < avg30 and avg1 < avg30 * 0.85:
        punkte -= 3; gruende.append("Preis fällt noch (Vorsicht)")

    # 8) Eigene Historie
    p7, p30 = eigene_historie(db, k["id"])
    if p30 and trend > p30 * 1.05:
        punkte += 2; gruende.append("Trend steigend (eigene Daten)")
    if p7 and trend < p7 * 0.9:
        punkte -= 1.5; gruende.append("Letzte 7 Tage stark gefallen")

    return {
        "score": round(punkte, 1), "name": k["name"],
        "set": k["set"]["name"], "nr": k.get("number"),
        "raritaet": k.get("rarity", ""), "trend_eur": trend,
        "avg30_eur": avg30, "avg7_eur": avg7, "low_eur": low,
        "gruende": "; ".join(gruende),
        "tcg_usd": _tcg_preis(k),
        "bild": (k.get("images") or {}).get("small", ""),
        "bild_gross": (k.get("images") or {}).get("large", ""),
        "link": (k.get("cardmarket") or {}).get("url", ""),
        "link_tcgplayer": (k.get("tcgplayer") or {}).get("url", ""),
        "link_ebay": "https://www.ebay.de/sch/i.html?_nkw=" + quote_plus(
            f"pokemon {k['name']} {k['set']['name']} {k.get('number','')}"),
        "id": k["id"], "neu": False,
    }


def schreibe_html(top):
    esc = _html.escape
    karten = ""
    for e in top:
        neu = '<span class="n">NEU</span> ' if e["neu"] else ""
        karten += f"""
        <div class="k">
          <a href="{e['bild_gross']}" target="_blank"><img src="{e['bild']}" loading="lazy"></a>
          <h3>{neu}{esc(e['name'])}</h3>
          <div class="s">{esc(e['set'])} #{esc(str(e['nr']))} &middot; {esc(e['raritaet'])}</div>
          <div class="p">{fmt(e['trend_eur'])}</div>
          <table>
            <tr><td>Günstigstes Angebot</td><td>{fmt(e['low_eur'])}</td></tr>
            <tr><td>7-Tage-Schnitt</td><td>{fmt(e['avg7_eur'])}</td></tr>
            <tr><td>30-Tage-Schnitt</td><td>{fmt(e['avg30_eur'])}</td></tr>
            <tr><td>TCGplayer</td><td>{fmt(e['tcg_usd'], 'USD')}</td></tr>
          </table>
          <div class="g">Score {e['score']}: {esc(e['gruende'])}</div>
          <div class="b">
            <a href="{e['link']}" target="_blank">Cardmarket</a>
            <a href="{e['link_ebay']}" target="_blank">eBay</a>
            <a href="{e['link_tcgplayer']}" target="_blank">TCGplayer</a>
          </div>
        </div>"""
    html = f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pokémon Karten {date.today()}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f3f3f5;margin:0;padding:12px;color:#222}}
h1{{font-size:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.k{{background:#fff;border-radius:12px;padding:12px;box-shadow:0 1px 3px #0002}}
img{{width:100%;border-radius:8px}}
h3{{margin:8px 0 2px;font-size:16px}}
.n{{background:#e8590c;color:#fff;font-size:10px;padding:2px 6px;border-radius:6px;vertical-align:middle}}
.s{{font-size:12px;color:#666}}
.p{{font-size:22px;font-weight:700;color:#0a7d33;margin:6px 0}}
table{{width:100%;font-size:12px;border-collapse:collapse}}
td{{padding:2px 0}} td:last-child{{text-align:right}}
.g{{font-size:12px;margin-top:6px;color:#444}}
.b{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}.b a{{font-size:12px;padding:6px 10px;border-radius:8px;background:#0a7d33;color:#fff;text-decoration:none}}.b a:nth-child(2){{background:#1a5fd0}}.b a:nth-child(3){{background:#777}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}.k{{background:#1d1d20}}.s,.g{{color:#aaa}}}}
</style></head><body>
<h1>Günstige Pokémon-Karten mit Potenzial &ndash; {date.today()}</h1>
<div class="grid">{karten}</div></body></html>"""
    for datei in (HTML_DATEI, "index.html"):  # index.html = immer der neueste Report
        with open(datei, "w", encoding="utf-8") as f:
            f.write(html)


def sende_telegram(top):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat) or not top:
        return
    zeilen = [f"Pokémon-Tipps {date.today()}"]
    for e in top[:10]:
        neu = "NEU " if e["neu"] else ""
        zeilen.append(f"{neu}{e['name']} ({e['set']}) {e['trend_eur']:.2f} EUR "
                      f"[Score {e['score']}]\n{e['link']}")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": "\n\n".join(zeilen)[:4000],
                            "disable_web_page_preview": "true"}, timeout=30)
    except requests.RequestException as ex:
        print(f"Telegram-Fehler: {ex}")


def main():
    db = init_db()
    heute = str(date.today())
    ergebnisse, n = [], 0
    for k in hole_karten():
        n += 1
        cm = (k.get("cardmarket") or {}).get("prices") or {}
        if cm.get("trendPrice"):
            db.execute("INSERT OR REPLACE INTO preise (tag,id,name,trend,avg30) VALUES (?,?,?,?,?)",
                       (heute, k["id"], k["name"], cm["trendPrice"], cm.get("avg30")))
        e = bewerte(k, db)
        if e and e["score"] > 0:
            ergebnisse.append(e)
    db.commit()

    if n == 0:
        print("Keine Daten erhalten (API/Internet prüfen).")
        return 1

    ergebnisse.sort(key=lambda x: x["score"], reverse=True)
    top = ergebnisse[:TOP_N]

    # Neu in der Top-Liste seit dem letzten Lauf?
    gestern = {r[0] for r in db.execute(
        "SELECT id FROM tops WHERE tag=(SELECT MAX(tag) FROM tops WHERE tag<?)", (heute,))}
    for e in top:
        e["neu"] = bool(gestern) and e["id"] not in gestern
    db.execute("DELETE FROM tops WHERE tag=?", (heute,))
    db.executemany("INSERT INTO tops VALUES (?,?)", [(heute, e["id"]) for e in top])
    db.commit()

    if top:
        with open(REPORT_DATEI, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=top[0].keys(), delimiter=";")
            w.writeheader(); w.writerows(top)
        schreibe_html(top)
        sende_telegram(top)

    print(f"{n} Karten geprüft. Top {min(10, len(top))} heute:\n")
    for e in top[:10]:
        neu = "NEU " if e["neu"] else ""
        print(f"[{e['score']:>5}] {neu}{e['name']} ({e['set']} #{e['nr']}) "
              f"- {e['trend_eur']:.2f} EUR | {e['gruende']}")
    print(f"\nCSV-Report: {REPORT_DATEI}")
    print(f"Bilder + Preise: {HTML_DATEI} (im Browser öffnen)")


if __name__ == "__main__":
    sys.exit(main())
