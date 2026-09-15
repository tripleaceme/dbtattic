"""Archive artifacts before dbt destroys them, then normalise into DuckDB.

Two-stage by design: the copy is a filesystem operation measured in
milliseconds, so it can sit in front of every dbt invocation without being
felt. The parse into DuckDB is the slow half and is separable, which is what
lets a pre-run hook stay effectively free.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import extract, store
from .config import Config


@dataclass
class CaptureResult:
    artifact: str
    invocation_id: str | None
    status: str  # captured | skipped-duplicate | absent | error
    rows: int = 0
    detail: str = ""


def _git(project_dir: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def rebuild(cfg: Config) -> list[CaptureResult]:
    """Re-derive the DuckDB store from the archived JSON.

    The archive is the source of truth; DuckDB is a cache built from it. That
    makes a schema change or a fixed extraction bug a rebuild rather than a
    migration, and it means nothing is ever lost to a bad parse.
    """
    results: list[CaptureResult] = []
    if not cfg.archive_dir.exists():
        return results

    if cfg.db_path.exists():
        cfg.db_path.unlink()

    con = store.connect(cfg.db_path)
    try:
        # manifests first: run_results reference nodes the manifest defines.
        order = {"manifest.json": 0, "run_results.json": 1, "sources.json": 2, "catalog.json": 3}
        files = sorted(
            (p for p in cfg.archive_dir.glob("*/*.json") if p.name in order),
            key=lambda p: (order[p.name], p.stat().st_mtime),
        )
        by_name = {v: k for k, v in extract.FILENAMES.items()}

        for path in files:
            artifact = by_name[path.name]
            inv = path.parent.name
            try:
                ingest = extract.INGESTORS.get(artifact)
                rows = ingest(con, path, inv, cfg.project_dir) if ingest else 0
                con.execute(
                    "insert or replace into artifact_capture values (?, ?, ?, ?, ?, ?)",
                    [
                        inv,
                        artifact,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                        "rebuild",
                        str(path.relative_to(cfg.store_dir)),
                        path.stat().st_size,
                    ],
                )
                results.append(CaptureResult(artifact, inv, "captured", rows=rows))
            except Exception as exc:
                results.append(CaptureResult(artifact, inv, "error", detail=str(exc)[:200]))
    finally:
        con.close()
    return results


def capture(cfg: Config, phase: str = "manual") -> list[CaptureResult]:
    """Capture every configured artifact currently sitting in target/.

    Each artifact is keyed by the invocation_id in its own metadata block, so a
    target/ folder holding a manifest from one invocation and run_results from
    another is recorded correctly as two separate invocations.
    """
    results: list[CaptureResult] = []
    con = store.connect(cfg.db_path)
    sha = _git(cfg.project_dir, "rev-parse", "HEAD")
    branch = _git(cfg.project_dir, "rev-parse", "--abbrev-ref", "HEAD")

    try:
        for artifact in cfg.artifacts:
            fname = extract.FILENAMES.get(artifact)
            if not fname:
                results.append(CaptureResult(artifact, None, "error", detail="unknown artifact"))
                continue

            src = cfg.target_path / fname
            if not src.exists():
                results.append(CaptureResult(artifact, None, "absent"))
                continue

            inv = extract.peek_invocation_id(src)
            if not inv:
                results.append(
                    CaptureResult(artifact, None, "error", detail="no invocation_id in metadata")
                )
                continue

            if store.already_captured(con, inv, artifact):
                results.append(CaptureResult(artifact, inv, "skipped-duplicate"))
                continue

            try:
                dest_dir = cfg.archive_dir / inv
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / fname
                shutil.copy2(src, dest)  # stage 1: cheap

                ingest = extract.INGESTORS.get(artifact)
                rows = ingest(con, dest, inv, cfg.project_dir) if ingest else 0  # stage 2: parse

                con.execute(
                    """
                    insert or replace into artifact_capture
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        inv,
                        artifact,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                        phase,
                        str(dest.relative_to(cfg.store_dir)),
                        dest.stat().st_size,
                    ],
                )
                if sha or branch:
                    con.execute(
                        """
                        update invocation
                           set git_sha = coalesce(git_sha, ?), git_branch = coalesce(git_branch, ?)
                         where invocation_id = ?
                        """,
                        [sha, branch, inv],
                    )
                results.append(CaptureResult(artifact, inv, "captured", rows=rows))
            except Exception as exc:  # capture must never break the dbt run
                results.append(CaptureResult(artifact, inv, "error", detail=str(exc)[:200]))
    finally:
        con.close()

    return results
