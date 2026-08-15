"""Settings comparisons: one file, several renderings.

The integration test at the bottom is the one that matters. It uses a clip
where frame N is a flat grey of value 2N, so the mean of a capture says which
frame it is with no ambiguity -- which is how the wrong-frame bug in mpv's
window screenshot was found in the first place, and the only way to be sure it
stays fixed.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from kiyas import config, engines, run
from kiyas.config import ConfigError, Engine, Mode, Source
from kiyas.engines import EngineError
from kiyas.engines.base import RenderSettings
from kiyas.media import binaries

SETTINGS = """
title = "Curves"
mode = "settings"

[[source]]
path = "a.mkv"
name = "the file"

[settings]
template = "tonemap"
"""


def write(tmp_path, text: str, name: str = "project.toml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Project file
# --------------------------------------------------------------------------


def test_a_template_becomes_the_columns(tmp_path):
    project = config.load(write(tmp_path, SETTINGS))

    assert project.mode is Mode.SETTINGS
    assert "spline" in project.column_names
    assert all(v.options for v in project.settings.variants)


def test_explicit_variants_and_a_template_can_be_combined(tmp_path):
    project = config.load(
        write(
            tmp_path,
            SETTINGS
            + """
            [[variant]]
            name = "clip"
            options = { tone-mapping = "clip" }
            """,
        )
    )

    assert project.column_names[-1] == "clip"


def test_the_base_table_applies_to_every_variant(tmp_path):
    project = config.load(
        write(
            tmp_path,
            SETTINGS.replace(
                'template = "tonemap"', 'template = "tonemap"\nbase = { target-peak = 203 }'
            ),
        )
    )

    assert all(v.options["target-peak"] == "203" for v in project.settings.variants)


def test_a_variant_overrides_the_base_rather_than_being_overridden(tmp_path):
    project = config.load(
        write(
            tmp_path,
            """
            mode = "settings"

            [[source]]
            path = "a.mkv"
            name = "f"

            [settings]
            base = { tone-mapping = "clip" }

            [[variant]]
            name = "spline"
            options = { tone-mapping = "spline" }

            [[variant]]
            name = "inherits"
            options = { gamut-mapping-mode = "perceptual" }
            """,
        )
    )

    by_name = {v.name: v.options for v in project.settings.variants}
    assert by_name["spline"]["tone-mapping"] == "spline"
    assert by_name["inherits"]["tone-mapping"] == "clip"


def test_a_single_variant_is_not_a_comparison(tmp_path):
    with pytest.raises(ConfigError, match="at least two variants"):
        config.load(
            write(
                tmp_path,
                """
                mode = "settings"

                [[source]]
                path = "a.mkv"
                name = "f"

                [[variant]]
                name = "only one"
                options = { deband = true }
                """,
            )
        )


def test_a_variant_that_changes_nothing_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="renders the same picture"):
        config.load(
            write(
                tmp_path,
                """
                mode = "settings"

                [[source]]
                path = "a.mkv"
                name = "f"

                [[variant]]
                name = "a"
                options = { deband = true }

                [[variant]]
                name = "b"
                """,
            )
        )


def test_duplicate_variant_names_are_refused(tmp_path):
    with pytest.raises(ConfigError, match="unique"):
        config.load(
            write(
                tmp_path,
                """
                mode = "settings"

                [[source]]
                path = "a.mkv"
                name = "f"

                [[variant]]
                name = "same"
                options = { deband = true }

                [[variant]]
                name = "same"
                options = { deband = false }
                """,
            )
        )


def test_several_sources_in_settings_mode_are_refused(tmp_path):
    with pytest.raises(ConfigError, match="exactly one"):
        config.load(
            write(
                tmp_path,
                SETTINGS
                + """
                [[source]]
                path = "b.mkv"
                name = "second"
                """,
            )
        )


def test_variants_in_a_source_comparison_are_refused(tmp_path):
    """Silently ignoring them would produce a comparison nobody asked for."""
    with pytest.raises(ConfigError, match="only mean something"):
        config.load(
            write(
                tmp_path,
                """
                [[source]]
                path = "a.mkv"
                name = "A"

                [[source]]
                path = "b.mkv"
                name = "B"

                [[variant]]
                name = "x"
                options = { deband = true }
                """,
            )
        )


def test_b_frame_selection_is_turned_off(tmp_path):
    """Every column is the same decoded frame, so there is nothing to be fair about."""
    project = config.load(write(tmp_path, SETTINGS))

    assert project.frames.b_frames_only is False


def test_relative_shader_paths_resolve_against_the_project_file(tmp_path):
    (tmp_path / "mine.glsl").write_text("//!HOOK MAIN\n", encoding="utf-8")
    project = config.load(
        write(
            tmp_path,
            """
            mode = "settings"

            [[source]]
            path = "a.mkv"
            name = "f"

            [settings]
            template = "shaders"
            shaders = ["mine.glsl"]
            """,
        )
    )

    resolved = project.settings.variants[1].options["glsl-shaders"]
    assert Path(resolved).is_absolute()
    assert Path(resolved).is_file()


def test_a_shader_that_is_not_there_is_reported_by_name(tmp_path):
    with pytest.raises(ConfigError, match="no shader at"):
        config.load(
            write(
                tmp_path,
                """
                mode = "settings"

                [[source]]
                path = "a.mkv"
                name = "f"

                [settings]
                template = "shaders"
                shaders = ["typo.glsl"]
                """,
            )
        )


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def test_columns_are_the_variants_not_the_file(tmp_path):
    project = config.load(write(tmp_path, SETTINGS))

    plan = run.columns(project)

    assert len(plan) == len(project.settings.variants)
    assert all(source is project.sources[0] for source, _ in plan)
    assert [render.name for _, render in plan] == project.column_names


def test_columns_in_a_source_comparison_carry_no_render_settings(tmp_path):
    project = config.load(
        write(
            tmp_path,
            """
            [[source]]
            path = "a.mkv"
            name = "A"

            [[source]]
            path = "b.mkv"
            name = "B"
            """,
        )
    )

    assert [render for _, render in run.columns(project)] == [None, None]


def test_capture_size_reaches_every_column(tmp_path):
    project = config.load(
        write(
            tmp_path, SETTINGS.replace('template = "tonemap"', 'template = "tonemap"\nwidth = 1280')
        )
    )

    assert all(render.width == 1280 for _, render in run.columns(project))


def test_settings_mode_refuses_another_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "available_engines", lambda tools=None: ["ffmpeg", "mpv"])
    project = config.load(write(tmp_path, SETTINGS.replace("mode =", 'engine = "ffmpeg"\nmode =')))

    assert project.engine is Engine.FFMPEG
    with pytest.raises(run.RunError, match="always renders with mpv"):
        run.choose_engine(project)


def test_settings_mode_without_mpv_says_why_it_is_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "available_engines", lambda tools=None: ["ffmpeg"])
    project = config.load(write(tmp_path, SETTINGS))

    with pytest.raises(run.RunError, match="only engine"):
        run.choose_engine(project)


# --------------------------------------------------------------------------
# The engines that cannot do this
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ffmpeg", "vapoursynth"])
def test_other_engines_refuse_render_settings_rather_than_ignore_them(name):
    """Ignoring them would render every column identically and look like a result."""
    if name == "vapoursynth":
        pytest.importorskip("vapoursynth")
    engine = engines.get_engine(name)

    with pytest.raises(EngineError, match="settings comparison"):
        engine.prepare(
            Source(path=Path("nope.mkv"), name="x"),
            render=RenderSettings(name="spline", options={"tone-mapping": "spline"}),
        )


# --------------------------------------------------------------------------
# The mpv engine's refusals
# --------------------------------------------------------------------------


def _mpv_engine():
    from kiyas.engines.mpv import MpvEngine

    return MpvEngine()


def test_mpv_engine_refuses_a_resize_and_points_at_the_right_knob(tmp_path):
    from kiyas.engines import mpv as mpv_module

    source = Source(path=tmp_path / "a.mkv", name="x", resize=(1280, 720))

    with pytest.raises(EngineError, match=r"\[settings\]"):
        mpv_module._refuse_unsupported(source)  # noqa: SLF001


def test_mpv_engine_refuses_a_tonemap_setting(tmp_path):
    """The renderer decides; varying that decision is what settings mode is for."""
    from kiyas.config import Tonemap
    from kiyas.engines import mpv as mpv_module

    source = Source(path=tmp_path / "a.mkv", name="x", tonemap=Tonemap.HDR10)

    with pytest.raises(EngineError, match="template = 'tonemap'"):
        mpv_module._refuse_unsupported(source)  # noqa: SLF001


def test_mpv_engine_refuses_luma_fix(tmp_path):
    from kiyas.engines import mpv as mpv_module

    source = Source(path=tmp_path / "a.mkv", name="x", luma_fix=True)

    with pytest.raises(EngineError, match="VapourSynth"):
        mpv_module._refuse_unsupported(source)  # noqa: SLF001


def test_pipe_tags_survive_a_name_full_of_punctuation():
    from kiyas.engines.mpv import _tag

    assert _tag("ArtCNN C4F32 (DS)") == "ArtCNN-C4F32--DS"
    assert _tag("///") == "session"
    assert len(_tag("x" * 200)) <= 40


# --------------------------------------------------------------------------
# Integration: the real thing
# --------------------------------------------------------------------------

#: Frame N of the ramp clip is a flat limited-range grey of 16 + 2N, which is
#: 2.329*N once converted to full range. A capture therefore says which frame
#: it is, to within rounding.
_GREY_PER_FRAME = 2 * 255 / 219


def _build_ramp(ffmpeg: Path, path: Path, size: str) -> Path:
    subprocess.run(  # noqa: S603
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s={size}:r=24:d=5",
            "-vf", "geq=lum='16+2*N':cb=128:cr=128,"
                   "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
                   "format=yuv420p",
            "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", "12", str(path),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=600,
    )  # fmt: skip
    return path


@pytest.fixture(scope="module")
def ramp(tmp_path_factory) -> Path:
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    directory = tmp_path_factory.mktemp("ramp-media")
    return _build_ramp(binaries.require_binary("ffmpeg"), directory / "ramp.mkv", "640x360")


@pytest.fixture(scope="module")
def ramp4k(tmp_path_factory) -> Path:
    """The same clip at 3840x2160.

    Resolution is the variable that decides whether the renderer falls behind
    the player: the small clip missed 4 captures in 48 without a barrier, this
    one missed 8 in 8. A guard that only ever runs against small frames is not
    guarding the case that broke.
    """
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    directory = tmp_path_factory.mktemp("ramp-media-4k")
    return _build_ramp(binaries.require_binary("ffmpeg"), directory / "ramp4k.mkv", "3840x2160")


def _mean_grey(ffmpeg: Path, path: Path) -> float:
    proc = subprocess.run(  # noqa: S603
        [str(ffmpeg), "-v", "error", "-i", str(path), "-vf", "format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return sum(proc.stdout) / len(proc.stdout)


@pytest.mark.mpv
def test_every_variant_captures_the_frame_it_was_asked_for(ramp, tmp_path):
    """The regression test for mpv's wrong-frame window screenshot.

    Before the render barrier in MpvSession, roughly one capture in twelve came
    back either black or showing the *previously* captured frame. On real
    material that reads as a difference between the two columns, which is
    exactly the conclusion a comparison is used to draw.
    """
    if not _mpv_engine().available():
        pytest.skip("mpv, ffmpeg or ffprobe is not available")
    ffmpeg = binaries.require_binary("ffmpeg")

    project = config.load(
        write(
            tmp_path,
            f"""
            title = "ramp"
            mode = "settings"

            [[source]]
            path = {str(ramp).replace("\\", "/")!r}
            name = "ramp"

            [frames]
            method = "manual"
            manual = [10, 30, 50, 70, 90]

            [settings]
            template = "scalers"
            width = 640

            [output]
            directory = "out"
            """,
        )
    )

    result = run.run(project, overlay=False)

    assert result.engine == "mpv"
    assert len(result.sources) == len(project.settings.variants)
    for column in result.sources:
        for image in column.files:
            frame = int(image.stem)
            expected = min(255.0, frame * _GREY_PER_FRAME)
            measured = _mean_grey(ffmpeg, image)
            assert measured == pytest.approx(expected, abs=6), (
                f"{column.name} frame {frame} captured something else "
                f"(mean {measured:.1f}, expected {expected:.1f})"
            )


@pytest.mark.mpv
@pytest.mark.parametrize("overlay", [False, True])
def test_4k_captures_are_not_one_frame_behind(ramp4k, tmp_path, overlay):
    """The case that actually broke, in both of the ways it broke.

    On 3840x2160 the renderer trails the player far enough that *every* capture
    came back as the previous frame -- 8 out of 8, silently. Waiting on
    ``video-frame-info`` did not help; it is player-side and had already moved.
    Only ``vo-passes``, which the video output writes after drawing, does.

    The overlay case is a separate bug with the same symptom, and the reason
    this test is parametrised: the caption makes mpv re-render, so setting it
    before seeking moved the barrier's marker with a picture of the *old*
    frame. Running the guard only without a caption -- which is the habit,
    because captions ruin pixel comparisons -- left that half untested while it
    was broken on real material.
    """
    if not _mpv_engine().available():
        pytest.skip("mpv, ffmpeg or ffprobe is not available")
    ffmpeg = binaries.require_binary("ffmpeg")
    engine = _mpv_engine()
    wanted = [10, 40, 70, 100, 20, 50]

    prepared = engine.prepare(Source(path=ramp4k, name="4k"), overlay=overlay)
    try:
        written = prepared.write_frames(wanted, tmp_path / "shots")
    finally:
        prepared.close()

    for image in written:
        frame = int(image.stem)
        measured = _mean_grey(ffmpeg, image) / _GREY_PER_FRAME
        assert measured == pytest.approx(frame, abs=3), (
            f"asked for frame {frame}, captured something that reads as frame {measured:.1f}"
        )


@pytest.mark.mpv
def test_the_capture_size_is_reported_back(ramp, tmp_path):
    """A comparison produced at an unexpected resolution has to say so."""
    if not _mpv_engine().available():
        pytest.skip("mpv, ffmpeg or ffprobe is not available")

    project = config.load(
        write(
            tmp_path,
            f"""
            mode = "settings"

            [[source]]
            path = {str(ramp).replace("\\", "/")!r}
            name = "ramp"

            [frames]
            method = "manual"
            manual = [20]

            [settings]
            width = 480

            [[variant]]
            name = "a"
            options = {{ scale = "bilinear" }}

            [[variant]]
            name = "b"
            options = {{ scale = "lanczos" }}

            [output]
            directory = "out"
            """,
        )
    )

    result = run.run(project, overlay=False)

    assert any("480x270" in note for note in result.warnings)


@pytest.mark.mpv
def test_a_source_comparison_can_fall_back_to_mpv(ramp, tmp_path):
    """Mode A through the player: same frame numbers, different route to them."""
    if not _mpv_engine().available():
        pytest.skip("mpv, ffmpeg or ffprobe is not available")
    ffmpeg = binaries.require_binary("ffmpeg")

    project = config.load(
        write(
            tmp_path,
            f"""
            engine = "mpv"

            [[source]]
            path = {str(ramp).replace("\\", "/")!r}
            name = "first"

            [[source]]
            path = {str(ramp).replace("\\", "/")!r}
            name = "second"

            [frames]
            method = "manual"
            manual = [25, 65]

            [output]
            directory = "out"
            """,
        )
    )

    result = run.run(project, overlay=False)

    assert result.engine == "mpv"
    for column in result.sources:
        for image in column.files:
            frame = int(image.stem)
            assert _mean_grey(ffmpeg, image) == pytest.approx(frame * _GREY_PER_FRAME, abs=6)


def _grey_pixels(ffmpeg: Path, path: Path) -> bytes:
    proc = subprocess.run(  # noqa: S603
        [str(ffmpeg), "-v", "error", "-i", str(path), "-vf", "format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return proc.stdout


@pytest.mark.mpv
def test_the_label_is_burnt_in_when_asked_for(ramp, tmp_path):
    """mpv can caption a capture, unlike ffmpeg. Check it actually shows.

    Counted pixel by pixel rather than by comparing means. A caption covers a
    small part of the frame -- on this 640x360 clip it moved the mean by 0.04 --
    so a mean-based check would need a threshold small enough to be indistinct
    from encoder noise. How many pixels changed is unambiguous.
    """
    if not _mpv_engine().available():
        pytest.skip("mpv, ffmpeg or ffprobe is not available")
    ffmpeg = binaries.require_binary("ffmpeg")
    engine = _mpv_engine()
    source = Source(path=ramp, name="labelled")

    plain = engine.prepare(source, overlay=False)
    try:
        bare = _grey_pixels(ffmpeg, plain.write_frames([50], tmp_path / "bare")[0])
        assert not plain.has_overlay
    finally:
        plain.close()

    captioned = engine.prepare(source, overlay=True)
    try:
        assert captioned.has_overlay
        marked = _grey_pixels(ffmpeg, captioned.write_frames([50], tmp_path / "marked")[0])
    finally:
        captioned.close()

    assert len(bare) == len(marked)
    changed = sum(1 for a, b in zip(bare, marked, strict=True) if abs(a - b) > 40)
    assert changed > 100, f"only {changed} pixels changed; the caption did not reach the picture"
