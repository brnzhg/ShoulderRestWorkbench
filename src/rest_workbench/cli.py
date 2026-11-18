"""``srw``: the shoulder rest workbench command line. Run ``srw --help``."""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path
from typing import Any

import tomlkit

from . import catalog, versions
from .config import Table, deep_merge


def _triple(text: str) -> list[float]:
    values = [float(v) for v in text.split(",")]
    if len(values) != 3:
        raise argparse.ArgumentTypeError("expected x,y,z")
    return values


def cmd_list(args: argparse.Namespace) -> None:
    print("\n".join(catalog.tree_lines()) or "No versions yet")


def cmd_info(args: argparse.Namespace) -> None:
    resolved = versions.resolve(args.version)
    print(f"# {resolved.id}: {resolved.version.summary}")
    print(f"# lineage: {' -> '.join(v.id for v in resolved.chain)}")
    table = resolved.design.table if args.full else resolved.design_table
    print(tomlkit.dumps(_tomlable(table)))
    print("# placement steps")
    for step in resolved.steps:
        print(f"#   {json.dumps(step)}")
    from .placement import describe
    print(f"# rest pose on shoulder: {describe(resolved.rest_pose())}")
    for record in resolved.version.prints:
        print(f"# print {record.get('date')}: {record.get('parts')} {record.get('scores', '')}")


def _tomlable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _tomlable(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_tomlable(v) for v in value]
    return value


def cmd_new(args: argparse.Namespace) -> None:
    adjust: Table = {}
    if args.shift:
        adjust["shift"] = args.shift
    if args.rotate:
        adjust["rotate"] = args.rotate
    if args.pivot:
        adjust["pivot"] = _triple(args.pivot) if "," in args.pivot else args.pivot
    overrides: Table = {}
    for assignment in args.set or []:
        overrides = deep_merge(overrides, versions.parse_assignment(assignment))
    version = versions.create(
        args.parent, summary=args.message, version_id=args.id,
        adjust_table=adjust or None, overrides=overrides,
    )
    print(f"Created {version.path.relative_to(Path.cwd()) if version.path.is_relative_to(Path.cwd()) else version.path}")


def cmd_build(args: argparse.Namespace) -> None:
    from .build import build

    for version_id in args.versions:
        result = build(versions.resolve(version_id), force=args.force, renders=not args.no_render)
        state = "up to date" if result.skipped else f"built in {result.manifest['build_seconds']} s"
        print(f"{version_id}: {state} -> {result.directory}")
        geometry = result.manifest["geometry"]
        print(f"  rest {geometry['rest_volume_cm3']} cm3, print size {geometry['rest_print_size_mm']} mm")


def cmd_render(args: argparse.Namespace) -> None:
    from shoulder_rest.render import render

    from .build import build_dir, construct

    resolved = versions.resolve(args.version)
    fitted = construct(resolved)
    shapes = [fitted.scene((args.left_angle, args.right_angle))]
    if args.housings:
        fitted.rest.add(fitted.rest.left.housing)
        fitted.rest.add(fitted.rest.right.housing)
    output = args.output or build_dir(resolved.id) / "renders" / f"{args.name}.png"
    legend = render(
        shapes, output, views=args.views.split(","),
        hide=[w for w in args.hide.split(",") if w], ghost=[w for w in args.ghost.split(",") if w],
        frame=args.frame,
    )
    print(", ".join(name for name, _ in legend))
    print(f"Wrote {output}")


def cmd_compare(args: argparse.Namespace) -> None:
    from . import paths
    from .build import compare

    resolved = [versions.resolve(v) for v in args.versions]
    output = args.output or paths.builds_dir() / "compare" / f"{'-'.join(args.versions)}.png"
    compare(
        resolved, output, views=tuple(args.views.split(",")), shoulder_frame=args.shoulder_frame,
    )
    print(f"Wrote {output} ({', '.join(args.versions[:-1])} ghosted)")


def cmd_view(args: argparse.Namespace) -> None:
    from ocp_vscode import show

    from .build import construct

    fitted = construct(versions.resolve(args.version))
    show(fitted.scene((args.left_angle, args.right_angle)))


def cmd_slice(args: argparse.Namespace) -> None:
    from . import settings
    from .build import build
    from .slicing import SlicerProfile, slice_part

    built = build(versions.resolve(args.version))
    for part in args.parts.split(","):
        profile = SlicerProfile.load(args.profile or settings.default_profile(part))
        result = slice_part(built, part, profile)
        s = result.summary
        print(f"{part} ({profile.name}): {s['print_minutes']} min, {s['filament_g']} g -> {result.output}")


def cmd_open(args: argparse.Namespace) -> None:
    from .build import existing
    from .slicing import latest_slice, open_in_gui

    built = existing(args.version)
    result = latest_slice(built, args.part) if built else None
    if result is None:
        sys.exit(f"No current slice of {args.version} {args.part}; run srw slice first")
    open_in_gui(result.output)
    print(f"Opening {result.output} in Bambu Studio")


def _print_record(version: str, parts: list[str], date: datetime.date) -> Table:
    """A ``[[prints]]`` entry with the build's commits and the slices printed."""
    from .build import existing
    from .slicing import latest_slice

    built = existing(version)
    record: Table = {"date": date, "parts": parts}
    if built is not None:
        provenance = built.manifest["provenance"]
        record["library_commit"] = provenance["library"]["commit"][:12]
        record["workbench_commit"] = provenance["workbench"]["commit"][:12]
        found = [s for s in (latest_slice(built, part) for part in parts) if s is not None]
        if found:
            record["slicer_profiles"] = [s.profile for s in found]
            record["gcode_sha256"] = [s.summary["sha256"][:16] for s in found]
            record["filament_g"] = round(sum(s.summary["filament_g"] for s in found), 1)
            record["slicer_version"] = found[0].summary.get("slicer_version", "unknown")
    return record


def cmd_printed(args: argparse.Namespace) -> None:
    date = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    record = _print_record(args.version, args.parts.split(","), date)
    if args.material:
        record["material"] = args.material
    record["feedback"] = args.feedback or ""
    versions.record_print(args.version, record)
    print(f"Recorded a print of {args.version}; add feedback and [prints.scores] in its file")


def cmd_print(args: argparse.Namespace) -> None:
    from . import printer
    from .build import existing
    from .slicing import latest_slice

    built = existing(args.version)
    sliced = latest_slice(built, args.part) if built else None
    if sliced is None:
        sys.exit(f"No current slice of {args.version} {args.part}; run srw slice first")
    job = printer.JobSettings.load()
    facts = printer.slice_facts(sliced.output)
    with printer.session() as session:
        checked = printer.plan(session.report, facts, args.spool or job.spools)
        for warning in checked.warnings:
            print(f"warning: {warning}")
        if checked.problems:
            sys.exit("Not printing:\n  " + "\n  ".join(checked.problems))
        s = sliced.summary
        print(f"{sliced.output.name}: {s['print_minutes']} min, {s['filament_g']} g of {checked.material}")
        if args.dry_run:
            print("Dry run: checks passed, nothing sent")
            return
        report = printer.start_print(session, sliced.output, sliced.output.name, job, checked.mapping)
    print(f"Started: printer is {report.get('gcode_state')}")
    record = _print_record(args.version, [args.part], datetime.date.today())
    record["started"] = datetime.datetime.now().replace(microsecond=0)
    record["printer_job"] = sliced.output.name.removesuffix(".gcode.3mf")
    record["material"] = checked.material
    record["outcome"] = "started"
    record["feedback"] = ""
    versions.record_print(args.version, record)
    print(f"Recorded the print in {args.version}; follow it with srw printer watch")


def _save_snapshot(conn: Any, output: Path) -> Path:
    from . import printer

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(printer.snapshot(conn))
    return output


def cmd_printer(args: argparse.Namespace) -> None:
    from . import paths, printer

    if args.action == "discover":
        found = printer.discover(args.timeout)
        for p in found:
            print(f"{p.name} {p.model} serial {p.serial} at {p.host}, firmware {p.firmware}, mode {p.mode}")
        if not found:
            print("No printer announced itself")
        return
    with printer.session() as session:
        if args.action == "status":
            summary = printer.summarize(session.report)
            print(json.dumps(summary, indent=1) if args.json else "\n".join(printer.status_lines(summary)))
        elif args.action == "snapshot":
            if args.light and printer.summarize(session.report)["lights"].get("chamber_light") != "on":
                session.set_light(True)
                time.sleep(3)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            output = args.output or paths.builds_dir() / "printer" / f"snapshot-{stamp}.jpg"
            print(f"Wrote {_save_snapshot(session.conn, output)}")
        elif args.action in ("pause", "resume", "stop"):
            session.command(args.action)
            wanted = {"pause": {"PAUSE"}, "resume": {"RUNNING", "PREPARE"}, "stop": {"FAILED", "IDLE", "FINISH"}}
            report = session.wait_for(lambda r: r.get("gcode_state") in wanted[args.action], 30)
            if report is None:
                sys.exit(f"Printer did not {args.action} within 30 s; is it in LAN-only Developer Mode?")
            print(f"Printer is {report['gcode_state']}")
        elif args.action == "light":
            session.set_light(args.state == "on")
            print(f"Chamber light {args.state}")
        elif args.action == "watch":
            sys.exit(_watch(session, args))


def _watch(session: Any, args: argparse.Namespace) -> int:
    """Follow the current job until it ends; 0 finished, 1 failed, 3 paused, 4 nothing printing."""
    from . import paths, printer

    report = session.report
    if report.get("gcode_state") not in printer.ACTIVE_STATES:
        print("\n".join(printer.status_lines(printer.summarize(report))))
        print("Nothing is printing")
        return 4
    job = str(report.get("subtask_name") or "job")
    folder = paths.builds_dir() / "printer" / job
    deadline = time.monotonic() + args.timeout * 3600
    next_snapshot = time.monotonic()
    while True:
        state = report.get("gcode_state")
        summary = printer.summarize(report)
        stamp = datetime.datetime.now().strftime("%H:%M")
        print(f"{stamp} {'; '.join(printer.status_lines(summary)[:2])}", flush=True)
        if args.snapshot_every and (time.monotonic() >= next_snapshot or state not in ("RUNNING", "PREPARE")):
            try:
                path = _save_snapshot(session.conn, folder / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.jpg")
                (folder.parent / "latest.jpg").write_bytes(path.read_bytes())
            except RuntimeError as error:
                print(f"snapshot failed: {error}")
            next_snapshot = time.monotonic() + args.snapshot_every * 60
        if state in ("FINISH", "FAILED", "IDLE"):
            outcome = "finished" if state == "FINISH" else "failed"
            _record_outcome(job, outcome, summary)
            print(f"{job} {outcome}")
            return 0 if state == "FINISH" else 1
        if state == "PAUSE":
            print(f"{job} paused: {summary['stage']}, error {summary['error']}; needs attention")
            return 3
        if time.monotonic() > deadline:
            print("Stopped watching (timeout); the print continues")
            return 0
        report = session.wait_for(
            lambda r, before=state: r.get("gcode_state") != before, args.interval * 60,
        ) or session.report


def _record_outcome(job: str, outcome: str, summary: dict[str, Any]) -> None:
    """Fill in ``outcome`` on the version's print record for this job, if srw started it."""
    version = job.split("-", 1)[0]
    try:
        updated = versions.update_print(
            version, lambda r: r.get("printer_job") == job and r.get("outcome") == "started",
            {"outcome": outcome, "ended": datetime.datetime.now().replace(microsecond=0)}
            | ({"printer_error": summary["error"]} if summary["error"] else {}),
        )
    except ValueError:
        return
    if updated:
        print(f"Recorded outcome {outcome!r} in {version}")


def cmd_table(args: argparse.Namespace) -> None:
    rows = catalog.dataset(args.versions or None, varying=args.varying)
    text = json.dumps(rows, indent=1, default=str) if args.json else catalog.to_csv(rows)
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {len(rows)} rows to {args.output}")
    else:
        print(text, end="")


def cmd_diff(args: argparse.Namespace) -> None:
    result = catalog.diff(args.a, args.b, pivot=args.pivot)
    for key, (before, after) in result["parameters"].items():
        print(f"{key}: {before} -> {after}")
    if not result["parameters"]:
        print("No design parameter changes")
    move = result["move"]
    print(f"rest move in {args.a}'s frame about {move['pivot']}: "
          f"shift {move['shift']} mm, rotate {move['rotate']} deg")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="srw", description=__doc__)
    commands = root.add_subparsers(required=True, metavar="command")

    def command(name: str, handler: Any, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.set_defaults(handler=handler)
        return sub

    command("list", cmd_list, "Show the version tree with prints, verdicts and scores")

    sub = command("info", cmd_info, "Show a version's merged design, placement and prints")
    sub.add_argument("version")
    sub.add_argument("--full", action="store_true", help="include library defaults")

    sub = command("new", cmd_new, "Create a version from a parent, with a rest-frame move and overrides")
    sub.add_argument("parent")
    sub.add_argument("-m", "--message", required=True, help="one-line summary of the change")
    sub.add_argument("--id", help="version id (default: next number)")
    sub.add_argument("--shift", type=_triple, help="mm along rest x,y,z")
    sub.add_argument("--rotate", type=_triple, help="degrees about rest x,y,z")
    sub.add_argument("--pivot", help="legs_midpoint, right_leg, left_leg, head_center, violin_mount, origin or x,y,z")
    sub.add_argument("--set", action="append", metavar="KEY=VALUE",
                     help="design override, e.g. rest.head.left_cap.angle=28 (repeatable)")

    sub = command("build", cmd_build, "Export STEP/STL, renders and a manifest to builds/<id>/")
    sub.add_argument("versions", nargs="+")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--no-render", action="store_true")

    sub = command("render", cmd_render, "Render a version's fitted scene with custom views")
    sub.add_argument("version")
    sub.add_argument("--name", default="custom", help="output name under builds/<id>/renders/")
    sub.add_argument("-o", "--output", type=Path)
    sub.add_argument("--views", default="iso,iso_below,top,bottom,front,right")
    sub.add_argument("--hide", default="violin", help="label substrings to skip")
    sub.add_argument("--ghost", default="", help="label substrings to draw translucent")
    sub.add_argument("--frame", help="label substring of the part whose frame the views use")
    sub.add_argument("--housings", action="store_true", help="add leg housing guides")
    sub.add_argument("--left-angle", type=float, default=0)
    sub.add_argument("--right-angle", type=float, default=0)

    sub = command("compare", cmd_compare, "Overlay rests on the shoulder; earlier ones ghosted")
    sub.add_argument("versions", nargs="+")
    sub.add_argument("-o", "--output", type=Path)
    sub.add_argument("--views", default="top,front,right,left,iso")
    sub.add_argument("--shoulder-frame", action="store_true",
                     help="view in the shoulder frame instead of the first rest's")

    sub = command("view", cmd_view, "Show a version's fitted scene in the OCP CAD Viewer")
    sub.add_argument("version")
    sub.add_argument("--left-angle", type=float, default=0)
    sub.add_argument("--right-angle", type=float, default=0)

    sub = command("slice", cmd_slice, "Build if needed, then slice parts with Bambu Studio")
    sub.add_argument("version")
    sub.add_argument("--parts", default="rest", help="comma-separated: rest, legs")
    sub.add_argument("--profile", help="slicer profile name in slicer/ (default per part)")

    sub = command("open", cmd_open, "Open the latest slice in Bambu Studio to send it to the printer")
    sub.add_argument("version")
    sub.add_argument("--part", default="rest")

    sub = command("printed", cmd_printed, "Record a print of a version, with its slice and commits")
    sub.add_argument("version")
    sub.add_argument("--parts", default="rest")
    sub.add_argument("--date", help="YYYY-MM-DD (default today)")
    sub.add_argument("--material", help="e.g. 'Bambu PETG Basic, black'")
    sub.add_argument("--feedback", help="first impressions; edit the file for more")

    sub = command("print", cmd_print, "Check the printer, upload the latest slice and start it (LAN Developer Mode)")
    sub.add_argument("version")
    sub.add_argument("--part", default="rest")
    sub.add_argument("--spool", type=int, action="append",
                     help="ams_mapping slot per filament: 4*unit+tray from 0, 254 external (default: match type)")
    sub.add_argument("--dry-run", action="store_true", help="run the checks, send nothing")

    sub = command("printer", cmd_printer, "Printer status, camera, light and job control over the LAN")
    actions = sub.add_subparsers(dest="action", required=True, metavar="action")
    sub_action = actions.add_parser("discover", help="list printers announcing themselves on the LAN")
    sub_action.add_argument("--timeout", type=float, default=15)
    sub_action = actions.add_parser("status", help="state, job progress, temperatures, spools, errors")
    sub_action.add_argument("--json", action="store_true")
    sub_action = actions.add_parser("snapshot", help="save a camera frame (default builds/printer/)")
    sub_action.add_argument("-o", "--output", type=Path)
    sub_action.add_argument("--no-light", dest="light", action="store_false",
                            help="do not turn on the chamber light first")
    actions.add_parser("pause", help="pause the current print")
    actions.add_parser("resume", help="resume a paused print")
    actions.add_parser("stop", help="cancel the current print")
    sub_action = actions.add_parser("light", help="turn the chamber light on or off")
    sub_action.add_argument("state", choices=["on", "off"])
    sub_action = actions.add_parser(
        "watch", help="follow the current print to its end; exit 0 finished, 1 failed, 3 paused, 4 idle",
    )
    sub_action.add_argument("--interval", type=float, default=10, help="minutes between status lines")
    sub_action.add_argument("--snapshot-every", type=float, default=15,
                            help="minutes between camera frames saved to builds/printer/<job>/ (0: none)")
    sub_action.add_argument("--timeout", type=float, default=24, help="hours before giving up watching")

    sub = command("table", cmd_table, "Dataset of every version: parameters, pose, scores")
    sub.add_argument("versions", nargs="*")
    sub.add_argument("--varying", action="store_true", help="only columns that differ")
    sub.add_argument("--json", action="store_true")
    sub.add_argument("-o", "--output")

    sub = command("diff", cmd_diff, "Parameter changes and rest move between two versions")
    sub.add_argument("a")
    sub.add_argument("b")
    sub.add_argument("--pivot", default="legs_midpoint")
    return root


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        args.handler(args)
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        sys.exit(f"error: {error}")
