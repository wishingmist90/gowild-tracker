"""GoWild pass rules, from flyfrontier.com/deals/gowild-pass (read 2026-10-14).

Domestic (US + Puerto Rico): bookable the day before departure (and same day).
International: bookable from 10 days before departure.
Blackout dates: no GoWild fares at all. Frontier posts them per pass period;
2027 dates after April are "not yet posted" - update BLACKOUTS when they are.
"""
from datetime import date, timedelta

DOMESTIC_DAYS_AHEAD = 1
INTERNATIONAL_DAYS_AHEAD = 10


def _expand(year, spec):
    out = set()
    for month, days in spec.items():
        for part in days.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-")
                rng = range(int(a), int(b) + 1)
            else:
                rng = [int(part)]
            out.update(date(year, month, d) for d in rng)
    return out


BLACKOUTS = (
    _expand(2026, {1: "1,3-4,15-16,19", 2: "12-13,16", 3: "13-15,20-22,27-29", 4: "3-6,10-12",
                   5: "21-22,25", 6: "25-28", 7: "2-6", 9: "3-4,7", 10: "8-9,11-12",
                   11: "24-25,28-30", 12: "19-24,26-31"})
    | _expand(2027, {1: "1-3,14-15,18", 2: "11-12,15", 3: "12-14,19-21,26-29", 4: "2-4"})
)
BLACKOUTS_POSTED_THROUGH = date(2027, 4, 30)


def is_blackout(d):
    return d in BLACKOUTS


def bookable_now(d, domestic, today=None):
    """True if a pass holder could book a GoWild seat on date d right now (ignoring seat inventory)."""
    today = today or date.today()
    ahead = (d - today).days
    if ahead < 0 or is_blackout(d):
        return False
    return ahead <= (DOMESTIC_DAYS_AHEAD if domestic else INTERNATIONAL_DAYS_AHEAD)


def scan_dates(today=None):
    """Dates worth scanning: today .. today+10 (domestic only matters for the first two)."""
    today = today or date.today()
    return [today + timedelta(days=i) for i in range(INTERNATIONAL_DAYS_AHEAD + 1)]
