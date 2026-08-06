from __future__ import annotations

import pytest

from kiyas import doctor
from kiyas.doctor import Check, Report, Status


def _ok(name: str) -> Check:
    return Check(name, Status.OK)


def _missing(name: str) -> Check:
    return Check(name, Status.MISSING)


def test_default_engine_prefers_vapoursynth():
    """Order is quality-first, not availability-first.

    VapourSynth is frame exact and tonemaps through libplacebo with real
    metadata; ffmpeg is the fallback that always works; mpv is last because it
    seeks to keyframes, which can desync two sources against each other.
    """
    report = Report(engines=[_ok("vapoursynth"), _ok("ffmpeg"), _ok("mpv")])

    assert report.default_engine == "vapoursynth"


def test_default_engine_falls_back_to_ffmpeg():
    report = Report(engines=[_missing("vapoursynth"), _ok("ffmpeg"), _ok("mpv")])

    assert report.default_engine == "ffmpeg"


def test_default_engine_uses_mpv_only_as_a_last_resort():
    report = Report(engines=[_missing("vapoursynth"), _missing("ffmpeg"), _ok("mpv")])

    assert report.default_engine == "mpv"


def test_partial_engine_still_counts_as_usable():
    """A VapourSynth missing one optional plugin can still make comparisons."""
    report = Report(engines=[Check("vapoursynth", Status.PARTIAL), _missing("ffmpeg")])

    assert report.default_engine == "vapoursynth"


def test_no_engine_available():
    report = Report(engines=[_missing("vapoursynth"), _missing("ffmpeg"), _missing("mpv")])

    assert report.default_engine is None
    assert report.usable_engines == []


def test_check_vapoursynth_reports_missing_when_absent(monkeypatch):
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _name: None)

    check = doctor.check_vapoursynth()

    assert check.status is Status.MISSING
    assert check.hint  # must tell the user what to do about it


class _FakeCore:
    """A core whose version accessors have all been renamed away."""

    def __init__(self, banner="VapourSynth Video Processing Library\nCore R99"):
        self._banner = banner

    def __getattr__(self, name):
        raise AttributeError(f"No attribute with the name {name} exists.")

    def __str__(self):
        return self._banner


class _FakeVS:
    __version__ = "R99"


def test_version_probe_survives_a_renamed_accessor():
    """A renamed version attribute must not look like a broken installation.

    The first version of check_vapoursynth() called core.version_string(),
    which does not exist in R78. Because vs.core raises AttributeError for any
    unknown name (it assumes you mistyped a plugin namespace), a fully working
    stack was reported as 'import failed'. The version string is cosmetic and
    is now allowed to degrade instead.
    """
    version = doctor._vapoursynth_version(_FakeVS(), _FakeCore())

    assert version == "R99"


def test_version_probe_falls_back_to_the_banner():
    class _NoDunderVersion:
        pass

    version = doctor._vapoursynth_version(_NoDunderVersion(), _FakeCore())

    assert version == "VapourSynth Video Processing Library"


def test_version_probe_never_raises():
    class _Hostile:
        def __getattr__(self, name):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("boom")

    assert doctor._vapoursynth_version(_Hostile(), _Hostile()) == "version unknown"


@pytest.mark.vapoursynth
def test_real_vapoursynth_is_fully_usable():
    """On a machine where 'kiyas setup' has run, nothing may be missing."""
    if doctor.importlib.util.find_spec("vapoursynth") is None:
        pytest.skip("VapourSynth is not installed")

    check = doctor.check_vapoursynth()

    assert check.status is Status.OK, check.detail


def test_hints_survive_rich_markup(capsys):
    """Square brackets in a hint must reach the user intact.

    rich reads ``[audio]`` as a style tag and drops it, which silently turns
    "pip install kiyas[audio]" into "pip install kiyas" -- an instruction that
    looks right and installs the wrong thing. Found in real output.
    """
    report = Report(
        engines=[_ok("ffmpeg")],
        packages=[Check("numpy (audio)", Status.MISSING, "not installed", doctor._INSTALL_AUDIO)],
    )

    doctor.render(report)

    assert "kiyas[audio]" in capsys.readouterr().out


def test_short_version_drops_the_copyright_tail():
    banner = (
        "ffmpeg version 2026-08-03-git-01a25f74cc Copyright (c) 2000-2026 the FFmpeg developers"
    )

    assert doctor._short_version(banner) == "ffmpeg version 2026-08-03-git-01a25f74cc"


def test_short_version_passes_through_a_plain_version():
    assert doctor._short_version("mpv v0.41.0") == "mpv v0.41.0"
    assert doctor._short_version(None) is None


def test_build_report_produces_all_sections():
    """Smoke test against the real machine; must not raise whatever is installed."""
    report = doctor.build_report()

    assert {c.name for c in report.engines} == {"vapoursynth", "ffmpeg", "mpv"}
    assert report.tools
    assert report.packages


@pytest.mark.integration
def test_render_returns_zero_when_an_engine_exists(capsys):
    report = doctor.build_report()
    if report.default_engine is None:
        pytest.skip("no frame engine on this machine")

    assert doctor.render(report) == 0
    assert capsys.readouterr().out
