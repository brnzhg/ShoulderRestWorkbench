"""A resolved version's design table, turned into library objects.

Design tables (``shoulder``, ``violin``, ``rest``, ``legs``) mirror the
library's parameter dataclasses: ``[rest]`` is ``WRestParameters`` with
``[rest.path]``, ``[rest.head]`` (and its caps and stripes) and
``[rest.tail]``; ``[legs.leg]``, ``[legs.cavity]`` and ``[legs.rod_retention]``
are the hinge leg's ``LegParameters``, ``CavityParameters`` and
``RodRetentionParameters``. Omitted keys keep library defaults.

Workbench-only keys:

- ``rest.type``: the rest design; only ``"w"`` exists so far.
- ``rest.tail.engraving``: text (``"{id}"`` becomes the version id), a table
  ``{text, font_size, depth, font}`` for ``text_engraving``, or ``false``.
- ``violin.outline``: right-hand outline points; defaults to the library's tracing.
- ``legs.type``: ``"hinge"``; ``legs.rod_retention = false`` drops the bumps.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from build123d import Location, Pos, Vector

from shoulder_rest.parts.engraving import Engraving
from shoulder_rest.parts.engraving.text_engraving import text_engraving
from shoulder_rest.parts.leg.hinge_leg import CavityParameters, HingeLeg, LegParameters
from shoulder_rest.parts.leg.rod_retention import RodRetentionParameters
from shoulder_rest.parts.rest import RestGeometry
from shoulder_rest.parts.rest.w_rest import WRestParameters, w_rest_geometry
from shoulder_rest.parts.shoulder import Shoulder
from shoulder_rest.parts.violin_outline import ViolinOutline
from shoulder_rest.parts.violin_outline.spline_violin_outline import (
    TRACED_RIGHT_OUTLINE, build_violin_outline,
)

from .config import Table, to_dataclass, to_table

DESIGN_KEYS = ("shoulder", "violin", "rest", "legs")


@dataclass(frozen=True)
class Design:
    """Everything needed to build one version's rest, plus its full parameter table."""

    version: str
    shoulder: str
    violin: Table
    rest: WRestParameters
    legs: Table
    engraving: Table | None
    table: Table
    """Every design parameter, defaults included, for records and comparison."""

    @classmethod
    def from_table(cls, version: str, design: Mapping[str, Any]) -> "Design":
        unknown = set(design) - set(DESIGN_KEYS)
        if unknown:
            raise ValueError(f"Unknown design tables {sorted(unknown)}")
        if "shoulder" not in design:
            raise ValueError("The design needs a shoulder (a directory name under shoulders/)")
        violin = _violin_table(design.get("violin", {}))
        rest_table = dict(design.get("rest", {}))
        rest_type = rest_table.pop("type", "w")
        if rest_type != "w":
            raise ValueError(f"Unknown rest.type {rest_type!r}; only 'w' is implemented")
        tail = dict(rest_table.get("tail", {}))
        engraving = _engraving_spec(tail.pop("engraving", False), version)
        if engraving is not None:
            tail["engraving"] = _engraving(engraving)
        rest = to_dataclass(WRestParameters, {**rest_table, "tail": tail}, "rest")
        legs = _legs_table(design.get("legs", {}))

        full_rest = to_table(rest)
        full_rest["type"] = rest_type
        full_rest["tail"]["engraving"] = engraving if engraving is not None else False
        table = {"shoulder": design["shoulder"], "violin": violin, "rest": full_rest, "legs": legs}
        return cls(version, design["shoulder"], violin, rest, legs, engraving, table)

    def violin_outline(self) -> ViolinOutline:
        return build_violin_outline(
            [tuple(point) for point in self.violin["outline"]],
            attachment_offset=self.violin["attachment_offset"],
            block_thickness=self.violin["block_thickness"],
        )

    def hinge_leg(self) -> HingeLeg:
        retention = self.legs["rod_retention"]
        return HingeLeg(
            to_dataclass(LegParameters, self.legs["leg"], "legs.leg"),
            to_dataclass(CavityParameters, self.legs["cavity"], "legs.cavity"),
            rod_retention=(
                None if retention is False
                else to_dataclass(RodRetentionParameters, retention, "legs.rod_retention")
            ),
        )

    def geometry(self, violin: ViolinOutline, shoulder: Shoulder) -> RestGeometry:
        return w_rest_geometry(violin, shoulder, self.rest)

    def violin_mount(self, violin: ViolinOutline) -> Location:
        """The rest frame that meets the violin; matches ``w_rest_geometry``'s."""
        return violin.mount_location * Pos(0, 0, self.rest.violin_gap)

    def pivots(self, violin: ViolinOutline) -> dict[str, Vector]:
        """Named points on the rest, in its frame, for adjustments and comparisons."""
        left = violin.attachment_point("left", self.rest.left_fraction)
        right = violin.attachment_point("right", self.rest.right_fraction)
        corners = [right]
        for stroke in self.rest.path.strokes:
            corners.append(corners[-1] + stroke.vector)
        head = sum(corners[1:], corners[0]) / len(corners)
        return {
            "legs_midpoint": (left + right) / 2,
            "right_leg": right,
            "left_leg": left,
            "head_center": head,
            "violin_mount": self.violin_mount(violin).position,
            "origin": Vector(0, 0, 0),
        }


def _violin_table(table: Mapping[str, Any]) -> Table:
    unknown = set(table) - {"outline", "attachment_offset", "block_thickness"}
    if unknown:
        raise ValueError(f"Unknown violin keys {sorted(unknown)}")
    return {
        "outline": [list(point) for point in table.get("outline", TRACED_RIGHT_OUTLINE)],
        "attachment_offset": float(table.get("attachment_offset", 6.0)),
        "block_thickness": float(table.get("block_thickness", 38.0)),
    }


def _engraving_spec(value: Any, version: str) -> Table | None:
    if value is False or value is None:
        return None
    spec = {"text": value} if isinstance(value, str) else dict(value)
    unknown = set(spec) - {"text", "font_size", "depth", "font"}
    if unknown or "text" not in spec:
        raise ValueError("rest.tail.engraving needs text, and optionally font_size, depth, font")
    spec["text"] = str(spec["text"]).replace("{id}", version)
    return spec


def _engraving(spec: Table) -> Engraving:
    options = {key: spec[key] for key in ("font_size", "depth", "font") if key in spec}
    return text_engraving(spec["text"], **options)


def _legs_table(table: Mapping[str, Any]) -> Table:
    unknown = set(table) - {"type", "leg", "cavity", "rod_retention"}
    if unknown:
        raise ValueError(f"Unknown legs keys {sorted(unknown)}")
    if table.get("type", "hinge") != "hinge":
        raise ValueError("Only legs.type = 'hinge' is implemented")
    retention = table.get("rod_retention", False)
    return {
        "type": "hinge",
        "leg": to_table(to_dataclass(LegParameters, table.get("leg", {}), "legs.leg")),
        "cavity": to_table(to_dataclass(CavityParameters, table.get("cavity", {}), "legs.cavity")),
        "rod_retention": False if retention is False else to_table(
            to_dataclass(RodRetentionParameters, retention, "legs.rod_retention"),
        ),
    }
