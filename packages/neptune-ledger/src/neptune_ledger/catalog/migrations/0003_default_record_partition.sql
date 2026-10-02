-- 0003 a home for every declared record kind (Ledger ADR 0008).
--
-- Applied after 0002 in the same tenant schema, with search_path set to that schema alone. 0001
-- gave each record kind of package schema 1 its own partition and no default, so a record kind a
-- later schema version adds (root ADR 0037: version 2's configuration kinds) had nowhere to go.
-- Which kinds a package may hold is decided at registration, before any row is written: its
-- schema version must be one this Ledger reads, its tables exactly that version's kinds, and every
-- line must pass the compiler's readers (ADR 0008 §2). The database keeps no list of kinds.

-- Rows of every declared kind without a partition of its own. A later migration may give a kind
-- its own partition; it then moves that kind's rows out of this one in the same migration.
CREATE TABLE record_default PARTITION OF record DEFAULT;

-- A kind names a package table, records/<kind>.jsonl: a lower-case identifier, never a path.
-- record_logical_id's kind is held to it by its foreign key into record.
ALTER TABLE record ADD CONSTRAINT record_kind_is_a_name CHECK (kind ~ '^[a-z][a-z0-9_]*$');
