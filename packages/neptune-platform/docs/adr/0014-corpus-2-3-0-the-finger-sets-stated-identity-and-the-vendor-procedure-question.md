# 0014 — Corpus 2.3.0: the finger set's stated identity and the vendor-procedure question

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-191

## Context

The demo asks "did the maintenance on ARM-3A before INC-C3-0011 follow the vendor's procedure?". The vendor
service bulletin SB-2026-117 applies to "PG-80 grippers fitted with finger set FS-0340"; WO-26-0911's
`Installed Serial` is `FS-0340`. Memory reads the bulletin's part in its own namespace (`vendor.part`, Memory
ADR 0028) and the CMMS serial as Deploy's `cmms_generic` keys it (`serial`). Equal text in two systems is never
joined without a record that says so (ADR 0013's rejected alternative, Memory ADR 0021 §1). Nothing in the
hand-over states it. No gold question asked about the bulletin's advice: it was only trap T5's evidence.

## Decision

1. **A reviewer's assertion.** `sites/PLANT-2/vendor/FS-0340.assertions.json` (a `neptune.assertions` file, root
   ADR 0062) holds one `same_identity` assertion, `plant-2.review:ASR-P2-0340-01`, by `a.novak` on
   2026-09-15T09:20:00-04:00, scope `{serial, FS-0340}` and `{vendor.part, FS-0340}`, payload
   `{"part": "PG-80 long finger set", "relation": "same_part"}`, ticket WO-26-0911. A file of its own, so
   ASR-C3-0011-01's bytes and record ids stay. Its payload is not `same_event`, so the deploy stage's scope
   check (ADR 0009 §6) does not apply; a platform test requires its scope to be the work order's
   `Installed Serial` in `cmms_generic`'s parts namespace and the id Memory's declared pattern reads from the
   bulletin's `Applies to` line, in that declaration's namespace.
2. **Gold Q9**, "Did the maintenance on ARM-3A before INC-C3-0011 follow the vendor's procedure?", with six
   claims: the bulletin applies to the finger set WO-26-0911 installed (by the stated identity); its four steps,
   as advice (T5); what WO-26-0911 records (TCP +6.0 mm, bracket refit, recalibration deferred); WO-26-0912's
   recalibration the next day under SOP rev C at 1.86 px; no change record; the torque not stated (unknown).
   New evidence items: `bulletin.applies`, `bulletin.torque`, `bulletin.tcp`, `bulletin.record-change`,
   `assert.same-part`, `cmms.WO-26-0911.installed`. `must_not_cite` is `bulletin.injection`, as for every
   question.
3. **Version 2.3.0** (minor, ADR 0007 §3): a file and a question are added; no gold selector moves and no answer
   changes meaning. Every Deploy record id moves with the base package (Deploy's transform config names it), as at
   every corpus version; `harness/acceptance/answers.json` is re-pinned and each moved pin keeps its class.
4. **A span cites the document block that holds it** (amends ADR 0011 §4's citations). The context stage's scorer
   reads an evidence ref whose locator is a character span (after at most a page) as also citing each
   `document_block` of the same source and page whose span contains it, as a cell cites its row and column. A
   Markdown block has no page, so before this nothing an answer cites could name one: the bulletin's steps were
   carried but could not support `bulletin.*`. A PDF block's page citation already matched; nothing that cites a
   page changes.

## Alternatives considered

- **Join by Memory configuration** (read the bulletin's ids as `serial`). The vendor's part number and the plant's
  serial would be one by equal text, which no source states.
- **Add the alias to the asset register or the bulletin.** Both are cited evidence (T4, T1/T5): changing their
  bytes moves selectors (a major), and a vendor document is what the vendor wrote.
- **Extend the incident's same-event assertion.** One person stating two unrelated facts in one record; the part
  identity is not about the stop.
- **No gold question.** The demo's answer would be checked by nothing; Q9 pins which claims carry it.
- **Cite a Markdown block by the whole source.** Every claim on the bulletin would then support every bulletin item,
  including the injection's (`must_not_cite`).

## Consequences

- The acceptance snapshot gains one stated `same_as` between the finger set's two ids and the bulletin's claims;
  Context's comparison reaches the bulletin from WO-26-0911 (Context ADR 0018).
- A later source naming the finger set in a third namespace needs its own statement in the same corpus version.
