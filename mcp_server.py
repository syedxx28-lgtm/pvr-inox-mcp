#!/usr/bin/env python3
"""
PVR INOX MCP server. Thin stdio transport over core.py.

All the logic lives in core, which is stdlib-only and drives the cron watcher
too. This module only exposes it as MCP tools so any client can ask about
cinema showtimes and, more usefully, which seats are actually free.

Run:  python3 mcp_server.py
"""

import base64
import json
import os
import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations

import core

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

You can also manage the cron watches that alert Slack when a booking window
opens: pvr_add_watch / pvr_list_watches / pvr_remove_watch.

Adding a watch only edits a local file. The cron runs the COMMITTED config, so
nothing takes effect until pvr_publish_watches pushes it. Always tell the
user a new watch is not live yet, and never publish unless they ask for it - it
pushes to a public repository.

Every tool returns a compact table by default. Pass format="json" for the full
structured result when you need to compute over it rather than report it.
"""

# Inlined as a data URI so the server stays self-contained - no asset host to
# depend on, and it survives being copied anywhere.
def _icon():
    path = os.path.join(HERE, "icon.svg")
    try:
        with open(path, "rb") as fh:
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


def _out(payload, text, fmt):
    if (fmt or "text").strip().lower() == "json":
        return json.dumps(payload, indent=2, default=str)
    return text


@mcp.tool(annotations=_LOOKUP)
def pvr_cities() -> str:
    """List every city where PVR/INOX sells tickets."""
    cities = core.list_cities()
    return "%d cities:\n%s" % (len(cities), ", ".join(cities))


@mcp.tool(annotations=_LOOKUP)
def pvr_cinemas(
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
def pvr_now_showing(
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
def pvr_showtimes(
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
    lines.append("\nStatus is show-level and hides seat quality - use pvr_seats.")
    return _out(shows, "\n".join(lines), format)


@mcp.tool(annotations=_LOOKUP)
def pvr_seats(
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
    if not status:
        return "watches.json has no uncommitted changes - already live."

    _git("add", "watches.json")
    commit = _git("commit", "-m", message or "watches: update via MCP")
    if commit.returncode:
        return "Commit failed:\n%s%s" % (commit.stdout, commit.stderr)

    push = _git("push", "origin", "HEAD")
    if push.returncode:
        return "Committed locally but push failed:\n%s\nPush by hand to go live." % push.stderr.strip()
    return "Published. The cron picks up the new config on its next run (within ~5-15 min)."


def _today():
    import datetime

    return datetime.date.today().isoformat()


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
