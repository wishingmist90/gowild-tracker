"""Scan jobs: check every route out of an airport for one date, record each result.

One job runs at a time and frontier.py paces requests (one every 4-7 s), so a full
airport (~90 routes) takes 6-10 minutes. That pace is deliberate: Frontier's bot wall
blocks bursts. If Frontier refuses us even once, the job stops and a 30-minute cooldown
kicks in instead of retrying harder.
"""
import itertools
import threading
import time
from datetime import date

import frontier
import rules
import store

BLOCK_LIMIT = 1            # blocked responses before we stop
COOLDOWN_SECONDS = 30 * 60

_ids = itertools.count(1)
_jobs = {}
_jobs_lock = threading.Lock()
_queue_lock = threading.Lock()   # serializes jobs
_cooldown_until = 0.0


def is_domestic_route(stations, a, b):
    return stations.get(a, {}).get("domestic", False) and stations.get(b, {}).get("domestic", False)


def plan(origin, day, dests=None):
    """Which destinations to check for this origin/date, and why others are skipped."""
    m = frontier.get_markets()
    stations, routes = m["stations"], m["routes"]
    all_dests = dests or routes.get(origin, [])
    ahead = (day - date.today()).days
    todo, skipped = [], {}
    for d in all_dests:
        if d == origin:
            continue
        dom = is_domestic_route(stations, origin, d)
        if not rules.bookable_now(day, dom) and not dests:
            skipped[d] = "domestic opens the day before" if dom and ahead > 1 else "outside window"
            continue
        todo.append(d)
    return todo, skipped


class Job:
    def __init__(self, origin, day, dests, label):
        self.id = next(_ids)
        self.origin, self.day, self.dests, self.label = origin, day, dests, label
        self.total = len(dests)
        self.done = 0
        self.found = 0
        self.errors = 0
        self.state = "queued"
        self.message = ""
        self.started = time.time()
        self.finished = None

    def public(self):
        return {"id": self.id, "origin": self.origin, "day": self.day.isoformat(), "label": self.label,
                "total": self.total, "done": self.done, "found": self.found, "errors": self.errors,
                "state": self.state, "message": self.message,
                "elapsed": round((self.finished or time.time()) - self.started)}


def _check(job, dest, blocked_streak):
    if job.state == "stopped":
        return
    o, d, day = job.origin, dest, job.day
    try:
        res = frontier.get_flights(o, d, day)
        if not res["flights"]:
            store.record(o, d, day.isoformat(), "no_service")
        else:
            store.record(o, d, day.isoformat(), "ok", res)
            if any(f["gowild"] for f in res["flights"]):
                job.found += 1
        blocked_streak[0] = 0
    except frontier.FrontierError as e:
        job.errors += 1
        store.record(o, d, day.isoformat(), "error", error=str(e))
        if e.blocked:
            blocked_streak[0] += 1
            if blocked_streak[0] >= BLOCK_LIMIT:
                job.state = "stopped"
                job.message = "Frontier started refusing requests (bot protection); paused 30 min."
    finally:
        job.done += 1


def _run(job):
    global _cooldown_until
    with _queue_lock:
        if time.time() < _cooldown_until:
            job.state, job.message = "stopped", "Cooling down after Frontier refused requests."
            job.finished = time.time()
            return
        job.state = "running"
        streak = [0]
        for d in job.dests:
            if job.state == "stopped":
                break
            _check(job, d, streak)
        if job.state == "stopped":
            _cooldown_until = time.time() + COOLDOWN_SECONDS
        else:
            job.state = "done"
        job.finished = time.time()


def start(origin, day, dests=None, label=""):
    origin = origin.upper()
    with _jobs_lock:
        for j in _jobs.values():  # same scan already queued/running: reuse it
            if j.origin == origin and j.day == day and j.state in ("queued", "running") and not dests:
                return j, {}
    todo, skipped = plan(origin, day, dests)
    job = Job(origin, day, todo, label or f"{origin} {day:%a %b %d}")
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job, skipped


def jobs():
    with _jobs_lock:
        cutoff = time.time() - 600
        for k in [k for k, j in _jobs.items() if j.finished and j.finished < cutoff]:
            del _jobs[k]
        return [j.public() for j in sorted(_jobs.values(), key=lambda j: j.id)]


def cooldown_left():
    return max(0, round(_cooldown_until - time.time()))
