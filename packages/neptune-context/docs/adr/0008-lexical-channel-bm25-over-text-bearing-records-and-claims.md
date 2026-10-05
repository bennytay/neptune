# 0008 — The lexical channel: in-process BM25 over text-bearing records and claims

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-142

## Context

The query language routes free text to a lexical (BM25) and a vector channel, scoped to fields
(ADR 0002 §5), and the channel interface exists (ADR 0007 §2). Robotics questions are full of
exact strings: serial numbers, ROS topic names, declared ids, firmware versions, SOP wording. A
prose search engine splits, stems or stop-words them away, and a channel that returns a snippet
without its evidence breaks package rule 2.

The issue asks for "Postgres FTS first, with the channel interface allowing tantivy/Lucene". Three
facts decide it differently:

- Context has no Postgres in its stack or CI, may not construct the Ledger's catalog (import
  boundary, ADR 0007 §5), and its channels must be deterministic with no network (rule 5).
- SQLite FTS5 is optional in builds and its tokenizers and `bm25()` are version-dependent, so
  "same index and query, same answer on every machine" cannot be promised.
- Neither upstream exposes text in bulk. The Ledger's `query(spec)` returns record ids, evidence
  anchors, transforms and assertion kinds, not record text. Memory's `MemoryReader` answers per node
  and has no enumeration; only a `GraphDocument` holds every claim version.

## Decision

1. **An in-process, pure-Python BM25 backend behind a small `TextIndex` protocol**
   (`retrieve.bm25`). `TextIndex` has two methods: `add(tenant, units)` and `search(tenant,
   request)`. It sees text units (`IndexedText`: key, field, source, text, `inferred`, visibility
   window) and returns scored keys; it knows nothing of claims, records or packets. `Bm25Index` is
   Okapi BM25 (k1 = 1.2, b = 0.75, Lucene's non-negative idf) over positional postings. Postgres
   full-text and tantivy are later implementations of the same two methods; nothing above the
   protocol changes. Scores are rounded to 9 decimals and ordered by score, then key, so results never
   depend on insertion order or on the last bit of a platform's `log`.
2. **Identifier-preserving analysis, two modes** (`retrieve.analysis`). Text is NFKC-normalised and
   case-folded and cut into words; words joined by one of `- _ . / :` between alphanumerics form a
   compound whose parts keep consecutive positions, so `SN-A4471-9`, `/uav21/imu/data` and
   `asset_tag:hx-02` are found as exact phrases of their parts and `SN-A4471-9` never matches
   `SN-A4471-7`. `prose` mode also stems a standalone all-letter word (`english-light`: regular
   plurals, `-ing`, `-ed`, trailing `e`; ASCII only, a rule list that cannot drift between library
   versions); `verbatim` mode (the `declared_id` field) and every compound part are never stemmed. No
   stop words. No script segmentation (CJK stays one word per run). The analyser is named per tenant
   (`english`, `verbatim`) and is part of the index's configuration: changing it means a new index.
3. **Query text.** Outside quotes, each compound is an optional phrase of its parts (BM25 over the
   disjunction); a quoted segment is one required, adjacent, ordered phrase. Unbalanced quotes are
   punctuation. At most 64 clauses are used; the rest are ignored and a `not_covered` gap says so.
4. **Statistics follow what the query may see.** Corpus size, average length and document frequency
   are computed over the visible units of the requested fields, per analysis mode, and a phrase is
   scored as one pseudo-term with its own document frequency. Scoping a query to `document` therefore
   scores against documents only, and withheld inferred text never changes the score of anything
   returned.
5. **What is indexed** (`retrieve.lexical`). `claim_text`: the text of text-valued claim objects
   (`has_summary`, `maintenance_state`, `has_description`, ...). `declared_id`: the node ids a claim
   names; a hit is the claims that name the id. `record`, `document`, `finding`: `Passage`s, spans of
   Ledger record text. The issue's `in: sop | incident | summary` are not new fields: SOP and incident
   text are `document` and `record` passages, an incident description or a summary is `claim_text`;
   `query-packet` does not change.
   - `LexicalCorpus.add_claims(claims, through=head)` takes every version of every claim (a graph
     document's `resolution.claims`), keeping `recorded_at` and `superseded_at`, so any earlier
     `as_of` is searchable. `through` is the head it covers.
   - `passages_from_catalog(catalog, kinds, text_of)` pages the Ledger's `query(spec)` for rows and
     builds a passage per row: evidence from the row's anchor, transform from `lineage`, assertion
     kind and `registration_seq` from the row. The catalog indexes records, not their bytes, so the
     host supplies `text_of(row)`. A row with no anchor, no stated assertion kind, no transform, a
     non-record id or invalid text is skipped and named (`Skipped`), never indexed with a guess.
6. **Snapshots and inference.** A claim unit is visible at Memory's `memory_as_of` when
   `recorded_at <= tx < superseded_at`; a passage at the Ledger's `as_of` when `registration_seq <=
   as_of`. Every matching claim is then read back from Memory's reader at the snapshot, so it is
   presented as known then (`superseded_at` open), with the resolver findings the reader attaches;
   one the reader does not hold is dropped. Supersessions in `(memory_as_of, head]` of hit claims are
   reported with the versions that superseded them. Inferred units take part only when
   `include_inferred`; otherwise a second search over inferred units alone names what was withheld in
   one `inferred_withheld` gap (claim ids, record ids; never content).
7. **Items and provenance.** A claim hit is a `ClaimItem`; a passage hit is a `DocumentSpanItem` with
   the text as extracted. A `Passage` cannot be built without evidence, a transform and an assertion
   kind: its constructor builds the span through the packet model and refuses what the model refuses,
   so no snippet reaches a packet without provenance. A claim beyond the pinned graph-schema is named
   in a `not_covered` gap and never carried (ADR 0007 §3, §6). Scores are BM25 raw scores; the
   interface's `answer()` ranks them and rewrites each item's relevance to this channel's hit.
8. **Explicit coverage.** A requested field with nothing indexed, claim text indexed through an
   earlier transaction than the snapshot reads, records skipped for want of provenance, text with no
   searchable term and clauses beyond the bound are gaps (`not_covered` or `unknown`) at `/text/...`;
   an empty result is never a bare empty list that reads as "nothing exists".
9. **Tenancy.** Every operation takes a tenant (1-128 printable characters); partitions share nothing,
   including statistics. `LexicalCorpus(index, tenant=...)` binds a corpus to one; a Context engine is
   single-tenant like the catalog API, and several corpora may share one index object.
10. **A corpus is rebuilt, not edited.** Adding an existing key with different content is refused
   (`conflicting_key`, first wins); new Memory transactions or a new parser lineage mean a new corpus.

## Alternatives considered

- **Postgres full-text first (the issue's wording).** No Postgres in Context's stack or CI, and the
  Ledger's catalog is off-limits to construct. Kept as a later `TextIndex` implementation.
- **SQLite FTS5 with `bm25()`.** Optional at build time, tokenizer and ranking depend on the SQLite
  version: breaks byte-identical answers across machines. Lost.
- **tantivy or Lucene now.** A native or JVM dependency for a local corpus of thousands; the protocol
  keeps the door open for when a budget test demands it.
- **One field-agnostic analyser with stemming everywhere.** Stems serial numbers, topics and ids; the
  issue forbids "stemming damage". Lost for the two-mode design.
- **Index only the claims Memory returns at query time.** The reader has no enumeration or by-text
  call, so the channel could only search nodes it was already told about. Lost: the index is built
  from the graph document and the reader is the arbiter at the snapshot.
- **Take text from the catalog.** `query(spec)` carries none; asking the catalog for bytes is
  `resolve` per record, and the bytes live in packages Context may not read. The host supplies
  `text_of`.
- **Add `sop`, `incident` and `summary` to `TextField`.** A packet-visible vocabulary change for what
  the existing fields already cover. Lost; revisit if consumers need finer scoping.

## Consequences

- MVL-143 (vector) implements the same `RetrievalChannel`; fusion treats both as peers. The engine
  takes `LexicalChannel(corpus, memory)` through `channels=`.
- **Open for the engine and Platform.** The text clause is answered on its own: this channel does not
  restrict hits to the query's subjects, site or `during` (ADR 0002 §1 conjunction); that is a plan
  step over fused items. Platform wires `text_of` (reading the packages' record text) and builds the
  corpus from the graph document and the catalog; Memory has no claim enumeration and the catalog no
  text, so both are asks if the index must be built from live services.
- The index lives in memory: fine for a local Ledger, a budget test (`eval/`) decides when a
  Postgres or tantivy backend is due. Revisit on that, on a need for per-field boosts or non-English
  analysers beyond `english` and `verbatim`, or on script segmentation for CJK corpora.
