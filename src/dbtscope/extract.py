"""JSON artifact -> relational tables, done inside DuckDB.

Why the JSON stays JSON: manifest.nodes is a dictionary keyed by unique_id
whose values are heterogeneous. read_json_auto() tries to unify them into one
STRUCT and produces a type signature 31k characters long for a 20-node project
-- with the model names as field names, so the schema changes whenever anyone
adds a model. Reading the key as JSON and iterating json_keys() gives a schema
that depends on our extraction SQL rather than on the user's project.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

# Artifacts whose top-level metadata block carries an invocation_id.
KEYED_ARTIFACTS = {"manifest", "run_results", "sources", "catalog"}

FILENAMES = {
    "manifest": "manifest.json",
    "run_results": "run_results.json",
    "sources": "sources.json",
    "catalog": "catalog.json",
}


def _lit(value: str | Path) -> str:
    """SQL string literal. DuckDB cannot bind parameters inside DDL, so paths
    that feed `create view ... read_json(...)` have to be inlined."""
    return "'" + str(value).replace("'", "''") + "'"


def peek_invocation_id(path: Path) -> str | None:
    """Read just the metadata block. Avoids parsing an 80MB manifest to get a UUID."""
    try:
        with path.open("rb") as fh:
            data = json.load(fh)
        return (data.get("metadata") or {}).get("invocation_id")
    except (OSError, json.JSONDecodeError):
        return None


def _upsert_invocation(con: duckdb.DuckDBPyConnection, cols: dict) -> None:
    """Both manifest and run_results contribute columns for the same invocation row."""
    keys = [k for k, v in cols.items() if v is not None]
    if "invocation_id" not in keys:
        return
    updates = [k for k in keys if k != "invocation_id"]
    sql = f"""
        insert into invocation ({", ".join(keys)})
        values ({", ".join("?" for _ in keys)})
        on conflict (invocation_id) do update set
        {", ".join(f"{k} = coalesce(excluded.{k}, invocation.{k})" for k in updates)}
    """ if updates else """
        insert into invocation (invocation_id) values (?) on conflict do nothing
    """
    con.execute(sql, [cols[k] for k in keys])


def ingest_manifest(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    invocation_id: str,
    project_dir: Path | None = None,
) -> int:
    con.execute(
        f"""
        create or replace temp view _m as
        select * from read_json({_lit(path)}, columns={{
            'metadata':'JSON', 'nodes':'JSON', 'sources':'JSON',
            'parent_map':'JSON', 'child_map':'JSON'
        }})
        """
    )

    meta = con.execute(
        """
        select
            metadata->>'dbt_version',
            metadata->>'project_name',
            metadata->>'project_id',
            metadata->>'adapter_type',
            try_cast(replace(metadata->>'generated_at','Z','') as timestamp),
            metadata->'env'->>'DBT_CLOUD_GIT_SHA'
        from _m
        """
    ).fetchone()

    _upsert_invocation(
        con,
        {
            "invocation_id": invocation_id,
            "dbt_version": meta[0],
            "project_name": meta[1],
            "project_id": meta[2],
            "adapter_type": meta[3],
            "generated_at": meta[4],
        },
    )

    # nodes + sources share a shape for the columns we keep. Each is reduced to
    # a content hash so that a node untouched between runs is stored once, not
    # once per invocation. to_json over an ordered list gives a deterministic
    # encoding that keeps NULL distinct from '' -- concat_ws would collide them.
    con.execute(
        """
        create or replace temp view _nodes as
        select
            k                                              as unique_id,
            n ->> 'resource_type'                          as resource_type,
            n ->> 'name'                                   as name,
            n ->> 'package_name'                           as package_name,
            n ->> 'path'                                   as path,
            n ->> 'database'                               as database,
            n ->> 'schema'                                 as schema_name,
            n ->> 'alias'                                  as alias,
            n -> 'config' ->> 'materialized'               as materialized,
            n -> 'checksum' ->> 'checksum'                 as checksum,
            n ->> 'description'                            as description,
            coalesce(from_json(n -> 'tags', '["VARCHAR"]'), []::varchar[]) as tags,
            n ->> 'compiled_code'                          as compiled_code,
            -- hashed but not stored: keeps the identity sensitive to real edits
            -- without carrying a second copy of every model's source.
            md5(coalesce(n ->> 'raw_code', ''))            as raw_digest,
            cast(n -> 'config' as varchar)                 as config_json
        from (
            select k, nodes -> k as n from _m, unnest(json_keys(nodes)) as t(k)
            union all
            select k, sources -> k as n from _m, unnest(json_keys(sources)) as t(k)
        )
        """
    )

    con.execute(
        """
        create or replace temp view _nv as
        select
            -- compiled_code is deliberately NOT in the identity: it is derived
            -- from raw_code + config + resolved target, all of which are here,
            -- and it is null in a parse-only manifest. Including it would mint
            -- a duplicate version set on every `dbt parse`.
            md5(to_json([
                unique_id, resource_type, name, package_name, path,
                database, schema_name, alias, materialized, checksum,
                description, cast(tags as varchar), config_json, raw_digest
            ])) as node_version_id,
            *
        from _nodes
        """
    )

    # A parse-only manifest contributes the identity; whichever manifest carries
    # the compiled SQL fills it in. Hence coalesce rather than overwrite.
    con.execute(
        """
        insert into node_version
        select distinct on (node_version_id)
               node_version_id, unique_id, resource_type, name, package_name,
               path, database, schema_name, alias, materialized, checksum,
               description, tags, compiled_code
        from _nv
        order by node_version_id, compiled_code is null
        on conflict (node_version_id) do update
            set compiled_code = coalesce(node_version.compiled_code, excluded.compiled_code)
        """
    )

    con.execute(
        """
        insert or replace into node_invocation
        select ?, unique_id, node_version_id from _nv
        """,
        [invocation_id],
    )

    # parent_map is the lineage edge list; recursive CTEs run over this table.
    con.execute(
        """
        insert or replace into node_depends_on
        select distinct ? as invocation_id, k as unique_id, unnest(parents) as depends_on_id
        from (
            select k, from_json(parent_map -> k, '["VARCHAR"]') as parents
            from _m, unnest(json_keys(parent_map)) as t(k)
        )
        where parents is not null
        """,
        [invocation_id],
    )

    return con.execute(
        "select count(*) from node_invocation where invocation_id = ?", [invocation_id]
    ).fetchone()[0]


def ingest_run_results(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    invocation_id: str,
    project_dir: Path | None = None,
) -> int:
    con.execute(
        f"""
        create or replace temp view _r as
        select * from read_json({_lit(path)}, columns={{
            'metadata':'JSON', 'args':'JSON', 'elapsed_time':'DOUBLE', 'results':'JSON'
        }})
        """
    )

    meta = con.execute(
        """
        select
            metadata->>'dbt_version',
            try_cast(replace(metadata->>'generated_at','Z','') as timestamp),
            try_cast(replace(metadata->>'invocation_started_at','Z','') as timestamp),
            elapsed_time,
            args->>'which',
            args->>'invocation_command',
            args->>'target',
            try_cast(args->>'full_refresh' as boolean),
            args->>'profiles_dir'
        from _r
        """
    ).fetchone()

    target = meta[6]
    if target is None and project_dir is not None:
        from .config import resolve_target

        target = resolve_target(project_dir, meta[8])

    _upsert_invocation(
        con,
        {
            "invocation_id": invocation_id,
            "dbt_version": meta[0],
            "generated_at": meta[1],
            "invocation_started_at": meta[2],
            "elapsed_time": meta[3],
            "command": meta[4],
            "invocation_command": meta[5],
            "target": target,
            "full_refresh": meta[7],
        },
    )

    con.execute(
        """
        insert or replace into run_result
        select
            ?                                       as invocation_id,
            res ->> 'unique_id',
            res ->> 'status',
            try_cast(res ->> 'execution_time' as double),
            try_cast(res -> 'adapter_response' ->> 'rows_affected' as bigint),
            res -> 'adapter_response' ->> 'query_id',
            res -> 'adapter_response' ->> '_message',
            try_cast(res ->> 'failures' as bigint),
            res ->> 'thread_id',
            res ->> 'message',
            (select try_cast(replace(t ->> 'started_at','Z','') as timestamp)
               from unnest(from_json(res -> 'timing', '["JSON"]')) as u(t)
              where t ->> 'name' = 'compile' limit 1),
            (select try_cast(replace(t ->> 'completed_at','Z','') as timestamp)
               from unnest(from_json(res -> 'timing', '["JSON"]')) as u(t)
              where t ->> 'name' = 'execute' limit 1)
        from _r, unnest(from_json(results, '["JSON"]')) as x(res)
        """,
        [invocation_id],
    )

    return con.execute(
        "select count(*) from run_result where invocation_id = ?", [invocation_id]
    ).fetchone()[0]


def ingest_sources(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    invocation_id: str,
    project_dir: Path | None = None,
) -> int:
    con.execute(
        f"""
        create or replace temp view _s as
        select * from read_json({_lit(path)}, columns={{'metadata':'JSON','results':'JSON'}})
        """
    )
    con.execute(
        """
        insert or replace into source_freshness
        select
            ? as invocation_id,
            res ->> 'unique_id',
            res ->> 'status',
            try_cast(replace(res -> 'max_loaded_at' ->> 0,'Z','') as timestamp),
            try_cast(res ->> 'age' as double)
        from _s, unnest(from_json(results, '["JSON"]')) as x(res)
        """,
        [invocation_id],
    )
    return con.execute(
        "select count(*) from source_freshness where invocation_id = ?", [invocation_id]
    ).fetchone()[0]


INGESTORS = {
    "manifest": ingest_manifest,
    "run_results": ingest_run_results,
    "sources": ingest_sources,
}
