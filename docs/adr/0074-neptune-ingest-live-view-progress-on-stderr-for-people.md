# 0074 — `neptune ingest`: a readable live view on stderr, for people

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-11
- Extends: ADR 0043 §3 (output)

## Context

`neptune ingest` is quiet, and `-v` prints one raw JSON event per line (about 500 for a 56-file
folder): useful to debug the job, unreadable to watch it. A person who points Neptune at a folder
wants to see which files were read, by which adapter, how many records came out and what was
flagged, and to trust the totals at the end.

## Decision

1. **A live view, stderr only.** `neptune.cli.progress.LiveView` renders the job's own events
   into one line per source as it finishes (`✓ path  adapter  N records  · k findings`), then, once
   the package is committed, the sources the package flagged with an error or warning, a summary
   rule (`── 55 ingested · 1,444 records · 1 error · 36 warnings · 5.6 s ──`) and the top finding
   kinds in plain words (`unknown:` for missing evidence, `flagged:` for the rest). It uses the
   event stream, the receipt and the package's own `ingest_finding` table; it parses nothing and
   decides nothing.
2. **When it shows.** On by default when stderr is a terminal and neither `--json` nor `-v` is
   given. `--progress` forces it on (for a pipe or a recording), `--no-progress` off. `--progress`
   with `-v` is a usage error: both write to stderr. `-v` keeps its meaning (raw events) and
   `--json` its byte-identical stdout; a non-terminal stderr prints exactly what it printed.
3. **Colour only for a terminal** (and not with `NO_COLOR`); ASCII glyphs if the stream cannot
   encode the Unicode ones.
4. **Findings are said in words, never hidden.** A code with no wording falls back to its count,
   code and message. Findings carry no source until the package exists, so a source's live line
   gives only a count; the sources that need a look follow the last line.
5. **No effect on the package.** The only clock is the elapsed time printed on stderr; it reaches
   neither stdout, the result line nor the package.

## Alternatives considered

- **Redraw in place (a progress bar).** Breaks in pipes and recordings and loses the history; one
  appended line per source reads the same live and in a log.
- **Make `-v` the readable view.** `-v` is how the event stream is debugged; changing it would
  break anyone parsing it.
- **Wait for the package and print everything at the end.** Nothing to watch for the tens of
  seconds a real folder takes.

## Consequences

Anyone filming or sharing a run gets text that explains itself. The wording tables
(`_UNKNOWN`, `_FLAGGED`, `_SHORT`) grow with finding codes; an unlisted code is still shown, less
tidily. Revisit if events gain a per-source finding code, which would put the words on the live
line.
