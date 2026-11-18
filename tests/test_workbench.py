"""Workbench checks: config handling, placement math, versions, presets and a smoke build."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from build123d import Location, Pos, Rotation, Vector

from rest_workbench import paths, settings
from rest_workbench.config import deep_merge, flatten, to_dataclass, to_table
from rest_workbench.placement import (
    Adjustment, adjust, fitted_violin_location, relative_move, rest_pose,
)
from rest_workbench.slicing import _preset_index, apply_overrides, flatten_preset
from shoulder_rest.parts.rest.w_rest import WHeadParameters, WRestParameters

REPO = Path(__file__).resolve().parents[1]


def close(a: Location, b: Location) -> bool:
    return (
        (Vector(*a.position) - Vector(*b.position)).length < 1e-6
        and all(abs(x - y) < 1e-6 for x, y in zip(a.orientation, b.orientation, strict=True))
    )


class ConfigTests(unittest.TestCase):
    def test_merge_replaces_values_and_merges_tables(self) -> None:
        merged = deep_merge(
            {"rest": {"head": {"front_width": 18, "depth": 80}, "path": {"strokes": [[1, 2]]}}},
            {"rest": {"head": {"front_width": 20}, "path": {"strokes": [[3, 4], [5, 6]]}}},
        )
        self.assertEqual(merged["rest"]["head"], {"front_width": 20, "depth": 80})
        self.assertEqual(merged["rest"]["path"]["strokes"], [[3, 4], [5, 6]])

    def test_dataclass_conversion(self) -> None:
        params = to_dataclass(WRestParameters, {
            "path": {"strokes": [[40, 10], {"length": 50, "angle": -20}, [36, 10]]},
            "head": {"left_cap": {"angle": 20}, "stripes": False},
        })
        self.assertEqual(params.path.strokes[1].length, 50)
        self.assertIsNone(params.head.stripes)
        self.assertEqual(params.head.left_cap.angle if params.head.left_cap else None, 20)
        self.assertEqual(to_dataclass(WRestParameters, to_table(params)), params)
        with self.assertRaisesRegex(ValueError, "did you mean 'front_width'"):
            to_dataclass(WHeadParameters, {"front_widht": 3})

    def test_flatten_indexes_arrays(self) -> None:
        self.assertEqual(
            flatten({"a": {"b": [[1, 2]], "c": 3}}), {"a.b.0.0": 1, "a.b.0.1": 2, "a.c": 3},
        )


class PlacementTests(unittest.TestCase):
    mount = Location((0, 73.5, 12))
    start = fitted_violin_location((-55, -87.5, 57.5), angle=16, level=-3.5, roll=28, offset=(7, -1, 12))

    def test_fitted_matches_previous_library_formula(self) -> None:
        expected = (
            Pos(-55, -87.5, 57.5) * Rotation(0, 0, 90 - 24) * Rotation(8, 0, 0)
            * Location((18, 2, 24), (0, -24, 0))
        )
        actual = fitted_violin_location((-55, -87.5, 57.5), angle=24, level=8, roll=24, offset=(18, 2, 24))
        self.assertTrue(close(actual, expected))

    def test_adjustment_pivot_stays_put_and_round_trips(self) -> None:
        pivot = Vector(95, 14, 0)
        move = Adjustment((1, -2, 0.5), (2, -3, 4), (95, 14, 0)).transform({})
        moved = adjust(self.start, self.mount, move)
        before, after = rest_pose(self.start, self.mount), rest_pose(moved, self.mount)
        # Without a shift, the pivot keeps its place on the shoulder.
        still = adjust(self.start, self.mount, Adjustment(rotate=(2, -3, 4), pivot=(95, 14, 0)).transform({}))
        self.assertLess(
            ((before * Pos(pivot)).position - (rest_pose(still, self.mount) * Pos(pivot)).position).length,
            1e-6,
        )
        recovered = relative_move(before, after, pivot)
        for got, want in zip(recovered["shift"] + recovered["rotate"], [1, -2, 0.5, 2, -3, 4], strict=True):
            self.assertAlmostEqual(got, want, places=3)

    def test_adjustment_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "pivot"):
            Adjustment.from_table({"pivot": "elbow"})
        with self.assertRaisesRegex(ValueError, "Unknown"):
            Adjustment.from_table({"rotation": [0, 0, 1]})


class PresetTests(unittest.TestCase):
    def test_flatten_follows_inherits_and_includes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "process").mkdir()
            presets = {
                "base": {"name": "base", "wall_loops": "2", "layer_height": "0.2"},
                "template": {"name": "template", "start_gcode": "G28"},
                "child": {"name": "child", "inherits": "base", "include": ["template"],
                          "wall_loops": "3", "instantiation": "true"},
            }
            for name, preset in presets.items():
                (root / "process" / f"{name}.json").write_text(json.dumps(preset))
            _preset_index.cache_clear()
            flat = flatten_preset("process", "child", root)
            self.assertEqual(flat["wall_loops"], "3")
            self.assertEqual(flat["layer_height"], "0.2")
            self.assertEqual(flat["start_gcode"], "G28")
            self.assertNotIn("inherits", flat)
            with self.assertRaisesRegex(ValueError, "close"):
                flatten_preset("process", "chlid", root)

    def test_overrides_use_bambu_encoding(self) -> None:
        result = apply_overrides(
            {"wall_loops": "2", "fan": ["40", "40"]},
            {"wall_loops": 4, "fan": 20, "enable_support": True},
        )
        self.assertEqual(result, {"wall_loops": "4", "fan": ["20", "20"], "enable_support": "1"})


class VersionTests(unittest.TestCase):
    """Versions in a temporary workbench that shares this checkout's shoulders and library."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        shutil.copy(REPO / "workbench.toml", root)
        for name in ("shoulders", "ShoulderRest", "slicer", "src"):
            (root / name).symlink_to(REPO / name)
        (root / "versions").mkdir()
        shutil.copy(REPO / "versions" / "v001.toml", root / "versions")
        os.environ["SRW_ROOT"] = str(root)
        paths.root.cache_clear()
        settings.settings.cache_clear()
        self.root = root

    def tearDown(self) -> None:
        os.environ.pop("SRW_ROOT", None)
        paths.root.cache_clear()
        settings.settings.cache_clear()
        self.directory.cleanup()

    def test_child_inherits_design_and_moves_rest(self) -> None:
        from rest_workbench import catalog, versions

        versions.create(
            "v001", summary="Turn about the right leg", adjust_table={
                "rotate": [0, 0, 2], "pivot": "right_leg",
            }, overrides={"rest": {"head": {"front_width": 19}}},
        )
        child = versions.resolve("v002")
        self.assertEqual([v.id for v in child.chain], ["v001", "v002"])
        self.assertEqual(child.design.rest.head.front_width, 19)
        self.assertEqual(child.design.rest.head.left_cap, versions.resolve("v001").design.rest.head.left_cap)
        self.assertEqual(child.design.engraving, {"text": "v002"})
        result = catalog.diff("v001", "v002", pivot="right_leg")
        self.assertEqual(result["parameters"]["rest.head.front_width"], (18, 19))
        self.assertAlmostEqual(result["move"]["rotate"][2], 2, places=4)
        self.assertAlmostEqual(max(map(abs, result["move"]["shift"])), 0, places=4)
        self.assertIn("  v002  Turn about the right leg", catalog.tree_lines())

    def test_record_print_keeps_comments(self) -> None:
        import tomllib

        from rest_workbench import versions

        versions.record_print("v001", {"parts": ["rest"], "feedback": "Digs in at the front"})
        text = (self.root / "versions" / "v001.toml").read_text()
        self.assertIn("# Fitted angles", text)
        self.assertEqual(tomllib.loads(text)["prints"][0]["feedback"], "Digs in at the front")

    def test_parse_assignment(self) -> None:
        from rest_workbench.versions import parse_assignment

        self.assertEqual(parse_assignment("rest.head.stripes=false"), {"rest": {"head": {"stripes": False}}})
        self.assertEqual(parse_assignment("legs.rod_retention={}"), {"legs": {"rod_retention": {}}})

    def test_smoke_build(self) -> None:
        """Catch interface drift between the workbench and the library."""
        from rest_workbench import versions
        from rest_workbench.build import build

        result = build(versions.resolve("v001"), renders=False)
        self.assertTrue(result.file("rest_stl").stat().st_size > 0)
        self.assertEqual(result.manifest["design"]["rest"]["tail"]["engraving"], {"text": "v001"})
        self.assertTrue(build(versions.resolve("v001"), renders=False).skipped)


if __name__ == "__main__":
    unittest.main()
