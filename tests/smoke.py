#!/usr/bin/env python
"""End-to-end smoke test against the jaffle_duck fixture.

Deliberately a plain script rather than a pytest suite: it runs real dbt
invocations and asserts on the store that results, which is the behaviour worth
protecting. Run it directly, or from CI.

    python tests/smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "jaffle_duck"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        FAILURES.append(label)


def sh(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DBT_PROFILES_DIR": str(FIXTURE)}
    proc = subprocess.run(args, cwd=FIXTURE, capture_output=True, text=True, env=env)
    if check_rc and proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(args)}")
    return proc


def scope_json(*args: str) -> dict:
    return json.loads(sh("dbtscope", *args).stdout)


def query(sql: str) -> list[list]:
    return scope_json("query", "--json", sql)["rows"]


def main() -> int:
    for stale in (".dbtscope", "target", "logs", "warehouse.duckdb"):
        p = FIXTURE / stale
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)

    print("1. capture around a real dbt build")
    sh("dbtscope", "run", "--", "dbt", "build")
    info = scope_json("info", "--json")
    check("store created", info["exists"])
    check("invocation recorded", info.get("invocations", 0) >= 1, f"{info.get('invocations')} invocations")
    check("nodes captured", info.get("node_rows", 0) == 20, f"{info.get('node_rows')} node rows")
    check("run results captured", len(query("select 1 from run_result")) == 20)

    print("2. capture is idempotent on invocation_id")
    before = scope_json("info", "--json")["artifacts_captured"]
    sh("dbtscope", "capture", "--quiet")
    after = scope_json("info", "--json")["artifacts_captured"]
    check("re-capture adds nothing", before == after, f"{before} -> {after}")

    print("3. a bare `dbt parse` leaves target/ holding two invocations")
    manifest_before = json.loads((FIXTURE / "target" / "manifest.json").read_text())
    sh("dbt", "parse")
    manifest_after = json.loads((FIXTURE / "target" / "manifest.json").read_text())
    results_after = json.loads((FIXTURE / "target" / "run_results.json").read_text())
    check(
        "parse overwrote the manifest",
        manifest_before["metadata"]["invocation_id"] != manifest_after["metadata"]["invocation_id"],
    )
    check(
        "target/ now holds a mismatched pair",
        manifest_after["metadata"]["invocation_id"] != results_after["metadata"]["invocation_id"],
    )
    sh("dbtscope", "capture", "--quiet")
    check(
        "parse recorded as its own invocation with no run results",
        len(query("select 1 from v_run_history where nodes_run = 0")) >= 1,
    )

    print("4. node de-duplication")
    sh("dbtscope", "run", "--", "dbt", "build")
    sh("dbtscope", "run", "--", "dbt", "build")
    info = scope_json("info", "--json")
    versions, rows = info["node_versions"], info["node_rows"]
    check("rows grew with invocations", rows > 20, f"{rows} rows")
    check(
        "versions did not grow (nothing changed)",
        versions == 20,
        f"{versions} versions backing {rows} rows ({rows / versions:.1f}x)",
    )
    check("no version lost its compiled SQL", not query(
        "select 1 from node_version where compiled_code is null and resource_type not in ('seed','source')"
    ))

    print("5. editing a model creates exactly one new version")
    model = FIXTURE / "models" / "staging" / "stg_customers.sql"
    original = model.read_text()
    try:
        # A trailing comment changes raw_code -- and so the content hash -- while
        # keeping the model valid and its results identical.
        model.write_text(original.rstrip() + "\n-- dbtscope smoke probe\n")
        sh("dbtscope", "run", "--", "dbt", "build")
        changed = query(
            "select count(distinct node_version_id) from node "
            "where unique_id = 'model.jaffle_duck.stg_customers'"
        )
        check("edited model has 2 versions", changed[0][0] == 2, f"{changed[0][0]} versions")
        untouched = query(
            "select count(distinct node_version_id) from node "
            "where unique_id = 'model.jaffle_duck.stg_payments'"
        )
        check("untouched model still has 1", untouched[0][0] == 1, f"{untouched[0][0]} versions")
    finally:
        model.write_text(original)

    print("6. state restore drives dbt state comparison")
    state_dir = sh("dbtscope", "state", "--last-success").stdout.strip()
    check("manifest restored", (Path(state_dir) / "manifest.json").exists(), state_dir)
    sh("dbt", "build")  # bring the warehouse back in line with the restored model
    out = sh("dbt", "build", "--select", "state:modified+", "--state", state_dir, check_rc=False)
    check(
        "dbt accepted the restored manifest",
        "Nothing to do" in out.stdout or "Done." in out.stdout,
        out.stdout.strip().splitlines()[-1][:80] if out.stdout.strip() else "",
    )

    print("7. rebuild re-derives the store from the archive")
    before = scope_json("info", "--json")
    sh("dbtscope", "rebuild")
    after = scope_json("info", "--json")
    check(
        "rebuild preserves invocations",
        before["invocations"] == after["invocations"],
        f"{before['invocations']} -> {after['invocations']}",
    )
    check(
        "rebuild preserves de-duplication",
        before["node_versions"] == after["node_versions"],
        f"{before['node_versions']} -> {after['node_versions']}",
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
