# Deployment targets

Neptune has **one** deployment target today: **local**, one machine running the uv workspace
(`make setup`). Every layer runs in process there. No other target is built: there is no hosted
service, no shared multi-tenant server, no on-robot or edge install and no cluster deployment in the
repository yet.

## The local target, layer by layer

| Layer | How it runs locally | Where its state lives |
|---|---|---|
| Compiler | `neptune ingest` or the Python SDK, in process ([CLI](docs/cli.md), [SDK](docs/sdk.md)) | the ingest package you name, plus a workspace directory for cache and checkpoints: `$NEPTUNE_HOME`, else `$XDG_CACHE_HOME/neptune`, else `~/.cache/neptune`. The workspace is local-only until you pass `--allow-network`. |
| Ledger | the `ledger` command line (`migrate`, `register`, `verify`, `dump`, …) against a database named by `--dsn` or `$NEPTUNE_LEDGER_DSN`, and the [catalog API](packages/neptune-ledger/docs/catalog-api.md) | a PostgreSQL 16 database. The integration harness and the Ledger's tests start an embedded one from the `pgserver` wheel, so no Docker is needed ([platform ADR 0006](packages/neptune-platform/docs/adr/0006-real-ledger-stage-on-embedded-postgres.md)). |
| Memory | the `memory` command line (`consolidate`, `rebuild`, `dump`; [guarantees](packages/neptune-memory/docs/guarantees.md)) | a directory per tenant on disk (graph documents and snapshot records), until Memory's G3 milestone moves the graph onto PostgreSQL 16 with pgvector ([Memory ADR 0004](packages/neptune-memory/docs/adr/0004-claim-graph-store.md)). |
| Context | the SDK's in-process engine, and the MCP server over stdio for agents: `python -m neptune_context.mcp --memory GRAPH.json` ([SDK and MCP server](packages/neptune-context/docs/sdk.md#mcp-server)) | nothing of its own: it reads Memory and the Ledger and never writes. |
| Deploy | `python -m neptune_deploy`: `map` (lifecycle records from a compiled package) and the evidence-pack commands, in process | the packages and packs it writes where you point it. |

The integration harness runs the layers in order in one command (`make harness`) over the
[acceptance corpus](packages/neptune-platform/docs/acceptance-corpus.md); its
[runbook](packages/neptune-platform/docs/harness.md) says which stages run for real and which as
contract stubs. The [quickstart](quickstart.md) is the guided way in.

## What exists but is not a deployment

- **`harness/compose.yaml`**: PostgreSQL 16 with Apache AGE and pgvector, and MinIO, bound to
  loopback with throwaway credentials. It is a test stack for a harness stage that declares it needs
  services; nothing starts it otherwise (`python -m harness --compose` does on request).
- **The SDK's wire protocol to a remote engine** (`POST /v1/query`, `POST /v1/hydrate`;
  [wire](packages/neptune-context/docs/sdk.md#wire-sdk-to-a-remote-engine-version-1)): the client side
  exists, but no server for it is in the repository. Authentication and tenancy belong to Platform and
  are not built.
