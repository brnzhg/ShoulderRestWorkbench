"""A Bambu Lab printer on the LAN: discovery, status, camera, upload and print control.

The printer speaks three local protocols, all over TLS with its self-signed
certificate, user ``bblp`` and the access code shown on its screen:

- MQTT on port 8883: ``device/<serial>/report`` streams status (P1 printers
  send only what changed, so the state here is merged from every report) and
  ``device/<serial>/request`` takes commands.
- Implicit FTPS on port 990: the SD card, where print files are uploaded.
- A JPEG stream on port 6000 (P1 and A1 cameras).

Status and the camera work in any network mode. Starting, pausing and
stopping prints from here needs LAN-only Developer Mode, because current
firmware ignores unsigned control commands otherwise.

Connection details are machine-local, never committed: ``BAMBU_HOST``,
``BAMBU_SERIAL`` and ``BAMBU_ACCESS_CODE``, or ``printer.local.toml`` with
``host``, ``serial`` and ``access_code``. Without a host the printer is found
by its discovery broadcast; without an access code, Bambu Studio's saved one
is used.
"""

import copy
import ftplib
import json
import os
import socket
import ssl
import struct
import threading
import time
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from . import paths, settings

MQTT_PORT = 8883
FTPS_PORT = 990
CAMERA_PORT = 6000
DISCOVERY_PORT = 2021
USER = "bblp"
BAMBU_STUDIO_CONFIG = Path("~/.var/app/com.bambulab.BambuStudio/config/BambuStudio/BambuStudio.conf")

READY_STATES = {"IDLE", "FINISH", "FAILED"}
"""States in which the printer takes a new job."""
ACTIVE_STATES = {"PREPARE", "SLICING", "RUNNING", "PAUSE"}

STAGES = {
    0: "printing", 1: "auto bed leveling", 2: "heatbed preheating", 3: "sweeping XY mech mode",
    4: "changing filament", 5: "M400 pause", 6: "paused: filament runout", 7: "heating hotend",
    8: "calibrating extrusion", 9: "scanning bed surface", 10: "inspecting first layer",
    11: "identifying build plate type", 12: "calibrating micro lidar", 13: "homing toolhead",
    14: "cleaning nozzle tip", 15: "checking extruder temperature", 16: "paused by user",
    17: "paused: front cover falling", 18: "calibrating lidar", 19: "calibrating extrusion flow",
    20: "paused: nozzle temperature malfunction", 21: "paused: heatbed temperature malfunction",
    22: "unloading filament", 23: "paused: skipped step", 24: "loading filament",
    25: "calibrating motor noise", 26: "paused: AMS lost", 27: "paused: low heat break fan speed",
    28: "paused: chamber temperature control error", 29: "cooling chamber", 30: "paused by G-code",
    31: "motor noise showoff", 32: "paused: nozzle filament covered", 33: "paused: cutter error",
    34: "paused: first layer error", 35: "paused: nozzle clog", 255: "idle",
}
HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}


# Connection ---------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    host: str
    serial: str
    access_code: str


@dataclass(frozen=True)
class Announcement:
    """One printer's discovery broadcast."""

    host: str
    serial: str
    name: str
    model: str
    firmware: str
    mode: str
    """``cloud`` or ``lan``."""


def discover(timeout: float = 15.0, serial: str = "") -> list[Announcement]:
    """Printers announcing themselves on the LAN within ``timeout`` seconds (each does every few).

    With ``serial``, returns as soon as that printer is heard.
    """
    found: dict[str, Announcement] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, (address, _) = sock.recvfrom(4096)
            except TimeoutError:
                break
            announcement = parse_announcement(data.decode(errors="replace"), address)
            if announcement is not None:
                found[announcement.serial] = announcement
            if serial and serial in found:
                break
    return list(found.values())


def parse_announcement(text: str, address: str) -> Announcement | None:
    headers = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if "bambulab" not in headers.get("nt", "") or not headers.get("usn"):
        return None
    return Announcement(
        host=headers.get("location", address), serial=headers["usn"],
        name=headers.get("devname.bambu.com", ""), model=headers.get("devmodel.bambu.com", ""),
        firmware=headers.get("devversion.bambu.com", ""), mode=headers.get("devconnect.bambu.com", ""),
    )


def _local_settings() -> dict[str, Any]:
    path = paths.root() / "printer.local.toml"
    return tomllib.loads(path.read_text()) if path.is_file() else {}


def _studio_access_code(serial: str) -> str | None:
    path = BAMBU_STUDIO_CONFIG.expanduser()
    if not serial or not path.is_file():
        return None
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    for key in ("user_access_code", "access_code"):
        code = config.get(key, {}).get(serial)
        if code:
            return str(code)
    return None


def connection() -> Connection:
    local = _local_settings()
    host = os.environ.get("BAMBU_HOST") or local.get("host", "")
    serial = os.environ.get("BAMBU_SERIAL") or local.get("serial", "")
    if not host or not serial:
        printers = [p for p in discover(serial=serial) if not serial or p.serial == serial]
        if len(printers) != 1:
            which = f"printer {serial}" if serial else "exactly one printer"
            raise RuntimeError(
                f"Could not find {which} on the LAN ({len(printers)} announced); "
                "set host and serial in printer.local.toml or BAMBU_HOST and BAMBU_SERIAL"
            )
        host, serial = host or printers[0].host, printers[0].serial
    code = os.environ.get("BAMBU_ACCESS_CODE") or local.get("access_code") or _studio_access_code(serial)
    if not code:
        raise RuntimeError(
            "No access code: set access_code in printer.local.toml or BAMBU_ACCESS_CODE "
            "(shown on the printer under Settings > LAN Only)"
        )
    return Connection(host, serial, str(code))


def _tls_context() -> ssl.SSLContext:
    """The printer's certificate is self-signed per device, so it is not verified."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# Status -------------------------------------------------------------------


def merge_report(state: dict[str, Any], report: dict[str, Any]) -> None:
    """Fold a (possibly partial) report into ``state`` in place."""
    for key, value in report.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            merge_report(state[key], value)
        else:
            state[key] = copy.deepcopy(value)


def hms_code(attr: int, code: int) -> str:
    """An HMS alert as Bambu writes it, such as ``0300_0100_0001_0007``."""
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def error_code(value: int) -> str:
    return f"{value >> 16:04X}_{value & 0xFFFF:04X}"


@dataclass(frozen=True)
class Spool:
    source: str
    """``external`` or ``AMS <unit> slot <tray>`` (1-based)."""
    slot: int
    """The ``ams_mapping`` value for this spool: 4 * unit + tray (0-based), 254 for external."""
    filament_type: str
    color: str
    remaining_percent: int | None


def spools(report: dict[str, Any]) -> list[Spool]:
    found = []
    external = report.get("vt_tray") or {}
    found.append(Spool(
        "external", 254, external.get("tray_type", ""), external.get("tray_color", ""), None,
    ))
    for unit in (report.get("ams") or {}).get("ams", []) or []:
        for tray in unit.get("tray", []) or []:
            if not tray.get("tray_type"):
                continue
            unit_id, tray_id = int(unit.get("id", 0)), int(tray.get("id", 0))
            remain = tray.get("remain")
            found.append(Spool(
                f"AMS {unit_id + 1} slot {tray_id + 1}", 4 * unit_id + tray_id,
                tray.get("tray_type", ""), tray.get("tray_color", ""),
                int(remain) if isinstance(remain, int) and remain >= 0 else None,
            ))
    return found


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """The parts of a merged ``print`` report worth showing, in plain units."""
    stage = report.get("stg_cur")
    summary: dict[str, Any] = {
        "state": report.get("gcode_state", "UNKNOWN"),
        "stage": STAGES.get(stage, str(stage)) if isinstance(stage, int) else None,
        "job": report.get("subtask_name") or report.get("gcode_file") or None,
        "percent": report.get("mc_percent"),
        "remaining_minutes": report.get("mc_remaining_time"),
        "layer": [report.get("layer_num"), report.get("total_layer_num")],
        "nozzle_c": [report.get("nozzle_temper"), report.get("nozzle_target_temper")],
        "bed_c": [report.get("bed_temper"), report.get("bed_target_temper")],
        "chamber_c": report.get("chamber_temper"),
        "nozzle_mm": report.get("nozzle_diameter"),
        "speed_level": report.get("spd_lvl"),
        "error": error_code(report["print_error"]) if report.get("print_error") else None,
        "hms": [
            {"code": hms_code(h["attr"], h["code"]),
             "severity": HMS_SEVERITY.get(h["code"] >> 16, "unknown")}
            for h in report.get("hms", []) or [] if "attr" in h and "code" in h
        ],
        "spools": [s.__dict__ for s in spools(report)],
        "lights": {
            light.get("node"): light.get("mode") for light in report.get("lights_report", []) or []
        },
    }
    return summary


def status_lines(summary: dict[str, Any]) -> list[str]:
    lines = [f"state: {summary['state']}" + (f" ({summary['stage']})" if summary["stage"] else "")]
    if summary["job"]:
        layer, total = summary["layer"]
        lines.append(
            f"job: {summary['job']}, {summary['percent']}%, layer {layer}/{total}, "
            f"{summary['remaining_minutes']} min left"
        )
    nozzle, bed = summary["nozzle_c"], summary["bed_c"]
    lines.append(f"nozzle {nozzle[0]}/{nozzle[1]} C, bed {bed[0]}/{bed[1]} C, chamber {summary['chamber_c']} C")
    for spool in summary["spools"]:
        if spool["filament_type"] or spool["source"] == "external":
            remain = f", {spool['remaining_percent']}% left" if spool["remaining_percent"] is not None else ""
            lines.append(
                f"spool {spool['source']} (mapping {spool['slot']}): "
                f"{spool['filament_type'] or 'not set'} #{spool['color'][:6] or '?'}{remain}"
            )
    if summary["error"]:
        lines.append(f"print error {summary['error']}")
    for alert in summary["hms"]:
        lines.append(f"HMS {alert['code']} ({alert['severity']})")
    return lines


# MQTT ---------------------------------------------------------------------


class Printer:
    """An MQTT session with the printer; use as a context manager."""

    def __init__(self, conn: Connection, timeout: float = 20.0):
        import paho.mqtt.client as mqtt

        self.conn = conn
        self.timeout = timeout
        self.state: dict[str, Any] = {}
        self._changed = threading.Condition()
        self._connected = threading.Event()
        self._refused: str | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"srw-{os.getpid()}")
        self._client.username_pw_set(USER, conn.access_code)
        self._client.tls_set_context(_tls_context())
        self._client.tls_insecure_set(True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    @property
    def report_topic(self) -> str:
        return f"device/{self.conn.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.conn.serial}/request"

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason: Any, properties: Any) -> None:
        if reason.is_failure:
            self._refused = str(reason)
        else:
            client.subscribe(self.report_topic)
        self._connected.set()

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            report = json.loads(message.payload)
        except json.JSONDecodeError:
            return
        with self._changed:
            merge_report(self.state, report)
            self._changed.notify_all()

    def __enter__(self) -> Self:
        try:
            self._client.connect(self.conn.host, MQTT_PORT, keepalive=60)
        except OSError as error:
            raise RuntimeError(f"Cannot reach the printer at {self.conn.host}:{MQTT_PORT}: {error}") from error
        self._client.loop_start()
        if not self._connected.wait(self.timeout) or self._refused:
            self.close()
            raise RuntimeError(
                f"Printer refused MQTT ({self._refused or 'timed out'}); check the access code and that LAN access is on"
            )
        self.refresh()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, payload: dict[str, Any]) -> None:
        info = self._client.publish(self.request_topic, json.dumps(payload))
        info.wait_for_publish(self.timeout)
        if not info.is_published():
            raise RuntimeError("Command was not delivered to the printer")

    def refresh(self) -> dict[str, Any]:
        """Ask for a full report and wait for it."""
        with self._changed:
            self.state.get("print", {}).pop("gcode_state", None)
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
        if self.wait_for(lambda report: "gcode_state" in report, self.timeout) is None:
            raise RuntimeError("Printer sent no status; is the serial number right?")
        return self.report

    @property
    def report(self) -> dict[str, Any]:
        with self._changed:
            return copy.deepcopy(self.state.get("print", {}))

    def wait_for(self, predicate: Callable[[dict[str, Any]], bool], timeout: float) -> dict[str, Any] | None:
        """The ``print`` report once ``predicate`` holds, or None after ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                report = self.state.get("print", {})
                if predicate(report):
                    return copy.deepcopy(report)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._changed.wait(remaining)

    def command(self, name: str, **fields: Any) -> None:
        self.publish({"print": {"sequence_id": "0", "command": name, **fields}})

    def set_light(self, on: bool) -> None:
        self.publish({"system": {
            "sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
            "led_mode": "on" if on else "off",
            "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0,
        }})


@contextmanager
def session(timeout: float = 20.0) -> Iterator[Printer]:
    with Printer(connection(), timeout) as printer:
        yield printer


# Files --------------------------------------------------------------------


class _ImplicitFTPS(ftplib.FTP_TLS):
    """FTPS with TLS from the first byte, reusing the control session for data as the printer requires."""

    def __init__(self, timeout: float) -> None:
        super().__init__(context=_tls_context(), timeout=timeout)
        self._sock: socket.socket | None = None

    @property  # type: ignore[override]
    def sock(self) -> socket.socket | None:
        return self._sock

    @sock.setter
    def sock(self, value: socket.socket | None) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd: str, rest: int | str | None = None) -> tuple[socket.socket, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # type: ignore[attr-defined]
            session = self.sock.session if isinstance(self.sock, ssl.SSLSocket) else None
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
        return conn, size

    def store(self, name: str, data: bytes) -> None:
        self.voidcmd("TYPE I")
        conn = self.transfercmd(f"STOR {name}")
        try:
            view = memoryview(data)
            for start in range(0, len(data), 65536):
                conn.sendall(view[start:start + 65536])
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()  # Some firmware waits for the TLS close before replying.
                except (ssl.SSLError, OSError):
                    pass
        finally:
            conn.close()
        self.voidresp()


def upload(conn: Connection, file: Path, name: str, timeout: float = 120.0) -> None:
    """Copy ``file`` to the printer's SD card root as ``name`` and check its size there."""
    data = file.read_bytes()
    ftp = _ImplicitFTPS(timeout)
    try:
        ftp.connect(conn.host, FTPS_PORT)
        ftp.login(USER, conn.access_code)
        ftp.prot_p()
        ftp.store(name, data)
        try:
            size = ftp.size(name)
        except ftplib.error_perm:
            size = None
        if size is not None and size != len(data):
            raise RuntimeError(f"Upload of {name} is {size} bytes on the printer, expected {len(data)}")
    except (OSError, ftplib.Error) as error:
        raise RuntimeError(f"Upload to {conn.host} failed: {error}") from error
    finally:
        try:
            ftp.quit()
        except (OSError, ftplib.Error, AttributeError):
            ftp.close()


# Camera -------------------------------------------------------------------


def camera_auth(access_code: str) -> bytes:
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + USER.encode().ljust(32, b"\0") + access_code.encode().ljust(32, b"\0")


def snapshot(conn: Connection, timeout: float = 20.0) -> bytes:
    """One JPEG frame from the printer's camera (P1 and A1 series)."""

    def read(sock: ssl.SSLSocket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("Camera closed the connection; check the access code and LAN liveview")
            data += chunk
        return bytes(data)

    try:
        with (
            socket.create_connection((conn.host, CAMERA_PORT), timeout=timeout) as raw,
            _tls_context().wrap_socket(raw, server_hostname=conn.host) as sock,
        ):
            sock.sendall(camera_auth(conn.access_code))
            for _ in range(5):
                header = read(sock, 16)
                size = struct.unpack("<I", header[:4])[0]
                frame = read(sock, size)
                if frame[:2] == b"\xff\xd8" and frame[-2:] == b"\xff\xd9":
                    return frame
    except OSError as error:
        raise RuntimeError(f"Camera at {conn.host}:{CAMERA_PORT} failed: {error}") from error
    raise RuntimeError("Camera sent no complete JPEG frame")


# Printing -----------------------------------------------------------------

EXTERNAL = 254
"""The ``ams_mapping`` slot of the external spool holder."""


@dataclass(frozen=True)
class JobSettings:
    spools: str | list[int]
    """``"auto"`` to match the slice's filament types to loaded spools, or slots per filament."""
    file_url: str
    bed_leveling: bool
    flow_calibration: bool
    vibration_calibration: bool
    timelapse: bool

    @classmethod
    def load(cls) -> "JobSettings":
        table = settings.settings().get("printer", {})
        spools = table.get("spools", "auto")
        return cls(
            spools=spools if isinstance(spools, str) else [int(v) for v in spools],
            file_url=table.get("file_url", "file:///sdcard/{file}"),
            bed_leveling=bool(table.get("bed_leveling", True)),
            flow_calibration=bool(table.get("flow_calibration", True)),
            vibration_calibration=bool(table.get("vibration_calibration", True)),
            timelapse=bool(table.get("timelapse", False)),
        )


def print_command(file_name: str, job: JobSettings, mapping: list[int], plate: int = 1) -> dict[str, Any]:
    """The ``project_file`` command Bambu Studio sends to start a LAN print."""
    use_ams = any(slot != EXTERNAL for slot in mapping)
    return {
        "command": "project_file",
        "param": f"Metadata/plate_{plate}.gcode",
        "url": job.file_url.format(file=file_name),
        "subtask_name": file_name.removesuffix(".gcode.3mf"),
        "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
        "bed_type": "auto",
        "timelapse": job.timelapse,
        "bed_leveling": job.bed_leveling,
        "auto_bed_leveling": int(job.bed_leveling),
        "flow_cali": job.flow_calibration,
        "extrude_cali_flag": int(job.flow_calibration),
        "vibration_cali": job.vibration_calibration,
        "layer_inspect": False,
        "use_ams": use_ams,
        "ams_mapping": mapping if use_ams else [0],
    }


@dataclass(frozen=True)
class SliceFacts:
    """What a sliced file needs from the printer."""

    plates: list[int]
    filament_types: list[str]
    nozzle_mm: float | None


def slice_facts(file: Path) -> SliceFacts:
    with zipfile.ZipFile(file) as archive:
        names = archive.namelist()
        project = json.loads(archive.read("Metadata/project_settings.config"))
    plates = sorted(
        int(n.removeprefix("Metadata/plate_").removesuffix(".gcode"))
        for n in names if n.startswith("Metadata/plate_") and n.endswith(".gcode")
    )
    nozzle = project.get("nozzle_diameter", [None])
    return SliceFacts(
        plates=plates,
        filament_types=[str(t) for t in project.get("filament_type", [])],
        nozzle_mm=float(nozzle[0]) if nozzle and nozzle[0] is not None else None,
    )


@dataclass
class Plan:
    """A checked print job: the spool for each filament, and what stands in the way."""

    mapping: list[int]
    spools: list[Spool]
    problems: list[str]
    warnings: list[str]

    @property
    def material(self) -> str:
        return ", ".join(f"{s.filament_type} #{s.color[:6]} ({s.source})" for s in self.spools)


def plan(report: dict[str, Any], facts: SliceFacts, wanted: str | list[int]) -> Plan:
    """Check the printer can take the slice now, and choose spools for it."""
    result = Plan([], [], [], [])
    state = report.get("gcode_state", "UNKNOWN")
    if state not in READY_STATES:
        result.problems.append(f"printer is {state}, not idle")
    if report.get("print_error"):
        result.problems.append(f"printer reports error {error_code(report['print_error'])}")
    for alert in summarize(report)["hms"]:
        serious = alert["severity"] in ("fatal", "serious")
        (result.problems if serious else result.warnings).append(f"HMS {alert['code']} ({alert['severity']})")
    if report.get("sdcard") is False:
        result.problems.append("no SD card in the printer")
    if not facts.plates:
        result.problems.append("sliced file has no plate G-code")
    nozzle = report.get("nozzle_diameter")
    if facts.nozzle_mm and nozzle:
        try:
            if abs(float(nozzle) - facts.nozzle_mm) > 1e-6:
                result.problems.append(f"printer has a {nozzle} mm nozzle, slice is for {facts.nozzle_mm} mm")
        except ValueError:
            result.warnings.append(f"cannot read the printer's nozzle diameter {nozzle!r}")

    loaded = spools(report)
    by_slot = {s.slot: s for s in loaded}
    if wanted != "auto" and (not isinstance(wanted, list) or len(wanted) != len(facts.filament_types)):
        result.problems.append(f"spools {wanted!r} do not give one slot per filament {facts.filament_types}")
        return result
    for index, filament in enumerate(facts.filament_types):
        if isinstance(wanted, list):
            spool = by_slot.get(wanted[index])
            if spool is None:
                result.problems.append(f"no spool in slot {wanted[index]}")
                continue
            if not spool.filament_type:
                result.warnings.append(f"{spool.source} has no filament type set; the slice needs {filament}")
            elif spool.filament_type.upper() != filament.upper():
                result.problems.append(f"{spool.source} holds {spool.filament_type}, the slice needs {filament}")
        else:
            matches = sorted(
                (s for s in loaded if s.filament_type.upper() == filament.upper()),
                key=lambda s: s.slot,  # AMS slots before the external spool (254)
            )
            if not matches:
                result.problems.append(f"no loaded spool holds {filament}")
                continue
            spool = matches[0]
            if len(matches) > 1:
                others = ", ".join(f"{s.source} #{s.color[:6]} (slot {s.slot})" for s in matches[1:])
                result.warnings.append(f"several {filament} spools; using {spool.source}, not {others}")
        result.mapping.append(spool.slot)
        result.spools.append(spool)
    if EXTERNAL in result.mapping and len(set(result.mapping)) > 1:
        result.problems.append("a print cannot mix the external spool with AMS slots")
    return result


def start_print(
    printer: Printer, file: Path, name: str, job: JobSettings, mapping: list[int], timeout: float = 120.0,
) -> dict[str, Any]:
    """Upload a sliced file, start it, and return the report once the printer has taken it."""
    upload(printer.conn, file, name)
    printer.command(**print_command(name, job, mapping))
    stem = name.removesuffix(".gcode.3mf")

    def taken(report: dict[str, Any]) -> bool:
        job_name = str(report.get("subtask_name") or report.get("gcode_file") or "")
        return (report.get("gcode_state") in ACTIVE_STATES and stem in job_name) or bool(report.get("print_error"))

    report = printer.wait_for(taken, timeout)
    if report is None:
        raise RuntimeError(
            f"Printer did not start {name} within {timeout:.0f} s; is it in LAN-only Developer Mode?"
        )
    if report.get("print_error") and report.get("gcode_state") not in ACTIVE_STATES:
        raise RuntimeError(f"Printer rejected {name} with error {error_code(report['print_error'])}")
    return report
