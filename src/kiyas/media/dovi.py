"""Getting a Dolby Vision enhancement layer out of a file, once.

A profile 7 release carries its picture in two pieces: a base layer that any
HDR10 decoder can read, and an enhancement layer that only a Dolby Vision
decoder composes back in. Screenshots taken from the base layer alone are not
what a Dolby Vision player shows, and comparing one release's base layer
against another's is comparing something neither viewer sees.

Extracting the layer means pushing the whole video stream through a pipe --
about 70 GB on a 4K disc remux -- so it happens once and the result is kept.
The layer itself is small by comparison: a few percent of the base layer,
because it codes a residual rather than a picture.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import binaries

#: Pushing 70 GB through two processes is disk-bound and slow, but it is not
#: unbounded: an hour is far longer than a local disc remux has ever taken and
#: still catches a pipe that has stopped moving.
_EXTRACT_TIMEOUT = 3600.0

#: What the extracted layer is called. What identifies which source it came
#: from goes in the name rather than into a sidecar file, so there is no second
#: file to go missing and leave a layer that looks valid for anything.
_SUFFIX = ".el.hevc"


class DoviError(RuntimeError):
    """Raised when an enhancement layer cannot be produced."""


def enhancement_layer_path(source: Path, cache_dir: Path) -> Path:
    """Where the layer extracted from ``source`` belongs.

    Three things go into the name and each rules out a way of composing the
    wrong residual onto every frame of a film:

    - the size and modification time, because re-encoding a source and keeping
      its filename is the ordinary way to end up with a stale layer;
    - a digest of the absolute path, because a shared ``index_dir`` collects
      several files called ``movie.mkv`` from different folders, and two of
      them can easily be the same size;
    - the stem, which does nothing but let a person looking at the directory
      tell what these files belong to.
    """
    stat = source.stat()
    fingerprint = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
    return cache_dir / f"{source.stem}.{stat.st_size}.{int(stat.st_mtime)}.{fingerprint}{_SUFFIX}"


def extract_enhancement_layer(
    source: Path,
    cache_dir: Path,
    *,
    ffmpeg: str | Path | None = None,
    dovi_tool: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Demux ``source``'s enhancement layer, or return the one already made.

    Raises :class:`DoviError` rather than letting either subprocess failure
    surface on its own, because the interesting part is which of the two
    stages failed and there are two of them.
    """
    target = enhancement_layer_path(source, cache_dir)
    if target.is_file() and target.stat().st_size > 0:
        return target

    ffmpeg_path = binaries.require_binary("ffmpeg", ffmpeg)
    dovi_path = binaries.require_binary("dovi_tool", dovi_tool)

    cache_dir.mkdir(parents=True, exist_ok=True)
    # Written under a temporary name and moved into place, so an extraction
    # that dies halfway cannot be picked up as a finished layer by the next
    # run -- the size check above would pass on a truncated file.
    partial = target.with_suffix(".partial")

    if progress is not None:
        progress(f"extracting the enhancement layer from {source.name} (this reads the whole file)")

    # '-i -' rather than the bare positional form: dovi_tool accepts both, and
    # the named one cannot be mistaken for the value of the option before it.
    demux = [str(dovi_path), "demux", "--el-only", "-i", "-", "--el-out", str(partial)]
    # -bsf:v hevc_mp4toannexb is not optional. Matroska stores HEVC with
    # length-prefixed NAL units and dovi_tool reads Annex-B start codes, so
    # without the filter it is handed a stream it cannot find a single NAL in.
    copy = [
        str(ffmpeg_path), "-v", "error", "-nostdin", "-i", str(source),
        "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-",
    ]  # fmt: skip

    try:
        with subprocess.Popen(  # noqa: S603
            copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        ) as reader:
            assert reader.stdout is not None
            try:
                result = subprocess.run(  # noqa: S603
                    demux,
                    stdin=reader.stdout,
                    capture_output=True,
                    text=True,
                    timeout=_EXTRACT_TIMEOUT,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                )
            finally:
                # Closing our end first stops ffmpeg filling a pipe nobody is
                # reading if dovi_tool exited early.
                reader.stdout.close()
                reader.terminate()
            ffmpeg_error = (reader.stderr.read() if reader.stderr else "") or ""
    except subprocess.TimeoutExpired as exc:
        partial.unlink(missing_ok=True)
        raise DoviError(
            f"extracting the enhancement layer from {source.name} timed out "
            f"after {_EXTRACT_TIMEOUT / 60:.0f} minutes"
        ) from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise DoviError(
            f"could not extract the enhancement layer from {source.name}: {exc}"
        ) from exc

    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        detail = (result.stderr or "").strip().splitlines()
        ffmpeg_detail = ffmpeg_error.strip().splitlines()
        said = detail[-1] if detail else (ffmpeg_detail[-1] if ffmpeg_detail else "no output")
        raise DoviError(f"dovi_tool could not demux {source.name}: {said}")

    if not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise DoviError(
            f"{source.name} reported an enhancement layer but demuxing produced nothing. "
            f"The file may be tagged as profile 7 without carrying a second layer."
        )

    partial.replace(target)
    return target
