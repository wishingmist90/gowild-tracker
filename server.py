"""GoWild tracker: local web app.  Run:  python server.py   then open http://localhost:8777"""
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import frontier
import rules
import scanner
import store

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
WATCH_PATH = os.path.join(ROOT, "data", "watch.json")
PORT = int(os.environ.get("GOWILD_PORT", "8777"))

with open(os.path.join(ROOT, "data", "airports.json"), encoding="utf-8") as f:
    AIRPORTS = json.load(f)


# ---------------------------------------------------------------- watchlist / auto-refresh

def load_watch():
    try:
        with open(WATCH_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"origins": [], "interval_minutes": 120}


def save_watch(w):
    with open(WATCH_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(w, f, indent=1)
    os.replace(WATCH_PATH + ".tmp", WATCH_PATH)


def watcher():
    """Re-scan watched airports for tomorrow (the date domestic GoWild opens for) on an
    interval, so seat history builds up. Kept to one date per airport to stay polite."""
    last, jobs = {}, {}
    while True:
        w = load_watch()
        every = max(30, int(w.get("interval_minutes", 120))) * 60
        for o in w.get("origins", []):
            j = jobs.get(o)
            if j and j.state == "stopped":   # blocked: retry once the cooldown ends, not in 2 h
                last.pop(o, None)
                jobs.pop(o)
            if time.time() - last.get(o, 0) >= every and not scanner.cooldown_left():
                last[o] = time.time()
                d = date.today() + timedelta(days=1)
                jobs[o], _ = scanner.start(o, d, label=f"auto {o} {d:%a %b %d}")
        time.sleep(60)


# ---------------------------------------------------------------- API

def meta():
    m = frontier.get_markets()
    stations = []
    for code, s in m["stations"].items():
        a = AIRPORTS.get(code)
        if not a:
            continue
        stations.append({**s, "lat": a["lat"], "lon": a["lon"], "airport": a["name"],
                         "routes": m["routes"].get(code, [])})
    today = date.today()
    days = []
    for d in rules.scan_dates(today):
        days.append({"date": d.isoformat(), "ahead": (d - today).days,
                     "blackout": rules.is_blackout(d),
                     "domestic_open": rules.bookable_now(d, True, today),
                     "international_open": rules.bookable_now(d, False, today)})
    return {"stations": sorted(stations, key=lambda s: s["label"]), "days": days,
            "markets_fetched_at": m["fetched_at"], "watch": load_watch(),
            "blackouts_posted_through": rules.BLACKOUTS_POSTED_THROUGH.isoformat()}


def results(origin, day):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    m = frontier.get_markets()
    _, skipped = scanner.plan(origin, d)
    return {"origin": origin, "day": day, "routes": m["routes"].get(origin, []),
            "results": store.latest(origin, day), "skipped": skipped,
            "blackout": rules.is_blackout(d)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name, ctype):
        path = os.path.join(STATIC, name)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if u.path == "/api/meta":
                return self._json(meta())
            if u.path == "/api/results":
                return self._json(results(q["origin"].upper(), q["day"]))
            if u.path == "/api/history":
                return self._json(store.history(q["origin"].upper(), q["dest"].upper(), q["day"]))
            if u.path == "/api/summary":
                return self._json(store.summary(q["origin"].upper()))
            if u.path == "/api/jobs":
                return self._json({"jobs": scanner.jobs(), "cooldown": scanner.cooldown_left()})
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except frontier.FrontierError as e:
            self._json({"error": str(e)}, 502)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/scan":
                day = datetime.strptime(body["day"], "%Y-%m-%d").date()
                dests = [d.upper() for d in body.get("dests") or []] or None
                job, skipped = scanner.start(body["origin"], day, dests, body.get("label", ""))
                return self._json({"job": job.public(), "skipped": skipped})
            if self.path == "/api/watch":
                w = load_watch()
                o = body["origin"].upper()
                if body.get("on"):
                    if o not in w["origins"]:
                        w["origins"].append(o)
                else:
                    w["origins"] = [x for x in w["origins"] if x != o]
                if body.get("interval_minutes"):
                    w["interval_minutes"] = int(body["interval_minutes"])
                save_watch(w)
                return self._json(w)
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except frontier.FrontierError as e:
            self._json({"error": str(e)}, 502)


def main():
    store.init()
    frontier.get_markets()
    threading.Thread(target=watcher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"GoWild tracker on http://localhost:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
