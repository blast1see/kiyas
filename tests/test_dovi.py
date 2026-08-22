"""Extracting a Dolby Vision enhancement layer.

Nothing here needs a Dolby Vision file. The two things that can go wrong in
this module are both structural rather than pictorial: the cache can hand back
a layer belonging to a different file, and a half-finished extraction can be
mistaken for a finished one. Both are silent -- a wrong enhancement layer
composes a plausible picture, and a truncated one composes correctly for the
first part of the film and not at all for the rest -- so they are worth pinning
down without waiting for tens of gigabytes of real material.

The integration test drives the real pipe with a fake ``dovi_tool``, which is
enough to prove the plumbing: that ffmpeg's output reaches the second process,
that the result lands atomically, and that a failure leaves no cache behind.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kiyas.media import binaries, dovi
from kiyas.media.dovi import DoviError


def _source(tmp_path: Path, name: str = "film.mkv", data: bytes = b"x" * 64) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# Where the layer is kept
# --------------------------------------------------------------------------


def test_the_cache_is_named_after_the_source(tmp_path):
    source = _source(tmp_path)

    target = dovi.enhancement_layer_path(source, tmp_path)

    assert target.parent == tmp_path
    assert target.name.startswith("film.")
    assert target.name.endswith(".el.hevc")


def test_a_replaced_source_does_not_reuse_the_old_layer(tmp_path):
    """A re-encode that keeps the filename is the realistic way to get a
    silently wrong picture here, so the size goes in the name."""
    source = _source(tmp_path, data=b"x" * 64)
    before = dovi.enhancement_layer_path(source, tmp_path)

    source.write_bytes(b"y" * 128)
    after = dovi.enhancement_layer_path(source, tmp_path)

    assert before != after


def test_two_sources_with_the_same_name_do_not_share_a_layer(tmp_path):
    """A shared index directory collects `film.mkv` from several folders.

    Same name and, here, the same size and modification time too -- which is
    ordinary for two rips of one disc. Only the path tells them apart.
    """
    first = _source_in(tmp_path / "a", b"x" * 64)
    second = _source_in(tmp_path / "b", b"x" * 64)

    assert dovi.enhancement_layer_path(first, tmp_path) != dovi.enhancement_layer_path(
        second, tmp_path
    )


def _source_in(directory: Path, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "film.mkv"
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# Reuse
# --------------------------------------------------------------------------


def test_an_existing_layer_is_reused_without_running_anything(tmp_path):
    source = _source(tmp_path)
    target = dovi.enhancement_layer_path(source, tmp_path)
    target.write_bytes(b"already here")

    # Both binaries are named as paths that do not exist. If either were run,
    # this would raise instead of returning.
    result = dovi.extract_enhancement_layer(
        source, tmp_path, ffmpeg=tmp_path / "nope", dovi_tool=tmp_path / "nope"
    )

    assert result == target


def test_an_empty_layer_is_not_treated_as_a_cache(tmp_path):
    """A zero-byte file is what a killed extraction leaves behind."""
    source = _source(tmp_path)
    dovi.enhancement_layer_path(source, tmp_path).touch()

    with pytest.raises(binaries.BinaryNotFound):
        dovi.extract_enhancement_layer(
            source, tmp_path, ffmpeg=tmp_path / "nope", dovi_tool=tmp_path / "nope"
        )


def test_a_missing_tool_is_named(tmp_path):
    source = _source(tmp_path)

    with pytest.raises(binaries.BinaryNotFound, match="nope"):
        dovi.extract_enhancement_layer(
            source, tmp_path, ffmpeg=tmp_path / "nope", dovi_tool=tmp_path / "nope"
        )


# --------------------------------------------------------------------------
# The pipe itself
# --------------------------------------------------------------------------


def _fake_dovi_tool(tmp_path: Path, *, body: str) -> Path:
    """A stand-in for dovi_tool that reads --el-out and writes something.

    Written as a script rather than mocked out, because what is being checked
    is that two real processes are connected to each other.
    """
    script = tmp_path / "fake_dovi_tool.py"
    script.write_text(body, encoding="utf-8")
    launcher = tmp_path / ("fake.cmd" if sys.platform == "win32" else "fake.sh")
    if sys.platform == "win32":
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return launcher


_COPIES_STDIN = """
import sys
out = sys.argv[sys.argv.index("--el-out") + 1]
with open(out, "wb") as handle:
    handle.write(sys.stdin.buffer.read())
"""

_FAILS = """
import sys
sys.stderr.write("something went wrong in the RPU\\n")
sys.exit(1)
"""

_WRITES_NOTHING = """
import sys
out = sys.argv[sys.argv.index("--el-out") + 1]
open(out, "wb").close()
"""


@pytest.mark.integration
def test_the_video_stream_reaches_the_second_process(tmp_path):
    """The bytes ffmpeg copies out have to arrive at dovi_tool's stdin."""
    ffmpeg = binaries.find_binary("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not available")

    media = tmp_path / "clip.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=1",
            "-c:v",
            "libx265",
            "-x265-params",
            "log-level=none",
            str(media),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )

    layer = dovi.extract_enhancement_layer(
        media,
        tmp_path / "cache",
        ffmpeg=ffmpeg,
        dovi_tool=_fake_dovi_tool(tmp_path, body=_COPIES_STDIN),
    )

    assert layer.is_file()
    assert layer.stat().st_size > 0
    # Annex-B start code: proof the bitstream filter was applied rather than
    # the length-prefixed form Matroska stores.
    assert layer.read_bytes()[:4] == b"\x00\x00\x00\x01"


@pytest.mark.integration
def test_a_failure_leaves_no_cache_behind(tmp_path):
    """The next run must extract again rather than reuse a stub."""
    ffmpeg = binaries.find_binary("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not available")

    media = tmp_path / "clip.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=1",
            "-c:v",
            "libx265",
            "-x265-params",
            "log-level=none",
            str(media),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    cache = tmp_path / "cache"

    with pytest.raises(DoviError, match="RPU"):
        dovi.extract_enhancement_layer(
            media, cache, ffmpeg=ffmpeg, dovi_tool=_fake_dovi_tool(tmp_path, body=_FAILS)
        )

    assert not dovi.enhancement_layer_path(media, cache).exists()
    assert list(cache.glob("*.partial")) == []


@pytest.mark.integration
def test_an_empty_result_is_reported_rather_than_cached(tmp_path):
    ffmpeg = binaries.find_binary("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not available")

    media = tmp_path / "clip.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=1",
            "-c:v",
            "libx265",
            "-x265-params",
            "log-level=none",
            str(media),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    cache = tmp_path / "cache"

    with pytest.raises(DoviError, match="produced nothing"):
        dovi.extract_enhancement_layer(
            media, cache, ffmpeg=ffmpeg, dovi_tool=_fake_dovi_tool(tmp_path, body=_WRITES_NOTHING)
        )

    assert not dovi.enhancement_layer_path(media, cache).exists()
