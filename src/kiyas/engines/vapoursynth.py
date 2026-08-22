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

from ..config import DoviEl, Source, Tonemap
from ..media.binaries import BinaryNotFound
from ..media.dovi import DoviError, extract_enhancement_layer
from ..media.probe import HdrFormat, VideoInfo, probe
from .base import ACTIVE_AREA, LUMA_SAMPLE, EngineError, RenderSettings, label_for

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

    # `sys.stderr` is None in a windowed build, not merely closed: a GUI entry
    # point runs without a console and Python leaves the streams unset. The
    # line below used to be an unguarded flush, so opening any source with this
    # engine from the window died on 'NoneType' object has no attribute
    # 'flush' -- before a single frame was read. The check under it already
    # named pythonw as a case; this one was written as though stderr were
    # always an object.
    if sys.stderr is not None:
        with contextlib.suppress(Exception):
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

#: How many consecutive frames to look at when asking whether an encode uses
#: B-frames. A GOP is a few dozen frames, so this covers more than one.
_B_FRAME_PROBE = 96

#: How hard VFM looks for combs, and why this is not its default of 9.
#:
#: It is a rejection rule, so a false positive throws away a good frame while
#: a false negative merely lets one through -- the conservative direction is
#: fewer positives. Measured on a real 480p film clip and the interlaced copy
#: of itself, 30 frames each: at cthresh 6 it caught 28 of 30 combed frames,
#: at 9 it caught 19, and at both it flagged **none** of the progressive
#: original. What decided it is the other end -- on ffmpeg's `testsrc2`, whose
#: hard horizontal edges look exactly like combing, cthresh 9 flagged 20
#: progressive frames out of 20. Real film is not testsrc2, but animation has
#: hard edges too, and that is why this rule is off unless it is asked for.
_COMB_THRESHOLD = 9

#: VFM needs a field order and this is a detector, not a field matcher. It
#: barely matters: on the clip above, order 0 and order 1 differed by at most
#: one frame in thirty at every threshold tried.
_COMB_FIELD_ORDER = 1

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
        self._small = None
        self._combed = None
        self._has_b_frames: bool | None = None
        self._overlay = overlay
        self.name = name
        self.info = info
        self.assumptions = assumptions
        self.frame_count = clip.num_frames
        self.width = clip.width
        self.height = clip.height
        self.fps = Fraction(clip.fps_num, clip.fps_den) if clip.fps_den else info.fps

    @property
    def supports_frame_types(self) -> bool:
        return True

    @property
    def has_overlay(self) -> bool:
        return self._overlay

    @property
    def has_b_frames(self) -> bool:
        """Whether this encode contains B-frames at all.

        Sampled from the middle of the clip, where an encode's normal frame
        pattern lives. A file with B-frames has them everywhere; a WEB-DL
        encoded without them has none anywhere, and on one the B-frame rule
        would otherwise reject every frame in the file.
        """
        if self._has_b_frames is None:
            middle = self.frame_count // 2
            span = range(middle, min(middle + _B_FRAME_PROBE, self.frame_count))
            self._has_b_frames = any(self.picture_type(frame) == "B" for frame in span)
        return self._has_b_frames

    def picture_type(self, frame: int) -> str | None:
        if not 0 <= frame < self.frame_count:
            return None
        pict_type = self._clip.get_frame(frame).props.get("_PictType")
        if isinstance(pict_type, bytes):
            pict_type = pict_type.decode("ascii", "replace")
        return str(pict_type) if pict_type else None

    def combed(self, frame: int) -> bool | None:
        """Whether ``frame`` shows interlacing combs.

        VFM is a field matcher rather than a comb detector, and its ``_Combed``
        prop means "the match it settled on still looks combed". That is the
        answer wanted here, but only once it is given something it can work
        with: it takes 8-bit YUV only, and the conversion has to be
        depth-only. Combing is a pattern between adjacent *lines*, so scaling
        the picture down to look for it would remove the thing being looked
        for.
        """
        if not 0 <= frame < self.frame_count:
            return None
        if self._combed is None:
            import vapoursynth as vs

            self._combed = vs.core.vivtc.VFM(
                self._clip.resize.Point(format=vs.YUV420P8),
                order=_COMB_FIELD_ORDER,
                cthresh=_COMB_THRESHOLD,
                mode=0,
            )
        return bool(self._combed.get_frame(frame).props.get("_Combed", 0))

    def mean_luma(self, frame: int) -> float:
        """Average luma of the active picture area, in [0, 1].

        Measured on the middle of the frame rather than all of it. A scope film
        in a 16:9 container is about a quarter black bars, which drags the mean
        down far enough that "skip dark frames" rejects perfectly good shots --
        seen on a real 2.39:1 remux, where a well-lit frame measured below the
        threshold purely because of the letterbox.

        **Reduced to full-range 8-bit grey first, as the ffmpeg engine does.**
        Reading PlaneStats off the clip in its own format looks simpler and
        quietly measures a different quantity: a limited-range YUV plane puts
        black at 16/255, so the same frame reads well above what ffmpeg
        reports, and the same threshold means two different things. Measured on
        a real remux before this: the engines returned *opposite* verdicts on
        two frames out of six, which is the same project picking different
        frames depending on which engine ran.

        They still do not agree exactly -- VapourSynth reads about 0.022 higher
        across the board, from rounding in the crop rather than anything chosen
        here -- but a constant offset that small sits well inside the margin
        the threshold is picked at, and the verdicts now match.

        The chain is attached lazily and only once: it is a filter, and
        rebuilding it per frame would discard the cache behind it.
        """
        if not 0 <= frame < self.frame_count:
            return 0.0
        if self._stats is None:
            import vapoursynth as vs

            self._stats = vs.core.std.PlaneStats(self._small_clip(), plane=0)
        return float(self._stats.get_frame(frame).props["PlaneStatsAverage"])

    def _small_clip(self):
        """The reduced grey clip both brightness and alignment read.

        Built once and kept: it is a filter chain, and rebuilding it per frame
        would throw away the cache behind it.
        """
        if self._small is None:
            import vapoursynth as vs

            self._small = vs.core.resize.Bicubic(
                _active_area(self._clip),
                width=LUMA_SAMPLE[0],
                height=LUMA_SAMPLE[1],
                format=vs.GRAY8,
                range_s="full",
            )
        return self._small

    def luma_thumbnails(self, start: int, count: int, step: int = 1) -> list[bytes]:
        """``count`` consecutive luma thumbnails from ``start``.

        Exactly the reduction :meth:`mean_luma` averages over, so the two
        engines are looking at the same quantity here as they are there.
        """
        if count <= 0 or self.frame_count <= 0:
            return []
        start = max(0, min(start, self.frame_count - 1))
        count = min(count, self.frame_count - start)
        small = self._small_clip()
        return [
            bytes(small.get_frame(index)[0]) for index in range(start, start + count, max(1, step))
        ]

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

        # The order matters for one reason worth knowing before changing it:
        # read out of the shipped binaries, lsmas sets `DolbyVisionRPU` but not
        # `HDR10Plus`, while BestSource sets both. Nothing here reads
        # `HDR10Plus` -- vs-placebo ignores it, which _apply_tonemap documents
        # -- so today the choice costs nothing. If that ever changes, this line
        # is what gates it.
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

    def _apply_tonemap(self, clip, mode: Tonemap, enhancement_layer=None):
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
            # This selects the ST2094-40 tone *curve*. It does not apply the
            # per-scene metadata, and this comment used to claim it did.
            #
            # Measured against vs-placebo 2.0.4, on a real remux carrying HDR10+
            # (BestSource exposes it as an ``HDR10Plus`` frame property, 56
            # bytes a frame; lsmas does not expose it at all):
            #
            #   metadata=HDR10 vs metadata=HDR10+   ->  identical output
            #   removing the HDR10Plus frame prop   ->  identical output
            #   changing tone_mapping_function      ->  different output
            #
            # So the curve is live and the metadata path is not. Passing
            # ``metadata`` is kept because it costs nothing and a later
            # vs-placebo may honour it -- but until someone measures that
            # again, this is a different curve applied statically, and calling
            # it dynamic tone mapping in a comparison would be a lie about what
            # the reader is looking at.
            clip = core.placebo.Tonemap(
                clip,
                src_csp=_CSP_HDR10,
                tone_mapping_function=_TONEMAP_ST2094_40,
                metadata=_META_HDR10PLUS,
                use_dovi=False,
                **common,
            )
        elif mode is Tonemap.DOVI:
            extra = {}
            if enhancement_layer is not None:
                # vs-placebo composes the enhancement layer and maps the result
                # in the same pass -- there is no call that does only the
                # composition. Both clips have to be 16-bit YUV carrying their
                # DolbyVisionRPU frame prop, which is what the plugin checks.
                extra["dovi_el"] = enhancement_layer.resize.Bicubic(format=vs.YUV444P16)
            clip = core.placebo.Tonemap(
                clip,
                src_csp=_CSP_DOVI,
                tone_mapping_function=_TONEMAP_MOBIUS,
                use_dovi=True,
                **extra,
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

    def _open_enhancement_layer(
        self,
        source: Source,
        info: VideoInfo,
        mode: Tonemap,
        *,
        tools: dict[str, str],
        index_dir: Path | None,
        progress: Callable[[str], None] | None,
        assumptions: list[str],
    ):
        """The clip to compose into ``source``, or ``None`` to leave it alone.

        Every way this can decline is a note rather than a failure. A base
        layer on its own is still a picture of the film, so refusing to produce
        the comparison would throw away material the user may well have meant
        to compare that way -- but it is a *different* picture from the one a
        Dolby Vision player shows, so saying nothing is not an option either.
        """
        if source.dovi_el is DoviEl.OFF:
            return None

        if not info.has_enhancement_layer:
            if source.dovi_el is DoviEl.ON:
                raise EngineError(
                    f"{source.name}: dovi_el = 'on', but ffprobe reports "
                    f"dv_profile={info.dovi_profile}, el_present_flag="
                    f"{int(info.dovi_el_present)}. Only a profile 7 file carries a "
                    f"picture-bearing enhancement layer; there is nothing here to bake."
                )
            return None

        # From here the file does have a layer. Under 'auto' kiyas says so and
        # captures the base layer anyway, because extracting the layer reads the
        # whole file -- tens of gigabytes on a disc remux -- and doing that
        # without being asked is a worse surprise than a stated caveat.
        if source.dovi_el is DoviEl.AUTO:
            assumptions.append(
                "carries a Dolby Vision profile 7 enhancement layer, which is not composed: "
                "these are screenshots of the base layer alone. Add \"dovi_el = 'on'\" to "
                "this source to bake it in, which reads the whole file once"
            )
            return None

        if source.resize:
            raise EngineError(
                f"{source.name}: 'resize' cannot be combined with dovi_el = 'on'. The "
                f"enhancement layer is composed pixel against pixel with the base layer, "
                f"and resampling either of them destroys that correspondence. Set "
                f"dovi_el = 'off' to compare the base layer alone, or drop the resize."
            )

        if mode is not Tonemap.DOVI:
            raise EngineError(
                f"{source.name}: dovi_el = 'on' needs Dolby Vision tone mapping, but this "
                f"source resolved to '{mode.value}'. The layer is composed inside that one "
                f"pass, so there is no way to bake it and map some other way."
            )

        cache_dir = index_dir if index_dir is not None else source.path.parent
        try:
            layer = extract_enhancement_layer(
                source.path,
                cache_dir,
                ffmpeg=tools.get("ffmpeg"),
                dovi_tool=tools.get("dovi_tool"),
                progress=progress,
            )
        except BinaryNotFound as exc:
            # 'on' is an explicit request for a specific picture, so failing to
            # produce it is an error rather than a note. Under 'auto' this code
            # is never reached.
            raise EngineError(
                f"{source.name}: dovi_el = 'on' needs dovi_tool, which was not found. "
                f"Install it and put it on PATH, name it under [tools], or set "
                f"dovi_el = 'off'. 'kiyas doctor' reports what it can see."
            ) from exc
        except DoviError as exc:
            raise EngineError(f"{source.name}: {exc}") from exc

        with _capture_indexing(progress, f"{source.name} enhancement layer") as indexer_output:
            clip = self._load(layer, index_dir)
        assumptions.extend(f"indexer said: {line}" for line in indexer_output)

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

        mode = self._tonemap_mode(source, info)
        enhancement = self._open_enhancement_layer(
            source,
            info,
            mode,
            tools=tools,
            index_dir=index_dir,
            progress=progress,
            assumptions=assumptions,
        )

        # --- the fixed order, see config.PROCESSING_ORDER ---
        # Whatever happens to the base layer happens to the enhancement layer
        # in the same statement. They are composed pixel against pixel, so a
        # transformation applied to one and not the other is not a wrong
        # picture, it is a wrong picture that still looks like a picture.
        if source.normalize_fps and target_fps is not None:
            clip = core.std.AssumeFPS(
                clip, fpsnum=target_fps.numerator, fpsden=target_fps.denominator
            )
            if enhancement is not None:
                enhancement = core.std.AssumeFPS(
                    enhancement, fpsnum=target_fps.numerator, fpsden=target_fps.denominator
                )

        if source.trim:
            if source.trim >= clip.num_frames:
                raise EngineError(
                    f"{source.name}: trim of {source.trim} frames leaves nothing "
                    f"(the clip has {clip.num_frames})"
                )
            clip = clip[source.trim :]
            if enhancement is not None:
                enhancement = enhancement[source.trim :]

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
            if enhancement is not None:
                enhancement = core.std.CropRel(enhancement, left, right, top, bottom)

        if mode is not Tonemap.NONE:
            clip = self._apply_tonemap(clip, mode, enhancement)

        if source.luma_fix:
            import awsmfunc as awf

            clip = awf.fixlvls(clip)

        label = label_for(source, mode, baked_el=enhancement is not None)

        if overlay:
            import awsmfunc as awf

            clip = awf.FrameInfo(clip, label)

        return VapourSynthSource(clip, source.name, info, assumptions, overlay=overlay)
