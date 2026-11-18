# Shoulder Rest Workbench

Builds, slices and tracks versions of my 3D-printed violin shoulder rest. The geometry comes from the [ShoulderRest](https://github.com/brnzhg/ShoulderRest) library, a git submodule in `ShoulderRest/`. This repository holds the data specific to me: my shoulder scan, every rest version I have tried with its feedback, and my slicer settings.

## Setup

```sh
git clone --recurse-submodules <this repo>
git lfs pull && git -C ShoulderRest lfs pull
uv sync
```

Slicing uses [Bambu Studio](https://flathub.org/apps/com.bambulab.BambuStudio) from Flathub. Its command line and preset directory are set in `workbench.toml`.

Run commands with `uv run srw <command>` (or activate `.venv`). `srw --help` lists them.

## Concepts

- **Shoulder** (`shoulders/<name>/`): a scan's STEP casts and `shoulder.toml`, which levels the scan and sets the base point for placement angles.
- **Version** (`versions/vNNN.toml`): one rest design and its placement on the shoulder. A version names a `parent` and stores only what changed, so lineage records what was tried and why. The version id is engraved under the tail, so a printed prototype maps back to its file.
- **Slicer profile** (`slicer/<name>.toml`): Bambu Studio presets plus overrides. `workbench.toml` picks one per printable group (`rest`, `legs`). A print is a version sliced with a profile; the print record keeps both.
- **Build** (`builds/<id>/`, not committed): STEP and STL exports, renders, slices and a `manifest.json` with every parameter, the rest's pose on the shoulder and the commits that produced it.

## Version files

```toml
summary = "Turn the rest 2 deg toward the front about the right leg"
parent = "v001"
created = 2026-11-18

[adjust]                 # move the rest in its own frame, from the parent's placement
rotate = [0, 0, 2]       # degrees about rest x, y, z
shift = [0, 0, -1]       # mm along rest x, y, z
pivot = "right_leg"      # legs_midpoint (default), right_leg, left_leg, head_center, violin_mount, origin, or [x, y, z]

[rest.head.left_cap]     # any WRestParameters field; omitted fields are inherited
angle = 28

[[prints]]               # added by `srw printed`, then filled in by hand
date = 2026-11-19
parts = ["rest"]
feedback = """Head sits well; tail lifts off the collarbone."""
[prints.scores]
comfort = 3
stability = 4
```

The rest frame is the violin outline's: **x** runs along the rest toward the head end (the violin's right side), **y** toward the back edge (the violin's end button), and **z** up toward the violin, with z=0 on the leg-mounting face. Moving the rest moves the violin with it, so `shift = [0, 0, -1]` lowers the rest and violin 1 mm onto the shoulder, and the shoulder cut gets 1 mm deeper. Rotations about **x** tip the front and back edges, about **y** raise one end against the other, and about **z** turn the rest in its own plane.

A root version has an absolute `[placement]` instead: either fitted angles (`angle`, `level`, `roll`, `offset`, from the shoulder's `placement_base`) or a direct `position` and `rotation` of the violin mount frame. A child may also give `[placement]` to start over from an absolute pose.

Design tables mirror the library's parameter dataclasses: `[rest]` is `WRestParameters` (`[rest.path]`, `[rest.head]`, `[rest.head.left_cap]`, `[rest.head.stripes]`, `[rest.tail]`), and `[legs.leg]`, `[legs.cavity]` and `[legs.rod_retention]` are the hinge leg's. Set a cap, stripes, rod retention or the engraving to `false` to turn it off. `srw info vNNN --full` prints every value, including library defaults. See `src/rest_workbench/design.py` for the extra workbench keys.

## Iterating

```sh
srw list                                   # version tree, prints, verdicts, scores
srw new v001 -m "Head 2 deg toward front" --rotate 0,0,2 --pivot right_leg
srw new v002 -m "Steeper left cap" --set rest.head.left_cap.angle=34
srw build v003                             # builds/v003/: rest.stl, fit.png, rest.png, ...
srw compare v002 v003                      # overlay in v002's frame, v002 ghosted
srw diff v002 v003                         # parameter changes and the rest's move
srw slice v003                             # --parts rest,legs; --profile petg-rest
srw print v003                             # start it on the printer (or srw open, then Print)
srw printed v003 --material "PETG Basic, black"   # record a print started by hand
srw table --varying -o builds/dataset.csv  # every version: parameters, pose, scores
```

`srw render vNNN --views ... --ghost shoulder` renders other views, and `srw view vNNN` opens the fitted scene in the OCP CAD Viewer.

## Slicing

Slicing calls the Bambu Studio command line directly. It is already installed, matches the presets I print with in the GUI, and writes Bambu `.gcode.3mf` files the P1S prints as-is. The command line ignores preset inheritance, so the workbench flattens presets first and then checks every override against the sliced project's settings, which catches misspelled keys. Each slice records the Bambu Studio version that made it.

OrcaSlicer (alone or through [estampo](https://github.com/estampo/estampo), which runs it in Docker) would add little here: `srw print` sends jobs itself, and Bambu Studio can still open a slice to check it (`srw open`).

## Printing

`srw print` sends the latest slice of a version to the printer over the LAN and starts it, so agents can run prints while I'm away. Starting, pausing and stopping need the printer in **LAN-only Developer Mode** (on the printer: Settings > Network > LAN Only, then Developer Mode). Current firmware ignores unsigned control commands from anything but Bambu's apps otherwise. That mode cuts the printer off from Bambu's cloud and Bambu Handy; Bambu Studio still reaches it on the LAN. Status, the camera and uploads work in either mode.

```sh
srw printer status                 # state, progress, temperatures, loaded spools, errors
srw printer snapshot               # camera frame to builds/printer/ (turns on the chamber light)
srw print v003 --dry-run           # every check, nothing sent
srw print v003                     # upload, start, record the print in versions/v003.toml
srw printer watch                  # follow the job; frames in builds/printer/<job>/, latest.jpg
srw printer pause|resume|stop
```

Before starting, `srw print` checks that the slice matches the current build, the printer is idle with no error or serious HMS alert, the nozzle diameter matches, and a loaded spool holds the slice's filament type. AMS slots are preferred. `--spool N` picks an `ams_mapping` slot instead (4 × unit + tray, from 0; 254 is the external spool). It cannot tell whether the build plate is empty and in place, so look at a snapshot first.

`srw printer watch` exits 0 when the print finishes, 1 when it fails, 3 when it pauses (filament runout, an error or a manual pause) and 4 when nothing is printing. When it ends, it writes `outcome` and `ended` on the print's record.

The printer is found by its LAN discovery broadcast, and the access code comes from Bambu Studio's saved settings. To pin them instead, create `printer.local.toml` (ignored by git) or set the matching `BAMBU_*` environment variables:

```toml
host = "192.168.1.50"
serial = "01P00..."
access_code = "..."   # Settings > Network on the printer
```

Job options (calibration, timelapse, default spools) are under `[printer]` in `workbench.toml`.

## Changing the library

Edit `ShoulderRest/` in place. It is installed editable, so changes take effect immediately. Follow `ShoulderRest/AGENTS.md`, keep the library generic, and commit there first. Then commit the new submodule pointer here. Builds record both commits.
