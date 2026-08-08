#!/usr/bin/env python3
"""
showwatch MCP server. Thin stdio transport over core.py.

All the logic lives in core, which is stdlib-only and drives the cron watcher
too. This module only exposes it as MCP tools so any client can ask about
cinema showtimes and, more usefully, which seats are actually free.

Run:  python3 mcp_server.py
"""

import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import core

INSTRUCTIONS = """
You can look up films, showtimes and live seat availability at any PVR or INOX
cinema in India - about 116 cities.

Typical flow: showwatch_cinemas to find the cinema id, then showwatch_seats to
see what is actually bookable. showwatch_now_showing answers "what's on".

Things that matter when answering:

- SHOW-LEVEL STATUS IS MISLEADING. A show marked "Available" or "Filling Up
  Fast" is routinely stripped of every decent seat. Shows with 100+ free seats
  have had zero free in the back-centre. Always check showwatch_seats before
  telling someone a show is worth booking, and lead with the zone figure.

- The "zone" is the good seats: the rows 60-85% of the way back, and the
  aisle-delimited centre block of each. It is derived from each auditorium's
  own geometry, so it works anywhere - row letters mean different things in
  different houses. Override with zone_rows / zone_seats when the user has a
  specific preference.

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

Every tool returns a compact table by default. Pass format="json" for the full
structured result when you need to compute over it rather than report it.
"""

mcp = FastMCP("showwatch", instructions=INSTRUCTIONS)

# Everything here reads a public booking API and writes nothing.
_LOOKUP = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


def _out(payload, text, fmt):
    if (fmt or "text").strip().lower() == "json":
        return json.dumps(payload, indent=2, default=str)
    return text


@mcp.tool(annotations=_LOOKUP)
def showwatch_cities() -> str:
    """List every city where PVR/INOX sells tickets."""
    cities = core.list_cities()
    return "%d cities:\n%s" % (len(cities), ", ".join(cities))


@mcp.tool(annotations=_LOOKUP)
def showwatch_cinemas(
    city: str, query: str = "", lat: str = "", lng: str = "", format: str = "text"
) -> str:
    """Cinemas in a city, busiest first. `query` filters on the name.

    Returns the cinema_id that every other tool needs. Pass lat/lng when you
    know where the user is - the list is distance-filtered.
    """
    rows = core.list_cinemas(city, lat or None, lng or None, query)
    if not rows:
        return "No cinemas found in %s%s." % (city, " matching %r" % query if query else "")

    lines = ["%-6s %-46s %-11s %s" % ("ID", "CINEMA", "DISTANCE", "SHOWS"), "-" * 78]
    for r in rows:
        lines.append(
            "%-6s %-46s %-11s %s"
            % (r["cinema_id"], r["name"][:46], r["distance"] or "-", r["shows"])
        )
    return _out(rows, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def showwatch_now_showing(
    city: str, lat: str = "", lng: str = "", format: str = "text"
) -> str:
    """Films currently playing in a city, with certificate, length and formats."""
    films = core.now_showing(city, lat or None, lng or None)
    if not films:
        return "Nothing showing in %s." % city

    lines = [
        "%-34s %-7s %-7s %-20s %-6s %s"
        % ("FILM", "CERT", "LENGTH", "LANGUAGES", "SHOWS", "FORMATS"),
        "-" * 100,
    ]
    for f in films:
        lines.append(
            "%-34s %-7s %-7s %-20s %-6d %s"
            % (
                (f["name"] or "")[:34],
                f["certificate"],
                f["length"],
                ", ".join(f["languages"])[:20],
                f["shows"],
                ", ".join(f["formats"]),
            )
        )
    return _out(films, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def showwatch_showtimes(
    city: str,
    cinema_id: str,
    date: str,
    film: str = "",
    experience: str = "",
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
    shows, err = core.day_sessions(city, cinema_id, date, lat or None, lng or None)
    if err == "closed":
        return "%s is NOT ON SALE yet at cinema %s. The chain books about 5 days ahead, so it should open closer to the date." % (
            date,
            cinema_id,
        )
    if err:
        return "Could not read showtimes: %s" % err

    if film:
        shows = [s for s in shows if film.upper() in (s["film"] or "").upper()]
    if experience:
        shows = [s for s in shows if s["experience"] == experience]
    if not shows:
        return "No matching shows at cinema %s on %s." % (cinema_id, date)

    lines = [
        "%-42s %-9s %-8s %-10s %s" % ("FILM", "TIME", "FORMAT", "SCREEN", "STATUS"),
        "-" * 92,
    ]
    for s in shows:
        lines.append(
            "%-42s %-9s %-8s %-10s %s"
            % (
                (s["film"] or "")[:42],
                s["time"],
                s["experience"] or s["format"] or "-",
                s["screen"][:10],
                s["status"],
            )
        )
    lines.append("\nStatus is show-level and hides seat quality - use showwatch_seats.")
    return _out(shows, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def showwatch_seats(
    city: str,
    cinema_id: str,
    date: str,
    film: str = "",
    time: str = "",
    experience: str = "",
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

    zone_rows: comma-separated row letters to override the auto zone ("F,E,D,C").
    zone_seats: "11-21" to override the seat-number range.
    seat_map: include an ASCII map. O = free in zone, x = taken in zone,
              o = free outside it, . = taken outside it.
    """
    shows, err = core.day_sessions(city, cinema_id, date, lat or None, lng or None)
    if err == "closed":
        return "%s is NOT ON SALE yet at cinema %s." % (date, cinema_id)
    if err:
        return "Could not read showtimes: %s" % err

    if film:
        shows = [s for s in shows if film.upper() in (s["film"] or "").upper()]
    if experience:
        shows = [s for s in shows if s["experience"] == experience]
    if time:
        shows = [s for s in shows if time.lower() in s["time"].lower()]
    shows = [s for s in shows if s["status"].lower() != "lapsed" and s["token"]]
    if not shows:
        return "No bookable shows match at cinema %s on %s." % (cinema_id, date)

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
        report, seat_err = core.seat_report(show["token"], rows, seats, want_map=seat_map)
        if seat_err:
            lines.append("%-9s %s" % (show["time"], seat_err))
            continue
        report["film"] = show["film"]
        report["time"] = show["time"]
        report["status"] = show["status"]
        results.append(report)

        lines.append(
            "%-9s %-8s %-22s %s"
            % (
                show["time"],
                show["experience"] or "-",
                show["status"][:22],
                core.describe_seats(report),
            )
        )
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
def showwatch_is_open(
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
