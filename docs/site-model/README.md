# Site model v3: start here

Neptune is being rebuilt as a **live, cited model of a whole site**: robots, machines, people (by role), work,
rules, and what feeds what. Claude queries it over MCP. This folder is the entry point for every M1 lane.

**Read only these:** this file, [`spec.md`](spec.md), and your GitHub issue. Don't read the v2 ADRs.

## Demo rules (this is a demo, not a product)

- **Keep:** every answer cites file/row/page/line; every fact is observed, stated or inferred; say
  `stale`/`unknown`/`ambiguous` instead of guessing; the same log folds to the same model.
- **Skip:** hostile-input, malformed, boundary, property and scale tests; security and sandbox work; schema
  versions, contracts and migrations; ADRs, unless you take a real fork from the spec (then one paragraph in
  the PR).
- **Tests:** a happy path per module, plus the gold questions (`packages/neptune-memory/tests/site/`).
- **Don't touch v2** (`neptune_memory/schema|consolidate|store|...`) or Demo v1. #188 deletes them.

## Lanes

| Issue | Files |
|---|---|
| #182 corpus + replay | `site/corpus/`, `site/replay.py`, `site/__main__.py` (`generate`), `make site-demo`/`site-replay` |
| #183 fact log | `site/log.py`, `site/build.py`, `__main__` `build`/`replay` |
| #184 site data model | `site/vocab.py`, `site/mappers/` |
| #185 views | `site/views.py` |
| #186 identity + confirm | `site/identity.py`, `site/confirm.py`, `__main__` `confirm` |
| #187 query + MCP | `site/query.py`, `site/render.py`, `neptune_context/mcp/site.py` |

`site/` = `packages/neptune-memory/src/neptune_memory/site/`. `site/facts.py` holds the shared types; it is
frozen, so import it and don't reshape it.

## Commands

```
make test-fast PKG=neptune-memory           # while iterating
uv run --all-packages --all-groups pytest packages/neptune-memory/tests/site -q
make check PKG=neptune-memory               # once, before the PR
```

PRs: branch `<issue>-<slug>`, body `Closes #<issue>`, commit trailer `Refs: #<issue>`. Merge on green CI.
