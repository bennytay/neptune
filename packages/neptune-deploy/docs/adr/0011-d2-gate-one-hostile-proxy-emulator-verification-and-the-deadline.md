# 0011 — D2 gate: one hostile proxy for every connector, emulator verification, and a socket timeout is the deadline

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-158
- Amends: ADR 0006 §1, §2 and §6; ADR 0009 §5 and §8. Review: [`docs/reviews/d2-stress-test.md`](../reviews/d2-stress-test.md)

## Context

D2 added fifteen connectors (ADRs 0006 to 0010) and the ROS 2 diagnostics mapper. Each was tested
against its own in-process fake, with its own hostile knobs, written by the PR that built it. The D2
gate has to show seven guarantees for every connector: external identity survives a re-sync; a
changed object is a new revision with the old one intact; nothing is written to the remote;
local-only mode refuses the source; credentials never reach an output; a hostile remote is findings,
not exceptions or hangs; and determinism. Fifteen sets of fakes, each with its own attacks, cannot
show that the guarantees hold *the same way* everywhere, and a connector a fake was written for
cannot find what the fake's author did not think of.

ADR 0006 §2 left GCS and Azure unverified until each ran against its emulator, and said a connector
that failed there would be withdrawn. ADRs 0007 to 0010 left each vendor connector unverified until a
live run. No vendor tenant or credential is available to this gate. The compiler cannot yet ingest a
plugin Source end to end (compiler PR #103, MVL-45, is open).

## Decision

1. **One gate module, one rig per connector, one hostile proxy.** `tests/test_deploy_d2_gate.py`
   runs every guarantee over every connector. A rig (`tests/deploy_d2_rigs.py`) holds only what
   differs: the connector's existing fake, its factory call, one edit the remote makes between two
   syncs, its read-only surface (methods and routes), its credentials and where on the wire they may
   travel, and its option names for page size, budget and timeout. Every networked connector is
   served through one reverse proxy (`tests/deploy_d2_proxy.py`) that logs every request and applies
   the same five attacks to whichever requests a test names: a redirect to the same URL, a body cut
   in half under its full `Content-Length`, no answer for six times the timeout, a body one byte per
   0.2 s, and a 40 MiB body (past every page limit). A client-side recorder of `http.client` proves
   that no request bypassed the proxy. Where a connector's own tests already go deeper (Open-RMF's
   file and SQLite attacks, the diagnostics mapper's malformed mappings), the gate cites them, and a
   test fails if a cited test disappears.
2. **A socket timeout is the request's deadline (amends ADR 0006 §6).** Each socket operation's
   timeout equals the request's deadline, and the deadline's clock starts first. So a socket timeout
   means the deadline has passed. Before, the cause was read from whether the deadline's timer
   thread had run yet. Under CPU load that is scheduling, so one silent server gave
   `cause: transport_failed` in some runs and `deadline_exceeded` in others, and therefore two finding
   ids for one input (B1, measured at 10 of 25 runs under load). A `TimeoutError` is now always
   `DeadlineExceeded`. This changes only which of two codes a timed-out request reports, and only
   where the race used to pick the other one.
3. **A failed storage listing is not an absent object (amends ADR 0009 §5 and §8).** Rerun resolves
   each storage URL with one exact-key listing. It reported `object_not_found` (category `missing`)
   whenever that key was not listed, including when the listing was refused, invalid or stopped at a
   limit (B2). That asserted an absence where nothing was known, against package non-negotiable 3. A
   key that a *complete* listing lacks is still `object_not_found`. A key whose listing did not
   complete is `object_unresolved` (category `failed`), next to the store's own finding saying why,
   and the object is not read. The gate now checks, for every connector, that a listing the remote
   broke reports nothing `missing`.
4. **GCS and Azure are verified against their emulators and stay registered (closes ADR 0006 §2's
   condition).** `tests/test_deploy_d2_emulators.py` runs the same check against moto server 5.2.3
   (S3), fake-gcs-server 1.56.1 (GCS JSON API) and Azurite 3.37.0 (Blob). The check is: a listing of
   five objects, including a key with `//` and an NFD key, read back byte for byte; identity across two
   syncs; one object rewritten, giving one new revision with the old one kept; only `GET` on the wire;
   local-only refusal; no credential in any output; and identical output from two sources. It is
   env-gated (`NEPTUNE_TEST_S3_ENDPOINT`, `NEPTUNE_TEST_GCS_ENDPOINT`, `NEPTUNE_TEST_AZURE_ENDPOINT`).
   The emulators are external oracles, run without a project dependency (`uv run --no-project`, a
   release binary, `npx`). Azurite has no blob versioning, so Azure's `version:` token path is
   verified only against the in-process fake. Its `etag:` and `If-Match` path is verified against
   Azurite.
5. **A vendor connector without a live run stays registered, marked "fake-verified".** None of the
   vendor connectors failed anything. They simply have not met their service. Withdrawing all of them
   would remove the D2 milestone for want of credentials, not for a defect. Each has an env-gated live
   test (`tests/test_deploy_d2_live.py`, `NEPTUNE_TEST_LIVE_<CONNECTOR>_URL`). It loads the connector
   through its `neptune.sources` entry point and checks identity, read-only methods and routes,
   local-only refusal, absence of secrets and determinism on the real service, reading at most three
   objects whole. The report lists, per connector, the assumptions that only that run can confirm. A
   connector that fails its live run is withdrawn until it passes, as ADRs 0006 to 0010 say.
6. **End-to-end ingest through plugin Sources is a tracked dependency, not a gate condition.** The
   gate proves the guarantees at the Source boundary, which is where the connectors end. Whether the
   compiler routes `s3://` and the rest to them is the compiler's work: ADR 0006's gaps 1 to 3, ADR
   0007's gap 4, MVL-45 and PR #103. Members may not run ingestion (the merge-freshness rule), so the
   end-to-end run of `neptune ingest s3://…` without `--connector` is the compiler's own
   `tests/integration/test_connector_deploy_s3_ingest.py` once #103 merges. To be ready for it, each
   object-store factory declares the URI scheme it reads as a plain attribute, `schemes` (`("s3",)`,
   `("gs",)`, `("az",)`), which #103's dispatch reads (compiler ADR 0067); amends ADR 0006 §1. No
   other connector declares one: they are chosen by name (`--connector`). Nothing here imports or
   depends on #103. `d2-gate` does not wait for it.
7. **Connector versions stay `0.1.0` through `d2-gate`.** B1 changes no finding a run without the race
   produced, B2 renames a finding only where the old one was wrong, and nothing was released (ADR 0005 §9's precedent). From the tag on, any change to a
   connector's output bumps its `CONNECTOR_VERSION`.

## Alternatives considered

- **Re-run each connector's own hostile tests and call that the gate.** They exist and pass, but each
  uses its own fake's knobs, so "a redirect loop" or "a trickle" means fifteen different things. Lost:
  the gate's claim is that every connector meets the same attack.
- **Write a fresh fake per system for the gate.** Twice the fakes to keep honest, for no new coverage
  of the wire formats. Lost. The proxy goes in front of the existing fakes instead.
- **Fix B1 by giving the socket a timeout longer than the deadline.** The deadline would then always
  win, except when its timer thread is starved, which is the case that matters. A socket that has
  timed out also cannot be read further. Lost.
- **Report a socket timeout as its own code (`socket_timeout`).** A third code for the same server
  behaviour. Lost.
- **Run the emulators in CI as test dependencies.** `moto[server]` alone is 61 packages in every
  member's lock (ADR 0006's alternatives). Azurite needs Node and fake-gcs-server needs a Go binary.
  Lost: they stay env-gated oracles, run at gates.

## Consequences

- The gate module takes about three minutes. Most of it is the deliberate timeouts of the slow and
  trickle attacks. It runs in `make check`.
- A new connector joins the gate by adding a rig. The parametrised tests then cover it with no new test
  code.
- Revisit §4 if Azurite gains versioning, or when a real Azure account with versioning is available.
  Revisit §5 when any live run happens: the report's per-connector line moves to "live-verified", or
  the connector is withdrawn. Revisit §6 when PR #103 merges and its end-to-end test is green.
