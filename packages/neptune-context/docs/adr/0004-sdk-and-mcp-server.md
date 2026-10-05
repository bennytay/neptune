# 0004 — The SDK and the MCP server: one engine seam, verified answers, four read-only tools

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-110

## Context

Three kinds of caller reach Context: a program (Deploy's console, Learn, an external user), an agent (Claude
Code asking "why did the arm-cell incident happen, what changed" and expecting cited answers) and, later, a
policy at inference time. They must all get the same packets (ADR 0003) from the same queries (ADR 0002), and
none of them may be handed a wrong answer silently. The engine that answers (C2) does not exist yet, and when
it does it will have several retrieval channels (graph, catalog, lexical, vector, spatial) that none of the
callers should have to know about. Deploy and Learn need to build now, against types that will not change when
the engine lands. The agent surface has one extra hazard: an agent that is not made to choose whether
inferences may enter its context will take the default, and the default becomes a policy nobody wrote.

## Decision

1. **One seam: the engine.** `neptune_context.sdk.Engine` (and its awaitable twin `AsyncEngine`) has two
   calls, both reads: `query(Query) -> ContextPacket` and `hydrate(EvidenceRef, as_of) -> Resolution`
   (the catalog API's `resolve`, so a Ledger type crosses the seam unchanged). Everything is built on it: the
   in-process engine (C2), `HttpEngine` (a remote engine over the wire, §3) and `StubEngine` (recorded
   packets, so consumers build before C2). Nothing in the seam names a channel; channels are the engine's
   business and surface only in the query schema.
2. **`Client` and `AsyncClient`, identical types.** `Client(engine | url, token=None)`: `query`, `why`,
   `diff`, `hydrate`. `why` and `diff` build the typed `Query` a caller could have written (ADR 0002 Q09, Q04)
   and call `query`: there is no second request type, so every answer is a packet for a visible query.
   `include_inferred` has no default on any call. A sync `Engine` serves the async client in a worker thread
   (`to_async`). `hydrate` takes an `EvidenceRef` or the `EvidenceItem` of a packet and returns the Ledger's
   `Resolution`; fetch locations are host-specific, so they appear here and never in a packet.
3. **Verify before trusting.** `query` runs `validate` first: a refused query never reaches an engine and
   raises `query_refused` with its findings. After the engine answers, the packet must carry this query's
   `query_id`, the snapshot an integer `as_of` pinned, and the same `inference_included` as the query;
   anything else is `invalid_response`. `hydrate` checks the resolution is for the same source. A defect in an
   engine is therefore loud at the boundary, not a wrong fact downstream.
4. **Wire, version 1** (`neptune_context.sdk.wire`): `POST /v1/query` (canonical query JSON in, canonical
   packet JSON out) and `POST /v1/hydrate` (`{"evidence", "as_of"?}` in, `Resolution` JSON out); non-200
   answers carry `{"error": {"code", "message", "findings"}}`. `HttpEngine` uses the standard library (no new
   dependency for it), accepts only `http(s)` URLs without credentials, query or fragment, never follows a
   redirect, ignores proxy variables in the environment (a proxy would see the token), bounds responses at the
   packet maximum (64 MiB), enforces `timeout` as a deadline on the whole answer (not per socket wait), maps
   every transport failure to `unavailable` or `timeout`, and reads answers with the strict packet decoder.
   Servers are Platform's (X2); this package defines only what both ends share.
5. **Errors, retries, auth.** Every failure is an `SdkError` with an `ErrorCode` (`invalid_argument`,
   `query_refused`, `unauthenticated`, `forbidden`, `not_found`, `unavailable`, `timeout`, `invalid_response`,
   `engine_error`), a bounded message and `retryable` (`unavailable` and `timeout` only). Every call is a
   read, hence idempotent: only retryable failures are retried, under `RetryPolicy` (default 3 tries, 0.2 s
   then 0.8 s, no jitter, injectable sleep); a client over an in-process engine does not retry unless given a policy. Remote auth is a
   bearer token (Platform X2 issues it) sent only over `https` or to a loopback host; local mode has an
   implicit tenant and refuses a token. A token is never in a `repr`, a message or a log line.
6. **MCP server.** Built on the official `mcp` Python SDK (`mcp>=1.12,<2`, low-level `Server`), over an
   `AsyncClient`: `build_server(client)`. Transport: stdio first (what Claude Code and other hosts spawn);
   `python -m neptune_context.mcp --url U | --packets DIR`, token from `$NEPTUNE_TOKEN`. The low-level server
   is used rather than `FastMCP` because tool schemas must be the contract's own JSON Schema, not one
   inferred from Python signatures.
7. **Tools.** `neptune_query`, `neptune_why`, `neptune_diff`, `neptune_hydrate`; all annotated read-only and
   idempotent. Names use underscores, not the dots of the issue text (`neptune.query`): underscores satisfy
   the strictest tool-name rule any model host applies, and a host prefixes the server name anyway
   (`mcp__neptune__neptune_query`). Input schemas are built from `query_schema()` (and the compiler's
   `EvidenceRef` schema), so they cannot drift from the contract and a new retrieval channel appears in them
   automatically. `include_inferred` is a **required** parameter of every packet tool with no default, so an
   agent must choose; `neptune_query` takes it as a tool parameter and refuses a query document whose own
   `include_inferred` disagrees. Members with one fixed meaning (`query_version`, `as_of: head`, empty
   lists, `same_as_depth: 0`) may be left out; the strict query reader still decides everything else.
8. **Answers.** A packet answers as `render_text` (ADR 0003 §7: `[E1]` keys, `Evidence:` footer, `INFERRED`
   marks) followed by one `resource_link` per evidence ref, `neptune://evidence/<base64url of the ref's
   canonical JSON>?as_of=<the answer's transaction>`, at most 100 per answer (the footer lists all; the text says how many links there are).
   Reading a link hydrates it at the transaction the answer was made at. One URI per ref: padded, re-ordered or aliased forms are refused. Resources are
   never enumerated. Failures are tool errors (`isError`) whose text is the SDK error as JSON, so an agent
   reads `code`, `retryable` and the query findings instead of prose. Server `instructions` tell the agent to
   cite `[E]` keys, present inferences as inferences, and treat "Not answered" as a gap, not a no.
9. **Demo v1 path.** Claude Code runs the server over stdio, asks with `neptune_query` (text clause plus
   subjects), follows with `neptune_why` on a claim id and `neptune_diff` on a subject, and opens evidence
   through the links. Until C2, `--packets` serves recorded packets only; the in-process engine over a local
   Ledger and Memory plugs in as `build_server(AsyncClient(engine))` with no change to the tools.

## Alternatives considered

- **Dotted tool names as written in the issue.** Valid in the MCP spec, but not in the stricter rules some
  hosts and model APIs enforce; a rename later would break every prompt that names a tool. Lost.
- **`FastMCP` with typed Python tool signatures.** Less code, but the tool schema would be derived from
  signatures, forking the query contract and losing its bounds and enums. Lost.
- **A default for `include_inferred` in the tools (convention by caller).** Hides the choice that decides
  whether a model sees inferences. ADR 0002 already refuses a default in the query; the tools match. Lost.
- **Separate request types for why/diff.** Two more shapes to version and test, and answers whose query is
  not a `Query`. The query already has `explain`. Lost.
- **`httpx` or `requests` in the SDK.** A dependency for two POSTs; `mcp` already brings `httpx`, but the
  SDK should not depend on the agent library. `urllib` with a no-redirect opener is enough. Lost for now.
- **Structured output (`outputSchema`) instead of text.** Hosts show models the text; the packet JSON is
  already one `decode` away for programs (use the SDK). Text with evidence links is what an agent can cite.
  Lost.
- **Streamable HTTP transport now.** Needs authentication and tenancy that belong to Platform (X2); stdio is
  what the demo needs. Deferred; `build_server` is transport-agnostic.

## Consequences

- Deploy's console and Learn can build against `Client(StubEngine.from_directory(...))` and the golden
  packets today; swapping in the real engine changes one constructor argument.
- The `mcp` distribution (and its `httpx`, `starlette`, `pydantic` tree) is a dependency of `neptune-context`
  only, pinned in `uv.lock`; the import-boundary test still allows only the Ledger's `api` and Memory's
  `schema` from upstream.
- The wire is version 1 and C2/X2 implement the server; a change to routes or error shape bumps `WIRE_VERSION`.
- `neptune_hydrate` and resource reads expose the Ledger's fetch locations to the agent host; a deployment that
  must hide them filters in its engine's `hydrate`.
- Revisit when: C2 lands (local mode becomes the default for the demo), a host needs streamable HTTP, a
  retrieval channel needs its own tool parameters, or a consumer needs packet pages (`PACKET_VERSION`).
