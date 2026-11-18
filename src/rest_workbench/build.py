"""Build a version: export printable parts and a fitted scene, render previews, write a manifest.

Everything goes to ``builds/<id>/`` (ignored by git; rebuildable from the
version file and the recorded commits):

- ``rest.step`` / ``rest.stl``: the rest body in its local frame / in print
  pose, mounting face down on the bed.
- ``leg.step`` / ``leg.stl``: one hinge leg / on its side for printing.
- ``scene.step``: shoulder cast, violin block and rest with legs, fitted.
- ``fit.png``, ``rest.png``: standard views of the fit and the body.
- ``manifest.json``: the full design, placement, poses, sizes and provenance.

A build is skipped when its manifest already matches the version's design,
placement and the workbench and library sources.
"""

import datetime
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from build123d import Compound, Part, Pos, Rotation, Shape, export_step, export_stl, import_step

from shoulder_rest.parts.leg.hinge_leg import HingeLeg
from shoulder_rest.parts.rest import Rest
from shoulder_rest.parts.shoulder import Shoulder
from shoulder_rest.parts.violin_outline import ViolinOutline

from . import paths
from .placement import describe
from .versions import Resolved

REST_PRINT_ROTATION = Rotation(180, 0, 0)
"""The rest prints with its flat mounting face (Z=0, facing +Z) on the bed."""
LEG_PRINT_ROTATION = Rotation(0, 90, 0)
"""Hinge legs print on their broad side."""
STL_TOLERANCE = 0.01
STL_ANGULAR_TOLERANCE = 0.1
FIT_VIEWS = ("iso", "top", "front", "back", "right", "left")
REST_VIEWS = ("iso", "iso_below", "top", "bottom", "front", "right")


@dataclass(frozen=True)
class Build:
    directory: Path
    manifest: dict[str, Any]
    skipped: bool

    def file(self, name: str) -> Path:
        return self.directory / self.manifest["files"][name]


def build_dir(version_id: str) -> Path:
    return paths.builds_dir() / version_id


def fingerprint(resolved: Resolved) -> str:
    """Hash of everything that determines the geometry."""
    digest = hashlib.sha256()
    digest.update(json.dumps(resolved.design_table, sort_keys=True, default=str).encode())
    digest.update(json.dumps(describe(resolved.violin_location)).encode())
    sources = [
        *sorted((paths.library_dir() / "src").rglob("*.py")),
        *sorted((paths.root() / "src").rglob("*.py")),
    ]
    for source in sources:
        digest.update(source.read_bytes())
    for file in resolved.scan.files():
        stat = file.stat()
        digest.update(f"{file.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


def existing(version_id: str) -> Build | None:
    manifest_path = build_dir(version_id) / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text())
    return Build(manifest_path.parent, manifest, skipped=True)


@dataclass
class Fitted:
    """A version's library objects; ``scene`` positions them once."""

    version: str
    shoulder: Shoulder
    violin: ViolinOutline
    rest: Rest[Any]
    leg: HingeLeg
    _scene: Compound | None = None

    def scene(self, leg_angles: tuple[float, float] = (0, 0)) -> Compound:
        """Shoulder, violin block and rest with legs, fitted; built on first call."""
        if self._scene is None:
            self.rest.add(self.rest.left.leg(angle=leg_angles[0]))
            self.rest.add(self.rest.right.leg(angle=leg_angles[1]))
            self.violin.position_on(self.shoulder)
            self.rest.position_on(self.violin)
            self._scene = Compound(
                label=f"{self.version} fitting",
                children=[self.shoulder.assembly, self.violin.block, self.rest.assembly],
            )
        return self._scene


def construct(resolved: Resolved) -> Fitted:
    """Build the version's rest on its shoulder, unpositioned."""
    design = resolved.design
    shoulder = resolved.scan.load(resolved.violin_location)
    violin = design.violin_outline()
    leg = design.hinge_leg()
    rest = Rest(design.geometry(violin, shoulder), leg)
    rest.part.label = f"{resolved.id} rest body"
    return Fitted(resolved.id, shoulder, violin, rest, leg)


def on_bed(part: Part, rotation: Rotation, label: str) -> Part:
    """A standalone copy turned into print pose, centered on the bed origin."""
    turned = Part(part.wrapped, label=label).moved(rotation)
    box = turned.bounding_box()
    return turned.moved(Pos(-box.center().X, -box.center().Y, -box.min.Z))


def build(resolved: Resolved, *, force: bool = False, renders: bool = True) -> Build:
    key = fingerprint(resolved)
    previous = existing(resolved.id)
    if not force and previous is not None and previous.manifest.get("fingerprint") == key and all(
        (previous.directory / name).is_file() for name in previous.manifest["files"].values()
    ) and (previous.manifest.get("renders") or not renders):
        return previous

    started = time.monotonic()
    out = build_dir(resolved.id)
    out.mkdir(parents=True, exist_ok=True)
    fitted = construct(resolved)
    design, shoulder, violin, rest, leg = (
        resolved.design, fitted.shoulder, fitted.violin, fitted.rest, fitted.leg,
    )
    body = Part(rest.part.wrapped, label=f"{resolved.id} rest body")
    printed_rest = on_bed(rest.part, REST_PRINT_ROTATION, f"{resolved.id} rest")
    printed_leg = on_bed(leg.part, LEG_PRINT_ROTATION, "Hinge leg")
    files = {
        "rest_step": "rest.step", "rest_stl": "rest.stl",
        "leg_step": "leg.step", "leg_stl": "leg.stl", "scene_step": "scene.step",
    }
    export_step(body, out / files["rest_step"])
    export_stl(printed_rest, out / files["rest_stl"], STL_TOLERANCE, STL_ANGULAR_TOLERANCE)
    export_step(Part(leg.part.wrapped, label="Hinge leg"), out / files["leg_step"])
    export_stl(printed_leg, out / files["leg_stl"], STL_TOLERANCE, STL_ANGULAR_TOLERANCE)

    scene = fitted.scene()
    export_step(scene, out / files["scene_step"])

    if renders:
        from shoulder_rest.render import render

        files["fit_png"] = "fit.png"
        files["rest_png"] = "rest.png"
        render([shoulder.assembly, rest.assembly], out / files["fit_png"], views=FIT_VIEWS)
        render([body], out / files["rest_png"], views=REST_VIEWS)

    pose = resolved.rest_pose()
    pivots = design.pivots(violin)
    size = printed_rest.bounding_box().size
    manifest = {
        "version": resolved.id,
        "summary": resolved.version.summary,
        "lineage": [version.id for version in resolved.chain],
        "built": datetime.datetime.now().isoformat(timespec="seconds"),
        "build_seconds": round(time.monotonic() - started, 1),
        "fingerprint": key,
        "renders": renders,
        "design": design.table,
        "placement": {
            "steps": resolved.steps,
            "violin_location": describe(resolved.violin_location),
            "rest_pose": describe(pose),
            "pivots_in_rest": {name: _xyz(point) for name, point in pivots.items()},
            "pivots_on_shoulder": {
                name: _xyz((pose * Pos(point)).position) for name, point in pivots.items()
            },
        },
        "geometry": {
            "rest_volume_cm3": round(rest.part.volume / 1000, 3),
            "rest_print_size_mm": _xyz(size),
            "leg_volume_cm3": round(leg.part.volume / 1000, 3),
        },
        "provenance": {
            "workbench": paths.git_state(paths.root()),
            "library": paths.git_state(paths.library_dir()),
        },
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return Build(out, manifest, skipped=False)


def _xyz(vector: Any) -> list[float]:
    return [round(vector.X, 3), round(vector.Y, 3), round(vector.Z, 3)]


COMPARE_VIEWS = ("top", "front", "right", "left", "iso")


def compare(
    resolved: list[Resolved], output: Path, *, views: tuple[str, ...] = COMPARE_VIEWS,
    shoulder_frame: bool = False,
) -> Path:
    """Overlay several built rests on the first one's shoulder; earlier ones are ghosted.

    Views are in the first rest's frame (top looks down onto its mounting
    face), zoomed to the rests, unless ``shoulder_frame``.
    """
    from shoulder_rest.render import render

    shapes: list[Shape] = []
    first = resolved[0]
    shoulder = first.scan.load(first.violin_location)
    shapes.append(shoulder.assembly)
    for index, item in enumerate(resolved):
        if item.design.shoulder != first.design.shoulder:
            raise ValueError("Compare versions fitted to the same shoulder")
        built = build(item, renders=False)
        body = import_step(built.file("rest_step"))
        body.label = f"{item.id} rest"
        shapes.append(body.moved(item.rest_pose()))
    ghosts = [f"{item.id} rest" for item in resolved[:-1]]
    render(
        shapes, output, views=views, ghost=ghosts, focus=[" rest"],
        frame=None if shoulder_frame else f"{first.id} rest",
    )
    return output
