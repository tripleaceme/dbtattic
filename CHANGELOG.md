# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-09-16

### Added

- A real help screen. `dbtattic` with no arguments now prints usage instead of a bare
  "Missing command" error: banner, what the tool is for, a commands panel with a sentence
  each, a typical first run, the Slim CI one-liner, and where the archive lives.
- Every command carries worked examples in its `--help`, and `query --help` lists the
  tables and views available to it.

### Changed

- Console entry point moved from `dbtattic.cli:app` to `dbtattic.cli:main`, so the banner
  can be printed before Click takes over. `--help` short-circuits inside Click before any
  Typer callback runs, so a callback cannot do this.
- Commands are listed in workflow order (capture, run, history, state, query, rebuild,
  info) rather than definition order.

## [0.1.0] — 2026-09-15

First release.

> Briefly published as `dbtscope` before release. Renamed to avoid confusion with
> Microsoft's SCOPE tooling, which has a VS Code extension of its own — and this project
> ships one too. `dbtscope` on PyPI is withdrawn; there is no upgrade path to follow
> because no functional release was ever made under that name.

### Added

- `dbtattic capture` — archive whatever artifacts are in `target/` and normalise them into
  DuckDB. Idempotent: every artifact is keyed by the `invocation_id` in its own metadata
  block, so capturing before and after a run never duplicates.
- `dbtattic run -- dbt build …` — capture, run dbt, capture again. Exit code, stdout and
  stderr pass straight through; a capture failure never breaks the dbt run.
- `dbtattic state --last-success` — restore a historical `manifest.json` and print its
  directory, so `dbt build --select state:modified --defer --state …` works on any adapter
  without dbt Cloud or Snowflake dbt Projects.
- `dbtattic query` / `history` / `info` — SQL and summaries over the store, with `--json`
  for tooling.
- `dbtattic rebuild` — re-derive the store from the archived JSON. The archive is the source
  of truth and DuckDB is a cache built from it, so schema changes and fixed extraction bugs
  are rebuilds rather than migrations.
- Config resolution: `--store` → `DBTATTIC_STORE` → `dbtattic.yml` → `vars.dbtattic` in
  `dbt_project.yml` → `./.dbtattic`.
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
- `retention` in `dbtattic.yml` is parsed but not enforced.
