"""Workbench directories and git provenance."""

import os
import subprocess
from functools import cache
from pathlib import Path


@cache
def root() -> Path:
    """The workbench checkout: ``$SRW_ROOT``, else the nearest parent holding workbench.toml."""
    if env := os.environ.get("SRW_ROOT"):
        return Path(env).resolve()
    for directory in (Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents):
        if (directory / "workbench.toml").is_file():
            return directory
    raise FileNotFoundError("No workbench.toml found; run inside the workbench or set SRW_ROOT")


def versions_dir() -> Path:
    return root() / "versions"


def shoulders_dir() -> Path:
    return root() / "shoulders"


def slicer_dir() -> Path:
    return root() / "slicer"


def builds_dir() -> Path:
    return root() / "builds"


def library_dir() -> Path:
    return root() / "ShoulderRest"


def git_state(path: Path) -> dict[str, str | bool]:
    """Commit and whether tracked files differ from it, for recording what produced a build."""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True,
        ).stdout.strip()
    try:
        return {
            "commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": True}
