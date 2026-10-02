-- 0007 package-schema version registry (Ledger ADR 0011).
--
-- Applied in the same tenant schema, with search_path set to that schema alone. Packages of every
-- package-schema version stay registered side by side forever (packages are never rewritten), and
-- each version's records are indexed by that version's projection mapping, which this Ledger
-- ships in catalog/projections.json. These tables record, per tenant, every version a registered
-- package or record states, the mapping it was indexed with, and the registration that first
-- brought it. A reader tells a projection column's NULL that is NotCovered (the record's schema
-- version has no such field) from one the package holds a state for (projection_covered below).

-- 0. A catalog that already holds packages has no registry rows for their versions, so its NULLs
-- could not be told apart. Every table here is append-only and filled at registration; such a
-- catalog is rebuilt from its packages and registration log instead (ADR 0002 §4).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM package) THEN
    RAISE EXCEPTION 'packages registered before migration 0007 have no schema-version registry'
      ' rows; rebuild this catalog from its packages and registration log (ADR 0011)';
  END IF;
END
$$;

-- 1. One row per package-schema version seen: a package's manifest version, and every version
-- its records state. mapping is the canonical JSON of the projection mapping the version was
-- indexed with (its kinds, hot-filter projections and free-form fields); mapping_digest is its
-- sha256. contract_version and schema_sha256 name the package-schema registry version
-- (contracts/package-schema/v<contract_version>/schema.json) the mapping was generated from: the
-- schema is referenced by digest, not copied.
CREATE TABLE schema_version (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  schema_version integer NOT NULL CHECK (schema_version >= 1),
  schema_id text NOT NULL CHECK (schema_id = 'urn:neptune:schema:canonical:' || schema_version),
  contract_version text NOT NULL
    CHECK (contract_version ~ ('^' || schema_version || '\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')),
  schema_sha256 content_id NOT NULL,
  kinds text[] NOT NULL CHECK (cardinality(kinds) >= 1),
  mapping text NOT NULL,
  mapping_digest content_id NOT NULL,
  first_registration_key bigint NOT NULL,
  PRIMARY KEY (tenant_id, schema_version),
  FOREIGN KEY (tenant_id, first_registration_key) REFERENCES package (tenant_id, tx_seq)
);

-- 2. The mapping's hot-filter projections, one row per record column a version's kind fills, so a
-- reader can ask whether a record's schema version states the field behind a column.
CREATE TABLE schema_version_projection (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  schema_version integer NOT NULL,
  kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
  field text NOT NULL CHECK (field ~ '^[a-z][a-z0-9_]*$'),
  column_name text NOT NULL CHECK (column_name ~ '^[a-z][a-z0-9_]*$'),
  PRIMARY KEY (tenant_id, schema_version, kind, column_name, field),
  FOREIGN KEY (tenant_id, schema_version) REFERENCES schema_version (tenant_id, schema_version)
);
CREATE INDEX schema_version_projection_by_column
  ON schema_version_projection (kind, column_name, schema_version);

CREATE TRIGGER schema_version_append_only BEFORE UPDATE OR DELETE ON schema_version
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER schema_version_no_truncate BEFORE TRUNCATE ON schema_version
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER schema_version_projection_append_only
  BEFORE UPDATE OR DELETE ON schema_version_projection
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER schema_version_projection_no_truncate BEFORE TRUNCATE ON schema_version_projection
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();

-- 3. Whether a record of p_kind at p_schema_version states the field that fills record column
-- p_column. For a NULL in that column: true means the record does not state the value as Known
-- (the package holds its state); false means the field is NotCovered by the record's schema
-- version, or the kind never has it. A NULL is never a fact either way.
CREATE FUNCTION projection_covered(p_kind text, p_schema_version integer, p_column text)
RETURNS boolean
LANGUAGE sql STABLE
SET search_path FROM CURRENT
AS $$
  SELECT EXISTS (
    SELECT 1 FROM schema_version_projection
    WHERE kind = p_kind AND schema_version = p_schema_version AND column_name = p_column
  )
$$;
