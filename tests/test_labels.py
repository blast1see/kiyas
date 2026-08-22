"""The label burnt into a captured frame.

Two engines draw it and they have to say the same thing, because the label is
what somebody reading a published comparison has instead of the project file.
The wording lives in one function for that reason, and most of this file is
about keeping it there.

The rest is about the font. It ships inside the package because ffmpeg's
drawtext needs one by path and there is no portable path -- so an install that
loses it, or a frozen build that leaves it behind, produces a comparison with
no labels and nothing to explain why. That is worth an assertion rather than a
discovery.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiyas import assets
from kiyas.config import Source, Tonemap
from kiyas.engines.base import label_for
from kiyas.engines.ffmpeg import (
    _LABEL_MARGIN_MIN,
    _LABEL_MIN_SIZE,
    _LABEL_SHARE,
    _escape_filter_path,
)


def _source(**overrides) -> Source:
    source = Source(path=Path("x.mkv"), name="UHD Remux")
    for key, value in overrides.items():
        setattr(source, key, value)
    return source


# --------------------------------------------------------------------------
# The wording
# --------------------------------------------------------------------------


def test_an_untouched_source_is_labelled_with_just_its_name():
    assert label_for(_source(), Tonemap.NONE) == "UHD Remux"


def test_everything_done_to_the_picture_is_named_in_one_clause():
    """One bracket rather than three.

    Three of them push the name off a narrow capture, and these are not three
    independent facts -- they are what was done to get this picture.
    """
    label = label_for(_source(luma_fix=True), Tonemap.DOVI, baked_el=True)

    assert label == "UHD Remux (tonemapped dovi, EL baked, luma adjusted)"


def test_a_composed_enhancement_layer_is_stated():
    """A reader cannot see that a residual was composed in; they can be told."""
    assert "EL baked" in label_for(_source(), Tonemap.DOVI, baked_el=True)
    assert "EL baked" not in label_for(_source(), Tonemap.DOVI)


@pytest.mark.parametrize("mode", list(Tonemap))
def test_the_label_always_starts_with_the_name(mode):
    """Whatever else it says, the column has to be identifiable."""
    assert label_for(_source(), mode).startswith("UHD Remux")


# --------------------------------------------------------------------------
# The font
# --------------------------------------------------------------------------


def test_the_bundled_font_is_present_and_is_a_font():
    """A missing font is an unlabelled comparison, and nothing else says so."""
    font = assets.label_font()

    assert font is not None, "the label font is missing from this install"
    assert font.is_file()
    # TrueType, or one of the two other shapes an OpenType file can take.
    assert font.read_bytes()[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO")


def test_the_font_licence_travels_with_it():
    """The licence requires the notice ship alongside the font."""
    font = assets.label_font()

    assert font is not None
    assert (font.parent / "DejaVuSans.LICENSE").is_file()


# --------------------------------------------------------------------------
# Getting a path past ffmpeg's filtergraph parser
# --------------------------------------------------------------------------


def test_a_windows_drive_letter_is_escaped_for_the_filtergraph():
    """``C:/...`` puts a colon where the parser expects the next option.

    Two backslashes, not one: one is eaten by the option-value parser and the
    colon then reaches the filtergraph splitter unprotected. Measured against
    ffmpeg 2026-08 -- the single-backslash form is rejected with "No option
    name near '/Users/...'".
    """
    escaped = _escape_filter_path(Path("C:/Users/Mert/font.ttf"))

    assert escaped == "C\\\\:/Users/Mert/font.ttf"


def test_backslashes_become_forward_slashes_first():
    """So the only backslashes left in the value are the deliberate ones."""
    escaped = _escape_filter_path(Path(r"C:\Users\Mert\font.ttf"))

    assert "\\\\:" in escaped
    assert escaped.count("\\") == 2


# --------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("height", "expected_at_least"),
    [(180, _LABEL_MIN_SIZE), (1080, 23), (2160, 47)],
)
def test_the_label_size_follows_the_frame_with_a_floor(height, expected_at_least):
    """A size legible on a 4K remux is a quarter of a 320x180 test clip.

    The same rule every threshold in kiyas follows: a share of the material,
    with a floor so short or small material still gets something readable.
    """
    size = max(_LABEL_MIN_SIZE, int(height * _LABEL_SHARE))

    assert size >= expected_at_least
    assert size >= _LABEL_MIN_SIZE


def test_the_margin_never_collapses_to_the_edge():
    assert max(_LABEL_MARGIN_MIN, int(90 * 0.012)) == _LABEL_MARGIN_MIN
