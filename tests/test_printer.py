"""Printer protocol helpers and print checks, without a printer."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

from rest_workbench import printer

ANNOUNCEMENT = """NOTIFY * HTTP/1.1
HOST: 239.255.255.250:1900
Location: 192.168.0.50
NT: urn:bambulab-com:device:3dprinter:1
USN: 01P00A000000001
DevModel.bambu.com: C12
DevName.bambu.com: p1s
DevConnect.bambu.com: lan
DevVersion.bambu.com: 01.10.00.00
"""


def idle_report(**changes: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "gcode_state": "FINISH", "print_error": 0, "hms": [], "sdcard": True, "nozzle_diameter": "0.4",
        "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000"},
        "ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PETG", "tray_color": "688197FF", "remain": -1},
            {"id": "1", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80},
            {"id": "2"},
        ]}]},
    }
    report.update(changes)
    return report


def job(**changes: Any) -> printer.JobSettings:
    values: dict[str, Any] = {
        "spools": "auto", "file_url": "file:///sdcard/{file}", "bed_leveling": True,
        "flow_calibration": True, "vibration_calibration": True, "timelapse": False,
    }
    values.update(changes)
    return printer.JobSettings(**values)


PETG = printer.SliceFacts(plates=[1], filament_types=["PETG"], nozzle_mm=0.4)


class ProtocolTests(unittest.TestCase):
    def test_announcement(self) -> None:
        found = printer.parse_announcement(ANNOUNCEMENT, "192.168.0.50")
        assert found is not None
        self.assertEqual((found.host, found.serial, found.mode), ("192.168.0.50", "01P00A000000001", "lan"))
        self.assertIsNone(printer.parse_announcement("NOTIFY * HTTP/1.1\nNT: upnp:rootdevice\n", "x"))

    def test_partial_reports_merge(self) -> None:
        state: dict[str, Any] = {}
        printer.merge_report(state, {"print": {"gcode_state": "RUNNING", "mc_percent": 10, "ams": {"tray_now": "0"}}})
        printer.merge_report(state, {"print": {"mc_percent": 11, "ams": {"ams_exist_bits": "1"}}})
        self.assertEqual(state["print"]["gcode_state"], "RUNNING")
        self.assertEqual(state["print"]["mc_percent"], 11)
        self.assertEqual(state["print"]["ams"], {"tray_now": "0", "ams_exist_bits": "1"})

    def test_codes(self) -> None:
        self.assertEqual(printer.hms_code(0x03000100, 0x00010007), "0300_0100_0001_0007")
        self.assertEqual(printer.error_code(0x0300800A), "0300_800A")
        summary = printer.summarize(idle_report(hms=[{"attr": 0x03000100, "code": 0x00020007}]))
        self.assertEqual(summary["hms"], [{"code": "0300_0100_0002_0007", "severity": "serious"}])

    def test_spools(self) -> None:
        found = {s.slot: s for s in printer.spools(idle_report())}
        self.assertEqual(sorted(found), [0, 1, 254])
        self.assertEqual(found[0].source, "AMS 1 slot 1")
        self.assertEqual(found[1].remaining_percent, 80)
        self.assertIsNone(found[0].remaining_percent)

    def test_camera_auth(self) -> None:
        auth = printer.camera_auth("12345678")
        self.assertEqual(len(auth), 80)
        self.assertEqual(auth[16:20], b"bblp")
        self.assertEqual(auth[48:56], b"12345678")

    def test_print_command(self) -> None:
        command = printer.print_command("v002-rest.gcode.3mf", job(), [0])
        self.assertEqual(command["url"], "file:///sdcard/v002-rest.gcode.3mf")
        self.assertEqual(command["param"], "Metadata/plate_1.gcode")
        self.assertEqual(command["subtask_name"], "v002-rest")
        self.assertTrue(command["use_ams"])
        self.assertEqual(command["ams_mapping"], [0])
        external = printer.print_command("a.gcode.3mf", job(), [printer.EXTERNAL])
        self.assertFalse(external["use_ams"])

    def test_slice_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.gcode.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/plate_1.gcode", "G28\n")
                archive.writestr("Metadata/project_settings.config", json.dumps(
                    {"filament_type": ["PETG"], "nozzle_diameter": ["0.4"]},
                ))
            facts = printer.slice_facts(path)
        self.assertEqual(facts, PETG)


class PlanTests(unittest.TestCase):
    def test_auto_picks_matching_ams_spool(self) -> None:
        checked = printer.plan(idle_report(), PETG, "auto")
        self.assertEqual((checked.mapping, checked.problems, checked.warnings), ([0], [], []))
        self.assertEqual(checked.material, "PETG #688197 (AMS 1 slot 1)")

    def test_blocks_busy_or_failing_printer(self) -> None:
        self.assertIn("printer is RUNNING, not idle", printer.plan(idle_report(gcode_state="RUNNING"), PETG, "auto").problems)
        self.assertTrue(printer.plan(idle_report(print_error=0x0300800A), PETG, "auto").problems)
        fatal = idle_report(hms=[{"attr": 0x05000100, "code": 0x00010001}])
        self.assertTrue(printer.plan(fatal, PETG, "auto").problems)
        info = idle_report(hms=[{"attr": 0x05000100, "code": 0x00040001}])
        self.assertEqual(printer.plan(info, PETG, "auto").problems, [])

    def test_blocks_wrong_material_or_nozzle(self) -> None:
        self.assertIn("no loaded spool holds ABS", printer.plan(
            idle_report(), printer.SliceFacts([1], ["ABS"], 0.4), "auto",
        ).problems)
        self.assertIn("AMS 1 slot 2 holds PLA, the slice needs PETG", printer.plan(idle_report(), PETG, [1]).problems)
        self.assertTrue(printer.plan(idle_report(nozzle_diameter="0.6"), PETG, "auto").problems)

    def test_external_spool_without_type_warns(self) -> None:
        checked = printer.plan(idle_report(), PETG, [printer.EXTERNAL])
        self.assertEqual((checked.mapping, checked.problems), ([printer.EXTERNAL], []))
        self.assertTrue(checked.warnings)

    def test_several_matches_warn_and_take_first(self) -> None:
        report = idle_report(vt_tray={"id": "254", "tray_type": "PETG", "tray_color": "000000FF"})
        checked = printer.plan(report, PETG, "auto")
        self.assertEqual(checked.mapping, [0])
        self.assertTrue(checked.warnings)


if __name__ == "__main__":
    unittest.main()
