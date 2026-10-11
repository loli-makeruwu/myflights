# Flight Outlier Tracker

עותק עצמאי של סורק הטיסות מישראל ליפן, קוריאה ותאילנד. אותם קבצים רצים גם משורש המאגר.

אין צורך במפתח בתשלום. כל הרצה בודקת את Google Flights ואת Kiwi.com. Aviasales נוסף רק עם `TRAVELPAYOUTS_TOKEN` חינמי, וזה מטמון ולא חיפוש חי. `SERPAPI_KEY` הוא גיבוי בלבד, אם Google הישיר נכשל. Amadeus לא בשימוש.

הכללים: עד 20 שעות מדלת לדלת לכל כיוון, עד עצירה אחת, והספים ב-`routes_config.json`. בכל הרצה Google ו-Kiwi נבדקים בחמישה ימי יציאה, והטלגרם מקבל את כל הטיסות שעברו את הכללים. מחיר הלוך־חזור מ-Google לא כולל בדיקת משך החזור. יש לאמת כל מחיר לפני הזמנה.

## GitHub Actions

1. צרו ריפו פרטי והעלו את תיקיית `flight_tool`.
2. הוסיפו Secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `TRAVELPAYOUTS_TOKEN` (אופציונלי)
   - `SERPAPI_KEY` (אופציונלי)
3. `.github/workflows/flight_check.yml` רץ ב-06:00 וב-18:00 UTC, ואפשר להפעיל אותו ידנית.

## הרצה מקומית

```bash
pip install -r requirements.txt
cp .env.example .env
python check_flights.py --sample --no-telegram
```

`python flight_outlier_tracker.py` מדפיס קישורי השוואה מתוך `routes_config.json` ולא דורס את הקובץ. `python telegram_bot.py` שולח דשבורד קישורים לטלגרם.
