# Backlog

last_updated: 2026-08-10

Ranked by value. Everything here came out of a real session on 2026-08-10 —
answering "what English films are running in Chennai", then drilling into
showtimes and seats at Palazzo and Phoenix. Call counts below are what that
session actually spent.

---

## 1. Return screen, language and variant title from `pvr_seats`

`pvr_seats` gives availability but no auditorium and no language, so every
answer needs a second `pvr_showtimes` call joined on the timestamp. That join
happened six times in one session.

Add `screen`, `language`, `variant_title` and `format` to each show row in the
seats response. Halves the calls for the most common question the server gets.

## 2. `pvr_now_showing(language=...)` filtered on the SCHEDULE

"What English films are on in Chennai" took 10 calls: one `now_showing`, one
`find_shows`, one `cinemas`, then seven per-cinema `pvr_showtimes` sweeps.

The reason is that `now_showing` reports RELEASE languages and its own docstring
correctly says not to trust them — so there is no cheap path to a filtered
answer, only an expensive one.

Add a `language` parameter that filters on the resolved per-show language.
A one-call answer to the single most common opening question.

## 3. Auto-widen when the zone comes back empty

Palazzo AUDI 5 and Phoenix Screen 1 both returned `0 free in zone` on every IMAX
show, across two dates. The response then explained how to widen and asked the
caller to re-call — so it already knows the answer is wrong, it just will not
act on it.

When the zone cannot seat the party, widen automatically and return the next-best
block labelled as such. Keep the current text as an explanation of what was
widened, not as an instruction to try again.

Note the underlying suspicion, worth checking separately: those centre rows may
be a distinct seat class in premium halls, in which case "0 free" is real and
frequent, which makes the auto-widen more important, not less.

## 4. `pvr_find_shows` covers only part of the city

The city-wide search covered 8 of 16 Chennai cinemas and named the 4 it skipped
(Palazzo, Ampa, Grand Galada, INOX National) — the flagging is right, but a
city-wide question cannot be answered by a partial sweep, and Palazzo is the
IMAX house.

Either raise the budget for city-wide searches, or add a cheap
schedule-only mode that skips seat counting and can therefore cover everything.

## 5. Most results come back `ON_SALE?` (seats not counted)

`find_shows` returned `ON_SALE?` for all but the first dozen rows. For a server
whose whole pitch is "is this actually worth booking", the uncounted state is
the common case in the broadest tool.

Tied to #4 — counting is what costs the budget. Decide which tool is the
"is it bookable" tool and let the other one be schedule-only and honest about it.

---

## 6. Optional: pre-rendered markdown output

Open question rather than a decision. Server `instructions` and tool
descriptions are advisory — they shape model behaviour but cannot enforce an
output format; the model paraphrases, reorders and drops things.

The one reliable lever is returning the formatted markdown as the payload:
format becomes data. A `style="markdown"` mode on `pvr_seats` could emit the
film heading, time-of-day sections and tables already assembled, done
deterministically in Python.

Caveats: keep `format="json"` for programmatic callers, and do not make it the
default — the time-of-day split and the "good seats free" wording are one
user's taste, and this is a public server.
