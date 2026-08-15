# kiyas

A comparison workbench for video and audio releases: generate frame-matched
screenshots from several sources, analyse audio, and publish the result to
[slow.pics](https://slow.pics) with forum-ready BBCode.

*kıyas* is Turkish for "comparison".

> **Status: usable.** Point it at two or more files and it writes frame-matched,
> tonemapped screenshots and publishes them to slow.pics. Point it at one file
> and a list of player settings and it renders that frame every way you asked
> for. Point it at audio tracks and it measures them. There is a window if you
> want one. See [Roadmap](#roadmap).

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
written. Project files cannot set the options that would undo this. Shader
files you name are read where they live, and never copied or modified.

**The GUI has no privileges.** It builds a project, writes it as TOML, and
calls the same core the CLI calls. Anything the window can do is reachable from
the command line, the project file is shareable and diffable, and the core stays
testable without a display. It also validates by writing the project out and
reading it back through the ordinary parser, so it cannot accept something
`kiyas run` would refuse -- and the message it shows you is that parser's.

**Binaries resolve from absolute PATH entries only.** kiyas usually runs with
the working directory set to wherever the media lives, so an `ffmpeg.exe`
dropped next to a release must never be executed.

## Requirements

- Windows, Linux or macOS
- Python 3.12+ (3.13 recommended)
- [FFmpeg](https://ffmpeg.org/) on PATH — a full build, for `libplacebo`,
  `zscale` and `showspectrumpic`
- [mpv](https://mpv.io/) 0.36+ on PATH, built with libplacebo — for settings
  comparisons and the frame picker. Not needed to compare files against each
  other. If it lives somewhere unusual, name it under `[tools]` in the project
  file instead of putting it on PATH
- Optional: [MediaInfo](https://mediaarea.net/en/MediaInfo) for detailed audio
  metadata, [dovi_tool](https://github.com/quietvoid/dovi_tool) for Dolby
  Vision enhancement layers

## Install

**A packaged build**, from [releases](https://github.com/blast1see/kiyas/releases):
unzip it and run `kiyas-gui.exe` for the window or `kiyas.exe` for the command
line. No Python needed. It has the ffmpeg and mpv engines — VapourSynth is a
stack of compiled plugins that installs into a Python environment, so that one
needs a checkout.

**A checkout**, for the VapourSynth engine and for working on it:

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
kiyas pick film.mkv       # choose frames by watching, in mpv
kiyas templates           # list the built-in settings comparisons
kiyas audio a.mkv b.mkv   # compare audio tracks
kiyas gui                 # the same things, in a window
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

## Settings comparisons

The other axis: *one* file, one frame, rendered several different ways. This is
how you decide what your player config should be, and mpv is the only engine
that can do it — tone-mapping curves and GLSL shaders live inside a renderer.

```powershell
kiyas init tonemap.toml --settings
kiyas templates            # what the built-in sets contain
kiyas run tonemap.toml
```

```toml
title = "1917 — tone mapping"
mode = "settings"

[[source]]
path = "1917.2160p.UHD.BluRay.REMUX.DV.HDR.mkv"
name = "1917 UHD"

[frames]
method = "manual"
manual = [36869, 67793]   # from `kiyas pick`

[settings]
template = "tonemap"      # tonemap | gamut | scalers | dscale | deband | shaders
# width = 1920            # capture width; the height follows the source aspect
# fullscreen = true       # capture at the full screen resolution
# base = { target-peak = 203 }   # applied to every variant

# Or spell the variants out. The name becomes the column label.
[[variant]]
name = "ArtCNN C4F32"
options = { glsl-shaders = "~/mpv/shaders/ArtCNN_C4F32.glsl" }
```

Shader files are read where they are. kiyas never copies, edits or imports
anything from your player configuration.

**About the capture size.** A settings comparison is captured at your *display*
size, not the source's — 2474x1392 on a 2560x1440 screen, or exactly 2560x1440
with `fullscreen`. A source comparison is not: that one is always the source's
own resolution, and no display is involved at any point.

The difference is not an oversight in one of them. What a settings comparison
compares only exists on a display:

- A tone-mapping curve maps HDR *to a display*. Ask mpv for a source-resolution
  capture and it writes the frame in its own colourspace, where no mapping has
  happened — measured, and all four curves come back byte-identical.
- An upscaling shader runs *because* the display is larger than the source.
  ArtCNN carries its own condition to that effect: at source resolution it does
  not fire, and the capture is byte-identical to having no shader at all.

So "the same comparison at 4K" is not a thing being withheld; for a display
transform it is not a question with an answer. Set `width` explicitly if you
want the same size on every machine — the default follows the screen, and the
size that came out is reported after every run.

## Audio

```
kiyas audio original.mkv dubbed.mkv
kiyas audio a.mkv b.mkv --track 1        # a different audio stream from each file
kiyas audio a.mkv b.mkv --publish        # upload it like any other comparison
```

Each track gets a spectrogram, a waveform with clipping marked, and an average
frequency response — laid out identically so they can be flipped between — plus
a specification table in Markdown and BBCode, and the offset between the tracks.

What it measures rather than reads off the header:

- **Real bit depth.** A 24-bit container holding 16-bit content is common and
  nothing in the header says so. Refused for lossy codecs, where the answer
  would be a measurement of the decoder rather than the file.
- **Clipping**, counted as runs of consecutive full-scale samples. One sample at
  full scale happens in any loud master; three in a row does not.
- **Silent channels**, which is what a fake 5.1 upmix looks like from the inside.
- **Identical channels**, which is what a mono dub in a stereo container looks
  like — and what a frequency-response plot cannot show you, because the two
  curves lie exactly on top of each other.
- **The offset**, because comparing two tracks that are not aligned measures the
  misalignment and nothing else. `+250 ms` means the second track plays *later*.
  [AudioSyncTool](https://github.com/blast1see/AudioSyncTool) is used when it is
  installed and its drift fit is wanted; otherwise a single GCC-PHAT
  correlation, with the peak-to-floor ratio reported so a weak match is visible
  as one.

## The window

```
kiyas gui                     # empty, ready for files dropped onto it
kiyas gui project.toml        # open a project
kiyas gui a.mkv b.mkv         # start from these files
pip install -e ".[gui]"       # if PySide6 is not installed yet
```

Drop files on it, pick what kind of comparison it is, press Run. It does the
same three things the commands do -- source comparisons, settings comparisons,
audio -- and saves what you set up as a project file you can run from a
terminal, hand to someone else, or keep in a repository.

The window never decides anything the command line cannot. What is on screen
becomes a project, the project is written and read back through the same
parser, and the sentence under the file list is whatever that parser said.

## Choosing frames by hand

Automatic selection spreads captures evenly and avoids black frames, which is
the right default and no help at all when the shot that settles the argument is
a specific one.

```
kiyas pick film.mkv
```

That opens the file in mpv with normal playback controls. Press `s` to mark the
frame you are on, `u` to undo, `q` to finish. It prints a `[frames]` block ready
to paste into a project file — it does not edit your project, because guessing
whether to add or replace would eventually destroy work.

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
| 3 | mpv layer: portable config dir, frame picker, settings comparison | **done** |
| 4 | Audio: spectrograms, waveforms, frequency response, bit depth, offset, metadata table | **done** |
| 5 | PySide6 desktop interface | **done** |
| 6 | Packaging, CI, release | **done** |

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
