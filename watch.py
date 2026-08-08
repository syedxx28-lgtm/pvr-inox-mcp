#!/usr/bin/env python3
"""
showwatch - watch cinema booking APIs and shout on Slack the moment something opens.

Polls a cinema's showtime API across a forward date window and diffs the result
against the last run. Three things are worth waking up for:

  new_date      a date that was closed (API error / no shows) now has shows
                -> the booking window just opened
  new_show      a new session appeared on a date already open
                -> extra show added, often for a sold-out title
  back_in_stock a session went from Sold Out back to Available
                -> someone's cancellation or a released block

State lives in state.json so the diff survives across runs. Stdlib only,
no pip install, so a GitHub Actions job is just "run python".

  python watch.py                 poll, diff, notify Slack
  python watch.py --dry-run       poll and print, never touch Slack or state
  python watch.py --show-all      print every matching show, not just changes
"""

import argparse
import datetime
import json
import os
import socket
from concurrent import futures
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "watches.json")
STATE_PATH = os.path.join(HERE, "state.json")

# Google/Cloudflare-fronted hosts hang for 75s on a black-holed IPv6 route
# before falling back. Pin every lookup to A records.
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **kw: [
    r for r in _getaddrinfo(*a, **kw) if r[0] == socket.AF_INET
]


# --------------------------------------------------------------------------
# PVR / INOX provider
# --------------------------------------------------------------------------

PVR_API = "https://api3.pvrcinemas.com/api/v1/booking/content/csessions"
PVR_SEATS = "https://api3.pvrcinemas.com/api/v1/booking/ticketing/seatlayout"

# The web app sends an empty bearer. It is not a placeholder for a real token -
# the endpoint 403s without the header and 200s with it blank.
PVR_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Authorization": "Bearer ",
    "chain": "PVR",
    "country": "INDIA",
    "appVersion": "1.0",
    "platform": "WEBSITE",
    "flow": "PVRINOX",
}


def pvr_fetch_day(watch, date_str):
    """Return the list of shows at this cinema on this date.

    A date that is not yet open for booking answers 500, which is the signal
    we actually care about, so it is reported as 'closed' rather than raised.
    """
    body = json.dumps(
        {
            "city": watch["city"],
            "cid": str(watch["cinema_id"]),
            "lat": str(watch["lat"]),
            "lng": str(watch["lng"]),
            "dated": date_str,
            "qr": "NO",
            "cineType": "",
            "cineTypeQR": "",
        }
    ).encode()

    headers = dict(PVR_HEADERS, city=watch["city"])
    req = urllib.request.Request(PVR_API, body, headers)
    try:
        payload = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as exc:
        return None, "http %s" % exc.code
    except Exception as exc:  # network flake - treat as unknown, never as closed
        return None, "error %s" % exc

    if payload.get("status") != 302 or not payload.get("output"):
        return None, "closed"

    shows = []
    for movie in payload["output"].get("cinemaMovieSessions") or []:
        for exp in movie.get("experienceSessions") or []:
            for show in exp.get("shows") or []:
                shows.append(
                    {
                        "session_id": show.get("sessionId"),
                        "movie_id": show.get("movieId"),
                        "film": (movie.get("movieRe") or {}).get("filmName", ""),
                        "experience": exp.get("experienceKey", ""),
                        "format": show.get("movieFormat", ""),
                        "language": show.get("language", ""),
                        "date": show.get("showDate"),
                        "time": show.get("showTime"),
                        "ts": show.get("showTimeStamp") or 0,
                        "screen": show.get("screenName", ""),
                        "status": show.get("statusTxt", ""),
                        "token": show.get("encrypted", ""),
                    }
                )
    return shows, None


def pvr_fetch_seats(token):
    """Seat map for one session. Returns (summary, error).

    In the layout, s == 1 is a free seat and s == 2 is taken; entries with no
    seat name are aisles and gaps. Verified against the rendered seat map.
    """
    req = urllib.request.Request(
        PVR_SEATS, json.dumps({"encrypted": token}).encode(), dict(PVR_HEADERS)
    )
    try:
        payload = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as exc:
        return None, "seatmap %s" % exc

    if payload.get("status") != 200 or not payload.get("output"):
        return None, "seatmap unavailable"

    total = free = 0
    free_names = []
    best_run, best_where = 0, ""

    for row in payload["output"].get("rows") or []:
        if row.get("t") != "seats":
            continue
        run = []
        for seat in row.get("s") or []:
            name = seat.get("sn")
            if not name:
                # Blank grid slot: an aisle or gap. Seats either side of it are
                # not "together", so this breaks the run rather than skipping.
                run = []
                continue
            total += 1
            if seat.get("s") == 1:
                free += 1
                free_names.append(name)
                run.append(name)
                if len(run) > best_run:
                    best_run, best_where = len(run), "%s-%s" % (run[0], run[-1])
            else:
                run = []

    return {
        "total": total,
        "free": free,
        "best_run": best_run,
        "best_where": best_where,
        "seats": free_names[:40],
    }, None


def pvr_booking_url(watch, date_str):
    return "https://www.pvrcinemas.com/cinemasessions/%s/%s/%s" % (
        watch["city"],
        watch.get("cinema_slug", "cinema"),
        watch["cinema_id"],
    )


PROVIDERS = {"pvr": (pvr_fetch_day, pvr_booking_url)}


# --------------------------------------------------------------------------
# Matching and polling
# --------------------------------------------------------------------------


def matches(show, watch):
    """Does this show satisfy the watch's film / experience / language filters?"""
    needle = watch.get("film_contains", "").upper()
    if needle and needle not in (show["film"] or "").upper():
        return False

    want_exp = watch.get("experience", "")
    if want_exp and show["experience"] != want_exp:
        return False

    want_lang = watch.get("language", "")
    if want_lang and (show["language"] or "").lower() != want_lang.lower():
        return False

    return True


def show_key(show):
    """Stable identity for a session across runs. sessionId alone is reused."""
    return "%s|%s|%s" % (show["date"], show["time"], show["screen"])


def poll(watch, today, verbose=False):
    """Poll the forward window. Returns {date: {show_key: show}} for open dates."""
    fetch_day, _ = PROVIDERS[watch.get("provider", "pvr")]
    snapshot = {}

    for offset in range(watch.get("horizon_days", 12)):
        date_str = (today + datetime.timedelta(days=offset)).isoformat()
        shows, err = fetch_day(watch, date_str)

        if err == "closed":
            if verbose:
                print("  %s  closed" % date_str)
            continue
        if err:
            # A transient network error must not look like a closed date,
            # otherwise it re-fires as new_date on the next successful run.
            print("  %s  %s (skipped)" % (date_str, err), file=sys.stderr)
            snapshot[date_str] = None
            continue

        hits = {show_key(s): s for s in shows if matches(s, watch)}

        if watch.get("seat_detail"):
            # One seat-map call per showtime, so fetch them concurrently or a
            # 5-minute cron spends most of its life waiting on serial requests.
            # A lapsed show has already started - it has no seat map and can't
            # be booked. Sold-out ones are still worth fetching, for restocks.
            todo = [
                s
                for s in hits.values()
                if s.get("token") and s["status"].lower() != "lapsed"
            ]
            with futures.ThreadPoolExecutor(max_workers=8) as pool:
                for show, (seats, err) in zip(
                    todo, pool.map(lambda s: pvr_fetch_seats(s["token"]), todo)
                ):
                    if err:
                        print(
                            "  %s %s: %s" % (date_str, show["time"], err),
                            file=sys.stderr,
                        )
                    else:
                        show["seats"] = seats

        if verbose:
            print(
                "  %s  %d show(s)  %s"
                % (
                    date_str,
                    len(hits),
                    "  ".join(
                        "%s[%s%s]"
                        % (
                            s["time"],
                            s["status"],
                            " %d/%d free" % (s["seats"]["free"], s["seats"]["total"])
                            if s.get("seats")
                            else "",
                        )
                        for s in hits.values()
                    ),
                )
            )
        if hits:
            snapshot[date_str] = hits

    return snapshot


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------

AVAILABLE = {"available", "filling up fast", "filling fast"}


def diff(watch, previous, snapshot):
    """Compare against the last run and return a list of alert-worthy events."""
    events = []
    open_now = {d: v for d, v in snapshot.items() if v is not None}

    for date_str in sorted(open_now):
        shows = open_now[date_str]
        was = previous.get(date_str)

        if was is None:
            # None here means "not in previous state at all" -> the window opened.
            events.append(
                {
                    "kind": "new_date",
                    "date": date_str,
                    "shows": sorted(shows.values(), key=lambda s: s.get("ts", 0)),
                }
            )
            continue

        for key, show in sorted(shows.items()):
            before = was.get(key)
            if before is None:
                events.append({"kind": "new_show", "date": date_str, "shows": [show]})
                continue

            if watch.get("alert_on_restock", True):
                old = (before.get("status") or "").lower()
                new = (show.get("status") or "").lower()
                if old not in AVAILABLE and new in AVAILABLE:
                    events.append(
                        {"kind": "back_in_stock", "date": date_str, "shows": [show]}
                    )
                    continue

            # A sold-out show that frees up a pair is the case worth waking for.
            # Alert only when the best adjacent block crosses the threshold from
            # below, so a show sitting at 6-together does not re-fire every run.
            need = watch.get("min_adjacent", 0)
            if need and show.get("seats") and before.get("seats"):
                was_run = before["seats"].get("best_run", 0)
                now_run = show["seats"].get("best_run", 0)
                if was_run < need <= now_run:
                    events.append(
                        {"kind": "seats_freed", "date": date_str, "shows": [show]}
                    )

    return events


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

HEADLINES = {
    "new_date": ":rotating_light: Booking just opened",
    "new_show": ":new: Show added",
    "back_in_stock": ":recycle: Back in stock",
    "seats_freed": ":seat: Seats together opened up",
}


def seat_summary(show):
    """'37/442 free - 11 together at O10-O20', or '' if seats weren't fetched."""
    s = show.get("seats")
    if not s or not s.get("total"):
        return ""
    pct = 100.0 * (s["total"] - s["free"]) / s["total"]
    out = "%d/%d free (%.0f%% booked)" % (s["free"], s["total"], pct)
    if s.get("best_run"):
        out += " - %d together at %s" % (s["best_run"], s["best_where"])
    return out


def format_slack(watch, events):
    url = PROVIDERS[watch.get("provider", "pvr")][1](watch, events[0]["date"])
    label = watch["name"]

    lines = []
    for ev in events:
        pretty = datetime.date.fromisoformat(ev["date"]).strftime("%a %d %b")
        lines.append("*%s* - %s" % (HEADLINES[ev["kind"]], pretty))
        for s in ev["shows"]:
            bits = [s["time"]]
            if s["screen"]:
                bits.append(s["screen"])
            if s["status"]:
                bits.append(s["status"])
            seats = seat_summary(s)
            if seats:
                bits.append(seats)
            lines.append("   - %s" % "  |  ".join(bits))

    lines.append("<%s|Book now>" % url)
    return {"text": "*%s*\n%s" % (label, "\n".join(lines))}


def notify_slack(payload):
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        print("SLACK_WEBHOOK_URL not set - printing instead:\n", payload["text"])
        return False

    req = urllib.request.Request(
        hook,
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    return True


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="never write state or Slack")
    ap.add_argument("--show-all", action="store_true", help="print every matching show")
    ap.add_argument("--watch", help="run only the watch with this name")
    args = ap.parse_args()

    with open(CONFIG_PATH) as fh:
        config = json.load(fh)

    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            state = json.load(fh)

    today = datetime.date.today()
    fired = 0

    for watch in config["watches"]:
        if not watch.get("enabled", True):
            continue
        if args.watch and watch["name"] != args.watch:
            continue

        print("%s" % watch["name"])
        snapshot = poll(watch, today, verbose=args.show_all)
        previous = state.get(watch["name"], {})

        # First ever run: record the baseline silently, or every open date
        # would fire as a discovery.
        if not previous:
            print("  baseline recorded (%d open date(s))" % len(snapshot))
        else:
            events = diff(watch, previous, snapshot)
            if events:
                fired += len(events)
                payload = format_slack(watch, events)
                if args.dry_run:
                    print(payload["text"])
                else:
                    delivered = notify_slack(payload)
                    if not delivered:
                        # Advancing state here would swallow the opening for
                        # good - it can only ever be reported once. Leave the
                        # old state so the next run fires it again.
                        print("  NOT DELIVERED - state held back", file=sys.stderr)
                        continue
                    print("  alerted: %d event(s)" % len(events))
            else:
                print("  no change (%d open date(s))" % len(snapshot))

        # Dates that errored this run keep their previous value, so a flaky
        # poll never manufactures a new_date on the following run.
        merged = dict(previous)
        for date_str, shows in snapshot.items():
            if shows is not None:
                merged[date_str] = shows
        # Drop dates that have fallen out of the window entirely.
        cutoff = today.isoformat()
        merged = {d: v for d, v in merged.items() if d >= cutoff}
        state[watch["name"]] = merged

    if not args.dry_run:
        with open(STATE_PATH, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)

    return 0 if fired == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
