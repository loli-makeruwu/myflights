#!/usr/bin/env python3
"""Flight-deal monitor using SerpAPI Google Flights and Telegram."""

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage" if TOKEN else ""
SERP_URL = "https://serpapi.com/search"
MAX_TELEGRAM_CHARS = 3900
SEARCH_ERRORS: list[str] = []
STATS = {"requests": 0, "successful": 0}


def load_config() -> dict[str, Any]:
    with open(Path(__file__).parent / "routes_config.json", encoding="utf-8") as f:
        import json
        return json.load(f)


def as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    amount = as_int(value)
    return "$" + f"{amount:,}" if amount is not None else "לא ידוע"


def send_telegram(message: str) -> bool:
    if not TOKEN or not CHAT_ID:
        print("Telegram not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    if len(message) > MAX_TELEGRAM_CHARS:
        message = message[: MAX_TELEGRAM_CHARS - 100].rstrip() + "\n\n… ההודעה קוצרה כדי לעמוד במגבלת Telegram."
    try:
        response = requests.post(
            TELEGRAM_URL,
            json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=20,
        )
        if not response.ok:
            print(f"Telegram HTTP error: {response.status_code}")
            return False
        try:
            payload = response.json()
        except ValueError:
            print("Telegram returned a non-JSON response.")
            return False
        if not payload.get("ok"):
            print("Telegram API rejected the message:", payload.get("description", "unknown error"))
            return False
        return True
    except requests.RequestException as exc:
        print(f"Telegram request failed: {type(exc).__name__}")
        return False


def api_search(params: dict[str, Any], context: str) -> dict[str, Any] | None:
    """Retry transient transport/server failures once; never log credentials."""
    for attempt in range(2):
        STATS["requests"] += 1
        try:
            response = requests.get(SERP_URL, params=params, timeout=45)
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1.5)
                continue
            SEARCH_ERRORS.append(f"{context}: request failed ({type(exc).__name__})")
            print(f"SerpAPI request failed for {context}: {type(exc).__name__}")
            return None

        try:
            data = response.json()
        except ValueError:
            data = {}
        error = data.get("error") if isinstance(data, dict) else None
        status = data.get("search_metadata", {}).get("status") if isinstance(data, dict) else None
        # Google Flights can return this for a valid search with no itineraries;
        # treat it as an empty result, not a broken API call.
        if isinstance(error, str) and "hasn't returned any results for this query" in error.lower():
            STATS["successful"] += 1
            print(f"No itineraries returned for {context}.")
            return {}
        if (response.status_code == 429 or response.status_code >= 500) and attempt == 0:
            time.sleep(1.5)
            continue
        if not response.ok or error or status == "Error" or not isinstance(data, dict) or not data:
            detail = str(error or f"HTTP {response.status_code}")[:220]
            if SERPAPI_KEY:
                detail = detail.replace(SERPAPI_KEY, "[redacted]")
            SEARCH_ERRORS.append(f"{context}: {detail}")
            print(f"SerpAPI error for {context}: {detail}")
            return None
        STATS["successful"] += 1
        return data
    SEARCH_ERRORS.append(f"{context}: transient API failure")
    return None


def flight_options(data: dict[str, Any] | None, max_hours: int, max_stops: int) -> list[dict[str, Any]]:
    """Parse best and other flights; sort by price, then duration and stops."""
    if not data:
        return []
    raw: list[dict[str, Any]] = []
    for key in ("best_flights", "other_flights", "flights"):
        entries = data.get(key, [])
        if isinstance(entries, list):
            raw.extend(item for item in entries if isinstance(item, dict))

    seen: set[tuple[Any, ...]] = set()
    accepted: list[dict[str, Any]] = []
    for item in raw:
        price = as_int(item.get("price"))
        segments = item.get("flights") if isinstance(item.get("flights"), list) else []
        duration = as_int(item.get("total_duration"))
        layovers = item.get("layovers")
        if duration is None:
            duration = sum(as_int(s.get("duration"), 0) or 0 for s in segments)
            if isinstance(layovers, list):
                duration += sum(as_int(s.get("duration"), 0) or 0 for s in layovers)
        stops = len(layovers) if isinstance(layovers, list) else max(0, len(segments) - 1)
        if price is None or price <= 0 or not duration or duration > max_hours * 60 or stops > max_stops:
            continue

        airlines: list[str] = []
        for segment in segments:
            airline = segment.get("airline")
            if airline and airline not in airlines:
                airlines.append(airline)
        if not airlines and item.get("airline"):
            airlines.append(item["airline"])

        identity = (price, duration, stops, tuple(s.get("flight_number", "") for s in segments), item.get("departure_token", ""))
        if identity in seen:
            continue
        seen.add(identity)
        accepted.append({
            "price": price,
            "duration_hours": round(duration / 60, 1),
            "stops": stops,
            "airline": ", ".join(airlines) if airlines else "לא צוין",
            "departure_token": item.get("departure_token"),
            "link": data.get("search_metadata", {}).get("google_flights_url", ""),
        })
    return sorted(accepted, key=lambda x: (x["price"], x["duration_hours"], x["stops"]))


def parse_trip_days(window: dict[str, Any]) -> list[int]:
    value = window.get("trip_length_days", "7-14")
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        numbers = [as_int(value[0], 7), as_int(value[1], 14)]
    else:
        numbers = [int(n) for n in re.findall(r"\d+", str(value))[:2]]
    low = max(1, numbers[0] if numbers else 7)
    high = max(1, numbers[1] if len(numbers) > 1 else low)
    if low > high:
        low, high = high, low
    return sorted(set((low, (low + high) // 2, high)))


def sample_search(
    window: dict[str, Any],
    route_index: int,
    window_index: int,
    today: date,
    one_way: bool = False,
    max_search_days_ahead: int = 320,
) -> tuple[str, int] | None:
    """Rotate date coverage while avoiding dates whose schedules may not be published yet."""
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    # Airlines commonly publish schedules roughly 10–12 months ahead. Keep round-trip
    # returns inside this conservative horizon to avoid false "no results" near the limit.
    horizon = today + timedelta(days=max_search_days_ahead)
    if one_way:
        end = min(end, horizon)
    else:
        latest_stay = max(parse_trip_days(window))
        end = min(end, horizon - timedelta(days=latest_stay))
    first_valid = max(start, today + timedelta(days=1))
    if first_valid > end:
        return None
    length = (end - first_valid).days + 1
    offset = (today.toordinal() + route_index * 11 + window_index * 17) % length
    depart = first_valid + timedelta(days=offset)
    stays = parse_trip_days(window)
    stay = stays[(today.toordinal() + route_index + window_index) % len(stays)]
    return depart.isoformat(), stay


def base_params(route: dict[str, Any], depart: str, max_hours: int, max_stops: int) -> dict[str, Any]:
    return {
        "engine": "google_flights",
        "departure_id": route["origin"],
        "arrival_id": route["destination"],
        "outbound_date": depart,
        "currency": "USD",
        "hl": "en",
        "gl": "il",
        "api_key": SERPAPI_KEY,
        "travel_class": "1",
        "adults": "1",
        "sort_by": "2",
        "show_hidden": "true",
        "max_duration": str(max_hours * 60),
        "stops": "2" if max_stops >= 1 else "1",
    }


def round_trip_query(route: dict[str, Any], depart: str, stay: int, max_hours: int, max_stops: int) -> dict[str, Any]:
    params = base_params(route, depart, max_hours, max_stops)
    params.update({
        "type": "1",
        "return_date": (date.fromisoformat(depart) + timedelta(days=stay)).isoformat(),
        "deep_search": "true",
    })
    return params


def one_way_query(route: dict[str, Any], depart: str, max_hours: int, max_stops: int) -> dict[str, Any]:
    params = base_params(route, depart, max_hours, max_stops)
    params["type"] = "2"
    return params


def find_route_pairs(routes: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs = []
    seen: set[str] = set()
    for outbound in routes:
        destination = outbound.get("destination")
        if outbound.get("origin") != "TLV" or destination in seen:
            continue
        inbound = next((r for r in routes if r.get("origin") == destination and r.get("destination") == "TLV"), outbound)
        pairs.append((outbound, inbound))
        seen.add(destination)
    return pairs


def run_search(params: dict[str, Any], context: str, max_hours: int, max_stops: int):
    data = api_search(params, context)
    return data, flight_options(data, max_hours, max_stops)


def format_option(route_name: str, window_name: str, option: dict[str, Any], depart: str, ret: str | None = None, label: str = "") -> str:
    dates = f"{depart} → {ret}" if ret else depart
    return (
        f"{label}{route_name} | {window_name}\n"
        f"📅 {dates}\n"
        f"💰 {money(option['price'])}\n"
        f"⏱️ {option['duration_hours']} ש׳ | {option['stops']} עצירות | {option['airline']}\n"
        f"🔗 {option.get('link') or 'פתח Google Flights ובדוק את המחיר'}"
    )


def main() -> int:
    if not all((TOKEN, CHAT_ID, SERPAPI_KEY)):
        missing = [name for name, val in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID), ("SERPAPI_KEY", SERPAPI_KEY)) if not val]
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    config = load_config()
    max_hours = as_int(config.get("max_total_hours"), 20) or 20
    max_stops = as_int(config.get("max_stops"), 1)
    max_stops = 1 if max_stops is None else max(0, max_stops)
    pairs = find_route_pairs(config.get("routes", []))
    windows = config.get("preferred_windows", [])
    today = date.today()
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    cycle = max(2, as_int(config.get("search_cycle_days"), 3) or 3)
    confirm_limit = max(0, as_int(config.get("max_return_checks_per_run"), 4) or 0)
    one_way_day = today.toordinal() % cycle == 0

    deals: list[str] = []
    unverified: list[str] = []
    snapshots: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    scan_count = 0
    mode_label = "חד־כיווניות" if one_way_day else "הלוך־חזור"
    print(f"Starting flight scan at {now}; mode={mode_label}")

    for route_index, (outbound, inbound) in enumerate(pairs):
        for window_index, window in enumerate(windows):
            sampled = sample_search(window, route_index, window_index, today, one_way=one_way_day)
            if not sampled:
                print(f"Skipping {outbound['name']} | {window['name']}: window has ended")
                continue
            depart, stay = sampled
            scan_count += 1

            if one_way_day:
                route = inbound if (today.toordinal() + route_index + window_index) % 2 else outbound
                threshold = as_int(route.get("extreme_oneway"), 0) or 0
                data, options = run_search(one_way_query(route, depart, max_hours, max_stops),
                                           f"one-way {route['name']} | {window['name']} | {depart}", max_hours, max_stops)
                if data is None or not options:
                    continue
                snapshots.append({"route": route["name"], "window": window["name"], "depart": depart,
                                  "return": None, "option": options[0], "threshold": threshold})
                for option in options:
                    if threshold and option["price"] < threshold:
                        deals.append(format_option(route["name"], window["name"], option, depart, label="🔥 דיל חד־כיווני\n"))
                        if len(deals) >= 12:
                            break
                continue

            params = round_trip_query(outbound, depart, stay, max_hours, max_stops)
            ret = params["return_date"]
            data, options = run_search(params, f"round-trip {outbound['name']} | {window['name']} | {depart}", max_hours, max_stops)
            if data is None or not options:
                continue
            threshold = as_int(outbound.get("extreme_roundtrip"), 0) or 0
            snapshots.append({"route": outbound["name"], "window": window["name"], "depart": depart,
                              "return": ret, "option": options[0], "threshold": threshold})
            for option in options:
                if threshold and option["price"] < threshold and option.get("departure_token"):
                    candidates.append({"route": outbound, "window": window, "depart": depart, "return": ret,
                                       "threshold": threshold, "option": option, "params": params})
                elif threshold and option["price"] < threshold:
                    unverified.append(format_option(outbound["name"], window["name"], option, depart, ret,
                                                    label="🟡 מחיר טוב, החזור לא אומת\n"))

    # Verify return itineraries only for the best candidates, to control API usage.
    candidates.sort(key=lambda c: (c["option"]["price"] / max(c["threshold"], 1), c["option"]["price"]))
    for candidate in candidates[confirm_limit:]:
        unverified.append(format_option(candidate["route"]["name"], candidate["window"]["name"], candidate["option"],
                                        candidate["depart"], candidate["return"],
                                        label="🟡 מחיר טוב, החזור טרם אומת כדי לשמור על מכסת החיפושים\n"))
    for candidate in candidates[:confirm_limit]:
        option = candidate["option"]
        params = dict(candidate["params"])
        params["departure_token"] = option["departure_token"]
        context = f"return-leg {candidate['route']['name']} | {candidate['depart']} → {candidate['return']}"
        return_data, return_options = run_search(params, context, max_hours, max_stops)
        if return_data is None:
            unverified.append(format_option(candidate["route"]["name"], candidate["window"]["name"], option,
                                            candidate["depart"], candidate["return"],
                                            label="🟡 מחיר טוב, החזור לא אומת בגלל שגיאת API\n"))
            continue
        if not return_options:
            unverified.append(format_option(candidate["route"]["name"], candidate["window"]["name"], option,
                                            candidate["depart"], candidate["return"],
                                            label="🟡 מחיר טוב, אך לא נמצאה טיסת חזור שעומדת במגבלות\n"))
            continue
        return_leg = return_options[0]
        deals.append(
            "🔥 דיל הלוך־חזור מאומת\n"
            f"📍 {candidate['route']['name']} | {candidate['window']['name']}\n"
            f"📅 {candidate['depart']} → {candidate['return']}\n"
            f"💰 {money(option['price'])} (סף: {money(candidate['threshold'])})\n"
            f"🛫 הלוך: {option['duration_hours']} ש׳, {option['stops']} עצירות\n"
            f"🛬 חזור: {return_leg['duration_hours']} ש׳, {return_leg['stops']} עצירות\n"
            f"✈️ {option['airline']}\n"
            f"🔗 {option.get('link') or 'פתח Google Flights ובדוק את המחיר'}"
        )

    deals = list(dict.fromkeys(deals))[:8]
    unverified = list(dict.fromkeys(unverified))[:3]
    snapshots.sort(key=lambda x: (x["option"]["price"] / max(x["threshold"], 1), x["option"]["price"]))
    snapshot_lines = []
    for item in snapshots[:6]:
        option = item["option"]
        dates = f"{item['depart']} → {item['return']}" if item["return"] else item["depart"]
        snapshot_lines.append(
            f"• {item['route']} | {item['window']} | {dates}: {money(option['price'])} "
            f"({option['duration_hours']} ש׳, {option['stops']} עצירות; סף {money(item['threshold'])})\n"
            f"  {option.get('link') or 'קישור לא זמין'}"
        )

    lines = [
        f"✈️ בדיקת טיסות {mode_label} | {now}",
        f"סריקות בסיס: {scan_count} | בקשות API בפועל: {STATS['requests']} | הצליחו: {STATS['successful']} | שגיאות: {len(SEARCH_ERRORS)}",
    ]
    if SEARCH_ERRORS and STATS["successful"] == 0:
        lines.append("\n⚠️ כל החיפושים נכשלו. אי אפשר להסיק שאין דילים.")
    elif SEARCH_ERRORS:
        lines.append("\n⚠️ הבדיקה חלקית בגלל שגיאות API; התוצאות אינן ממצות.")
    elif not deals:
        lines.append("\nלא נמצא דיל מאומת מתחת לסף בהרצה הזו. המחירים הנמוכים שנמצאו מוצגים למעקב.")
    if deals:
        lines.append("\n" + "\n\n".join(deals))
    if unverified:
        lines.append("\n\n🟡 מחירים מעניינים שדורשים אימות ידני:\n" + "\n\n".join(unverified))
    if snapshot_lines:
        lines.append("\n\n📊 המחירים הנמוכים שנבדקו (לא בהכרח דיל):\n" + "\n".join(snapshot_lines))
    if SEARCH_ERRORS:
        lines.append("\n\nשגיאות לדוגמה: " + " | ".join(SEARCH_ERRORS[:2]))
    message = "\n".join(lines)

    if send_telegram(message):
        print("Flight scan summary sent to Telegram.")
    else:
        print("Failed to send flight scan summary to Telegram.")
    return 0 if STATS["successful"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
