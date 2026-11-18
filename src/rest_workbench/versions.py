"""Rest versions: one TOML file per version under versions/, each inheriting from a parent.

A version file holds:

- ``summary``, ``created``, ``parent``, and optionally ``verdict`` and ``notes``.
- Design tables (``shoulder``, ``violin``, ``rest``, ``legs``; see ``design``),
  holding only what differs from the parent. Tables merge recursively;
  other values replace. ``false`` turns an optional group (a cap, stripes,
  rod retention, the engraving) off.
- ``[placement]``: an absolute violin placement on the shoulder, required on
  a root version. Otherwise the parent's placement is inherited.
- ``[adjust]``: a move of this version's rest in its own frame, applied after
  the inherited or absolute placement (see ``placement``).
- ``[[prints]]``: physical prints of this version, with feedback and scores.
"""

import datetime
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import tomlkit
from build123d import Location

from . import paths, shoulders
from .config import Table, deep_merge
from .design import DESIGN_KEYS, Design
from .placement import Adjustment, absolute_violin_location, adjust, rest_pose

ID_PATTERN = re.compile(r"^v(\d+)$")
METADATA_KEYS = ("summary", "created", "parent", "verdict", "notes", "tags")
PLACEMENT_KEYS = ("placement", "adjust")
RECORD_KEYS = ("prints",)


@dataclass
class Version:
    id: str
    path: Path
    data: Table

    @property
    def parent(self) -> str | None:
        parent = self.data.get("parent")
        return str(parent) if parent else None

    @property
    def summary(self) -> str:
        return str(self.data.get("summary", ""))

    @property
    def prints(self) -> list[Table]:
        return list(self.data.get("prints", []))

    def own_design(self) -> Table:
        return {key: self.data[key] for key in DESIGN_KEYS if key in self.data}


def _check(version_id: str, data: Mapping[str, Any]) -> None:
    allowed = {*METADATA_KEYS, *DESIGN_KEYS, *PLACEMENT_KEYS, *RECORD_KEYS}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{version_id}: unknown top-level keys {sorted(unknown)}")


def load(version_id: str) -> Version:
    path = paths.versions_dir() / f"{version_id}.toml"
    if not path.is_file():
        raise ValueError(f"No version {version_id!r} ({path})")
    data = tomllib.loads(path.read_text())
    _check(version_id, data)
    return Version(version_id, path, data)


def all_versions() -> list[Version]:
    files = sorted(paths.versions_dir().glob("v*.toml"), key=lambda p: _number(p.stem))
    return [load(path.stem) for path in files if ID_PATTERN.match(path.stem)]


def _number(version_id: str) -> int:
    match = ID_PATTERN.match(version_id)
    return int(match.group(1)) if match else -1


def next_id() -> str:
    numbers = [_number(path.stem) for path in paths.versions_dir().glob("v*.toml")]
    return f"v{max(numbers, default=0) + 1:03d}"


@dataclass
class Resolved:
    """A version with its inherited design and placement worked out."""

    version: Version
    chain: list[Version]
    """Root first, this version last."""
    design_table: Table
    violin_location: Location
    steps: list[Table] = field(default_factory=list)
    """How the placement was reached, root first, for records."""

    @property
    def id(self) -> str:
        return self.version.id

    @cached_property
    def design(self) -> Design:
        return Design.from_table(self.id, self.design_table)

    @cached_property
    def scan(self) -> shoulders.ShoulderScan:
        return shoulders.scan(self.design.shoulder)

    def violin_mount(self) -> Location:
        return self.design.violin_mount(self.design.violin_outline())

    def rest_pose(self) -> Location:
        return rest_pose(self.violin_location, self.violin_mount())


def resolve(version_id: str) -> Resolved:
    """Merge design tables down the parent chain and compose placements."""
    chain: list[Version] = []
    seen: set[str] = set()
    current: str | None = version_id
    while current is not None:
        if current in seen:
            raise ValueError(f"Parent cycle through {current}")
        seen.add(current)
        version = load(current)
        chain.append(version)
        current = version.parent
    chain.reverse()

    design: Table = {}
    location: Location | None = None
    steps: list[Table] = []
    for version in chain:
        design = deep_merge(design, version.own_design())
        resolved_design = Design.from_table(version.id, design)
        if "placement" in version.data:
            base = shoulders.scan(resolved_design.shoulder).placement_base
            location = absolute_violin_location(version.data["placement"], base)
            steps.append({"version": version.id, "placement": version.data["placement"]})
        elif location is None:
            raise ValueError(f"{version.id} is a root version and needs a [placement] table")
        if "adjust" in version.data:
            adjustment = Adjustment.from_table(version.data["adjust"])
            violin = resolved_design.violin_outline()
            move = adjustment.transform(resolved_design.pivots(violin))
            location = adjust(location, resolved_design.violin_mount(violin), move)
            steps.append({"version": version.id, "adjust": version.data["adjust"]})
    assert location is not None
    return Resolved(chain[-1], chain, design, location, steps)


def lineage(versions: list[Version]) -> Iterator[tuple[int, Version]]:
    """Versions depth-first from each root, with their depth, children in id order."""
    children: dict[str | None, list[Version]] = {}
    for version in versions:
        children.setdefault(version.parent, []).append(version)

    def walk(parent: str | None, depth: int) -> Iterator[tuple[int, Version]]:
        for child in sorted(children.get(parent, []), key=lambda v: _number(v.id)):
            yield depth, child
            yield from walk(child.id, depth + 1)

    known = {version.id for version in versions}
    orphans = [v for v in versions if v.parent is not None and v.parent not in known]
    yield from walk(None, 0)
    for orphan in orphans:
        yield 0, orphan


def create(
    parent: str, *, summary: str, version_id: str | None = None,
    adjust_table: Table | None = None, overrides: Table | None = None,
) -> Version:
    """Write a new version file inheriting from ``parent``; returns it."""
    load(parent)  # Fail early on a missing parent.
    version_id = version_id or next_id()
    if not ID_PATTERN.match(version_id):
        raise ValueError("Version ids look like v012")
    path = paths.versions_dir() / f"{version_id}.toml"
    if path.exists():
        raise ValueError(f"{version_id} already exists")
    document = tomlkit.document()
    document["summary"] = summary
    document["parent"] = parent
    document["created"] = datetime.date.today()
    if adjust_table:
        document["adjust"] = adjust_table
    for key, value in (overrides or {}).items():
        document[key] = value
    _check(version_id, document.unwrap())
    path.write_text(tomlkit.dumps(document))
    version = load(version_id)
    resolve(version_id)  # Fail early on a bad override; the caller can fix the file.
    return version


def record_print(version_id: str, record: Table) -> None:
    """Append a ``[[prints]]`` entry, keeping the file's comments and layout."""
    version = load(version_id)
    document = tomlkit.parse(version.path.read_text())
    prints = document.get("prints")
    if prints is None:
        prints = tomlkit.aot()
        document["prints"] = prints
    entry = tomlkit.table()
    for key, value in record.items():
        entry[key] = value
    prints.append(entry)
    version.path.write_text(tomlkit.dumps(document))


def parse_assignment(text: str) -> Table:
    """``rest.head.front_width=19`` as a nested table; the value is TOML (bare text is a string)."""
    key, sep, raw = text.partition("=")
    if not sep or not key.strip():
        raise ValueError(f"Expected key=value, got {text!r}")
    try:
        value = tomllib.loads(f"value = {raw.strip()}")["value"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()
    table: Table = {}
    node = table
    *parents, leaf = [part.strip() for part in key.split(".")]
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    return table
