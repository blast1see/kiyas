# Working on kiyas

Notes for anyone — human or agent — picking this codebase up.

## What the tool does

It produces frame-matched screenshots from video sources, analyses audio, and
publishes both to slow.pics with forum-ready BBCode. Two comparison axes, one
core:

| Mode | Input | Engine | Question it answers |
|---|---|---|---|
| Source | N files, one frame | VapourSynth | Which release is better? |
| Settings | 1 file, one frame, N render configs | mpv | How should my player be configured? |
| Audio | N tracks | ffmpeg | Which of these is the real master? |

The settings mode is not a bonus feature. Tone-mapping curves and GLSL shaders
only exist inside mpv, so VapourSynth cannot answer that question at all.

All three write the same shape of output -- a directory per column, the same
images in the same order, a manifest -- so publishing is written once.

## Language

**English only.** Code, comments, commit messages, and all user-facing output.
An earlier revision had a bilingual `i18n.py`; it was removed deliberately, so
do not reintroduce message-key indirection.

## Three invariants

Breaking any of these is a bug even if every test passes.

**1. Nothing is installed system-wide.** The VapourSynth stack goes into the
virtualenv and nowhere else. `kiyas setup` runs `vapoursynth config`, which
writes one additive file (`%APPDATA%/vapoursynth/vapoursynth.toml`) keyed by
this environment's own `vsscript` path. It must never run
`vapoursynth register-install`, `register-legacy-install` or `register-vfw` —
those write to `HKCU\SOFTWARE\VapourSynth` and register a system-wide VFW
provider. `test_configure_never_registers_system_wide` parses the module's AST
and fails if those strings become reachable from code.

**2. The user's mpv config is never touched.** mpv is always invoked with
`--config-dir` pointing at a directory kiyas owns. Verified behaviour: with
that flag set, mpv reads *only* that directory — `%APPDATA%/mpv` is not read,
not merged, not written. Shader files under the user's config may be *read* by
path; they are never copied or modified.

**3. Binaries resolve from absolute PATH entries only.** kiyas usually runs
with the working directory set to wherever the media lives, so an `ffmpeg.exe`
dropped next to a release must never execute. `shutil.which` is not used — its
current-directory behaviour varies by platform and Python version. See
`media/binaries.py`; `test_cwd_is_never_searched` is the assertion that
matters.

## Where the logic lives

```
config.py           the project TOML: model, validation, error messages
media/binaries.py   locating and probing external executables
media/probe.py      what a file is, via ffprobe
frames/selector.py  which frames to capture (arithmetic only, no decoding)
frames/align.py     how far apart two sources are (arithmetic only, no decoding)
media/dovi.py       extracting a Dolby Vision enhancement layer, once
engines/base.py     the protocol every engine implements
engines/*.py        VapourSynth (default), ffmpeg (fallback), mpv (settings)
mpvctl/ipc.py       JSON IPC: named pipe on Windows, socket elsewhere
mpvctl/profile.py   the mpv configuration kiyas owns
mpvctl/session.py   one mpv process, driven frame by frame
mpvctl/variants.py  render variants and the built-in templates
mpvctl/picker.py    choosing frames by watching the film
audio/probe.py      what a track claims to be (ffprobe, MediaInfo)
audio/analysis.py   what it actually contains, in one streaming pass
audio/visuals.py    spectrogram, waveform, frequency response
audio/sync.py       how far apart two tracks are
audio/table.py      the specification table
audio/run.py        orchestration for an audio comparison
gui/window.py       the window: builds a project, calls the core
gui/worker.py       running the core off the UI thread
run.py              orchestration: config in, PNGs and a manifest out
doctor.py           what this machine can do, and what is missing
setup_env.py        installing the VapourSynth stack into this venv
cli.py              argument parsing; imports subcommands lazily
```

Two boundaries are worth keeping:

**`frames/selector.py` never decodes anything.** It does the arithmetic and
takes a predicate for the rest, so the B-frame and dark-frame rules are tested
without media and neither engine reimplements the search. `frames/align.py`
holds the same line for the same reason: it takes callables that hand back luma
thumbnails, so the score and the search are pinned down on content whose answer
is known rather than on a feature film.

**`run.py` names no engine.** It picks one, asks it to prepare each source, and
writes what comes back. Anything engine-specific that leaks up to here is in
the wrong place. The one exception is `choose_engine`, whose entire job is
naming engines: a settings comparison resolves to mpv there, because no other
engine can produce one.

**The two modes meet in `run.columns()`.** A source comparison is N files and
no render settings; a settings comparison is one file and N variants. Below
that function nothing knows which it is looking at, which is why the frame
selection, capture, manifest and publishing paths are not written twice.

## The order transformations are applied in

Defined once, in `config.PROCESSING_ORDER`, and it is not arbitrary:

- **trim before resize and crop** — trim values are frame indices the user read
  off a previewer showing the untransformed clip.
- **tonemap after crop** — peak-brightness detection averages over the frame,
  and letterbox bars drag the measured peak down. Visible on scope material.

A new transformation that has no place in that list will be applied wherever
the engine happens to put it, and the only symptom is a subtly wrong
screenshot.

`cli.py` must not import VapourSynth, matplotlib or PySide6 at module level.
`kiyas doctor` has to run on an environment where none of them are installed —
a diagnostic that needs its own dependencies is worthless.
`test_importing_cli_does_not_pull_in_heavy_dependencies` enforces this.

## The GUI has no privileges

The window builds a project, writes it as TOML, and calls the same core the CLI
calls. If a feature is only reachable through the window, that is a design
error, and the way to tell is that you cannot describe it in a project file.
Consequences worth keeping: project files are shareable and diffable, the core
is testable without a display, and nothing has to be implemented twice.

**It validates by round-tripping.** `build_project` writes the draft with
`config.dumps` and reads it back with `config.parse`. Every rule in `config.py`
therefore applies in the window for free, including the ones it has no widget
for, and the message under the file list is the parser's own sentence. A second
set of checks living in the GUI would drift out of step with the first, and the
drift would show up as the window accepting something `kiyas run` rejects.

- **`config.dumps` is the other half of `config.parse`,** and a round-trip test
  holds them together. Paths go out as TOML *literal* strings: a Windows path
  in a basic string needs every backslash doubled, and a project file people
  edit by hand should not look like that.

- **A QThread whose end is connected to `quit` with a queued connection only
  stops while the UI event loop is spinning.** `quit` belongs to the main
  thread, so the call sits in a queue nobody is reading exactly when the main
  thread is waiting -- which is what closing the window mid-run does. It
  deadlocked until the timeout: five seconds of frozen window on the way out,
  and twenty seconds of test suite. The connection is direct;
  `test_waiting_for_a_finished_task_returns_at_once` times it, because the
  symptom of a regression is slowness and slow tests get shrugged at.

- **Qt signals fire mid-construction.** `rowsInserted` arrives before the row's
  cell widgets exist, so anything reading the table from a signal handler sees
  a half-built row. Adding a row blocks signals until it is complete, and the
  status line swallows everything: a label that fails to update is a much
  smaller problem than a window that throws while somebody is typing.

## Testing

Run before claiming anything works:

```bash
python -m ruff check . && python -m ruff format --check .
python -m pytest -q                       # unit tests, no media needed
python -m pytest -m integration           # needs FFmpeg
python -m pytest -m vapoursynth           # needs 'kiyas setup' to have run
python -m pytest -m mpv                   # needs mpv and a display
QT_QPA_PLATFORM=offscreen python -m pytest -m gui   # needs PySide6, not a display
```

Markers: `integration`, `vapoursynth`, `mpv`, `gui`, `live`. `live` is reserved
for tests that talk to slow.pics for real, and no test carries it yet —
publishing is checked by hand, against the live site, because the interesting
answers there are the ones the site gives and not the ones a fake session was
told to give. Both Cloudflare fixes in 0.1.1 came out of doing that; neither
was visible from the suite.

**Unit tests are necessary and not sufficient.** The version-probe bug below
passed every unit test. Frame accuracy, tonemapping and sync have to be checked
against real material: feature-length remuxes, kept outside this repository
because they are tens of gigabytes each. Any varied set will do; what the set
has to be is varied, for the reason below.

**Nine real files, deliberately unalike, are the sweep that finds this class
of bug.** A Dolby Vision profile 7 remux, a profile 8 hybrid, a 4K WEB-DL with
DV, four 1080p AVC remuxes, a WEB-DL with no B-frames, and an anime episode.
The first run produced eight comparisons out of nine and several of those
returned fewer frames than were asked for; every one of the four causes was a
rule that was right for the material it had been written against. Synthetic
clips cannot find these, because a synthetic clip is whatever you made it.

**The settings engine has been checked against captures made by hand.** Four
tone-mapping curves on two scenes of a 4K Dolby Vision remux, against the same
frames captured in mpv by a person: 54.3 to 56.8 dB against the matching curve,
and 37.3 to 39.1 dB against a *different* curve of the same frame. The gap is
the point — it says kiyas reproduced each specific curve rather than merely
producing a tonemapped picture. The residual is dither: those captures are
16-bit and kiyas writes 8-bit.

Worth knowing if this is ever repeated: finding the frame is the hard part. A
coarse index of keyframe thumbnails locates the *scene* and cannot locate the
frame, because in a continuous take a second either side looks identical at
48x27 — it was 58 seconds out, and the wrong frame read as a rendering
difference. Decoding a two-minute window at full frame rate and rank-correlating
every frame found it in 23 seconds. Rank correlation rather than difference,
because a tone curve is monotonic: it moves every value and reorders none.

## Traps already paid for

- **VapourSynth's version accessor keeps moving.** `core.version_string()`
  never existed; `core.version()` and `core.version_number()` are deprecated in
  R78 in favour of `core.core_version`. Worse, `vs.core` raises `AttributeError`
  for *any* unknown name because it assumes you mistyped a plugin namespace —
  so probing the wrong accessor makes a perfectly working install report as
  "import failed". Version strings are cosmetic and `_vapoursynth_version()`
  is allowed to degrade to "version unknown" instead of failing the check.

- **rich eats square brackets.** `"pip install kiyas[audio]"` renders as
  `pip install kiyas` because `[audio]` parses as a style tag — turning a hint
  into a wrong instruction. Anything user-supplied or bracket-bearing goes
  through `rich.markup.escape`.

- **mpv hangs on `--frames=1` if the user's config has `keep-open=yes`.** Any
  subprocess call to mpv needs an explicit timeout and, where relevant,
  `--keep-open=no`. `_PROBE_TIMEOUT` in `binaries.py` exists because of this.

- **PowerShell's `-match` on an array filters instead of returning a boolean.**
  `if ($lines -notmatch "x")` is truthy whenever *any* line does not match,
  which silently passes. `bootstrap.ps1` joins to a single string first.

- **Hatchling rejects direct URL references in extras.** AudioSyncTool is not
  on PyPI, so it cannot be an extra; it stays a documented manual install and
  a soft dependency at runtime.

- **`FrameInfo` burns the frame number and source name into the picture.**
  Any test that compares pixels must pass `overlay=False`. A test asserting
  that tonemapping changed the image passed for a while without the tonemapper
  doing anything, because the two captures had different labels in the corner.
  If a pixel comparison passes suspiciously easily, check the overlay first.
  The converse also bites: turning the overlay off is such a reflex that the
  mpv guard ran only without a caption, and the caption had a bug of its own.
  Where the label can affect the result, test both.

- **L-SMASH writes indexing progress straight to file descriptor 2.** Not
  through VapourSynth's logger, so `add_log_handler` does not see it and
  replacing `sys.stderr` does nothing — it is a C library writing to the fd. It
  has no option to turn it off either. `_capture_indexing` redirects the
  descriptor and keeps whatever is *not* progress, because a real indexer
  complaint is exactly what explains a wrong-looking result. BestSource is
  better behaved and takes `showprogress=0`.

- **Indexer caches land next to the media by default.** A `.lwi` file appears
  beside every source, which is right for reuse and wrong for a read-only or
  shared library. `[output] index_dir` moves them.

- **Plenty of real remuxes carry no colour tags at all.** VapourSynth then
  refuses to convert to RGB with "no path between colorspaces". Seen on a
  retail Blu-ray remux where `color_primaries`, `color_transfer` and
  `color_space` were all absent. `_ensure_colour_props` fills them from the
  standard convention (SD is BT.601, HD and above BT.709) and *reports the
  assumption*, because a guess that silently changes the colours of a
  comparison is worse than no comparison.

- **Letterbox bars defeat mean-luma dark detection.** A 2.39:1 film in a 16:9
  container is about a quarter black, which pulls the mean under the threshold
  on well-lit shots. Brightness is measured over the centre `ACTIVE_AREA` of
  the frame, and both engines must use the same value or the same project
  picks different frames depending on which engine ran.

- **Absolute thresholds cannot serve both a test clip and a feature film.**
  This has now been got wrong three times, so treat it as a rule: **any
  threshold measured in seconds or frames must be a fraction of the material,
  with a floor.** A four-second test clip and a three-hour remux differ by four
  orders of magnitude, and a constant tuned for one is nonsense for the other.
  - The source-length warning is 1% of the longest source, floor one second.
    A fixed sixty seconds never fired on short clips and fired on every
    alternate cut of a film.
  - The ffmpeg tail margin is 0.2% of the frame count, floor two frames. A
    fixed ten seconds zeroed out every four-second clip in the test suite.
  - The nudge budget is 25% of the gap to the next requested frame, floor 48.
    A fixed 48 is two seconds: enough to find a B-frame, useless for escaping
    a night scene, and a dark film returned one frame of the three asked for.
    Watch the second-order effect too -- a bigger budget meant thousands of
    ffmpeg launches per position, so past the first two seconds the search
    samples every 24 frames. Brightness is a property of a shot; the picture
    type is not.

- **A rule that rejects everything is a bug with a message attached.** An
  encode with no B-frames at all is ordinary -- a WEB-DL made for low latency
  can be entirely I and P -- and "must be a B-frame" then threw away every
  frame in the file and told the user to guess between two knobs. What the rule
  is *for* is avoiding I-frames, so that is the fallback. Before adding a rule,
  ask what it does to material that cannot satisfy it.

- **DTS is not one codec and ffprobe will not tell you which one you have.**
  Every variant reports `codec_name=dts`; `DTS-HD MA` is lossless and lives in
  the *profile*. Going by the name treats the most common lossless track on a
  Blu-ray as lossy and refuses to measure its bit depth, which is the file
  people want that number for.

- **ffprobe's frame count overshoots, and the two engines disagree because of
  it.** VapourSynth reports `clip.num_frames`, which is exact. ffmpeg has only
  `duration x fps`, measured 125 frames (0.08%) high on a retail remux. Two
  consequences: a capture near the end of a file can seek past EOF and fail
  with nothing but an ffmpeg error, which the tail margin exists to prevent;
  and the *same project file* selects slightly different frames depending on
  the engine. Within one run only one engine is used, so a comparison is never
  internally inconsistent -- but a run is only reproducible against itself.

- **ffmpeg's `drawtext` text option cannot be escaped reliably, so the text
  goes in a file.** Three parsers read it in turn and they disagree. Measured
  against ffmpeg 2026-08, one character at a time, argument passed straight to
  the process with no shell: `:` needs *two* backslashes, `,[];` need one, and
  an apostrophe cannot be made to work at all once any escaped colon follows
  it -- so "Director's Cut" plus "Picture type: B" in one label has no working
  form. `textfile=` has no such rules. Only the path still needs escaping, and
  a Windows drive letter needs `C\:/...` there for the same reason.

- **Dolby Vision profile 7 is composed by vs-placebo, not by awsmfunc.**
  `awsmfunc` 1.3.5 is what PyPI has and its `MapDolbyVision` hard-fails without
  `vs-nlq`, which has no distribution under any name and would need a Rust
  toolchain -- the one thing this stack promises it never needs. vs-placebo
  2.0.4 has `sh_dovi_compose_nlq` and `placebo.Tonemap` takes `dovi_el`, so it
  is one keyword on a call that was already being made. Do not reintroduce the
  awsmfunc route.

- **The enhancement layer is a quarter of the base layer's size, and must not
  be resized.** Measured on a UHD disc remux: 1920x1080 against 3840x2160.
  libplacebo scales it in the shader. `MapDolbyVision` point-resizes it because
  `vsnlq.MapNLQ` demands matching dimensions; copying that here would be
  carrying a workaround for a plugin that is not being used.

- **`DolbyVisionRPU` survives the depth conversion**, measured at 221 bytes
  before and after `resize.Bicubic(format=YUV444P16)` on both layers. Worth
  knowing because vs-placebo raises "Clips are missing `DolbyVisionRPU` prop"
  at the first `get_frame` -- which is *after* a forty-minute extraction.

- **Numbers from the profile 7 remux this was built against**, so the next
  person can tell whether their file is behaving: 78 GB source, extraction
  10.3 minutes, layer 4.64 GB (5.9%), and composed output differing from
  base-layer-only by 61.2 dB PSNR. Real, and far too subtle to find by looking.

- **Two sources that are pixel-identical still score about 36 dB against each
  other if their labels differ.** Measured on a pair whose base layers hash
  identically: the burnt-in name is a few hundred bright pixels on a 3840x1606
  frame and that is enough. This is the same trap as `overlay=False` in pixel
  tests, seen from the outside: a PSNR between two captures is not a
  measurement of the encodes unless the labels match or are off.

- **The wide alignment search happens once, not once per position.** The
  offset between two releases is one number for the whole film, so nine wide
  searches measure the same thing nine times and pay nine times for it: one
  percent of a feature is 2093 frames either side, which is tens of thousands
  of 4K frames decoded per source and minutes of waiting. The first position
  searches wide, the rest confirm within a stride of where it landed -- which
  is what catches a position that happened to fall in a repeated shot. The
  confidence comes from the wide search, because "is this peak distinctive
  against the rest of the film" is a question a narrow window cannot answer.

- **ffmpeg's default pacing silently rewrites a run of frames.** Asking for
  N consecutive frames and getting N frames back does not mean they are the
  ones asked for: without `-fps_mode passthrough` ffmpeg fits the output to a
  constant frame rate by duplicating and dropping, and the result still looks
  contiguous. Measured on a 24fps clip with no filter doing anything unusual:
  one duplicate near the start put **45 of 48** thumbnails one frame late, and
  `default[24]` was `passthrough[23]`. In an alignment that is a systematic
  one-frame bias in every number the module produces. It cancels when both
  sources are affected identically, which is exactly why it would have
  survived a test that only checked the answer.

- **The audio module's peak-to-floor confidence does not transfer to
  pictures.** It was the obvious thing to reuse -- the two measurements answer
  the same question -- and it is worthless here. Its value tracks how
  self-similar the material is, not how good the match is: measured on a real
  4K feature, a *correct* alignment scored 3.5 peak-to-median and two
  completely different films scored **3.1**. On ffmpeg's `testsrc2` a correct
  answer scored 1.1. There is no threshold that separates those. Alignment
  confidence is agreement between sampled positions instead, which is
  content-independent and is something a reader can act on. The same three
  cases, measured again with it: an aligned pair 9 of 9, a deliberately
  mistrimmed pair 8 of 9 (and the offset recovered exactly), two different
  films 1 of 9.

- **Striding a search window only pays when the window is wide.** Below
  `MIN_COARSE_FIELD` candidates it searches at full rate, which a short clip
  can afford precisely because it is short, and striding it would only cost
  precision.

- **A confirming position has to be able to disagree.** Pinning it to a couple
  of frames around the first answer makes agreement unanimous by construction
  and the confidence meaningless. `CONFIRM_REACH` is about four seconds: wide
  enough that a position which does not really agree lands somewhere else.

- **Indexers do not expose the same frame props.** Read out of the shipped
  DLLs: lsmas has `DolbyVisionRPU` but not `HDR10Plus`; BestSource and ffms2
  have both. It costs nothing today because vs-placebo ignores `HDR10Plus`
  anyway -- but if that ever changes, the base layer's indexer preference in
  `_load` is what would gate it, and it tries lsmas first.

## Driving mpv

mpv is a player being used as a renderer, and almost everything below is a
consequence of that.

- **Windows serialises I/O on a synchronous handle, so the IPC client is
  single-threaded.** The natural design -- a reader thread plus commands from
  the caller's thread -- deadlocks after a couple of messages, because a
  pending read blocks a write on the same handle. It is not intermittent and it
  looks exactly like mpv hanging. `ipc.py` asks `PeekNamedPipe` how many bytes
  are waiting and never has two operations outstanding.

- **The seek timestamp for frame N is `N / fps`, not the middle of the frame.**
  mpv's exact seek lands on the first frame whose timestamp is at or after the
  target, so aiming at the middle of frame N's interval gives N+1. The ffmpeg
  engine wants the opposite convention for the same accuracy, which is why the
  two look inconsistent and are both right.

- **The renderer runs behind the player, and the gap grows with resolution.**
  This is the big one. `screenshot window` draws whatever the video output
  currently holds, and after `playback-restart` that is still the *previous*
  frame: 4 wrong captures in 48 at 640x360, and **8 in 8** at 3840x2160. Two
  things make it dangerous. It is silent, and a screenshot of the wrong frame
  in a comparison reads as a difference between the sources. Waiting on
  `video-frame-info` looks like the fix and is not -- it is player-side and has
  already moved. The barrier is `vo-passes`, which the video output writes
  after drawing; `session.py` waits for its `fresh` section to change.

- **Anything that makes mpv redraw satisfies the barrier.** Setting the caption
  does exactly that, so captioning before seeking moved the marker with a
  picture of the old frame and put every capture one frame out -- on real
  material, while the same code passed on a test clip. Captions are applied
  inside `MpvSession.capture`, after the seek, so no caller can get the order
  wrong. Both failures have deterministic unit tests built on a fake mpv whose
  renderer lags on purpose; the integration guard is parametrised over the
  caption because running it only without one is what hid the second bug.

- **A window cannot be larger than the display, and `screenshot subtitles` is
  not the way out.** It does capture at source resolution -- and without the
  display transform, which is most of what a settings comparison compares. All
  four tone curves come back byte-identical there, and ArtCNN does not fire at
  all: the shader's own `//!WHEN` condition says an upscaler has nothing to do
  when the output is the source's size. Both measured. A source comparison is
  the case that owes you the source's resolution, and it delivers it; a
  settings comparison is a picture of a renderer driving a display, so it is
  the display's size. The size that came out is measured and reported rather
  than assumed.

- **`--geometry=WIDTH`, never `WIDTHxHEIGHT`.** Giving both makes mpv letterbox
  a mismatched aspect ratio and the bars land in the screenshot: measured 138
  rows top and bottom on a 2.39:1 source in a 1920x1080 window.

- **`--hidpi-window-scale=no` or the capture size is a lie.** At 150% display
  scaling, `--geometry=640x360` produces a 960x540 framebuffer.

- **Options in a project file are filtered.** `config-dir`, `include`,
  `script`, `profile` and friends are refused, because a shared project file
  that quietly pulled in somebody's whole player configuration would still
  produce plausible-looking screenshots. See `FORBIDDEN_OPTIONS`.

## Audio

An audio comparison writes the same shape of output as a picture one -- a
directory per track, the same images in the same order, a manifest -- so
`kiyas publish` works on it unchanged. The manifest carries `labels` instead of
`frames`, because its rows are analyses and a spectrogram has no timestamp.

- **The decode is one streaming pass, in float.** A two-hour 5.1 track is about
  eight gigabytes of samples. Everything -- peak, RMS, clipping, envelope, bit
  depth, frequency response, channel duplication -- is accumulated chunk by
  chunk from a single ffmpeg pipe. Float rather than 32-bit integers because
  integers *clamp*: the English AC-3 track of a real Blu-ray peaks at **+2.31
  dBFS**, and an integer decode would have hidden that behind a flat 0.0.

- **A pipe hands over partial frames, and dropping them rotates the channels.**
  `read(n)` returns at most n bytes. Discarding the remainder starts the next
  read part-way through a frame, and from there every channel is shifted by
  one -- which does not fail, it silently attributes the left channel's audio
  to the right. It is invisible on mono files, so it survived the first round
  of testing; what gave it away was a 1 kHz tone whose measured spectrum peaked
  at DC. `analysis.analyse` carries the remainder forward.

- **`showspectrumpic=mode=separate` crashes on multichannel audio.** ffmpeg
  N-124864 died with an access violation in roughly two runs in five on a 5.1
  track, at every image size, with and without the legend; `mode=combined` and
  single-channel renders never crashed in any run. kiyas splits the stream and
  renders one channel at a time inside the same graph, so it stays at one
  decode, then lays the tiles out itself.

- **Composing the spectrogram is not decoration.** ffmpeg's legend adds margins
  per channel, so a 5.1 track and a stereo one come out different heights and
  stop being comparable. And the tiles are drawn with the highest frequency in
  the first row, so they go into `imshow` as `origin="upper"` -- flipping them
  puts a 1 kHz tone at 22 kHz, which looks like a perfectly plausible
  spectrogram of something else.

- **The offset's sign is stated and tested.** `offset_ms > 0` means the second
  track plays *later*. Backwards is worse than nothing: it corrects in the
  wrong direction and doubles the error. Checked against a file delayed by a
  known 250 ms, and the fallback's answer on a real pair matched an independent
  GCC-PHAT measurement to 0.1 ms.

- **Bit depth is refused for lossy codecs.** A lossy decoder emits float that
  uses every bit whatever the source was, so "24-bit" there would be a
  measurement of the decoder. Saying "not measurable" is the honest answer.

## Publishing

There are two backends. `publish/slowpics.py` and `publish/comppics.py` share
`publish/result.py` (the one `UploadError` and `UploadResult` both hand back),
`publish/transport.py` (reading a Cloudflare refusal apart from an API error)
and `publish/manifest.py`. The CLI picks one with `--to` and never branches on
the host again after `_SENDERS`.

They are not symmetrical, and the differences below are each a place where
carrying one backend's habits into the other produces a bug that does not look
like one.

### slow.pics

slow.pics has no public API documentation. The request shapes in
`publish/slowpics.py` were read off a working client (`vsview-comp`) and then
checked against the live service; do not change them from first principles.

- **The image grid is transposed between the two sides.** The server returns
  `images[frame][source]`; kiyas holds `sources[source][frame]`. Getting this
  backwards uploads every picture into the wrong cell, and the result does not
  error — it looks like a dramatic difference between the releases. Verified
  live by giving the two sources visibly different file sizes and reading them
  back out of the server's own JSON.

- **A 400 is sometimes a success.** `X-Error-Message: IMAGE_IS_COMPLETE` means
  the hash matched something already stored, so the image is in the collection
  and there is nothing to do. Treating it as an error aborts uploads that
  actually worked. Any other 400 is real and stops everything.

- **Send the hashes first.** They are what let the server say which images it
  already has, so re-running after a partial failure only sends the rest.

- **`/api/collection/{key}` is blocked** (403 at the edge) even though
  `/c/{key}` is not. To inspect a published collection, fetch the page and read
  the `var collection = {...}` blob out of the HTML.

- **Publishing defaults are deliberately timid.** `LINK_ONLY`, no expiry, no
  markup. `run --publish` uses the same conservative set. Anything that pushes
  a comparison further into the world than the person asked for should require
  them to say so.

- **Never test publishing with the user's media.** `tests/` and any live check
  use ffmpeg-generated synthetic images, unlisted, with an expiry. This holds
  for both backends.

### comp.pics

comp.pics is the hosted instance of `thezak48/comps`, which is open source and
publishes an OpenAPI 3.1 document at `/openapi.json` with Swagger UI at
`/api/docs`. The shapes in `publish/comppics.py` were read off that spec and
the server's own source, so unlike slow.pics they can be checked rather than
guessed. Read the spec before changing them.

- **There is no transpose here.** The server takes `row` and `column` as
  separate form fields, so `row` is the frame and `column` is the source and
  the mapping from what kiyas holds is direct. Transposing out of habit,
  because the other backend needs it, would put every picture in the wrong cell
  without erroring — the same failure as above, arrived at from the opposite
  direction. `test_row_is_the_frame_and_column_is_the_source` is the guard.

- **`custom_name` is the column heading.** The site builds its headings from
  the first row's image names, so every cell carries its *source's* name. A
  frame label there would print "0:00:04.170 / 100" above the column and lose
  which release is which.

- **`image_urls` is source-major** — `urls[source * rows + row]` — because that
  is what `bbcode` indexes. Any other order pairs the wrong picture with the
  wrong source in the markup, silently. This is the one backend that fills the
  field in, and the reason `bbcode.render` is reachable at all.

- **The expiry is an enum**, not an integer: 1, 7, 30 or 90 days. Anything else
  is a 422 *after* the comparison has been created, leaving an empty one
  behind, so `snap_expiration` rounds before the first request. There is no
  "never".

- **The public instance does not apply the expiry the JSON API is given.**
  Measured 2026-08-21: two creates carrying `expiration_days: 1` were stored,
  and still read back, as 7. The field is in the spec and the current source
  honours it, so the request is right and that deployment is behind. The client
  keeps sending it and compares the echoed value, reporting the difference
  through `UploadResult.notes` — asking for one day and silently getting seven
  is the kind of thing nobody goes and checks.

- **There is no unlisted mode.** The API listed every comparison on the public
  instance to anyone who asked when this was written. That makes publishing
  there a more public act than the same command against slow.pics, so the CLI
  says so before it sends anything rather than after.

- **The public instance runs behind its own `develop` branch.** It has neither
  the `edit_token` that newer builds issue at creation nor the authorisation
  check that consumes it, so anonymous uploads work today. The client stores
  the token when the server offers one and sends it as `X-Edit-Token`
  regardless: harmless against the current instance, and required the day that
  branch ships. Do not make it conditional on a version.

- **`MAX_ROWS` is checked here, not left to the server.** The server clamps
  with `min(total_rows, 200)` instead of refusing, so without the check a long
  comparison uploads for minutes and comes out quietly short.

## Packaging

`kiyas.spec` builds two executables from one collection: `kiyas.exe` with a
console and `kiyas-gui.exe` without, so double-clicking the window does not
leave a terminal behind it. One directory, not one file -- a single-file
PySide6 build unpacks several hundred megabytes to a temporary directory on
every launch.

- **VapourSynth is deliberately not frozen.** It is compiled plugins that
  install into a virtualenv's site-packages; freezing it would ship something
  that cannot then be extended with the plugin a particular comparison wants.

- **A frozen build must not be told to run `kiyas setup`.** It cannot install
  anything -- there is no pip and no interpreter to point one at -- so both
  `setup` and `doctor`'s hint check `setup_env.is_frozen()` and say what is
  actually possible instead. The rest of what `doctor` reports is true either
  way, which is exactly why that one line has to differ.

- **CI installs ffmpeg through the runner's own package manager**, not a
  third-party action, because this is the part nobody notices has stopped
  working. Windows needs `ffmpeg-full`: the essentials build has no zscale, and
  the tonemap chain does not work without it. Tests that need a filter check
  for it and skip, the same way `doctor` reports a partial engine rather than a
  failure -- a suite that skips its way to green is not a suite, but a suite
  that fails on a build variation is not one either.

- **CI builds the package on every push,** not at tag time. Finding out that
  the release artefact does not build is only useful before you need it.

## House style

Comments explain *why*, not *what*, and the reason is usually a measurement or
an observed failure. When a threshold or a rule exists because of something
seen on real material, say what was seen — those numbers are why the gate is
where it is, and without them the next person cannot tell a tuned constant from
an arbitrary one.

## Credits and licence

GPL-3.0. The per-source parameter model and the order operations are applied in
come from squash's `multi_comps.vpy`; the B-frame selection rule and the
pip-installable stack come from Solarios' VapourSynth comparison guide. Both
are credited in the README and both deserve to stay credited.

kiyas is not tied to any tracker, site or community. Output formats are named
after the markup they produce, never after a place that accepts it, and no
site-specific branding, URL or terminology belongs anywhere in this repository.

A publishing *destination* is the one thing that has to be named, because a
request has to go somewhere. That is a backend, not a brand: it may name the
host in its own module, its own flag value and its own errors, and nowhere
else. It buys no vocabulary anywhere in the tool — `FORMATS` stays
`comparison`, `img`, `markdown`, and a second destination is a reason to make
the shared code more neutral rather than less.
