#!/usr/bin/env python3
"""
showwatch - watch cinema booking APIs and shout the moment something opens.

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

Alerts go to whichever channels are configured - see notify.py. Slack,
Telegram, ntfy, Pushover, Discord, email, a generic webhook, or a GitHub
issue. Set the environment variables for the one you want.

  python watch.py                 poll, diff, notify
  python watch.py --dry-run       poll and print, notify nobody, save nothing
  python watch.py --show-all      print every matching show, not just changes
"""

import argparse
import datetime
import json
import os
import sys
from concurrent import futures

import core
import notify

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "watches.json")
STATE_PATH = os.path.join(HERE, "state.json")


def pvr_fetch_day(watch, date_str):
    return core.day_sessions(
        watch["city"], watch["cinema_id"], date_str, watch.get("lat"), watch.get("lng")
    )


def pvr_fetch_seats(token, watch):
    return core.seat_report(token, watch.get("zone_rows"), watch.get("zone_seats"))


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

    want_days = watch.get("weekdays")

    for offset in range(watch.get("horizon_days", 12)):
        date = today + datetime.timedelta(days=offset)
        date_str = date.isoformat()

        # Only the days you'd actually go. Also keeps the request count down,
        # since each open date costs a seat-map call per showtime.
        if want_days and date.strftime("%a") not in want_days:
            continue

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
                    todo, pool.map(lambda s: pvr_fetch_seats(s["token"], watch), todo)
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
                            " zone %d/%d"
                            % (s["seats"]["zone_free"], s["seats"]["zone_total"])
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

            # A show that frees up a seat in the zone is the case worth waking
            # for. Alert only when the best block crosses the threshold from
            # below, so a show sitting above it does not re-fire every run.
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
    # Real emoji, not Slack :codes: - these have to render on Telegram, ntfy,
    # email and a GitHub issue too.
    "new_date": "\U0001F6A8 Booking just opened",
    "new_show": "\U0001F195 Show added",
    "back_in_stock": "\u267B\uFE0F Back in stock",
    "seats_freed": "\U0001FA91 Good seats opened up",
}


def seat_summary(show):
    return core.describe_seats(show.get("seats"))


def format_alert(watch, events):
    """Plain text, so it renders the same on every channel. Returns (title, body, url)."""
    url = PROVIDERS[watch.get("provider", "pvr")][1](watch, events[0]["date"])

    lines = []
    for ev in events:
        pretty = datetime.date.fromisoformat(ev["date"]).strftime("%a %d %b")
        lines.append("%s - %s" % (HEADLINES[ev["kind"]], pretty))
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

    return watch["name"], "\n".join(lines), url


def deliver(title, body, url):
    """True only if at least one channel actually took it."""
    channels = notify.configured()
    if not channels:
        print(
            "No notification channel configured - printing instead:\n%s\n%s\n%s"
            % (title, body, url)
        )
        return False

    sent, failed = notify.send(title, body, url)
    for problem in failed:
        print("  delivery failed - %s" % problem, file=sys.stderr)
    if sent:
        print("  alerted via %s" % ", ".join(sent))
    return bool(sent)


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="never write state or notify")
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
                title, body, url = format_alert(watch, events)
                if args.dry_run:
                    print("%s\n%s\n%s" % (title, body, url))
                elif not deliver(title, body, url):
                    # Advancing state here would swallow the opening for good -
                    # it can only ever be reported once. Leave the old state so
                    # the next run fires it again.
                    print("  NOT DELIVERED - state held back", file=sys.stderr)
                    continue
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
