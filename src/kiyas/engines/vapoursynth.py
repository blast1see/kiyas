"""The VapourSynth engine: frame exact, and the one that tonemaps properly.

This is the default for source comparisons. Frames are addressed by index
rather than by timestamp, so two releases of the same title stay aligned no
matter where their keyframes fall, and tonemapping goes through libplacebo with
the file's real HDR metadata.

The transformation order is squash's, and two steps in it are load-bearing:

- **trim before resize and crop**, because trim values are frame indices the
  user read off a previewer showing the untransformed clip.
- **tonemap after crop**, because peak brightness detection averages over the
  frame and letterbox bars drag the measured peak down. On scope-ratio
  material the difference is visible.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator, Mapping
from fractions import Fraction
from pathlib import Path

from ..config import Source, Tonemap
from ..media.probe import HdrFormat, VideoInfo, probe
from .base import ACTIVE_AREA, EngineError, RenderSettings

#: lsmas and BestSource report indexing progress through VapourSynth's logger,
#: one message per percent. Left alone that is 200 lines of noise per source,
#: printed straight over whatever progress display the caller is drawing.
_INDEX_PROGRESS = re.compile(r"index file\s+(\d+)%", re.IGNORECASE)

#: Chatter that is not a problem and must not be reported as one. VapourSynth
#: announces its own log handler on stderr, which would otherwise surface as
#: "indexer said: New message handler installed from python" on every run.
_BENIGN = re.compile(r"message handler installed", re.IGNORECASE)


@contextlib.contextmanager
def _capture_indexing(report: Callable[[str], None] | None, label: str) -> Iterator[list[str]]:
    """Keep indexer chatter off the terminal, collecting anything important.

    L-SMASH does not route its progress through VapourSynth's logger; it writes
    straight to file descriptor 2 from C. Replacing ``sys.stderr`` therefore
    does nothing, and the only thing that works is redirecting the descriptor
    itself. BestSource is better behaved and takes ``showprogress=0``.

    Progress lines are dropped -- 200 of them per source, printed over whatever
    the caller is drawing. Everything else is collected and handed back, because
    a genuine complaint from an indexer ("unsupported codec", "broken index")
    is exactly what the user needs to see when the result looks wrong.
    """
    import os
    import sys
    import tempfile

    import vapoursynth as vs

    leftovers: list[str] = []

    def handler(message_type, message: str) -> None:
        progress_match = _INDEX_PROGRESS.search(message)
        if progress_match:
            if report:
                report(f"indexing {label} {progress_match.group(1)}%")
        elif message_type != vs.MESSAGE_TYPE_INFORMATION and not _BENIGN.search(message):
            leftovers.append(message.strip())

    log_handle = vs.core.add_log_handler(handler)

    sys.stderr.flush()
    try:
        saved_fd = os.dup(2)
    except OSError:
        # No real stderr to redirect (pythonw, some CI capture modes). The
        # progress has nowhere to print anyway.
        saved_fd = None

    sink = tempfile.TemporaryFile(mode="w+b") if saved_fd is not None else None
    if sink is not None:
        os.dup2(sink.fileno(), 2)

    try:
        yield leftovers
    finally:
        with contextlib.suppress(Exception):
            vs.core.remove_log_handler(log_handle)

        if sink is not None and saved_fd is not None:
            with contextlib.suppress(Exception):
                os.dup2(saved_fd, 2)
                os.close(saved_fd)
            sink.seek(0)
            captured = sink.read().decode("utf-8", "replace")
            sink.close()
            for line in captured.replace("\r", "\n").splitlines():
                text = line.strip()
                if text and not _INDEX_PROGRESS.search(text) and not _BENIGN.search(text):
                    leftovers.append(text)


# --- vs-placebo enum values -------------------------------------------------
# Spelled out rather than imported from awsmfunc.types.placebo so that a change
# in that package cannot silently alter what the tonemapper does. Verified
# against vs-placebo 2.0.4.
_CSP_SDR = 0
_CSP_HDR10 = 1
_CSP_DOVI = 3

_TONEMAP_ST2094_40 = 2
_TONEMAP_MOBIUS = 8

_GAMUT_PERCEPTUAL = 1

_META_HDR10 = 2
_META_HDR10PLUS = 3

# VapourSynth colour constants. 2 means "unspecified" for all three.
_UNSPECIFIED = 2
_MATRIX_BT709 = 1
_MATRIX_ST170M = 6
_MATRIX_BT2020NCL = 9
_TRANSFER_BT709 = 1
_TRANSFER_ST2084 = 16
_PRIMARIES_BT709 = 1
_PRIMARIES_BT2020 = 9

#: PNG is lossless at every setting, so this only trades file size against
#: time. 1 is awsmfunc's default and produces files small enough to upload
#: without making a 4K capture noticeably slow.
_FPNG_COMPRESSION = 1


def _active_area(clip):
    """The centre of the frame, for brightness measurement only."""
    import vapoursynth as vs

    margin_x = int(clip.width * (1 - ACTIVE_AREA) / 2)
    margin_y = int(clip.height * (1 - ACTIVE_AREA) / 2)
    # CropRel needs even values on subsampled formats.
    if clip.format.subsampling_w:
        margin_x -= margin_x % (1 << clip.format.subsampling_w)
    if clip.format.subsampling_h:
        margin_y -= margin_y % (1 << clip.format.subsampling_h)
    if margin_x <= 0 and margin_y <= 0:
        return clip
    return vs.core.std.CropRel(clip, margin_x, margin_x, margin_y, margin_y)


class VapourSynthSource:
    """One source with every transformation applied."""

    def __init__(self, clip, name: str, info: VideoInfo, assumptions: list[str], *, overlay: bool):
        self._clip = clip
        self._stats = None
        self._overlay = overlay
        self.name = name
        self.info = info
        self.assumptions = assumptions
        self.frame_count = clip.num_frames
        self.fps = Fraction(clip.fps_num, clip.fps_den) if clip.fps_den else info.fps

    @property
    def supports_frame_types(self) -> bool:
        return True

    @property
    def has_overlay(self) -> bool:
        return self._overlay

    def is_b_frame(self, frame: int) -> bool:
        if not 0 <= frame < self.frame_count:
            return False
        pict_type = self._clip.get_frame(frame).props.get("_PictType")
        if isinstance(pict_type, bytes):
            pict_type = pict_type.decode("ascii", "replace")
        return pict_type == "B"

    def mean_luma(self, frame: int) -> float:
        """Average luma of the active picture area, in [0, 1].

        Measured on the middle of the frame rather than all of it. A scope film
        in a 16:9 container is about a quarter black bars, which drags the mean
        down far enough that "skip dark frames" rejects perfectly good shots --
        seen on a real 2.39:1 remux, where a well-lit frame measured below the
        threshold purely because of the letterbox.

        PlaneStats is attached lazily and only once: it is a filter, and
        rebuilding it per frame would discard the cache behind it.
        """
        if not 0 <= frame < self.frame_count:
            return 0.0
        if self._stats is None:
            import vapoursynth as vs

            self._stats = vs.core.std.PlaneStats(_active_area(self._clip), plane=0)
        return float(self._stats.get_frame(frame).props["PlaneStatsAverage"])

    def write_frames(self, frames: list[int], directory: Path) -> list[Path]:
        import vapoursynth as vs

        directory.mkdir(parents=True, exist_ok=True)
        rgb = self._clip.resize.Spline36(format=vs.RGB24, dither_type="error_diffusion")

        written: list[Path] = []
        for frame in frames:
            if not 0 <= frame < self.frame_count:
                raise EngineError(
                    f"{self.name}: frame {frame} is outside the clip (0..{self.frame_count - 1})"
                )
            path = directory / f"{frame:06d}.png"
            # The whole clip is handed to Write and a single frame requested;
            # that is what actually triggers the encode. Slicing the clip first
            # would renumber it and break the filename.
            vs.core.fpng.Write(
                rgb, filename=str(path), overwrite=True, compression=_FPNG_COMPRESSION
            ).get_frame(frame)
            written.append(path)
        return written

    def close(self) -> None:
        self._clip = None
        self._stats = None


class VapourSynthEngine:
    name = "vapoursynth"

    def available(self, tools: Mapping[str, str] | None = None) -> bool:
        # tools is ignored: this engine is a Python import, not an executable.
        _ = tools
        try:
            import vapoursynth as vs

            loaded = {p.namespace for p in vs.core.plugins()}
        except Exception:  # noqa: BLE001 - an unusable engine is not an error
            return False
        return bool(loaded & {"lsmas", "bs"}) and "placebo" in loaded and "fpng" in loaded

    # -- loading ---------------------------------------------------------

    def _load(self, path: Path, index_dir: Path | None = None):
        import vapoursynth as vs

        core = vs.core
        loaded = {p.namespace for p in core.plugins()}

        # Both indexers write a cache file next to the media by default. That
        # is right for reuse -- indexing a remux is expensive and the cache is
        # worth keeping -- but wrong for read-only or shared libraries, so it
        # can be pointed elsewhere.
        lsmas_options: dict[str, object] = {}
        bs_options: dict[str, object] = {"showprogress": 0}
        if index_dir is not None:
            index_dir.mkdir(parents=True, exist_ok=True)
            lsmas_options["cachedir"] = str(index_dir)
            bs_options["cachepath"] = str(index_dir)

        errors = []
        for namespace, call in (
            ("lsmas", lambda: core.lsmas.LWLibavSource(str(path), **lsmas_options)),
            ("bs", lambda: core.bs.VideoSource(str(path), **bs_options)),
        ):
            if namespace not in loaded:
                continue
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - try the next indexer
                errors.append(f"{namespace}: {exc}")
        # Written out rather than as a conditional expression: mixing `+` and
        # `if/else` in one argument reads as though the concatenation is part
        # of the condition, and it is the kind of line someone "tidies" into an
        # actual bug later.
        if errors:
            raise EngineError(f"no indexer could open {path.name}. " + "; ".join(errors))
        raise EngineError(f"no indexer is installed to open {path.name}")

    def _ensure_colour_props(self, clip, info: VideoInfo) -> tuple[object, list[str]]:
        """Fill in missing colour frame properties.

        Files with no colour tags at all are common -- catalogue Blu-ray remuxes
        especially. VapourSynth then refuses to convert to RGB with "no path
        between colorspaces", and the fix has to guess. The guess is the
        standard one (SD is BT.601, HD and above is BT.709), it is only applied
        where a property is actually absent, and it is reported to the caller
        so a wrong colour cast has an explanation rather than being a mystery.
        """
        import vapoursynth as vs

        props = clip.get_frame(0).props
        assumptions: list[str] = []
        patch: dict[str, int] = {}

        matrix = props.get("_Matrix", _UNSPECIFIED)
        transfer = props.get("_Transfer", _UNSPECIFIED)
        primaries = props.get("_Primaries", _UNSPECIFIED)

        if info.hdr_format.is_hdr:
            default_matrix = _MATRIX_BT2020NCL
            default_transfer = _TRANSFER_ST2084
            default_primaries = _PRIMARIES_BT2020
            basis = "HDR tagging in the container"
        elif clip.height <= 576:
            default_matrix = _MATRIX_ST170M
            default_transfer = _TRANSFER_BT709
            default_primaries = _PRIMARIES_BT709
            basis = f"standard definition ({clip.width}x{clip.height})"
        else:
            default_matrix = _MATRIX_BT709
            default_transfer = _TRANSFER_BT709
            default_primaries = _PRIMARIES_BT709
            basis = f"high definition ({clip.width}x{clip.height})"

        for key, current, default in (
            ("_Matrix", matrix, default_matrix),
            ("_Transfer", transfer, default_transfer),
            ("_Primaries", primaries, default_primaries),
        ):
            if current == _UNSPECIFIED:
                patch[key] = default
                assumptions.append(f"{key}={default}")

        if patch:
            clip = vs.core.std.SetFrameProps(clip, **patch)
            return clip, [f"colour tags missing, assumed {', '.join(assumptions)} from {basis}"]
        return clip, []

    # -- transformations -------------------------------------------------

    def _tonemap_mode(self, source: Source, info: VideoInfo) -> Tonemap:
        if source.tonemap is not Tonemap.AUTO:
            return source.tonemap
        return {
            HdrFormat.HDR10: Tonemap.HDR10,
            HdrFormat.HLG: Tonemap.HDR10,
            HdrFormat.DOVI: Tonemap.DOVI,
            HdrFormat.SDR: Tonemap.NONE,
        }[info.hdr_format]

    def _apply_tonemap(self, clip, mode: Tonemap):
        import vapoursynth as vs

        core = vs.core
        clip = clip.resize.Bicubic(format=vs.YUV444P16)

        common = {
            "dst_csp": _CSP_SDR,
            "dynamic_peak_detection": 0,
            "gamut_mapping": _GAMUT_PERCEPTUAL,
            "contrast_recovery": 0.0,
        }
        if mode is Tonemap.HDR10:
            clip = core.placebo.Tonemap(
                clip,
                src_csp=_CSP_HDR10,
                tone_mapping_function=_TONEMAP_MOBIUS,
                metadata=_META_HDR10,
                use_dovi=False,
                **common,
            )
        elif mode is Tonemap.HDR10PLUS:
            # ST2094-40 follows the per-scene metadata HDR10+ carries, which is
            # the entire reason to prefer it over a static curve.
            clip = core.placebo.Tonemap(
                clip,
                src_csp=_CSP_HDR10,
                tone_mapping_function=_TONEMAP_ST2094_40,
                metadata=_META_HDR10PLUS,
                use_dovi=False,
                **common,
            )
        elif mode is Tonemap.DOVI:
            clip = core.placebo.Tonemap(
                clip,
                src_csp=_CSP_DOVI,
                tone_mapping_function=_TONEMAP_MOBIUS,
                use_dovi=True,
                **common,
            )
        else:
            raise EngineError(f"cannot tonemap with mode {mode!r}")

        return core.std.SetFrameProps(
            clip,
            _Matrix=_MATRIX_BT709,
            _Transfer=_TRANSFER_BT709,
            _Primaries=_PRIMARIES_BT709,
        )

    def _even_format(self, clip, changed: bool):
        """Return to 4:2:0 10-bit when the geometry allows it.

        Odd crops cannot be represented in subsampled chroma, so those clips
        stay 4:4:4. Doing this unconditionally would quietly resample every
        clip twice.
        """
        import vapoursynth as vs

        if changed and clip.width % 2 == 0 and clip.height % 2 == 0:
            return clip.resize.Bicubic(format=vs.YUV420P10)
        return clip

    def prepare(
        self,
        source: Source,
        *,
        target_fps: Fraction | None = None,
        overlay: bool = True,
        tools: dict[str, str] | None = None,
        progress: Callable[[str], None] | None = None,
        index_dir: Path | None = None,
        render: RenderSettings | None = None,
    ) -> VapourSynthSource:
        import vapoursynth as vs

        core = vs.core
        tools = tools or {}

        if render is not None:
            raise EngineError(
                f"{source.name}: the VapourSynth engine cannot render one frame several "
                f"different ways. Tone-mapping curves and GLSL shaders only exist inside a "
                f"player, so a settings comparison is mpv's job."
            )
        if not source.path.is_file():
            raise EngineError(f"{source.name}: no such file: {source.path}")

        info = probe(source.path, ffprobe=tools.get("ffprobe"))
        with _capture_indexing(progress, source.name) as indexer_output:
            clip = self._load(source.path, index_dir)
            clip, assumptions = self._ensure_colour_props(clip, info)
        assumptions.extend(f"indexer said: {line}" for line in indexer_output)

        # --- the fixed order, see config.PROCESSING_ORDER ---
        if source.normalize_fps and target_fps is not None:
            clip = core.std.AssumeFPS(
                clip, fpsnum=target_fps.numerator, fpsden=target_fps.denominator
            )

        if source.trim:
            if source.trim >= clip.num_frames:
                raise EngineError(
                    f"{source.name}: trim of {source.trim} frames leaves nothing "
                    f"(the clip has {clip.num_frames})"
                )
            clip = clip[source.trim :]

        if source.resize:
            width, height = source.resize
            clip = clip.resize.Spline36(width, height, dither_type="error_diffusion")

        if source.crop:
            left, right, top, bottom = source.crop
            odd = any(value % 2 for value in source.crop)
            if odd:
                clip = clip.resize.Bicubic(format=vs.YUV444P16)
            if left + right >= clip.width or top + bottom >= clip.height:
                raise EngineError(
                    f"{source.name}: crop {source.crop} removes the whole frame "
                    f"({clip.width}x{clip.height})"
                )
            clip = core.std.CropRel(clip, left, right, top, bottom)
            clip = self._even_format(clip, odd)

        mode = self._tonemap_mode(source, info)
        if mode is not Tonemap.NONE:
            clip = self._apply_tonemap(clip, mode)

        if source.luma_fix:
            import awsmfunc as awf

            clip = awf.fixlvls(clip)

        label = source.name
        if mode is not Tonemap.NONE:
            label = f"{label} (tonemapped {mode.value})"
        if source.luma_fix:
            label = f"{label} (luma adjusted)"

        if overlay:
            import awsmfunc as awf

            clip = awf.FrameInfo(clip, label)

        return VapourSynthSource(clip, source.name, info, assumptions, overlay=overlay)
