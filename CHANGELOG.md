# Changelog

## 0.1.0

First release.

### Comparing files

Point it at two or more releases and it writes frame-matched, tonemapped
screenshots. Frames are chosen for you — spread across the runtime, skipping
logos and credits, nudged onto a B-frame *in every source* because I-frames get
a disproportionate share of the bitrate and flatter the weaker encode, and past
anything essentially black because a black frame compares nothing.

VapourSynth is the default engine and addresses frames by index; ffmpeg is the
fallback and needs nothing installed. `kiyas doctor` says which you have.

### Comparing settings

One file, one frame, rendered several ways: tone-mapping curves, GLSL shaders,
scalers, deband strengths. Six built-in templates, or spell the variants out.
mpv is the only engine that can do this, because that is where the renderer is.

### Comparing audio

A spectrogram, a waveform with clipping marked, an average frequency response
and a specification table per track, plus the offset between them. Bit depth is
measured rather than believed — a 24-bit container holding 16-bit content is
common and nothing in the header says so — as are clipping, silent channels,
and channels carrying identical audio.

### Publishing

Uploads to slow.pics and writes forum markup. Unlisted by default: a comparison
is usually a working document, and putting one on the front page should be a
decision rather than the result of not passing a flag.

### The window

`kiyas gui`. It builds a project, writes it as TOML and calls the same core the
commands call, so everything it can do is reachable from a terminal and what it
saves runs there.

### Your machine

Nothing is installed system-wide: the VapourSynth stack goes into one
virtualenv you can delete. Your mpv configuration is never read, merged or
written — mpv always runs against a profile kiyas owns. Binaries resolve from
absolute PATH entries only, so an `ffmpeg.exe` sitting next to a release is
never the one that runs.

### Known limits

- The packaged build has the ffmpeg and mpv engines. VapourSynth is a stack of
  compiled plugins that installs into a Python environment, so it needs a
  checkout and `bootstrap.ps1`.
- A settings comparison is captured at display size rather than the source's.
  That is what it is comparing: ask mpv for a source-resolution capture instead
  and all four tone curves come back byte-identical and an upscaling shader
  does not fire. Use `fullscreen`, or set `width` for the same size on every
  machine; whatever size came out is reported.
- The offset measurement is a single correlation and assumes the two tracks are
  a constant distance apart. Install
  [AudioSyncTool](https://github.com/blast1see/AudioSyncTool) when drift
  matters.
