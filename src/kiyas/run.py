"""Running a comparison: config in, PNG files and a manifest out.

The whole point of this module is that it contains no knowledge of VapourSynth,
ffmpeg or mpv. It picks an engine, asks it to prepare each source, works out
which frames to capture, and writes them. Swapping the engine changes nothing
here.

One rule shapes most of the code below: **a frame is only usable if it is
usable in every source.** A B-frame in the remux that happens to be an I-frame
in the web-dl is not a fair comparison, and a frame that is black in one source
because its trim is off by two is not a comparison at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from . import engines
from .config import Engine, FrameMethod, Project
from .engines.base import DARK_LUMA_THRESHOLD, EngineError
from .frames import selector
from .media.probe import probe

MANIFEST_NAME = "kiyas-manifest.json"

#: Characters Windows refuses in a filename, plus the path separators.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Names Windows reserves regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}  # fmt: skip


def safe_directory_name(name: str) -> str:
    """A directory name that survives every filesystem kiyas runs on.

    Source names are free text -- "Lionsgate GBR/USA" and "REMUX (DV: FEL)" are
    both realistic -- and both contain characters Windows rejects. The original
    name is kept in the manifest; only the folder is sanitised.
    """
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(".")
    if not cleaned:
        cleaned = "source"
    if cleaned.upper() in _RESERVED or cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:100]


@dataclass(slots=True)
class SourceResult:
    name: str
    directory: Path
    files: list[Path]
    source_path: Path
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunResult:
    project: Project
    engine: str
    frames: list[int]
    sources: list[SourceResult]
    manifest: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return sum(len(source.files) for source in self.sources)


class RunError(RuntimeError):
    """Raised when a comparison cannot be produced."""


def choose_engine(project: Project) -> str:
    available = engines.available_engines()
    if not available:
        raise RunError("no frame engine is available. Run 'kiyas doctor' to see what is missing.")

    if project.engine is Engine.AUTO:
        return available[0]

    wanted = project.engine.value
    if wanted not in available:
        raise RunError(
            f"engine '{wanted}' is not available here (available: {', '.join(available)}). "
            f"Run 'kiyas doctor' for details."
        )
    return wanted


def _acceptability(prepared: list, project: Project, warnings: list[str]):
    """Build the predicate the frame selector nudges against.

    A frame has to satisfy every source, not just the first. Checking only one
    source is the subtle version of this bug: the comparison looks right and
    is quietly unfair.
    """
    want_b_frames = project.frames.b_frames_only
    want_bright = project.frames.skip_dark

    if want_b_frames and not all(p.supports_frame_types for p in prepared):
        blind = [p.name for p in prepared if not p.supports_frame_types]
        warnings.append(
            f"B-frame selection is off: {', '.join(blind)} cannot report picture types. "
            f"I-frames get a disproportionate share of the bitrate, so a comparison that "
            f"includes them flatters the weaker encode."
        )
        want_b_frames = False

    if not want_b_frames and not want_bright:
        return None

    def acceptable(frame: int) -> bool:
        for source in prepared:
            if frame >= source.frame_count:
                return False
            if want_b_frames and not source.is_b_frame(frame):
                return False
            if want_bright and source.mean_luma(frame) < DARK_LUMA_THRESHOLD:
                return False
        return True

    return acceptable


def run(project: Project, *, overlay: bool = True, progress=None) -> RunResult:
    """Produce the comparison described by ``project``."""
    warnings: list[str] = []
    engine_name = choose_engine(project)
    engine = engines.get_engine(engine_name)

    missing = [s.path for s in project.sources if not s.path.is_file()]
    if missing:
        raise RunError(
            "these source files do not exist:\n  " + "\n  ".join(str(p) for p in missing)
        )

    # Every clip is normalised to the first source's frame rate. Without this a
    # 25fps PAL transfer and a 23.976 NTSC one disagree about what frame 10000
    # means, and the comparison silently drifts apart.
    reference = probe(project.sources[0].path, ffprobe=project.tools.get("ffprobe"))
    target_fps: Fraction | None = reference.fps

    prepared = []
    try:
        for source in project.sources:
            if progress:
                progress(f"opening {source.name}")
            try:
                prepared.append(
                    engine.prepare(
                        source,
                        target_fps=target_fps,
                        overlay=overlay,
                        tools=project.tools,
                        progress=progress,
                        index_dir=project.index_dir,
                    )
                )
            except EngineError as exc:
                raise RunError(str(exc)) from exc

        for item in prepared:
            for note in getattr(item, "assumptions", []):
                warnings.append(f"{item.name}: {note}")

        labelled = {p.has_overlay for p in prepared}
        if overlay and len(labelled) > 1:
            warnings.append(
                "only some sources could be labelled in-frame; the comparison will be "
                "inconsistent. Consider turning the overlay off."
            )

        lengths = {p.name: p.frame_count for p in prepared}
        total = min(lengths.values())
        if total <= 0:
            raise RunError(f"nothing left to compare after trimming: {lengths}")

        longest = max(lengths.values())
        spread = longest - total
        # Relative, not absolute. An absolute threshold in seconds cannot work
        # for both a six-second test clip and a three-hour film: pick one that
        # tolerates a theatrical-versus-extended difference and it never fires
        # on short material; pick one that fires there and every alternate cut
        # trips it. One percent of the longest source scales with the content,
        # and the one-second floor stops rounding noise warning on tiny clips.
        threshold = max(int(float(target_fps)), int(longest * 0.01))
        if spread > threshold:
            warnings.append(
                f"source lengths differ by {spread} frames "
                f"({spread / float(target_fps):.1f}s): {lengths}. "
                f"That usually means a trim value is wrong, or the editions differ. "
                f"Only the first {total} frames are comparable."
            )

        if progress:
            progress("choosing frames")
        try:
            frames = selector.select(project.frames, total, target_fps)
        except selector.SelectionError as exc:
            raise RunError(str(exc)) from exc

        acceptable = _acceptability(prepared, project, warnings)
        if acceptable is not None and project.frames.method is not FrameMethod.MANUAL:
            frames, rejected = selector.refine(frames, total, acceptable)
            if rejected:
                warnings.append(
                    f"{len(rejected)} requested position(s) had no usable frame nearby "
                    f"and were dropped: {rejected}"
                )
        if not frames:
            raise RunError(
                "no usable frames were found. Try turning off b_frames_only or "
                "skip_dark in [frames]."
            )

        project.output.mkdir(parents=True, exist_ok=True)
        results: list[SourceResult] = []
        for item, source in zip(prepared, project.sources):
            directory = project.output / safe_directory_name(item.name)
            if progress:
                progress(f"capturing {len(frames)} frames from {item.name}")
            try:
                files = item.write_frames(frames, directory)
            except EngineError as exc:
                raise RunError(str(exc)) from exc
            results.append(
                SourceResult(
                    name=item.name,
                    directory=directory,
                    files=files,
                    source_path=source.path,
                    notes=list(getattr(item, "assumptions", [])),
                )
            )
    finally:
        for item in prepared:
            item.close()

    manifest = _write_manifest(project, engine_name, frames, results, warnings, target_fps)
    return RunResult(project, engine_name, frames, results, manifest, warnings)


def _write_manifest(
    project: Project,
    engine_name: str,
    frames: list[int],
    results: list[SourceResult],
    warnings: list[str],
    fps: Fraction,
) -> Path:
    """Record what was produced.

    Publishing reads this instead of re-deriving anything, so uploading twice
    cannot produce a differently ordered or differently labelled comparison
    than the one on disk.
    """
    payload = {
        "kiyas": 1,
        "title": project.title,
        "mode": project.mode.value,
        "engine": engine_name,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        # As a string ratio, never a float: publishing turns frame numbers into
        # timestamps, and 23.976 is not 24000/1001.
        "fps": f"{fps.numerator}/{fps.denominator}",
        "frames": frames,
        "warnings": warnings,
        "sources": [
            {
                "name": result.name,
                "directory": result.directory.name,
                "path": str(result.source_path),
                "files": [f.name for f in result.files],
                "notes": result.notes,
            }
            for result in results
        ],
    }
    path = project.output / MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


TEMPLATE = """\
# kiyas comparison project
#
# Run it with:  kiyas run {name}

title = "{title}"
mode = "source"        # "source" (several files) or "settings" (one file, several renders)
engine = "auto"        # auto | vapoursynth | ffmpeg

[frames]
method = "count"       # count | interval | manual
count = 12
skip_start = "5%"      # skip logos and black leader
skip_end = "10%"       # skip credits
b_frames_only = true   # I-frames get more bitrate and flatter a weak encode
skip_dark = true       # a black frame compares nothing

[[source]]
path = "CHANGE ME.mkv"
name = "Source 1"
# trim = 0             # frames to drop from the start, to sync against the others
# crop = [0, 0, 140, 140]   # left, right, top, bottom
# resize = [1920, 1080]
# tonemap = "auto"     # auto | hdr10 | hdr10plus | dovi | none

[[source]]
path = "CHANGE ME TOO.mkv"
name = "Source 2"

[output]
directory = "out"
# index_dir = "indexes"  # where VapourSynth caches its frame indexes.
                         # Default: next to the media, which is what makes a
                         # second run fast. Set this for a read-only library.
"""


def scaffold(path: Path, title: str = "Untitled comparison") -> Path:
    """Write a starter project file. Refuses to overwrite."""
    if path.exists():
        raise RunError(f"{path} already exists; not overwriting it")
    path.write_text(TEMPLATE.format(name=path.name, title=title), encoding="utf-8")
    return path
