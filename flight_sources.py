"""Keyless and free flight-price sources for the deal monitor.

Live sources that need no paid plan:
- Google Flights, through the public search page (fast-flights)
- Kiwi.com, through its public search endpoint
- Optional Aviasales cache, through the free Travelpayouts Data API
- Optional SerpAPI fallback, only when direct Google Flights fails
- Public deal headlines from The Flight Deal and Fly4free

Kayak, Skyscanner, Momondo and Wego are linked for a manual check. Their
pages do not return a stable price feed without a paid key.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests

GOOGLE = "Google Flights"
KIWI = "Kiwi.com"
AVIASALES = "Aviasales"
SERPAPI = "Google Flights (SerpAPI)"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
KIWI_ENDPOINT = "https://api.skypicker.com/umbrella/v2/graphql"
AVIASALES_ENDPOINT = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERP_ENDPOINT = "https://serpapi.com/search"
DEAL_FEEDS = (
    ("The Flight Deal", "https://www.theflightdeal.com/feed/"),
    ("Fly4free", "https://www.fly4free.com/feed/"),
)
ASIA_RE = re.compile(
    r"\b(japan|tokyo|osaka|korea|seoul|thailand|bangkok|phuket|nrt|hnd|kix|icn|gmp|bkk|dmk|hkt)\b",
    re.IGNORECASE,
)
ISRAEL_RE = re.compile(r"\b(israel|tel aviv|tel-aviv|tlv)\b", re.IGNORECASE)
FLIGHT_RE = re.compile(
    r"\b(flight|flights|airfare|fare|non-?stop|round-?trip|one-?way|airline)\b|[$€£]\s?\d",
    re.IGNORECASE,
)
MAX_SANE_USD = 10000
DATE_SAMPLES = 5
KIWI_RESULT_LIMIT = 25

try:
    from fast_flights import (
        FlightQuery,
        FlightsNotFound,
        Passengers,
        create_query,
        get_flights,
    )
    HAS_DIRECT_GOOGLE = True
except ImportError:  # pragma: no cover - exercised only when the extra is missing
    FlightQuery = FlightsNotFound = Passengers = create_query = get_flights = None
    HAS_DIRECT_GOOGLE = False


@dataclass
class Offer:
    price: int
    leg_durations: list[float]
    stops_per_leg: list[int]
    airline: str
    link: str
    source: str
    depart: str
    ret: str | None
    return_verified: bool
    note: str = ""
    depart_time: str = ""
    return_time: str = ""

    @property
    def duration_hours(self) -> float:
        if not self.leg_durations:
            return 0.0
        if len(self.leg_durations) == 1:
            return self.leg_durations[0]
        return round(sum(self.leg_durations), 1)


@dataclass
class SearchResult:
    source: str
    offers: list[Offer] = field(default_factory=list)
    ok: bool = False
    error: str | None = None
    requests: int = 0
    raw_count: int = 0
    near_miss: Offer | None = None
    near_miss_reason: str = ""


def airport_codes(route: dict, role: str) -> list[str]:
    """Airport codes for one end of a route, including configured alternates."""
    if role == "origin":
        raw = route.get("google_origin") or route.get("origin") or ""
    else:
        raw = route.get("google_destination") or route.get("destination") or ""
    codes: list[str] = []
    for part in str(raw).split(","):
        code = part.strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes[:2]


def primary_code(route: dict, role: str) -> str:
    codes = airport_codes(route, role)
    fallback = route.get("origin") if role == "origin" else route.get("destination")
    return codes[0] if codes else str(fallback or "").upper()


def money_amount(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            amount = int(round(float(value)))
        else:
            cleaned = re.sub(r"[^0-9.]", "", str(value))
            if not cleaned:
                return None
            amount = int(round(float(cleaned)))
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount > MAX_SANE_USD:
        return None
    return amount


def _minutes_between(start: str, end: str) -> float | None:
    """Positive clock minutes. Valid for a layover at one airport, not a whole international leg."""
    try:
        departure = datetime.fromisoformat(start)
        arrival = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    minutes = (arrival - departure).total_seconds() / 60
    if minutes <= 0:
        return None
    return minutes


def hours_between(start: str, end: str) -> float | None:
    """Clock difference in hours, rounded once. Same-airport layovers only."""
    minutes = _minutes_between(start, end)
    if minutes is None:
        return None
    return round(minutes / 60, 1)


def elapsed_hours(segments: list[dict]) -> float | None:
    """Actual door-to-door hours: airborne time plus layover time at the connecting airport.

    Subtracting departure and arrival local times is wrong across time zones.
    A layover is in one city, so that local gap is real elapsed time.
    Minutes are summed first and rounded once, so a 110-minute connection stays 110.
    """
    if not segments:
        return None
    total_minutes = 0.0
    for index, segment in enumerate(segments):
        airborne = segment.get("duration_minutes")
        try:
            airborne_minutes = float(airborne)
        except (TypeError, ValueError):
            return None
        if airborne_minutes <= 0:
            return None
        total_minutes += airborne_minutes
        if index + 1 < len(segments):
            layover = _minutes_between(segment.get("arrive", ""), segments[index + 1].get("depart", ""))
            if layover is None:
                return None
            total_minutes += layover
    return round(total_minutes / 60, 1)


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def offer_fits(
    offer: Offer,
    max_hours: int,
    max_stops: int,
    window: tuple[date, date] | None = None,
    stay: tuple[int, int] | None = None,
) -> bool:
    """Keep itineraries inside the duration, stop, date and stay rules."""
    if not offer.leg_durations or not offer.stops_per_leg:
        return False
    if len(offer.leg_durations) != len(offer.stops_per_leg):
        return False
    if any(hours <= 0 or hours > max_hours for hours in offer.leg_durations):
        return False
    if any(stops > max_stops for stops in offer.stops_per_leg):
        return False
    depart = _iso_date(offer.depart)
    if depart is None:
        return False
    if window and (depart < window[0] or depart > window[1]):
        return False
    if offer.ret:
        ret = _iso_date(offer.ret)
        if ret is None or ret <= depart:
            return False
        if stay:
            nights = (ret - depart).days
            if nights < stay[0] or nights > stay[1]:
                return False
    return True


def deal_status(offer: Offer, threshold: int) -> str:
    """Return verified, unverified, or none for a threshold."""
    if threshold <= 0 or offer.price >= threshold:
        return "none"
    if offer.ret and not offer.return_verified:
        return "unverified"
    return "verified"


def _dedupe(offers: list[Offer]) -> list[Offer]:
    seen: set[tuple] = set()
    unique: list[Offer] = []
    for offer in sorted(offers, key=lambda item: (item.price, item.duration_hours, item.stops_per_leg)):
        identity = (offer.source, offer.price, offer.depart, offer.ret, offer.airline, tuple(offer.stops_per_leg))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(offer)
    return unique


def _redact(text: str) -> str:
    cleaned = str(text)
    for name in ("SERPAPI_KEY", "TRAVELPAYOUTS_TOKEN", "TELEGRAM_BOT_TOKEN"):
        secret = os.getenv(name)
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:220]


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def sky_date(iso_day: str) -> str:
    year, month, day = iso_day.split("-")
    return f"{year[2:]}{month}{day}"


def comparison_links(origin: str, destination: str, depart: str | None = None, ret: str | None = None) -> list[tuple[str, str]]:
    """Manual-check links for the same route. Dates are included when known."""
    origin = origin.upper()
    destination = destination.upper()
    google_q = f"flights from {origin} to {destination}"
    if depart and ret:
        google_q += f" on {depart} through {ret}"
    elif depart:
        google_q += f" on {depart} one way"
    links = [
        ("Google Flights", f"https://www.google.com/travel/flights?hl=he&curr=USD&q={quote(google_q)}"),
        ("Kiwi", _kiwi_search_link(origin, destination, depart, ret)),
        ("Kayak", _kayak_link(origin, destination, depart, ret)),
        ("Skyscanner", _skyscanner_link(origin, destination, depart, ret)),
        ("Aviasales", _aviasales_search_link(origin, destination, depart, ret)),
        ("Momondo", _momondo_link(origin, destination, depart, ret)),
    ]
    return links


def _kiwi_search_link(origin: str, destination: str, depart: str | None, ret: str | None) -> str:
    base = f"https://www.kiwi.com/en/search/results/{origin.lower()}/{destination.lower()}"
    if depart and ret:
        return f"{base}/{depart}/{ret}"
    if depart:
        return f"{base}/{depart}/no-return"
    return base


def _kayak_link(origin: str, destination: str, depart: str | None, ret: str | None) -> str:
    base = f"https://www.kayak.com/flights/{origin}-{destination}"
    if depart and ret:
        return f"{base}/{depart}/{ret}?sort=price_a"
    if depart:
        return f"{base}/{depart}?sort=price_a"
    return base


def _skyscanner_link(origin: str, destination: str, depart: str | None, ret: str | None) -> str:
    base = f"https://www.skyscanner.co.il/transport/flights/{origin.lower()}/{destination.lower()}"
    if depart and ret:
        return f"{base}/{sky_date(depart)}/{sky_date(ret)}/"
    if depart:
        return f"{base}/{sky_date(depart)}/"
    return base + "/"


def _aviasales_search_link(origin: str, destination: str, depart: str | None, ret: str | None) -> str:
    if depart and ret:
        leave = datetime.fromisoformat(depart)
        back = datetime.fromisoformat(ret)
        code = f"{origin}{leave:%d%m}{destination}{back:%d%m}1"
        return f"https://www.aviasales.com/search/{code}"
    if depart:
        leave = datetime.fromisoformat(depart)
        return f"https://www.aviasales.com/search/{origin}{leave:%d%m}{destination}1"
    return f"https://www.aviasales.com/routes/{origin.lower()}/{destination.lower()}"


def _momondo_link(origin: str, destination: str, depart: str | None, ret: str | None) -> str:
    base = f"https://www.momondo.com/flight-search/{origin}-{destination}"
    if depart and ret:
        return f"{base}/{depart}/{ret}?sort=price_a"
    if depart:
        return f"{base}/{depart}?sort=price_a"
    return base


def explain_reject(
    offer: Offer,
    max_hours: int,
    max_stops: int,
    window: tuple[date, date] | None = None,
    stay: tuple[int, int] | None = None,
) -> str | None:
    """Short Hebrew reason when an offer misses the trip rules."""
    if offer_fits(offer, max_hours, max_stops, window, stay):
        return None
    reasons: list[str] = []
    if any(hours > max_hours for hours in offer.leg_durations):
        reasons.append(f"משך מעל {max_hours} ש׳")
    if any(stops > max_stops for stops in offer.stops_per_leg):
        reasons.append(f"יותר מ-{max_stops} עצירות לכיוון")
    depart = _iso_date(offer.depart)
    ret = _iso_date(offer.ret) if offer.ret else None
    if depart and ret and stay:
        nights = (ret - depart).days
        if nights < stay[0] or nights > stay[1]:
            reasons.append(f"שהייה של {nights} ימים")
    elif depart and window and (depart < window[0] or depart > window[1]):
        reasons.append("תאריך מחוץ לחלון")
    return ", ".join(reasons) if reasons else "מחוץ לכללים"


def _finish(
    result: SearchResult,
    raw: list[Offer],
    max_hours: int,
    max_stops: int,
    window: tuple[date, date] | None,
    stay: tuple[int, int] | None,
) -> SearchResult:
    result.raw_count = len(raw)
    result.offers = _keep(raw, max_hours, max_stops, window, stay)
    if not result.offers and raw:
        rejected = []
        for offer in raw:
            reason = explain_reject(offer, max_hours, max_stops, window, stay)
            if reason:
                rejected.append((offer, reason))
        if rejected:
            offer, reason = min(rejected, key=lambda item: item[0].price)
            result.near_miss = offer
            result.near_miss_reason = reason
    return result


def _keep(
    offers: list[Offer],
    max_hours: int,
    max_stops: int,
    window: tuple[date, date] | None,
    stay: tuple[int, int] | None,
) -> list[Offer]:
    kept = [offer for offer in offers if offer_fits(offer, max_hours, max_stops, window, stay)]
    return _dedupe(kept)


def departure_samples(window: tuple[date, date], count: int = 3) -> list[date]:
    """Spread a few departure days across the window so each Kiwi search has a valid stay."""
    start, end = window
    span = (end - start).days
    if span <= 0 or count <= 1:
        return [start]
    count = min(count, span + 1)
    return [start + timedelta(days=round(span * index / (count - 1))) for index in range(count)]


def search_google(
    origins: list[str],
    destinations: list[str],
    depart: str,
    ret: str | None,
    max_hours: int,
    max_stops: int,
    window: tuple[date, date],
    stay: tuple[int, int] | None,
) -> SearchResult:
    """Several dates across the window. Round-trip prices do not include the return leg."""
    result = SearchResult(source=GOOGLE)
    if not HAS_DIRECT_GOOGLE:
        result.error = "fast-flights is not installed"
        return result
    failures: list[str] = []
    raw: list[Offer] = []
    succeeded = False
    for leave, back in _google_trips(window, depart, ret):
        for origin in origins:
            for destination in destinations:
                result.requests += 1
                try:
                    offers = _google_pair(origin, destination, leave, back, max_stops, max_hours)
                except Exception as exc:  # noqa: BLE001 - one blocked airport must not stop the scan
                    failures.append(f"{origin}-{destination} {leave}: {type(exc).__name__}")
                    print(f"Google Flights {origin}-{destination} {leave} failed: {type(exc).__name__}")
                    continue
                succeeded = True
                raw.extend(offers)
                time.sleep(0.6)
    _finish(result, raw, max_hours, max_stops, window, stay if ret else None)
    result.ok = succeeded
    if not succeeded:
        result.error = "; ".join(failures)[:220] or "Google Flights returned no response"
    return result


def _google_trips(window: tuple[date, date], depart: str, ret: str | None) -> list[tuple[str, str | None]]:
    """Spread Google across the window. The return date keeps the planned stay length."""
    stay_days = None
    if ret:
        stay_days = (date.fromisoformat(ret) - date.fromisoformat(depart)).days
        if stay_days < 1:
            stay_days = None
    trips: list[tuple[str, str | None]] = []
    for day in departure_samples(window, DATE_SAMPLES):
        back = None if stay_days is None else (day + timedelta(days=stay_days)).isoformat()
        trips.append((day.isoformat(), back))
    return trips


def _clock(stamp: str) -> str:
    return stamp[11:16] if len(stamp) >= 16 and stamp[10] == "T" else ""


def _google_pair(origin: str, destination: str, depart: str, ret: str | None, max_stops: int, max_hours: int) -> list[Offer]:
    legs = [FlightQuery(date=depart, from_airport=origin, to_airport=destination, max_stops=max_stops, max_duration_minutes=max_hours * 60)]
    trip = "one-way"
    if ret:
        trip = "round-trip"
        legs.append(FlightQuery(date=ret, from_airport=destination, to_airport=origin, max_stops=max_stops, max_duration_minutes=max_hours * 60))
    query = create_query(
        flights=legs,
        trip=trip,
        seat="economy",
        passengers=Passengers(adults=1),
        language="en",
        currency="USD",
        max_stops=max_stops,
        hide_separate_and_self_transfer=True,
    )
    try:
        found = get_flights(query)
    except FlightsNotFound:
        return []
    except (TypeError, IndexError, KeyError, ValueError):
        # Google sometimes returns an empty script payload for one airport.
        return []
    offers: list[Offer] = []
    for flight in found:
        offer = _offer_from_google(flight, query.url(), ret)
        if offer:
            offers.append(offer)
    return offers


def _offer_from_google(flight: object, link: str, queried_return: str | None) -> Offer | None:
    segments = []
    for segment in getattr(flight, "flights", []) or []:
        departure = _point_stamp(segment.departure)
        arrival = _point_stamp(segment.arrival)
        if not departure or not arrival:
            return None
        segments.append({
            "depart": departure,
            "arrive": arrival,
            "origin": segment.from_airport.code,
            "destination": segment.to_airport.code,
            "duration_minutes": getattr(segment, "duration", None),
        })
    airlines = [name for name in (getattr(flight, "airlines", None) or []) if name]
    return parse_google_segments(segments, getattr(flight, "price", None), airlines, link, queried_return)


def _point_stamp(point: object) -> str | None:
    try:
        year, month, day = point.date
        hour, minute = point.time
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T{int(hour):02d}:{int(minute):02d}:00"
    except (TypeError, ValueError):
        return None


def parse_google_segments(
    segments: list[dict],
    price: object,
    airlines: list[str],
    link: str,
    queried_return: str | None,
) -> Offer | None:
    amount = money_amount(price)
    if amount is None or not segments:
        return None
    elapsed = elapsed_hours(segments)
    depart = segments[0]["depart"][:10]
    if elapsed is None or not _iso_date(depart):
        return None
    names: list[str] = []
    for name in airlines:
        if name and name not in names:
            names.append(name)
    # Google's round-trip page prices the selected outbound only. The return
    # itinerary is not in this response, so that price stays unverified.
    return Offer(
        price=amount,
        leg_durations=[elapsed],
        stops_per_leg=[max(0, len(segments) - 1)],
        airline=", ".join(names) if names else "לא צוין",
        link=link,
        source=GOOGLE,
        depart=depart,
        ret=queried_return,
        return_verified=queried_return is None,
        depart_time=_clock(segments[0]["depart"]),
    )


def search_kiwi(
    origins: list[str],
    destinations: list[str],
    window: tuple[date, date],
    stay: tuple[int, int] | None,
    max_hours: int,
    max_stops: int,
) -> SearchResult:
    """Kiwi prices across the window.

    A wide departure range lets short trips crowd out valid stays, so round
    trips are searched on a few single departure days with a matching return range.
    """
    result = SearchResult(source=KIWI)
    slices = [(day, day) for day in departure_samples(window, DATE_SAMPLES)] if stay else [window]
    raw: list[Offer] = []
    errors: list[str] = []
    for narrow in slices:
        offers, error, calls = _kiwi_with_fallback(origins, destinations, narrow, stay, max_stops)
        result.requests += calls
        if error and not offers:
            errors.append(error)
            continue
        raw.extend(offers)
    if not raw and errors:
        result.error = errors[0]
        return result
    _finish(result, raw, max_hours, max_stops, window, stay)
    for offer in result.offers:
        if not offer.link:
            offer.link = _kiwi_search_link(origins[0], destinations[0], offer.depart, offer.ret)
    if result.near_miss and not result.near_miss.link:
        result.near_miss.link = _kiwi_search_link(origins[0], destinations[0], result.near_miss.depart, result.near_miss.ret)
    result.ok = True
    return result


def _kiwi_with_fallback(
    origins: list[str],
    destinations: list[str],
    window: tuple[date, date],
    stay: tuple[int, int] | None,
    max_stops: int,
) -> tuple[list[Offer], str | None, int]:
    payloads = [(origins, destinations)]
    if len(origins) > 1 or len(destinations) > 1:
        payloads.append((origins[:1], destinations[:1]))
    last_error = "no response"
    calls = 0
    for attempt_origins, attempt_destinations in payloads:
        calls += 1
        try:
            offers, error = _kiwi_request(attempt_origins, attempt_destinations, window, stay, max_stops)
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            continue
        if error:
            last_error = error
            continue
        return offers, None, calls
    return [], last_error, calls


def _kiwi_request(
    origins: list[str],
    destinations: list[str],
    window: tuple[date, date],
    stay: tuple[int, int] | None,
    max_stops: int,
) -> tuple[list[Offer], str | None]:
    itinerary: dict = {
        "source": {"ids": origins},
        "destination": {"ids": destinations},
        "outboundDepartureDate": {
            "start": f"{window[0].isoformat()}T00:00:00",
            "end": f"{window[1].isoformat()}T23:59:59",
        },
    }
    one_way = stay is None
    if stay:
        ret_start = window[0] + timedelta(days=stay[0])
        ret_end = window[1] + timedelta(days=stay[1])
        itinerary["inboundDepartureDate"] = {
            "start": f"{ret_start.isoformat()}T00:00:00",
            "end": f"{ret_end.isoformat()}T23:59:59",
        }
    variables = {
        "search": {
            "itinerary": itinerary,
            "passengers": {
                "adults": 1,
                "children": 0,
                "infants": 0,
                "adultsHoldBags": 0,
                "adultsHandBags": 0,
                "childrenHoldBags": [],
                "childrenHandBags": [],
            },
            "cabinClass": {"cabinClass": "ECONOMY", "applyMixedClasses": False},
        },
        "filter": {
            "allowChangeInboundDestination": True,
            "allowChangeInboundSource": True,
            "allowDifferentStationConnection": True,
            "enableSelfTransfer": False,
            "enableThrowAwayTicketing": False,
            "enableTrueHiddenCity": False,
            "transportTypes": ["FLIGHT"],
            "contentProviders": ["KIWI"],
            "flightsApiLimit": KIWI_RESULT_LIMIT,
            "limit": KIWI_RESULT_LIMIT,
            "maxStopsCount": max_stops,
        },
        "options": {
            "sortBy": "PRICE",
            "mergePriceDiffRule": "INCREASED",
            "currency": "USD",
            "locale": "en",
            "partner": "skypicker",
            "affilID": "skypicker",
            "storeSearch": False,
            "searchStrategy": "REDUCED",
        },
    }
    feature = "SearchOneWayItinerariesQuery" if one_way else "SearchReturnItinerariesQuery"
    query = _KIWI_ONE_WAY if one_way else _KIWI_ROUND_TRIP
    response = _session().post(
        KIWI_ENDPOINT,
        params={"featureName": feature},
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json", "Origin": "https://www.kiwi.com", "Referer": "https://www.kiwi.com/"},
        timeout=55,
    )
    try:
        payload = response.json()
    except ValueError:
        return [], f"HTTP {response.status_code}"
    if not response.ok:
        return [], f"HTTP {response.status_code}"
    if payload.get("errors"):
        message = "; ".join(str(item.get("message") or item) for item in payload["errors"][:2])
        return [], message[:180]
    node_name = "onewayItineraries" if one_way else "returnItineraries"
    node = (payload.get("data") or {}).get(node_name) or {}
    if node.get("__typename") == "AppError":
        return [], str(node.get("error") or "Kiwi search failed")[:180]
    offers: list[Offer] = []
    for item in node.get("itineraries") or []:
        if not isinstance(item, dict):
            continue
        offer = parse_kiwi_itinerary(item, one_way=one_way)
        if offer:
            offers.append(offer)
    return offers, None


def parse_kiwi_itinerary(item: dict, one_way: bool) -> Offer | None:
    amount = money_amount((item.get("price") or {}).get("amount"))
    if amount is None:
        return None
    if one_way:
        legs = [_kiwi_leg(item.get("sector") or {})]
        ret = None
        verified = True
    else:
        legs = [_kiwi_leg(item.get("outbound") or {}), _kiwi_leg(item.get("inbound") or {})]
        inbound = legs[1]
        ret = inbound["depart"] if inbound else None
        verified = True
    if any(leg is None for leg in legs):
        return None
    depart = legs[0]["depart"]
    if not depart:
        return None
    return Offer(
        price=amount,
        leg_durations=[leg["hours"] for leg in legs],
        stops_per_leg=[leg["stops"] for leg in legs],
        airline=legs[0]["airline"] if one_way else _join_airlines(legs),
        link=_kiwi_booking_link(item),
        source=KIWI,
        depart=depart,
        ret=ret,
        return_verified=verified,
        depart_time=legs[0].get("clock") or "",
        return_time="" if one_way or legs[1] is None else (legs[1].get("clock") or ""),
    )


def _kiwi_leg(sector: dict) -> dict | None:
    parsed = []
    for wrapper in sector.get("sectorSegments") or []:
        segment = (wrapper or {}).get("segment") or {}
        source = segment.get("source") or {}
        destination = segment.get("destination") or {}
        start = (source.get("localTime") or "")[:19]
        end = (destination.get("localTime") or "")[:19]
        if not start or not end:
            return None
        carrier = ((segment.get("carrier") or {}).get("name") or "").strip()
        parsed.append({
            "depart": start,
            "arrive": end,
            "carrier": carrier,
            "duration_minutes": _kiwi_minutes(segment.get("duration")),
        })
    if not parsed:
        return None
    sector_minutes = _kiwi_minutes(sector.get("duration"))
    if sector_minutes:
        elapsed = round(sector_minutes / 60, 1)
    else:
        elapsed = elapsed_hours(parsed)
    if elapsed is None:
        return None
    names = []
    for segment in parsed:
        if segment["carrier"] and segment["carrier"] not in names:
            names.append(segment["carrier"])
    return {
        "depart": parsed[0]["depart"][:10],
        "clock": _clock(parsed[0]["depart"]),
        "hours": elapsed,
        "stops": max(0, len(parsed) - 1),
        "airline": ", ".join(names) if names else "לא צוין",
    }


def _kiwi_minutes(value: object) -> float | None:
    """Kiwi durations are seconds."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds / 60


def _join_airlines(legs: list[dict]) -> str:
    names: list[str] = []
    for leg in legs:
        for part in str(leg.get("airline") or "").split(","):
            name = part.strip()
            if name and name not in names and name != "לא צוין":
                names.append(name)
    return ", ".join(names) if names else "לא צוין"


def _kiwi_booking_link(item: dict) -> str:
    edges = ((item.get("bookingOptions") or {}).get("edges") or [])
    url = ""
    if edges and isinstance(edges[0], dict):
        url = str(((edges[0].get("node") or {}).get("bookingUrl") or ""))
    if url.startswith("/"):
        return "https://www.kiwi.com" + url
    if url.startswith("http"):
        return url
    return ""


_KIWI_SEGMENT = """
segment {
  duration
  carrier { name }
  source { station { code } localTime }
  destination { station { code } localTime }
}
"""

_KIWI_LEG = f"""
duration
sectorSegments {{ {_KIWI_SEGMENT} }}
"""

_KIWI_ONE_WAY = f"""
query SearchOneWayItinerariesQuery($search: SearchOnewayInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput) {{
  onewayItineraries(search: $search, filter: $filter, options: $options) {{
    __typename
    ... on AppError {{ error: message }}
    ... on Itineraries {{
      itineraries {{
        ... on ItineraryOneWay {{
          price {{ amount }}
          sector {{ {_KIWI_LEG} }}
          bookingOptions {{ edges {{ node {{ bookingUrl }} }} }}
        }}
      }}
    }}
  }}
}}
"""

_KIWI_ROUND_TRIP = f"""
query SearchReturnItinerariesQuery($search: SearchReturnInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput) {{
  returnItineraries(search: $search, filter: $filter, options: $options) {{
    __typename
    ... on AppError {{ error: message }}
    ... on Itineraries {{
      itineraries {{
        ... on ItineraryReturn {{
          price {{ amount }}
          outbound {{ {_KIWI_LEG} }}
          inbound {{ {_KIWI_LEG} }}
          bookingOptions {{ edges {{ node {{ bookingUrl }} }} }}
        }}
      }}
    }}
  }}
}}
"""


def aviasales_configured() -> bool:
    return bool(os.getenv("TRAVELPAYOUTS_TOKEN"))


def search_aviasales(
    origins: list[str],
    destinations: list[str],
    window: tuple[date, date],
    stay: tuple[int, int] | None,
    max_hours: int,
    max_stops: int,
) -> SearchResult:
    """Cached Aviasales fares for the window. Requires a free Travelpayouts token."""
    result = SearchResult(source=AVIASALES)
    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if not token:
        result.ok = True
        return result
    one_way = stay is None
    use_ru = False
    raw: list[Offer] = []
    failures: list[str] = []
    currency_problem = False
    for origin in origins:
        for destination in destinations:
            for depart_month in _months(window[0], window[1]):
                return_months = [None] if one_way else _return_months(depart_month)
                for return_month in return_months:
                    batch, problem = _aviasales_attempt(
                        result, token, origin, destination, depart_month, return_month, one_way, "il",
                    )
                    if problem == "currency":
                        currency_problem = True
                        batch = []
                    elif problem:
                        failures.append(problem)
                        batch = []
                    if not batch and not problem:
                        use_ru = True
                    if not batch and use_ru:
                        ru_batch, ru_problem = _aviasales_attempt(
                            result, token, origin, destination, depart_month, return_month, one_way, "ru",
                        )
                        if ru_problem == "currency":
                            currency_problem = True
                        elif ru_problem:
                            failures.append(ru_problem)
                        else:
                            batch = ru_batch
                    raw.extend(batch)
                    time.sleep(0.15)
    if currency_problem and not raw:
        result.error = "Aviasales did not return USD prices"
        return result
    _finish(result, raw, max_hours, max_stops, window, stay)
    if raw or not failures:
        result.ok = True
        return result
    result.error = failures[0]
    return result


def _aviasales_attempt(
    result: SearchResult,
    token: str,
    origin: str,
    destination: str,
    depart_month: str,
    return_month: str | None,
    one_way: bool,
    market: str,
) -> tuple[list[Offer], str | None]:
    result.requests += 1
    try:
        batch, _, problem = _aviasales_page(
            token, origin, destination, depart_month, return_month, one_way, market,
        )
    except requests.RequestException as exc:
        return [], type(exc).__name__
    return batch, problem


def _months(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month) and len(months) < 6:
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def _return_months(depart_month: str) -> list[str]:
    year, month = (int(part) for part in depart_month.split("-"))
    month += 1
    if month == 13:
        month = 1
        year += 1
    return [depart_month, f"{year:04d}-{month:02d}"]


def _aviasales_page(
    token: str,
    origin: str,
    destination: str,
    depart_month: str,
    return_month: str | None,
    one_way: bool,
    market: str,
) -> tuple[list[Offer], str, str | None]:
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart_month,
        "cy": "usd",
        "currency": "usd",
        "limit": 100,
        "page": 1,
        "sorting": "price",
        "direct": "false",
        "one_way": "true" if one_way else "false",
        "market": market,
        "unique": "false",
    }
    if return_month and not one_way:
        params["return_at"] = return_month
    response = _session().get(
        AVIASALES_ENDPOINT,
        params=params,
        headers={"X-Access-Token": token, "Accept-Encoding": "gzip"},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        return [], market, f"HTTP {response.status_code}"
    if response.status_code in (401, 403):
        return [], market, "token was rejected"
    if not response.ok:
        return [], market, f"HTTP {response.status_code}"
    if not isinstance(payload, dict) or payload.get("success") is False:
        return [], market, str(payload.get("error") or f"HTTP {response.status_code}")[:180]
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = list(data.values())
    offers: list[Offer] = []
    bad_currency = False
    for item in data:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or payload.get("currency") or "").upper()
        if currency and currency != "USD":
            bad_currency = True
            continue
        offer = parse_aviasales_offer(item, one_way=one_way)
        if offer:
            offers.append(offer)
    if bad_currency and not offers:
        return [], market, "currency"
    return offers, market, None


def parse_aviasales_offer(item: dict, one_way: bool) -> Offer | None:
    amount = money_amount(item.get("price"))
    depart = str(item.get("departure_at") or "")[:10]
    if amount is None or not _iso_date(depart):
        return None
    try:
        outbound_stops = int(item.get("transfers"))
    except (TypeError, ValueError):
        return None
    outbound_minutes = _minutes(item.get("duration_to") if not one_way else (item.get("duration_to") or item.get("duration")))
    if one_way and outbound_minutes is None:
        outbound_minutes = _minutes(item.get("duration"))
    if outbound_minutes is None:
        return None
    durations = [round(outbound_minutes / 60, 1)]
    stops = [max(0, outbound_stops)]
    ret = None
    if not one_way:
        ret = str(item.get("return_at") or "")[:10]
        if not _iso_date(ret):
            return None
        try:
            return_stops = int(item.get("return_transfers"))
        except (TypeError, ValueError):
            return None
        return_minutes = _minutes(item.get("duration_back"))
        if return_minutes is None:
            return None
        durations.append(round(return_minutes / 60, 1))
        stops.append(max(0, return_stops))
    airline = str(item.get("airline") or "").strip()
    number = str(item.get("flight_number") or "").strip()
    if airline and number:
        airline = f"{airline} {number}"
    link = str(item.get("link") or "")
    if link.startswith("/"):
        link = "https://www.aviasales.com" + link
    elif link and not link.startswith("http"):
        link = "https://www.aviasales.com/" + link.lstrip("/")
    return Offer(
        price=amount,
        leg_durations=durations,
        stops_per_leg=stops,
        airline=airline or "לא צוין",
        link=link,
        source=AVIASALES,
        depart=depart,
        ret=ret,
        return_verified=True,
        note="מחיר ממטמון Aviasales, לא חיפוש חי",
    )


def _minutes(value: object) -> int | None:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return minutes


def serpapi_configured() -> bool:
    return bool(os.getenv("SERPAPI_KEY"))


def search_serpapi(
    origins: list[str],
    destinations: list[str],
    depart: str,
    ret: str | None,
    max_hours: int,
    max_stops: int,
    window: tuple[date, date],
    stay: tuple[int, int] | None,
) -> SearchResult:
    """Paid fallback used only when direct Google Flights fails and a key exists."""
    result = SearchResult(source=SERPAPI)
    key = os.getenv("SERPAPI_KEY")
    if not key:
        result.ok = True
        return result
    params = {
        "engine": "google_flights",
        "departure_id": ",".join(origins),
        "arrival_id": ",".join(destinations),
        "outbound_date": depart,
        "currency": "USD",
        "hl": "en",
        "gl": "il",
        "api_key": key,
        "travel_class": "1",
        "adults": "1",
        "sort_by": "2",
        "show_hidden": "true",
        "max_duration": str(max_hours * 60),
        # SerpAPI: 1 = nonstop, 2 = one stop or fewer.
        "stops": "2" if max_stops >= 1 else "1",
        "type": "2" if ret is None else "1",
    }
    if ret:
        params["return_date"] = ret
        params["deep_search"] = "true"
    result.requests += 1
    try:
        response = _session().get(SERP_ENDPOINT, params=params, timeout=45)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        result.error = type(exc).__name__
        return result
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, str) and "hasn't returned any results" in error.lower():
        result.ok = True
        return result
    if not response.ok or error or not isinstance(payload, dict):
        result.error = _redact(str(error or f"HTTP {response.status_code}"))
        return result
    raw = parse_serpapi_payload(payload, ret)
    _finish(result, raw, max_hours, max_stops, window, stay if ret else None)
    result.ok = True
    return result


def parse_serpapi_payload(data: dict, queried_return: str | None) -> list[Offer]:
    raw_items: list[dict] = []
    for key in ("best_flights", "other_flights", "flights"):
        entries = data.get(key, [])
        if isinstance(entries, list):
            raw_items.extend(item for item in entries if isinstance(item, dict))
    link = str((data.get("search_metadata") or {}).get("google_flights_url") or "")
    offers: list[Offer] = []
    for item in raw_items:
        offer = _serpapi_offer(item, link, queried_return)
        if offer:
            offers.append(offer)
    return offers


def _serpapi_offer(item: dict, link: str, queried_return: str | None) -> Offer | None:
    amount = money_amount(item.get("price"))
    segments = item.get("flights") if isinstance(item.get("flights"), list) else []
    duration = _minutes(item.get("total_duration"))
    layovers = item.get("layovers")
    if duration is None:
        duration = sum(_minutes(segment.get("duration")) or 0 for segment in segments)
        if isinstance(layovers, list):
            duration += sum(_minutes(layover.get("duration")) or 0 for layover in layovers)
        if duration <= 0:
            duration = None
    if amount is None or duration is None:
        return None
    if isinstance(layovers, list):
        stops = len(layovers)
    else:
        stops = max(0, len(segments) - 1)
    airlines: list[str] = []
    depart = ""
    for segment in segments:
        airline = segment.get("airline")
        if airline and airline not in airlines:
            airlines.append(str(airline))
        if not depart:
            depart = str(segment.get("departure_airport", {}).get("time") or "")[:10]
    if not _iso_date(depart):
        depart = str(item.get("departure_time") or "")[:10]
    if not _iso_date(depart):
        return None
    return Offer(
        price=amount,
        leg_durations=[round(duration / 60, 1)],
        stops_per_leg=[stops],
        airline=", ".join(airlines) if airlines else "לא צוין",
        link=link,
        source=SERPAPI,
        depart=depart,
        ret=queried_return,
        return_verified=queried_return is None,
    )


def fetch_deal_headlines(limit: int = 4) -> tuple[list[dict], str | None]:
    """Headlines from free public deal feeds. These are not personal fare quotes."""
    headlines: list[dict] = []
    failures = 0
    session = _session()
    for source, url in DEAL_FEEDS:
        try:
            response = session.get(url, timeout=25, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            response.raise_for_status()
            headlines.extend(parse_feed_xml(response.text, source))
        except (requests.RequestException, ET.ParseError, ValueError):
            failures += 1
    picked = select_headlines(headlines, limit)
    error = "deal feeds were unavailable" if failures == len(DEAL_FEEDS) else None
    return picked, error


def parse_feed_xml(xml_text: str, source: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        if _tag(node) != "item":
            continue
        title = ""
        link = ""
        description = ""
        for child in list(node):
            name = _tag(child)
            text = "".join(child.itertext()).strip()
            if name == "title" and not title:
                title = re.sub(r"\s+", " ", text)
            elif name == "link" and not link:
                link = text
            elif name in ("description", "encoded") and not description:
                description = re.sub(r"<[^>]+>", " ", text)
                description = re.sub(r"\s+", " ", description).strip()
        if title and link:
            items.append({"source": source, "title": title[:180], "link": link, "description": description[:500]})
    return items


def select_headlines(items: list[dict], limit: int) -> list[dict]:
    picked: list[dict] = []
    seen: set[str] = set()
    asia_only = 0
    for item in items:
        match = match_deal(item.get("title", ""), item.get("description", ""))
        if not match:
            continue
        key = item.get("link") or item.get("title")
        if key in seen:
            continue
        if match == "asia":
            if asia_only >= 2:
                continue
            asia_only += 1
        seen.add(key)
        picked.append({**item, "relevance": match})
        if len(picked) >= limit:
            break
    return picked


def match_deal(title: str, description: str = "") -> str | None:
    text = f"{title} {description}"
    if not ASIA_RE.search(text) or not FLIGHT_RE.search(text):
        return None
    if ISRAEL_RE.search(text):
        return "israel"
    return "asia"


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()
