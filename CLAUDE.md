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

The settings mode is not a bonus feature. Tone-mapping curves and GLSL shaders
only exist inside mpv, so VapourSynth cannot answer that question at all.

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
engines/base.py     the protocol both engines implement
engines/*.py        VapourSynth (default) and ffmpeg (fallback)
run.py              orchestration: config in, PNGs and a manifest out
doctor.py           what this machine can do, and what is missing
setup_env.py        installing the VapourSynth stack into this venv
cli.py              argument parsing; imports subcommands lazily
```

Two boundaries are worth keeping:

**`frames/selector.py` never decodes anything.** It does the arithmetic and
takes a predicate for the rest, so the B-frame and dark-frame rules are tested
without media and neither engine reimplements the search.

**`run.py` names no engine.** It picks one, asks it to prepare each source, and
writes what comes back. Anything engine-specific that leaks up to here is in
the wrong place.

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

The GUI writes a project TOML and calls the same core the CLI calls. If a
feature is only reachable through the GUI, that is a design error. Consequences
worth keeping: project files are shareable and diffable, the core is testable
without a display, and nothing has to be implemented twice.

## Testing

Run before claiming anything works:

```bash
python -m ruff check . && python -m ruff format --check .
python -m pytest -q                       # unit tests, no media needed
python -m pytest -m integration           # needs FFmpeg
python -m pytest -m vapoursynth           # needs 'kiyas setup' to have run
```

Markers: `integration`, `vapoursynth`, `mpv`, `gui`, `live`. `live` talks to
slow.pics for real and never runs unattended.

**Unit tests are necessary and not sufficient.** The version-probe bug below
passed every unit test. Frame accuracy, tonemapping and sync have to be checked
against real material; feature-length remuxes are at
`a directory outside this repository`.

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
  This has now been got wrong twice, so treat it as a rule: **any threshold
  measured in seconds or frames must be a fraction of the material, with a
  floor.** A four-second test clip and a three-hour remux differ by four orders
  of magnitude, and a constant tuned for one is nonsense for the other.
  - The source-length warning is 1% of the longest source, floor one second.
    A fixed sixty seconds never fired on short clips and fired on every
    alternate cut of a film.
  - The ffmpeg tail margin is 0.2% of the frame count, floor two frames. A
    fixed ten seconds zeroed out every four-second clip in the test suite.

- **ffprobe's frame count overshoots, and the two engines disagree because of
  it.** VapourSynth reports `clip.num_frames`, which is exact. ffmpeg has only
  `duration x fps`, measured 125 frames (0.08%) high on a retail remux. Two
  consequences: a capture near the end of a file can seek past EOF and fail
  with nothing but an ffmpeg error, which the tail margin exists to prevent;
  and the *same project file* selects slightly different frames depending on
  the engine. Within one run only one engine is used, so a comparison is never
  internally inconsistent -- but a run is only reproducible against itself.

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
