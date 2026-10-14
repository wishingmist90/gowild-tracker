"""Read-only client for Frontier's public booking site.

Everything here comes from the same pages flyfrontier.com serves to a logged-out
visitor. Nothing is guessed: if Frontier doesn't return a field, we store None.

  GetMarkets       every Frontier station + every city pair it sells (incl. connections)
  RetrieveSchedule per-route calendar: dates with no service, last bookable date
  InternalSelect   the flight-select page; its embedded FlightData JSON carries each
                   itinerary's GoWild fare, whether GoWild is open, and seats-left text
"""
import html
import json
import os
import random
import re
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta

from curl_cffi import requests as cffi

BASE = "https://booking.flyfrontier.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MARKETS_PATH = os.path.join(DATA_DIR, "markets.json")
SCHEDULES_PATH = os.path.join(DATA_DIR, "schedules.json")
COOKIES_PATH = os.path.join(DATA_DIR, "cookies.json")
MARKETS_MAX_AGE = 24 * 3600
SCHEDULE_MAX_AGE_DAYS = 3

# Frontier's site sits behind DataDome. It tolerates one steady visitor, not a burst of
# fresh ones, so: one persistent Chrome-fingerprinted session, one request at a time,
# a few seconds apart.
MIN_GAP = 4.0
MAX_GAP = 7.0

# Frontier sells Puerto Rico as domestic (US taxes, day-before GoWild window).
DOMESTIC_COUNTRIES = {"US", "PR", "VI"}


class FrontierError(Exception):
    """Frontier refused or returned something we can't read. `blocked` = bot wall / rate limit."""

    def __init__(self, msg, status=None, blocked=False):
        super().__init__(msg)
        self.status = status
        self.blocked = blocked


_session = None
_net_lock = threading.Lock()
_last_request = 0.0


def _get_session():
    global _session
    if _session is None:
        _session = cffi.Session(impersonate="chrome")
        _session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        try:
            with open(COOKIES_PATH, encoding="utf-8") as f:
                for name, value in json.load(f).items():
                    _session.cookies.set(name, value, domain=".flyfrontier.com")
        except (OSError, ValueError):
            pass
    return _session


def _save_cookies(s):
    try:
        jar = {c.name: c.value for c in s.cookies.jar if c.name in ("datadome",)}
        if jar:
            with open(COOKIES_PATH, "w", encoding="utf-8") as f:
                json.dump(jar, f)
    except Exception:
        pass


def _get(url, timeout=40):
    """Paced GET through the shared session. Serialized: only one request in flight."""
    global _last_request
    with _net_lock:
        wait = _last_request + random.uniform(MIN_GAP, MAX_GAP) - time.time()
        if wait > 0:
            time.sleep(wait)
        s = _get_session()
        try:
            r = s.get(url, timeout=timeout, allow_redirects=True)
        except Exception as e:
            raise FrontierError(f"network error: {e}") from None
        finally:
            _last_request = time.time()
        _save_cookies(s)
    if r.status_code != 200:
        body = (r.text or "")[:500].lower()
        blocked = r.status_code in (403, 405, 406, 429) or "captcha" in body or "datadome" in body
        raise FrontierError(f"HTTP {r.status_code} from Frontier", r.status_code, blocked)
    return r.status_code, r.text


# ---------------------------------------------------------------- markets

_markets_lock = threading.Lock()


def get_markets(force=False):
    """{'stations': {code: {...}}, 'routes': {from: [to,...]}, 'fetched_at': iso}"""
    with _markets_lock:
        if not force and os.path.exists(MARKETS_PATH):
            if time.time() - os.path.getmtime(MARKETS_PATH) < MARKETS_MAX_AGE:
                with open(MARKETS_PATH, encoding="utf-8") as f:
                    return json.load(f)
        try:
            status, body = _get(BASE + "/Resource/GetMarkets")
            raw = json.loads(body.lstrip("﻿"))
        except (FrontierError, ValueError):
            if os.path.exists(MARKETS_PATH):  # stale beats nothing
                with open(MARKETS_PATH, encoding="utf-8") as f:
                    return json.load(f)
            raise
        stations = {}
        for m in raw["marketDetails"]:
            stations[m["stationCode"]] = {
                "code": m["stationCode"],
                "city": m["stationName"],
                "label": m["cityAndCode"],
                "state": m.get("stateCode") or "",
                "country": m["countryCode"],
                "countryName": m["countryHeader"],
                "domestic": m["countryCode"] in DOMESTIC_COUNTRIES,
            }
        routes = {r["fromStation"]: sorted(r["toStations"]) for r in raw["markets"]}
        out = {"stations": stations, "routes": routes,
               "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = MARKETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, MARKETS_PATH)
        return out


# ---------------------------------------------------------------- schedule

_sched_lock = threading.Lock()


def _load_schedules():
    try:
        with open(SCHEDULES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_schedules = _load_schedules()


def get_schedule(origin, dest):
    """Route calendar from Frontier, kept on disk for a few days (it barely changes).
    Returns (disabled_dates:set[date], last_date:date|None)."""
    key = f"{origin}-{dest}"
    with _sched_lock:
        hit = _schedules.get(key)
    fresh_after = (date.today() - timedelta(days=SCHEDULE_MAX_AGE_DAYS)).isoformat()
    if not hit or hit["fetched"] < fresh_after:
        q = urllib.parse.urlencode({"calendarSelectableDays.Origin": origin,
                                    "calendarSelectableDays.Destination": dest})
        _, body = _get(f"{BASE}/Flight/RetrieveSchedule?{q}", timeout=20)
        try:
            cal = json.loads(body)["calendarSelectableDays"]
        except (ValueError, KeyError, TypeError):
            raise FrontierError("unreadable schedule response", blocked=True) from None
        disabled = []
        for s in cal.get("disabledDates") or []:
            try:
                disabled.append(datetime.strptime(s, "%m/%d/%Y").date().isoformat())
            except ValueError:
                pass
        lad = cal.get("lastAvailableDate") or ""
        last = lad[:10] if lad and not lad.startswith("0001") else None
        hit = {"fetched": date.today().isoformat(), "disabled": disabled, "last": last}
        with _sched_lock:
            _schedules[key] = hit
            tmp = SCHEDULES_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_schedules, f)
            os.replace(tmp, SCHEDULES_PATH)
    disabled = {date.fromisoformat(x) for x in hit["disabled"]}
    last = date.fromisoformat(hit["last"]) if hit["last"] else None
    return disabled, last


def route_operates(origin, dest, d):
    disabled, last = get_schedule(origin, dest)
    if last is None:
        return False
    return d <= last and d not in disabled


# ---------------------------------------------------------------- flights

_FLIGHTDATA_RE = re.compile(r"FlightData\s*=\s*'(.*?)';", re.S)


def search_url(origin, dest, d):
    dd = d.strftime("%b %d, %Y")
    q = urllib.parse.urlencode({"o1": origin, "d1": dest, "dd1": dd, "ADT": 1, "mon": "true", "promo": ""})
    return f"{BASE}/Flight/InternalSelect?{q}"


def _price(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if v > 0 else None


def _seats(text):
    """'2 Seats Left!' -> 2. None when Frontier doesn't show a count (means more than a handful)."""
    if not text:
        return None
    m = re.search(r"(\d+)", str(text))
    return int(m.group(1)) if m else None


def get_flights(origin, dest, d):
    """All itineraries Frontier lists for origin->dest on date d, normalized.

    Returns {'flights': [...], 'lowest_gowild': float|None, 'gowild_out_of_range': bool}
    """
    _, page = _get(search_url(origin, dest, d))
    m = _FLIGHTDATA_RE.search(page)
    if not m:
        low = page.lower()
        raise FrontierError("no FlightData on select page", blocked="captcha" in low)
    try:
        data = json.loads(html.unescape(m.group(1)))
    except ValueError:
        raise FrontierError("FlightData was not valid JSON") from None

    flights = []
    for j in data.get("journeys") or []:
        if j.get("isReturnTrip"):
            continue
        for f in j.get("flights") or []:
            legs = f.get("legs") or []
            if not legs:
                continue
            nums = [f"{l.get('carrierCode', 'F9')} {l.get('flightNumber')}" for l in legs]
            gw_on = bool(f.get("isGoWildFareEnabled"))
            gw_price = _price(f.get("goWildFare")) if gw_on else None
            flights.append({
                "key": "+".join(nums) + "@" + legs[0]["departureDate"],
                "flight_numbers": nums,
                "depart": legs[0]["departureDate"],          # local time at origin
                "arrive": legs[-1]["arrivalDate"],           # local time at destination
                "depart_utc": legs[0].get("departureDateUtc"),
                "arrive_utc": legs[-1].get("arrivalDateUtc"),
                "stops": len(legs) - 1,
                "via": [l["arrivalStation"] for l in legs[:-1]],
                "duration": f.get("duration"),
                "next_day_arrival": bool(f.get("isNextDayArrival")),
                "gowild": gw_on and gw_price is not None,
                "gowild_price": gw_price,
                "gowild_seats_text": f.get("goWildFareSeatsRemaining"),
                "gowild_seats": _seats(f.get("goWildFareSeatsRemaining")),
                "standard_price": _price(f.get("standardFare")),
                "discount_den_price": _price(f.get("discountDenFare")),
            })
    flights.sort(key=lambda x: x["depart"])
    return {
        "flights": flights,
        "lowest_gowild": _price(data.get("lowestGWAmount")),
        "gowild_out_of_range": bool(data.get("isGoWildOutOfRange")),
    }
