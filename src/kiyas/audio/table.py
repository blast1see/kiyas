"""The specification table.

Two columns of numbers per track, side by side, in Markdown and in BBCode. It
exists because half of an audio comparison is not a picture: "24-bit" against
"24-bit container, 16 bits used" settles an argument that no spectrogram will.

Measured values are marked as measured. Where a file's claim and the
measurement disagree, both are shown -- reporting only the measurement would
hide the more interesting half, which is that the file is wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

from .analysis import AudioAnalysis
from .sync import Offset


def _bitrate(value: int | None) -> str:
    if not value:
        return "—"
    return f"{value / 1000:.0f} kbps"


def _rows(analyses: Sequence[AudioAnalysis]) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []

    def add(label: str, values: list[str]) -> None:
        rows.append((label, values))

    add("codec", [a.track.codec for a in analyses])
    add("profile", [a.track.profile or "—" for a in analyses])
    add("channels", [f"{a.track.channels} ({a.track.channel_layout})" for a in analyses])
    add("sample rate", [f"{a.track.sample_rate / 1000:g} kHz" for a in analyses])
    add("bitrate", [_bitrate(a.track.bitrate) for a in analyses])
    add("declared bit depth", [str(a.track.bit_depth or "—") for a in analyses])
    add(
        "measured bit depth",
        [str(a.real_bit_depth) if a.real_bit_depth else "not measurable" for a in analyses],
    )
    add("duration", [f"{a.duration:.3f} s" for a in analyses])
    add("peak", [f"{a.overall_peak_dbfs:.2f} dBFS" for a in analyses])
    add("loudest channel RMS", [f"{max(a.rms_dbfs):.2f} dBFS" for a in analyses])
    add("clipped runs", [str(a.clipped) for a in analyses])
    add(
        "silent channels",
        [", ".join(a.silent_channels) if a.silent_channels else "none" for a in analyses],
    )
    add(
        "identical channels",
        [
            ", ".join(f"{left}={right}" for left, right in a.duplicate_channels) or "none"
            for a in analyses
        ],
    )
    add("language", [a.track.language or "—" for a in analyses])

    # Whatever MediaInfo knew about any of them, so a field that only one file
    # carries still shows up -- dialnorm usually being that field. Anything
    # already covered above is skipped: the same label twice with different
    # values reads as a contradiction rather than two sources agreeing.
    covered = {label for label, _ in rows}
    extra_keys: list[str] = []
    for analysis in analyses:
        for key in analysis.track.extra:
            if key not in extra_keys and key not in covered:
                extra_keys.append(key)
    for key in extra_keys:
        add(key, [a.track.extra.get(key, "—") for a in analyses])

    return rows


def markdown(analyses: Sequence[AudioAnalysis], *, offsets: Sequence[Offset] = ()) -> str:
    """A Markdown table, one column per track."""
    if not analyses:
        return ""
    names = [a.track.path.name for a in analyses]
    lines = ["| | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for label, values in _rows(analyses):
        lines.append(f"| **{label}** | " + " | ".join(values) + " |")

    if offsets:
        lines.append("")
        lines.append("Offset against the first track:")
        for name, offset in zip(names[1:], offsets, strict=True):
            lines.append(f"- **{name}**: {offset.summary}")

    notes = [(a.track.path.name, note) for a in analyses for note in a.notes]
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- **{name}**: {note}" for name, note in notes)
    return "\n".join(lines)


def bbcode(analyses: Sequence[AudioAnalysis], *, offsets: Sequence[Offset] = ()) -> str:
    """The same table in BBCode, for forums that do not take Markdown."""
    if not analyses:
        return ""
    names = [a.track.path.name for a in analyses]
    lines = ["[table]", "[tr][td][/td]" + "".join(f"[td][b]{n}[/b][/td]" for n in names) + "[/tr]"]
    for label, values in _rows(analyses):
        cells = "".join(f"[td]{value}[/td]" for value in values)
        lines.append(f"[tr][td][b]{label}[/b][/td]{cells}[/tr]")
    lines.append("[/table]")

    if offsets:
        lines.append("")
        for name, offset in zip(names[1:], offsets, strict=True):
            lines.append(f"[b]{name}[/b]: {offset.summary}")

    notes = [(a.track.path.name, note) for a in analyses for note in a.notes]
    if notes:
        lines.append("")
        lines.extend(f"[b]{name}[/b]: {note}" for name, note in notes)
    return "\n".join(lines)
