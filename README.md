# kiyas

A comparison workbench for video and audio releases: generate frame-matched
screenshots from several sources, analyse audio, and publish the result to
[slow.pics](https://slow.pics) with forum-ready BBCode.

*kıyas* is Turkish for "comparison".

> **Status: usable.** Point it at two or more files and it writes frame-matched,
> tonemapped screenshots and publishes them to slow.pics. mpv integration,
> audio analysis and the GUI are still to come. See [Roadmap](#roadmap).

---

## Why another one

Two workflows that look different turn out to be the same problem:

**Source comparison** — several releases of the same title, one frame, side by
side. Normally done by editing a VapourSynth script by hand and opening a
previewer.

**Settings comparison** — *one* source, one frame, rendered several different
ways: tone-mapping curves (`spline` vs `bt.2446a` vs `st2094-40`), upscaling
shaders, scalers, deband strengths. This is how you decide what your player
config should actually be, and it cannot be done in VapourSynth at all —
GLSL shaders and libplacebo curves only exist inside mpv.

Both need the same three things: produce the frames, label them, share them.
kiyas does those three things for both.

## Design constraints

**Nothing is installed system-wide.** Since VapourSynth R74 the entire stack is
pip-installable, and every plugin wheel drops its binary into
`<site-packages>/vapoursynth/plugins`. kiyas builds one virtualenv and puts
everything inside it. Deleting `.venv` undoes the installation completely.

The one file written outside the project is
`%APPDATA%/vapoursynth/vapoursynth.toml`, which `vapoursynth config` needs so
VSScript can find the interpreter. It is additive and keyed by this
environment's own `vsscript` path, so other VapourSynth installations are
untouched. kiyas never runs `vapoursynth register-install`,
`register-legacy-install` or `register-vfw` — those write to the registry and
exist for people who want one blessed system-wide install, which is the
opposite of what this tool is for. A test asserts those strings do not appear
in the source.

**Your mpv config is never touched.** mpv is always invoked with
`--config-dir` pointing at a directory kiyas owns. When that flag is set mpv
reads only that directory; `%APPDATA%/mpv` is not read, not merged and not
written.

**The GUI has no privileges.** It writes a project TOML and calls the same core
the CLI calls. Anything the GUI can do is reachable from the command line, the
project file is shareable and diffable, and the core stays testable without a
display.

**Binaries resolve from absolute PATH entries only.** kiyas usually runs with
the working directory set to wherever the media lives, so an `ffmpeg.exe`
dropped next to a release must never be executed.

## Requirements

- Windows, Linux or macOS
- Python 3.12+ (3.13 recommended)
- [FFmpeg](https://ffmpeg.org/) on PATH — a full build, for `libplacebo`,
  `zscale`, `showspectrumpic` and `showwavespic`
- [mpv](https://mpv.io/) on PATH — only for settings comparisons and the frame
  picker
- Optional: [MediaInfo](https://mediaarea.net/en/MediaInfo) for detailed audio
  metadata, [dovi_tool](https://github.com/quietvoid/dovi_tool) for Dolby
  Vision enhancement layers

## Install

```powershell
git clone https://github.com/blast1see/kiyas
cd kiyas
.\bootstrap.ps1
```

That creates `.venv`, installs kiyas, installs the VapourSynth stack into the
virtualenv and runs the environment check. To skip the ~400 MB VapourSynth
download and use the ffmpeg engine instead:

```powershell
.\bootstrap.ps1 -SkipVapourSynth
```

On Linux and macOS, or if you prefer to do it by hand:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,audio]"
kiyas setup      # installs the VapourSynth stack into this venv
kiyas doctor     # reports what is available
```

Optional extras:

```bash
pip install -e ".[gui]"                                       # desktop interface
pip install git+https://github.com/blast1see/AudioSyncTool    # accurate offset measurement
```

## Usage

```
kiyas doctor              # what engines and tools are available, and what is missing
kiyas setup               # install the VapourSynth stack into the current environment
kiyas init project.toml   # write a starter project file
kiyas run project.toml    # produce the comparison
kiyas publish out/        # upload it to slow.pics
```

A project is a TOML file. Six sources with individual crops, trims and
tonemapping do not fit on a command line, and the description is worth keeping:
it is what you edit when the sync turns out to be a frame off, and what you
hand to someone else so they can reproduce the comparison exactly.

```toml
title = "1917 (2019)"
engine = "auto"           # auto | vapoursynth | ffmpeg

[frames]
method = "count"          # count | interval | manual
count = 12
skip_start = "5%"         # skip logos and black leader
skip_end = "10%"          # skip credits
b_frames_only = true      # I-frames get more bitrate and flatter a weak encode
skip_dark = true          # a black frame compares nothing

[[source]]
path = "1917.2160p.UHD.BluRay.REMUX.DV.HDR.mkv"
name = "UHD REMUX"
tonemap = "auto"          # auto | hdr10 | hdr10plus | dovi | none

[[source]]
path = "1917.1080p.BluRay.x264.mkv"
name = "1080p BluRay"
trim = 24                 # frames to drop from the start, to line it up
# crop = [0, 0, 140, 140] # left, right, top, bottom
# resize = [1920, 1080]

[output]
directory = "out"
```

Output is one directory per source, each holding PNGs named after the frame
number, plus `kiyas-manifest.json` recording exactly what was produced.

Two rules are on by default and worth knowing about. **B-frames only**: I-frames
land in different places in every encode and get a disproportionate share of the
bitrate, so comparing them flatters the weaker encode — kiyas nudges each
selected position forward until the frame is a B-frame *in every source*.
**Skip dark**: a frame that is essentially black compares nothing. Brightness is
measured over the centre of the picture so letterbox bars do not skew it.

## Publishing

```
kiyas publish out/                  # unlisted, PNG preserved
kiyas publish out/ --public         # listed on the site
kiyas publish out/ --remove-after 7 # let slow.pics delete it after a week
kiyas run project.toml --publish    # do both in one go
```

Comparisons are **unlisted by default**. A comparison is usually a working
document, and putting one on the site's front page should be a decision rather
than something that happens because you did not pass a flag.

Every image is hashed before upload and the digests go up with the collection
metadata, so slow.pics can say which ones it already holds. Re-running a
publish that died halfway only sends what is missing.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 0 | Environment setup, diagnostics, binary resolution | **done** |
| 1 | Source model, project TOML, frame selection, VapourSynth + ffmpeg engines, tonemapping | **done** |
| 2 | slow.pics upload, forum markup | **done** |
| 3 | mpv layer: portable config dir, frame picker, settings comparison, side-by-side playback | planned |
| 4 | Audio: spectrograms, waveforms, frequency response, bit depth, offset, metadata table | planned |
| 5 | PySide6 desktop interface | planned |
| 6 | Packaging and release | planned |

## Credits

kiyas would not exist without prior work by other people:

- **Solarios** — whose *Install VapourSynth and run screenshot comparisons*
  guide documented the pip-installable VapourSynth stack and the B-frame
  selection rule kiyas uses.
- **afunnylookingsquash** — [Squash-P2P-Scriptorium](https://github.com/9Oc/Squash-P2P-Scriptorium),
  whose `multi_comps.vpy` defines the per-source parameter model and the order
  operations have to be applied in, and whose audio scripts are the model for
  kiyas' audio analysis.
- **[Jaded Encoding Thaumaturgy](https://github.com/Jaded-Encoding-Thaumaturgy)** —
  `vsview`, `vstools`, and the plugin ecosystem.
- **[awsmfunc](https://github.com/OpusGang/awsmfunc)** — `FrameInfo`, `fixlvls`,
  `MapDolbyVision`.
- **[Slowpoke Pics](https://slow.pics)** — for hosting comparisons for free.

## License

GPL-3.0. See [LICENSE](LICENSE).
