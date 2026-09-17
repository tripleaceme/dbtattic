"""dbtattic command line interface."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import capture as capture_mod
from . import config as config_mod
from . import store

BANNER = r"""
     _ _     _        _   _   _
  __| | |__ | |_ __ _| |_| |_(_) ___
 / _` | '_ \| __/ _` | __| __| |/ __|
| (_| | |_) | || (_| | |_| |_| | (__
 \__,_|_.__/ \__\__,_|\__|\__|_|\___|
"""

EPILOG = """[bold]Typical first run[/bold]

  dbtattic run -- dbt build     run dbt, keeping the artifacts either side
  dbtattic history              what has run, what failed, how long
  dbtattic info                 where the archive is and what is in it

[bold]Slim CI on any warehouse[/bold]

  dbt build --select state:modified+ --defer \\
            --state $(dbtattic state --last-success)

[bold]Every command takes --help[/bold], e.g. [cyan]dbtattic state --help[/cyan]

The archive is JSON on disk plus one DuckDB file, by default at [cyan]./.dbtattic[/cyan]
(set [cyan]store:[/cyan] in [cyan]dbtattic.yml[/cyan], or [cyan]$DBTATTIC_STORE[/cyan]).
"""

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=(
        "[bold]Preserve and query dbt artifact history, on any warehouse.[/bold]\n\n"
        "dbt overwrites [cyan]target/[/cyan] on every invocation, and a bare "
        "[cyan]dbt parse[/cyan] is enough to destroy the manifest from your last real "
        "build. This keeps those artifacts and makes them queryable in DuckDB. "
        "No warehouse compute, no dbt Cloud, every adapter."
    ),
    epilog=EPILOG,
)
console = Console()
err = Console(stderr=True)

StoreOpt = typer.Option(None, "--store", help="Override the configured store location.")
ProjOpt = typer.Option(None, "--project-dir", help="dbt project root (default: search upwards).")


def _cfg(store_: str | None, project_dir: Path | None) -> config_mod.Config:
    try:
        return config_mod.load(store=store_, project_dir=project_dir)
    except FileNotFoundError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)


def _jsonable(v):
    """DuckDB hands back datetimes, Decimals and lists; JSON needs plain values."""
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ")
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _open(cfg: config_mod.Config, read_only: bool = True):
    """Open the store, turning a stale schema into an actionable message."""
    if not cfg.db_path.exists():
        err.print(f"[red]No store at {cfg.db_path}. Run `dbtattic capture` first.[/red]")
        raise typer.Exit(1)
    try:
        return store.connect(cfg.db_path, read_only=read_only)
    except store.SchemaOutOfDate as exc:
        err.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(3)


def _report(results: list[capture_mod.CaptureResult], quiet: bool) -> None:
    if quiet:
        bad = [r for r in results if r.status == "error"]
        for r in bad:
            err.print(f"[yellow]dbtattic: {r.artifact}: {r.detail}[/yellow]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("artifact")
    table.add_column("invocation_id")
    table.add_column("status")
    table.add_column("rows", justify="right")
    for r in results:
        colour = {"captured": "green", "skipped-duplicate": "dim", "absent": "dim", "error": "red"}
        table.add_row(
            r.artifact,
            (r.invocation_id or "-")[:8],
            f"[{colour[r.status]}]{r.status}[/{colour[r.status]}]",
            str(r.rows or ""),
        )
    console.print(table)


@app.command(epilog="""[bold]Examples[/bold]

  dbtattic capture                      keep whatever is in target/ right now
  dbtattic capture --phase pre          label it as taken before a run
  dbtattic capture --quiet              only speak up on error, for shell hooks
""")
def capture(
    store_: str = StoreOpt,
    project_dir: Path = ProjOpt,
    phase: str = typer.Option("manual", "--phase", help="pre | post | manual"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Archive the artifacts in target/ before dbt overwrites them.

    Safe to run repeatedly. Each artifact is keyed by the invocation_id in its
    own metadata, so re-running captures nothing twice -- and a target/ holding
    a manifest from one invocation beside run results from another is recorded
    correctly as two invocations rather than one corrupt row.
    """
    cfg = _cfg(store_, project_dir)
    results = capture_mod.capture(cfg, phase=phase)
    _report(results, quiet)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    epilog="""[bold]Examples[/bold]

  dbtattic run -- dbt build
  dbtattic run -- dbt build --target prod --select marts
  dbtattic run -- dbt test
""",
)
def run(ctx: typer.Context) -> None:
    """Run dbt with a capture either side. Your command, unchanged.

    Captures before the run, because dbt overwrites manifest.json during
    parsing, and again afterwards for the results. Exit code, stdout and stderr
    pass straight through, and a capture failure is a warning -- never a broken
    dbt run.

    To capture in front of every dbt command without retyping anything, add a
    shell function instead: dbt() { dbtattic capture --quiet --phase pre;
    command dbt "$@"; }
    """
    argv = list(ctx.args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        err.print("[red]Nothing to run. Usage: dbtattic run -- dbt build[/red]")
        raise typer.Exit(2)

    cfg = _cfg(None, None)

    # Pre-capture: the previous invocation's artifacts are about to be overwritten.
    if cfg.when in ("before-run", "both"):
        try:
            _report(capture_mod.capture(cfg, phase="pre"), quiet=True)
        except Exception as exc:
            err.print(f"[yellow]dbtattic: pre-capture skipped: {exc}[/yellow]")

    proc = subprocess.run(argv, cwd=cfg.project_dir)

    if cfg.when in ("after-run", "both"):
        try:
            _report(capture_mod.capture(cfg, phase="post"), quiet=True)
        except Exception as exc:
            err.print(f"[yellow]dbtattic: post-capture skipped: {exc}[/yellow]")

    raise typer.Exit(proc.returncode)


@app.command(epilog="""[bold]Examples[/bold]

  dbtattic history                      the last 20 invocations
  dbtattic history --limit 100

Runs showing 0 nodes are parse, compile or ls: they executed nothing but
still overwrote your manifest, which is why they are worth keeping.
""")
def history(store_: str = StoreOpt, project_dir: Path = ProjOpt, limit: int = 20) -> None:
    """Recent invocations: what ran, what failed, how long it took."""
    cfg = _cfg(store_, project_dir)
    con = _open(cfg)
    con.sql(f"select * from v_run_history limit {int(limit)}").show(max_rows=limit)
    con.close()


@app.command(epilog="""[bold]Examples[/bold]

  dbt build --select state:modified+ --defer \\
            --state $(dbtattic state --last-success)

  dbtattic state --target prod          the last prod manifest
  dbtattic state --invocation 0c503aa2  one specific run
""")
def state(
    store_: str = StoreOpt,
    project_dir: Path = ProjOpt,
    last_success: bool = typer.Option(
        False, "--last-success", help="Most recent invocation with no failed nodes."
    ),
    invocation: str = typer.Option(None, "--invocation", help="Restore a specific invocation_id."),
    target: str = typer.Option(None, "--target", help="Only consider this dbt target."),
    out: Path = typer.Option(None, "--out", help="Directory to restore into."),
) -> None:
    """Restore a past manifest, for --state and --defer on any warehouse.

    Prints the directory it restored into, so it can be used inline. This is
    what gives you state:modified and deferral without dbt Cloud or Snowflake
    dbt Projects -- the manifest dbt wants has simply been kept.

    Only the path goes to stdout, so $(...) stays clean.
    """
    cfg = _cfg(store_, project_dir)
    con = _open(cfg)

    where = ["ac.artifact_type = 'manifest'"]
    params: list = []
    if invocation:
        where.append("i.invocation_id = ?")
        params.append(invocation)
    if target:
        where.append("i.target = ?")
        params.append(target)
    if last_success:
        where.append(
            "not exists (select 1 from run_result r "
            "where r.invocation_id = i.invocation_id and r.status in ('error','fail'))"
        )
        where.append("exists (select 1 from run_result r where r.invocation_id = i.invocation_id)")

    row = con.execute(
        f"""
        select i.invocation_id, ac.archive_path, i.generated_at
        from invocation i
        join artifact_capture ac using (invocation_id)
        where {" and ".join(where)}
        order by i.generated_at desc
        limit 1
        """,
        params,
    ).fetchone()
    con.close()

    if not row:
        err.print("[red]No matching manifest in the store.[/red]")
        raise typer.Exit(1)

    inv, archive_path, generated_at = row
    src = cfg.store_dir / archive_path
    dest_dir = out or (cfg.store_dir / "state" / inv)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / "manifest.json")

    err.print(f"[dim]restored manifest from {inv[:8]} ({generated_at})[/dim]")
    print(dest_dir)  # stdout stays clean so it can be used in $( )


@app.command(epilog="""[bold]Examples[/bold]

  dbtattic query "select * from v_node_runtime where name = 'fct_orders'"
  dbtattic query "select * from v_test_history where failures > 0"
  dbtattic query --json "select * from v_run_history"     for tooling

[bold]Tables[/bold]  invocation, node, run_result, node_depends_on, node_version
[bold]Views[/bold]   v_run_history, v_node_runtime, v_test_history, v_node_changes
""")
def query(
    sql: str = typer.Argument(None, help="SQL to run. Omitted: reads from stdin."),
    store_: str = StoreOpt,
    project_dir: Path = ProjOpt,
    json_out: bool = typer.Option(False, "--json", help="Emit JSON (for the VS Code extension)."),
    limit: int = typer.Option(50, "--limit", help="Max rows to render."),
) -> None:
    """Escape hatch: run SQL directly against the archive.

    The store is plain DuckDB, so anything DuckDB can do it can do -- including
    recursive CTEs over node_depends_on for lineage and blast radius.
    """
    cfg = _cfg(store_, project_dir)
    con = _open(cfg)
    try:
        if not sql:
            sql = sys.stdin.read()
        rel = con.sql(sql)
        if json_out:
            rows = rel.fetchmany(limit)
            cols = rel.columns
            print(
                json.dumps(
                    {
                        "columns": cols,
                        "rows": [[_jsonable(v) for v in r] for r in rows],
                    },
                    default=str,
                )
            )
        else:
            rel.show(max_rows=limit)
    except Exception as exc:
        if json_out:
            print(json.dumps({"error": str(exc)}))
            raise typer.Exit(1)
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        con.close()


@app.command(epilog="""[bold]Examples[/bold]

  dbtattic rebuild                      re-derive after an upgrade
""")
def rebuild(store_: str = StoreOpt, project_dir: Path = ProjOpt) -> None:
    """Re-derive the DuckDB store from the archived JSON.

    The archived JSON is the source of truth and DuckDB is a cache built from
    it, so an upgrade or a fixed extraction bug is a rebuild rather than a
    migration. Nothing captured is ever lost to a bad parse.
    """
    cfg = _cfg(store_, project_dir)
    if not cfg.archive_dir.exists():
        err.print(f"[red]No archive at {cfg.archive_dir}. Nothing to rebuild from.[/red]")
        raise typer.Exit(1)
    results = capture_mod.rebuild(cfg)
    ok = sum(1 for r in results if r.status == "captured")
    bad = [r for r in results if r.status == "error"]
    console.print(f"[green]rebuilt {ok} artifacts[/green]")
    for r in bad:
        err.print(f"[red]{r.artifact} {r.invocation_id[:8]}: {r.detail}[/red]")


@app.command(epilog="""[bold]Examples[/bold]

  dbtattic info                         resolved config and store contents
  dbtattic info --json                  the same, for tooling

Store location resolves in this order, first wins:
  --store  >  $DBTATTIC_STORE  >  dbtattic.yml  >  vars.dbtattic  >  ./.dbtattic
""")
def info(
    store_: str = StoreOpt,
    project_dir: Path = ProjOpt,
    json_out: bool = typer.Option(False, "--json", help="Emit JSON (for the VS Code extension)."),
) -> None:
    """Where the archive lives, what is in it, and which setting chose it."""
    cfg = _cfg(store_, project_dir)

    if json_out:
        payload = {
            "project_dir": str(cfg.project_dir),
            "store": cfg.store,
            "store_source": cfg.source,
            "db_path": str(cfg.db_path),
            "archive_dir": str(cfg.archive_dir),
            "target_path": str(cfg.target_path),
            "capture_when": cfg.when,
            "artifacts": cfg.artifacts,
            "exists": cfg.db_path.exists(),
        }
        if cfg.db_path.exists():
            try:
                con = store.connect(cfg.db_path, read_only=True)
                n_inv, n_art, n_nv, n_ni = con.execute(
                    """select (select count(*) from invocation),
                              (select count(*) from artifact_capture),
                              (select count(*) from node_version),
                              (select count(*) from node_invocation)"""
                ).fetchone()
                con.close()
                payload.update(
                    invocations=n_inv, artifacts_captured=n_art,
                    node_versions=n_nv, node_rows=n_ni,
                )
            except store.SchemaOutOfDate as exc:
                payload.update(stale=True, message=str(exc))
        print(json.dumps(payload))
        return

    console.print(f"[bold]project[/bold]      {cfg.project_dir}")
    console.print(f"[bold]store[/bold]        {cfg.store}   [dim](from {cfg.source})[/dim]")
    console.print(f"[bold]db[/bold]           {cfg.db_path}")
    console.print(f"[bold]target path[/bold]  {cfg.target_path}")
    console.print(f"[bold]capture[/bold]      {cfg.when} :: {', '.join(cfg.artifacts)}")
    if cfg.db_path.exists():
        try:
            con = store.connect(cfg.db_path, read_only=True)
        except store.SchemaOutOfDate as exc:
            console.print(f"[yellow]contents     {exc}[/yellow]")
            return
        n_inv, n_art, n_nv, n_ni = con.execute(
            """select (select count(*) from invocation),
                      (select count(*) from artifact_capture),
                      (select count(*) from node_version),
                      (select count(*) from node_invocation)"""
        ).fetchone()
        con.close()
        console.print(f"[bold]contents[/bold]     {n_inv} invocations, {n_art} artifacts")
        ratio = f"{n_ni / n_nv:.1f}x" if n_nv else "-"
        console.print(f"[bold]nodes[/bold]        {n_nv} versions backing {n_ni} rows ({ratio} dedup)")
    else:
        console.print("[dim]store not created yet[/dim]")


def main() -> None:
    """Console-script entry point.

    The banner is printed here rather than from a Typer callback because
    `--help` short-circuits inside Click before any callback runs, and rich
    would reflow the ASCII art if it were part of the help string.
    """
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        console.print(f"[#ff6b35]{BANNER}[/#ff6b35]", highlight=False)
    app()


if __name__ == "__main__":
    main()
