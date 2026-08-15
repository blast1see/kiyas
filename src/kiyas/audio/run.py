"""Producing an audio comparison.

The output is deliberately the same shape as a picture comparison: one
directory per track, the same images in the same order in each, and a manifest.
That is not tidiness -- it means ``kiyas publish`` works on it unchanged, and
the grid that slow.pics builds has one row per kind of analysis with every
track's version of it side by side, which is the arrangement that makes two
spectrograms comparable at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ..run import MANIFEST_NAME, safe_directory_name
from . import table, visuals
from .analysis import AnalysisError, AudioAnalysis, analyse
from .probe import AudioTrack, mediainfo_extras, probe_track
from .sync import Offset, measure

#: The rows of the published comparison, in order. The filenames are numbered
#: so the directory listing matches the order they are published in.
ANALYSES: tuple[tuple[str, str], ...] = (
    ("Spectrogram", "01-spectrogram.png"),
    ("Waveform", "02-waveform.png"),
    ("Frequency response", "03-frequency.png"),
)

SPECIFICATIONS = "specifications.md"
SPECIFICATIONS_BBCODE = "specifications.bbcode.txt"


@dataclass(slots=True)
class TrackResult:
    name: str
    directory: Path
    files: list[Path]
    source_path: Path
    analysis: AudioAnalysis
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AudioResult:
    title: str
    output: Path
    tracks: list[TrackResult]
    offsets: list[Offset]
    manifest: Path
    specifications: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return sum(len(track.files) for track in self.tracks)


def _unique_names(tracks: Sequence[AudioTrack]) -> list[str]:
    """A distinct column label for every track.

    Two tracks pulled from files with the same name -- a remux and a web-dl in
    different folders -- would otherwise share a directory and overwrite each
    other's images, which publishes as a comparison of one track with itself.
    """
    names: list[str] = []
    for track in tracks:
        base = track.path.stem
        if track.index:
            base = f"{base} (track {track.index})"
        candidate = base
        suffix = 2
        while candidate in names:
            candidate = f"{base} #{suffix}"
            suffix += 1
        names.append(candidate)
    return names


def run(
    paths: Sequence[Path],
    *,
    output: Path,
    title: str = "",
    track_index: int = 0,
    tools: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> AudioResult:
    """Analyse every track and write the comparison."""
    tools = tools or {}
    if not paths:
        raise AnalysisError("nothing to compare: give at least one file")

    missing = [path for path in paths if not Path(path).is_file()]
    if missing:
        raise AnalysisError(
            "these files do not exist:\n  " + "\n  ".join(str(path) for path in missing)
        )

    tracks: list[AudioTrack] = []
    for path in paths:
        if progress:
            progress(f"reading {Path(path).name}")
        track = probe_track(path, index=track_index, ffprobe=tools.get("ffprobe"))
        extras = mediainfo_extras(path, index=track_index, mediainfo=tools.get("mediainfo"))
        tracks.append(replace(track, extra=extras) if extras else track)

    names = _unique_names(tracks)
    warnings: list[str] = []

    rates = {track.sample_rate for track in tracks}
    if len(rates) > 1:
        warnings.append(
            f"the tracks have different sample rates ({sorted(rates)}). Their spectrograms "
            f"cover different frequency ranges, so the pictures are not directly comparable."
        )

    output.mkdir(parents=True, exist_ok=True)
    results: list[TrackResult] = []
    for track, name in zip(tracks, names, strict=True):
        if progress:
            progress(f"analysing {name}")
        analysis = analyse(track, ffmpeg=tools.get("ffmpeg"), progress=progress)

        directory = output / safe_directory_name(name)
        directory.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for label, filename in ANALYSES:
            if progress:
                progress(f"drawing {label.lower()} for {name}")
            destination = directory / filename
            if label == "Spectrogram":
                visuals.spectrogram(
                    track,
                    destination,
                    ffmpeg=tools.get("ffmpeg"),
                    channel_labels=analysis.channel_names,
                )
            elif label == "Waveform":
                visuals.waveform(analysis, destination)
            else:
                visuals.frequency_response(analysis, destination)
            files.append(destination)

        results.append(
            TrackResult(
                name=name,
                directory=directory,
                files=files,
                source_path=Path(track.path),
                analysis=analysis,
                notes=list(analysis.notes),
            )
        )

    offsets: list[Offset] = []
    for track, name in zip(tracks[1:], names[1:], strict=True):
        if progress:
            progress(f"measuring the offset of {name}")
        try:
            offset = measure(tracks[0], track, ffmpeg=tools.get("ffmpeg"))
        except AnalysisError as exc:
            warnings.append(f"{name}: could not measure an offset: {exc}")
            continue
        offsets.append(offset)
        if abs(offset.milliseconds) > 1.0:
            warnings.append(
                f"{name} is {offset.summary} than {names[0]}. Until that is corrected, the "
                f"pictures below differ because the tracks are not aligned."
            )
        if offset.is_weak:
            warnings.append(
                f"{name}: the offset measurement is weak (peak-to-floor "
                f"{offset.confidence:.1f}). Repetitive material can lock onto the wrong "
                f"period and look just as certain."
            )

    for result in results:
        warnings.extend(f"{result.name}: {note}" for note in result.notes)

    analyses = [result.analysis for result in results]
    specifications = output / SPECIFICATIONS
    specifications.write_text(table.markdown(analyses, offsets=offsets) + "\n", encoding="utf-8")
    (output / SPECIFICATIONS_BBCODE).write_text(
        table.bbcode(analyses, offsets=offsets) + "\n", encoding="utf-8"
    )

    manifest = _write_manifest(output, title or _default_title(paths), results, warnings)
    return AudioResult(
        title=title or _default_title(paths),
        output=output,
        tracks=results,
        offsets=offsets,
        manifest=manifest,
        specifications=specifications,
        warnings=warnings,
    )


def _default_title(paths: Sequence[Path]) -> str:
    return f"Audio comparison: {Path(paths[0]).stem}"


def _write_manifest(
    output: Path, title: str, results: Sequence[TrackResult], warnings: Sequence[str]
) -> Path:
    """The same manifest the picture side writes, with named rows.

    ``labels`` rather than ``frames``: these rows are analyses, and giving them
    frame numbers would put a timestamp on a spectrogram in the published
    labels.
    """
    payload = {
        "kiyas": 1,
        "title": title,
        "mode": "audio",
        "engine": "ffmpeg",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "labels": [label for label, _ in ANALYSES],
        "warnings": list(warnings),
        "sources": [
            {
                "name": result.name,
                "directory": result.directory.name,
                "path": str(result.source_path),
                "files": [f.name for f in result.files],
                "notes": result.notes,
                "measurements": {
                    "peak_dbfs": round(result.analysis.overall_peak_dbfs, 3),
                    "rms_dbfs": [round(value, 3) for value in result.analysis.rms_dbfs],
                    "clipped_runs": result.analysis.clipped_runs,
                    "real_bit_depth": result.analysis.real_bit_depth,
                    "silent_channels": result.analysis.silent_channels,
                },
            }
            for result in results
        ],
    }
    path = output / MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
