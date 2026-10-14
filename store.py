"""SQLite history. Every check of a route is kept, so we can show when a GoWild seat
first appeared, when it disappeared, and how the price moved."""
import json
import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gowild.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY,
  origin TEXT NOT NULL, dest TEXT NOT NULL, day TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  status TEXT NOT NULL,           -- ok | no_service | error
  error TEXT,
  n_flights INTEGER, n_gowild INTEGER, min_gowild REAL
);
CREATE INDEX IF NOT EXISTS checks_odd ON checks(origin, day, dest, checked_at);
CREATE TABLE IF NOT EXISTS flights (
  check_id INTEGER NOT NULL REFERENCES checks(id),
  origin TEXT NOT NULL, dest TEXT NOT NULL, day TEXT NOT NULL,
  key TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  gowild INTEGER NOT NULL,
  gowild_price REAL, gowild_seats INTEGER, gowild_seats_text TEXT,
  standard_price REAL, discount_den_price REAL,
  data TEXT NOT NULL              -- full normalized flight JSON
);
CREATE INDEX IF NOT EXISTS flights_check ON flights(check_id);
CREATE INDEX IF NOT EXISTS flights_track ON flights(origin, dest, day, key, checked_at);
"""


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record(origin, dest, day, status, result=None, error=None):
    flights = (result or {}).get("flights", [])
    gw = [f for f in flights if f["gowild"]]
    ts = now_iso()
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO checks(origin,dest,day,checked_at,status,error,n_flights,n_gowild,min_gowild)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (origin, dest, day, ts, status, error, len(flights), len(gw),
             min((f["gowild_price"] for f in gw), default=None)))
        cid = cur.lastrowid
        c.executemany(
            "INSERT INTO flights(check_id,origin,dest,day,key,checked_at,gowild,gowild_price,gowild_seats,"
            "gowild_seats_text,standard_price,discount_den_price,data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(cid, origin, dest, day, f["key"], ts, int(f["gowild"]), f["gowild_price"], f["gowild_seats"],
              f["gowild_seats_text"], f["standard_price"], f["discount_den_price"], json.dumps(f))
             for f in flights])
    return cid


def latest(origin, day):
    """Most recent successful (or no-service) check per destination, with its flights and tracking info."""
    with _conn() as c:
        rows = c.execute(
            """SELECT ch.* FROM checks ch
               JOIN (SELECT dest, MAX(id) mid FROM checks
                     WHERE origin=? AND day=? AND status IN ('ok','no_service') GROUP BY dest) t
               ON ch.id = t.mid""", (origin, day)).fetchall()
        errs = {r["dest"]: dict(r) for r in c.execute(
            """SELECT ch.* FROM checks ch
               JOIN (SELECT dest, MAX(id) mid FROM checks WHERE origin=? AND day=? GROUP BY dest) t
               ON ch.id = t.mid WHERE ch.status='error'""", (origin, day))}
        # first time each flight was seen with GoWild open, and the last check it was open
        track = {}
        for r in c.execute(
                """SELECT dest, key, MIN(checked_at) first_open, MAX(checked_at) last_open,
                          MIN(gowild_price) lo, MAX(gowild_price) hi, COUNT(*) n
                   FROM flights WHERE origin=? AND day=? AND gowild=1 GROUP BY dest, key""", (origin, day)):
            track[(r["dest"], r["key"])] = dict(r)
        out = {}
        for r in rows:
            d = dict(r)
            fl = []
            for fr in c.execute("SELECT data FROM flights WHERE check_id=?", (r["id"],)):
                f = json.loads(fr["data"])
                t = track.get((r["dest"], f["key"]))
                if t:
                    f["first_open"] = t["first_open"]
                    f["last_open"] = t["last_open"]
                    f["price_lo"], f["price_hi"] = t["lo"], t["hi"]
                fl.append(f)
            # GoWild seats seen earlier that are gone now
            open_keys = {f["key"] for f in fl if f["gowild"]}
            d["closed_since"] = [
                {"key": k, "last_open": t["last_open"]}
                for (dest, k), t in track.items() if dest == r["dest"] and k not in open_keys]
            d["flights"] = fl
            d["last_error"] = errs.get(r["dest"])
            out[r["dest"]] = d
        for dest, e in errs.items():
            if dest not in out:
                out[dest] = {**e, "flights": [], "closed_since": [], "last_error": e}
        return out


def history(origin, dest, day):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT checked_at,status,n_flights,n_gowild,min_gowild,error FROM checks"
            " WHERE origin=? AND dest=? AND day=? ORDER BY id", (origin, dest, day))]


def summary(origin):
    """Per-day counts of destinations with GoWild open, from the latest check of each route."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT ch.day, COUNT(*) checked, SUM(ch.n_gowild>0) open_dests, MIN(ch.min_gowild) cheapest,
                      MAX(ch.checked_at) last_checked
               FROM checks ch JOIN (SELECT dest, day, MAX(id) mid FROM checks
                                    WHERE origin=? AND status IN ('ok','no_service') GROUP BY dest, day) t
               ON ch.id=t.mid GROUP BY ch.day ORDER BY ch.day""", (origin,))]
