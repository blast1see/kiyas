# PyInstaller build. Run with: pyinstaller --noconfirm kiyas.spec
#
# Two executables from one collection: kiyas.exe is a console program and
# kiyas-gui.exe is not, so double-clicking the window does not leave a black
# terminal behind it. They share every bundled library, which is why this is
# one spec rather than two.
#
# **VapourSynth is deliberately not in here.** It is a stack of compiled
# plugins that installs into a virtualenv's site-packages, and freezing it
# would mean shipping something that cannot then be extended with the plugin
# a particular comparison needs. A frozen kiyas has the ffmpeg and mpv engines;
# `kiyas doctor` says so rather than leaving anyone to work it out. Anyone who
# wants the VapourSynth engine wants the checkout and bootstrap.ps1.
#
# One directory rather than one file: a single-file PySide6 build unpacks
# several hundred megabytes into a temporary directory on *every* launch, which
# turns a double-click into a wait.

from pathlib import Path

BLOCK_CIPHER = None
ROOT = Path(SPECPATH)

#: Things a frozen build must not drag in.
EXCLUDES = [
    # Not shippable; see above.
    "vapoursynth",
    "awsmfunc",
    "vstools",
    # matplotlib pulls these in and they are megabytes of interactive backends
    # that a program drawing to a PNG never touches.
    "tkinter",
    "matplotlib.backends._backend_tk",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    # Test machinery has no business in a release.
    "pytest",
    "ruff",
]

analysis = Analysis(
    [str(ROOT / "packaging" / "entry_cli.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["kiyas.gui", "kiyas.gui.window", "kiyas.audio"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

gui_analysis = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["kiyas.gui", "kiyas.gui.window", "kiyas.audio"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

MERGE((analysis, "kiyas", "kiyas"), (gui_analysis, "kiyas-gui", "kiyas-gui"))

cli_pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=BLOCK_CIPHER)
gui_pyz = PYZ(gui_analysis.pure, gui_analysis.zipped_data, cipher=BLOCK_CIPHER)

cli_exe = EXE(
    cli_pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="kiyas",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="kiyas-gui",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

COLLECT(
    cli_exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.zipfiles,
    gui_analysis.datas,
    strip=False,
    upx=False,
    name="kiyas",
)
