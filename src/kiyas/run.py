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
from .config import Engine, FrameMethod, FrameSelection, Mode, Project, Source
from .engines.base import DARK_LUMA_THRESHOLD, EngineError, RenderSettings
from .frames import align, selector
from .media import rpu
from .media.binaries import BinaryNotFound
from .media.probe import ProbeError, probe

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
    #: The render options this column was produced with, in a settings
    #: comparison. Empty for a source comparison.
    options: dict[str, str] = field(default_factory=dict)
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
    available = engines.available_engines(project.tools)
    if not available:
        raise RunError("no frame engine is available. Run 'kiyas doctor' to see what is missing.")

    if project.mode is Mode.SETTINGS:
        # Not a preference. Tone-mapping curves and GLSL shaders live inside a
        # player's renderer, so this comparison cannot be produced any other
        # way -- honouring `engine = "vapoursynth"` here would mean rendering
        # every variant identically and calling it a result.
        if "mpv" not in available:
            raise RunError(
                "a settings comparison needs mpv, which was not found. It is the only engine "
                "that can render the same frame with different tone-mapping curves or shaders. "
                "Run 'kiyas doctor' to see how to point kiyas at it."
            )
        if project.engine not in (Engine.AUTO, Engine.MPV):
            raise RunError(
                f"mode = 'settings' always renders with mpv, so engine = "
                f"'{project.engine.value}' cannot be honoured. Remove the engine line."
            )
        return "mpv"

    if project.engine is Engine.AUTO:
        return available[0]

    wanted = project.engine.value
    if wanted not in available:
        raise RunError(
            f"engine '{wanted}' is not available here (available: {', '.join(available)}). "
            f"Run 'kiyas doctor' for details."
        )
    return wanted


def columns(project: Project) -> list[tuple[Source, RenderSettings | None]]:
    """What the comparison's columns are, and how each one is produced.

    A source comparison has one column per file and no render settings. A
    settings comparison has one file and one column per variant. Everything
    downstream works on this list, so the two modes only differ here.
    """
    if project.settings is None:
        return [(source, None) for source in project.sources]

    source = project.sources[0]
    return [
        (
            source,
            RenderSettings(
                name=variant.name,
                options=dict(variant.options),
                width=project.settings.width,
                fullscreen=project.settings.fullscreen,
            ),
        )
        for variant in project.settings.variants
    ]


def _warn_about_sizes(prepared: list, project: Project, warnings: list[str], **advice) -> None:
    """Complain if the columns are not the same size.

    Columns that are not the same size cannot be flipped between, and flipping
    between them is the one thing a comparison is for. Nothing else notices:
    every image is written, the manifest is valid, the upload succeeds, and the
    result is two pictures of different shapes.

    Settings mode is exempt because every column there is the same file
    rendered the same size, so there is nothing to disagree.

    The numbers are never guessed from the picture. Splitting the difference
    looks right and is not: measured on a real pair, the arithmetic gives 277
    rows top and bottom where the source's own Dolby Vision metadata says 276.
    So the offsets are read out of the RPU, which is the only place they are
    stated, and when they cannot be read the message says less rather than
    inventing them.
    """
    if project.mode is Mode.SETTINGS:
        return
    sizes = {(item.width, item.height) for item in prepared if item.width and item.height}
    if len(sizes) <= 1:
        return
    listed = ", ".join(f"{item.name} {item.width}x{item.height}" for item in prepared)
    message = (
        f"the sources are not the same size ({listed}), so the comparison cannot be flipped "
        f"between. Crop or resize them to match -- on a Dolby Vision source the active picture "
        f"is in the RPU's level 5 offsets, not something to guess from the black bars."
    )
    for note in _active_area_advice(prepared, project, **advice):
        message += " " + note
    warnings.append(message)


def _active_area_advice(
    prepared: list,
    project: Project,
    *,
    inspect=probe,
    read=rpu.sample,
) -> list[str]:
    """What each source's own Dolby Vision metadata says about where its picture is.

    This is the part of the size warning that can be pasted into a project
    file, and it is worth the second or so it costs: reading it by hand means
    extracting an RPU and knowing which of dovi_tool's outputs to look at.

    Sources that already carry a crop or a resize are skipped. The offsets
    describe the frame as encoded, so against a source that has already been
    transformed they would be an answer to a different question.
    """
    advice: list[str] = []
    for item, source in zip(prepared, project.sources, strict=False):
        if source.crop or source.resize or not (item.width and item.height):
            continue
        try:
            info = inspect(source.path, ffprobe=project.tools.get("ffprobe"))
            if info.dovi_profile is None:
                continue
            duration = item.frame_count / float(item.fps) if item.fps else 0.0
            reading = read(
                source.path,
                duration=duration,
                ffmpeg=project.tools.get("ffmpeg"),
                dovi_tool=project.tools.get("dovi_tool"),
            )
        except (rpu.ActiveAreaError, BinaryNotFound, ProbeError, OSError):
            # Enriching a warning is not worth failing a run that has already
            # written its images. Without dovi_tool, or on an RPU that will not
            # parse, the sentence above still says where to look.
            continue
        advice.append(_describe_active_area(item, prepared, reading))
    return [note for note in advice if note]


def _describe_active_area(item, prepared: list, reading: rpu.Reading) -> str:
    """One sentence about one source's active area."""
    if reading.varies:
        return (
            f"{item.name}'s picture changes shape as the film plays "
            f"({len(reading.shapes)} shapes across {reading.positions} positions read), "
            f"so no single crop fits it."
        )
    area = reading.fixed
    if area is None or area.is_whole_frame:
        # Nothing to paste and nothing to do: this source has no bars to take
        # off. Saying so would lengthen an already long warning with the one
        # sentence in it that carries no number.
        return ""

    width, height = area.size_within(item.width, item.height)
    line = (
        f"{item.name}'s own Dolby Vision metadata puts the picture at "
        f"crop = {list(area.as_crop())}, which leaves {width}x{height}"
    )

    others = {
        (p.width, p.height): p.name for p in prepared if p is not item and p.width and p.height
    }
    if (width, height) in others:
        return line + " and matches the others."
    if len(others) != 1:
        return line + "."
    ((other_width, other_height), other_name) = next(iter(others.items()))
    # Two releases can disagree with the master and with each other: an iTunes
    # WEB-DL of a scope film measured 1606 rows where the disc's own RPU says
    # 1608. Saying which way and by how much is the difference between a number
    # that can be adjusted and one that just looks wrong.
    gaps = []
    if height != other_height:
        gaps.append(
            f"{abs(height - other_height)} rows {'taller' if height > other_height else 'shorter'}"
        )
    if width != other_width:
        gaps.append(
            f"{abs(width - other_width)} columns {'wider' if width > other_width else 'narrower'}"
        )
    return (
        line + f" -- still {' and '.join(gaps)} than {other_name} ({other_width}x{other_height}), "
        f"which was cropped to something other than the master's active area."
    )


def _acceptability(prepared: list, project: Project, warnings: list[str]):
    """Build the predicate the frame selector nudges against.

    A frame has to satisfy every source, not just the first. Checking only one
    source is the subtle version of this bug: the comparison looks right and
    is quietly unfair.
    """
    want_b_frames = project.frames.b_frames_only
    want_bright = project.frames.skip_dark
    want_clean = project.frames.skip_combed

    if want_b_frames and not all(p.supports_frame_types for p in prepared):
        blind = [p.name for p in prepared if not p.supports_frame_types]
        warnings.append(
            f"B-frame selection is off: {', '.join(blind)} cannot report picture types. "
            f"I-frames get a disproportionate share of the bitrate, so a comparison that "
            f"includes them flatters the weaker encode."
        )
        want_b_frames = False

    # An encode with no B-frames at all is not unusual -- a WEB-DL made for low
    # latency can be entirely I and P -- and on one, "must be a B-frame" throws
    # away every frame in the file. What the rule is actually for is avoiding
    # I-frames, so that is what it falls back to. Refusing to produce a
    # comparison because a file was encoded a common way is not a rule, it is a
    # bug with a message attached.
    flat = [p.name for p in prepared if want_b_frames and not p.has_b_frames]
    if flat:
        warnings.append(
            f"{', '.join(flat)} contains no B-frames at all, so frames are chosen by avoiding "
            f"I-frames instead. That is what the rule is for: I-frames get a disproportionate "
            f"share of the bitrate and flatter the weaker encode."
        )

    if want_clean:
        blind = [p.name for p in prepared if p.combed(0) is None]
        if blind:
            warnings.append(
                f"comb detection is off: {', '.join(blind)} cannot answer for a single frame. "
                f"Only the VapourSynth engine can, so a combed frame may be captured and it "
                f"would show the deinterlacer's work rather than the encode's."
            )
            want_clean = False

    if not want_b_frames and not want_bright and not want_clean:
        return None

    def acceptable(frame: int) -> bool:
        for source in prepared:
            if frame >= source.frame_count:
                return False
            if want_b_frames:
                kind = source.picture_type(frame)
                if source.has_b_frames:
                    if kind != "B":
                        return False
                elif kind == "I":
                    return False
            if want_bright and source.mean_luma(frame) < DARK_LUMA_THRESHOLD:
                return False
            if want_clean and source.combed(frame):
                return False
        return True

    return acceptable


def align_project(
    project: Project, *, samples: int = align.SAMPLES, progress=None
) -> tuple[Fraction, list]:
    """Open every source and measure how far each is from the first.

    Prepared with ``overlay=False``, and that is not a detail. The burnt-in
    label carries the frame number, so it changes with every frame in a way
    that is perfectly correlated with an offset of zero -- the strongest
    possible spurious peak, and it would look like a confident answer.
    """
    if project.mode is Mode.SETTINGS:
        raise RunError(
            "a settings comparison renders one file several ways, so every column is "
            "already the same frame of the same source. There is nothing to align."
        )

    missing = [source.path for source in project.sources if not source.path.is_file()]
    if missing:
        raise RunError(
            "these source files do not exist:\n  " + "\n  ".join(str(path) for path in missing)
        )

    engine = engines.get_engine(choose_engine(project))
    reference = probe(project.sources[0].path, ffprobe=project.tools.get("ffprobe"))

    prepared = []
    try:
        for source in project.sources:
            if progress:
                progress(f"opening {source.name}")
            try:
                prepared.append(
                    engine.prepare(
                        source,
                        target_fps=reference.fps,
                        overlay=False,
                        tools=project.tools,
                        progress=progress,
                        index_dir=project.index_dir,
                    )
                )
            except EngineError as exc:
                raise RunError(str(exc)) from exc

        if progress:
            progress("measuring the offset between sources")
        return reference.fps, check_sync(prepared, project, samples=samples)
    finally:
        for item in prepared:
            item.close()


def check_sync(prepared, project: Project, *, samples: int = align.SAMPLES) -> list:
    """Measure how far each source is from the first one.

    Measured on the *prepared* sources rather than the files, so whatever crop,
    resize and trim the project already applies has been applied. That makes
    the answer a residual -- "your trim of 24 is three frames short" -- which
    is what validates a hand-set number rather than replacing it, and it means
    two sources at different resolutions have already been brought to a
    comparable picture by the project itself.

    This is not run automatically. `run` decodes a few dozen frames; this
    decodes tens of thousands, and making every comparison minutes slower to
    catch a mistake most projects do not have is the wrong trade. The
    length-difference warning above is the cheap signal that it is worth doing.
    """
    if len(prepared) < 2:
        return []

    reference = prepared[0]
    total = min(item.frame_count for item in prepared)
    window = align.window_for(total)
    positions = selector.select(
        FrameSelection(method=FrameMethod.COUNT, count=samples),
        total,
        reference.fps,
    )

    results = []
    for item in prepared[1:]:
        results.append(
            align.measure(
                item.name,
                reference.luma_thumbnails,
                item.luma_thumbnails,
                positions=positions,
                window=window,
            )
        )
    return results


def run(project: Project, *, overlay: bool = True, progress=None) -> RunResult:
    """Produce the comparison described by ``project``."""
    warnings: list[str] = []
    engine_name = choose_engine(project)
    engine = engines.get_engine(engine_name)
    plan = columns(project)

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
        for source, render in plan:
            if progress:
                progress(f"opening {render.name if render else source.name}")
            try:
                prepared.append(
                    engine.prepare(
                        source,
                        target_fps=target_fps,
                        overlay=overlay,
                        tools=project.tools,
                        progress=progress,
                        index_dir=project.index_dir,
                        render=render,
                    )
                )
            except EngineError as exc:
                raise RunError(str(exc)) from exc

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

        # Columns that are not the same size cannot be flipped between, and
        # flipping between them is the one thing a comparison is for. Nothing
        # else notices: every image is written, the manifest is valid, the
        # upload succeeds, and the result is two pictures of different shapes.
        #
        # Settings mode is exempt because every column there is the same file
        # rendered the same size, so there is nothing to disagree.
        _warn_about_sizes(prepared, project, warnings)

        # In a settings comparison every column is the same file, so asking all
        # of them whether a frame is usable would run the same ffprobe once per
        # variant for the same answer.
        judges = prepared[:1] if project.mode is Mode.SETTINGS else prepared
        acceptable = _acceptability(judges, project, warnings)
        if acceptable is not None and project.frames.method is not FrameMethod.MANUAL:
            frames, rejected, moved = selector.refine(frames, total, acceptable)
            if rejected:
                warnings.append(
                    f"{len(rejected)} requested position(s) had no usable frame nearby "
                    f"and were dropped: {rejected}"
                )
            for was, now in moved:
                warnings.append(
                    f"frame {was} moved to {now} ({(now - was) / float(target_fps):.0f}s later) "
                    f"to satisfy the frame rules; it is a different moment from the one the "
                    f"even spacing picked."
                )

        # Deliberately dark and bright frames, on top of the evenly spaced
        # ones. Even spacing finds the typical frame, and the two questions
        # people bring to a comparison do not live there: banding is in the
        # dark scenes and highlight rolloff is in the bright ones.
        if project.frames.method is not FrameMethod.MANUAL and (
            project.frames.dark or project.frames.light
        ):
            if progress:
                progress("looking for the darkest and brightest frames")
            reference = judges[0]
            picked = selector.extremes(
                selector.window_for(project.frames, total),
                project.frames.dark,
                project.frames.light,
                reference.mean_luma,
                acceptable=acceptable,
                avoid=set(frames),
            )
            wanted = project.frames.dark + project.frames.light
            if len(picked) < wanted:
                warnings.append(
                    f"asked for {wanted} extreme frame(s) and found {len(picked)}: the rest of "
                    f"the sampled positions did not satisfy the frame rules."
                )
            frames = sorted(set(frames) | set(picked))

        if not frames:
            raise RunError(
                "no usable frames were found. Every candidate was rejected by the rules in "
                "[frames]. Turning off skip_dark helps on a film that is mostly night, and "
                "turning off b_frames_only helps on an encode this cannot read picture types "
                "from."
            )

        project.output.mkdir(parents=True, exist_ok=True)
        results: list[SourceResult] = []
        # `plan`, not project.sources: a settings comparison has one file and
        # several columns, so zipping against the sources would produce exactly
        # one column and quietly drop the rest.
        for item, (source, render) in zip(prepared, plan, strict=True):
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
                    options=dict(render.options) if render else {},
                    notes=list(getattr(item, "assumptions", [])),
                )
            )

        # Collected after capture, not before: some notes are only known once
        # something has been rendered -- mpv cannot say what size its window
        # settled at until it has drawn a frame.
        for result in results:
            warnings.extend(f"{result.name}: {note}" for note in result.notes)
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
                # What produced this column. In a settings comparison it is the
                # answer to "which of these did I actually like", which is the
                # whole reason for running one.
                "options": result.options,
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


SETTINGS_TEMPLATE = """\
# kiyas settings comparison
#
# One file, rendered several different ways, so you can decide what your
# player's configuration should be. Run it with:  kiyas run {name}
#
# 'kiyas templates' lists the built-in sets of variants.

title = "{title}"
mode = "settings"

[[source]]
path = "CHANGE ME.mkv"
name = "source"
# crop = [0, 0, 140, 140]   # left, right, top, bottom

[frames]
# For this kind of comparison, choosing the frames yourself is usually better
# than spreading them evenly: the shot that settles the argument is a specific
# one. 'kiyas pick CHANGE ME.mkv' plays the file and prints the block to paste.
method = "count"
count = 6
skip_start = "5%"
skip_end = "10%"
skip_dark = true

[settings]
template = "tonemap"   # tonemap | gamut | scalers | dscale | deband | shaders
# width = 1920         # capture width; the height follows the source's aspect
                       # ratio. Default: the source's own width. mpv renders
                       # into a window and a window cannot be bigger than your
                       # screen, so a 4K source is captured smaller unless you
                       # set fullscreen.
# fullscreen = true    # capture at the full screen resolution instead
# base = {{ target-peak = 203 }}   # applied to every variant

# Instead of (or as well as) a template, spell the variants out. The name is
# the column label in the published comparison.
# [[variant]]
# name = "ArtCNN C4F32"
# options = {{ glsl-shaders = "C:/path/to/ArtCNN_C4F32.glsl" }}

[output]
directory = "out"
"""


def scaffold(path: Path, title: str = "Untitled comparison", *, settings: bool = False) -> Path:
    """Write a starter project file. Refuses to overwrite."""
    if path.exists():
        raise RunError(f"{path} already exists; not overwriting it")
    text = SETTINGS_TEMPLATE if settings else TEMPLATE
    path.write_text(text.format(name=path.name, title=title), encoding="utf-8")
    return path
