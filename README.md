# pvr-inox-mcp

Watches cinema booking APIs and pings you the moment a show you want becomes
bookable - ntfy, Telegram, Pushover, Slack, Discord, email, or a webhook.
Built because IMAX tickets at PVR Palazzo sell out before you notice the
window opened.

Stdlib-only Python. No pip install, no browser, no scraping - it reads the same
JSON API the PVR web app calls.

Two ways in:

- **`watch.py`** - the cron watcher. Polls on a schedule, alerts you.
- **`mcp_server.py`** - an MCP server, so you can just *ask*: what's showing,
  where are the good seats, is that date on sale yet. Any city in India.

Both sit on `core.py`, which is the actual API client.

**Coverage: the PVR/INOX chain only**, across ~116 Indian cities. Independent
cinemas and other chains are not here, and neither is BookMyShow - it blocks
automated requests outright.

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

#### The zone derives itself

Row letters mean different things in different houses - Palazzo's AUDI 5 runs
O at the front to A at the back over 15 rows; Phoenix's IMAX runs P to A over
16. So hardcoded rows never transfer.

With `zone_rows` / `zone_seats` omitted, the zone is computed from the
auditorium's own geometry:

- **rows** 60-85% of the way back from the screen
- **seats** the aisle-delimited block containing the row's midpoint - the
  actual centre section, not a naive "middle half" that would straddle aisles

That reproduces a hand-picked `F,E,D,C` + `11-21` exactly on Palazzo (44
seats), and independently derives `G,F,E,D,C` (80 seats) on Phoenix. Set the
keys explicitly only to override it.

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

`zone_rows` and `zone_seats` (inclusive seat numbers) override the good-seats
zone. Omit both and it derives itself from the auditorium's geometry, which is
usually what you want - see above.

`min_lead_minutes` suppresses alerts for shows starting sooner than that - an
alert for a show beginning in 11 minutes at a cinema 25 km away is accurate and
useless. Those shows are still tracked in state, just not alerted on. Omit it
to alert regardless.

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

## MCP server

Exposes the same core as tools, so any MCP client can ask instead of you
writing throwaway scripts.

```bash
pip install -r requirements.txt
claude mcp add pvr-inox -- python3 /path/to/pvr-inox-mcp/mcp_server.py
```

### Hosted endpoint

```
https://pvr-inox-mcp-612942167838.asia-south1.run.app/mcp
```

Add it in Claude as a custom connector ("Remote MCP server URL"). No auth -
it is read-only lookups over a public API.

Deployed to Cloud Run (`asia-south1`, close to the origin), scale-to-zero:

```bash
gcloud run deploy pvr-inox-mcp --source . --region=asia-south1 \
  --allow-unauthenticated \
  --set-env-vars="PVR_MCP_TRANSPORT=streamable-http,PVR_MCP_HOST=0.0.0.0,\
PVR_MCP_PATH=/mcp,PVR_MCP_ALLOWED_HOSTS=<your-run-hostname>"
```

`PVR_MCP_ALLOWED_HOSTS` is required, and for a public connector it should be
`*`. MCP enables DNS-rebinding protection by default, which checks **two**
headers:

- **`Host`** - validated against localhost only, so a hosted deployment answers
  **HTTP 421** until its own hostname is listed.
- **`Origin`** - a browser-based client such as Claude's connector sends
  `Origin: https://claude.ai`, which is rejected with **HTTP 403 "Invalid
  Origin header"** unless that origin is allowed. The connector just spins.

A client called with no `Origin` header at all passes both checks, so testing
with curl or a Python client will not reveal the second problem. `*` turns the
protection off, which is the right setting for an intentionally public,
read-only endpoint.

### Serving it remotely

```bash
PVR_MCP_TRANSPORT=streamable-http PVR_MCP_PORT=8760 python3 mcp_server.py
```

Put TLS in front of it and the URL works as a custom connector.

**The four watch-management tools are not registered in remote mode.** A
remote URL is reachable by anyone holding it, and `pvr_publish_watches` runs
`git push`. Rather than guard them, remote mode simply never registers them -
absent beats guarded, since there is no handler to reach. Remote exposes only
the six read-only lookups.

| Tool | Answers |
|---|---|
| `pvr_cities` | Which cities the chain covers |
| `pvr_cinemas` | Cinemas in a city + the `cinema_id` everything else needs |
| `pvr_now_showing` | What's playing, with certificate, length, formats |
| `pvr_showtimes` | Showtimes at a cinema on a date |
| `pvr_seats` | **Live seat availability, zone counted separately** - the one that matters |
| `pvr_is_open` | Is that date on sale yet |
| `pvr_list_watches` | What the cron watches, and whether it's live |
| `pvr_add_watch` | Create a watch conversationally |
| `pvr_remove_watch` | Delete one |
| `pvr_publish_watches` | Commit + push so the cron picks it up |

`pvr_seats` takes `seat_map=true` for an ASCII auditorium, which makes
the problem obvious at a glance (`O` free in zone, `x` taken in zone, `o` free
outside it, `.` taken outside it):

```
                    SCREEN
 P        oo  ooooooooooooooo   oooo         front: wide open
 ...
 G    ......  xxxxxxxxxxxxxxxx  .......o     zone: solid
 F    ......  xxxxxxxxxxxxxxxx  ........
 E    ......  xxxxxxxxxxxxxxxx  ........
```

That show reads "Filling Up Fast" with 75% booked - and not one free seat
worth having. The server's instructions tell the client to never call a show
bookable on show-level status alone.

### Setting up a watch by asking

`pvr_add_watch` resolves a cinema name fragment to its id and
coordinates, then sanity-checks the film against what that cinema is listing
today - so a typo surfaces immediately rather than as months of silence:

```
WARNING: nothing matches 'ODDYSSEY' in imax at this cinema today.
```

An ambiguous cinema is refused rather than guessed:

```
'PVR' matches 12 cinemas in Chennai - be more specific:
  388  PVR Palazzo-The Nexus Vijaya Mall
  331  PVR Sathyam Royapettah Chennai
  ...
```

**A new watch is not live when it is added.** The cron runs the *committed*
config, so adding one only edits the local file; `pvr_publish_watches`
commits and pushes it. That split is deliberate - publishing pushes to a
public repository, which should be a decision rather than a side effect.
`pvr_list_watches` flags the gap whenever the file is dirty.

## Deploy

Runs on GitHub Actions cron (`.github/workflows/watch.yml`), every 5 minutes,
committing `state.json` back to the repo so the diff survives between runs.

1. Push this to its **own repo**. Keep it public - 5-minute cron on a private
   repo burns ~8,600 Actions minutes a month against a 2,000 free allowance.
   Nothing sensitive is in the code.
2. Pick a notification channel below and add its secrets to the repo.
3. Actions tab -> "pvr-inox watch" -> Run workflow, to record the baseline.

## Two ways to be told

If you already have an MCP client, you may not need a notification service at
all - but the two modes are not interchangeable, and the difference is what
happens when you close your laptop.

| | Durable watch | In-session watch |
|---|---|---|
| Runs on | GitHub Actions cron | Your machine, inside an agent session |
| Setup | Repo + one secret | **None** - the MCP is already there |
| Survives closing the laptop | **Yes** | No |
| Survives closing the agent | **Yes** | No |
| Good for | Days of waiting for an unknown moment | An afternoon of watching for a restock |

**In-session** is `watch.py --stream`, which polls forever and prints one line
per event on stdout - the shape an agent watch tool wants:

```bash
python watch.py --stream --interval 60
```
```
🚨 Booking just opened | The Odyssey IMAX | Sat 15 Aug 09:00 AM | 11 together - D11-D21
🪑 Good seats opened up | The Odyssey IMAX | Sat 8 Aug 04:05 PM | 4 together - C16-C19
```

Point an agent's monitor at that and each line becomes a notification. Note
what it does *not* do: wake a model every minute to poll an API. The polling
stays in Python, where it is free, and only real events reach the model.

**The catch, and it decides the choice:** agent-side schedulers are tied to the
session. Claude Code's cron jobs live only in the current session, fire only
while it is idle, and expire after 7 days; monitors end when the session ends.
A booking window opening on a Monday morning while your laptop is shut is
exactly the case that needs the durable path.

Use in-session for a watch measured in hours. Use the cron for anything longer.

## Notification channels

Set the environment variables for the channel you want and it switches itself
on. Configure several and all of them get the alert. Nothing to edit in code.

| Channel | Variables | Cost / friction |
|---|---|---|
| **ntfy** | `NTFY_TOPIC` (opt. `NTFY_SERVER`) | **No account at all.** Install the app, pick a topic name. Sent at priority 5 so it breaks through a silenced phone. |
| **Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Free. ~5 min with @BotFather. Reliable lock-screen push. |
| **Pushover** | `PUSHOVER_USER_KEY`, `PUSHOVER_APP_TOKEN` | $5 one-off. The best custom alert sounds. |
| **Slack** | `SLACK_WEBHOOK_URL` | Free. Only useful if you live in Slack. |
| **Discord** | `DISCORD_WEBHOOK_URL` | Free, same shape as Slack. |
| **Email** | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO` (opt. `SMTP_PORT`) | Universal, but does not reliably wake you. |
| **Generic webhook** | `GENERIC_WEBHOOK_URL` | POSTs `{title, text, url}`. Bridge to anything else. |
| **GitHub issue** | `GITHUB_TOKEN`, `GITHUB_REPOSITORY` | Zero extra accounts - the Actions run already has both. Relies on GitHub app notifications. |

**If you want to be woken up, use ntfy, Telegram or Pushover.** Email and
GitHub issues are for a record, not an interrupt - and an alert you don't see
is not an alert.

Two that are deliberately absent, both because of Indian regulatory friction
rather than technical difficulty: **WhatsApp** needs a Meta Business account
and template pre-approval, and **SMS/voice** to Indian numbers needs DLT
registration. Use the generic webhook to bridge to either if you have that set
up already.

Messages are plain text with real emoji, so they render the same everywhere -
no Slack `:codes:` leaking into a Telegram message.

### Timing caveat

GitHub's cron floor is 5 minutes and scheduled runs are routinely delayed 5-15
minutes under load, occasionally skipped. That is fine for catching a booking
window opening - the listing goes up in a batch and stays up. It will not win a
seat race measured in seconds. If that matters, move the same script to an
always-on box on a 30-second loop; nothing in it is Actions-specific.
