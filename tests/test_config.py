from __future__ import annotations

import textwrap

import pytest

from kiyas import config
from kiyas.config import ConfigError, Engine, FrameMethod, Mode, Tonemap

MINIMAL = """
title = "Example"

[[source]]
path = "a.mkv"
name = "A"

[[source]]
path = "b.mkv"
name = "B"
"""


def write(tmp_path, text: str, name: str = "project.toml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_minimal_project_parses(tmp_path):
    project = config.load(write(tmp_path, MINIMAL))

    assert project.mode is Mode.SOURCE
    assert project.title == "Example"
    assert project.source_names == ["A", "B"]
    assert project.engine is Engine.AUTO


def test_defaults_are_sensible(tmp_path):
    project = config.load(write(tmp_path, MINIMAL))

    assert project.frames.method is FrameMethod.COUNT
    assert project.frames.count == 12
    assert project.frames.b_frames_only is True
    assert project.frames.skip_dark is True
    assert project.sources[0].tonemap is Tonemap.AUTO


def test_relative_paths_resolve_against_the_project_file(tmp_path):
    """A project file must be runnable from any working directory."""
    media = tmp_path / "media"
    media.mkdir()
    path = write(
        tmp_path,
        """
        [[source]]
        path = "media/a.mkv"
        name = "A"

        [[source]]
        path = "media/b.mkv"
        name = "B"

        [output]
        directory = "screens"
        """,
    )

    project = config.load(path)

    assert project.sources[0].path == (media / "a.mkv").resolve()
    assert project.output == (tmp_path / "screens").resolve()


def test_absolute_paths_are_left_alone(tmp_path):
    absolute = (tmp_path / "elsewhere" / "a.mkv").resolve()
    path = write(
        tmp_path,
        f"""
        [[source]]
        path = "{absolute.as_posix()}"
        name = "A"

        [[source]]
        path = "b.mkv"
        name = "B"
        """,
    )

    project = config.load(path)

    assert project.sources[0].path == absolute


# --------------------------------------------------------------------------
# Rejections. The error text is the user interface, so the tests assert on it.
# --------------------------------------------------------------------------


def test_source_comparison_needs_two_sources(tmp_path):
    path = write(
        tmp_path,
        """
        [[source]]
        path = "a.mkv"
        name = "A"
        """,
    )

    with pytest.raises(ConfigError, match="at least two"):
        config.load(path)


def test_settings_mode_allows_a_single_source(tmp_path):
    """One file is the *point* of a settings comparison, not a shortfall."""
    path = write(
        tmp_path,
        """
        mode = "settings"

        [[source]]
        path = "a.mkv"
        name = "A"

        [settings]
        template = "tonemap"
        """,
    )

    project = config.load(path)
    assert project.mode is Mode.SETTINGS
    assert len(project.sources) == 1
    assert len(project.settings.variants) > 1


def test_no_sources_at_all_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="at least one"):
        config.load(write(tmp_path, 'title = "nothing"'))


def test_duplicate_source_names_are_rejected(tmp_path):
    """Names become the comparison's column labels; duplicates make it unreadable."""
    path = write(
        tmp_path,
        """
        [[source]]
        path = "a.mkv"
        name = "REMUX"

        [[source]]
        path = "b.mkv"
        name = "REMUX"
        """,
    )

    with pytest.raises(ConfigError, match="unique"):
        config.load(path)


def test_missing_name_is_rejected_with_a_reason(tmp_path):
    path = write(
        tmp_path,
        """
        [[source]]
        path = "a.mkv"

        [[source]]
        path = "b.mkv"
        name = "B"
        """,
    )

    with pytest.raises(ConfigError, match="cannot be guessed"):
        config.load(path)


def test_typo_in_a_key_is_rejected_not_ignored(tmp_path):
    """Silently ignoring b_frame_only would leave the user trusting a rule that is off."""
    path = write(
        tmp_path,
        MINIMAL
        + """
        [frames]
        b_frame_only = true
        """,
    )

    with pytest.raises(ConfigError, match="unknown key"):
        config.load(path)


def test_unknown_key_lists_the_known_ones(tmp_path):
    path = write(tmp_path, MINIMAL + "\n[frames]\nnope = 1\n")

    with pytest.raises(ConfigError, match="b_frames_only"):
        config.load(path)


def test_crop_needs_four_values(tmp_path):
    path = write(
        tmp_path,
        """
        [[source]]
        path = "a.mkv"
        name = "A"
        crop = [10, 10]

        [[source]]
        path = "b.mkv"
        name = "B"
        """,
    )

    with pytest.raises(ConfigError, match=r"left, right, top, bottom"):
        config.load(path)


def test_bad_tonemap_value_lists_the_alternatives(tmp_path):
    path = write(
        tmp_path,
        """
        [[source]]
        path = "a.mkv"
        name = "A"
        tonemap = "hdr"

        [[source]]
        path = "b.mkv"
        name = "B"
        """,
    )

    with pytest.raises(ConfigError, match="hdr10plus"):
        config.load(path)


def test_manual_method_without_frames_is_rejected(tmp_path):
    path = write(tmp_path, MINIMAL + "\n[frames]\nmethod = 'manual'\n")

    with pytest.raises(ConfigError, match="manual"):
        config.load(path)


def test_invalid_toml_names_the_file(tmp_path):
    path = write(tmp_path, "this is not toml [[[")

    with pytest.raises(ConfigError, match="invalid TOML"):
        config.load(path)


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        config.load(tmp_path / "nope.toml")


# --------------------------------------------------------------------------
# skip_start / skip_end accept three spellings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [("0.05", 0.05), ('"5%"', 0.05), ("5", 0.05), ("0", 0.0), ('"12.5%"', 0.125)],
)
def test_fraction_spellings(tmp_path, written, expected):
    path = write(tmp_path, MINIMAL + f"\n[frames]\nskip_start = {written}\n")

    assert config.load(path).frames.skip_start == pytest.approx(expected)


def test_bare_number_above_one_is_read_as_a_percentage(tmp_path):
    """'skip_start = 5' obviously means 5%, and reading it as 500% would
    silently produce an empty comparison."""
    path = write(tmp_path, MINIMAL + "\n[frames]\nskip_start = 5\n")

    assert config.load(path).frames.skip_start == pytest.approx(0.05)


def test_skipping_half_the_runtime_is_rejected(tmp_path):
    path = write(tmp_path, MINIMAL + '\n[frames]\nskip_start = "60%"\n')

    with pytest.raises(ConfigError, match="nothing to compare"):
        config.load(path)


def test_manual_frames_are_sorted_and_deduplicated(tmp_path):
    path = write(
        tmp_path,
        MINIMAL + "\n[frames]\nmethod = 'manual'\nmanual = [900, 100, 500, 100]\n",
    )

    assert config.load(path).frames.manual == (100, 500, 900)


def test_tools_paths_are_kept_verbatim(tmp_path):
    path = write(
        tmp_path,
        MINIMAL + '\n[tools]\nmpv = "C:/Program Files/mpv/mpv.exe"\n',
    )

    assert config.load(path).tools["mpv"] == "C:/Program Files/mpv/mpv.exe"


def test_unknown_tool_is_rejected(tmp_path):
    path = write(tmp_path, MINIMAL + '\n[tools]\nvlc = "vlc.exe"\n')

    with pytest.raises(ConfigError, match="unknown key"):
        config.load(path)


def test_processing_order_is_documented_and_complete():
    """Every field that transforms a clip must appear in PROCESSING_ORDER.

    A new transformation added to Source without a place in the order would be
    applied wherever the engine happened to put it, and the difference only
    shows up as a subtly wrong screenshot.
    """
    transforming = {"trim", "crop", "resize", "tonemap", "luma_fix", "normalize_fps"}

    assert set(config.PROCESSING_ORDER) == transforming


# --------------------------------------------------------------------------
# Writing a project back out
# --------------------------------------------------------------------------

FULL = r"""
title = "Round trip"
engine = "ffmpeg"

[frames]
method = "count"
count = 7
skip_start = "5%"
skip_end = "12.5%"
b_frames_only = false
skip_dark = true
seed = 3

[[source]]
path = 'C:\media\a.mkv'
name = "A"
trim = 24
crop = [0, 0, 140, 140]
resize = [1920, 1080]
tonemap = "hdr10"
luma_fix = true
normalize_fps = false

[[source]]
path = 'C:\media\b.mkv'
name = "B"

[output]
directory = "out"
index_dir = "idx"

[tools]
mpv = 'C:\Program Files\mpv\mpv.exe'
"""


def _round_trip(tmp_path, text: str):
    """Load, write back out, load again."""
    first = config.load(write(tmp_path, text))
    rewritten = write(tmp_path, config.dumps(first), name="again.toml")
    return first, config.load(rewritten)


def test_a_project_survives_being_written_and_read_back(tmp_path):
    """The GUI writes project files through dumps(), so this is what stops it
    from quietly losing a setting somebody typed."""
    first, second = _round_trip(tmp_path, FULL)

    assert second.title == first.title
    assert second.mode is first.mode
    assert second.engine is first.engine
    assert second.frames == first.frames
    assert second.output == first.output
    assert second.index_dir == first.index_dir
    assert second.tools == first.tools


def test_every_per_source_setting_survives(tmp_path):
    first, second = _round_trip(tmp_path, FULL)

    for before, after in zip(first.sources, second.sources, strict=True):
        assert after == before, f"{before.name} changed"


def test_windows_paths_are_written_without_being_mangled(tmp_path):
    """A basic TOML string would need every backslash doubled.

    Asserted on what is written rather than on what comes back from `load`,
    because ``C:\\media\\a.mkv`` is not an absolute path on Linux -- `load`
    resolves it against the project file there, correctly, and the test would
    be measuring that instead. Found by CI, on a machine where the difference
    is not invisible.
    """
    import tomllib

    project = config.load(write(tmp_path, FULL))

    text = config.dumps(project)

    assert r"'C:\Program Files\mpv\mpv.exe'" in text, "written literally, not escaped"
    assert "\\\\" not in text, "no doubled backslashes"
    assert tomllib.loads(text)["tools"]["mpv"] == r"C:\Program Files\mpv\mpv.exe"


def test_a_settings_project_survives(tmp_path):
    text = """
    title = "Curves"
    mode = "settings"

    [[source]]
    path = "a.mkv"
    name = "the file"

    [settings]
    template = "tonemap"
    width = 1280
    fullscreen = false
    """
    first, second = _round_trip(tmp_path, text)

    assert second.mode is Mode.SETTINGS
    assert second.settings.width == 1280
    assert [v.name for v in second.settings.variants] == [v.name for v in first.settings.variants]
    assert [v.options for v in second.settings.variants] == [
        v.options for v in first.settings.variants
    ]


def test_variants_are_written_out_rather_than_left_as_a_template(tmp_path):
    """A template is a shorthand whose meaning can change between versions.

    The written file has to record what was rendered, not the name of a set
    that might expand differently next year.
    """
    project = config.load(
        write(
            tmp_path,
            """
            mode = "settings"
            [[source]]
            path = "a.mkv"
            name = "f"
            [settings]
            template = "tonemap"
            """,
        )
    )

    text = config.dumps(project)

    assert "template" not in text
    assert text.count("[[variant]]") == len(project.settings.variants)


def test_manual_frames_survive(tmp_path):
    text = """
    [[source]]
    path = "a.mkv"
    name = "A"
    [[source]]
    path = "b.mkv"
    name = "B"
    [frames]
    method = "manual"
    manual = [10, 500, 12345]
    """
    _, second = _round_trip(tmp_path, text)

    assert second.frames.method is FrameMethod.MANUAL
    assert second.frames.manual == (10, 500, 12345)


def test_a_name_with_a_quote_in_it_survives(tmp_path):
    """A literal TOML string cannot hold one, so it has to fall back."""
    text = """
    [[source]]
    path = "a.mkv"
    name = "Marty's cut"
    [[source]]
    path = "b.mkv"
    name = "B"
    """
    _, second = _round_trip(tmp_path, text)

    assert second.sources[0].name == "Marty's cut"


def test_what_is_written_is_readable_toml(tmp_path):
    import tomllib

    project = config.load(write(tmp_path, FULL))

    tomllib.loads(config.dumps(project))
