"""The binary resolution rules.

The security-relevant assertion is :func:`test_cwd_is_never_searched`. Everything
else in this file exists to keep that one honest.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from kiyas.media import binaries


@pytest.fixture(autouse=True)
def _clear_cache():
    binaries.clear_cache()
    yield
    binaries.clear_cache()


def _make_fake_binary(directory: Path, name: str = "ffmpeg") -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = directory / f"{name}{suffix}"
    path.write_text("not really an executable")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def test_finds_binary_on_absolute_path_entry(tmp_path, monkeypatch):
    expected = _make_fake_binary(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert binaries.find_binary("ffmpeg") == expected


def test_cwd_is_never_searched(tmp_path, monkeypatch):
    """An executable next to the user's media must never be picked up.

    kiyas is routinely run from a release directory. If PATH resolution ever
    fell back to the working directory, dropping a hostile ffmpeg.exe beside a
    torrent would be enough to get it executed.
    """
    _make_fake_binary(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    assert binaries.find_binary("ffmpeg") is None


def test_relative_path_entries_are_ignored(tmp_path, monkeypatch):
    _make_fake_binary(tmp_path)
    monkeypatch.chdir(tmp_path)
    # "." and "" are how a shell spells "the current directory".
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "relative/sub"]))

    assert binaries.find_binary("ffmpeg") is None


def test_first_matching_path_entry_wins(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    expected = _make_fake_binary(first)
    _make_fake_binary(second)
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))

    assert binaries.find_binary("ffmpeg") == expected


def test_unreadable_path_entry_does_not_abort_search(tmp_path, monkeypatch):
    """A stale network drive on PATH is common and must not break resolution."""
    good = tmp_path / "good"
    good.mkdir()
    expected = _make_fake_binary(good)
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("PATH", os.pathsep.join([str(missing), str(good)]))

    assert binaries.find_binary("ffmpeg") == expected


def test_configured_absolute_path_is_used(tmp_path, monkeypatch):
    configured = _make_fake_binary(tmp_path, "mediainfo")
    monkeypatch.setenv("PATH", "")

    assert binaries.find_binary("mediainfo", configured=configured) == configured


def test_configured_relative_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_fake_binary(tmp_path, "mediainfo")

    with pytest.raises(binaries.BinaryNotFound):
        binaries.find_binary("mediainfo", configured="mediainfo.exe")


def test_configured_missing_path_is_reported(tmp_path):
    with pytest.raises(binaries.BinaryNotFound):
        binaries.find_binary("mediainfo", configured=tmp_path / "nope.exe")


def test_require_binary_raises_when_absent(monkeypatch):
    monkeypatch.setenv("PATH", "")

    with pytest.raises(binaries.BinaryNotFound):
        binaries.require_binary("definitely-not-a-real-tool")


def test_duplicate_path_entries_are_searched_once(tmp_path, monkeypatch):
    expected = _make_fake_binary(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path)] * 3))

    assert binaries.find_binary("ffmpeg") == expected


@pytest.mark.integration
def test_probe_version_reads_real_ffmpeg():
    path = binaries.find_binary("ffmpeg")
    if path is None:
        pytest.skip("ffmpeg is not installed")

    version = binaries.probe_version(path, "ffmpeg")

    assert version is not None
    assert "ffmpeg" in version.lower()


# --------------------------------------------------------------------------
# Console windows
# --------------------------------------------------------------------------


def test_every_subprocess_is_launched_without_a_console_window():
    """A windowed build has no console, so each child that wants one gets its own.

    That is not a cosmetic detail at this scale: a comparison runs hundreds of
    short ffprobe and ffmpeg calls, and every one of them flashed a black
    window on screen. Measured from a real windowed parent, watching every
    20 ms for console windows that were not there before -- 20 calls put up 39
    windows without the flag and none with it.

    This walks the source rather than the behaviour because the failure is
    invisible to a test: everything works, the pictures are right, and the only
    symptom is on the user's screen. It is also the kind of thing that comes
    back one call site at a time.
    """
    root = Path(__file__).resolve().parent.parent / "src" / "kiyas"
    launchers = ("subprocess.Popen", "subprocess.run", "subprocess.check_output")
    offenders = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not ast.unparse(node.func).endswith(launchers):
                continue
            if "creationflags" in {keyword.arg for keyword in node.keywords}:
                continue
            # Named by file rather than by line: pinning a line number here
            # makes the guard fail on any edit above the call, which teaches
            # people to update the expected value instead of reading it.
            offenders.append(str(path.relative_to(root)))

    # setup_env._run inherits the console on purpose, so pip's progress is
    # visible while it installs; the flag would send that output nowhere. It is
    # unreachable from a windowed build anyway, which refuses to run setup.
    assert offenders == ["setup_env.py"], (
        f"these subprocesses can open a console window on Windows: {offenders}"
    )
