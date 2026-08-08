#!/usr/bin/env python3
"""
pvr-inox core - a stdlib-only client for the PVR/INOX booking API.

Everything here works for any of the ~116 cities the chain serves, not just
the one the watcher happens to be configured for. Two findings drive the
design:

- A date not yet open for booking answers HTTP 500. "Has the window opened"
  is therefore a boolean, not a diff of show lists.
- The API needs an `Authorization: Bearer ` header with an EMPTY token. It
  403s without the header and works with it blank. No login, no key.

Note this covers the PVR/INOX chain only. Other chains, and BookMyShow (which
is Cloudflare-gated against every plain request), are out of reach.
"""

import json
import socket
import urllib.error
import urllib.request

# Google-fronted hosts hang for ~75s on a black-holed IPv6 route before falling
# back, so pin every lookup to A records.
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **kw: [
    r for r in _getaddrinfo(*a, **kw) if r[0] == socket.AF_INET
]

API = "https://api3.pvrcinemas.com/api/v1/booking"

# Chennai's centre, used when a caller gives no coordinates. The API applies a
# distance filter to cinema lists, so coordinates decide what comes back.
DEFAULT_LATLNG = ("13.0827", "80.2707")


def _headers(city):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Authorization": "Bearer ",  # deliberately blank - see module docstring
        "chain": "PVR",
        "country": "INDIA",
        "appVersion": "1.0",
        "platform": "WEBSITE",
        "flow": "PVRINOX",
        "city": city,
    }


def _post(path, body, city="Chennai", timeout=30):
    req = urllib.request.Request(
        "%s/%s" % (API, path), json.dumps(body).encode(), _headers(city)
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def list_cities():
    """Every city the chain sells tickets in."""
    payload = _post("content/city", {"lat": DEFAULT_LATLNG[0], "lng": DEFAULT_LATLNG[1]})
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("name", "cityName") and isinstance(value, str):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload.get("output") or payload)
    return sorted(found)


def list_cinemas(city, lat=None, lng=None, query=""):
    """Cinemas in a city, nearest first. `query` filters on the name."""
    lat = lat or DEFAULT_LATLNG[0]
    lng = lng or DEFAULT_LATLNG[1]
    payload = _post(
        "content/cinemas",
        {"city": city, "lat": str(lat), "lng": str(lng), "text": ""},
        city,
    )

    found = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("theatreId") and node.get("name"):
                found[node["theatreId"]] = {
                    "cinema_id": node["theatreId"],
                    "name": node["name"],
                    "address": node.get("address1", ""),
                    "lat": node.get("latitude"),
                    "lng": node.get("longitude"),
                    "distance": node.get("distanceText", ""),
                    "shows": node.get("showCount", 0),
                }
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload.get("output") or {})
    rows = list(found.values())
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in r["name"].lower()]
    return sorted(rows, key=lambda r: -r["shows"])


def now_showing(city, lat=None, lng=None):
    """Films currently playing in a city."""
    payload = _post(
        "content/nowshowing",
        {"city": city, "lat": str(lat or DEFAULT_LATLNG[0]), "lng": str(lng or DEFAULT_LATLNG[1])},
        city,
    )
    films = []
    for movie in (payload.get("output") or {}).get("mv") or []:
        films.append(
            {
                "name": movie.get("n"),
                "certificate": movie.get("ce") or movie.get("certificateLk") or "",
                "length": movie.get("mlength") or "",
                "languages": movie.get("mfs") or [],
                "genres": movie.get("grs") or [],
                "shows": movie.get("showCount") or 0,
                "formats": [
                    e.get("expName")
                    for e in (movie.get("experiences") or [])
                    if e.get("expName")
                ],
                "released": movie.get("releaseDate") or "",
            }
        )
    return sorted(films, key=lambda f: -f["shows"])


# --------------------------------------------------------------------------
# Showtimes
# --------------------------------------------------------------------------


def day_sessions(city, cinema_id, date, lat=None, lng=None):
    """Sessions at one cinema on one date. Returns (shows, error).

    error == "closed" means the date is not open for booking yet - the single
    most useful signal this API gives. A network failure returns a different
    error so callers never mistake a flake for a closed window.
    """
    body = {
        "city": city,
        "cid": str(cinema_id),
        "lat": str(lat or DEFAULT_LATLNG[0]),
        "lng": str(lng or DEFAULT_LATLNG[1]),
        "dated": date,
        "qr": "NO",
        "cineType": "",
        "cineTypeQR": "",
    }
    try:
        payload = _post("content/csessions", body, city)
    except urllib.error.HTTPError as exc:
        return None, "http %s" % exc.code
    except Exception as exc:
        return None, "error %s" % exc

    if payload.get("status") != 302 or not payload.get("output"):
        return None, "closed"

    shows = []
    for movie in payload["output"].get("cinemaMovieSessions") or []:
        film = (movie.get("movieRe") or {}).get("filmName", "")
        for exp in movie.get("experienceSessions") or []:
            for show in exp.get("shows") or []:
                shows.append(
                    {
                        "film": film,
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
    return sorted(shows, key=lambda s: s["ts"]), None


def is_open(city, cinema_id, date, lat=None, lng=None):
    """Is this date on sale yet?"""
    shows, err = day_sessions(city, cinema_id, date, lat, lng)
    if err == "closed":
        return False, "not open for booking"
    if err:
        return None, err
    return True, "%d shows" % len(shows)


# --------------------------------------------------------------------------
# Seat maps and the good-seats zone
# --------------------------------------------------------------------------


def _row_blocks(row):
    """Split a row into aisle-delimited blocks of real seats.

    Entries with no seat name are aisles or grid padding, so they end the
    current block. This is what makes "together" mean actually together.
    """
    blocks, current = [], []
    for seat in row.get("s") or []:
        if not seat.get("sn"):
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(seat)
    if current:
        blocks.append(current)
    return blocks


def _centre_block(row):
    """The block a row's midpoint falls in - the middle section between aisles."""
    blocks = _row_blocks(row)
    if not blocks:
        return []
    total = sum(len(b) for b in blocks)
    midpoint, seen = total // 2, 0
    for block in blocks:
        seen += len(block)
        if seen > midpoint:
            return block
    return blocks[-1]


# Rows are returned front-first (nearest the screen). The stretch from 60% to
# 85% back is the usual big-screen sweet spot: far enough that the screen fills
# your view without craning, short of the back wall.
BAND_FROM, BAND_TO = 0.60, 0.85


def resolve_zone(seat_rows, zone_rows=None, zone_seats=None):
    """Work out which seats count as good ones.

    With no configuration this derives the zone from the auditorium's own
    geometry, so it transfers to any cinema. Row letters differ between
    houses - Palazzo's AUDI 5 runs O at the front to A at the back, Phoenix's
    IMAX runs P to A - so a hardcoded row list is never portable.

    Returns {row_name: set_of_seat_numbers}.
    """
    if zone_rows:
        chosen = [r for r in seat_rows if r.get("n") in zone_rows]
    else:
        count = len(seat_rows)
        lo, hi = int(count * BAND_FROM), int(count * BAND_TO)
        chosen = seat_rows[lo : hi + 1] or seat_rows

    zone = {}
    for row in chosen:
        if zone_seats:
            lo, hi = zone_seats
            numbers = set()
            for seat in row.get("s") or []:
                if seat.get("sn"):
                    try:
                        number = int(seat.get("displaynumber") or 0)
                    except ValueError:
                        continue
                    if lo <= number <= hi:
                        numbers.add(number)
        else:
            numbers = set()
            for seat in _centre_block(row):
                try:
                    numbers.add(int(seat.get("displaynumber") or 0))
                except ValueError:
                    pass
        zone[row.get("n")] = numbers
    return zone


def seat_report(token, zone_rows=None, zone_seats=None, want_map=False):
    """Seat availability for one session. Returns (report, error).

    s == 1 is free, s == 2 is taken - verified against the rendered seat map.
    """
    try:
        payload = _post("ticketing/seatlayout", {"encrypted": token})
    except Exception as exc:
        return None, "seatmap %s" % exc

    if payload.get("status") != 200 or not payload.get("output"):
        return None, "seatmap unavailable"

    output = payload["output"]
    seat_rows = [r for r in output.get("rows") or [] if r.get("t") == "seats"]
    zone = resolve_zone(seat_rows, zone_rows, zone_seats)

    total = free = zone_total = zone_free = 0
    zone_names = []
    best_run, best_where = 0, ""
    picture = []

    for row in seat_rows:
        name = row.get("n")
        allowed = zone.get(name, set())
        run = []
        glyphs = ""

        for seat in row.get("s") or []:
            label = seat.get("sn")
            if not label:
                glyphs += " "
                run = []
                continue

            total += 1
            is_free = seat.get("s") == 1
            free += is_free
            try:
                number = int(seat.get("displaynumber") or 0)
            except ValueError:
                number = 0

            if number not in allowed:
                glyphs += "o" if is_free else "."
                run = []  # outside the zone, so a run can't straddle the edge
                continue

            zone_total += 1
            glyphs += "O" if is_free else "x"
            if is_free:
                zone_free += 1
                zone_names.append(label)
                run.append(label)
                if len(run) > best_run:
                    best_run = len(run)
                    best_where = (
                        run[0] if len(run) == 1 else "%s-%s" % (run[0], run[-1])
                    )
            else:
                run = []

        picture.append("%-3s %s" % (name, glyphs))

    report = {
        "cinema": output.get("cinemaName", ""),
        "when": output.get("showDateTime", ""),
        "experience": output.get("experience", ""),
        "total": total,
        "free": free,
        "zone_total": zone_total,
        "zone_free": zone_free,
        "best_run": best_run,
        "best_where": best_where,
        "seats": zone_names[:60],
        "zone_rows": [r for r in zone if zone[r]],
    }
    if want_map:
        # Front row first, matching how the cinema's own layout is drawn.
        report["map"] = ["    " + "SCREEN".center(40)] + picture
    return report, None


def describe_seats(report):
    """One line: the zone read first, since that is the part worth acting on."""
    if not report or not report.get("total"):
        return ""
    booked = 100.0 * (report["total"] - report["free"]) / report["total"]
    context = "%d free in zone, %.0f%% booked overall" % (report["zone_free"], booked)
    if not report.get("best_run"):
        return "no good seats (%s)" % context
    if report["best_run"] == 1:
        return "GOOD SEAT: %s (%s)" % (report["best_where"], context)
    return "GOOD SEATS: %d together at %s (%s)" % (
        report["best_run"],
        report["best_where"],
        context,
    )
