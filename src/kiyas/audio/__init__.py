"""Comparing audio tracks.

The same three questions as the picture side -- produce the evidence, label it,
share it -- asked of sound. What "the evidence" means is different, because you
cannot look at audio: a track has to be turned into something with a shape
before two of them can be compared at all.

Four pictures and a table, per track:

- a **spectrogram**, which shows where a lossy encode's low-pass sits and where
  a fake upmix has nothing at all
- a **waveform**, with clipping marked, which shows dynamic range and whether
  the master was crushed
- a **frequency response**, which is the spectrogram's information averaged
  into a curve you can lay over another one
- **bit depth**, measured rather than believed: a 24-bit container holding
  16-bit content is common and the header will not say so
- a **specification table** of everything the file claims

And one measurement between tracks: the **offset**, because a comparison of two
audio tracks that are not aligned measures nothing but the misalignment.

Every heavy import in this package is deferred. `kiyas doctor` has to run on an
environment with no numpy, and it is the command that tells you to install it.
"""

from __future__ import annotations

from .analysis import AnalysisError, AudioAnalysis

__all__ = ["AnalysisError", "AudioAnalysis"]
