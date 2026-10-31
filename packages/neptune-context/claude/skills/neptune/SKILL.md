---
name: neptune
description: Answer questions about a robot site (what is blocked, what went wrong, who is qualified and on shift, what changed, what is unknown) from Neptune's live, cited site model through the `neptune-site` MCP server. Use it whenever the user asks about a robot, machine, person, task, rule or document at a site Neptune has built; every fact you state must carry Neptune's citation keys.
---

# Neptune: cited answers about a robot site

The `neptune-site` MCP server (`python -m neptune_context.mcp --site <site_dir>`) offers the
read-only `neptune_site_*` tools. Answer every question about the site (what is blocked, why output
dropped, who is qualified and on shift, what changed, what is unknown) with them, never from memory
or guesswork:

- **Start with the insights.** `neptune_site_insights` (what is going on, what went wrong, at a
  time) and `neptune_site_shift_report` (`shift_id` such as `shift:A`, or `start` and `end`) give
  Neptune's findings: starvation chains, qualification gaps, stale sources, changes before faults
  and work orders checked against rule clauses. Every finding is **inferred** by a deterministic
  rule and cites the facts behind it: say "Neptune infers ...", and call a change before a fault
  a correlation, never its cause. Then drill in with the tools below. `neptune_site_sql` also has
  `downtime`, `utilisation` and `fault_clear_time`.
- `neptune_site_resolve` turns a name (`MR-1142`, `cv-2`) into entity refs; pass refs to the rest.
- `neptune_site_impact` (what is blocked), `neptune_site_state_at` (one entity now or at a time),
  `neptune_site_timeline`, `neptune_site_diff` (what changed between two times), `neptune_site_find`
  (who holds a qualification, which rule requires it), `neptune_site_gaps` (stale, unknown,
  pending), `neptune_site_why` (one `fact:` id), `neptune_site_search_evidence`, `neptune_site_sql`.
- The site may be live: `neptune_site_changes` with the `log seq` of your last answer lists what
  arrived since.
- Times are site-clock `YYYY-MM-DDTHH:MM:SS`, never converted. Leave `at` out for "now".
- Each line ends `[S<n>]`; the `Sources:` footer gives the file and line, row or page. Keep the key
  and name the file in your answer. Say `stale`, `unknown`, `ambiguous`, `pending` and `inferred`
  as the line does; a pending proposal is not a fact until an operator confirms it.
- A file `neptune_site_gaps` lists as `not mapped` is still searchable with
  `neptune_site_search_evidence`, but none of its rows are facts yet. To map it: run
  `python -m neptune_memory.site draft-mapping <site_dir> <file>`, **show the user the draft**, fix
  what they say, save it into `<site_dir>/static/neptune.mappings.yaml`, then
  `python -m neptune_memory.site build <site_dir>`. Never save a mapping the user has not seen.
- **Actions: you propose, a person approves.** `neptune_site_propose_action` (`pause`, `resume`,
  `send_to` with a `zone`, `cancel_task`, or `page` a person) logs an inferred proposal with your
  reason and the `fact:` ids behind it. Nothing moves until a person runs
  `python -m neptune_memory.site approve <site_dir> <id>` (or uses the web view); no tool approves,
  so never claim an action happened because you proposed it. `neptune_site_actions` shows each one:
  pending, rejected, intent (approved, by whom), dispatched (the message as sent) and outcome (the
  observed change, or not observed in time). Actions never touch safety functions (e-stops,
  protective stops, interlocks, speed limits): they only go through the fleet manager's protocol.
