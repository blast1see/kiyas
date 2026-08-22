"""The main window.

The window holds a :class:`kiyas.config.Project` and edits it. When it is time
to run, it writes that project to TOML and reads it back through the ordinary
parser -- so the GUI validates with exactly the code the command line uses, and
cannot accept something ``kiyas run`` would refuse. The error messages the user
sees are the same ones, too, which is why they were worth writing carefully.

Audio is a mode here but not a project mode: a project file describes a set of
frames to capture, and an audio comparison has none. The button runs the same
thing ``kiyas audio`` does.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..config import Engine, FrameMethod, FrameSelection, Mode, Project, Source, Tonemap
from ..mpvctl.variants import TEMPLATES, Variant, expand_template
from .worker import Runner

#: Extensions offered in the file dialog. Anything ffmpeg opens will work; this
#: is the list that saves scrolling, not a restriction.
MEDIA_FILTER = (
    "Media (*.mkv *.mp4 *.m2ts *.ts *.avi *.mov *.webm *.flac *.ac3 *.eac3 "
    "*.dts *.thd *.wav *.m4a *.aac *.opus);;All files (*)"
)

SOURCE_COLUMNS = ("Name", "Trim", "Crop", "Resize", "Tonemap", "File")


class GuiError(ValueError):
    """Raised when what is on screen cannot be turned into a project."""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("kiyas")
        self.resize(1100, 760)
        self.setAcceptDrops(True)

        self._runner = Runner(self)
        self._variants: list[Variant] = []
        self._output_directory: Path | None = None

        self._build_menu()
        self._build_body()
        self._apply_mode()

    # -- construction ----------------------------------------------------

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open project…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        file_menu.addAction(open_action)

        save_action = QAction("&Save project…", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_project)
        file_menu.addAction(save_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _build_body(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        layout.addLayout(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_sources())
        splitter.addWidget(self._build_settings())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, stretch=3)

        layout.addLayout(self._build_run_row())

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setPlaceholderText("Progress and warnings appear here.")
        layout.addWidget(self.log, stretch=2)

        layout.addLayout(self._build_results_row())
        self.setCentralWidget(central)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("What is being compared")
        row.addWidget(self.title_edit, stretch=1)

        row.addWidget(QLabel("Compare"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("several files (source)", "source")
        self.mode_combo.addItem("one file, several render settings", "settings")
        self.mode_combo.addItem("audio tracks", "audio")
        self.mode_combo.currentIndexChanged.connect(self._apply_mode)
        row.addWidget(self.mode_combo)
        return row

    def _build_sources(self) -> QWidget:
        box = QGroupBox("Files")
        layout = QVBoxLayout(box)

        self.sources = QTableWidget(0, len(SOURCE_COLUMNS))
        self.sources.setHorizontalHeaderLabels(SOURCE_COLUMNS)
        self.sources.verticalHeader().setVisible(False)
        header = self.sources.horizontalHeader()
        header.setSectionResizeMode(len(SOURCE_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.sources.itemChanged.connect(self._refresh_status)
        self.sources.model().rowsInserted.connect(self._refresh_status)
        self.sources.model().rowsRemoved.connect(self._refresh_status)
        layout.addWidget(self.sources)

        # Says what is wrong now rather than at the moment Run is pressed. The
        # text comes from actually building the project, so it is the same
        # sentence the command line would print -- there is no second set of
        # rules here to drift out of step with the first.
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        for label, slot in (
            ("Add files…", self.add_files),
            ("Remove", self.remove_selected),
            ("Move up", lambda: self._move_selected(-1)),
            ("Move down", lambda: self._move_selected(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return box

    def _build_settings(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # -- frames
        self.frames_box = QGroupBox("Frames")
        frames = QVBoxLayout(self.frames_box)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Choose"))
        self.method_combo = QComboBox()
        self.method_combo.addItem("a number of frames", "count")
        self.method_combo.addItem("one every so often", "interval")
        self.method_combo.addItem("these exact frames", "manual")
        self.method_combo.currentIndexChanged.connect(self._apply_method)
        method_row.addWidget(self.method_combo, stretch=1)
        frames.addLayout(method_row)

        self.method_stack = QStackedWidget()
        self.count_spin = QSpinBox(minimum=1, maximum=999, value=12)
        self.interval_spin = QDoubleSpinBox(minimum=1.0, maximum=36000.0, value=300.0)
        self.interval_spin.setSuffix(" s")
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("1200, 34500, 91000   (kiyas pick prints these)")
        for widget in (self.count_spin, self.interval_spin, self.manual_edit):
            self.method_stack.addWidget(widget)
        frames.addWidget(self.method_stack)

        skips = QHBoxLayout()
        skips.addWidget(QLabel("Skip start"))
        self.skip_start = QDoubleSpinBox(minimum=0.0, maximum=49.0, value=5.0)
        self.skip_start.setSuffix(" %")
        skips.addWidget(self.skip_start)
        skips.addWidget(QLabel("end"))
        self.skip_end = QDoubleSpinBox(minimum=0.0, maximum=49.0, value=10.0)
        self.skip_end.setSuffix(" %")
        skips.addWidget(self.skip_end)
        skips.addStretch(1)
        frames.addLayout(skips)

        self.b_frames_check = QCheckBox("B-frames only (I-frames flatter a weak encode)")
        self.b_frames_check.setChecked(True)
        frames.addWidget(self.b_frames_check)
        self.skip_dark_check = QCheckBox("Skip dark frames (a black frame compares nothing)")
        self.skip_dark_check.setChecked(True)
        frames.addWidget(self.skip_dark_check)
        layout.addWidget(self.frames_box)

        # -- render settings, for a settings comparison
        self.render_box = QGroupBox("Render settings")
        render = QVBoxLayout(self.render_box)
        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("Template"))
        self.template_combo = QComboBox()
        self.template_combo.addItem("(keep what the project has)", "")
        for name in sorted(TEMPLATES):
            self.template_combo.addItem(name, name)
        self.template_combo.currentIndexChanged.connect(self._apply_template)
        template_row.addWidget(self.template_combo, stretch=1)
        render.addLayout(template_row)

        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Capture width"))
        self.width_spin = QSpinBox(minimum=0, maximum=7680, value=0)
        self.width_spin.setSpecialValueText("source width")
        self.width_spin.setSuffix(" px")
        width_row.addWidget(self.width_spin)
        self.fullscreen_check = QCheckBox("Full screen")
        width_row.addWidget(self.fullscreen_check)
        width_row.addStretch(1)
        render.addLayout(width_row)

        self.variants_label = QLabel("No variants yet.")
        self.variants_label.setWordWrap(True)
        render.addWidget(self.variants_label)
        layout.addWidget(self.render_box)

        # -- audio
        self.audio_box = QGroupBox("Audio")
        audio = QHBoxLayout(self.audio_box)
        audio.addWidget(QLabel("Track"))
        self.track_spin = QSpinBox(minimum=0, maximum=32, value=0)
        self.track_spin.setPrefix("#")
        audio.addWidget(self.track_spin)
        audio.addWidget(QLabel("the same stream index is taken from every file"))
        audio.addStretch(1)
        layout.addWidget(self.audio_box)

        # -- output
        output_box = QGroupBox("Output")
        output = QVBoxLayout(output_box)
        directory_row = QHBoxLayout()
        self.output_edit = QLineEdit("out")
        directory_row.addWidget(self.output_edit, stretch=1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_output)
        directory_row.addWidget(browse)
        output.addLayout(directory_row)

        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("Engine"))
        self.engine_combo = QComboBox()
        for engine in Engine:
            self.engine_combo.addItem(engine.value, engine.value)
        engine_row.addWidget(self.engine_combo, stretch=1)
        output.addLayout(engine_row)
        layout.addWidget(output_box)

        layout.addStretch(1)
        return panel

    def _build_run_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setDefault(True)
        self.run_button.setMinimumWidth(140)
        self.run_button.clicked.connect(self.run)
        row.addWidget(self.run_button)
        row.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        row.addWidget(self.progress, stretch=1)
        return row

    def _build_results_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.open_button = QPushButton("Open output folder")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_output)
        row.addWidget(self.open_button)

        self.publish_button = QPushButton("Publish to slow.pics")
        self.publish_button.setEnabled(False)
        self.publish_button.clicked.connect(self.publish)
        row.addWidget(self.publish_button)

        self.link_edit = QLineEdit(readOnly=True)
        self.link_edit.setPlaceholderText("The published link appears here.")
        row.addWidget(self.link_edit, stretch=1)
        return row

    # -- mode ------------------------------------------------------------

    @property
    def mode(self) -> str:
        return str(self.mode_combo.currentData())

    def _apply_mode(self) -> None:
        mode = self.mode
        self.frames_box.setVisible(mode != "audio")
        self.render_box.setVisible(mode == "settings")
        self.audio_box.setVisible(mode == "audio")
        self.engine_combo.setEnabled(mode == "source")
        # Every column of a settings comparison is the same decoded frame, so
        # there is no weaker encode to flatter and nothing to be fair about.
        self.b_frames_check.setEnabled(mode != "settings")
        self._apply_method()
        self._refresh_status()

    def _refresh_status(self, *_args) -> None:
        """Report what is stopping a run, in the parser's own words.

        Runs on every keystroke, so it swallows everything: a label that
        cannot be updated is a much smaller problem than a window that throws
        while somebody is typing a name into a table.
        """
        count = self.sources.rowCount()
        if not count:
            self.status_label.setText("Drop files onto the window, or use Add files.")
            return
        if self.mode == "audio":
            self.status_label.setText(f"{count} file(s) ready.")
            return
        try:
            self.build_project()
        except GuiError as exc:
            self.status_label.setText(str(exc))
            return
        except Exception:  # noqa: BLE001 - never break typing over a hint
            self.status_label.setText(f"{count} file(s).")
            return
        self.status_label.setText(f"{count} file(s) ready.")

    def _apply_method(self) -> None:
        index = self.method_combo.currentIndex()
        self.method_stack.setCurrentIndex(index)

    def _apply_template(self) -> None:
        name = str(self.template_combo.currentData())
        if not name:
            return
        try:
            self._variants = expand_template(name, {})
        except Exception as exc:  # noqa: BLE001 - shaders need a list kiyas cannot guess
            self._variants = []
            self._note(f"{name}: {exc}")
        self._show_variants()

    def _show_variants(self) -> None:
        if not self._variants:
            self.variants_label.setText(
                "No variants yet. Pick a template, or open a project file that lists them."
            )
            self._refresh_status()
            return
        names = ", ".join(variant.name for variant in self._variants)
        self.variants_label.setText(f"{len(self._variants)} variants: {names}")
        self._refresh_status()

    # -- files -----------------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_file()
        ]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def add_files(self) -> None:
        chosen, _ = QFileDialog.getOpenFileNames(self, "Add files", "", MEDIA_FILTER)
        self.add_paths([Path(name) for name in chosen])

    def add_paths(self, paths: list[Path]) -> None:
        for path in paths:
            self._append_source(Source(path=path, name=self._suggest_name(path)))

    def _suggest_name(self, path: Path) -> str:
        """A column label that is not already taken.

        Names are the comparison's column headings and have to be unique, so
        two files called ``video.mkv`` from different folders cannot both be
        "video".
        """
        taken = {name for name, *_ in self._rows()}
        base = path.stem
        if base not in taken:
            return base
        suffix = 2
        while f"{base} #{suffix}" in taken:
            suffix += 1
        return f"{base} #{suffix}"

    def _append_source(self, source: Source) -> None:
        # Signals stay blocked until the row is complete: the status line reads
        # the table, and a row that is half built is not a project.
        self.sources.blockSignals(True)
        try:
            self._append_source_cells(source)
        finally:
            self.sources.blockSignals(False)
        self._refresh_status()

    def _append_source_cells(self, source: Source) -> None:
        row = self.sources.rowCount()
        self.sources.insertRow(row)
        self.sources.setItem(row, 0, QTableWidgetItem(source.name))
        self.sources.setItem(row, 1, QTableWidgetItem(str(source.trim or "")))
        self.sources.setItem(
            row, 2, QTableWidgetItem(",".join(str(v) for v in source.crop) if source.crop else "")
        )
        self.sources.setItem(
            row,
            3,
            QTableWidgetItem(f"{source.resize[0]}x{source.resize[1]}" if source.resize else ""),
        )

        tonemap = QComboBox()
        for option in Tonemap:
            tonemap.addItem(option.value, option.value)
        tonemap.setCurrentText(source.tonemap.value)
        self.sources.setCellWidget(row, 4, tonemap)

        path_item = QTableWidgetItem(str(source.path))
        path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        path_item.setToolTip(str(source.path))
        self.sources.setItem(row, 5, path_item)

    def remove_selected(self) -> None:
        for row in sorted({index.row() for index in self.sources.selectedIndexes()}, reverse=True):
            self.sources.removeRow(row)

    def _move_selected(self, delta: int) -> None:
        rows = sorted({index.row() for index in self.sources.selectedIndexes()})
        if len(rows) != 1:
            return
        row = rows[0]
        target = row + delta
        if not 0 <= target < self.sources.rowCount():
            return
        sources = self._collect_sources()
        sources[row], sources[target] = sources[target], sources[row]
        self._set_sources(sources)
        self.sources.selectRow(target)

    def _rows(self) -> list[tuple[str, str, str, str, str, str]]:
        collected = []
        for row in range(self.sources.rowCount()):
            cells = []
            for column in range(len(SOURCE_COLUMNS)):
                widget = self.sources.cellWidget(row, column)
                if isinstance(widget, QComboBox):
                    cells.append(widget.currentText())
                else:
                    item = self.sources.item(row, column)
                    cells.append(item.text().strip() if item else "")
            collected.append(tuple(cells))
        return collected

    def _set_sources(self, sources: list[Source]) -> None:
        self.sources.setRowCount(0)
        for source in sources:
            self._append_source(source)

    # -- turning the window into a project -------------------------------

    def _collect_sources(self) -> list[Source]:
        sources: list[Source] = []
        for index, (name, trim, crop, resize, tonemap, path) in enumerate(self._rows(), start=1):
            source = Source(path=Path(path), name=name or Path(path).stem)
            if trim:
                source.trim = _positive_int(trim, f"row {index}", "trim")
            if crop:
                values = _int_list(crop, 4, f"row {index}", "crop", "left,right,top,bottom")
                source.crop = (values[0], values[1], values[2], values[3])
            if resize:
                values = _int_list(
                    resize.replace("x", ","), 2, f"row {index}", "resize", "WIDTHxHEIGHT"
                )
                source.resize = (values[0], values[1])
            # A half-built row -- the combo is added after the cells -- reads
            # as an empty tonemap. It means "not set yet", not "invalid".
            source.tonemap = Tonemap(tonemap) if tonemap else Tonemap.AUTO
            sources.append(source)
        return sources

    def _collect_frames(self) -> FrameSelection:
        method = FrameMethod(str(self.method_combo.currentData()))
        frames = FrameSelection(
            method=method,
            count=self.count_spin.value(),
            interval_seconds=self.interval_spin.value(),
            skip_start=self.skip_start.value() / 100.0,
            skip_end=self.skip_end.value() / 100.0,
            b_frames_only=self.b_frames_check.isChecked() and self.mode != "settings",
            skip_dark=self.skip_dark_check.isChecked(),
        )
        if method is FrameMethod.MANUAL:
            text = self.manual_edit.text().replace(",", " ")
            numbers = [_positive_int(part, "frames", "manual") for part in text.split()]
            if not numbers:
                raise GuiError("Choosing exact frames needs at least one frame number.")
            frames.manual = tuple(sorted(set(numbers)))
        return frames

    def build_project(self) -> Project:
        """Everything on screen, as a validated project.

        Validation happens by writing the project out and reading it back
        through the ordinary parser. It costs nothing and it means the window
        can never accept something ``kiyas run`` would refuse -- including the
        rules it has no widget for.
        """
        sources = self._collect_sources()
        if not sources:
            raise GuiError("Add at least one file.")

        settings = None
        if self.mode == "settings":
            if not self._variants:
                raise GuiError("A settings comparison needs variants. Pick a template.")
            settings = config.Settings(
                variants=list(self._variants),
                width=self.width_spin.value() or None,
                fullscreen=self.fullscreen_check.isChecked(),
            )

        draft = Project(
            mode=Mode.SETTINGS if self.mode == "settings" else Mode.SOURCE,
            title=self.title_edit.text().strip(),
            sources=sources,
            frames=self._collect_frames(),
            engine=Engine(str(self.engine_combo.currentData())),
            output=Path(self.output_edit.text().strip() or "out"),
            settings=settings,
        )

        try:
            project = config.parse(tomllib.loads(config.dumps(draft)))
        except (config.ConfigError, tomllib.TOMLDecodeError) as exc:
            raise GuiError(str(exc)) from exc

        # Relative paths in a window mean "next to the media", since there is
        # no project file to be relative to yet.
        base = sources[0].path.parent
        if not project.output.is_absolute():
            project.output = (base / project.output).resolve()
        return project

    def load_project(self, project: Project) -> None:
        """Put a project on screen."""
        self.title_edit.setText(project.title)
        self.mode_combo.setCurrentIndex(1 if project.mode is Mode.SETTINGS else 0)
        self._set_sources(project.sources)

        frames = project.frames
        self.method_combo.setCurrentIndex(
            {FrameMethod.COUNT: 0, FrameMethod.INTERVAL: 1, FrameMethod.MANUAL: 2}[frames.method]
        )
        self.count_spin.setValue(frames.count)
        self.interval_spin.setValue(frames.interval_seconds)
        self.manual_edit.setText(", ".join(str(frame) for frame in frames.manual))
        self.skip_start.setValue(frames.skip_start * 100)
        self.skip_end.setValue(frames.skip_end * 100)
        self.b_frames_check.setChecked(frames.b_frames_only)
        self.skip_dark_check.setChecked(frames.skip_dark)

        self.engine_combo.setCurrentText(project.engine.value)
        self.output_edit.setText(str(project.output))

        self._variants = list(project.settings.variants) if project.settings else []
        if project.settings:
            self.width_spin.setValue(project.settings.width or 0)
            self.fullscreen_check.setChecked(project.settings.fullscreen)
        # Back to "keep what the project has", so opening a file and pressing
        # Run cannot silently swap its variants for a template's.
        self.template_combo.setCurrentIndex(0)
        self._show_variants()
        self._apply_mode()

    # -- actions ---------------------------------------------------------

    def open_project(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open project", "", "kiyas project (*.toml)")
        if not name:
            return
        try:
            self.load_project(config.load(name))
        except config.ConfigError as exc:
            self._complain("That project file could not be read", str(exc))
            return
        self._note(f"opened {name}")

    def save_project(self) -> None:
        try:
            project = self.build_project()
        except GuiError as exc:
            self._complain("Nothing to save yet", str(exc))
            return
        name, _ = QFileDialog.getSaveFileName(self, "Save project", "kiyas.toml", "TOML (*.toml)")
        if not name:
            return
        Path(name).write_text(config.dumps(project), encoding="utf-8")
        self._note(f"saved {name}")

    def browse_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Output directory")
        if chosen:
            self.output_edit.setText(chosen)

    def open_output(self) -> None:
        if self._output_directory is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_directory)))

    def run(self) -> None:
        if self._runner.busy:
            return
        try:
            work = self._audio_work() if self.mode == "audio" else self._comparison_work()
        except GuiError as exc:
            self._complain("That cannot be run yet", str(exc))
            return

        self.log.clear()
        self.link_edit.clear()
        self._busy(True)
        self._runner.start(
            work, on_progress=self._note, on_done=self._finished, on_failed=self._failed
        )

    def _comparison_work(self):
        from .. import run as run_module

        project = self.build_project()
        # A relative output is resolved against the first source, the way the
        # audio path already does it. Left alone it resolves against the
        # process's working directory, which a window has no way of showing --
        # typing "out" and finding the screenshots on whichever drive the file
        # dialog last visited is not a place anybody would look for them.
        if not project.output.is_absolute():
            project.output = (project.sources[0].path.parent / project.output).resolve()
        self._output_directory = project.output

        def work(progress):
            return run_module.run(project, progress=progress)

        return work

    def _audio_work(self):
        from ..audio import run as audio_run

        paths = [Path(path) for *_, path in self._rows()]
        if not paths:
            raise GuiError("Add at least one file.")
        output = Path(self.output_edit.text().strip() or "audio-out")
        if not output.is_absolute():
            output = (paths[0].parent / output).resolve()
        self._output_directory = output
        title = self.title_edit.text().strip()
        track = self.track_spin.value()

        def work(progress):
            return audio_run.run(
                paths, output=output, title=title, track_index=track, progress=progress
            )

        return work

    def publish(self) -> None:
        if self._runner.busy or self._output_directory is None:
            return
        from ..publish import load_manifest, slowpics

        try:
            comparison = load_manifest(self._output_directory)
        except Exception as exc:  # noqa: BLE001 - reported in the window
            self._complain("There is nothing to publish", str(exc))
            return

        answer = QMessageBox.question(
            self,
            "Publish to slow.pics",
            f"Upload {comparison.total_images} images as “{comparison.title}”?\n\n"
            f"The collection will be unlisted: anyone with the link can see it, "
            f"but it will not appear on the site.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return

        self._busy(True)

        def work(progress):
            return slowpics.upload(comparison, progress=progress)

        self._runner.start(
            work, on_progress=self._note, on_done=self._published, on_failed=self._failed
        )

    # -- results ---------------------------------------------------------

    def _busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.run_button.setEnabled(not busy)
        self.publish_button.setEnabled(not busy and self._output_directory is not None)

    def _note(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.progress.setFormat(text)

    def _finished(self, result) -> None:
        self._busy(False)
        for warning in getattr(result, "warnings", []):
            self._note(f"warning: {warning}")
        count = getattr(result, "image_count", 0)
        self._note(f"\ndone: {count} images in {self._output_directory}")
        self.open_button.setEnabled(True)
        self.publish_button.setEnabled(True)

    def _published(self, result) -> None:
        self._busy(False)
        url = getattr(result, "url", "")
        self.link_edit.setText(url)
        self._note(f"published: {url}")

    def _failed(self, message: str) -> None:
        self._busy(False)
        self._note(f"failed: {message}")
        self._complain("That did not work", message)

    def _complain(self, title: str, detail: str) -> None:
        QMessageBox.warning(self, title, detail)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        # A QThread destroyed while running takes the process with it.
        self._runner.wait(5000)
        super().closeEvent(event)


def _positive_int(text: str, where: str, field: str) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        raise GuiError(f"{where}: {field} must be a whole number, not {text!r}") from None
    if value < 0:
        raise GuiError(f"{where}: {field} cannot be negative")
    return value


def _int_list(text: str, count: int, where: str, field: str, shape: str) -> list[int]:
    parts = [part for part in text.replace(",", " ").split() if part]
    if len(parts) != count:
        raise GuiError(f"{where}: {field} looks like {shape}, got {text!r}")
    return [_positive_int(part, where, field) for part in parts]
