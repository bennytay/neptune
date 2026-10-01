"""Test helper: register a bare package row the way the registration API will (ADR 0002 §4, §6).

A tick from the clock, then the registration log entry, then the package row that copies it.
"""

import psycopg

RECEIPT = "rec:sha256:" + "c" * 64


def add_package(
    conn: psycopg.Connection[tuple[object, ...]],
    schema: str,
    package_id: str,
    seq: int,
    *,
    time: str | None = None,
    tenant: str = "acme",
) -> str:
    time = time or f"2026-10-02T00:00:{seq:02d}.000000Z"
    conn.execute(f"SELECT {schema}.replay_tx(%s, %s)", (seq, time))
    conn.execute(
        f"INSERT INTO {schema}.registration_log VALUES (%s, %s, %s, %s, 'root', '0.0.1')",
        (tenant, seq, time, package_id),
    )
    conn.execute(
        f"INSERT INTO {schema}.package (tenant_id, package_id, schema_version, receipt_id)"
        " VALUES (%s, %s, 1, %s)",
        (tenant, package_id, RECEIPT),
    )
    return package_id
