#!/usr/bin/env python3
"""
כלי אישי למעקב אחרי מחירים חריגים (outlier) במסלולים:
ישראל ↔ יפן, ישראל ↔ קוריאה, ישראל ↔ תאילנד

מיועד לזיהוי דילים כמו Centrum Air ב-300$ וכו'.

כללים:
- זמן טיסה כולל (כולל עצירות) ≤ 20 שעות
- חלונות תאריכים מועדפים: פברואר (1-2 שבועות) + קיץ אחרי סמסטר
"""

import json
from datetime import datetime
from urllib.parse import quote

# ==================== הגדרות אישיות ====================
MAX_TOTAL_HOURS = 20

# חלונות תאריכים מועדפים (2027 לקיץ, 2026/2027 לפברואר)
PREFERRED_WINDOWS = [
    {
        "name": "פברואר 2027",
        "start": "2027-02-01",
        "end": "2027-02-28",
        "trip_length_days": "7-14",
        "notes": "טיול של שבוע עד שבועיים"
    },
    {
        "name": "קיץ 2027 (אחרי סמסטר)",
        "start": "2027-07-01",
        "end": "2027-09-10",
        "trip_length_days": "10-21",
        "notes": "אחרי סוף הסמסטר השני עד תחילת הסמסטר הבא"
    }
]

ROUTES = [
    {
        "name": "ישראל → יפן",
        "origin": "TLV",
        "destination": "NRT",
        "extreme_oneway": 450,
        "extreme_roundtrip": 750,
        "notes": "ישיר: אל על / ארקיע (~12ש'). עצירה אחת רק אם סה״כ ≤20ש'."
    },
    {
        "name": "יפן → ישראל",
        "origin": "NRT",
        "destination": "TLV",
        "extreme_oneway": 450,
        "extreme_roundtrip": 750,
        "notes": "אותו כלל זמן."
    },
    {
        "name": "ישראל → קוריאה",
        "origin": "TLV",
        "destination": "ICN",
        "extreme_oneway": 350,
        "extreme_roundtrip": 600,
        "notes": "Centrum Air (דרך טשקנט) היה זול. רק עצירות ששומרות על ≤20ש' סה״כ."
    },
    {
        "name": "קוריאה → ישראל",
        "origin": "ICN",
        "destination": "TLV",
        "extreme_oneway": 350,
        "extreme_roundtrip": 600,
        "notes": "אותו כלל זמן."
    },
    {
        "name": "ישראל → תאילנד",
        "origin": "TLV",
        "destination": "BKK",
        "extreme_oneway": 300,
        "extreme_roundtrip": 550,
        "notes": "ישיר: אל על / ארקיע (~11-12ש'). עצירה אחת אפשרית אם סה״כ ≤20ש'."
    },
    {
        "name": "תאילנד → ישראל",
        "origin": "BKK",
        "destination": "TLV",
        "extreme_oneway": 300,
        "extreme_roundtrip": 550,
        "notes": "אותו כלל זמן."
    },
]

def generate_google_flights_link(origin, destination, start=None, end=None):
    """יוצר קישור ל-Google Flights עם חלון תאריכים אם ניתן"""
    base = "https://www.google.com/travel/flights?hl=he&curr=USD"
    if start and end:
        q = f"flights from {origin} to {destination} from {start} to {end}"
    else:
        q = f"flights from {origin} to {destination}"
    return f"{base}&q={quote(q)}"

def generate_skyscanner_link(origin, destination):
    return f"https://www.skyscanner.co.il/transport/flights/{origin}/{destination}/"

def print_dashboard():
    print("=" * 80)
    print("✈️  כלי אישי למחירים חריגים - ישראל ↔ יפן / קוריאה / תאילנד")
    print(f"⏱️  כלל זמן: זמן טיסה כולל ≤ {MAX_TOTAL_HOURS} שעות")
    print("=" * 80)
    print(f"עודכן: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    print("📅 חלונות תאריכים מועדפים:")
    for w in PREFERRED_WINDOWS:
        print(f"   • {w['name']}: {w['start']} → {w['end']}  ({w['trip_length_days']} ימים) — {w['notes']}")
    print()

    print("סף 'מחיר חריג' (בדולרים):\n")
    for r in ROUTES:
        print(f"📍 {r['name']}")
        print(f"   כיוון אחד < ${r['extreme_oneway']}  |  הלוך-חזור < ${r['extreme_roundtrip']}")
        print(f"   {r['notes']}")
        for w in PREFERRED_WINDOWS:
            link = generate_google_flights_link(r['origin'], r['destination'], w['start'], w['end'])
            print(f"   Google ({w['name']}): {link}")
        print(f"   Skyscanner: {generate_skyscanner_link(r['origin'], r['destination'])}")
        print()

    print("=" * 80)
    print("איך להשתמש:")
    print("1. פתח את הקישורים של החלונות המועדפים.")
    print("2. ב-Google Flights סנן Duration ≤20 שעות ו-Stops.")
    print("3. מחיר מתחת לסף + זמן ≤20ש' = התראה טובה.")
    print("4. ערוך ספים / תאריכים בקובץ לפי הצורך.")
    print("=" * 80)

if __name__ == "__main__":
    print_dashboard()

    config = {
        "max_total_hours": MAX_TOTAL_HOURS,
        "preferred_windows": PREFERRED_WINDOWS,
        "routes": ROUTES
    }
    with open("routes_config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print("\n✅ נשמר routes_config.json")
