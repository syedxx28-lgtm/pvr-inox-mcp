#!/usr/bin/env python3
"""
PVR INOX MCP server. Thin stdio transport over core.py.

All the logic lives in core, which is stdlib-only and drives the cron watcher
too. This module only exposes it as MCP tools so any client can ask about
cinema showtimes and, more usefully, which seats are actually free.

Run:  python3 mcp_server.py
"""

import base64
import datetime
import json
import os
import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations

import core
from starlette.responses import FileResponse, JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "watches.json")

INSTRUCTIONS = """
You can look up films, showtimes and live seat availability at any PVR or INOX
cinema in India - about 116 cities.

Typical flow: pvr_cinemas to find the cinema id, then pvr_seats to
see what is actually bookable. pvr_now_showing answers "what's on".

Things that matter when answering:

- SHOW-LEVEL STATUS IS MISLEADING. A show marked "Available" or "Filling Up
  Fast" is routinely stripped of every decent seat. Shows with 100+ free seats
  have had zero free in the back-centre. Always check pvr_seats before
  telling someone a show is worth booking, and lead with the zone figure.

- The "zone" is the good seats: the rows 60-85% of the way back, and the
  aisle-delimited centre block of each. It is derived from each auditorium's
  own geometry, so it works anywhere - row letters mean different things in
  different houses.

- "NO GOOD SEATS" IS NOT "NO SEATS". The zone is a heuristic and it is often
  wrong in small or premium halls. When a show reports no good seats, pvr_seats
  now tells you how many are free outside the zone and which rows have runs big
  enough for the party, with a ready-made zone_rows to re-call with. Do that
  before telling anyone a show is unusable. Always pass party_size.

- LANGUAGE is per SHOW, not per film. One film has many prints (Tamil, 3D
  Tamil, English, 3D English Atmos...) and a single cinema runs several of them
  the same day. The schedule groups them under ONE block title, so a block
  reading "SPIDERMAN BRAND NEW DAY (TAMIL)" can contain English shows - eight
  of them at Palazzo on 2026-08-09. pvr_showtimes resolves each show to its own
  print and exposes `language` (ISO), `variant_id` and the full variant title.
  Filter with language="en". Do NOT judge a film's languages from the block
  title or from the release list in pvr_now_showing; both mislead.

- STATE is a derived enum, never the upstream label: ON_SALE, LIMITED (<15%
  free), SOLD_OUT, CLOSED (booking shut, or the show is under way), COMPLETED,
  NOT_ON_SALE. "Lapsed" never appears - it is a SALE state, not a time state,
  and shows up on screenings hours in the future. Only ON_SALE and LIMITED are
  bookable. A trailing "?" means inventory was not counted.

- START WITH pvr_find_shows for anything constrained ("English, tonight, near
  me, 2 together"). It takes party_size (required), a radius, a date range, and
  returns ranked bookable shows with seat blocks and booking links in ONE call.
  It reports which cinemas it did NOT search - never present its results as
  city-wide coverage.

- BOOKING HANDOFF: every bookable show carries `booking_url`, which opens the
  seat-selection screen for that exact show. Give it to the user rather than
  telling them to find the show in the app.

- ERRORS START WITH "ERROR <CODE>:" and are not availability facts.
  CITY_NOT_SERVICED, CINEMA_NOT_FOUND, DATE_IN_PAST, BEYOND_BOOKING_WINDOW,
  SHOW_NOT_BOOKABLE, SHOW_NOT_FOUND, UPSTREAM_ERROR. A past date is DATE_IN_PAST,
  never "not on sale yet" - do not tell anyone to wait for a date that has gone.

- "closed" means the date is NOT YET ON SALE, not that it is sold out. The
  chain books roughly 5 days ahead on a rolling window, so a date beyond that
  answers closed and will open later. Say "not on sale yet", never "sold out".

- Coverage is the PVR/INOX chain only. Independent cinemas and other chains
  are not here, and neither is BookMyShow, which blocks automated requests.
  Say so rather than implying a city has only these cinemas.

- Cinema lists are distance-filtered from the coordinates you pass, so a
  cinema can be missing simply because the coordinates were far away. Pass the
  user's own lat/lng when you have it.

- Seat names like "D14" are row letter plus number. "4 together at D14-D17"
  means genuinely adjacent, with aisles treated as breaks.

You can also manage the cron watches that alert Slack when a booking window
opens: pvr_add_watch / pvr_list_watches / pvr_remove_watch.

Adding a watch only edits a local file. The cron runs the COMMITTED config, so
nothing takes effect until pvr_publish_watches pushes it. Always tell the
user a new watch is not live yet, and never publish unless they ask for it - it
pushes to a public repository.

Every tool returns a compact table by default. Pass format="json" for the full
structured result when you need to compute over it rather than report it.
"""

# Served over HTTPS rather than inlined as a data: URI. Clients commonly refuse
# to render data: URIs, and PNG is more broadly supported than SVG, so offer a
# real PNG first with an explicit size, then the SVG. Falls back to the data
# URI when no public base URL is configured (e.g. local stdio use).
def _icon():
    base = os.environ.get("PVR_MCP_PUBLIC_URL", "").rstrip("/")
    if base:
        return [
            Icon(src=base + "/icon.png", mimeType="image/png", sizes=["512x512"]),
            Icon(src=base + "/icon.svg", mimeType="image/svg+xml", sizes=["any"]),
        ]
    try:
        with open(os.path.join(HERE, "icon.svg"), "rb") as fh:
            return [
                Icon(
                    src="data:image/svg+xml;base64," + base64.b64encode(fh.read()).decode(),
                    mimeType="image/svg+xml",
                    sizes=["any"],
                )
            ]
    except OSError:
        return None  # an icon is decoration; never let it stop the server


# DNS-rebinding protection validates the Host header and defaults to localhost
# only, so a hosted deployment answers 421 until its own hostname is allowed.
# PVR_MCP_ALLOWED_HOSTS is a comma-separated list; "*" disables the check.
_hosts = [h.strip() for h in os.environ.get("PVR_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
_security = (
    TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if _hosts == ["*"]
    else TransportSecuritySettings(
        allowed_hosts=_hosts, allowed_origins=["https://" + h for h in _hosts]
    )
    if _hosts
    else None
)

mcp = FastMCP(
    "pvr-inox",
    instructions=INSTRUCTIONS,
    icons=_icon(),
    website_url="https://github.com/notprashanth/pvr-inox-mcp",
    transport_security=_security,
)

# Remote mode: the server is reachable by anyone holding the URL, so the tools
# that touch the filesystem or push to git are not registered at all. Absent
# beats guarded - there is no handler to reach.
REMOTE = os.environ.get("PVR_MCP_TRANSPORT", "stdio") != "stdio"


def local_only(**kw):
    """Register this tool only when served over stdio to a single local user."""
    def wrap(fn):
        return fn if REMOTE else mcp.tool(**kw)(fn)
    return wrap


# Everything here reads a public booking API and writes nothing.
_LOOKUP = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


def _err(code, message, **extra):
    """Errors are structurally distinguishable from data.

    The connector used to answer an unknown cinema id, and a date a week in the
    past, with the SAME cheerful "NOT ON SALE yet" string - a confident,
    plausible, wrong assertion about an entity that may not even exist. A
    caller must never have to parse prose to tell an error from a fact.
    """
    line = "ERROR %s: %s" % (code, message)
    if extra:
        line += "\n" + json.dumps({"error": code, **extra}, indent=1)
    return line


def _validate(city, cinema_id=None, date=None):
    """Returns an error string, or None when the inputs are real."""
    serviced = core.city_is_serviced(city)
    if serviced is False:
        return _err("CITY_NOT_SERVICED",
                    "%r is not a city where PVR/INOX sells tickets. Use pvr_cities." % city)

    if cinema_id is not None and not core.find_cinema(city, cinema_id):
        return _err("CINEMA_NOT_FOUND",
                    "No cinema %r in %s. Use pvr_cinemas to get valid ids." % (cinema_id, city))

    if date:
        try:
            when = datetime.date.fromisoformat(date)
        except ValueError:
            return _err("BAD_DATE", "%r is not an ISO date (YYYY-MM-DD)." % date)
        today = core.today_ist()
        if when < today:
            return _err("DATE_IN_PAST",
                        "%s has already passed (today is %s). It will not go on sale."
                        % (date, today.isoformat()), date=date, today=today.isoformat())
    return None


def _out(payload, text, fmt):
    if (fmt or "text").strip().lower() == "json":
        return json.dumps(payload, indent=2, default=str)
    return text


@mcp.tool(annotations=_LOOKUP)
def pvr_cities(format: str = "text") -> str:
    """Every city where PVR/INOX sells tickets, flagged city vs metro roll-up.

    Roll-ups overlap their constituents - summing show counts across the raw
    list double-counts cinemas. De-duplicate by cinema id, never by city name.
    """
    records = core.city_records()
    rollups = [r for r in records if r["type"] == "metro_rollup"]
    plain = [r for r in records if r["type"] == "city"]

    lines = ["%d cities (%d metro roll-ups)" % (len(records), len(rollups))]
    if rollups:
        lines.append("\nMETRO ROLL-UPS - overlap the cities listed after them:")
        for r in rollups:
            lines.append("  %-16s covers: %s" % (r["name"], ", ".join(r["subcities"]) or "(unnamed constituents)"))
    lines.append("\nCITIES:")
    lines.append("  " + ", ".join(r["name"] for r in plain))
    return _out(records, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_cinemas(
    city: str, query: str = "", lat: str = "", lng: str = "", format: str = "text"
) -> str:
    """Cinemas in a city, nearest first when a distance can be measured.

    Distance is computed from a stated origin - your lat/lng if given, else the
    city centre. 11 of 116 cities publish no coordinates; for those the
    distance reads "unknown" rather than a wrong number.

    Returns the cinema_id that every other tool needs. Pass lat/lng when you
    know where the user is - the list is distance-filtered.
    """
    rows = core.list_cinemas(city, lat or None, lng or None, query)
    if not rows:
        return "No cinemas found in %s%s." % (city, " matching %r" % query if query else "")

    origin = rows[0].get("distance_from") if rows else None
    lines = ["%-6s %-46s %-11s %s" % ("ID", "CINEMA", "DISTANCE", "SHOWS"), "-" * 78]
    for r in rows:
        lines.append(
            "%-6s %-46s %-11s %s"
            % (r["cinema_id"], r["name"][:46], r["distance"], r["shows"])
        )
    if origin:
        lines.append("\ndistance measured from: %s" %
                     ("the lat/lng you passed" if origin == "caller" else "the city centre"))
    else:
        lines.append("\nDISTANCE UNAVAILABLE - this city publishes no coordinates, so no "
                     "reference point exists. Pass lat/lng to get real distances.")
    return _out(rows, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_now_showing(
    city: str, lat: str = "", lng: str = "", format: str = "text"
) -> str:
    """Films currently playing in a city, with certificate, length and formats."""
    films = core.now_showing(city, lat or None, lng or None)
    if not films:
        return "Nothing showing in %s." % city

    width = max([len(f["name"] or "") for f in films] + [24])
    lines = [
        "%-*s %-7s %-7s %-20s %-6s %s"
        % (width, "FILM", "CERT", "LENGTH", "RELEASE LANGS", "SHOWS", "FORMATS"),
        "-" * (width + 52),
    ]
    for f in films:
        lines.append(
            "%-*s %-7s %-7s %-20s %-6d %s"
            % (
                width,
                f["name"] or "",
                f["certificate"],
                f["length"],
                ", ".join(f["languages"])[:20],
                f["shows"],
                ", ".join(f["formats"]),
            )
        )
    lines.append(
        "\nRELEASE LANGS is what the film was released in, NOT what is scheduled here. "
        "A film listed as English may have zero English showtimes in this city. "
        "Always confirm with pvr_showtimes, where the title suffix carries the "
        "actual schedule language."
    )
    return _out(films, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_showtimes(
    city: str,
    cinema_id: str,
    date: str,
    film: str = "",
    language: str = "",
    experience: str = "",
    bookable_only: bool = True,
    lat: str = "",
    lng: str = "",
    format: str = "text",
) -> str:
    """Showtimes at one cinema on one date (YYYY-MM-DD).

    `film` is a case-insensitive substring. `experience` filters on format:
    imax, pxl, bigpix, 4dx, luxe - omit for all.

    If the date is not on sale yet this says so. That is different from being
    sold out.
    """
    bad = _validate(city, cinema_id, date)
    if bad:
        return bad

    shows, err = core.day_sessions(city, cinema_id, date, lat or None, lng or None)
    if err == "closed":
        last, days = core.booking_horizon(city, cinema_id, lat or None, lng or None)
        return _err(
            "BEYOND_BOOKING_WINDOW",
            "%s is not on sale yet at cinema %s." % (date, cinema_id),
            on_sale_through=last,
            booking_window_days=days,
            note="Bookings open on a rolling window; this date should open nearer the time.",
        )
    if err:
        return _err("UPSTREAM_ERROR", "Could not read showtimes: %s" % err)

    if film:
        needle = film.upper()
        shows = [
            s for s in shows
            if needle in (s["film"] or "").upper()
            or needle in (s.get("title") or "").upper()
            or needle == str(s.get("canonical_film_id") or "")
        ]
    if language:
        wanted = core.lang_code(language) or language.lower()
        # Unknown language is never treated as a match - a null must not pass.
        shows = [s for s in shows if s.get("language") == wanted]
    if experience:
        shows = [s for s in shows if s["experience"] == experience]

    # R-P0-6: unbookable shows are excluded by default, with a count so the
    # caller can say "3 shows, all sold out" without another call.
    for s in shows:
        s["state"], s["state_verified"] = core.show_status(s)

    excluded = {}
    if bookable_only:
        keep = []
        for s in shows:
            if s["state"] in core.BOOKABLE:
                keep.append(s)
            else:
                excluded[s["state"]] = excluded.get(s["state"], 0) + 1
        shows = keep

    if not shows:
        if excluded:
            return _err("NO_BOOKABLE_SHOWS",
                        "Shows exist at cinema %s on %s but none are bookable." % (cinema_id, date),
                        excluded=excluded,
                        hint="Pass bookable_only=false to see them.")
        return "No matching shows at cinema %s on %s (the date IS on sale)." % (cinema_id, date)

    # Titles are NOT truncated: the language and subtitle information is
    # embedded in the parenthetical suffix ("(3D ENGLISH WITH ENGLISH
    # SUBTITLE...")), so clipping the column silently destroys the one field a
    # multilingual market cares most about.
    width = max([len(s["film"] or "") for s in shows] + [30])
    lines = [
        "%-*s %-9s %-5s %-8s %-10s %s" % (width, "FILM", "TIME", "LANG", "FORMAT", "SCREEN", "STATE"),
        "-" * (width + 42),
    ]
    for s in shows:
        lines.append(
            "%-*s %-9s %-5s %-8s %-10s %s"
            % (
                width,
                s["film"] or "",
                s["time"],
                s.get("language") or "?",
                s["experience"] or s["format"] or "-",
                s["screen"][:10],
                s["state"] + ("" if s["state_verified"] else "?"),
            )
        )
    if excluded:
        lines.append("\nexcluded (not bookable): %s" % json.dumps(excluded))
    lines.append(
        "\nLanguage is in the title suffix and is the SCHEDULE language - trust it "
        "over the release languages in pvr_now_showing, which list what the film "
        "was released in, not what is playing here."
    )
    lines.append(
        "STATE is derived, not the upstream label: time decides COMPLETED/CLOSED, "
        "inventory decides ON_SALE/LIMITED/SOLD_OUT. A trailing '?' means seats "
        "were not counted - use pvr_seats to confirm before promising a booking. "
        "The upstream label is unreliable both ways: 'Lapsed' appears on shows "
        "hours in the future, and a 'Housefull' show had 1 free seat."
    )
    return _out(shows, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_seats(
    city: str,
    cinema_id: str,
    date: str,
    film: str = "",
    time: str = "",
    experience: str = "",
    party_size: int = 1,
    zone_rows: str = "",
    zone_seats: str = "",
    seat_map: bool = False,
    lat: str = "",
    lng: str = "",
    format: str = "text",
) -> str:
    """Live seat availability, with the good seats counted separately.

    This is the tool that actually answers "is this worth booking". For each
    matching show it reports free seats overall, free seats in the zone, and
    the largest run of adjacent free seats in the zone.

    party_size: how many seats you need TOGETHER. Availability is judged against
      this - "1 seat free" is not a result for a party of 2.

    THE ZONE IS A HEURISTIC, AND WIDENING IT IS THE FIX. The default zone is the
    back-centre block. When it cannot seat the party, the response reports how
    many seats are free OUTSIDE it and names the rows, e.g.

        no good seats (0 free in zone, 97% booked overall)
        zone covered rows F,E,D,C (0 free); 15 more free seats OUTSIDE it
        seats 2+ together elsewhere: N (2 together at N3-N4), O (9 at O21-O29)
        -> widen with zone_rows="F,E,D,C,N,O"

    Re-call with that zone_rows to get those blocks scored. Never report "no
    seats" to a user on the strength of the default zone alone.

    zone_rows: comma-separated row letters to override the auto zone ("F,E,D,C").
    zone_seats: "11-21" to override the seat-number range.
    seat_map: include an ASCII map. O = free in zone, x = taken in zone,
              o = free outside it, . = taken outside it.
    """
    bad = _validate(city, cinema_id, date)
    if bad:
        return bad

    shows, err = core.day_sessions(city, cinema_id, date, lat or None, lng or None)
    if err == "closed":
        last, days = core.booking_horizon(city, cinema_id, lat or None, lng or None)
        return _err("BEYOND_BOOKING_WINDOW",
                    "%s is not on sale yet at cinema %s." % (date, cinema_id),
                    on_sale_through=last, booking_window_days=days)
    if err:
        return _err("UPSTREAM_ERROR", "Could not read showtimes: %s" % err)

    if film:
        shows = [s for s in shows if film.upper() in (s["film"] or "").upper()]
    if experience:
        shows = [s for s in shows if s["experience"] == experience]
    if time:
        shows = [s for s in shows if time.lower() in s["time"].lower()]
    closed = [s for s in shows if s["status"].lower() == "lapsed" or not s["token"]]
    shows = [s for s in shows if s["status"].lower() != "lapsed" and s["token"]]
    if not shows:
        if closed:
            return _err("SHOW_NOT_BOOKABLE",
                        "%d matching show(s) exist at cinema %s on %s but none can be booked "
                        "(already started or closed)." % (len(closed), cinema_id, date),
                        times=[s["time"] for s in closed])
        return _err("SHOW_NOT_FOUND",
                    "No show matches those filters at cinema %s on %s." % (cinema_id, date))

    rows = [r.strip().upper() for r in zone_rows.split(",") if r.strip()] or None
    seats = None
    if zone_seats:
        try:
            lo, hi = zone_seats.replace("-", " ").split()
            seats = [int(lo), int(hi)]
        except ValueError:
            return "zone_seats should look like '11-21'."

    results, lines = [], []
    for show in shows[:12]:
        report, seat_err = core.seat_report(
            show["token"], rows, seats, want_map=seat_map, party_size=max(1, party_size)
        )
        if not seat_err:
            core.remember_geometry(cinema_id, show.get("screen"), report)
        if seat_err:
            lines.append("%-9s %s" % (show["time"], seat_err))
            continue
        report["film"] = show["film"]
        report["time"] = show["time"]
        report["status"] = show["status"]
        results.append(report)

        need = max(1, party_size)
        state, _ = core.show_status(show, report)
        lines.append(
            "%-9s %-8s %-10s %s"
            % (show["time"], show["experience"] or "-", state, core.describe_seats(report))
        )
        if show.get("booking_url") and state in core.BOOKABLE:
            lines.append("   book: %s" % show["booking_url"])

        # R-P0-4a: the zone is a heuristic. When it cannot seat the party, say
        # what the zone covered, what exists outside it, and how to widen it -
        # rather than reporting "no good seats" while 15 sit one row away.
        if not report["meets_party_size"]:
            lines.append(
                "   zone covered rows %s (%d free); %d more free seats OUTSIDE it"
                % (
                    ",".join(report["zone_rows"]) or "-",
                    report["zone_free"],
                    report["free_outside_zone"],
                )
            )
            alts = [a for a in report["alternatives"] if a["best_run"] >= need]
            if alts:
                shown = ", ".join(
                    "%s (%d together at %s)" % (a["row"], a["best_run"], a["best_where"])
                    for a in alts[:3]
                )
                lines.append("   seats %d+ together elsewhere: %s" % (need, shown))
                lines.append(
                    '   -> widen with zone_rows="%s"'
                    % ",".join(
                        dict.fromkeys(report["zone_rows"] + [a["row"] for a in alts[:3]])
                    )
                )
            else:
                lines.append("   no %d adjacent seats anywhere in this hall" % need)
        if seat_map:
            lines.append("   zone rows: %s" % ", ".join(report["zone_rows"]))
            lines += ["   " + line for line in report.get("map", [])]
            lines.append("")

    header = "%s - cinema %s - %s\n%s" % (
        film or "all films",
        cinema_id,
        date,
        "-" * 92,
    )
    return _out(results, header + "\n" + "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_find_shows(
    city: str,
    party_size: int,
    lat: str = "",
    lng: str = "",
    radius_km: float = 6.0,
    film: str = "",
    language: str = "",
    experience: str = "",
    date: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    bookable_only: bool = True,
    sort: str = "relevance",
    limit: int = 20,
    format: str = "text",
) -> str:
    """Answer a whole constrained query in one call: what can I actually book?

    "English, tonight, within 8 km, 2 seats together" resolves here rather than
    through one call per cinema. One request covers every cinema within ~4-5 km
    of a point, so a "near me" search is cheap; a city-wide search is not, and
    anything not searched is listed rather than silently dropped.

    party_size is REQUIRED and drives availability: a show whose largest free
    block is smaller than the party is not a result.

    date_to searches a range - ask for tonight and tomorrow in one call.

    On zero results it relaxes constraints in order (seat block -> radius ->
    time window) and says what it relaxed. Film and language are never relaxed.
    """
    if party_size is None or party_size < 1:
        return _err("PARTY_SIZE_REQUIRED",
                    "party_size must be 1 or more - availability is judged against it.")
    bad = _validate(city, None, date or None)
    if bad:
        return bad

    def run(**over):
        args = dict(city=city, lat=lat or None, lng=lng or None, radius_km=radius_km,
                    film=film or None, language=language or None,
                    experience=experience or None, date=date or None,
                    date_to=date_to or None, time_from=time_from or None,
                    time_to=time_to or None, party_size=party_size,
                    bookable_only=bookable_only, sort=sort, limit=limit)
        args.update(over)
        return core.find_shows(**args)

    results, meta = run()

    # R-P0-7: relax in a documented order, never film or language.
    if not results:
        ladder = [
            ("seat block", {"party_size": 1}),
            ("radius", {"radius_km": max(radius_km * 2.5, 15)}),
            ("time window", {"time_from": None, "time_to": None}),
        ]
        applied = {}
        for label, over in ladder:
            applied.update(over)
            results, meta = run(**applied)
            if results:
                # Report only what the RESULTS actually give up. Relaxing a
                # constraint internally then returning rows that satisfy it
                # anyway is not a relaxation - saying so would be a small lie.
                given_up = {}
                if party_size > 1 and any(
                    (r.get("seats") or {}).get("best_run", 0) < party_size for r in results
                ):
                    given_up["seats together"] = "some results seat fewer than %d" % party_size
                widest = max((r.get("distance_km") or 0) for r in results)
                if widest > radius_km:
                    given_up["radius"] = "widened to %.1f km (you asked for %.0f)" % (
                        widest, radius_km)
                if (time_from or time_to) and any(
                    (time_to and core._minutes(r["time"]) > core._minutes(time_to))
                    or (time_from and core._minutes(r["time"]) < core._minutes(time_from))
                    for r in results
                ):
                    given_up["time window"] = "outside the window you gave"
                meta["relaxed"] = given_up
                break

    if not results:
        return _err("NO_MATCH",
                    "Nothing bookable matches, even after relaxing seats, radius and time.",
                    searched=meta.get("cinemas_searched"),
                    not_searched=meta.get("cinemas_skipped"),
                    calls=meta.get("calls"))

    lines = []
    if meta.get("relaxed"):
        for what, how in sorted(meta["relaxed"].items()):
            lines.append("RELAXED %s - %s" % (what, how))
        lines.append("")
    for r in results:
        seats = r.get("seats")
        block = ""
        if seats and seats.get("best_run"):
            block = "%d together %s" % (seats["best_run"], seats["best_where"])
        elif seats:
            block = "no block of %d" % party_size
        lines.append("%s  %s  %s" % (
            r["date"], r["time"], (r["title"] or r["film"])[:38]))
        lines.append("   %-34s %s%s %s" % (
            r["cinema"][:34],
            ("%.1f km  " % r["distance_km"]) if r.get("distance_km") is not None else "",
            r["state"] + ("" if r.get("state_verified") else "?"),
            ("· " + block) if block else ""))
        if r.get("booking_url"):
            lines.append("   %s" % r["booking_url"])
    lines.append("")
    lines.append("searched %d cinema(s) in %d call(s)%s" % (
        len(meta.get("cinemas_searched") or []), meta.get("calls", 0),
        ("; NOT searched: " + ", ".join(meta["cinemas_skipped"][:4]))
        if meta.get("cinemas_skipped") else ""))
    return _out({"results": results, "meta": meta}, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_screens(city: str = "", cinema_id: str = "", format: str = "text") -> str:
    """Screen sizes for a cinema - seat count and size class per auditorium.

    The chain publishes no screen inventory, so this is built up from seat maps
    already fetched for other questions: hall geometry never changes, so each
    one is learned once and kept. A screen appears here after any call has
    looked at its seats.

    Use it for "which is the biggest screen" instead of guessing from a name
    like LASER 4.
    """
    rows = core.known_screens(cinema_id or None)
    if not rows:
        return ("No screens learned yet%s. Geometry is recorded whenever a seat "
                "map is fetched - run pvr_seats on a show at this cinema first."
                % (" for cinema %s" % cinema_id if cinema_id else ""))
    lines = ["%-8s %-14s %7s  %s" % ("CINEMA", "SCREEN", "SEATS", "SIZE"), "-" * 44]
    for r in rows:
        lines.append("%-8s %-14s %7s  %s" % (
            r["cinema_id"], r["screen"][:14], r["seats"], r["size_class"]))
    return _out(rows, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_is_open(
    city: str, cinema_id: str, date: str, lat: str = "", lng: str = ""
) -> str:
    """Is a date on sale yet at this cinema?

    Answers the one question the booking window actually poses. "Not on sale"
    means it opens later, not that it sold out.
    """
    open_now, detail = core.is_open(city, cinema_id, date, lat or None, lng or None)
    if open_now is None:
        return "Could not tell: %s" % detail
    if open_now:
        return "%s is ON SALE at cinema %s (%s)." % (date, cinema_id, detail)
    return "%s is NOT ON SALE yet at cinema %s. Bookings open roughly 5 days ahead on a rolling window." % (
        date,
        cinema_id,
    )


# --------------------------------------------------------------------------
# Managing the cron watches
# --------------------------------------------------------------------------

_WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)


def _load_watches():
    if not os.path.exists(CONFIG_PATH):
        return {"watches": []}
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def _save_watches(config):
    with open(CONFIG_PATH, "w") as fh:
        json.dump(config, fh, indent=1)
        fh.write("\n")


def _git(*args):
    return subprocess.run(
        ["git"] + list(args), cwd=HERE, capture_output=True, text=True, timeout=60
    )


@local_only(annotations=_LOOKUP)
def pvr_list_watches() -> str:
    """The watches the cron currently runs, and whether each is live."""
    config = _load_watches()
    watches = config.get("watches") or []
    if not watches:
        return "No watches configured."

    dirty = _git("status", "--porcelain", "watches.json").stdout.strip()
    lines = []
    for w in watches:
        lines.append(
            "%-40s %s | cinema %s in %s | %s%s | %s"
            % (
                w.get("name", "?"),
                "on " if w.get("enabled", True) else "OFF",
                w.get("cinema_id"),
                w.get("city"),
                w.get("film_contains", "any film"),
                " (%s)" % w["experience"] if w.get("experience") else "",
                ",".join(w.get("weekdays") or ["any day"]),
            )
        )
    if dirty:
        lines.append(
            "\nwatches.json has uncommitted changes - the cron still runs the "
            "committed version. Call pvr_publish_watches to make it live."
        )
    return "\n".join(lines)


@local_only(annotations=_WRITES)
def pvr_add_watch(
    name: str,
    city: str,
    cinema: str,
    film: str,
    experience: str = "",
    language: str = "",
    weekdays: str = "",
    min_adjacent: int = 1,
    horizon_days: int = 16,
    zone_rows: str = "",
    zone_seats: str = "",
) -> str:
    """Create a watch for the cron to poll.

    `cinema` is a name fragment ("Palazzo", "Phoenix") - it is resolved to the
    cinema id and coordinates for you. `film` is a case-insensitive substring.
    `weekdays` like "Sat,Sun" limits it to days worth going; omit for any day.

    Leave zone_rows / zone_seats empty to let the good-seats zone derive itself
    from that auditorium's geometry, which is what you normally want.

    The watch is written locally and is NOT live until published.
    """
    matches = core.list_cinemas(city, query=cinema)
    if not matches:
        return "No cinema matching %r in %s. Try pvr_cinemas to see the list." % (
            cinema,
            city,
        )
    if len(matches) > 1:
        listing = "\n".join("  %s  %s" % (m["cinema_id"], m["name"]) for m in matches[:8])
        return "%r matches %d cinemas in %s - be more specific:\n%s" % (
            cinema,
            len(matches),
            city,
            listing,
        )
    venue = matches[0]

    config = _load_watches()
    if any(w.get("name") == name for w in config.get("watches") or []):
        return "A watch named %r already exists. Remove it first, or pick another name." % name

    watch = {
        "name": name,
        "enabled": True,
        "provider": "pvr",
        "city": city,
        "cinema_id": venue["cinema_id"],
        "cinema_slug": venue["name"].replace(" ", "-"),
        "lat": venue["lat"],
        "lng": venue["lng"],
        "film_contains": film.upper(),
        "horizon_days": horizon_days,
        "alert_on_restock": True,
        "seat_detail": True,
        "min_adjacent": min_adjacent,
    }
    if experience:
        watch["experience"] = experience
    if language:
        watch["language"] = language
    if weekdays:
        watch["weekdays"] = [d.strip().title()[:3] for d in weekdays.split(",") if d.strip()]
    if zone_rows:
        watch["zone_rows"] = [r.strip().upper() for r in zone_rows.split(",") if r.strip()]
    if zone_seats:
        try:
            lo, hi = zone_seats.replace("-", " ").split()
            watch["zone_seats"] = [int(lo), int(hi)]
        except ValueError:
            return "zone_seats should look like '11-21'."

    config.setdefault("watches", []).append(watch)
    _save_watches(config)

    # Sanity-check the film against what the cinema is actually listing, so a
    # typo surfaces now rather than as months of silence.
    note = ""
    shows, err = core.day_sessions(city, venue["cinema_id"], _today(), venue["lat"], venue["lng"])
    if not err:
        hits = [s for s in shows if film.upper() in (s["film"] or "").upper()]
        if experience:
            hits = [s for s in hits if s["experience"] == experience]
        note = (
            "\nMatched %d show(s) at this cinema today, so the filters look right."
            % len(hits)
            if hits
            else "\nWARNING: nothing matches %r%s at this cinema today. Fine if the "
            "film hasn't opened yet - otherwise check the spelling with "
            "pvr_showtimes." % (film, " in " + experience if experience else "")
        )

    return (
        "Added %r: %s (cinema %s), %s%s.\n"
        "NOT LIVE YET - the cron runs the committed config. "
        "Call pvr_publish_watches to push it.%s"
        % (
            name,
            venue["name"],
            venue["cinema_id"],
            film,
            " in " + experience if experience else "",
            note,
        )
    )


@local_only(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def pvr_remove_watch(name: str) -> str:
    """Delete a watch by name. Publish afterwards to stop the cron running it."""
    config = _load_watches()
    before = len(config.get("watches") or [])
    config["watches"] = [w for w in config.get("watches") or [] if w.get("name") != name]
    if len(config["watches"]) == before:
        return "No watch named %r." % name
    _save_watches(config)
    return "Removed %r. Call pvr_publish_watches to make that live." % name


@local_only(annotations=_WRITES)
def pvr_publish_watches(message: str = "") -> str:
    """Commit and push watches.json so the GitHub Actions cron picks it up.

    This publishes to the remote repository - the watch does nothing until it
    runs. Only watches.json is committed; nothing else in the tree is touched.
    """
    status = _git("status", "--porcelain", "watches.json").stdout.strip()
    # "Clean working tree" is not the same as "live": a previous publish may
    # have committed and then failed to push, which read as already-live.
    _git("fetch", "-q", "origin", "main")
    ahead = _git("rev-list", "--count", "origin/main..HEAD").stdout.strip() or "0"
    if not status and ahead == "0":
        return "watches.json is committed and pushed - already live."
    if not status and ahead != "0":
        push = _git("push", "origin", "HEAD")
        if push.returncode:
            pull = _git("pull", "--rebase", "--autostash", "origin", "main")
            push = _git("push", "origin", "HEAD")
        return ("Pushed %s commit(s) that were committed but never pushed. Live on the "
                "next run." % ahead) if not push.returncode else (
                "Could not push:\n%s" % push.stderr.strip())

    _git("add", "watches.json")
    commit = _git("commit", "-m", message or "watches: update via MCP")
    if commit.returncode:
        return "Commit failed:\n%s%s" % (commit.stdout, commit.stderr)

    push = _git("push", "origin", "HEAD")
    if push.returncode:
        # The cron commits state.json on every run, so the remote moves under
        # us constantly. Rebase onto it and retry once before giving up.
        pull = _git("pull", "--rebase", "--autostash", "origin", "main")
        if pull.returncode:
            return ("Committed locally, and rebasing onto the remote failed:\n%s\n"
                    "Resolve by hand." % pull.stderr.strip())
        push = _git("push", "origin", "HEAD")
        if push.returncode:
            return ("Committed locally but push still failed after rebase:\n%s"
                    % push.stderr.strip())
    return "Published. The cron picks up the new config on its next run (within ~5-15 min)."


def _today():
    import datetime

    return core.today_ist().isoformat()


# Only the endpoints this project actually uses. An open forwarder would let
# anyone route arbitrary traffic through this service's IP.
PROXY_PATHS = {
    "content/csessions",
    "content/cinemasessions",
    "content/cinemas",
    "content/nowshowing",
    "content/city",
    "ticketing/seatlayout",
}


@mcp.custom_route("/proxy/{path:path}", methods=["POST"])
async def _proxy(request):
    """Forward one upstream call, for clients whose IP the chain refuses.

    Token-gated: this lends out the service's IP reputation, which is the whole
    point and also the risk.
    """
    token = os.environ.get("PVR_PROXY_TOKEN", "")
    if not token:
        return JSONResponse({"error": "proxy_disabled"}, status_code=404)
    if request.headers.get("x-proxy-token") != token:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    path = request.path_params["path"]
    if path not in PROXY_PATHS:
        return JSONResponse({"error": "path_not_allowed", "path": path}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)

    try:
        data = core._post(path, body, request.headers.get("x-city", "Chennai"))
    except core.Blocked as exc:
        return JSONResponse({"error": "blocked", "detail": str(exc)}, status_code=429)
    except Exception as exc:
        return JSONResponse({"error": "upstream", "detail": str(exc)}, status_code=502)
    return JSONResponse(data)


@mcp.custom_route("/icon.png", methods=["GET"])
async def _serve_png(_request):
    return FileResponse(
        os.path.join(HERE, "icon.png"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@mcp.custom_route("/icon.svg", methods=["GET"])
async def _serve_svg(_request):
    return FileResponse(
        os.path.join(HERE, "icon.svg"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Some UIs fall back to the origin's favicon when they cannot use the declared
# icon, so answer that too.
@mcp.custom_route("/favicon.ico", methods=["GET"])
async def _serve_favicon(_request):
    return FileResponse(os.path.join(HERE, "icon.png"), media_type="image/png")


def main():
    """stdio by default; PVR_MCP_TRANSPORT=streamable-http to serve remotely."""
    transport = os.environ.get("PVR_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
        return

    # Cloud Run and most PaaS inject PORT and require binding 0.0.0.0.
    mcp.settings.host = os.environ.get("PVR_MCP_HOST", "127.0.0.1")
    mcp.settings.port = int(
        os.environ.get("PORT") or os.environ.get("PVR_MCP_PORT", "8760")
    )
    mcp.settings.streamable_http_path = os.environ.get("PVR_MCP_PATH", "/mcp")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
