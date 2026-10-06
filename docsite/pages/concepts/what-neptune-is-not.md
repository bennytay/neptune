# What Neptune is not

Neptune answers *what exactly exists in this evidence, who says so, and since when*. It is easy to
mistake it for something nearby. It is none of these.

**Not RAG for MCAP.** Neptune does not chop recordings into text, embed the chunks and hope a similar
chunk comes back. The compiler decodes each format into typed records (streams, series, transforms,
calibrations, lifecycle records) with a locator down to the message, row or span. Context answers with
claims and records that cite that evidence, at a stated transaction. A retrieval channel may rank them;
it never replaces the citation.

**Not a document chunker.** A PDF, a work order or a standard operating procedure is evidence with
structure: spans, tables, rows and declared identifiers, each addressable. Neptune keeps that structure
and its provenance; it does not reduce documents to overlapping windows of text.

**Not a vector-database wrapper.** The product is a provenance-preserving claim graph with two time
axes, explicit missingness and identities that are linked, never merged. An index over it (lexical or
vector) is a way in, not the store of truth: every hit resolves to a claim or a record with its evidence.

**Not a dashboard.** Neptune is not an analytics or fleet-operations screen. It produces packages,
claims, context packets and evidence packs for agents, engineers and the tools they already use; the
one front end Deploy plans is a thin, read-only view over evidence packs.

**Not a trainer.** Neptune does not train or fine-tune models, learn policies, generate evaluations or run
simulations. It gives those systems evidence they can trust and trace, with inference labelled as
inference.

It is also not a safety-case generator, a compliance engine or a maintenance system. Deploy reads
work orders, incidents and authorisation envelopes as **stated** evidence and cites them; it never
ranks a severity or decides that a robot is safe.

And it is not built for one kind of robot. Nothing in the data model assumes a flight controller, a
single vehicle or one morphology: arms, mobile bases, legged robots, humanoids, aerial, marine and road
vehicles and mixed fleets go through the same compiler.

## Where to go next

- [Architecture: where Neptune sits](../docs/architecture.md#where-neptune-sits)
- [What a claim is](claims.md)
- [Evidence and inference](evidence-and-inference.md)
