"""Machine-local settings from workbench.toml."""

import tomllib
from functools import cache
from pathlib import Path
from typing import Any

from . import paths


@cache
def settings() -> dict[str, Any]:
    return tomllib.loads((paths.root() / "workbench.toml").read_text())


def slicer_command() -> list[str]:
    return list(settings()["slicer"]["command"])


def slicer_gui_command() -> list[str]:
    return list(settings()["slicer"]["gui"])


def slicer_profiles_dir() -> Path:
    return Path(settings()["slicer"]["profiles"]).expanduser()


def default_profile(part: str) -> str:
    profiles: dict[str, str] = settings()["slicer"]["parts"]
    if part not in profiles:
        raise ValueError(f"No default slicer profile for {part!r} in workbench.toml [slicer.parts]")
    return profiles[part]
