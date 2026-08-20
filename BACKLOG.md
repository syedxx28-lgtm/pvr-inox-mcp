# Backlog

last_updated: 2026-08-20

Ranked by value. The original seven came out of a real session on 2026-08-10 —
answering "what English films are running in Chennai", then drilling into
showtimes and seats at Palazzo and Phoenix — plus one found while fixing them.

**All seven are shipped.** The record is below, because the reasoning is worth
more than the checkmarks. Open work starts at "Still open".

---

## Shipped 2026-08-20

### 1. `pvr_seats` returns screen, language and variant title

Was: seats gave availability but no auditorium and no language, so every answer
needed a second `pvr_showtimes` call joined on the timestamp — six times in one
session, for the commonest question the server gets.

Now each row carries `screen`, `language`, `language_name`, `variant_id`,
`variant_title`, `format` and `formats`, and the text output has SCREEN and LNG
columns. Every field already existed on the show dict; none of it cost a
request. The join is gone.

### 2. `pvr_now_showing(language=...)` filtered on the SCHEDULE

Was: "what English films are on in Chennai" cost 10 calls — one `now_showing`,
one `find_shows`, one `cinemas`, then seven per-cinema `pvr_showtimes` sweeps —
because `now_showing` reports RELEASE languages and its own footer says not to
trust them. There was no cheap path to a filtered answer, only an expensive one.

Now one call. It runs a schedule-only city sweep and returns only films with a
real showtime in that language, with show and venue counts. Measured on
2026-08-20: 9 English films, 15 cinemas, 17 upstream calls, one tool call.

### 3. Auto-widen when the zone comes back empty

Was: Palazzo AUDI 5 and Phoenix Screen 1 returned `0 free in zone` on every IMAX
show across two dates. The response explained how to widen and asked the caller
to call again — so it already knew its answer was wrong and would not act on it.

Now `seat_report` widens into the best rows outside the zone and re-scores from
the SAME payload, at no extra request, reporting `widened_to`. Measured on the
23 Aug 12:35 PM IMAX show: `0 free, best_run 0` became `46 free, 10 together at
J1-J10`.

Two things this surfaced that the original note did not anticipate:

- **Widened rows must be taken WHOLE.** `_alternatives` finds runs across the
  full row, but `resolve_zone` re-restricts each row to its centre block — the
  exact part that was sold out. The first implementation widened into B, G, H
  and changed `zone_free` by zero. Widening now adds the whole row.
- **An explicit `zone_rows` is an instruction, not a guess.** `auto_widen`
  defaults to widening only a DERIVED zone. A standing watch pinned to the
  back-centre block would otherwise start firing at 05:26 for a front row it
  never asked about; `watch.py` also passes `auto_widen=False` explicitly.

### 4 and 5. Coverage versus seat counting

Was: a city-wide search covered 8 of 16 Chennai cinemas and skipped Palazzo, the
IMAX house; everything past the first dozen rows came back `ON_SALE?`. The
uncounted state was the common case in the broadest tool.

These were one problem. Counting seats costs a request per show, and that is
what caps coverage, so "which tool is the is-it-bookable tool" is really a
choice about coverage. `count_seats=True` (default) stays the booking tool.
`count_seats=False` is the honest other half: schedule only, budget raised to
cover every cinema in radius, every state openly unverified and a note saying
so. Measured: 15 cinemas, 0 skipped, 449 shows, 17 calls.

### 6. Optional pre-rendered markdown output

Shipped as `style="markdown"` on `pvr_seats`, non-default, with `format="json"`
untouched for programmatic callers. Server instructions and tool descriptions
are advisory — a model paraphrases and reorders them — so returning assembled
markdown is the only lever that makes format deterministic: format becomes
data. The time-of-day split and the "good seats free" wording are one user's
taste, so it is asked for rather than imposed.

### 7. `find_shows` returned nothing for a bare city query

Found while testing the above. With no `lat`/`lng` the origin fell back to the
city's own published coordinate, and no Chennai cinema is within the 6 km
default of it — so `find_shows("Chennai", party_size=2)`, the simplest possible
call, answered `NOTHING_IN_RADIUS`.

Now the radius starts city-wide whenever the caller passed no position, on the
grounds that someone who gave no coordinates meant the city rather than a point
in it. The 6 km default survives only for a caller who supplied a real position.

### Also shipped, not from this list

- **Format-aware venue selection.** `find_shows` chose cinemas by distance
  alone and applied `experience` only to the shows that came back, so an IMAX
  search could spend its budget on multiplexes while the IMAX house sat one slot
  outside it — and widening `radius_km` made it worse. `content/cinemas`
  already carried `screens[].screenType` and `list_cinemas` was discarding it;
  venues that can run the format are now searched first, demoted never dropped.
  Chennai's two IMAX screens are 19.8 km and 24.9 km from the centre, so the old
  6 km default could never reach either.
- **A global upstream-call ceiling** (`PVR_MAX_CALLS_PER_MIN`, off by default,
  `/proxy` exempt) so a public instance sheds load as `RATE_LIMITED` rather than
  getting its shared egress IP blocked for 15 minutes.
- **Per-call usage logging** (`tool=<name> format=<text|json>`), since the
  transport only ever logged `CallToolRequest`.
- **Dead code removed:** `sessions_near`, `_anchor_set`, `ANCHOR_RADIUS_KM` (the
  multi-cinema endpoint they served ignores the requested date and lags across
  midnight, which is why `find_shows` never used them), and the single-entry
  `PROVIDERS` indirection with its vestigial `provider` config key.

---

## Still open

### 8. Premium halls may need a different zone rule, not a wider one

Auto-widening treats a short zone as a coverage problem. The underlying
suspicion from the original #3 is still unchecked: in premium halls the centre
rows may be a distinct seat CLASS, in which case `0 free` is real and frequent
rather than a heuristic failure, and widening quietly reframes a sold-out
premium block as an ordinary one. Worth measuring before trusting the widened
verdict in IMAX and Luxe houses specifically.

### 9. No per-caller rate limiting

The ceiling is global to the process because Cloud Run reports `0.0.0.0` for
connector traffic, so callers cannot be told apart. One heavy client can
therefore exhaust the budget for everyone. Needs a real caller identity before
it can be fixed.
