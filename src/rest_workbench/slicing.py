"""Slice built parts with the Bambu Studio command line into printer-ready .gcode.3mf files.

A slicer profile is a TOML file in ``slicer/`` naming Bambu Studio system
presets, plus overrides in Bambu's own setting keys::

    printer = "Bambu Lab P1S 0.4 nozzle"
    process = "0.20mm Standard @BBL X1C"
    filament = "Bambu PETG Basic @BBL X1C"

    [process_overrides]
    wall_loops = 4
    sparse_infill_density = "25%"

The command line does not follow a preset's ``inherits`` and ``include``
chain, so presets are flattened from Bambu Studio's system profile directory
first. Overrides are checked against the sliced project's settings. Each slice
records the exact preset files it used next to its output.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
import zipfile
from dataclasses import dataclass
from difflib import get_close_matches
from functools import cache
from pathlib import Path
from typing import Any

from . import paths, settings
from .build import Build

PARTS = {"rest": ("rest_stl", 1), "legs": ("leg_stl", 2)}
"""Each printable group: the build file and how many copies go on the plate."""
CATEGORIES = {"printer": "machine", "process": "process", "filament": "filament"}


@dataclass(frozen=True)
class SlicerProfile:
    name: str
    presets: dict[str, str]
    """printer, process and filament preset names."""
    overrides: dict[str, dict[str, Any]]
    """Per preset kind, Bambu setting key -> value."""
    description: str = ""

    @classmethod
    def load(cls, name: str) -> "SlicerProfile":
        path = paths.slicer_dir() / f"{name}.toml"
        if not path.is_file():
            available = sorted(p.stem for p in paths.slicer_dir().glob("*.toml"))
            raise ValueError(f"No slicer profile {name!r}; available: {available}")
        data = tomllib.loads(path.read_text())
        allowed = {"description", *CATEGORIES, *(f"{kind}_overrides" for kind in CATEGORIES)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"{path.name}: unknown keys {sorted(unknown)}")
        missing = [kind for kind in CATEGORIES if kind not in data]
        if missing:
            raise ValueError(f"{path.name}: missing presets {missing}")
        return cls(
            name=name,
            presets={kind: data[kind] for kind in CATEGORIES},
            overrides={kind: dict(data.get(f"{kind}_overrides", {})) for kind in CATEGORIES},
            description=str(data.get("description", "")),
        )


@cache
def _preset_index(category: str, root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for file in (root / category).glob("*.json"):
        try:
            name = json.loads(file.read_text()).get("name", file.stem)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        index[name] = file
    return index


def flatten_preset(category: str, name: str, root: Path) -> dict[str, Any]:
    """A system preset with its ``inherits`` parents and ``include`` templates merged in."""
    index = _preset_index(category, root)
    if name not in index:
        close = get_close_matches(name, index, n=3)
        raise ValueError(f"No {category} preset {name!r} in {root}; close: {close}")
    preset = json.loads(index[name].read_text())
    merged: dict[str, Any] = {}
    if preset.get("inherits"):
        merged.update(flatten_preset(category, preset["inherits"], root))
    for template in preset.get("include", []):
        included = flatten_preset(category, template, root)
        merged.update({k: v for k, v in included.items() if k not in ("name", "type")})
    merged.update(preset)
    for key in ("inherits", "include", "instantiation"):
        merged.pop(key, None)
    merged["name"] = name
    return merged


def apply_overrides(preset: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Overrides in Bambu's string encoding; list settings repeat a scalar per entry.

    Presets only list settings that differ from Bambu Studio's built-in
    defaults, so a key may be valid without appearing here; ``slice_part``
    checks every override against the sliced project's settings instead.
    """
    result = dict(preset)
    for key, value in overrides.items():
        current = preset.get(key)
        if isinstance(current, list):
            values = value if isinstance(value, list) else [value] * max(len(current), 1)
            result[key] = [_encode(v) for v in values]
        else:
            result[key] = [_encode(v) for v in value] if isinstance(value, list) else _encode(value)
    return result


def _check_applied(output: Path, overrides: dict[str, dict[str, Any]]) -> None:
    """Fail if the sliced project does not carry every override, e.g. a misspelled key."""
    with zipfile.ZipFile(output) as archive:
        applied = json.loads(archive.read("Metadata/project_settings.config"))
    problems = []
    for key, value in (item for kind in overrides.values() for item in kind.items()):
        expected = [_encode(v) for v in value] if isinstance(value, list) else [_encode(value)]
        actual = applied.get(key)
        actual = actual if isinstance(actual, list) else [actual]
        if key not in applied:
            close = get_close_matches(key, applied, n=3)
            problems.append(f"{key!r} is not a Bambu Studio setting (close: {close})")
        elif actual[: len(expected)] != expected:
            problems.append(f"{key!r} sliced as {actual} instead of {expected}")
    if problems:
        raise RuntimeError("Slicer overrides not applied: " + "; ".join(problems))


def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


@dataclass(frozen=True)
class SliceResult:
    part: str
    profile: str
    output: Path
    summary: dict[str, Any]


def slice_part(built: Build, part: str, profile: SlicerProfile) -> SliceResult:
    if part not in PARTS:
        raise ValueError(f"Unknown part {part!r}; choose from {sorted(PARTS)}")
    file_key, copies = PARTS[part]
    model = built.file(file_key)
    work = built.directory / "slice" / f"{part}-{profile.name}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    root = settings.slicer_profiles_dir()
    preset_files: dict[str, Path] = {}
    for kind, category in CATEGORIES.items():
        preset = flatten_preset(category, profile.presets[kind], root)
        preset = apply_overrides(preset, profile.overrides[kind])
        preset_files[kind] = work / f"{kind}.json"
        preset_files[kind].write_text(json.dumps(preset, indent=1))

    version_id = built.manifest["version"]
    output_name = f"{version_id}-{part}.gcode.3mf"
    command = [
        *settings.slicer_command(),
        "--slice", "0", "--arrange", "1", "--orient", "0", "--allow-rotations",
        "--load-settings", f"{preset_files['printer']};{preset_files['process']}",
        "--load-filaments", str(preset_files["filament"]),
        "--outputdir", str(work), "--export-3mf", output_name,
        *[str(model)] * copies,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    (work / "slicer.log").write_text(completed.stdout + completed.stderr)
    result_file = work / "result.json"
    result = json.loads(result_file.read_text()) if result_file.is_file() else {}
    output = work / output_name
    if completed.returncode != 0 or result.get("return_code", -1) != 0 or not output.is_file():
        message = result.get("error_string") or completed.stderr.strip().splitlines()[-1:]
        raise RuntimeError(f"Slicing {part} failed: {message} (see {work / 'slicer.log'})")
    _check_applied(output, profile.overrides)

    plates = result.get("sliced_plates", [])
    summary = {
        "version": version_id,
        "part": part,
        "copies": copies,
        "profile": profile.name,
        "slicer_version": _slicer_version(output),
        "presets": profile.presets,
        "overrides": profile.overrides,
        "print_minutes": round(sum(p.get("main_predication", 0) for p in plates) / 60, 1),
        "filament_g": round(sum(
            f.get("total_used_g", 0) for p in plates for f in p.get("filaments", [])
        ), 2),
        "gcode_3mf": output.name,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "fingerprint": built.manifest["fingerprint"],
    }
    (work / "slice.json").write_text(json.dumps(summary, indent=2) + "\n")
    return SliceResult(part, profile.name, output, summary)


def _slicer_version(output: Path) -> str:
    """The application that wrote a sliced 3MF, such as BambuStudio-02.09.01.70."""
    with zipfile.ZipFile(output) as archive:
        model = archive.read("3D/3dmodel.model").decode(errors="replace")
    match = re.search(r'name="Application">([^<]+)<', model)
    return match.group(1) if match else "unknown"


def latest_slice(built: Build, part: str) -> SliceResult | None:
    """The most recent slice of a part, if it matches the current build."""
    candidates = sorted(
        (built.directory / "slice").glob(f"{part}-*/slice.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        return None
    summary = json.loads(candidates[-1].read_text())
    if summary.get("fingerprint") != built.manifest["fingerprint"]:
        return None
    output = candidates[-1].parent / summary["gcode_3mf"]
    return SliceResult(part, summary["profile"], output, summary) if output.is_file() else None


def open_in_gui(file: Path) -> None:
    """Open a sliced file in Bambu Studio, to inspect it and send it to the printer."""
    subprocess.Popen(
        [*settings.slicer_gui_command(), str(file)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
