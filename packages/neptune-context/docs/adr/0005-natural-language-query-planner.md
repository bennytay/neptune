# 0005 — The natural-language query planner: a model that emits a typed Query and shows it, never an answer

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-185

## Context

Agents can write typed queries themselves, but an engineer or SDK user will type English. Translating it is
inference, so it must be visible, labelled and overridable, and the answer must still come from the
deterministic engine over a typed [query](0002-query-language.md). ADR 0002 deferred any textual form to this
planner and kept the JSON form as the only wire form; the planner therefore emits that JSON and nothing else.
The hard part is not the translation but what a model is most likely to do silently wrong: invent an entity,
pick one of two things with the same name, guess a clock, frame or unit, or turn "last week" into ticks it
cannot know. Each of those is exactly what the layers below refuse.

## Decision

1. **`plan(text, as_of, defaults, *, resolver, client) -> PlannedQuery`** (`neptune_context.query.plan`). It
   asks one model once and returns a `PlannedQuery`: `status`, the `query` (the thing to show and edit),
   `lineage`, the `mentions` (every name found with all its candidates), `findings` and, when the output was
   refused, the validator's own `refusal`. It never raises for bad input or a bad model: those are plans with
   findings. It builds no SDK, MCP tool or console: Context's SDK and MCP server (MVL-110) call it and expose
   `neptune_plan` apart from `neptune_query`.
2. **The result is inference and says so.** `assertion_kind` is always `"inferred"`. `ModelLineage` carries
   `model_id` (the model the provider reports), `client_id`, `template_sha256` (the system prompt with its
   pinned vocabularies, and the output schema it is bound to), `schema_id`/`schema_version` (ADR 0002's
   `urn:neptune:schema:query:1` and `QUERY_VERSION`), `request_sha256` and `response_sha256`. A plan's
   canonical JSON (`PlannedQuery.canonical_bytes`, no `null`) is byte-identical for equal plans. A planned
   query becomes a packet's `query_id` only when a person or an agent runs it; the plan itself is not
   evidence and never enters `model/`.
3. **Names resolve through an injected `EntityResolver`**, which reads Ledger declared identifiers
   (`<namespace>:<value>`). `find(text, as_of)` returns each name in the question with all candidates;
   `lookup(declared_id, as_of)` returns the `Entity` a declared id names, with its declared primary clock,
   frames and the clock and frame bridges declared for it. `DeclaredIdentifierIndex` is the in-memory
   implementation (whole-token, case-insensitive, longest name first). The catalog API cannot enumerate
   declared ids, so a Ledger-backed `find` needs a listing call there; until then callers load entities.
   Context still imports only `neptune_ledger.api` and `neptune_memory.schema`.
4. **One model call, behind a protocol.** `ModelClient.complete(ModelRequest) -> ModelResponse`; the request is
   (model, system, user, schema, max_tokens) and its SHA-256 identifies it. The default client is
   `AnthropicClient` (`claude-sonnet-5-5`, Messages API, `output_config.format` json_schema, adaptive thinking
   (set explicitly) at `medium` effort, 16000 max tokens, no sampling parameters, no refusal fallback so the lineage names the one model that
   answered). The SDK is the optional extra `neptune-context[anthropic]`, imported only inside the client.
5. **The query JSON Schema is the output contract**, reduced for constrained decoding (`output_schema()`:
   root inlined, `oneOf` as `anyOf`, constraints the API does not enforce removed). The decoder of ADR 0002
   remains the authority: a document it refuses is an `INVALID` plan with no query and its findings as
   `refusal`. An answer smuggled into the output is an unknown member and is refused the same way.
6. **Nothing is guessed; every default is stated.** After decoding, the planner checks the query against the
   question and the resolver and states or blocks, as `PlanFinding`s (`info` or `blocking`):
   - *Entities*: every declared id must exist and have the stated kind; a name with several candidates is an
     `ambiguous_entity` blocker listing every candidate (the model is told to use the first as a placeholder;
     `choose(plan, mention, declared_id, resolver=, defaults=)` settles a `NEEDS_CHOICE` plan without a
     second model call: it rewrites the draft's subjects and diff subjects to the chosen id and re-runs
     the whole review for that entity alone, so a clock, frame or bridge declared only for another
     candidate is blocked and removed). A name that resolves
     to nothing is not given an id: the model selects by kind and puts the words in the text clause.
   - *Clocks*: `during` and diff instants may use only the caller's declared civil clock when the question
     names its timescale (a civil clock's epoch and tick length are never the model's to choose), an entity's
     declared primary clock, or the caller's default clock; the last two are stated (`clock_defaulted_to_primary` / `_to_caller`) unless the question
     says "its own clock". Anything else is `clock_not_stated` or `clock_not_declared` and is removed from the
     draft. A relative phrase ("overnight", "last week") with no explicit date or tick is
     `time_phrase_unresolved`: the planner has no clock reading (determinism).
   - *Frames and units*: a region needs a frame declared for an entity or the caller and a unit written in
     the question (next to a number, or spelled out; the bare word "in" is not inches) or declared by the
     caller (stated back as `unit_defaulted_to_caller`, and `frame_defaulted_to_caller` for a caller frame); bridges must be declared for an entity. Otherwise `frame_not_declared`,
     `unit_not_stated`, `bridge_not_declared`, and the region is removed.
   - *Claims*: a `Why` needs its claim id quoted in the question as a whole token (`claim_not_quoted`);
     finding pointers index the model's own `explain` tuple.
   - *Defaults applied and overrides*: `as_of_default`, `include_inferred_default`, `budget_default`; where
     the model departs from the caller's value without the question saying so, the caller's value wins and
     the departure is stated: `as_of_overridden` (no transaction stated). The caller's `Budget` is a
     ceiling: a question may narrow a limit by writing the number (names of entities do not count),
     never loosen or drop one the caller set (`budget_overridden`, listing the fields restored).
     `include_inferred` may be narrowed freely (`include_inferred_narrowed`); widening a policy
     default needs a positive, word-bounded, un-negated request ("include inferred claims"; never
     "evidence only", "no", "never", "without") and even then is a blocking
     `include_inferred_widening_unconfirmed` (the draft keeps the default): question text is untrusted
     input, so the caller confirms. A widening without such a request is `include_inferred_overridden`.
   - Parts removed for a blocker take their dependents with them (regions take frame bridges, a removed
     `during` or diff takes the clock bridges that related its clock), so the draft stays editable.
7. **Status and execution.** `READY` (nothing blocks), `NEEDS_CHOICE` (only ambiguity blocks), `NEEDS_INPUT`
   (something the question did not state; the draft with the unsupported parts removed is shown, or none if
   no valid draft remains: `draft_withdrawn`), `INVALID`, `FAILED` (unavailable, refused, truncated, bad
   input). `executable` is true only for `READY`; no path answers without a typed `Query`. A failure is never
   re-planned: one request, one response, visible.
8. **Golden set and replay.** `tests/golden/planner/` holds `world.json` (declared identifiers across every
   embodiment, with clocks and frames, and caller profiles), `cases.jsonl` (101 questions with expected status,
   query, blocking and info finding codes) and `recordings.jsonl`. A recording is one JSON line keyed by
   `request_sha256` with `model`, `stop`, optional `text` and `recorded_by` (`live` or `synthetic`); replay of
   an unrecorded request is `model_unavailable`, never a guess, so any prompt, schema or vocabulary change
   fails CI until re-recorded. CI replays (no network) and the report prints the pass rate with the count of
   live and synthetic recordings. `scripts/record_planner_golden.py` re-records the `model` cases against the
   live API (never run in CI); `scripted` cases keep fixed bad responses that exercise the checks above.
   The committed recordings are all `synthetic` (authored with the case table, no API credentials were
   available when written), so the committed pass rate says the planner checks behave, not how well a model
   translates; the live rate is the number for the C4 review.

## Alternatives considered

- **The model picks among ambiguous candidates.** A guess dressed as a result. Lost: ambiguity blocks and
  lists candidates.
- **Resolve names first and give the model only ids.** Needs a named-entity step with its own errors; the
  resolver already does exact matching and the model sees its candidates. Lost.
- **A separate draft schema with entity mentions instead of declared ids.** Not "the query schema as the
  contract", and a second schema to keep in step. Lost.
- **Re-plan on an invalid output.** Hides model failure and makes lineage ambiguous. Lost: one call, visible
  failure.
- **Let the planner default a missing clock to UTC.** Converts timestamps implicitly. Lost.
- **Compute dates in the planner.** Deterministic, but needs a time zone and calendar vocabulary beyond this
  issue; the model converts stated dates to ticks, and `time_phrase_unresolved` refuses relative ones. Revisit.
- **Server-side refusal fallback to another model.** The plan would be produced by a model other than the one
  requested. Lost; a refusal is `model_refused`.

## Consequences

- MVL-110 wraps `plan`, `choose` and `PlannedQuery.to_json` (SDK returns the plan beside the packet; MCP
  `neptune_plan`); the console shows `query` and the findings and sends an edited query to `neptune_query`.
- A prompt-template, schema, vocabulary (graph-schema or catalog-api pin) or model change changes
  `template_sha256` and every request hash: re-record, and explain the golden diff in the PR.
- Live output is not byte-reproducible; determinism is of replay and of everything after the model call.
- The date-to-tick arithmetic a model does is its largest remaining error source; the live pass rate will say.
- Revisit when: the catalog API gains declared-id listing (a Ledger-backed resolver), a textual query form is
  wanted for editing, or a deterministic date parser is justified.
