# showwatch

Watches cinema booking APIs and pings Slack the moment a show you want becomes
bookable. Built because IMAX tickets at PVR Palazzo sell out before you notice
the window opened.

Stdlib-only Python. No pip install, no browser, no scraping - it reads the same
JSON API the PVR web app calls.

## What it alerts on

| Event | Meaning |
|---|---|
| `new_date` | A date that answered "closed" now has shows. **The booking window just opened.** |
| `new_show` | An extra session appeared on an already-open date. |
| `back_in_stock` | A session went from Sold Out back to Available - a cancellation or a released block. |
| `seats_freed` | Seats opened up **inside the zone** - the rows and centre block you actually want. The one that matters. |

## How it works

`POST https://api3.pvrcinemas.com/api/v1/booking/content/csessions` with a
cinema id and a date returns that day's sessions. Two findings make this cheap
and reliable:

- **A date not yet open for booking answers `status: 500`.** So "has the window
  opened" is a boolean, not a diff of show lists.
- The endpoint needs an `Authorization: Bearer ` header with an **empty** token.
  Without the header it 403s; with it blank it works. No login, no key.

Every run polls `horizon_days` forward, keeps the shows matching your filters,
and diffs against `state.json` from the previous run.

### Seat-level detail

With `seat_detail: true`, each showtime is followed up with
`POST /api/v1/booking/ticketing/seatlayout` using the `encrypted` token that
`csessions` returns per session. That gives the full seat map:

- `s == 1` is a free seat, `s == 2` is taken (verified against the rendered map)
- entries with no seat name (`sn`) are aisles and gaps - these **break**
  adjacency, since seats either side of an aisle are not "together"

### The zone is the point

A show-level `Available` is close to meaningless: the good seats go first. On
AUDI 5 at Palazzo, shows sitting at 37-114 free seats had **zero** free in the
back-centre block. So `zone_rows` x `zone_seats` defines the seats you'd
actually sit in, and only those trigger `seats_freed`.

AUDI 5 is 15 rows, **O nearest the screen through A at the back**, each split
into three blocks by two aisles - the centre block is seats **11-21**:

```
              SCREEN
 O    1-9      11-21     22-29      front
 N    1-10     11-21     22-31
 ...
 G    1-6      11-21     22-23      (narrow rows)
 D    1-10     11-21     22-31
 A    ----------- 1-34 ----------   back wall
```

A seat outside the zone also **breaks** adjacency, so a run can never straddle
the zone edge and report seats you don't want as part of a block.

Alerts then read
`GOOD SEATS: 11 together at D11-D21 (40 free in zone, 15% booked overall)`.

This costs one extra request per showtime, so the calls are issued
concurrently. Already-started ("Lapsed") shows are skipped - they have no seat
map. Sold-out ones are still fetched, since that is where a restock shows up.

Note `bookmyshow.com` is fully Cloudflare-gated - every plain request, including
the mobile-app endpoints with correct headers, returns 403. Going through PVR
direct avoids that entirely.

## Config

`watches.json`:

```json
{
 "watches": [
  {
   "name": "The Odyssey - IMAX - PVR Palazzo",
   "provider": "pvr",
   "city": "Chennai",
   "cinema_id": "388",
   "cinema_slug": "PVR-Palazzo-The-Nexus-Vijaya-Mall",
   "lat": "13.05053777",
   "lng": "80.2093132",
   "film_contains": "ODYSSEY",
   "experience": "imax",
   "language": "English",
   "horizon_days": 16,
   "weekdays": ["Sat", "Sun"],
   "alert_on_restock": true,
   "seat_detail": true,
   "zone_rows": ["F", "E", "D", "C"],
   "zone_seats": [11, 21],
   "min_adjacent": 1
  }
 ]
}
```

`weekdays` limits the watch to days you'd actually go (`%a` names - `Mon`,
`Sat`...). Omit it to watch every day. It also cuts the request count sharply,
since every open date costs one seat-map call per showtime - so `horizon_days`
can reach further out for the same work.

`zone_rows` and `zone_seats` (inclusive seat numbers) define the good seats.
Omit both to treat the whole auditorium as the zone.

`min_adjacent` is how many seats side by side you need; `seats_freed` fires only
when a show crosses that threshold from below, so a show already above it does
not re-fire every run. Set it to `0` to switch that off.

`film_contains` is a case-insensitive substring of the film name.
`experience` matches PVR's key (`imax`, `pxl`, `bigpix`, `4dx`, ...); leave it
empty for any format. `lat`/`lng` should be the cinema's own coordinates - the
API applies a distance filter and will drop the cinema if you are too far.

### Finding a cinema id

```bash
curl -s -X POST https://api3.pvrcinemas.com/api/v1/booking/content/cinemas \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer ' \
  -H 'chain: PVR' -H 'city: Chennai' -H 'country: INDIA' \
  -H 'appVersion: 1.0' -H 'platform: WEBSITE' -H 'flow: PVRINOX' \
  -d '{"city":"Chennai","lat":"13.08","lng":"80.27","text":""}' \
  | python3 -c 'import json,sys;[print(c["theatreId"],c["name"]) for c in json.load(sys.stdin)["output"]["cinemas"]]'
```

## Running

```bash
python watch.py --dry-run --show-all   # poll and print, touches nothing
python watch.py                        # poll, diff, alert, save state
python watch.py --watch "<name>"       # just one watch
```

The first run records a baseline silently - otherwise every currently-open date
would fire as a discovery.

## Deploy

Runs on GitHub Actions cron (`.github/workflows/watch.yml`), every 5 minutes,
committing `state.json` back to the repo so the diff survives between runs.

1. Push this to its **own repo**. Keep it public - 5-minute cron on a private
   repo burns ~8,600 Actions minutes a month against a 2,000 free allowance.
   Nothing sensitive is in the code.
2. Slack: create an Incoming Webhook pointed at the channel or DM you want.
3. Add it as repo secret **`SLACK_WEBHOOK_URL`**.
4. Actions tab -> showwatch -> Run workflow, to record the baseline.

### Timing caveat

GitHub's cron floor is 5 minutes and scheduled runs are routinely delayed 5-15
minutes under load, occasionally skipped. That is fine for catching a booking
window opening - the listing goes up in a batch and stays up. It will not win a
seat race measured in seconds. If that matters, move the same script to an
always-on box on a 30-second loop; nothing in it is Actions-specific.
