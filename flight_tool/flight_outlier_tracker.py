#!/usr/bin/env python3
"""Print comparison links for the routes in routes_config.json.

This reads the config and does not write it.
"""

import json
from datetime import datetime
from pathlib import Path

from flight_sources import comparison_links, primary_code


def load_config() -> dict:
    path = Path(__file__).parent / "routes_config.json"
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def print_dashboard() -> None:
    config = load_config()
    max_hours = config.get("max_total_hours", 20)
    max_stops = config.get("max_stops", 1)
    windows = config.get("preferred_windows", [])
    routes = config.get("routes", [])

    print("=" * 80)
    print("כלי קישורים למחירים חריגים — ישראל ↔ יפן / קוריאה / תאילנד")
    stop_label = "בלי עצירות" if int(max_stops) == 0 else ("עצירה אחת" if int(max_stops) == 1 else f"{max_stops} עצירות")
    print(f"כלל: עד {max_hours} שעות לכל כיוון, עד {stop_label}")
    print("=" * 80)
    print(f"עודכן: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("הקובץ routes_config.json נקרא ולא נדרס.\n")

    print("חלונות מועדפים:")
    for window in windows:
        print(f"   • {window['name']}: {window['start']} → {window['end']} ({window.get('trip_length_days', '')})")
    print()

    for route in routes:
        origin = primary_code(route, "origin")
        destination = primary_code(route, "destination")
        print(f"{route['name']} ({origin} → {destination})")
        print(f"   כיוון אחד < ${route['extreme_oneway']}  |  הלוך-חזור < ${route['extreme_roundtrip']}")
        if route.get("notes"):
            print(f"   {route['notes']}")
        for name, url in comparison_links(origin, destination):
            print(f"   {name}: {url}")
        print()

    print("=" * 80)
    print("הסריקה האוטומטית היא check_flights.py. הקישורים כאן לבדיקה ידנית.")
    print("אמתו מחיר לפני הזמנה. משך הטיסה נמדד מדלת לדלת, כולל המתנה במעבר.")
    print("=" * 80)


if __name__ == "__main__":
    print_dashboard()
