"""Where the violin, and so the rest, sits on the shoulder.

A version places the violin's mount frame in the shoulder frame (the
library's ``Shoulder.violin_location``). The rest hangs rigidly from the
violin, so its pose on the shoulder follows:

    rest_pose = violin_location * violin_mount.inverse()

where ``violin_mount`` is the rest body's frame that meets the violin
(``RestGeometry.violin_mount``).

A root version gives an absolute placement. Later versions usually adjust
their parent's placement in the rest's own frame: shift it along its axes and
rotate it about a pivot on the rest, which is easier to judge from a printed
prototype than angles about a point on the scan. In the rest frame, X runs
along the rest toward the head end (the violin's right side), Y toward the
back edge (the violin's end button), and Z up toward the violin; Z=0 is the
leg-mounting face.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

from build123d import Location, Pos, Rotation, Vector

PIVOTS = ("legs_midpoint", "right_leg", "left_leg", "head_center", "violin_mount", "origin")


def fitted_violin_location(
    base: Sequence[float], *, angle: float, level: float, roll: float,
    offset: Sequence[float],
) -> Location:
    """The absolute placement used for the first fits, in degrees and mm.

    Starting from ``base``, a point on the cast in the shoulder frame,
    ``angle`` turns the violin about the vertical, ``level`` tilts it level,
    ``offset`` moves it in that turned frame and ``roll`` rolls it about the
    mount Y axis.
    """
    return (
        Pos(*base) * Rotation(0, 0, 90 - angle) * Rotation(level, 0, 0)
        * Location(tuple(offset), (0, -roll, 0))
    )


def absolute_violin_location(placement: Mapping[str, Any], base: Sequence[float]) -> Location:
    """A ``[placement]`` table: either fitted angles or a direct position and rotation."""
    keys = set(placement)
    if keys <= {"position", "rotation"} and "position" in keys:
        return Location(tuple(placement["position"]), tuple(placement.get("rotation", (0, 0, 0))))
    fitted = {"angle", "level", "roll", "offset"}
    if keys == fitted:
        return fitted_violin_location(
            base, angle=placement["angle"], level=placement["level"],
            roll=placement["roll"], offset=placement["offset"],
        )
    raise ValueError(
        "[placement] needs either angle, level, roll and offset, or position (and rotation)"
    )


@dataclass(frozen=True)
class Adjustment:
    """Move the rest in its own frame: shift in mm along its axes, then rotate in degrees.

    The rotation turns about axes through ``pivot`` parallel to the rest's
    axes, applied as build123d's ``Rotation(*rotate)``. The shift is along the
    unadjusted rest's axes.
    """

    shift: tuple[float, float, float] = (0, 0, 0)
    rotate: tuple[float, float, float] = (0, 0, 0)
    pivot: str | tuple[float, float, float] = "legs_midpoint"

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> "Adjustment":
        unknown = set(table) - {"shift", "rotate", "pivot"}
        if unknown:
            raise ValueError(f"Unknown [adjust] keys {sorted(unknown)}; use shift, rotate, pivot")
        shift = _triple(table.get("shift", (0, 0, 0)), "adjust.shift")
        rotate = _triple(table.get("rotate", (0, 0, 0)), "adjust.rotate")
        pivot = table.get("pivot", "legs_midpoint")
        if isinstance(pivot, str):
            if pivot not in PIVOTS:
                raise ValueError(f"adjust.pivot must be one of {PIVOTS} or [x, y, z]")
            return cls(shift, rotate, pivot)
        return cls(shift, rotate, _triple(pivot, "adjust.pivot"))

    def transform(self, pivots: Mapping[str, Vector]) -> Location:
        """The move as a Location in the rest frame."""
        point = pivots[self.pivot] if isinstance(self.pivot, str) else Vector(*self.pivot)
        return Pos(*self.shift) * Pos(point) * Rotation(*self.rotate) * Pos(-point)


def _triple(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"{name} must be [x, y, z]")
    if not all(isinstance(v, int | float) and isfinite(v) for v in value):
        raise ValueError(f"{name} must hold finite numbers")
    return (float(value[0]), float(value[1]), float(value[2]))


def adjust(violin_location: Location, violin_mount: Location, move: Location) -> Location:
    """The violin location that moves the rest by ``move`` in its own frame."""
    return violin_location * violin_mount.inverse() * move * violin_mount


def rest_pose(violin_location: Location, violin_mount: Location) -> Location:
    """The rest body's local frame in the shoulder frame."""
    return violin_location * violin_mount.inverse()


def describe(location: Location) -> dict[str, list[float]]:
    """Position and build123d rotation angles, rounded for records."""
    return {
        "position": [round(v, 4) for v in location.position],
        "rotation": [round(v, 4) for v in location.orientation],
    }


def relative_move(
    before: Location, after: Location, pivot: Vector,
) -> dict[str, list[float]]:
    """How a rest moved between two poses, in the earlier rest's frame.

    ``rotate`` is in build123d ``Rotation`` angles; ``shift`` is how far
    ``pivot`` (a point on the earlier rest) moved. Together they form the
    ``[adjust]`` that reproduces the move about that pivot.
    """
    move = before.inverse() * after
    moved_pivot = move * Pos(pivot)
    return {
        "shift": [round(v, 4) for v in moved_pivot.position - pivot],
        "rotate": [round(v, 4) for v in move.orientation],
    }
