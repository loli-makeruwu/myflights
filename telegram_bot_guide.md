# מדריך לבוט הטלגרם

הבוט מקבל את סיכום הסריקה מ-`check_flights.py`. הדשבורד ב-`telegram_bot.py` שולח קישורים לבדיקה ידנית. אין צורך במפתח טיסות בתשלום.

## 1. יצירת הבוט

1. בטלגרם, פתחו את **@BotFather**.
2. שלחו `/newbot`.
3. בחרו שם ו-username שמסתיים ב-`bot`.
4. שמרו את הטוקן ש-BotFather שולח. אל תשימו אותו בקוד או בריפו ציבורי.

## 2. Chat ID

1. שלחו הודעה כלשהי לבוט.
2. שימו את הטוקן ב-`.env` בשורה `TELEGRAM_BOT_TOKEN=...` (העתיקו מ-`.env.example`).
3. הריצו `python telegram_bot.py chatid`.
4. הוסיפו את המספר שמודפס כ-`TELEGRAM_CHAT_ID` באותו קובץ.

אפשר גם לפתוח `https://api.telegram.org/bot<הטוקן>/getUpdates` ולחפש את `"chat":{"id":`.

## 3. מה רץ אוטומטית

`check_flights.py` סורק Google Flights ו-Kiwi.com בלי מפתח, ושולח סיכום לטלגרם גם כשאין דיל. ב-GitHub Actions מגדירים את `TELEGRAM_BOT_TOKEN` ואת `TELEGRAM_CHAT_ID` כ-Repository secrets.

אופציונלי:

- `TRAVELPAYOUTS_TOKEN` — מטמון Aviasales מחשבון Travelpayouts חינמי. זה לא חיפוש חי.
- `SERPAPI_KEY` — גיבוי רק אם החיפוש הישיר ב-Google נכשל.

## 4. דשבורד ידני

```bash
pip install -r requirements.txt
python telegram_bot.py
```

ההודעה כוללת את הספים וקישורי השוואה לכל מסלול ב-`routes_config.json`.
