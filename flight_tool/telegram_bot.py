#!/usr/bin/env python3
"""Personal Telegram dashboard with comparison links for the configured routes."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

from flight_sources import comparison_links, primary_code

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
MAX_TELEGRAM_CHARS = 3900


def send_message(text: str) -> bool:
    if not CHAT_ID:
        print("No TELEGRAM_CHAT_ID. Message the bot, then run: python telegram_bot.py chatid")
        return False
    if len(text) > MAX_TELEGRAM_CHARS:
        text = text[: MAX_TELEGRAM_CHARS - 40].rstrip() + "\n\n… ההודעה קוצרה."
    response = requests.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    if response.ok:
        print("Message sent.")
        return True
    print("Telegram rejected the message.")
    return False


def get_chat_id():
    """Print the chat id from the latest update after you message the bot."""
    response = requests.get(f"{BASE_URL}/getUpdates", timeout=10)
    data = response.json()
    if not data.get("ok") or not data["result"]:
        print("No updates yet. Send any message to the bot and run this again.")
        return None
    chat_id = data["result"][-1]["message"]["chat"]["id"]
    print(f"Chat ID: {chat_id}")
    print("Add this line to .env: TELEGRAM_CHAT_ID=" + str(chat_id))
    return chat_id


def load_config() -> dict:
    config_path = Path(__file__).parent / "routes_config.json"
    with open(config_path, encoding="utf-8") as handle:
        return json.load(handle)


def build_dashboard_message() -> str:
    config = load_config()
    windows = config.get("preferred_windows", [])
    routes = config.get("routes", [])
    max_hours = config.get("max_total_hours", 20)
    max_stops = config.get("max_stops", 1)

    lines = [
        "✈️ דשבורד מחירים חריגים",
        f"⏱️ עד {max_hours} שעות לכל כיוון | עד {'עצירה אחת' if int(max_stops) == 1 else str(max_stops) + ' עצירות'}",
        f"📅 עודכן: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "הקישורים הם לבדיקה ידנית. הסורק עצמו הוא check_flights.py.",
        "",
    ]
    if windows:
        lines.append("חלונות מועדפים:")
        for window in windows:
            lines.append(f"• {window['name']}: {window['start']} → {window['end']} ({window.get('trip_length_days', '')})")
        lines.append("")

    for route in routes:
        origin = primary_code(route, "origin")
        destination = primary_code(route, "destination")
        lines.append(f"📍 {route['name']} ({origin} → {destination})")
        lines.append(f"סף: כיוון אחד < ${route['extreme_oneway']} | הלוך-חזור < ${route['extreme_roundtrip']}")
        for name, url in comparison_links(origin, destination):
            lines.append(f"{name}: {url}")
        lines.append("")

    lines.append("מחיר מתחת לסף, עם זמן ועצירות בתוך הכללים, הוא דיל ששווה בדיקה. אמתו את המחיר לפני הזמנה.")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "chatid":
        get_chat_id()
    else:
        message = build_dashboard_message()
        print(message)
        print("---")
        send_message(message)
