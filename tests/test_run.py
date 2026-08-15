from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from kiyas import config, engines, run
from kiyas.config import Engine, Project
from kiyas.media import binaries
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
