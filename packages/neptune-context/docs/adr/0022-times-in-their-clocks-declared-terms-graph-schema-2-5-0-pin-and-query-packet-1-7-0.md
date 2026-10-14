# 0022 — Times in their clock's declared terms, the graph-schema 2.5.0 pin and query-packet 1.7.0

- Status: Accepted
- Date: 2026-10-15
- Issue: MVL-191

## Context

In a filmed Demo v1 run, Claude Code asked over MCP "When exactly did the stop on ARM-3A happen on 2026-09-14?
Do the records agree?". It answered "I can't give you a single clock time … I haven't converted them to dates".
Every time it had seen read like `tick 1789396358 on clock rec:sha256:7f95…`. SKILL.md had told it never to
turn ticks into dates without a mapping. The gold answer (Q7) is 14:32:38 in the syslog and 14:33:10 in the
CMMS, both PLANT-2 local time, America/New_York, as Deploy's declaration states for each export.

Memory ADR 0029 (graph-schema 2.5.0) now states each clock as declared, in `clock_description`: tick length,
epoch, timescale, and the civil zone a companion record declares. A wall clock's ticks count from 1970-01-01
00:00:00 of that wall clock (root ADR 0023 §2), so the declared count can be read as a date and time with no
conversion. Context had no way to carry those descriptions into an answer.

## Decision

1. **Pin graph-schema 2.5.0.** `pins.py` holds `CATALOG_API_VERSION = "1.7.0"` and
   `GRAPH_SCHEMA_VERSION = "2.5.0"`. `contracts/lock.toml`, `docs/contracts.md` and `pinned.json` agree.
   `clock_description` joins the predicate enums, and its literal joins `TypedLiteral`.
2. **The packet carries the items' clocks.** A new optional member `clocks` (query-packet **1.7.0**, minor)
   holds Memory's `clock_description` claim for each clock that a claim item's interval or instant object is
   on, or that a sensor sample's time is on. The claims are as Memory knew them at its snapshot and sorted by
   clock id. They are not items: they have no relevance, they use no budget, and they describe what the items
   rest on. A clock with no description, or with two, has none, and its ticks stay ticks. The member is written
   only when there is one, so every 1.6 packet keeps its bytes. The packet checks that each entry is a current,
   non-withheld `clock_description` of a clock node.
3. **Times are rendered in their own clock's declared terms** (`render/clocks.py`), never converted:
   - **Wall clock.** A clock that a `civil_time_zone` record names, with a Unix epoch and a stated tick
     length, is shown as the date and time that the ticks count on that wall clock, at the precision the tick
     length states: `2026-09-14 14:32:38` for 1 s ticks, `2026-09-14 14:32` for 1 min ticks. After it comes
     `(local wall time, America/New_York as declared)`, or `(local wall time, zone not stated)`.
   - **POSIX clock.** A Unix-epoch POSIX clock with no zone record is shown as `… UTC (POSIX time, as
     declared)`, which is what that timescale counts.
   - **Anything else** stays ticks and says why in plain words: an epoch that is the device's boot, an epoch
     not stated, a tick length that is not a decimal fraction of a second, or a timescale with no zone (whether
     it counts UTC or a local wall clock is not known). A clock that Memory does not describe says so.

   A one-tick interval is an instant (Memory ADR 0013): `valid at <time> on <clock>`. Anything longer is
   `valid from … until …` or `valid from …, open-ended`, with each end on the same clock.
4. **Readable clock labels.** A fact line names a clock by what the records state about it: its time field,
   its scope, and, with a source index, the file it is read from (`"Stopped" clock in "downtime_log.csv"`).
   The `rec:sha256` id is not used there. If two clocks would share a label, they are numbered by clock id. A
   new **Clocks** section after the facts lists every label with its full id and what is declared about it
   (tick length, origin, zone, and the adapter that declared the zone). Each line cites the evidence that
   declares it (`[E..]` keys, after the items' and trails' keys). `parse_answer` reads each clock line as a
   statement with evidence and no item.
5. **Comparing two times is the agent's job.** The answer sets times on different clocks side by side, each
   with its own declared zone, and never relates or aligns two clocks that no source relates. SKILL.md says:
   report times as shown, with their declared zone, and never convert. If both times declare the same zone,
   the agent may compare them as declared wall times. The tool does not compute the difference.

## Alternatives considered

- **Look the descriptions up at render time.** The renderer turns a packet into text, with nothing else to
  read. An answer has to be reproducible from its packet alone (ADR 0003).
- **Clock descriptions as items.** They would compete with facts for the budget and need an invented
  relevance score. A short query could then show ticks again because the clocks were cut.
- **Convert wall times to UTC with a tz database.** That is the silent conversion non-negotiable 4 forbids, and
  the answer would depend on a database release (root ADR 0061 §2).
- **Read every Unix-epoch clock with an unknown timescale as a wall clock** (the shape root ADR 0023 §2 gives
  a zone-less civil time). Only a zone record says a clock is civil. Another adapter's Unix-epoch clock may
  count UTC.
- **State the difference between two same-zone wall times in the tool.** It would read as a fact Memory
  holds, when it is arithmetic over two declarations. The agent can do the arithmetic and say what it is.

## Consequences

- Q7 can be answered in the gold's terms: 14:32:38 and 14:33:10, America/New_York as declared, 32 s apart as
  declared wall times. The incident report's times read as `local wall time, zone not stated`, as the report
  states them.
- The `World time` header and the diff headings still name clocks by id. Their grammar is fixed (ADR 0011),
  and the fact lines are what an agent quotes.
- Deploy raises its graph-schema lock to 2.5.0 and its query-packet lock to 1.7.0 in the same change.
- Revisit when a derived tz-database transform states instants for wall times, which would add a second
  rendering beside the declared one and never replace it.
