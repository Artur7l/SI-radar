"""Narrativ-Radar (kostenlos): findet, was heute viral ist, prüft Coins dazu und bewertet das Potenzial.

Quellen (alle ohne Key): Google Trends, Wikipedia-Toplist, CoinGecko-Trend-Kategorien, DexScreener.
Bewertung: mit GEMINI_API_KEY durch KI (Gemini), sonst regelbasiert.
Optionale Secrets: GEMINI_API_KEY, DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Schreibt data.json. Der Zeitplan kommt von GitHub Actions.
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

UA = {"User-Agent": "narrativ-radar/1.0"}
NOW = time.time()
TOP_N = 15                       # so viele Themen pro Lauf bewerten
ALERT_MIN = 60                   # Alert ab diesem Potenzial (0-100)
MAX_ALERTS = 3                   # höchstens so viele Alerts pro Lauf
COOLDOWN = 24 * 3600             # gleiches Thema höchstens 1x pro Tag melden
REASSESS = 6 * 3600              # KI-Bewertung höchstens alle 6 h erneuern
GEMINI_MODEL = "gemini-2.5-flash"   # bei Fehler 404: hier den aktuellen Modellnamen eintragen


# ---------- Quellen: was ist heute viral? ----------
def google_trends():
    r = requests.get("https://trends.google.com/trending/rss?geo=US", headers=UA, timeout=20)
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        traffic = 0
        for ch in it:
            if ch.tag.endswith("approx_traffic"):
                traffic = int(re.sub(r"\D", "", ch.text or "") or 0)
        items.append((it.findtext("title") or "", traffic))
    return [t for t, _ in sorted(items, key=lambda x: -x[1]) if t]


def wikipedia():
    d = datetime.now(timezone.utc) - timedelta(days=1)
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
           f"en.wikipedia/all-access/{d:%Y/%m/%d}")
    arts = requests.get(url, headers=UA, timeout=20).json()["items"][0]["articles"]
    out = []
    for a in arts:
        t = a["article"].replace("_", " ")
        if t == "Main Page" or ":" in t or t.startswith("Deaths in"):
            continue
        out.append(t)
    return out[:25]


def coingecko_categories():
    r = requests.get("https://api.coingecko.com/api/v3/search/trending", headers=UA, timeout=20)
    return [c["name"] for c in r.json().get("categories", [])]


def collect():
    merged = {}
    for name, fn in (("Google Trends", google_trends), ("Wikipedia", wikipedia),
                     ("CoinGecko", coingecko_categories)):
        try:
            terms = fn()
        except Exception as e:
            print("Quelle ausgefallen:", name, type(e).__name__)
            continue
        for i, t in enumerate(terms):
            m = merged.setdefault(t.lower().strip(),
                                  {"term": t.strip(), "sources": [], "strength": 0.0})
            m["sources"].append(name)
            m["strength"] += 100 * (1 - i / len(terms))   # Rang in der Quelle, Mehrfachnennung addiert
    items = sorted(merged.values(), key=lambda m: -m["strength"])[:TOP_N]
    for m in items:
        m["strength"] = round(min(100, m["strength"]))
    return items


# ---------- Coins zum Thema ----------
def coin_stats(term):
    k = term.lower()
    r = requests.get("https://api.dexscreener.com/latest/dex/search",
                     params={"q": term}, headers=UA, timeout=20)
    pairs = [p for p in (r.json().get("pairs") or [])
             if k in (p["baseToken"]["name"] + " " + p["baseToken"]["symbol"]).lower()]
    if not pairs:
        return {"count": 0}
    best = max(pairs, key=lambda p: (p.get("volume") or {}).get("h24") or 0)
    young = [p for p in pairs if NOW - (p.get("pairCreatedAt") or 0) / 1000 < 48 * 3600]
    return {
        "count": len({p["baseToken"]["address"] for p in pairs}),
        "new48h": len({p["baseToken"]["address"] for p in young}),
        "top": best["baseToken"]["symbol"],
        "top_vol24h": round((best.get("volume") or {}).get("h24") or 0),
        "top_fdv": round(best.get("fdv") or 0),
        "top_liq": round((best.get("liquidity") or {}).get("usd") or 0),
        "top_chg24h": (best.get("priceChange") or {}).get("h24"),
    }


# ---------- Bewertung ----------
def verdict(p):
    return "Hoch" if p >= 65 else "Mittel" if p >= 40 else "Niedrig"


def rule_score(it):
    c = it["coins"]
    p = 0.6 * it["strength"]
    if c["count"] == 0:
        p *= 0.5                                   # Aufmerksamkeit da, aber nichts bewiesen
    else:
        p += min(25, c["top_vol24h"] / 4000)       # echtes Handelsvolumen
        p *= 1 if c["count"] <= 3 else 0.75 if c["count"] <= 10 else 0.5   # Sättigung
        if c["top_liq"] < 10000:
            p *= 0.6                               # kaum Liquidität
    return int(max(0, min(100, p)))


def gemini_assess(items):
    key = os.getenv("GEMINI_API_KEY")
    if not key or not items:
        return {}
    data = [{"term": i["term"], "quellen": i["sources"], "staerke": i["strength"],
             "coins": i["coins"]} for i in items]
    prompt = (
        "Du bewertest Krypto-Narrative. Für jedes Thema: Wie viel Potenzial hat ein Coin zu diesem "
        "viralen Thema in den nächsten Tagen? Beachte: echter Krypto-Bezug, wie lange der Hype "
        "voraussichtlich hält, Anzahl schon existierender Coins (Sättigung), Liquidität und Volumen. "
        "Die meisten Memecoins verlieren Geld: sei kritisch, kein Hype. Themen ohne Krypto-Bezug "
        "bekommen niedrige Werte. Antworte NUR mit einer JSON-Liste: "
        '[{"term": str, "potential": 0-100, "verdict": "Hoch|Mittel|Niedrig", '
        '"reason": "max 2 Sätze, Deutsch", "risk": "1 Satz, Deutsch"}]. Daten: '
        + json.dumps(data, ensure_ascii=False))
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}},
        timeout=60)
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return {a["term"]: a for a in json.loads(txt)}


# ---------- Alerts ----------
def send(text):
    tg, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg and chat:
        requests.post(f"https://api.telegram.org/bot{tg}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    if os.getenv("DISCORD_WEBHOOK_URL"):
        requests.post(os.environ["DISCORD_WEBHOOK_URL"], json={"content": text}, timeout=20)


def main():
    try:
        old = json.load(open("data.json"))
    except Exception:
        old = {}
    cache = {t["term"].lower(): t for t in old.get("trends", [])}
    last = old.get("last_alert", {})

    items = collect()
    for it in items:
        try:
            it["coins"] = coin_stats(it["term"])
        except Exception as e:
            print("Coin-Check fehlgeschlagen:", it["term"], type(e).__name__)
            it["coins"] = {"count": 0}

    todo = []
    for it in items:
        c = cache.get(it["term"].lower())
        if c and c.get("mode") == "KI" and NOW - c.get("assessed_at", 0) < REASSESS:
            for k in ("potential", "verdict", "reason", "risk", "mode", "assessed_at"):
                it[k] = c[k]
        else:
            todo.append(it)
    try:
        ai = gemini_assess(todo)
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", "")
        print("Gemini fehlgeschlagen:", type(e).__name__, code)
        ai = {}
    for it in todo:
        a = ai.get(it["term"])
        if a:
            it.update(potential=int(a["potential"]), verdict=a["verdict"], reason=a["reason"],
                      risk=a["risk"], mode="KI", assessed_at=NOW)
        else:
            p = rule_score(it)
            it.update(potential=p, verdict=verdict(p), mode="Regeln", assessed_at=NOW,
                      reason="Regelbasierte Schätzung aus Trendstärke, vorhandenen Coins und Volumen.",
                      risk="Memecoins verlieren oft schnell an Wert.")

    items.sort(key=lambda x: -x["potential"])
    if "trends" in old:    # beim allerersten Lauf nichts senden
        new = [i for i in items if i["potential"] >= ALERT_MIN
               and NOW - last.get(i["term"].lower(), 0) > COOLDOWN]
        for it in new[:MAX_ALERTS]:
            last[it["term"].lower()] = NOW
            c = it["coins"]
            send(f"Narrativ-Radar: '{it['term']}' ist viral ({', '.join(it['sources'])}).\n"
                 f"Potenzial: {it['potential']}/100 ({it['verdict']}, {it['mode']}).\n"
                 f"Coins dazu: {c['count']} (neu in 48 h: {c.get('new48h', 0)}).\n"
                 f"{it['reason']}\nRisiko: {it['risk']}\nKeine Anlageberatung.")

    json.dump({"updated": NOW, "trends": items, "last_alert": last},
              open("data.json", "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
