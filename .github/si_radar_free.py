"""SI-Radar (kostenlos): Google News, Hacker News, DexScreener, CoinGecko. Kein API-Key nötig.

Schreibt data.json (für die Website) und schickt Alerts an Telegram/Discord, falls gesetzt:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL  (alle optional)
Einmal ausführen: python si_radar_free.py   (der Zeitplan kommt von GitHub Actions)
"""
import json
import os
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

TERMS = ["White House Accord", "Super Intelligence", "Frontier SI",
         "SI race", "AI self-regulation"]
COIN_WORDS = ["superintelligence", "super intelligence", "si accord", "asi"]
FACTOR, MIN_RECENT, COOLDOWN = 3.0, 4, 6 * 3600
NOW = time.time()


def news_counts(term):
    """(letzte 3h, letzte 24h) Artikel bei Google News."""
    url = f"https://news.google.com/rss/search?q={quote(term)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    root = ET.fromstring(requests.get(url, timeout=20).content)
    ts = [parsedate_to_datetime(i.findtext("pubDate")).timestamp() for i in root.iter("item")]
    return sum(NOW - t < 3 * 3600 for t in ts), len(ts)


def hn_counts(term):
    def hits(hours):
        r = requests.get("https://hn.algolia.com/api/v1/search_by_date", timeout=20, params={
            "query": term, "tags": "story",
            "numericFilters": f"created_at_i>{int(NOW - hours * 3600)}"})
        return r.json().get("nbHits", 0)
    return hits(3), hits(24)


def new_coins():
    """Neue Coins der letzten 24h mit SI-Namen (DexScreener) + CoinGecko-Trends."""
    found = {}
    for w in COIN_WORDS[:3]:
        r = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": w}, timeout=20)
        for p in r.json().get("pairs") or []:
            created = (p.get("pairCreatedAt") or 0) / 1000
            name = p["baseToken"]["name"].lower()
            if NOW - created < 24 * 3600 and ("super" in name or "accord" in name):
                found[p["baseToken"]["symbol"]] = p["baseToken"]["name"]
    trend = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=20).json()
    for c in trend.get("coins", []):
        n = c["item"]["name"].lower()
        if any(w in n for w in COIN_WORDS):
            found[c["item"]["symbol"]] = c["item"]["name"] + " (CoinGecko Trend)"
    return found


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
    last = old.get("last_alert", {})
    rows = []
    for term in TERMS:
        try:
            n3, n24 = news_counts(term)
            h3, h24 = hn_counts(term)
        except Exception as e:
            print("Fehler", term, e)
            continue
        recent, day = n3 + h3, n24 + h24
        ratio = recent / max(day / 8, 1)   # 3h-Anteil gegen 24h-Durchschnitt
        rows.append({"term": term, "recent": recent, "day": day, "ratio": round(ratio, 1)})
        if recent >= MIN_RECENT and ratio >= FACTOR and NOW - last.get(term, 0) > COOLDOWN:
            last[term] = NOW
            send(f"SI-Radar: '{term}' steigt. {recent} neue Meldungen in 3 h "
                 f"({ratio:.1f}x normal). Quellen: Google News, Hacker News.")
    coins = {}
    try:
        coins = new_coins()
        fresh = set(coins) - set(old.get("coins", {}))
        if fresh:
            send("SI-Radar: neue Coins mit SI-Namen: " + ", ".join(f"{coins[s]} (${s})" for s in fresh))
    except Exception as e:
        print("Fehler Coins", e)
    rows.sort(key=lambda r: r["ratio"], reverse=True)
    json.dump({"updated": NOW, "terms": rows, "coins": coins, "last_alert": last},
              open("data.json", "w"), indent=1)


if __name__ == "__main__":
    main()
