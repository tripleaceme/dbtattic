"""DuckDB store: schema and connection handling.

Grain note: the unit of capture is (invocation_id, artifact_type), NOT "a
snapshot of target/". dbt overwrites manifest.json during parsing and
run_results.json at the end of a run, so a `dbt parse` between two builds
leaves target/ holding artifacts from two *different* invocations. Every
artifact is therefore keyed by the invocation_id found in its own metadata
block, and an invocation may legitimately have a manifest and no run results.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_VERSION = 2

DDL = """
create table if not exists dbtscope_meta (
    schema_version integer not null
);

-- One row per (invocation, artifact type). The dedup key for idempotent capture.
create table if not exists artifact_capture (
    invocation_id   varchar not null,
    artifact_type   varchar not null,
    captured_at     timestamp not null,
    captured_phase  varchar,          -- 'pre' | 'post' | 'manual'
    archive_path    varchar,
    size_bytes      bigint,
    primary key (invocation_id, artifact_type)
);

create table if not exists invocation (
    invocation_id        varchar primary key,
    dbt_version          varchar,
    project_name         varchar,
    project_id           varchar,
    adapter_type         varchar,
    generated_at         timestamp,
    invocation_started_at timestamp,
    elapsed_time         double,
    command              varchar,      -- args.which: run | build | test | parse ...
    invocation_command   varchar,      -- full CLI string, when run_results is present
    target               varchar,
    full_refresh         boolean,
    git_sha              varchar,
    git_branch           varchar,
    env                  varchar
);

-- Node content, stored once per distinct version.
--
-- Keyed on a hash of the content itself, NOT on dbt's `checksum` field.
-- dbt's checksum covers the raw file only, so the same file compiled against
-- dev and prod -- different database/schema, different compiled_code -- shares
-- a checksum while being genuinely different content. Hashing what we store
-- keeps those apart and still collapses the common case, where a node is
-- byte-identical across every run that did not touch it.
create table if not exists node_version (
    node_version_id varchar primary key,
    unique_id       varchar not null,
    resource_type   varchar,
    name            varchar,
    package_name    varchar,
    path            varchar,
    database        varchar,
    schema_name     varchar,
    alias           varchar,
    materialized    varchar,
    checksum        varchar,          -- dbt's own raw-file checksum, kept for reference
    description     varchar,
    tags            varchar[],
    compiled_code   varchar
);

-- Which node version was present in which invocation. Narrow by design.
create table if not exists node_invocation (
    invocation_id   varchar not null,
    unique_id       varchar not null,
    node_version_id varchar not null,
    primary key (invocation_id, unique_id)
);

create table if not exists node_depends_on (
    invocation_id   varchar not null,
    unique_id       varchar not null,
    depends_on_id   varchar not null,
    primary key (invocation_id, unique_id, depends_on_id)
);

create table if not exists run_result (
    invocation_id       varchar not null,
    unique_id           varchar not null,
    status              varchar,
    execution_time      double,
    rows_affected       bigint,
    query_id            varchar,
    adapter_code        varchar,
    failures            bigint,
    thread_id           varchar,
    message             varchar,
    compile_started_at  timestamp,
    execute_completed_at timestamp,
    primary key (invocation_id, unique_id)
);

create table if not exists source_freshness (
    invocation_id   varchar not null,
    unique_id       varchar not null,
    status          varchar,
    max_loaded_at   timestamp,
    age             double,
    primary key (invocation_id, unique_id)
);
"""

VIEWS = """
-- `node` is the flat, per-invocation shape everything queries against. The
-- split into node_version + node_invocation is a storage concern, so it stays
-- behind this view rather than leaking into every query.
create or replace view node as
select
    ni.invocation_id,
    ni.unique_id,
    nv.resource_type,
    nv.name,
    nv.package_name,
    nv.path,
    nv.database,
    nv.schema_name,
    nv.alias,
    nv.materialized,
    nv.checksum,
    nv.description,
    nv.tags,
    nv.compiled_code,
    ni.node_version_id
from node_invocation ni
join node_version nv using (node_version_id);

-- When a node's content last changed, and what it changed from. This is the
-- incident-forensics query: "the numbers moved on Tuesday -- what deployed?"
create or replace view v_node_changes as
select
    unique_id,
    name,
    resource_type,
    node_version_id,
    min(generated_at) as first_seen,
    max(generated_at) as last_seen,
    count(*)          as invocations
from node
join invocation using (invocation_id)
group by all
order by unique_id, first_seen;

-- Run history: one row per invocation that actually executed nodes.
create or replace view v_run_history as
select
    i.invocation_id,
    i.generated_at,
    i.command,
    i.target,
    i.dbt_version,
    i.elapsed_time,
    count(r.unique_id)                                       as nodes_run,
    count(*) filter (where r.status in ('error','fail'))      as failures,
    count(*) filter (where r.status = 'skipped')              as skipped,
    round(sum(r.execution_time), 3)                           as node_seconds
from invocation i
left join run_result r using (invocation_id)
group by all
order by i.generated_at desc;

-- Per-node runtime across invocations: the regression detector.
create or replace view v_node_runtime as
select
    r.unique_id,
    n.resource_type,
    n.name,
    i.generated_at,
    r.invocation_id,
    r.status,
    r.execution_time,
    r.rows_affected
from run_result r
join invocation i using (invocation_id)
left join node n using (invocation_id, unique_id)
order by r.unique_id, i.generated_at desc;

-- Test outcome history: flakiness becomes visible over many invocations.
create or replace view v_test_history as
select
    r.unique_id,
    n.name,
    count(*)                                          as runs,
    count(*) filter (where r.status = 'pass')         as passes,
    count(*) filter (where r.status in ('fail','error')) as failures,
    -- a test that is constantly skipped is its own signal: something upstream
    -- keeps failing, so the test has not actually been checking anything.
    count(*) filter (where r.status = 'skipped')      as skipped,
    round(100.0 * count(*) filter (where r.status in ('fail','error'))
          / nullif(count(*) filter (where r.status <> 'skipped'), 0), 1)
                                                      as failure_pct,
    max(i.generated_at)                               as last_seen
from run_result r
join invocation i using (invocation_id)
left join node n using (invocation_id, unique_id)
where n.resource_type = 'test'
group by all
order by failure_pct desc nulls last, runs desc;

-- Blast radius: everything downstream of a node, at its latest captured state.
-- parent_map is stored as an edge list precisely so this is a recursive CTE.
create or replace view v_latest_edges as
select e.*
from node_depends_on e
join (
    select invocation_id from invocation
    where invocation_id in (select invocation_id from node_depends_on)
    order by generated_at desc limit 1
) latest using (invocation_id);
"""


class SchemaOutOfDate(Exception):
    """The DuckDB file predates the current extraction schema."""

    def __init__(self, found: int) -> None:
        self.found = found
        super().__init__(
            f"store schema is v{found}, this dbtscope expects v{SCHEMA_VERSION}. "
            "Run `dbtscope rebuild` -- the archived JSON is the source of truth, "
            "so nothing is lost."
        )


def _stored_version(con: duckdb.DuckDBPyConnection) -> int | None:
    try:
        row = con.execute("select max(schema_version) from dbtscope_meta").fetchone()
        return row[0] if row else None
    except duckdb.Error:
        return None  # pre-versioning or empty file


def connect(
    db_path: Path, read_only: bool = False, allow_stale: bool = False
) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists()
    con = duckdb.connect(str(db_path), read_only=read_only)

    if existed and not allow_stale:
        found = _stored_version(con)
        if found is not None and found != SCHEMA_VERSION:
            con.close()
            raise SchemaOutOfDate(found)

    if not read_only:
        con.execute(DDL)
        con.execute(VIEWS)
        if con.execute("select count(*) from dbtscope_meta").fetchone()[0] == 0:
            con.execute("insert into dbtscope_meta values (?)", [SCHEMA_VERSION])
    return con


def already_captured(con: duckdb.DuckDBPyConnection, invocation_id: str, artifact: str) -> bool:
    """The idempotency check that makes before+after capture safe to run repeatedly."""
    row = con.execute(
        "select 1 from artifact_capture where invocation_id = ? and artifact_type = ?",
        [invocation_id, artifact],
    ).fetchone()
    return row is not None
