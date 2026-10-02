# 0058 — Plugin adapters and Sources from entry points, in a fixed order, refused as findings

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-200
- Amends: ADR 0024 §9 (the adapters a default job uses) and its deferral of "entry-point plugin
  discovery … when a third-party adapter exists"

## Context

ADR 0024 listed the shipped adapters in `neptune.adapters.builtin` and deferred entry-point
discovery until an adapter outside the compiler existed, because discovery makes a job's adapter
set depend on what is installed. Neptune Deploy (Deploy ADR 0001) now registers `deploy_lifecycle`
under `neptune.adapters`, and reserves `neptune.sources` for read-only connectors that D2's object
stores need (MVL-153). `neptune ingest` never reads either group, so Deploy's adapters run only in
code that builds its own registry.

Reading installed plugins risks exactly what the non-negotiables forbid: an adapter set, and so a
package, that changes with installation order; one plugin silently replacing another of the same
id; a package that does not say which plugin version made its records; and a broken plugin
failing every job on the host.

## Decision

1. **Two groups, read by `neptune.runtime.plugins.load_plugins`.**
   - `neptune.adapters`: name = adapter id; value = a zero-argument callable returning an adapter.
   - `neptune.sources`: name = connector id; value = a callable returning a `Source`. It is
     imported and checked to be callable, never called: what a connector is given (a URI,
     credentials) is MVL-153's decision. Clients expose them (`Neptune.plugins.sources`).
2. **A fixed order.** Distributions are found on the import path; a name found twice is the first,
   as `import` loads it. Entry points are then sorted by (group, normalised distribution name
   (PEP 503), entry-point name). Installation order, path order and `entry_points.txt` order
   change nothing. A distribution whose metadata has no usable name is never merged with another:
   each of its plugin entry points is refused (`refused`, reason `unnamed_distribution`). A
   distribution whose `entry_points.txt` names a plugin group but cannot be parsed is a
   `load_failed` finding (step `listed`) naming the distribution; a broken file that names no
   plugin group is not Neptune's to report.
3. **Admission.** An entry point is used only if: its name is an id; it imports; it is callable;
   calling it builds an object with an `AdapterDescriptor` and the four methods, at this
   `ABI_VERSION`; the descriptor's id is the entry-point name; and its `libraries` do not pin its own
   distribution at another version. Otherwise a finding, and the plugin is not used:
   `neptune.plugins.load_failed` (import or build raised; the exception's class and step, never its
   text) or `neptune.plugins.refused` (with a `reason`). Anything raised but `KeyboardInterrupt`
   is contained, `SystemExit` and other `BaseException` subclasses included: a plugin never ends
   the client.
   Whatever a plugin prints to stdout or stderr while it is imported or built is captured, so it
   never reaches the CLI's stdout (which `--json` owns): it is a `neptune.plugins.output` finding
   (info) holding at most the first 1000 characters and the total count, or a detail of the
   `load_failed` finding if the plugin then failed. Output written below Python's `sys.stdout`
   (a C extension writing to file descriptor 1) is not captured.
4. **Duplicate ids are refused, never resolved.** Two plugins with one id, or a plugin with a
   built-in's id, are each refused with `neptune.plugins.duplicate_id` naming every claimant. None
   of them is used: no order, first or last, decides a winner. A built-in keeps its id.
5. **The plugin is in the lineage.** An admitted adapter is wrapped (`PluginAdapter`) so its
   descriptor lists `(distribution, version)` among its `libraries`. `configure` puts libraries into
   the `TransformRecord`, whose id is in every chunk id, plan key and cache key (ADR 0024 §4,
   ADR 0031): records name the plugin that made them, and upgrading it is a new lineage. Record ids
   keep ADR 0003's formula (adapter id, version, config hash).
   Installing a plugin can change what an existing folder gives even where no plugin record lands
   (a plugin that ties a built-in turns a selection into an ambiguity), so the package records the
   plugins themselves: the loader's transform lists every distribution it admitted a plugin from as
   its `libraries` (normalised name to version, sorted), and enters every package of a job that had
   any, findings or not. A `neptune.probe.ambiguous` finding whose tie includes a plugin adapter
   names its distribution and version (`plugins` in its details, and in its message); a tie among
   built-ins reads as before.
6. **Same sandbox, same laws.** Importing a plugin runs its code in the client's process, as
   importing any installed library does. Everything it is asked to do (probe, inspect, plan,
   ingest) runs through the same registry, sandbox (ADR 0030) and per-call checks
   (`neptune.adapters.check`) as a built-in, because the job cannot tell them apart. Load time runs
   no adapter method: `neptune.adapters.conformance` would call them unsandboxed in the parent, so
   it stays a test-time gate for plugin authors.
7. **Findings, not failures.** The loader is a producer, `neptune.plugins` at 0.1.0, with the
   policy as its config and the admitted distributions as its libraries (§5). Its findings are about no bytes: the subject is an `ExternalObjectRef`
   (`neptune.plugins`, `<group>/<distribution>/<entry point>`, the version as revision), severity
   `warning`. Every job a client builds records them, so they are in the result and the package's
   receipt; the job does not fail.
8. **Policy.** `PluginPolicy(enabled=True, allow=None)`: every installed plugin by default;
   `enabled=False` none; `allow` only the named distributions. A distribution the policy leaves out
   leaves no trace, so a package made with `--no-plugins` does not depend on what is installed.
   SDK: `Neptune(plugins=None | True | False | PluginPolicy)`. CLI (`ingest`, `init-manifest`):
   `--no-plugins`, `--plugin DIST` (repeatable); both together is a usage error. An allowlist name
   that no installed distribution registering a plugin answers to (a typo, an uninstalled package)
   is refused before anything runs (`ConfigurationError`, `invalid_configuration`): an explicit
   allowlist never becomes a silent run without the plugin it meant.
9. **Explicit adapters stay explicit.** `Neptune(adapters=...)` uses exactly those adapters; plugins
   then add only their Sources (and findings about them). `default_registry()` stays the shipped
   adapters only.

## Alternatives considered

- **Last-wins or first-wins on a duplicate id**, ordered by distribution name. Deterministic, but
  a silent choice between two readers (non-negotiable 4), decided by naming. Lost.
- **Fail the job on a broken plugin.** One bad install would stop every ingest on the host,
  against partial success (non-negotiable 7). Lost.
- **Run `check_conformance` at load.** It calls probe, inspect and ingest in the parent process,
  outside the sandbox, on every client construction. The runtime already checks every call's
  output. Lost.
- **Opt-in plugins (none unless allowed).** Deploy's adapters would need a flag on every
  `neptune ingest`, which the issue's acceptance rejects; `--no-plugins` and the allowlist give the
  closed set when it is wanted. Lost.
- **Record each plugin as an adapter version suffix** (`1.0.0+neptune-deploy.0.0.1`). It breaks
  SemVer comparison and record ids for an unchanged adapter whose distribution only repackaged.
  `libraries` already means "output-affecting dependencies". Lost.
- **A finding subject that is a local path** (the distribution's `.dist-info`). An absolute,
  host-specific path in a package breaks determinism. Lost.

## Consequences

- With Deploy installed, a default `neptune ingest` registers `deploy_lifecycle`, probes every
  source with it, and records `neptune-deploy` in the loader's transform in every package. It claims nothing until a format lands (and a manifest cannot pin an adapter
  whose probe declines, ADR 0047), so no selection or evidence record changes; an unread file's
  `neptune.probe.unsupported` finding lists its decline beside the built-ins'.
- Every root test runs in the whole workspace environment, so installed members' adapters join
  default-registry tests. Tests that count probes or list adapters build their registry
  explicitly, or pass `plugins=False`.
- MVL-153 decides how a URI reaches a plugin Source; the loader already gives it a fixed,
  duplicate-free set of connectors.
- Importing runs in the client process with no time bound: a plugin whose import hangs (a network
  call, a deadlock) hangs every `Neptune()` and `neptune ingest` on the host before any job starts.
  The mitigation is `--no-plugins` (`plugins=False`), or an allowlist (`--plugin DIST`) that leaves
  the hanging distribution out; neither imports it. A bounded, isolated import would need a child
  process per plugin and a way back for the adapter object, which the sandbox's fork-per-call model
  does not have.
- Revisit if a plugin needs configuration at load time, if plugins need isolation (or a time bound)
  at import, or if two distributions legitimately need to share an id.
