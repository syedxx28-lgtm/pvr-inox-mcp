#!/usr/bin/env python3
"""
pvr-inox-mcp - watch PVR/INOX booking APIs and shout the moment something opens.

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

    # Accept either "English" or "en" in config; shows now carry ISO codes.
    want_lang = watch.get("language", "")
    if want_lang:
        wanted = core.lang_code(want_lang) or str(want_lang).lower()
        if (show.get("language") or "") != wanted:
            return False

    return True


def show_key(show):
    """Stable identity for a session across runs. sessionId alone is reused."""
    return "%s|%s|%s" % (show["date"], show["time"], show["screen"])


def poll(watch, today, verbose=False):
    """Poll the forward window. Returns {date: {show_key: show}} for open dates.

    Sets poll.answered to the number of dates the upstream actually answered
    for. A "closed" date counts: the API replied, it just has nothing on sale.
    Only network failures and blocks count as no answer - that distinction is
    what the heartbeat needs.
    """
    fetch_day, _ = PROVIDERS[watch.get("provider", "pvr")]
    snapshot = {}
    answered = 0

    want_days = watch.get("weekdays")

    for offset in range(watch.get("horizon_days", 12)):
        date = today + datetime.timedelta(days=offset)
        date_str = date.isoformat()

        # Only the days you'd actually go. Also keeps the request count down,
        # since each open date costs a seat-map call per showtime.
        if want_days and date.strftime("%a") not in want_days:
            continue

        shows, err = fetch_day(watch, date_str)

        if err and err.startswith("blocked"):
            # Every further request digs the hole deeper. Abandon this cycle.
            print("  UPSTREAM BLOCKED - %s" % err, file=sys.stderr)
            raise core.Blocked(err)

        if err == "closed":
            answered += 1
            if verbose:
                print("  %s  closed" % date_str)
            continue
        if err:
            # A transient network error must not look like a closed date,
            # otherwise it re-fires as new_date on the next successful run.
            print("  %s  %s (skipped)" % (date_str, err), file=sys.stderr)
            snapshot[date_str] = None
            continue

        answered += 1
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

    poll.answered = answered
    return snapshot


HEARTBEAT_KEY = "__heartbeat__"
HEARTBEAT_HOURS = float(os.environ.get("PVR_HEARTBEAT_HOURS", "6"))


def check_heartbeat(state, answered_total, dry_run=False):
    """Alert when the watch has gone blind.

    A blocked or broken watcher fails silently and looks exactly like "nothing
    has opened yet" - which is how three days of green Actions runs detected
    nothing. Silence is not evidence, so absence of a successful poll is
    itself reported.
    """
    beat = state.get(HEARTBEAT_KEY) or {}
    now = core.now_ist()
    stamp = now.isoformat(timespec="seconds")

    if answered_total:
        state[HEARTBEAT_KEY] = {"last_success": stamp, "last_alert": beat.get("last_alert")}
        return False

    last = beat.get("last_success")
    if not last:
        state[HEARTBEAT_KEY] = {"last_success": stamp, "last_alert": beat.get("last_alert")}
        return False

    quiet_h = (now - datetime.datetime.fromisoformat(last)).total_seconds() / 3600.0
    if quiet_h < HEARTBEAT_HOURS:
        return False

    # Do not repeat more often than the threshold itself.
    alerted = beat.get("last_alert")
    if alerted:
        since = (now - datetime.datetime.fromisoformat(alerted)).total_seconds() / 3600.0
        if since < HEARTBEAT_HOURS:
            return False

    title = "\u26a0\ufe0f Watch is blind"
    body = ("No successful poll for %.1f hours (last: %s).\n"
            "The watcher is running but cannot reach the cinema API, so it "
            "would NOT see a booking window open." % (quiet_h, last[:16].replace("T", " ")))
    if dry_run:
        print("%s\n%s" % (title, body))
    else:
        deliver(title, body, "", 5)
    beat["last_alert"] = stamp
    state[HEARTBEAT_KEY] = beat
    return True


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------

ICON = "\U0001F6A8"
AVAILABLE = {"available", "filling up fast", "filling fast"}


def lead_ok(show, watch, now_ms):
    """Is there still time to actually get there?

    An alert for a show starting in 11 minutes at a cinema 25 km away is
    accurate and useless. Shows nearer than min_lead_minutes are still tracked
    in state - they are just not worth waking someone for.
    """
    need = watch.get("min_lead_minutes", 0)
    if not need or not show.get("ts"):
        return True
    return (show["ts"] - now_ms) >= need * 60000


def diff(watch, previous, snapshot):
    """Compare against the last run and return a list of alert-worthy events."""
    events = []
    now_ms = int(core.now_ist().timestamp() * 1000)
    open_now = {d: v for d, v in snapshot.items() if v is not None}

    for date_str in sorted(open_now):
        shows = open_now[date_str]
        was = previous.get(date_str)

        if was is None:
            # None here means "not in previous state at all" -> the window opened.
            in_time = [s for s in shows.values() if lead_ok(s, watch, now_ms)]
            if in_time:
                events.append(
                    {
                        "kind": "new_date",
                        "date": date_str,
                        "shows": sorted(in_time, key=lambda s: s.get("ts", 0)),
                    }
                )
            continue

        for key, show in sorted(shows.items()):
            before = was.get(key)
            if not lead_ok(show, watch, now_ms):
                continue
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


def short_seats(show):
    """The seat read at a glance: '11 together - D11-D21'.

    A phone notification gets three or four lines before it truncates, so this
    drops everything you already know (the screen, the film, the percentage)
    and keeps only what decides whether you stop what you're doing.
    """
    report = show.get("seats")
    if not report:
        return show.get("status", "")
    if not report.get("best_run"):
        return "no good seats"
    if report["best_run"] == 1:
        return "1 seat - %s" % report["best_where"]
    return "%d together - %s" % (report["best_run"], report["best_where"])


def format_alert(watch, events):
    """Plain text, so it renders the same on every channel.

    Returns (title, body, url, priority). The title carries the event and the
    date, because on a lock screen that is often all you read.
    """
    url = PROVIDERS[watch.get("provider", "pvr")][1](watch, events[0]["date"])

    def pretty(date_str):
        return datetime.date.fromisoformat(date_str).strftime("%a %-d %b")

    if len(events) == 1:
        ev = events[0]
        title = "%s - %s" % (HEADLINES[ev["kind"]], pretty(ev["date"]))
    else:
        dates = sorted({pretty(e["date"]) for e in events})
        title = "%s %d updates - %s" % (
            ICON,
            len(events),
            ", ".join(dates[:3]) + ("..." if len(dates) > 3 else ""),
        )

    lines = [watch["name"]]
    for ev in events:
        if len(events) > 1:
            lines.append("")
            lines.append("%s - %s" % (HEADLINES[ev["kind"]], pretty(ev["date"])))
        lines.append("")
        for s in ev["shows"]:
            # Two columns, so the times line up and the eye runs straight down
            # the seat column.
            lines.append("%-9s %s" % (s["time"], short_seats(s)))

    # Anything that needs you to act now goes at max priority, so it breaks
    # through Do Not Disturb. An extra show on an already-open date does not.
    urgent = {"new_date", "seats_freed"}
    priority = 5 if any(e["kind"] in urgent for e in events) else 4

    return title, "\n".join(lines), url, priority


def deliver(title, body, url, priority=5):
    """True only if at least one channel actually took it."""
    channels = notify.configured()
    if not channels:
        print(
            "No notification channel configured - printing instead:\n%s\n%s\n%s"
            % (title, body, url)
        )
        return False

    sent, failed = notify.send(title, body, url, priority)
    for problem in failed:
        print("  delivery failed - %s" % problem, file=sys.stderr)
    if sent:
        print("  alerted via %s" % ", ".join(sent))
    return bool(sent)


# --------------------------------------------------------------------------


def stream(config, state, args, state_path):
    """Poll forever, printing ONE LINE per event on stdout.

    This is the shape an agent's watch tool wants: keep the polling in Python,
    where it costs nothing, and surface only actual events to the model. The
    alternative - waking a model every few minutes to poll an API itself - buys
    nothing and burns a turn each time.

    Nothing is delivered to a notification channel here; the caller reading
    stdout is the channel.
    """
    import time

    while True:
        today = core.today_ist()
        for watch in config["watches"]:
            if not watch.get("enabled", True):
                continue
            if args.watch and watch["name"] != args.watch:
                continue

            try:
                snapshot = poll(watch, today)
            except core.Blocked as exc:
                print("  paused: %s" % exc, file=sys.stderr)
                continue
            previous = state.get(watch["name"], {})
            if previous:
                for ev in diff(watch, previous, snapshot):
                    when = datetime.date.fromisoformat(ev["date"]).strftime("%a %-d %b")
                    for show in ev["shows"]:
                        print(
                            "%s | %s | %s %s | %s"
                            % (
                                HEADLINES[ev["kind"]],
                                watch["name"],
                                when,
                                show["time"],
                                short_seats(show),
                            ),
                            flush=True,  # unbuffered, or events sit unseen
                        )

            merged = dict(previous)
            for date_str, shows in snapshot.items():
                if shows is not None:
                    merged[date_str] = shows
            cutoff = today.isoformat()
            state[watch["name"]] = {d: v for d, v in merged.items() if d >= cutoff}

        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        time.sleep(max(30, args.interval))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="never write state or notify")
    ap.add_argument("--show-all", action="store_true", help="print every matching show")
    ap.add_argument("--watch", help="run only the watch with this name")
    ap.add_argument(
        "--stream",
        action="store_true",
        help="poll forever, print one line per event (for an agent to watch)",
    )
    ap.add_argument(
        "--interval", type=int, default=60, help="seconds between polls in --stream"
    )
    ap.add_argument(
        "--state",
        default=STATE_PATH,
        help="state file to use. Point an in-session --stream at its own copy "
        "so it does not fight the cron over the committed one.",
    )
    args = ap.parse_args()

    with open(CONFIG_PATH) as fh:
        config = json.load(fh)

    state_path = args.state
    state = {}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)

    if args.stream:
        return stream(config, state, args, state_path)

    today = core.today_ist()
    fired = 0
    blocked = 0
    answered_total = 0

    for watch in config["watches"]:
        if not watch.get("enabled", True):
            continue
        if args.watch and watch["name"] != args.watch:
            continue

        print("%s" % watch["name"])
        try:
            snapshot = poll(watch, today, verbose=args.show_all)
        except core.Blocked as exc:
            # Upstream is refusing us. Skipping a cycle is the correct outcome -
            # crashing the job turns a transient block into a red workflow and
            # loses the run's state write.
            print("  skipped: %s" % exc, file=sys.stderr)
            blocked += 1
            continue
        answered_total += getattr(poll, "answered", 0)
        previous = state.get(watch["name"], {})

        # First ever run: record the baseline silently, or every open date
        # would fire as a discovery.
        if not previous:
            # Count only dates that actually answered - an errored date is not
            # an open one, and saying so overstates what was recorded.
            print("  baseline recorded (%d open date(s))"
                  % len([v for v in snapshot.values() if v]))
        else:
            events = diff(watch, previous, snapshot)
            if events:
                fired += len(events)
                title, body, url, priority = format_alert(watch, events)
                if args.dry_run:
                    print("%s\n%s\n%s" % (title, body, url))
                elif not deliver(title, body, url, priority):
                    # Advancing state here would swallow the opening for good -
                    # it can only ever be reported once. Leave the old state so
                    # the next run fires it again.
                    print("  NOT DELIVERED - state held back", file=sys.stderr)
                    continue
            else:
                print("  no change (%d open date(s))"
                      % len([v for v in snapshot.values() if v]))

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

    if check_heartbeat(state, answered_total, args.dry_run):
        print("  heartbeat ALERT sent - watch has gone blind", file=sys.stderr)

    if not args.dry_run:
        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)

    if blocked:
        print("%d watch(es) skipped - upstream blocked this runner" % blocked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
