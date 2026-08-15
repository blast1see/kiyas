"""Turning sources into PNG files.

Each engine implements the same protocol (:mod:`kiyas.engines.base`) so the
orchestrator never learns which one is running.
"""

from __future__ import annotations

from collections.abc import Mapping

from .base import EngineError, FrameEngine, PreparedSource, RenderSettings

__all__ = [
    "EngineError",
    "FrameEngine",
    "PreparedSource",
    "RenderSettings",
    "available_engines",
    "get_engine",
]


def get_engine(name: str):
    """Return an engine instance by name.

    Imported lazily: constructing the VapourSynth engine imports VapourSynth,
    and asking for the ffmpeg engine must not require it to be installed.
    """
    if name == "vapoursynth":
        from .vapoursynth import VapourSynthEngine

        return VapourSynthEngine()
    if name == "ffmpeg":
        from .ffmpeg import FfmpegEngine

        return FfmpegEngine()
    if name == "mpv":
        from .mpv import MpvEngine

        return MpvEngine()
    raise EngineError(f"unknown engine {name!r}")


def available_engines(tools: Mapping[str, str] | None = None) -> list[str]:
    """Engine names that can run here, best first.

    ``tools`` is the project's ``[tools]`` table. Passing it matters: on a
    machine where mpv is installed somewhere unusual and named in the project
    file, checking PATH alone reports it missing and the run fails with advice
    to install something that is already installed.

    mpv is last for a source comparison: it addresses frames by timestamp
    through a player's seek, which is one more layer of interpretation than
    the other two need. It is not last for a settings comparison -- there it
    is the only engine that can do the job at all.
    """
    usable = []
    for name in ("vapoursynth", "ffmpeg", "mpv"):
        try:
            if get_engine(name).available(tools):
                usable.append(name)
        except Exception:  # noqa: BLE001 - an unusable engine is not an error
            continue
    return usable
