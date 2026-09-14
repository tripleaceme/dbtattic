# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-09-14

First release.

### Added

- `dbtscope capture` — archive whatever artifacts are in `target/` and normalise them into
  DuckDB. Idempotent: every artifact is keyed by the `invocation_id` in its own metadata
  block, so capturing before and after a run never duplicates.
- `dbtscope run -- dbt build …` — capture, run dbt, capture again. Exit code, stdout and
  stderr pass straight through; a capture failure never breaks the dbt run.
- `dbtscope state --last-success` — restore a historical `manifest.json` and print its
  directory, so `dbt build --select state:modified --defer --state …` works on any adapter
  without dbt Cloud or Snowflake dbt Projects.
- `dbtscope query` / `history` / `info` — SQL and summaries over the store, with `--json`
  for tooling.
- `dbtscope rebuild` — re-derive the store from the archived JSON. The archive is the source
  of truth and DuckDB is a cache built from it, so schema changes and fixed extraction bugs
  are rebuilds rather than migrations.
- Config resolution: `--store` → `DBTSCOPE_STORE` → `dbtscope.yml` → `vars.dbtscope` in
  `dbt_project.yml` → `./.dbtscope`.
- Tables `invocation`, `artifact_capture`, `node_version`, `node_invocation`,
  `node_depends_on`, `run_result`, `source_freshness`; views `node`, `v_run_history`,
  `v_node_runtime`, `v_test_history`, `v_node_changes`, `v_latest_edges`.
- Node de-duplication: node content is stored once per distinct version, keyed on a content
  hash rather than dbt's `checksum` (which is empty for every test node and shared across
  dev/prod compiles of the same file). `compiled_code` is excluded from the identity because
  it is null in a parse-only manifest and is derived from inputs already in the hash.

### Known limitations

- Only the local store backend is implemented. S3/GCS and warehouse backends are planned.
- `logs/dbt.log`, `compiled/` and `run/` are not yet ingested.
- `retention` in `dbtscope.yml` is parsed but not enforced.
