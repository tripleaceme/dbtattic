# dbtscope

Preserve and query dbt artifact history — on any warehouse, with no warehouse compute.

## The problem

dbt overwrites `target/` on every invocation. `manifest.json` is rewritten *during parsing*,
`run_results.json` at the *end of the run*. So the metadata you most want for monitoring,
deferral and debugging is destroyed by the next command you type.

It is worse than it sounds. A bare `dbt parse` — no models built, nothing executed — destroys
the manifest belonging to your last real run:

```
BEFORE  manifest.json     invocation_id=9bf2d731…   ← pair matches
        run_results.json  invocation_id=9bf2d731…

$ dbt parse

AFTER   manifest.json     invocation_id=f2805d03…   ← mismatched
        run_results.json  invocation_id=9bf2d731…
```

`dbt compile`, `dbt ls`, `dbt docs generate` and your IDE's background autocomplete all do this.

## Why not the existing options

`dbt_artifacts` and `elementary` solve this by running *inside* dbt via `on-run-end` hooks. A dbt
package can only write through the adapter connection, so every run issues extra `insert`
statements against your warehouse. That is a structural tax, not an implementation choice.

Snowflake's dbt Projects feature preserves artifacts automatically — if you are on Snowflake.
Databricks, BigQuery, Postgres and ClickHouse users hand-roll it.

dbtscope runs as a separate process, so it costs no warehouse compute, works identically on every
adapter, and can capture things a dbt package cannot see: `logs/dbt.log`, `compiled/`, `run/`.

## Install

```bash
pip install dbtscope        # or: uvx dbtscope
```

From source:

```bash
git clone https://github.com/tripleaceme/dbtscope && cd dbtscope
uv venv && uv pip install -e . "dbt-core~=1.10" dbt-duckdb
python tests/smoke.py       # end-to-end against the bundled fixture project
```

## Use

```bash
# capture whatever is in target/ right now (idempotent)
dbtscope capture

# capture, run dbt, capture again — exit code and output pass straight through
dbtscope run -- dbt build --target prod

# state comparison and deferral, on any adapter
dbt build --select state:modified --defer --state $(dbtscope state --last-success)

# query the history
dbtscope history
dbtscope query "select * from v_node_runtime where name = 'customer_summary'"
```

Capture before every dbt command without changing how you type them:

```bash
# .zshrc — one line, inspectable, reversible
dbt() { dbtscope capture --quiet --phase pre; command dbt "$@"; }
```

`on-run-start` cannot do this: hooks fire *after* parsing, by which point `manifest.json` is
already gone.

## Configure

```yaml
# dbtscope.yml, next to dbt_project.yml
store: ./.dbtscope                 # or s3://…  (0.2)
capture:
  when: both                       # before-run | after-run | both
  artifacts: [manifest, run_results, sources, catalog]
```

Resolution order, highest first: `--store` → `DBTSCOPE_STORE` → `dbtscope.yml` →
`vars.dbtscope` in `dbt_project.yml` → `./.dbtscope`.

## Schema

Grain is `(invocation_id, artifact_type)` — never "a snapshot of target/", because target/ can
hold artifacts from two different invocations at once.

| table | grain |
|---|---|
| `invocation` | invocation_id |
| `artifact_capture` | invocation_id, artifact_type |
| `node_version` | node_version_id (content hash) |
| `node_invocation` | invocation_id, unique_id |
| `node_depends_on` | invocation_id, unique_id, depends_on_id |
| `run_result` | invocation_id, unique_id |
| `source_freshness` | invocation_id, unique_id |

Views: `node` (the flat per-invocation shape — query this, not the two tables behind it),
`v_run_history`, `v_node_runtime`, `v_test_history`, `v_node_changes`, `v_latest_edges`.

### Node de-duplication

Node content is stored once per distinct version, so a model untouched between runs costs one
row in `node_invocation` rather than a fresh copy of its SQL. The ratio improves as history
grows — on the fixture project, 22 versions back 160 rows after 8 invocations.

Two things that are *not* the identity key, both learned the hard way:

- **dbt's own `checksum`.** It covers the raw file only, it is empty for every test node, and
  the same file compiled against dev and prod shares it while being different content.
- **`compiled_code`.** It is null in a parse-only manifest, so including it made every
  `dbt parse` mint a duplicate version set that differed by one null column. It is derived
  from raw code + config + resolved target, all of which are in the hash, so it is stored as
  a fill-in attribute instead.

## Rebuild

The archived JSON is the source of truth; DuckDB is a cache derived from it.

```bash
dbtscope rebuild     # re-derive the store from archive/
```

That makes a schema change or a fixed extraction bug a rebuild rather than a migration —
opening a store written by an older dbtscope reports the version mismatch and points here.

## VS Code extension

`vscode/` — Invocations tree, canned Insights (runtime regressions, flaky tests, recently
changed models), and a themed query panel over the same store.

```bash
cd vscode && npm install && npm run package
```

It shells out to this CLI (`dbtscope query --json`, `dbtscope info --json`) rather than
embedding DuckDB: no native dependencies, and the schema has one definition instead of two
that drift. The packaged vsix is ~12 KB.

## Status

0.1.0 — local store, manifest/run_results/sources/catalog capture, node de-duplication,
state restore, rebuild, VS Code extension. Remote backends (S3/GCS/warehouse), log
ingestion and the MCP server are next.
