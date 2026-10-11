#!/usr/bin/env python3
"""Flight-deal monitor for Israel to Japan, Korea and Thailand.

Each run checks Google Flights and Kiwi.com without a paid key. Aviasales is
added when a free Travelpayouts token is set. SerpAPI is used only if direct
Google Flights fails and a key is already configured.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from flight_sources import (
    AVIASALES,
    GOOGLE,
    KIWI,
    SERPAPI,
    Offer,
    SearchResult,
    airport_codes,
    aviasales_configured,
    comparison_links,
    deal_status,
    fetch_deal_headlines,
    primary_code,
    search_aviasales,
    search_google,
    search_kiwi,
    search_serpapi,
    serpapi_configured,
)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage" if TOKEN else ""
MAX_TELEGRAM_CHARS = 3900


def load_config() -> dict[str, Any]:
    with open(Path(__file__).parent / "routes_config.json", encoding="utf-8") as handle:
        import json
        return json.load(handle)


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
        print("Telegram is not configured.")
        return False
    if len(message) > MAX_TELEGRAM_CHARS:
        message = message[: MAX_TELEGRAM_CHARS - 80].rstrip() + "\n\n… ההודעה קוצרה כדי לעמוד במגבלת Telegram."
    try:
        response = requests.post(
            TELEGRAM_URL,
            json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=20,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Telegram request failed: {type(exc).__name__}")
        return False
    if not response.ok or not payload.get("ok"):
        print("Telegram rejected the message.")
        return False
    return True


def pack_messages(blocks: list[str], limit: int = MAX_TELEGRAM_CHARS) -> list[str]:
    """Pack text blocks into Telegram-sized messages without dropping a block."""
    budget = max(200, limit - 16)
    messages: list[str] = []
    current = ""
    for block in blocks:
        text = block.strip("\n")
        if not text:
            continue
        while text:
            gap = "\n" if current else ""
            if len(current) + len(gap) + len(text) <= budget:
                current = f"{current}{gap}{text}"
                break
            if current:
                messages.append(current)
                current = ""
                continue
            messages.append(text[:budget])
            text = text[budget:].lstrip("\n")
    if current:
        messages.append(current)
    if len(messages) < 2:
        return messages
    total = len(messages)
    return [f"({index}/{total})\n{text}" for index, text in enumerate(messages, 1)]


def send_telegram_messages(messages: list[str]) -> bool:
    if not messages:
        return False
    for index, message in enumerate(messages):
        if not send_telegram(message):
            return False
        if index + 1 < len(messages):
            time.sleep(0.4)
    return True


def trip_bounds(window: dict[str, Any]) -> tuple[int, int]:
    value = window.get("trip_length_days", "7-14")
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        numbers = [as_int(value[0], 7) or 7, as_int(value[1], 14) or 14]
    else:
        numbers = [int(item) for item in re.findall(r"\d+", str(value))[:2]]
    low = max(1, numbers[0] if numbers else 7)
    high = max(1, numbers[1] if len(numbers) > 1 else low)
    if low > high:
        low, high = high, low
    return low, high


def plan_search(
    window: dict[str, Any],
    route_index: int,
    window_index: int,
    today: date,
    one_way: bool,
    max_search_days_ahead: int = 320,
) -> dict[str, Any] | None:
    """Clamp the window to published schedules and rotate Google's sample date."""
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    low, high = trip_bounds(window)
    horizon = today + timedelta(days=max_search_days_ahead)
    if one_way:
        end = min(end, horizon)
    else:
        end = min(end, horizon - timedelta(days=high))
    first = max(start, today + timedelta(days=1))
    if first > end:
        return None
    length = (end - first).days + 1
    offset = (today.toordinal() + route_index * 11 + window_index * 17) % length
    depart = first + timedelta(days=offset)
    choices = sorted({low, (low + high) // 2, high})
    stay = choices[(today.toordinal() + route_index + window_index) % len(choices)]
    return {"depart": depart.isoformat(), "stay": stay, "window": (first, end), "bounds": (low, high)}


def find_route_pairs(routes: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs = []
    seen: set[str] = set()
    for outbound in routes:
        destination = outbound.get("destination")
        if outbound.get("origin") != "TLV" or destination in seen:
            continue
        inbound = next((route for route in routes if route.get("origin") == destination and route.get("destination") == "TLV"), outbound)
        pairs.append((outbound, inbound))
        seen.add(destination)
    return pairs


def scan_market(
    route: dict[str, Any],
    plan: dict[str, Any],
    max_hours: int,
    max_stops: int,
    one_way: bool,
) -> list[SearchResult]:
    origins = airport_codes(route, "origin")
    destinations = airport_codes(route, "destination")
    depart = plan["depart"]
    ret = None if one_way else (date.fromisoformat(depart) + timedelta(days=plan["stay"])).isoformat()
    window = plan["window"]
    stay = None if one_way else plan["bounds"]
    results = [
        search_google(origins, destinations, depart, ret, max_hours, max_stops, window, stay),
        search_kiwi(origins, destinations, window, stay, max_hours, max_stops),
    ]
    if aviasales_configured():
        results.append(search_aviasales(origins, destinations, window, stay, max_hours, max_stops))
    if not results[0].ok and serpapi_configured():
        print(f"Direct Google Flights failed for {route['name']}; trying SerpAPI.")
        results.append(search_serpapi(origins, destinations, depart, ret, max_hours, max_stops, window, stay))
    for result in results:
        if result.requests == 0 and result.source == AVIASALES:
            continue
        status = "ok" if result.ok else "FAILED"
        detail = result.error or f"{len(result.offers)} within limits, {result.raw_count} raw"
        if result.near_miss:
            detail += f", nearest ${result.near_miss.price} rejected ({result.near_miss_reason})"
        print(f"{result.source} {route['name']} {window[0]}..{window[1]}: {status}, {detail}")
    return results


def format_stops(offer: Offer) -> str:
    parts = []
    for stops in offer.stops_per_leg:
        if stops <= 0:
            parts.append("ישיר")
        elif stops == 1:
            parts.append("עצירה אחת")
        else:
            parts.append(f"{stops} עצירות")
    return " / ".join(parts)


def _snapshot(route_name: str, window_name: str, offer: Offer, threshold: int, rejected: str) -> tuple[float, str]:
    dates = f"{offer.depart} → {offer.ret}" if offer.ret else offer.depart
    duration = format_duration(offer)
    if offer.ret and not offer.return_verified:
        duration += " הלוך; החזור לא נמדד"
    reason = f" — נפסל: {rejected}" if rejected else ""
    line = (
        f"• {route_name} | {window_name} | {dates}: {money(offer.price)} "
        f"({duration}, {format_stops(offer)}; סף {money(threshold)}; {offer.source}){reason}"
    )
    return offer.price / max(threshold, 1), line


def format_duration(offer: Offer) -> str:
    return " / ".join(f"{hours:g} ש׳" for hours in offer.leg_durations)


def format_flight_line(route_name: str, offer: Offer, origin: str, destination: str) -> str:
    """One compact row. Long booking tokens are replaced by a short search link."""
    dates = f"{offer.depart} → {offer.ret}" if offer.ret else offer.depart
    clocks = ""
    if offer.depart_time:
        clocks = f" {offer.depart_time}"
        if offer.return_time:
            clocks += f" / {offer.return_time}"
    unverified = " | חזור לא נמדד" if offer.ret and not offer.return_verified else ""
    link = _list_link(offer, origin, destination)
    link_line = f"\n  {link}" if link else ""
    return (
        f"• {money(offer.price)} | {route_name} | {dates}{clocks} | "
        f"{format_duration(offer)} | {format_stops(offer)} | {offer.airline} | {offer.source}{unverified}{link_line}"
    )


def _list_link(offer: Offer, origin: str, destination: str) -> str:
    if offer.link and len(offer.link) <= 180:
        return offer.link
    links = dict(comparison_links(origin, destination, offer.depart, offer.ret))
    if offer.source == KIWI:
        return links.get("Kiwi", "")
    if offer.source in (GOOGLE, SERPAPI):
        return links.get("Google Flights", "")
    if offer.source == AVIASALES:
        return links.get("Aviasales", "")
    return ""


def _priced(route: dict[str, Any], window_name: str, offer: Offer, origin: str, destination: str, threshold: int, label: str) -> str:
    text = format_offer(route["name"], window_name, offer, origin, destination, label)
    return text.replace(f"💰 {money(offer.price)}", f"💰 {money(offer.price)} (סף: {money(threshold)})", 1)


def format_offer(route_name: str, window_name: str, offer: Offer, origin: str, destination: str, label: str) -> str:
    dates = f"{offer.depart} → {offer.ret}" if offer.ret else offer.depart
    extras = comparison_links(origin, destination, offer.depart, offer.ret)
    wanted = {"Kayak", "Skyscanner"}
    if offer.source != AVIASALES:
        wanted.add("Aviasales")
    if offer.source not in (GOOGLE, SERPAPI):
        wanted.add("Google Flights")
    extra = " | ".join(f"{name}: {url}" for name, url in extras if name in wanted)
    note = f"\nℹ️ {offer.note}" if offer.note else ""
    return (
        f"{label}{route_name} | {window_name}\n"
        f"📅 {dates}\n"
        f"💰 {money(offer.price)}\n"
        f"⏱️ {format_duration(offer)} | {format_stops(offer)} | {offer.airline}\n"
        f"🔎 {offer.source}{note}\n"
        f"🔗 {offer.link or 'אין קישור ישיר; פתחו את קישורי ההשוואה'}\n"
        f"השוואה: {extra}"
    )


def build_message(
    mode_label: str,
    now: str,
    scan_count: int,
    requests_made: int,
    successes: int,
    errors: list[str],
    deals: list[str],
    unverified: list[str],
    snapshots: list[str],
    headlines: list[dict],
    headline_error: str | None,
) -> str:
    sources = f"{GOOGLE} + {KIWI}"
    if aviasales_configured():
        sources += f" + {AVIASALES}"
    else:
        sources += " (Aviasales ממתין לטוקן חינמי)"
    if serpapi_configured():
        sources += f"; גיבוי {SERPAPI} רק אם Google הישיר נכשל"
    lines = [
        f"✈️ בדיקת טיסות {mode_label} | {now}",
        f"מקורות: {sources}",
        "Google ו-Kiwi נבדקים בחמישה ימי יציאה לאורך החלון. במחיר הלוך־חזור מ-Google משך החזור לא נמדד. Aviasales, אם מוגדר, הוא מטמון לחודש ולא חיפוש חי.",
        f"סריקות: {scan_count} | בקשות: {requests_made} | מקורות שהצליחו: {successes} | שגיאות: {len(errors)}",
    ]
    if errors and successes == 0:
        lines.append("\n⚠️ כל החיפושים נכשלו. אי אפשר להסיק שאין דילים.")
    elif errors:
        lines.append("\n⚠️ הבדיקה חלקית בגלל שגיאות מקור; התוצאות אינן ממצות.")
    elif not deals:
        lines.append("\nלא נמצא דיל מאומת מתחת לסף בהרצה הזו. המחירים הנמוכים שנמצאו מוצגים למעקב.")
    if deals:
        lines.append("\n" + "\n\n".join(deals))
    if unverified:
        lines.append("\n\n🟡 מחירים מעניינים שדורשים בדיקת חזור ידנית:\n" + "\n\n".join(unverified))
    if snapshots:
        lines.append("\n\n📊 המחירים הנמוכים שנבדקו (לא בהכרח דיל):\n" + "\n".join(snapshots))
    if headlines:
        lines.append("\n\n📰 כותרות מדילים ציבוריים (לא הצעת מחיר אישית):")
        for item in headlines:
            scope = "קשור לישראל" if item["relevance"] == "israel" else "יעד רלוונטי, המוצא לא בהכרח ישראל"
            lines.append(f"• {item['source']}: {item['title']} ({scope})\n  {item['link']}")
    elif headline_error:
        lines.append("\n\n📰 ערוצי הדילים הציבוריים לא נענו בהרצה הזו.")
    if errors:
        lines.append("\n\nשגיאות לדוגמה: " + " | ".join(errors[:2]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan configured flight routes and alert on cheap fares.")
    parser.add_argument("--sample", action="store_true", help="Scan only the first route and the first date window.")
    parser.add_argument("--no-telegram", action="store_true", help="Print the summary without sending it.")
    args = parser.parse_args(argv)

    if not args.no_telegram and not args.sample and not all((TOKEN, CHAT_ID)):
        missing = [name for name, value in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID)) if not value]
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    config = load_config()
    max_hours = as_int(config.get("max_total_hours"), 20) or 20
    max_stops = as_int(config.get("max_stops"), 1)
    max_stops = 1 if max_stops is None else max(0, max_stops)
    pairs = find_route_pairs(config.get("routes", []))
    windows = config.get("preferred_windows", [])
    if args.sample:
        pairs = pairs[:1]
        windows = windows[:1]
    today = date.today()
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    cycle = max(2, as_int(config.get("search_cycle_days"), 3) or 3)
    one_way_day = today.toordinal() % cycle == 0
    mode_label = "חד־כיווניות" if one_way_day else "הלוך־חזור"
    print(f"Starting flight scan at {now}; mode={mode_label}; sample={args.sample}")

    deals: list[str] = []
    unverified: list[str] = []
    snapshots: list[tuple[float, str]] = []
    catalog: list[tuple[int, str, tuple]] = []
    errors: list[str] = []
    requests_made = 0
    successes = 0
    scan_count = 0

    for route_index, (outbound, inbound) in enumerate(pairs):
        for window_index, window in enumerate(windows):
            plan = plan_search(window, route_index, window_index, today, one_way_day)
            if not plan:
                print(f"Skipping {outbound['name']} | {window['name']}: window is outside the search horizon")
                continue
            route = outbound
            threshold = as_int(outbound.get("extreme_roundtrip"), 0) or 0
            one_way = False
            if one_way_day:
                route = inbound if (today.toordinal() + route_index + window_index) % 2 else outbound
                threshold = as_int(route.get("extreme_oneway"), 0) or 0
                one_way = True
            scan_count += 1
            try:
                results = scan_market(route, plan, max_hours, max_stops, one_way)
            except Exception as exc:  # noqa: BLE001 - keep the rest of the scan running
                errors.append(f"{route['name']}: {type(exc).__name__}")
                print(f"Scan crashed for {route['name']}: {type(exc).__name__}")
                continue
            offers: list[Offer] = []
            for result in results:
                requests_made += result.requests
                if result.ok and result.requests:
                    successes += 1
                elif result.error:
                    errors.append(f"{result.source} {route['name']}: {result.error}")
                offers.extend(result.offers)
            if not offers:
                continue
            offers.sort(key=lambda item: (item.price, item.duration_hours, sum(item.stops_per_leg)))
            origin = primary_code(route, "origin")
            destination = primary_code(route, "destination")
            best_by_source: dict[str, Offer] = {}
            verified_here = 0
            unverified_here = 0
            for offer in offers:
                best_by_source.setdefault(offer.source, offer)
                identity = (
                    offer.source, offer.price, offer.depart, offer.ret, offer.airline,
                    tuple(offer.leg_durations), tuple(offer.stops_per_leg),
                )
                catalog.append((offer.price, format_flight_line(route["name"], offer, origin, destination), identity))
                status = deal_status(offer, threshold)
                if status == "verified" and verified_here < 2:
                    deals.append(_priced(route, window["name"], offer, origin, destination, threshold,
                                          "🔥 דיל חד־כיווני\n" if one_way else "🔥 דיל הלוך־חזור\n"))
                    verified_here += 1
                elif status == "unverified" and unverified_here < 1:
                    unverified.append(_priced(route, window["name"], offer, origin, destination, threshold,
                                               "🟡 מחיר הלוך־חזור; משך החזור לא נבדק\n"))
                    unverified_here += 1
            for offer in best_by_source.values():
                snapshots.append(_snapshot(route["name"], window["name"], offer, threshold, ""))
            for result in results:
                if result.source in best_by_source or not result.near_miss:
                    continue
                snapshots.append(_snapshot(route["name"], window["name"], result.near_miss, threshold, result.near_miss_reason))

    deals = list(dict.fromkeys(deals))[:8]
    unverified = list(dict.fromkeys(unverified))[:3]
    snapshots.sort(key=lambda item: item[0])
    snapshot_lines = [item[1] for item in snapshots[:8]]
    seen: set[tuple] = set()
    unique_flights: list[tuple[int, str]] = []
    for price, line, identity in sorted(catalog, key=lambda item: (item[0], item[1])):
        if identity in seen:
            continue
        seen.add(identity)
        unique_flights.append((price, line))
    flight_cap = 80
    hidden = max(0, len(unique_flights) - flight_cap)
    flight_lines = [line for _, line in unique_flights[:flight_cap]]
    if hidden:
        flight_lines.append(f"עוד {hidden} טיסות עברו את הסינון ולא נכנסו להודעות.")
    headlines, headline_error = fetch_deal_headlines()
    summary = build_message(
        mode_label, now, scan_count, requests_made, successes, errors,
        deals, unverified, snapshot_lines, headlines, headline_error,
    )
    blocks = [summary]
    if flight_lines:
        blocks.append(f"📋 כל הטיסות שעברו את כללי הזמן והעצירות ({len(unique_flights)}):")
        blocks.extend(flight_lines)
    messages = pack_messages(blocks)
    print("\n\n".join(messages))
    if args.no_telegram or (args.sample and not all((TOKEN, CHAT_ID))):
        print("Telegram send skipped.")
        return 0 if successes else 1
    if send_telegram_messages(messages):
        print(f"Flight scan sent to Telegram in {len(messages)} message(s).")
    else:
        print("Failed to send the flight scan summary to Telegram.")
        return 1
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
