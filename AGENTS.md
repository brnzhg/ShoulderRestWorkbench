# Project context

This workbench iterates on one player's violin shoulder rest. Read [README.md](README.md) for concepts, the version file format and the rest frame. Geometry comes from the `ShoulderRest/` submodule; read `ShoulderRest/README.md` and `ShoulderRest/AGENTS.md` before changing it. Code lives in `src/rest_workbench/`, and the `srw` CLI (`uv run srw --help`) is the entry point.

## Iterating on feedback

The usual loop: the player prints a version, tries it, and reports back. You record the feedback, make the next version, build and inspect it, and slice it.

1. **Record feedback** on the printed version's `[[prints]]` entry: `feedback` in the player's words, and `[prints.scores]` on a 1 to 5 scale. Reuse existing score names (`srw table` shows them), such as `comfort`, `stability`, `head_fit`, `tail_fit`. Add `verdict = "keeper"` or `"rejected"` at the top level when the player gives one. If `srw printed` was not run for the print, run it first.
2. **Make the next version** with `srw new <parent> -m "<intent>"`. Prefer one idea per version so the comparison stays readable. State the intent and the feedback behind it in `summary`; put longer reasoning in `notes`.
   - Placement feedback ("dig in at the front", "slides toward my neck", "sits too high") becomes `--shift`, `--rotate` and `--pivot` in the rest frame. Pick the pivot the player would hold still, such as the leg that already feels right.
   - Shape feedback becomes `--set` overrides of rest parameters, such as cap angles, head widths and outline strokes.
   - Small steps: about 1 to 3 mm and 1 to 3 degrees, unless the player asks for more.
3. **Build and inspect.** Run `srw build`, then look at `builds/<id>/fit.png` and `rest.png` and run `srw compare <parent> <id>`. Check that the change reads as intended and nothing else moved. When fillets, caps or stripes fail at a new pose, adjust them in the same version and say so in `notes`. Never loosen a library validation to make a version build.
4. **Slice** with `srw slice <id>` (add `--parts legs` only when the player wants new legs). Report print time and grams. Do not start a print or send files to the printer. The player opens the slice with `srw open <id>` and sends it.
5. **Commit** the version file, and any feedback edits, as you go.

To find where to look next, use `srw table --varying` (one row per version: parameters, rest pose on the shoulder, scores) and `srw diff`. Lineage matters: compare a version with its parent before comparing it with distant relatives.

## Changing code

- Workbench code adapts library interfaces to version files, builds and slicing. It holds no geometry. Geometry changes belong in the library, generalized so that another player could use them.
- Library changes happen in `ShoulderRest/` on a branch. Run its checks, commit there, then commit the submodule pointer here in a separate commit. Player data (scans, fitted placements, tuned parameters) stays in this repository.
- Shoulder scans live in `shoulders/<name>/` and are tracked with Git LFS. Never edit a scan in place. Add a new directory instead, because existing versions reference the old one by name.
- Keep the version file format backward compatible. Old versions must still resolve and build. When a format change is unavoidable, migrate every file under `versions/` in the same commit.
- Slicer profiles use Bambu Studio setting keys. Slicing fails on any override the sliced project does not carry, which catches misspellings.

## Validation

Run `uv run python -m unittest discover -s tests`, `uv run mypy src tests` and `uv run ruff check .`. After changing the library or the build code, also build a recent version with `srw build <id> --force` and look at its renders.

## Files

- `versions/`, `shoulders/`, `slicer/`, `workbench.toml`: tracked data and settings.
- `builds/`: generated output, ignored. Rebuild it rather than committing it.
- `ShoulderRest/`: the library submodule.
