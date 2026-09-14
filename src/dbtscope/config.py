"""Configuration resolution.

Precedence, highest first:
    1. --store CLI flag
    2. DBTSCOPE_STORE env var
    3. dbtscope.yml in the project root
    4. vars.dbtscope in dbt_project.yml
    5. ./.dbtscope

The VS Code extension runs this same resolution against the workspace root, so
the store never has to be configured in two places.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_STORE = ".dbtscope"
DEFAULT_ARTIFACTS = ["manifest", "run_results", "sources", "catalog"]


@dataclass
class Config:
    project_dir: Path
    store: str
    target_path: Path
    log_path: Path
    artifacts: list[str] = field(default_factory=lambda: list(DEFAULT_ARTIFACTS))
    when: str = "both"  # before-run | after-run | both
    source: str = "default"  # where the store setting came from, for `dbtscope info`

    @property
    def is_local(self) -> bool:
        return "://" not in self.store

    @property
    def store_dir(self) -> Path:
        """Local archive + DuckDB directory. Remote backends land in 0.2."""
        p = Path(self.store)
        return p if p.is_absolute() else self.project_dir / p

    @property
    def db_path(self) -> Path:
        return self.store_dir / "store.duckdb"

    @property
    def archive_dir(self) -> Path:
        return self.store_dir / "archive"


def find_project_dir(start: Path | None = None) -> Path:
    """Walk up looking for dbt_project.yml, like dbt itself does."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "dbt_project.yml").exists():
            return candidate
    raise FileNotFoundError(
        "No dbt_project.yml found in this directory or any parent. "
        "Run dbtscope from inside a dbt project, or pass --project-dir."
    )


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def resolve_target(project_dir: Path, profiles_dir: str | None = None) -> str | None:
    """Work out which dbt target a run actually used.

    dbt only records `args.target` when --target was passed explicitly; a run
    using the profile's default target records None. Since target is how we
    separate prod artifacts from dev ones -- and deferral depends on that --
    replicate dbt's own resolution: --target > DBT_TARGET > the profile's
    `target:` key.
    """
    if os.environ.get("DBT_TARGET"):
        return os.environ["DBT_TARGET"]

    profile_name = _read_yaml(project_dir / "dbt_project.yml").get("profile")
    if not profile_name:
        return None

    candidates = [
        Path(profiles_dir) if profiles_dir else None,
        Path(os.environ["DBT_PROFILES_DIR"]) if os.environ.get("DBT_PROFILES_DIR") else None,
        project_dir,
        Path.home() / ".dbt",
    ]
    for d in candidates:
        if d and (d / "profiles.yml").exists():
            profile = _read_yaml(d / "profiles.yml").get(profile_name) or {}
            if profile.get("target"):
                return profile["target"]
    return None


def load(store: str | None = None, project_dir: Path | None = None) -> Config:
    proj = project_dir.resolve() if project_dir else find_project_dir()

    scope_yml = _read_yaml(proj / "dbtscope.yml")
    dbt_yml = _read_yaml(proj / "dbt_project.yml")
    from_vars = (dbt_yml.get("vars") or {}).get("dbtscope") or {}

    # dbt lets the project override where artifacts land; honour it.
    target_path = Path(os.environ.get("DBT_TARGET_PATH") or dbt_yml.get("target-path") or "target")
    log_path = Path(os.environ.get("DBT_LOG_PATH") or dbt_yml.get("log-path") or "logs")

    if store:
        resolved, src = store, "--store flag"
    elif os.environ.get("DBTSCOPE_STORE"):
        resolved, src = os.environ["DBTSCOPE_STORE"], "DBTSCOPE_STORE env var"
    elif scope_yml.get("store"):
        resolved, src = scope_yml["store"], "dbtscope.yml"
    elif from_vars.get("store"):
        resolved, src = from_vars["store"], "dbt_project.yml vars"
    else:
        resolved, src = DEFAULT_STORE, "default"

    capture = scope_yml.get("capture") or from_vars.get("capture") or {}

    return Config(
        project_dir=proj,
        store=resolved,
        target_path=target_path if target_path.is_absolute() else proj / target_path,
        log_path=log_path if log_path.is_absolute() else proj / log_path,
        artifacts=capture.get("artifacts") or list(DEFAULT_ARTIFACTS),
        when=capture.get("when", "both"),
        source=src,
    )
