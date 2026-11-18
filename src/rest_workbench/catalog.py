"""What has been tried: the version tree, a flat dataset of every version, and diffs."""

import csv
import io
from collections.abc import Iterable
from typing import Any

from build123d import Pos

from .config import flatten
from .placement import describe, relative_move
from .versions import Resolved, Version, all_versions, lineage, resolve


def latest_scores(version: Version) -> dict[str, Any]:
    """Scores from the most recent print that has any."""
    for record in reversed(version.prints):
        if record.get("scores"):
            return dict(record["scores"])
    return {}


def tree_lines() -> list[str]:
    lines = []
    for depth, version in lineage(all_versions()):
        marks = []
        if version.prints:
            marks.append(f"printed x{len(version.prints)}")
        if verdict := version.data.get("verdict"):
            marks.append(str(verdict))
        if scores := latest_scores(version):
            marks.append(" ".join(f"{k}={v}" for k, v in scores.items()))
        suffix = f"  [{'; '.join(marks)}]" if marks else ""
        lines.append(f"{'  ' * depth}{version.id}  {version.summary}{suffix}")
    return lines


def row(resolved: Resolved) -> dict[str, Any]:
    """One dataset row: metadata, scores, rest pose on the shoulder and every design parameter."""
    version = resolved.version
    design = resolved.design
    violin = design.violin_outline()
    pose = resolved.rest_pose()
    placed = describe(pose)
    data: dict[str, Any] = {
        "id": version.id,
        "parent": version.parent or "",
        "created": str(version.data.get("created", "")),
        "summary": version.summary,
        "verdict": version.data.get("verdict", ""),
        "prints": len(version.prints),
    }
    data.update({f"score.{k}": v for k, v in latest_scores(version).items()})
    for axis, value in zip("xyz", placed["position"], strict=True):
        data[f"pose.{axis}"] = value
    for axis, value in zip("xyz", placed["rotation"], strict=True):
        data[f"pose.r{axis}"] = value
    for name, point in design.pivots(violin).items():
        if name in ("legs_midpoint", "right_leg", "left_leg", "head_center"):
            on_shoulder = (pose * Pos(point)).position
            for axis, value in zip("xyz", on_shoulder, strict=True):
                data[f"shoulder.{name}.{axis}"] = round(value, 3)
    table = dict(design.table)
    table["violin"] = {k: v for k, v in table["violin"].items() if k != "outline"}
    data.update(flatten(table))
    return data


def dataset(versions: Iterable[str] | None = None, *, varying: bool = False) -> list[dict[str, Any]]:
    ids = list(versions) if versions is not None else [v.id for v in all_versions()]
    rows = [row(resolve(version_id)) for version_id in ids]
    if varying and len(rows) > 1:
        keep = {"id", "parent", "summary"}
        columns = {key for r in rows for key in r}
        keep |= {key for key in columns if len({str(r.get(key)) for r in rows}) > 1}
        rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
    return rows


def to_csv(rows: list[dict[str, Any]]) -> str:
    columns: list[str] = []
    for r in rows:
        columns.extend(key for key in r if key not in columns)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def diff(a: str, b: str, *, pivot: str = "legs_midpoint") -> dict[str, Any]:
    """Design parameters that differ, and how the rest moved from a to b in a's frame."""
    first, second = resolve(a), resolve(b)
    flat_a, flat_b = flatten(first.design.table), flatten(second.design.table)
    changes = {
        key: (flat_a.get(key), flat_b.get(key))
        for key in sorted(set(flat_a) | set(flat_b)) if flat_a.get(key) != flat_b.get(key)
    }
    pivots = first.design.pivots(first.design.violin_outline())
    if pivot not in pivots:
        raise ValueError(f"Pivot must be one of {sorted(pivots)}")
    move = relative_move(first.rest_pose(), second.rest_pose(), pivots[pivot])
    return {"parameters": changes, "move": {"pivot": pivot, **move}}
