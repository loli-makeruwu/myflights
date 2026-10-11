#!/usr/bin/env python3
"""
בוט טלגרם אישי להתראות מחירים חריגים
ישראל ↔ יפן / קוריאה / תאילנד
"""

import os
import json
import requests
from datetime import datetime
from pathlib import Path

# טעינת משתני סביבה מקובץ .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN:
    raise SystemExit("❌ חסר TELEGRAM_BOT_TOKEN בקובץ .env")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

def send_message(text: str, parse_mode=None):
    if not CHAT_ID:
        print("⚠️  אין TELEGRAM_CHAT_ID. שלח הודעה לבוט ואז הרץ get_chat_id()")
        return False
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(url, json=payload, timeout=15)
    if r.ok:
        print("✅ הודעה נשלחה")
        return True
    else:
        print("❌ שגיאה בשליחה:", r.text)
        return False

def get_chat_id():
    """מביא את ה-Chat ID מהעדכון האחרון"""
    r = requests.get(f"{BASE_URL}/getUpdates", timeout=10)
    data = r.json()
    if not data.get("ok") or not data["result"]:
        print("אין עדכונים. שלח הודעה לבוט (@Flightsjustformebot) ונסה שוב.")
        return None
    last = data["result"][-1]
    chat_id = last["message"]["chat"]["id"]
    print(f"Chat ID שלך: {chat_id}")
    print("הוסף אותו לקובץ .env בשורה: TELEGRAM_CHAT_ID=" + str(chat_id))
    return chat_id

def load_config():
    config_path = Path(__file__).parent / "routes_config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def build_dashboard_message():
    config = load_config()
    windows = config.get("preferred_windows", [])
    routes = config.get("routes", [])
    max_hours = config.get("max_total_hours", 20)

    lines = [
        "✈️ דשבורד מחירים חריגים",
        f"⏱️ זמן כולל ≤ {max_hours} שעות",
        f"📅 עודכן: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        ""
    ]

    if windows:
        lines.append("חלונות מועדפים:")
        for w in windows:
            lines.append(f"• {w['name']}: {w['start']} → {w['end']}")
        lines.append("")

    for r in routes:
        lines.append(f"📍 {r['name']}")
        lines.append(f"סף: כיוון אחד < ${r['extreme_oneway']} | הלוך-חזור < ${r['extreme_roundtrip']}")
        link = f"https://www.google.com/travel/flights?hl=he&curr=USD&q=flights%20from%20{r['origin']}%20to%20{r['destination']}"
        lines.append(f"Google Flights: {link}")
        lines.append("")

    lines.append("סנן ב-Google Flights: Duration ≤ 20 שעות.")
    lines.append("מחיר מתחת לסף + זמן בסדר = דיל חריג.")
    return "\n".join(lines)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "chatid":
        get_chat_id()
    else:
        msg = build_dashboard_message()
        print(msg)
        print("---")
        send_message(msg)
