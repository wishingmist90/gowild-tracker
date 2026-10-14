# GoWild Tracker

Map of every Frontier destination from an airport, with live GoWild seat availability,
pass prices, flight times, and seat history. All data comes from flyfrontier.com.

## Run

Double-click `start.bat`, or:

    python server.py        # then open http://localhost:8777

Needs Python 3.11+ and `pip install -r requirements.txt`.

## How it works

| Frontier source | What we use it for |
|---|---|
| `booking.flyfrontier.com/Resource/GetMarkets` | All 123 stations and every city pair Frontier sells (cached 24 h) |
| `booking.flyfrontier.com/Flight/InternalSelect` | The flight-select page. Its embedded `FlightData` JSON has each itinerary's GoWild fare, whether GoWild is open, and the "N seats left" text |

- **Price shown** is Frontier's own GoWild price for that itinerary: the $0.01 fare plus taxes
  and fees (about $15.41 nonstop domestic, more with connections, $85+ international).
- **Windows**: domestic GoWild opens the day before departure; international 10 days before.
  The date picker marks which dates are bookable. Blackout dates come from Frontier's
  GoWild page and live in `rules.py` (posted through April 2027; add new ones there).
- **Tracking**: every check is saved in `data/gowild.db`. The flight list shows when each
  GoWild seat was first seen open, the price range seen, and seats that have since closed.
  Turn on **Auto-track** for an airport to re-check tomorrow's flights every 2 hours while
  the app runs.
- **Return trips**: open a destination and pick a return date; it checks that single route.

## Rate limits

Frontier's site is behind DataDome bot protection. Bursts of requests get the IP blocked
(HTTP 406) for a while. The tracker therefore sends one request at a time, 4-7 seconds
apart, from one persistent session, so a full airport (~90 routes) takes 6-10 minutes.
If Frontier refuses a request, the scan stops and waits 30 minutes before allowing another.

## Files

- `frontier.py` reads Frontier's site
- `scanner.py` runs scans, pacing and block handling
- `store.py` SQLite history
- `rules.py` GoWild windows and blackout dates
- `server.py` local web server and API
- `static/index.html` the map UI
- `data/airports.json` airport coordinates (OurAirports)
