#!/usr/bin/env python3
"""
בודק מחירים חריגים ושולח התראה לטלגרם.
משתמש ב-SerpAPI (Google Flights).
"""

import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

if not all([TOKEN, CHAT_ID, SERPAPI_KEY]):
    raise SystemExit("חסרים משתני סביבה. בדוק .env או GitHub Secrets.")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
SERP_URL = "https://serpapi.com/search"

def load_config():
    with open(Path(__file__).parent / "routes_config.json", encoding="utf-8") as f:
        return json.load(f)

def send_telegram(text: str):
    r = requests.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print("Telegram error:", r.text)
    return r.ok

def search_flights(origin, destination, depart_date, return_date=None):
    """חיפוש אחד ב-SerpAPI Google Flights"""
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": depart_date,
        "currency": "USD",
        "hl": "en",
        "api_key": SERPAPI_KEY,
    }
    if return_date:
        params["return_date"] = return_date
        params["type"] = "2"  # round trip
    else:
        params["type"] = "1"  # one way

    try:
        r = requests.get(SERP_URL, params=params, timeout=30)
        data = r.json()
        if "error" in data:
            print(f"SerpAPI error: {data['error']}")
            return None
        return data
    except Exception as e:
        print(f"Request failed: {e}")
        return None

def parse_best_option(data, max_hours=20):
    """מחזיר את האפשרות הזולה ביותר שעומדת בזמן"""
    if not data or "best_flights" not in data:
        return None

    for flight in data["best_flights"]:
        duration_min = flight.get("total_duration", 9999)
        duration_hours = duration_min / 60
        if duration_hours > max_hours:
            continue
        price = flight.get("price")
        if not price:
            continue
        return {
            "price": price,
            "duration_hours": round(duration_hours, 1),
            "airline": flight.get("flights", [{}])[0].get("airline", "?"),
            "stops": len(flight.get("flights", [])) - 1,
            "link": data.get("search_metadata", {}).get("google_flights_url", ""),
        }
    return None

def check_route(route, window, max_hours):
    """בודק כמה תאריכים בחלון ומחזיר את הטוב ביותר"""
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    # דגימה של 3-4 תאריכים בחלון כדי לא לבזבז יותר מדי קריאות
    dates = [
        start + timedelta(days=5),
        start + timedelta(days=12),
        end - timedelta(days=10),
    ]
    best = None

    for d in dates:
        if d < start or d > end:
            continue
        depart = d.strftime("%Y-%m-%d")
        # הלוך-חזור של 10 ימים בערך
        ret = (d + timedelta(days=10)).strftime("%Y-%m-%d")
        data = search_flights(route["origin"], route["destination"], depart, ret)
        option = parse_best_option(data, max_hours)
        if option and (best is None or option["price"] < best["price"]):
            best = option
            best["depart"] = depart
            best["return"] = ret

    return best

def main():
    config = load_config()
    max_hours = config.get("max_total_hours", 20)
    windows = config.get("preferred_windows", [])
    routes = config.get("routes", [])

    alerts = []
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    print(f"מתחיל בדיקה... {now}")

    for window in windows:
        for route in routes:
            print(f"בודק {route['name']} | {window['name']}")
            best = check_route(route, window, max_hours)
            if not best:
                continue

            threshold = route["extreme_roundtrip"]
            if best["price"] < threshold:
                msg = (
                    f"🔥 דיל חריג!\n"
                    f"📍 {route['name']}\n"
                    f"📅 {window['name']} ({best['depart']} → {best['return']})\n"
                    f"💰 ${best['price']} (סף: ${threshold})\n"
                    f"⏱️ {best['duration_hours']} שעות | {best['stops']} עצירות | {best['airline']}\n"
                    f"🔗 {best.get('link', 'Google Flights')}"
                )
                alerts.append(msg)
                print("  → נמצא דיל!")
            else:
                print(f"  מחיר ${best['price']} (מעל הסף)")

    if alerts:
        full = f"✈️ נמצאו דילים חריגים ({now})\n\n" + "\n\n".join(alerts)
        send_telegram(full)
        print(f"נשלחו {len(alerts)} התראות")
    else:
        print("לא נמצאו דילים מתחת לסף הפעם.")
        # אפשר לשלוח סיכום שקט או לא - כרגע לא שולחים כדי לא לספאם

if __name__ == "__main__":
    main()
