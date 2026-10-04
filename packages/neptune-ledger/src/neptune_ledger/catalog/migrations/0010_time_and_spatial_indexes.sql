-- 0010 the derived time and spatial indexes (Ledger ADR 0015).
--
-- Applied in the same tenant schema, with search_path set to that schema alone. Both tables are
-- derived: registration computes every row from the verified package (its record lines and its
-- series files) and writes it in the registration transaction, so a replay of the registration
-- log rebuilds them byte for byte (ADR 0012). Nothing here converts a tick, a unit or a frame.

-- 0. Packages registered before this migration have no index rows. Every table here is
-- append-only, so they cannot be filled in: such a catalog is rebuilt from its packages and
-- registration log instead (ADR 0002 §4, ADR 0012).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM package) THEN
    RAISE EXCEPTION 'packages registered before migration 0010 have no time or spatial index;'
      ' rebuild this catalog from its packages and registration log (ADR 0015)';
  END IF;
END
$$;

-- 1. One row per interval on one clock (ADR 0015 §1). subject 'record' is a record's world time
-- (ADR 0003 §3, as record.world_* holds it): last_tick NULL when the end is open. subject 'series'
-- is the least and greatest known tick of one stream's series file on one of its clocks, with
-- how many rows have a known tick there and how many do not. Ticks are as stated, never
-- converted; intervals on two clocks are never compared.
CREATE TABLE time_interval (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  subject text NOT NULL CHECK (subject IN ('record', 'series')),
  kind text NOT NULL,
  record_id record_id NOT NULL,
  package_id content_id NOT NULL,
  clock record_id NOT NULL,
  first_tick bigint NOT NULL,
  last_tick bigint,
  rows_known bigint,
  rows_unknown bigint,
  registration_key bigint NOT NULL,
  -- The search key only: the stated extent [first, last or first] as a range, ends ordered so a
  -- stated last < first still has one. Results are decided on first_tick and last_tick.
  span int8range NOT NULL GENERATED ALWAYS AS (
    CASE
      WHEN greatest(first_tick, coalesce(last_tick, first_tick)) = 9223372036854775807
        THEN int8range(least(first_tick, coalesce(last_tick, first_tick)), NULL, '[)')
      ELSE int8range(least(first_tick, coalesce(last_tick, first_tick)),
                     greatest(first_tick, coalesce(last_tick, first_tick)) + 1, '[)')
    END
  ) STORED,
  PRIMARY KEY (tenant_id, subject, record_id, package_id, clock),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq),
  CHECK ((subject = 'series') = (rows_known IS NOT NULL)),
  CHECK ((rows_known IS NULL) = (rows_unknown IS NULL)),
  CHECK (subject = 'record' OR (kind = 'stream' AND last_tick IS NOT NULL
                                AND last_tick >= first_tick AND rows_known >= 1
                                AND rows_unknown >= 0))
);
-- "What exists on clock C between t1 and t2": the clock's rows, by start, and the overlap search.
CREATE INDEX time_interval_by_clock ON time_interval (clock, first_tick, last_tick);
CREATE INDEX time_interval_by_span ON time_interval USING gist (span);
CREATE INDEX time_interval_by_record ON time_interval (record_id);

-- 2. One row per spatial reference a record states (ADR 0015 §2): a FrameRef (reference_kind
-- 'frame', reference the canonical JSON of {frame_graph_id, frame_id}) or a CRS ('crs', of
-- {authority, code}), at JSON pointer `pointer` of the record. Where the record also states
-- coordinates in that reference, extent_pointer names them and min_* / max_* hold them as
-- declared, in `unit` (NULL when the unit is not Known). There is no default world frame: a row
-- exists only for a reference the record states as Known.
CREATE TABLE spatial_extent (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  kind text NOT NULL,
  record_id record_id NOT NULL,
  package_id content_id NOT NULL,
  pointer text NOT NULL CHECK (pointer ~ '^(/([^~/]|~[01])*)+$'),
  registration_key bigint NOT NULL,
  reference_kind text NOT NULL CHECK (reference_kind IN ('crs', 'frame')),
  reference text NOT NULL CHECK (reference LIKE '{%}'),
  extent_pointer text CHECK (extent_pointer ~ '^(/([^~/]|~[01])*)+$'),
  dims smallint CHECK (dims IN (2, 3)),
  unit text,
  min_x double precision,
  min_y double precision,
  min_z double precision,
  max_x double precision,
  max_y double precision,
  max_z double precision,
  -- The R-tree key (GiST over box): the extent's x and y. z is compared on the rows it returns.
  xy box GENERATED ALWAYS AS (box(point(min_x, min_y), point(max_x, max_y))) STORED,
  PRIMARY KEY (tenant_id, record_id, package_id, pointer),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq),
  CHECK ((extent_pointer IS NULL) = (dims IS NULL)),
  CHECK (unit IS NULL OR dims IS NOT NULL),
  CHECK ((dims IS NULL) = (min_x IS NULL) AND (dims IS NULL) = (max_x IS NULL)
         AND (dims IS NULL) = (min_y IS NULL) AND (dims IS NULL) = (max_y IS NULL)),
  CHECK ((dims IS NOT DISTINCT FROM 3) = (min_z IS NOT NULL)
         AND (min_z IS NULL) = (max_z IS NULL)),
  CHECK (min_x <= max_x AND min_y <= max_y AND (min_z IS NULL OR min_z <= max_z))
);
-- A query names its reference and unit: the rows of that reference, and the R-tree over extents.
CREATE INDEX spatial_extent_by_reference ON spatial_extent (reference_kind, reference, unit);
CREATE INDEX spatial_extent_by_xy ON spatial_extent USING gist (xy);
CREATE INDEX spatial_extent_by_record ON spatial_extent (record_id);

-- Append-only, like every table registration writes (ADR 0002 §6).
CREATE TRIGGER time_interval_append_only BEFORE UPDATE OR DELETE ON time_interval
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER time_interval_no_truncate BEFORE TRUNCATE ON time_interval
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER spatial_extent_append_only BEFORE UPDATE OR DELETE ON spatial_extent
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER spatial_extent_no_truncate BEFORE TRUNCATE ON spatial_extent
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
