"""Reading back what a run produced.

Publishing works from the manifest rather than from the project file. The
project describes what was *asked for*; the manifest records what actually came
out, including the frames that survived B-frame nudging and dark-frame
rejection. Re-deriving them would mean re-opening every source, and any
difference between the two derivations would show up as a comparison whose
labels do not match its images.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

MANIFEST_NAME = "kiyas-manifest.json"


class ManifestError(ValueError):
    """Raised when a manifest cannot be read or does not describe a full set."""


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """One row of the published grid: the same thing seen in every source.

    Usually a frame, in which case the label is its timestamp and number. An
    audio comparison's rows are analyses -- a spectrogram, a waveform -- and
    carry their own label instead. The publishing layer only ever needs the
    label, so the two kinds do not have to be told apart below this line.
    """

    label: str

    @classmethod
    def for_frame(cls, number: int, fps: Fraction) -> ComparisonRow:
        """``H:MM:SS.mmm / N``, which is how slow.pics labels a comparison."""
        seconds = number / float(fps)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return cls(f"{int(hours)}:{int(minutes):02d}:{secs:06.3f} / {number}")


@dataclass(frozen=True, slots=True)
class ComparisonSource:
    name: str
    images: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class Comparison:
    title: str
    rows: tuple[ComparisonRow, ...]
    sources: tuple[ComparisonSource, ...]
    directory: Path

    @property
    def is_comparison(self) -> bool:
        """A single source is a plain gallery; slow.pics calls that a collection."""
        return len(self.sources) > 1

    @property
    def total_images(self) -> int:
        return sum(len(source.images) for source in self.sources)

    @property
    def source_names(self) -> list[str]:
        return [source.name for source in self.sources]


def _resolve(path: str | Path) -> Path:
    """Accept either a manifest file or the directory containing one."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(
            f"no manifest at {path}. Run 'kiyas run' first, or point at the output directory."
        )
    return path


def load_manifest(path: str | Path) -> Comparison:
    manifest_path = _resolve(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {manifest_path}: {exc}") from exc

    if not isinstance(payload, dict) or "sources" not in payload:
        raise ManifestError(f"{manifest_path} is not a kiyas manifest")

    directory = manifest_path.parent

    try:
        fps = Fraction(str(payload.get("fps", "24000/1001")))
    except (ValueError, ZeroDivisionError):
        fps = Fraction(24000, 1001)

    # Explicit labels win. A run that has them -- an audio comparison, whose
    # rows are analyses rather than moments -- has nothing sensible to say
    # about frame numbers, and inventing a timestamp for a spectrogram would
    # put a lie in the published labels.
    labels = payload.get("labels")
    if labels:
        rows = tuple(ComparisonRow(str(label)) for label in labels)
    else:
        rows = tuple(ComparisonRow.for_frame(int(n), fps) for n in payload.get("frames") or [])

    sources: list[ComparisonSource] = []
    for entry in payload["sources"]:
        folder = directory / entry["directory"]
        images = tuple(folder / name for name in entry.get("files", []))
        sources.append(ComparisonSource(name=entry["name"], images=images))

    if not sources:
        raise ManifestError(f"{manifest_path} lists no sources")

    comparison = Comparison(
        title=payload.get("title", "") or directory.name,
        rows=rows,
        sources=tuple(sources),
        directory=directory,
    )
    _validate(comparison)
    return comparison


def _validate(comparison: Comparison) -> None:
    """Refuse a set that would upload into a misaligned comparison.

    slow.pics lays a comparison out as a grid: one row per frame, one column
    per source, addressed positionally. A source with a missing image does not
    fail -- it shifts every later row by one, so the labels stop matching the
    pictures and the result looks like a real difference between releases.
    Catching it here is the difference between an error and a wrong answer.
    """
    counts = {source.name: len(source.images) for source in comparison.sources}
    expected = len(comparison.rows)

    if expected and any(count != expected for count in counts.values()):
        raise ManifestError(
            f"the manifest lists {expected} rows but the sources have "
            f"{counts}. Every source needs an image for every row, or the "
            f"comparison columns will not line up."
        )

    missing = [
        str(path) for source in comparison.sources for path in source.images if not path.is_file()
    ]
    if missing:
        shown = "\n  ".join(missing[:10])
        more = f"\n  ... and {len(missing) - 10} more" if len(missing) > 10 else ""
        raise ManifestError(f"these images are missing:\n  {shown}{more}")
