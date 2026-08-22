"""Turning a measured track into pictures you can put side by side.

Three, and each answers a question the others cannot:

**Spectrogram** — where the energy is, over time and frequency. This is the one
that shows a lossy encode's low-pass as a hard ceiling, and a fake 5.1 upmix as
surround channels that are copies of the front ones. ffmpeg renders the
spectrum itself, because doing that in Python would mean holding the decoded
track in memory; the axes and layout are added here, for reasons given in
:func:`_render_channel_tiles`.

**Waveform** — level over time, with clipping marked. Shows dynamic range: a
modern loudness-war master is a solid block, a film mix is not.

**Frequency response** — the spectrogram averaged into a curve, which is the
form you can lay two tracks over each other in and read a difference off.

All three are drawn on a dark background at a fixed size. Fixed because a
comparison is flipped between images and anything that moves between them --
including the axes -- reads as a difference in the content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..media import binaries
from .analysis import AnalysisError, AudioAnalysis
from .probe import AudioTrack

#: Every picture in an audio comparison is this size. slow.pics lays images out
#: in a grid and flipping between two of different sizes is unreadable.
FIGURE_SIZE = (1600, 900)

#: Rendering a spectrogram of a two-hour track is minutes of work in ffmpeg.
_RENDER_TIMEOUT = 1800.0

_BACKGROUND = "#111318"
_FOREGROUND = "#e6e6e6"
_GRID = "#2a2e37"
_TRACE = "#6fb2ff"
_CLIP = "#ff5555"


#: Pixels of spectrum rendered per channel before it is laid into the figure.
#: Rendering larger than the row it ends up in keeps detail that ffmpeg's own
#: downscaling would otherwise throw away.
_SPECTRUM_TILE = (1600, 400)


def _render_channel_tiles(track: AudioTrack, directory: Path, *, ffmpeg: Path) -> list[Path]:
    """One bare spectrogram image per channel, from a single decode.

    **Not ``mode=separate``.** That is the filter's own way of doing this and it
    crashes: on a 5.1 track, ffmpeg N-124864 died with an access violation in
    roughly two runs in five, at every image size tried, with and without the
    legend. One channel at a time never crashed in any run, and splitting the
    stream inside the same graph keeps it to a single decode -- which matters,
    because the decode is the expensive part on a feature-length track.

    The tiles carry no axes. They are laid out with real ones by
    :func:`spectrogram`, which also keeps every track's picture the same size:
    ffmpeg's own legend adds margins per channel, so a 5.1 track and a stereo
    one would come out different heights and stop being comparable.
    """
    channels = track.channels
    width, height = _SPECTRUM_TILE
    draw = (
        f"showspectrumpic=s={width}x{height}:legend=0:color=intensity:scale=log:fscale=lin:gain=3"
    )
    tiles = [directory / f"tile{index}.png" for index in range(channels)]

    graph: list[str] = []
    if channels == 1:
        graph.append(f"[0:a:{track.index}]{draw}[v0]")
    else:
        labels = "".join(f"[a{index}]" for index in range(channels))
        graph.append(f"[0:a:{track.index}]asplit={channels}{labels}")
        graph.extend(
            f"[a{index}]pan=mono|c0=c{index},{draw}[v{index}]" for index in range(channels)
        )

    args = [str(ffmpeg), "-y", "-v", "error", "-nostdin", "-i", str(track.path)]
    args += ["-filter_complex", ";".join(graph)]
    for index, tile in enumerate(tiles):
        args += ["-map", f"[v{index}]", "-frames:v", "1", str(tile)]

    try:
        proc = subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            text=True,
            timeout=_RENDER_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=binaries.no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AnalysisError(f"could not render a spectrogram of {track.path.name}: {exc}") from exc

    if proc.returncode != 0 or any(not tile.is_file() for tile in tiles):
        detail = (proc.stderr or "").strip().splitlines()
        raise AnalysisError(
            f"ffmpeg could not render a spectrogram of {track.path.name} "
            f"(exit {proc.returncode}): {detail[-1] if detail else 'no output'}"
        )
    return tiles


def spectrogram(
    track: AudioTrack,
    destination: Path,
    *,
    ffmpeg: str | Path | None = None,
    channel_labels: list[str] | None = None,
) -> Path:
    """Render a per-channel spectrogram of the whole track, with axes."""
    import tempfile

    import matplotlib.image as mpimg

    binary = binaries.require_binary("ffmpeg", ffmpeg)
    labels = channel_labels or [f"ch{n + 1}" for n in range(track.channels)]
    nyquist = track.sample_rate / 2

    with tempfile.TemporaryDirectory(prefix="kiyas-spectrum-") as scratch:
        tiles = _render_channel_tiles(track, Path(scratch), ffmpeg=binary)
        images = [mpimg.imread(tile) for tile in tiles]

    plt, figure = _figure()
    try:
        axes_list = figure.subplots(len(images), 1, sharex=True, squeeze=False)[:, 0]
        for axes, image, name in zip(axes_list, images, labels, strict=True):
            axes.imshow(
                image,
                aspect="auto",
                # "upper", because ffmpeg already draws the tile with the
                # highest frequency in its first row. Flipping it as well puts
                # a 1 kHz tone at 22 kHz -- which looks like a perfectly
                # plausible spectrogram of something else.
                origin="upper",
                extent=(0.0, track.duration, 0.0, nyquist / 1000),
                interpolation="nearest",
            )
            axes.set_ylabel(f"{name}\nkHz", color=_FOREGROUND, fontsize=8)
            axes.tick_params(colors=_FOREGROUND, labelsize=8)
            for spine in axes.spines.values():
                spine.set_color(_GRID)
        axes_list[0].set_title(
            f"{track.path.name} — spectrogram", color=_FOREGROUND, fontsize=11, loc="left"
        )
        axes_list[-1].set_xlabel("seconds", color=_FOREGROUND, fontsize=9)
        figure.tight_layout()
        destination.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(destination, facecolor=_BACKGROUND)
    finally:
        plt.close(figure)
    return destination


def _figure():
    """A dark figure at exactly FIGURE_SIZE pixels."""
    import matplotlib

    matplotlib.use("Agg")  # no display, and none needed
    import matplotlib.pyplot as plt

    width, height = FIGURE_SIZE
    dpi = 100
    figure = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor=_BACKGROUND)
    return plt, figure


def _style(axes, *, title: str) -> None:
    axes.set_facecolor(_BACKGROUND)
    axes.set_title(title, color=_FOREGROUND, fontsize=11, loc="left")
    axes.grid(True, color=_GRID, linewidth=0.6)
    axes.tick_params(colors=_FOREGROUND, labelsize=8)
    for spine in axes.spines.values():
        spine.set_color(_GRID)


def waveform(analysis: AudioAnalysis, destination: Path) -> Path:
    """Level over time, one row per channel, clipping marked in red."""
    if analysis.envelope.size == 0:
        raise AnalysisError(f"{analysis.track.path.name}: nothing to draw a waveform from")

    import numpy as np

    plt, figure = _figure()
    try:
        channels = len(analysis.channel_names)
        windows = analysis.envelope.shape[0]
        seconds = np.linspace(0, analysis.duration, windows)
        axes_list = figure.subplots(channels, 1, sharex=True, squeeze=False)[:, 0]

        for index, (axes, name) in enumerate(zip(axes_list, analysis.channel_names, strict=True)):
            low = analysis.envelope[:, index, 0]
            high = analysis.envelope[:, index, 1]
            axes.fill_between(seconds, low, high, color=_TRACE, linewidth=0)
            axes.set_ylim(-1.05, 1.05)
            axes.set_ylabel(name, color=_FOREGROUND, fontsize=9)
            # Full scale drawn as a line, so "how close does it get" is
            # readable rather than a matter of judging the top of the plot.
            for level in (-1.0, 1.0):
                axes.axhline(level, color=_GRID, linewidth=0.8)
            _style(axes, title="")
            if analysis.clipped_runs[index]:
                for position in analysis.clip_positions:
                    axes.axvline(position, color=_CLIP, linewidth=0.5, alpha=0.6)
                axes.set_ylabel(
                    f"{name}\n{analysis.clipped_runs[index]} clipped",
                    color=_CLIP,
                    fontsize=9,
                )

        axes_list[0].set_title(
            f"{analysis.track.path.name} — waveform (peak {analysis.overall_peak_dbfs:.1f} dBFS)",
            color=_FOREGROUND,
            fontsize=11,
            loc="left",
        )
        axes_list[-1].set_xlabel("seconds", color=_FOREGROUND, fontsize=9)
        figure.tight_layout()
        destination.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(destination, facecolor=_BACKGROUND)
    finally:
        plt.close(figure)
    return destination


def frequency_response(analysis: AudioAnalysis, destination: Path) -> Path:
    """One curve per channel, averaged over the whole track."""
    import numpy as np

    plt, figure = _figure()
    try:
        axes = figure.add_subplot(111)
        frequencies = analysis.frequencies
        # Skip DC: it is not audio, and on a log axis it is at minus infinity.
        usable = frequencies > 20
        # Alternating dashes, because duplicated channels lie exactly on top of
        # each other: with solid lines the second one is invisible and the plot
        # silently claims the track has fewer channels than it does.
        styles = ("-", "--", "-.", ":")
        for index, name in enumerate(analysis.channel_names):
            axes.semilogx(
                frequencies[usable],
                analysis.spectrum[index][usable],
                linewidth=1.0,
                linestyle=styles[index % len(styles)],
                label=name,
            )
        axes.set_xlim(20, max(20000, float(frequencies[-1])))
        # A floor rather than autoscale: an empty channel sits at -200 dB and
        # would otherwise flatten every real curve into a straight line.
        top = float(np.max(analysis.spectrum[:, usable])) if usable.any() else 0.0
        axes.set_ylim(top - 120, top + 5)
        axes.set_xlabel("Hz", color=_FOREGROUND, fontsize=9)
        axes.set_ylabel("dB", color=_FOREGROUND, fontsize=9)
        _style(axes, title=f"{analysis.track.path.name} — average frequency response")
        legend = axes.legend(loc="lower left", fontsize=8, facecolor=_BACKGROUND, framealpha=0.8)
        for text in legend.get_texts():
            text.set_color(_FOREGROUND)
        figure.tight_layout()
        destination.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(destination, facecolor=_BACKGROUND)
    finally:
        plt.close(figure)
    return destination
