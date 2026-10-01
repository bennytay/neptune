# PostgreSQL 16 with Apache AGE (graph) and pgvector (vectors) for the harness's Ledger and Memory
# stages. The base image is pinned by digest (docker.io/apache/age, tag release_PG16_1.5.0); the
# pgvector package comes from the PGDG repository the base image already uses.
FROM apache/age:release_PG16_1.5.0@sha256:1a11a7035ca1585ebc56e649f9a000c49178ef250041ac7e6d69b23c0c58e678

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
