from __future__ import annotations

import json
import subprocess
import textwrap
from fractions import Fraction
from pathlib import Path

import pytest

from kiyas import config, engines, run
from kiyas.config import Engine, Project
from kiyas.media import binaries, rpu
from kiyas.run import RunError, safe_directory_name

# --------------------------------------------------------------------------
# Directory naming
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("REMUX", "REMUX"),
        ("Lionsgate GBR/USA", "Lionsgate GBR_USA"),
        ("REMUX (DV: FEL)", "REMUX (DV_ FEL)"),
        ("a<b>c|d?e*f", "a_b_c_d_e_f"),
        ('quote"name', "quote_name"),
        ("trailing.", "trailing"),
        ("  padded  ", "padded"),
    ],
)
def test_illegal_characters_are_replaced(name, expected):
    """Source names are free text and routinely contain path characters."""
    assert safe_directory_name(name) == expected


@pytest.mark.parametrize("reserved", ["CON", "PRN", "NUL", "COM1", "LPT9", "con"])
def test_windows_reserved_names_are_escaped(reserved):
    """Windows refuses these regardless of extension."""
    result = safe_directory_name(reserved)

    assert result.upper() not in run._RESERVED


def test_name_that_sanitises_to_nothing_still_produces_a_directory():
    """Whatever the name, a usable directory has to come out the other side."""
    assert safe_directory_name("") == "source"
    assert safe_directory_name("   ") == "source"
    # Illegal characters become underscores, so this is a real name, not empty.
    assert safe_directory_name("///") == "___"


def test_very_long_names_are_truncated():
    assert len(safe_directory_name("x" * 500)) <= 100


def test_distinct_names_can_collide_after_sanitising():
    """Documented limitation rather than a silent one.

    'A/B' and 'A_B' both become 'A_B'. Config rejects duplicate source names,
    but not names that only collide once sanitised, so the second capture
    would overwrite the first. Worth knowing before it happens in the field.
    """
    assert safe_directory_name("A/B") == safe_directory_name("A_B")


# --------------------------------------------------------------------------
# Engine choice
# --------------------------------------------------------------------------


def _project(tmp_path, **overrides) -> Project:
    project = config.parse(
        {
            "title": "T",
            "source": [
                {"path": "a.mkv", "name": "A"},
                {"path": "b.mkv", "name": "B"},
            ],
        }
    )
    project.output = tmp_path / "out"
    for key, value in overrides.items():
        setattr(project, key, value)
    return project


def test_auto_picks_the_best_available(tmp_path):
    project = _project(tmp_path, engine=Engine.AUTO)

    assert run.choose_engine(project) == engines.available_engines()[0]


def test_explicit_unavailable_engine_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "available_engines", lambda tools=None: ["ffmpeg"])
    project = _project(tmp_path, engine=Engine.VAPOURSYNTH)

    with pytest.raises(RunError, match="not available here"):
        run.choose_engine(project)


def test_no_engine_at_all_points_at_doctor(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "available_engines", lambda tools=None: [])
    project = _project(tmp_path)

    with pytest.raises(RunError, match="kiyas doctor"):
        run.choose_engine(project)


def test_missing_source_files_are_listed_before_any_work(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(RunError, match="do not exist"):
        run.run(project)


# --------------------------------------------------------------------------
# scaffold
# --------------------------------------------------------------------------


def test_scaffold_writes_a_loadable_skeleton(tmp_path):
    path = run.scaffold(tmp_path / "kiyas.toml", "My comparison")
    text = path.read_text(encoding="utf-8")

    assert "My comparison" in text
    # It must parse; only the paths are placeholders.
    data = config.parse(__import__("tomllib").loads(text))
    assert data.title == "My comparison"
    assert len(data.sources) == 2


def test_scaffold_refuses_to_overwrite(tmp_path):
    path = tmp_path / "kiyas.toml"
    path.write_text("precious", encoding="utf-8")

    with pytest.raises(RunError, match="not overwriting"):
        run.scaffold(path)

    assert path.read_text(encoding="utf-8") == "precious"


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def two_clips(tmp_path_factory):
    """Two clips of the same content, one visibly different."""
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    ffmpeg = binaries.require_binary("ffmpeg")
    directory = tmp_path_factory.mktemp("run-media")

    made = {}
    for name, extra in (("a", []), ("b", ["-vf", "eq=brightness=0.06"])):
        path = directory / f"{name}.mkv"
        subprocess.run(
            [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=6",
                *extra,
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "12", "-bf", "3",
                "-pix_fmt", "yuv420p", str(path),
            ],  # fmt: skip
            check=True, capture_output=True, timeout=300,
        )  # fmt: skip
        made[name] = path
    return made


def _write_project(tmp_path, clips, body: str = "") -> Path:
    path = tmp_path / "project.toml"
    path.write_text(
        textwrap.dedent(f"""
        title = "End to end"

        [frames]
        method = "count"
        count = 3
        skip_start = "10%"
        skip_end = "10%"
        b_frames_only = false
        skip_dark = false
        {body}

        [[source]]
        path = "{clips["a"].as_posix()}"
        name = "First"

        [[source]]
        path = "{clips["b"].as_posix()}"
        name = "Second"

        [output]
        directory = "{(tmp_path / "out").as_posix()}"
        """),
        encoding="utf-8",
    )
    return path


@pytest.mark.integration
def test_end_to_end_produces_one_image_per_source_per_frame(tmp_path, two_clips):
    project = config.load(_write_project(tmp_path, two_clips))

    result = run.run(project)

    assert len(result.frames) == 3
    assert len(result.sources) == 2
    assert result.image_count == 6
    for source in result.sources:
        assert len(source.files) == 3
        assert all(f.stat().st_size > 0 for f in source.files)


@pytest.mark.integration
def test_every_source_captures_the_same_frame_numbers(tmp_path, two_clips):
    """The comparison falls apart if the sources are captured at different frames."""
    project = config.load(_write_project(tmp_path, two_clips))

    result = run.run(project)

    names = {tuple(f.name for f in source.files) for source in result.sources}
    assert len(names) == 1


@pytest.mark.integration
def test_manifest_records_what_was_produced(tmp_path, two_clips):
    project = config.load(_write_project(tmp_path, two_clips))

    result = run.run(project)
    payload = json.loads(result.manifest.read_text(encoding="utf-8"))

    assert payload["title"] == "End to end"
    assert payload["frames"] == result.frames
    assert [s["name"] for s in payload["sources"]] == ["First", "Second"]
    assert payload["engine"] == result.engine
    for entry, source in zip(payload["sources"], result.sources):
        assert entry["files"] == [f.name for f in source.files]


@pytest.mark.integration
def test_b_frame_rule_moves_the_selection(tmp_path, two_clips):
    """With b_frames_only on, every captured frame must be a B-frame everywhere."""
    project = config.load(_write_project(tmp_path, two_clips, body=""))
    project.frames.b_frames_only = True

    engine = engines.get_engine(run.choose_engine(project))
    result = run.run(project)

    prepared = [engine.prepare(s, overlay=False) for s in project.sources]
    try:
        if not all(p.supports_frame_types for p in prepared):
            pytest.skip("engine cannot report picture types")
        for frame in result.frames:
            assert all(p.picture_type(frame) == "B" for p in prepared), (
                f"frame {frame} is not a B-frame"
            )
    finally:
        for p in prepared:
            p.close()


@pytest.mark.integration
def test_warning_when_source_lengths_differ_a_lot(tmp_path, two_clips):
    """A trim that leaves the sources very different lengths is nearly always wrong.

    The threshold is relative: 100 frames off a 144-frame clip is a serious
    mismatch, while the same 100 frames between two cuts of a feature is
    nothing. An absolute threshold cannot serve both.
    """
    project = config.load(_write_project(tmp_path, two_clips))
    project.sources[1].trim = 100

    result = run.run(project)

    assert any("differ" in w for w in result.warnings), result.warnings


@pytest.mark.integration
def test_no_length_warning_when_sources_match(tmp_path, two_clips):
    project = config.load(_write_project(tmp_path, two_clips))

    result = run.run(project)

    assert not any("differ" in w for w in result.warnings), result.warnings


@pytest.mark.integration
def test_source_directories_are_named_after_the_sources(tmp_path, two_clips):
    project = config.load(_write_project(tmp_path, two_clips))

    result = run.run(project)

    assert {s.directory.name for s in result.sources} == {"First", "Second"}


@pytest.mark.integration
def test_cli_run_reports_success(tmp_path, two_clips, capsys):
    from kiyas import cli

    path = _write_project(tmp_path, two_clips)

    assert cli.main(["run", str(path)]) == 0
    assert "images" in capsys.readouterr().out


@pytest.mark.integration
def test_cli_run_reports_a_bad_project_file(tmp_path, capsys):
    from kiyas import cli

    bad = tmp_path / "bad.toml"
    bad.write_text('title = "x"\n', encoding="utf-8")

    assert cli.main(["run", str(bad)]) == 2
    assert "source" in capsys.readouterr().out.lower()


def test_cli_init_then_run_is_a_complete_loop(tmp_path, capsys):
    from kiyas import cli

    path = tmp_path / "new.toml"

    assert cli.main(["init", str(path), "--title", "Loop"]) == 0
    assert path.is_file()

    # The scaffold has placeholder paths, so running it must fail cleanly
    # rather than crash.
    assert cli.main(["run", str(path)]) == 1
    assert "do not exist" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Comb detection wiring
#
# Whether VFM actually finds combs is not testable here: it works on
# photographic content and reads the hard horizontal edges of ffmpeg's
# synthetic sources as combing -- measured, 20 progressive frames out of 20
# flagged on `testsrc2` and `mandelbrot`, against none on a real film clip and
# its interlaced copy. So the detector is checked by hand against real
# material, the way frame accuracy and tonemapping are, and what is pinned
# down here is everything around it.
# --------------------------------------------------------------------------


class _Prepared:
    """A prepared source that answers only what the predicate asks."""

    def __init__(self, name="A", *, combed=None, frame_count=1000, width=1920, height=1080):
        self.name = name
        self.frame_count = frame_count
        self.width = width
        self.height = height
        # Part of the protocol, and the size warning divides by it to work out
        # where along the film to read metadata.
        self.fps = Fraction(24000, 1001)
        self._combed = combed

    supports_frame_types = True
    has_b_frames = True

    def picture_type(self, frame):
        return "B"

    def mean_luma(self, frame):
        return 0.5

    def combed(self, frame):
        return self._combed


def _frames_project(**frames):
    text = "\n".join(f"{key} = {value}" for key, value in frames.items())
    data = f"""
        [frames]
        {text}

        [[source]]
        path = "a.mkv"
        name = "A"

        [[source]]
        path = "b.mkv"
        name = "B"
    """
    return config.parse(__import__("tomllib").loads(textwrap.dedent(data)))


def test_a_combed_frame_is_rejected_when_asked_for():
    project = _frames_project(skip_combed="true")
    warnings: list[str] = []

    acceptable = run._acceptability([_Prepared(combed=True)], project, warnings)

    assert acceptable is not None
    assert acceptable(100) is False
    assert warnings == []


def test_a_clean_frame_passes():
    project = _frames_project(skip_combed="true")

    acceptable = run._acceptability([_Prepared(combed=False)], project, [])

    assert acceptable(100) is True


def test_an_engine_that_cannot_tell_turns_the_rule_off_and_says_so():
    """`None` means "cannot answer", not "clean".

    Reporting every frame as clean would be the silent version of this: the
    rule would look active and reject nothing, which is worse than saying it
    is off.
    """
    project = _frames_project(skip_combed="true")
    warnings: list[str] = []

    acceptable = run._acceptability([_Prepared(combed=None)], project, warnings)

    assert len(warnings) == 1
    assert "comb detection is off" in warnings[0]
    assert acceptable is None or acceptable(100) is True


def test_combing_is_not_looked_at_unless_it_is_asked_for():
    """It costs a full-resolution 8-bit conversion per frame."""
    looked: list[int] = []

    class _Counting(_Prepared):
        def combed(self, frame):
            looked.append(frame)
            return True

    acceptable = run._acceptability([_Counting()], _frames_project(skip_combed="false"), [])

    if acceptable is not None:
        acceptable(100)
    assert looked == []


# --------------------------------------------------------------------------
# Columns that are not the same size
# --------------------------------------------------------------------------


def _size_warnings(prepared, mode="source"):
    """The size complaint, if there is one, from a project in `mode`."""
    project = _frames_project()
    if mode == "settings":
        project.mode = config.Mode.SETTINGS
    warnings: list[str] = []
    run._warn_about_sizes(prepared, project, warnings)
    return warnings


def test_columns_of_different_sizes_are_reported():
    """Nothing else notices.

    Every image is written, the manifest is valid, the upload succeeds -- and
    the result is two pictures of different shapes, which cannot be flipped
    between. Measured on a real pair: a 3840x1606 WEB-DL against a 3840x2160
    remux, and the run had nothing to say about it.
    """
    warnings = _size_warnings(
        [_Prepared("WEB-DL", width=3840, height=1606), _Prepared("Remux", width=3840, height=2160)]
    )

    assert len(warnings) == 1
    assert "3840x1606" in warnings[0] and "3840x2160" in warnings[0]


def test_matching_columns_say_nothing():
    """A rule that fires when nothing is wrong is noise, and noise gets skimmed."""
    assert _size_warnings([_Prepared("A"), _Prepared("B")]) == []


def test_a_settings_comparison_is_exempt():
    """Every column there is the same file at the same size."""
    assert (
        _size_warnings(
            [_Prepared("bt.2390", height=1080), _Prepared("st2094-40", height=1080)],
            mode="settings",
        )
        == []
    )


def test_the_dolby_vision_answer_is_pointed_at_rather_than_guessed():
    """The active picture is in the RPU's level 5 offsets.

    Splitting the difference looks right and is not: on a real remux the
    arithmetic gives 277/277 and the RPU says 276, with the last row coming
    from the other source's conformance window. A suggestion that is one row
    out is worse than no suggestion, because it looks like an answer.
    """
    warnings = _size_warnings([_Prepared("A", height=1606), _Prepared("B", height=2160)])

    assert "level 5" in warnings[0]
    assert "277" not in warnings[0]


# --------------------------------------------------------------------------
# What the size warning reads out of the Dolby Vision metadata
#
# The offsets are never guessed from the picture: cropdetect finds no bars on a
# PQ source, and splitting the difference arithmetically came out 277 where the
# file's own metadata says 276. So the numbers come from the RPU, and these
# tests are about what the sentence does with them.
# --------------------------------------------------------------------------


class _Dolby:
    """A probe result for a source that carries Dolby Vision."""

    def __init__(self, profile=7):
        self.dovi_profile = profile


def _advised(prepared, reading, *, profile=7, on="b.mkv", project=None):
    """The size complaint, with only the source at `on` carrying an active area.

    Answering the same offsets for every source is the unrealistic case: the
    release that was already cropped has no level 5 at all. Giving both the
    same reading produced a second, nonsensical suggestion, which is worth
    keeping out of the fixture rather than out of the assertions.
    """
    project = project or _frames_project()
    absent = rpu.Reading(shapes=(), positions=reading.positions, carrying=0)
    warnings: list[str] = []
    run._warn_about_sizes(
        prepared,
        project,
        warnings,
        inspect=lambda *args, **kwargs: _Dolby(profile),
        read=lambda path, **kwargs: reading if Path(path).name == on else absent,
    )
    return warnings[0] if warnings else ""


def _fixed(top, bottom, positions=5):
    return rpu.Reading(
        shapes=(rpu.ActiveArea(0, 0, top, bottom),), positions=positions, carrying=positions
    )


def test_the_warning_prints_the_crop_the_metadata_asks_for():
    """The line has to be pasteable, in the order a project file writes crop.

    Reading it by hand means extracting an RPU and knowing which of dovi_tool's
    outputs to look at, which is exactly the work worth not repeating.
    """
    message = _advised(
        [_Prepared("WEB-DL", width=3840, height=1608), _Prepared("Remux", width=3840, height=2160)],
        _fixed(276, 276),
    )

    assert "crop = [0, 0, 276, 276]" in message
    assert "3840x1608" in message
    assert "matches the others" in message


def test_a_crop_that_still_does_not_match_says_so_and_by_how_much():
    """Measured on the pair this was built against.

    The disc's own RPU says 276 rows top and bottom, leaving 1608, while the
    iTunes WEB-DL of the same film is 1606. Neither number is wrong; the
    releases disagree. Printing the crop and stopping would leave the reader
    wondering why the warning fired again.
    """
    message = _advised(
        [_Prepared("WEB-DL", width=3840, height=1606), _Prepared("Remux", width=3840, height=2160)],
        _fixed(276, 276),
    )

    assert "crop = [0, 0, 276, 276]" in message
    assert "2 rows taller" in message
    assert "WEB-DL" in message.split("still")[1]


def test_a_film_that_changes_shape_gets_no_crop_suggested():
    """An IMAX sequence opens the frame out and closes it again.

    There is no single crop for that, and one read from inside either stretch
    looks perfectly constant -- which is why several are read.
    """
    changing = rpu.Reading(
        shapes=(rpu.ActiveArea(0, 0, 276, 276), rpu.ActiveArea(0, 0, 0, 0)),
        positions=5,
        carrying=5,
    )

    message = _advised(
        [_Prepared("WEB-DL", width=3840, height=1606), _Prepared("Remux", width=3840, height=2160)],
        changing,
    )

    assert "changes shape" in message
    assert "crop = [" not in message


def test_a_source_with_no_level_5_adds_nothing_to_the_warning():
    """A release cropped to its own picture has no bars to mask.

    There is no number to paste, so the sentence would be the only one in a
    long warning carrying no number.
    """
    message = _advised(
        [_Prepared("WEB-DL", width=3840, height=1606), _Prepared("Remux", width=3840, height=2160)],
        rpu.Reading(shapes=(), positions=5, carrying=0),
    )

    assert "not the same size" in message
    assert "crop = [" not in message


def test_a_source_that_is_not_dolby_vision_is_never_read():
    read: list[object] = []

    project = _frames_project()
    warnings: list[str] = []
    run._warn_about_sizes(
        [_Prepared("A", width=1920, height=1080), _Prepared("B", width=1920, height=800)],
        project,
        warnings,
        inspect=lambda *args, **kwargs: _Dolby(None),
        read=lambda *args, **kwargs: read.append(1) or _fixed(276, 276),
    )

    assert read == [], "an RPU was extracted from a file that has none"


def test_a_source_that_is_already_cropped_is_not_advised_about():
    """The offsets describe the frame as encoded.

    Against a source that has already been transformed they answer a different
    question, and the answer would look authoritative.
    """
    read: list[object] = []
    project = _frames_project()
    project.sources[0].crop = (0, 0, 276, 276)
    project.sources[1].crop = (0, 0, 100, 100)

    warnings: list[str] = []
    run._warn_about_sizes(
        [_Prepared("A", width=3840, height=1608), _Prepared("B", width=3840, height=1960)],
        project,
        warnings,
        inspect=lambda *args, **kwargs: _Dolby(7),
        read=lambda *args, **kwargs: read.append(1) or _fixed(276, 276),
    )

    assert read == []
    assert "not the same size" in warnings[0]


def test_the_warning_survives_a_machine_with_no_dovi_tool():
    """Enriching a message is not worth failing a run that wrote its images."""

    def _missing(*args, **kwargs):
        raise binaries.BinaryNotFound("dovi_tool was not found")

    project = _frames_project()
    warnings: list[str] = []
    run._warn_about_sizes(
        [_Prepared("A", width=3840, height=1606), _Prepared("B", width=3840, height=2160)],
        project,
        warnings,
        inspect=lambda *args, **kwargs: _Dolby(7),
        read=_missing,
    )

    assert len(warnings) == 1
    assert "not the same size" in warnings[0]


def test_an_unreadable_rpu_does_not_fail_the_run():
    def _broken(*args, **kwargs):
        raise rpu.ActiveAreaError("the RPU did not parse as JSON")

    project = _frames_project()
    warnings: list[str] = []
    run._warn_about_sizes(
        [_Prepared("A", width=3840, height=1606), _Prepared("B", width=3840, height=2160)],
        project,
        warnings,
        inspect=lambda *args, **kwargs: _Dolby(7),
        read=_broken,
    )

    assert len(warnings) == 1
    assert "crop = [" not in warnings[0]
