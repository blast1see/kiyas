"""Turning sources into PNG files.

Each engine implements the same protocol (:mod:`kiyas.engines.base`) so the
orchestrator never learns which one is running.
"""

from __future__ import annotations

from .base import EngineError, FrameEngine, PreparedSource

__all__ = ["EngineError", "FrameEngine", "PreparedSource", "get_engine", "available_engines"]


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
    raise EngineError(f"unknown engine {name!r}")


def available_engines() -> list[str]:
    """Engine names that can run here, best first."""
    usable = []
    for name in ("vapoursynth", "ffmpeg"):
        try:
            if get_engine(name).available():
                usable.append(name)
        except Exception:  # noqa: BLE001 - an unusable engine is not an error
            continue
    return usable
