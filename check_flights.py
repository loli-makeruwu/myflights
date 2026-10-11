#!/usr/bin/env python3
"""בודק מחירי טיסות חריגים ושולח סיכום לטלגרם בכל הרצה."""

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
BASE_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
SERP_URL = "https://serpapi.com/search"
SEARCH_ERRORS = []

def load_config():
    with open(Path(__file__).parent / "routes_config.json", encoding="utf-8") as f:
        return json.load(f)

def send_telegram(text: str) -> bool:
    if not TOKEN or not CHAT_ID:
        print("Telegram not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.ok:
            print("Telegram HTTP error:", r.text)
            return False
        payload = r.json()
        if not payload.get("ok"):
            print("Telegram API error:", payload)
            return False
        return True
    except Exception as e:
        print(f"Telegram request failed: {e}")
        return False

def search_flights(origin, destination, depart_date, return_date=None):
    """חיפוש ב-SerpAPI Google Flights. type=1 הוא הלוך-חזור."""
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": depart_date,
        "currency": "USD",
        "hl": "en",
        "api_key": SERPAPI_KEY,
        "type": "1" if return_date else "2",
    }
    if return_date:
        params["return_date"] = return_date

    try:
        r = requests.get(SERP_URL, params=params, timeout=30)
        data = r.json()
        if not r.ok or "error" in data:
            error = data.get("error", f"HTTP {r.status_code}")
            SEARCH_ERRORS.append(f"{origin}→{destination} {depart_date}: {error}")
            print(f"SerpAPI error: {error}")
            return None
        return data
    except Exception as e:
        SEARCH_ERRORS.append(f"{origin}→{destination} {depart_date}: {e}")
        print(f"Request failed: {e}")
        return None

def parse_best_option(data, max_hours=20):
    """מחזיר את האפשרות הזולה ביותר שעומדת במגבלת הזמן."""
    if not data or "best_flights" not in data:
        return None
    for flight in data["best_flights"]:
        duration_min = flight.get("total_duration", 9999)
        duration_hours = duration_min / 60
        if duration_hours > max_hours:
            continue
        price = flight.get("price")
        if price is None:
            continue
        segments = flight.get("flights", [])
        return {
            "price": price,
            "duration_hours": round(duration_hours, 1),
            "airline": segments[0].get("airline", "?") if segments else "?",
            "stops": max(0, len(segments) - 1),
            "link": data.get("search_metadata", {}).get("google_flights_url", ""),
        }
    return None

def check_route(route, window, max_hours):
    """בודק מספר תאריכים בחלון ומחזיר את האפשרות הזולה ביותר."""
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    dates = [start + timedelta(days=5), start + timedelta(days=12), end - timedelta(days=10)]
    best = None
    for d in dates:
        if d < start or d > end:
            continue
        depart = d.strftime("%Y-%m-%d")
        ret = (d + timedelta(days=10)).strftime("%Y-%m-%d")
        data = search_flights(route["origin"], route["destination"], depart, ret)
        option = parse_best_option(data, max_hours)
        if option and (best is None or option["price"] < best["price"]):
            best = {**option, "depart": depart, "return": ret}
    return best

def main():
    if not all([TOKEN, CHAT_ID, SERPAPI_KEY]):
        missing = [name for name, value in [
            ("TELEGRAM_BOT_TOKEN", TOKEN),
            ("TELEGRAM_CHAT_ID", CHAT_ID),
            ("SERPAPI_KEY", SERPAPI_KEY),
        ] if not value]
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    config = load_config()
    max_hours = config.get("max_total_hours", 20)
    windows = config.get("preferred_windows", [])
    routes = config.get("routes", [])
    alerts = []
    successful_searches = 0
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    print(f"מתחיל בדיקה... {now}")
    for window in windows:
        for route in routes:
            print(f"בודק {route['name']} | {window['name']}")
            errors_before = len(SEARCH_ERRORS)
            best = check_route(route, window, max_hours)
            # Count successful searches by comparing total attempted requests with errors.
            attempted = 3
            successful_searches += max(0, attempted - (len(SEARCH_ERRORS) - errors_before))
            if not best:
                continue
            threshold = route["extreme_roundtrip"]
            if best["price"] < threshold:
                alerts.append(
                    f"🔥 דיל חריג!\n"
                    f"📍 {route['name']}\n"
                    f"📅 {window['name']} ({best['depart']} → {best['return']})\n"
                    f"💰 ${best['price']} (סף: ${threshold})\n"
                    f"⏱️ {best['duration_hours']} שעות | {best['stops']} עצירות | {best['airline']}\n"
                    f"🔗 {best.get('link') or 'Google Flights'}"
                )
            else:
                print(f"מחיר ${best['price']} מעל הסף ${threshold}")

    if SEARCH_ERRORS and successful_searches == 0:
        summary = (
            f"⚠️ בדיקת הטיסות נכשלה ({now})\n"
            f"כל {len(SEARCH_ERRORS)} בקשות החיפוש נכשלו. לא ניתן לקבוע אם יש דילים.\n"
            f"שגיאה לדוגמה: {SEARCH_ERRORS[0]}"
        )
    elif SEARCH_ERRORS:
        summary = (
            f"⚠️ בדיקת הטיסות הסתיימה חלקית ({now})\n"
            f"הצלחות חיפוש: {successful_searches}; שגיאות: {len(SEARCH_ERRORS)}.\n"
            "התוצאות עלולות להיות חלקיות."
        )
        if alerts:
            summary += "\n\n" + "\n\n".join(alerts)
        else:
            summary += "\nלא נמצא דיל בסריקות שהצליחו."
    elif alerts:
        summary = f"✈️ נמצאו דילים חריגים ({now})\n\n" + "\n\n".join(alerts)
    else:
        summary = f"🔎 בדיקת הטיסות הסתיימה ({now})\nלא נמצאו דילים מתחת לסף הפעם."

    if send_telegram(summary):
        print("סיכום הבדיקה נשלח לטלגרם.")
    else:
        print("לא הצלחתי לשלוח את סיכום הבדיקה לטלגרם.")
    if SEARCH_ERRORS:
        print(f"Search errors: {len(SEARCH_ERRORS)}")

if __name__ == "__main__":
    main()
