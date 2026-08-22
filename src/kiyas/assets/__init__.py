"""Files kiyas ships alongside its code.

There is one, and it is a font. ffmpeg's ``drawtext`` filter needs a font file
by path, and there is no path that exists on Windows, Linux and macOS alike --
which is why the ffmpeg engine produced unlabelled screenshots for as long as
it did. Looking one up on the machine would make a comparison labelled here and
unlabelled there, so the font travels with the code.

DejaVu Sans, unmodified, under the Bitstream Vera and Arev licences (see
``DejaVuSans.LICENSE``, which the licence requires travel with it). Chosen over
a smaller face for its Latin, Greek and Cyrillic coverage: source names are free
text and get written in whatever language the person doing the comparison
thinks in. It is not subsetted -- subsetting is modification, which trips the
licence's rename clause, to save a few hundred kilobytes.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FONT_NAME = "DejaVuSans.ttf"


def label_font() -> Path | None:
    """The bundled font, or ``None`` if this install does not have it.

    ``None`` rather than an exception: a missing font means an unlabelled
    screenshot, which is what the ffmpeg engine produced until now, and not a
    reason to refuse to produce a comparison at all. The engine reports it as
    an assumption and ``doctor`` reports it as a partial capability.

    A plain path rather than ``importlib.resources.as_file`` because this is
    handed to a subprocess: ``as_file`` can return a temporary extraction whose
    lifetime ends with its context manager, and ffmpeg has to be able to open
    the file after that manager has closed.
    """
    roots = [Path(__file__).resolve().parent]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen is not None:
        roots.insert(0, Path(frozen) / "kiyas" / "assets")

    for root in roots:
        candidate = root / _FONT_NAME
        if candidate.is_file():
            return candidate
    return None
