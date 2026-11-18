"""Shoulder scans: casts and how to level them, one directory per scan under shoulders/.

Each directory holds ``shoulder.toml`` and the STEP casts it names::

    reference = "reference.step"      # cast surface, for display
    extended = "extended.step"        # cast grown away from the skin, for cutting
    placement_base = [x, y, z]        # base point for fitted [placement] angles
    [cast_location]                   # scan frame -> shoulder frame
    position = [0, 0, 0]
    rotation = [30, 30, -5]
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from build123d import Location

from shoulder_rest.parts.shoulder import Shoulder
from shoulder_rest.parts.shoulder.step_shoulder import load_shoulder

from . import paths


@dataclass(frozen=True)
class ShoulderScan:
    name: str
    directory: Path
    reference: Path
    extended: Path
    cast_location: Location
    placement_base: tuple[float, float, float]

    def load(self, violin_location: Location) -> Shoulder:
        return load_shoulder(
            self.reference, self.extended, violin_location, cast_location=self.cast_location,
        )

    def files(self) -> list[Path]:
        return [self.directory / "shoulder.toml", self.reference, self.extended]


def scan(name: str) -> ShoulderScan:
    directory = paths.shoulders_dir() / name
    config_path = directory / "shoulder.toml"
    if not config_path.is_file():
        available = sorted(p.name for p in paths.shoulders_dir().iterdir() if p.is_dir())
        raise ValueError(f"No shoulder {name!r}; available: {available}")
    config = tomllib.loads(config_path.read_text())
    cast = config.get("cast_location", {})
    base = config.get("placement_base", (0, 0, 0))
    return ShoulderScan(
        name=name,
        directory=directory,
        reference=directory / config["reference"],
        extended=directory / config["extended"],
        cast_location=Location(
            tuple(cast.get("position", (0, 0, 0))), tuple(cast.get("rotation", (0, 0, 0))),
        ),
        placement_base=(float(base[0]), float(base[1]), float(base[2])),
    )
