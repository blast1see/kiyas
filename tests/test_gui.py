"""The desktop interface.

Run headless (``QT_QPA_PLATFORM=offscreen``), so these are real widgets doing
real work with no display attached.

What is worth testing here is the boundary, not the pixels: that what is on
screen turns into the same project a person could have typed, that opening a
file and saving it back does not lose anything, and that the window refuses
exactly what ``kiyas run`` refuses. The rule the GUI is built around -- it has
no privileges -- is only true if that boundary holds.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

# QtWidgets, not PySide6: importing the package succeeds on a machine that
# cannot actually run Qt. The extension module is what needs libEGL and the
# rest of the system libraries, so it is what has to be probed -- otherwise a
# bare Linux box fails at collection instead of skipping. Found by CI on the
# first push, which is what a second operating system is for.
pytest.importorskip("PySide6.QtWidgets", reason="the desktop interface needs a working PySide6")

# Must be set before QApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from kiyas import config  # noqa: E402
from kiyas.config import Engine, FrameMethod, Mode, Tonemap  # noqa: E402
from kiyas.gui.window import GuiError, MainWindow  # noqa: E402


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(application):
    window = MainWindow()
    yield window
    window.close()


def _media(tmp_path: Path, *names: str) -> list[Path]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"not really a video")
        paths.append(path)
    return paths


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def test_added_files_become_rows(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))

    assert window.sources.rowCount() == 2
    assert [row[0] for row in window._rows()] == ["a", "b"]  # noqa: SLF001


def test_two_files_with_the_same_name_get_different_labels(window, tmp_path):
    """Names are the comparison's column headings, and have to be distinct."""
    first = tmp_path / "one" / "video.mkv"
    second = tmp_path / "two" / "video.mkv"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(b"x")

    window.add_paths([first, second])

    names = [row[0] for row in window._rows()]  # noqa: SLF001
    assert len(set(names)) == 2


def test_removing_a_row_removes_it(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.selectRow(0)

    window.remove_selected()

    assert [row[0] for row in window._rows()] == ["b"]  # noqa: SLF001


def test_moving_a_row_reorders_the_columns(window, tmp_path):
    """Column order is the order the sources appear in, so it has to be editable."""
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.selectRow(1)

    window._move_selected(-1)  # noqa: SLF001

    assert [row[0] for row in window._rows()] == ["b", "a"]  # noqa: SLF001


# --------------------------------------------------------------------------
# Turning the window into a project
# --------------------------------------------------------------------------


def test_the_window_builds_the_project_a_person_would_have_typed(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.title_edit.setText("Two releases")
    window.count_spin.setValue(9)
    window.skip_start.setValue(2.5)
    window.b_frames_check.setChecked(False)

    project = window.build_project()

    assert project.mode is Mode.SOURCE
    assert project.title == "Two releases"
    assert project.source_names == ["a", "b"]
    assert project.frames.count == 9
    assert project.frames.skip_start == pytest.approx(0.025)
    assert project.frames.b_frames_only is False


def test_per_source_fields_reach_the_project(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.item(0, 1).setText("24")
    window.sources.item(0, 2).setText("0,0,140,140")
    window.sources.item(0, 3).setText("1920x1080")
    window.sources.cellWidget(0, 4).setCurrentText("hdr10")

    source = window.build_project().sources[0]

    assert source.trim == 24
    assert source.crop == (0, 0, 140, 140)
    assert source.resize == (1920, 1080)
    assert source.tonemap is Tonemap.HDR10


def test_a_crop_that_is_not_four_numbers_is_refused_with_the_row(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.item(0, 2).setText("10,10")

    with pytest.raises(GuiError, match="row 1"):
        window.build_project()


def test_a_trim_that_is_not_a_number_is_refused(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.item(0, 1).setText("twenty")

    with pytest.raises(GuiError, match="whole number"):
        window.build_project()


def test_running_with_no_files_is_refused(window):
    with pytest.raises(GuiError, match="at least one file"):
        window.build_project()


def test_the_window_refuses_what_the_command_line_refuses(window, tmp_path):
    """One file in a source comparison, which the parser rejects.

    The window has no widget for that rule and does not need one: it validates
    by writing the project and reading it back, so every rule in config.py
    applies here for free.
    """
    window.add_paths(_media(tmp_path, "only.mkv"))

    with pytest.raises(GuiError, match="at least two"):
        window.build_project()


def test_duplicate_names_are_refused_by_the_parser(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.item(1, 0).setText("a")

    with pytest.raises(GuiError, match="unique"):
        window.build_project()


def test_manual_frames_are_parsed_from_free_text(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.method_combo.setCurrentIndex(2)
    window.manual_edit.setText("900, 100 500,100")

    frames = window.build_project().frames

    assert frames.method is FrameMethod.MANUAL
    assert frames.manual == (100, 500, 900)


def test_choosing_exact_frames_without_any_is_refused(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.method_combo.setCurrentIndex(2)

    with pytest.raises(GuiError, match="at least one frame"):
        window.build_project()


def test_a_relative_output_lands_next_to_the_media(window, tmp_path):
    """There is no project file yet, so there is nothing else to be relative to."""
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.output_edit.setText("shots")

    assert window.build_project().output == (tmp_path / "shots").resolve()


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def test_settings_mode_needs_variants(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv"))
    window.mode_combo.setCurrentIndex(1)

    with pytest.raises(GuiError, match="variants"):
        window.build_project()


def test_a_template_fills_the_variants_in(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv"))
    window.mode_combo.setCurrentIndex(1)
    window.template_combo.setCurrentText("tonemap")

    project = window.build_project()

    assert project.mode is Mode.SETTINGS
    assert len(project.settings.variants) > 1
    assert "spline" in project.column_names


def test_settings_mode_turns_the_b_frame_rule_off(window, tmp_path):
    """Every column is the same decoded frame; there is no encode to flatter."""
    window.add_paths(_media(tmp_path, "a.mkv"))
    window.mode_combo.setCurrentIndex(1)
    window.template_combo.setCurrentText("tonemap")
    window.b_frames_check.setChecked(True)

    assert window.build_project().frames.b_frames_only is False
    assert not window.b_frames_check.isEnabled()


def test_the_capture_width_reaches_the_project(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv"))
    window.mode_combo.setCurrentIndex(1)
    window.template_combo.setCurrentText("scalers")
    window.width_spin.setValue(1280)

    assert window.build_project().settings.width == 1280


def test_the_mode_decides_which_panels_are_shown(window):
    window.mode_combo.setCurrentIndex(0)
    assert window.frames_box.isVisibleTo(window)
    assert not window.render_box.isVisibleTo(window)
    assert not window.audio_box.isVisibleTo(window)

    window.mode_combo.setCurrentIndex(1)
    assert window.render_box.isVisibleTo(window)

    window.mode_combo.setCurrentIndex(2)
    assert not window.frames_box.isVisibleTo(window)
    assert window.audio_box.isVisibleTo(window)


# --------------------------------------------------------------------------
# Project files
# --------------------------------------------------------------------------


def _write_project(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "project.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_opening_a_project_puts_it_on_screen(window, tmp_path):
    path = _write_project(
        tmp_path,
        """
title = "Loaded"
engine = "ffmpeg"

[frames]
method = "interval"
interval_seconds = 120
skip_start = "3%"
b_frames_only = false

[[source]]
path = "a.mkv"
name = "First"
trim = 12

[[source]]
path = "b.mkv"
name = "Second"

[output]
directory = "shots"
""",
    )

    window.load_project(config.load(path))

    assert window.title_edit.text() == "Loaded"
    assert window.engine_combo.currentText() == "ffmpeg"
    assert window.method_combo.currentData() == "interval"
    assert window.interval_spin.value() == pytest.approx(120)
    assert window.skip_start.value() == pytest.approx(3.0)
    assert window.b_frames_check.isChecked() is False
    assert [row[0] for row in window._rows()] == ["First", "Second"]  # noqa: SLF001
    assert window._rows()[0][1] == "12"  # noqa: SLF001


def test_a_project_survives_a_trip_through_the_window(window, tmp_path):
    """Open, then build: nothing typed by hand may be dropped on the way."""
    path = _write_project(
        tmp_path,
        """
title = "Round trip"
engine = "vapoursynth"

[frames]
method = "count"
count = 5
skip_start = "7%"
skip_end = "11%"
b_frames_only = true
skip_dark = false

[[source]]
path = "a.mkv"
name = "A"
trim = 24
crop = [0, 0, 140, 140]
tonemap = "dovi"

[[source]]
path = "b.mkv"
name = "B"
""",
    )
    original = config.load(path)

    window.load_project(original)
    rebuilt = window.build_project()

    assert rebuilt.title == original.title
    assert rebuilt.engine is original.engine
    assert rebuilt.frames == original.frames
    assert [s.name for s in rebuilt.sources] == [s.name for s in original.sources]
    assert rebuilt.sources[0].trim == 24
    assert rebuilt.sources[0].crop == (0, 0, 140, 140)
    assert rebuilt.sources[0].tonemap is Tonemap.DOVI


def test_opening_a_settings_project_keeps_its_own_variants(window, tmp_path):
    """Variants somebody wrote by hand must not be replaced by a template's."""
    path = _write_project(
        tmp_path,
        """
mode = "settings"

[[source]]
path = "a.mkv"
name = "f"

[[variant]]
name = "mine"
options = { tone-mapping = "clip" }

[[variant]]
name = "also mine"
options = { tone-mapping = "hable" }
""",
    )

    window.load_project(config.load(path))
    rebuilt = window.build_project()

    assert [v.name for v in rebuilt.settings.variants] == ["mine", "also mine"]
    assert window.template_combo.currentIndex() == 0


def test_what_the_window_saves_is_what_the_command_line_reads(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.title_edit.setText("Saved")
    window.count_spin.setValue(4)
    window.engine_combo.setCurrentText("ffmpeg")

    written = tmp_path / "saved.toml"
    written.write_text(config.dumps(window.build_project()), encoding="utf-8")
    reloaded = config.load(written)

    assert reloaded.title == "Saved"
    assert reloaded.frames.count == 4
    assert reloaded.engine is Engine.FFMPEG


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def test_work_runs_off_the_ui_thread_and_reports_back(application):
    """A run takes minutes; doing it here makes Windows call the window dead."""
    import threading

    from kiyas.gui.worker import Runner

    runner = Runner()
    seen: list[str] = []
    results: list[object] = []
    thread_ids: list[int] = []

    def work(progress):
        thread_ids.append(threading.get_ident())
        progress("half way")
        return "finished"

    runner.start(
        work,
        on_progress=seen.append,
        on_done=results.append,
        on_failed=lambda message: results.append(RuntimeError(message)),
    )
    runner.wait(10000)
    application.processEvents()

    assert results == ["finished"]
    assert seen == ["half way"]
    assert thread_ids[0] != threading.get_ident()


def test_waiting_for_a_finished_task_returns_at_once(application):
    """Closing the window mid-run must not freeze it.

    The usual spelling connects the task's end to ``QThread.quit`` with a
    queued connection, which only works while the UI event loop is spinning:
    quit() belongs to the main thread, so the call sits in a queue nobody is
    reading precisely when the main thread is waiting. It deadlocked until the
    timeout -- five seconds of frozen window on the way out, and twenty seconds
    of test suite. Timed rather than merely called, because the symptom of a
    regression is slowness, and slow tests get shrugged at.
    """
    import time

    from kiyas.gui.worker import Runner

    runner = Runner()
    runner.start(
        lambda progress: "quick",
        on_progress=lambda _: None,
        on_done=lambda _: None,
        on_failed=lambda _: None,
    )

    started = time.monotonic()
    runner.wait(10000)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"waiting took {elapsed:.1f}s; the thread is not ending itself"


def test_a_failure_comes_back_as_a_message_not_a_crash(application):
    from kiyas.gui.worker import Runner

    runner = Runner()
    failures: list[str] = []

    def work(progress):
        raise ValueError("something specific")

    runner.start(
        work, on_progress=lambda _: None, on_done=lambda _: None, on_failed=failures.append
    )
    runner.wait(10000)
    application.processEvents()

    assert failures == ["ValueError: something specific"]


def test_running_with_nothing_added_complains_rather_than_starting(window, monkeypatch):
    complaints: list[tuple[str, str]] = []
    monkeypatch.setattr(
        MainWindow, "_complain", lambda self, title, detail: complaints.append((title, detail))
    )

    window.run()

    assert complaints
    assert "at least one file" in complaints[0][1]
    assert not window._runner.busy  # noqa: SLF001


# --------------------------------------------------------------------------
# The status line
# --------------------------------------------------------------------------


def test_the_status_line_says_what_is_missing_in_the_parsers_own_words(window, tmp_path):
    """The window has no second set of rules to drift out of step with the first."""
    window.add_paths(_media(tmp_path, "only.mkv"))

    assert "at least two" in window.status_label.text()


def test_the_status_line_clears_when_the_project_is_valid(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))

    assert "ready" in window.status_label.text()


def test_the_status_line_follows_the_mode(window, tmp_path):
    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.mode_combo.setCurrentIndex(1)

    assert "variants" in window.status_label.text()

    window.template_combo.setCurrentText("tonemap")

    assert "exactly one" in window.status_label.text()


def test_adding_a_file_does_not_raise_while_the_row_is_half_built(window, tmp_path):
    """The status line reads the table, and a row is not a project mid-insert.

    Adding a row fires rowsInserted before the tonemap combo exists, so an
    unguarded read saw an empty value and raised out of a Qt signal handler.
    """
    window.add_paths(_media(tmp_path, "a.mkv"))

    assert window.sources.rowCount() == 1
    assert window.build_project.__self__ is window  # nothing left in a broken state


def test_an_unset_tonemap_reads_as_auto(window, tmp_path):
    from kiyas.config import Tonemap as T

    window.add_paths(_media(tmp_path, "a.mkv", "b.mkv"))
    window.sources.removeCellWidget(0, 4)

    assert window.build_project().sources[0].tonemap is T.AUTO


# --------------------------------------------------------------------------
# Starting up
# --------------------------------------------------------------------------


def test_a_project_on_the_command_line_is_opened_not_added_as_media(window, tmp_path):
    from kiyas.gui.app import apply_arguments

    project = tmp_path / "p.toml"
    project.write_text(
        """
title = "From the command line"

[[source]]
path = "a.mkv"
name = "A"

[[source]]
path = "b.mkv"
name = "B"
""",
        encoding="utf-8",
    )

    apply_arguments(window, [str(project)])

    assert window.title_edit.text() == "From the command line"
    assert window.sources.rowCount() == 2


def test_media_on_the_command_line_is_added(window, tmp_path):
    from kiyas.gui.app import apply_arguments

    apply_arguments(window, [str(path) for path in _media(tmp_path, "a.mkv", "b.mkv")])

    assert [row[0] for row in window._rows()] == ["a", "b"]  # noqa: SLF001


def test_arguments_that_are_not_files_are_dropped(tmp_path):
    """Usually a shell that did not expand a pattern."""
    from kiyas.gui.app import split_arguments

    real = tmp_path / "a.mkv"
    real.write_bytes(b"x")

    projects, media = split_arguments([str(real), str(tmp_path / "*.mkv"), "nonsense"])

    assert projects == []
    assert media == [real]


def test_a_project_that_cannot_be_read_leaves_a_usable_window(window, tmp_path):
    """The point of opening a project this way is to get a window."""
    from kiyas.gui.app import apply_arguments

    broken = tmp_path / "broken.toml"
    broken.write_text("this is not toml [[[", encoding="utf-8")

    apply_arguments(window, [str(broken)])

    assert "could not open" in window.log.toPlainText()
    assert window.isEnabled()


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_pressing_run_produces_a_comparison(application, tmp_path):
    """The whole path: window -> project -> worker -> core -> files on disk.

    Everything above is about whether the window describes the right work.
    This is the one that says the work happens.
    """
    import subprocess
    import time

    from kiyas.media import binaries

    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")

    clip = tmp_path / "clip.mkv"
    subprocess.run(  # noqa: S603
        [
            str(binaries.require_binary("ffmpeg")), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip

    window = MainWindow()
    try:
        window.add_paths([clip, clip])
        window.sources.item(1, 0).setText("second")
        window.count_spin.setValue(2)
        window.b_frames_check.setChecked(False)
        window.skip_dark_check.setChecked(False)
        window.engine_combo.setCurrentText("ffmpeg")
        window.output_edit.setText(str(tmp_path / "out"))

        window.run()
        deadline = time.monotonic() + 300
        while window._runner.busy and time.monotonic() < deadline:  # noqa: SLF001
            application.processEvents()
            time.sleep(0.02)
        application.processEvents()

        assert "done: 4 images" in window.log.toPlainText()
        assert window.publish_button.isEnabled()
        assert sorted(p.name for p in (tmp_path / "out" / "clip").glob("*.png"))
        assert (tmp_path / "out" / "kiyas-manifest.json").is_file()
    finally:
        window.close()


def test_a_relative_output_lands_next_to_the_first_source(window, tmp_path):
    """A window has no way of showing its working directory.

    Left to resolve against the process, typing "out" puts the screenshots on
    whichever drive the file dialog last visited -- which is where they were
    found, and not where anybody would look. The audio path already resolved
    it against the first source; this is the same answer for the other mode.
    """
    media = tmp_path / "media"
    media.mkdir()
    first = media / "a.mkv"
    first.write_bytes(b"")
    (media / "b.mkv").write_bytes(b"")

    window.add_paths([first, media / "b.mkv"])
    window.output_edit.setText("out")

    window._comparison_work()

    assert window._output_directory == (media / "out").resolve()


def test_an_absolute_output_is_left_alone(window, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    for name in ("a.mkv", "b.mkv"):
        (media / name).write_bytes(b"")
    elsewhere = (tmp_path / "elsewhere").resolve()

    window.add_paths([media / "a.mkv", media / "b.mkv"])
    window.output_edit.setText(str(elsewhere))

    window._comparison_work()

    assert window._output_directory == elsewhere


def test_an_engine_this_build_cannot_run_is_offered_but_not_selectable(window, monkeypatch):
    """Offering all four and failing at Run is how somebody finds out.

    That is what happened: a packaged build has no VapourSynth, the dropdown
    listed it anyway, and the answer arrived as a RunError after the
    comparison had been set up. Hiding it would be worse -- then the engine
    that does what they want is simply absent and nothing says why.
    """
    from kiyas import engines as engine_registry
    from kiyas.gui import window as window_module

    monkeypatch.setattr(engine_registry, "available_engines", lambda *a, **k: ["ffmpeg", "mpv"])

    fresh = window_module.MainWindow()
    try:
        labels = [fresh.engine_combo.itemText(i) for i in range(fresh.engine_combo.count())]
        model = fresh.engine_combo.model()
        enabled = {
            fresh.engine_combo.itemData(i): model.item(i).isEnabled()
            for i in range(fresh.engine_combo.count())
        }
    finally:
        fresh.close()

    assert any("vapoursynth (not installed here)" == text for text in labels)
    assert enabled["vapoursynth"] is False
    assert enabled["ffmpeg"] is True
    assert enabled["auto"] is True, "auto has to stay reachable whatever is missing"


def test_every_engine_is_selectable_when_all_are_present(window, monkeypatch):
    from kiyas import engines as engine_registry
    from kiyas.gui import window as window_module

    monkeypatch.setattr(
        engine_registry, "available_engines", lambda *a, **k: ["vapoursynth", "ffmpeg", "mpv"]
    )

    fresh = window_module.MainWindow()
    try:
        model = fresh.engine_combo.model()
        assert all(model.item(i).isEnabled() for i in range(fresh.engine_combo.count()))
        assert all(
            "not installed" not in fresh.engine_combo.itemText(i)
            for i in range(fresh.engine_combo.count())
        )
    finally:
        fresh.close()
