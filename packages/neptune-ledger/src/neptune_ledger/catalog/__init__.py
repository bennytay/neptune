"""The catalog: package registry and the record, source, transform and clock indexes (ADR 0002).

The schema lives in ``migrations/`` as plain PostgreSQL 16 DDL; ``migrate`` applies it to a tenant.
``registry.PostgresCatalog`` registers and verifies packages: ``check`` verifies a package directory
without following links, ``index`` maps its records to rows, and ``sources`` re-hashes referenced
sources on request (Ledger ADRs 0006, 0007). ``manifest`` keeps the registration log as a file and
``rebuild`` rebuilds a tenant from it and dumps the catalog canonically (Ledger ADR 0012).
"""
