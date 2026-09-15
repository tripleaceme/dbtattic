# dbtattic for VS Code

Browse and query your dbt artifact history without leaving the editor.

dbt overwrites `target/` on every invocation — `manifest.json` during parsing, `run_results.json`
at the end of a run. The [dbtattic CLI](https://pypi.org/project/dbtattic/) preserves those
artifacts and normalises them into DuckDB. This extension is the window onto that store.

## Requires the CLI

```bash
pip install dbtattic
```

If it is not on your `PATH` — a project virtualenv, say — point the extension at it:

```json
{ "dbtattic.cliPath": "${workspaceFolder}/.venv/bin/dbtattic" }
```

The extension shells out to the CLI rather than embedding DuckDB. That keeps it free of native
dependencies and means the schema has exactly one definition rather than two that can drift.

## What you get

**Invocations** — every captured run, newest first, expanding to the nodes that failed or ran
slowest. Runs that executed nothing (`dbt parse`, `compile`, `ls`) are shown too, because those
are precisely the commands that silently destroy the previous manifest.

**Insights** — the questions a single run cannot answer:

| | |
|---|---|
| Runtime regressions | nodes whose worst run is well off their own median |
| Flaky and skipped tests | failure rate across runs; a test that is always skipped is its own signal |
| Recently changed models | content hash changed between runs — the incident-forensics view |
| Invocations that destroyed a manifest | parse/compile/ls runs |
| Store contents | what is captured, and the node de-duplication ratio |

**Query panel** — editable SQL over the store, ⌘/Ctrl + Enter to run. Every table and view is
available: `invocation`, `node`, `run_result`, `node_depends_on`, `v_run_history`,
`v_node_runtime`, `v_test_history`, `v_node_changes`.

**Status bar** — captured invocation count, the resolved store location and where that setting
came from. Click to open a query.

## Commands

| Command | Does |
|---|---|
| `dbtattic: New Query` | open the query panel |
| `dbtattic: Capture Artifacts Now` | capture whatever is in `target/` right now |
| `dbtattic: Rebuild Store from Archive` | re-derive the store from the archived JSON |
| `dbtattic: Copy --state Path (Last Success)` | clipboard path for `dbt build --defer --state …` |
| `dbtattic: Refresh` | reload the views |

## Settings

| Setting | Default | |
|---|---|---|
| `dbtattic.cliPath` | `dbtattic` | path to the executable |
| `dbtattic.projectDir` | *(auto)* | dbt project root; empty searches for `dbt_project.yml` |
| `dbtattic.historyLimit` | `25` | invocations listed in the tree |

The store location itself is **not** configured here. It comes from `dbtattic.yml` in the project,
and the extension runs the same resolution the CLI does, so it is never set in two places.
