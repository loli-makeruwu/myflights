# Flight Outlier Tracker - Personal Telegram Bot

כלי אישי שבודק מחירים חריגים במסלולים:
ישראל ↔ יפן / קוריאה / תאילנד

**כללים:**
- זמן כולל ≤ 20 שעות
- מחיר מתחת לסף שהגדרת
- חלונות: פברואר + קיץ

## איך להפעיל אוטומטית (GitHub Actions)

1. צור ריפו חדש ב-GitHub (פרטי).
2. העלה את כל הקבצים מתיקיית `flight_tool`.
3. הוסף Secrets בריפו (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `SERPAPI_KEY` (הירשם חינם ב-serpapi.com)

4. ה-workflow רץ אוטומטית (כל יום או לפי מה שתגדיר).

## קבצים חשובים
- `check_flights.py` — הסקריפט הראשי שבודק מחירים ושולח התראה
- `.github/workflows/flight_check.yml` — אוטומציה
- `routes_config.json` — מסלולים, ספים וחלונות
- `.env.example` — דוגמה למשתנים

## הרצה מקומית
```bash
pip install -r requirements.txt
cp .env.example .env
# מלא את הערכים
python3 check_flights.py
```