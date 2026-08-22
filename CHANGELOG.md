# Changelog

## 0.1.11

### The VapourSynth engine could not run from the window at all

Opening any source with it died on `'NoneType' object has no attribute
'flush'` before a single frame was read. A windowed build has no console, so
Python leaves `sys.stderr` unset — it is `None`, not a closed file — and the
indexing capture flushed it unguarded.

The irony is that the very next line already handled this case: redirecting
file descriptor 2 is wrapped in a `try` whose comment names pythonw. Only the
flush above it was written as though stderr were always an object. So the
console build worked, the tests worked, and the engine had never once worked
from `kiyas-gui.exe`.

A stderr that raises on use is now tolerated too, for the capture modes that
hand one back.

## 0.1.10

### The window stops offering an engine it cannot run

The engine dropdown listed all four whatever the machine had. In a packaged
build, which has no VapourSynth, picking it was allowed and the answer came
back as a `RunError` — after the comparison had been set up, and with nothing
in the window to suggest it would fail.

Missing engines are still listed, greyed, with the reason in the name:
`vapoursynth (not installed here)`. Hiding them would be worse — then the
engine that does what you want is simply absent and nothing says why.

`auto` stays selectable whatever is missing, and a probe that throws leaves
every entry enabled rather than emptying the list.

## 0.1.9

### Columns that are not the same size say so

Two pictures of different shapes cannot be flipped between, and flipping
between them is the one thing a comparison is for. Nothing noticed: every image
was written, the manifest was valid, the upload would have succeeded. Measured
on a real pair — a 3840x1606 WEB-DL against a 3840x2160 remux, run without a
crop, and the run had nothing to say about it.

It does not suggest the numbers. The obvious arithmetic is wrong: splitting the
difference gives 277 rows top and bottom where that remux's own Dolby Vision
metadata says 276, with the last row coming from the WEB-DL's conformance
window. A suggestion one row out is worse than none, because it looks like an
answer. The warning points at the RPU's level 5 offsets, which is where the
real number is, and the README shows how to read them.

## 0.1.8

### The packaged build stops giving instructions it cannot follow

`kiyas audio` in a packaged build answered "Run 'pip install kiyas[audio]'",
and `doctor` printed the same line beside numpy, scipy and matplotlib. A frozen
build has no pip and no interpreter to point one at, so that is an instruction
it cannot follow — the rule `kiyas setup` has followed since it existed, in a
different table.

Both now say what is actually possible: use a checkout.

The libraries are not bundled instead, and the numbers are why: they come to
180 MB against a 58 MB package, and being the small download is the whole
argument for the packaged build. The README now says audio needs a checkout
rather than leaving it to be discovered.

## 0.1.7

### The window puts screenshots where you would look for them

Typing `out` in the output box resolved against the process's working
directory, which a window has no way of showing. Launched from a file dialog
that had last visited another drive, a comparison of two files wrote its
screenshots to `E:\out` — correct by the rule, and not where the person who
asked for it would look.

The audio side of the same window already resolved a relative output against
the first source. Both modes now agree about what `out` means. Absolute paths
are untouched, and the command line is unchanged: there the working directory
is something you typed.

## 0.1.6

### The ffmpeg engine says what it left out

0.1.5 taught kiyas to compose a Dolby Vision profile 7 enhancement layer, and
the whole point was to stop producing base-layer screenshots and offering them
as screenshots of the release. The ffmpeg engine kept doing exactly that — it
cannot compose the layer, and it said nothing.

That is the engine the packaged build uses, since VapourSynth is deliberately
not frozen. So the one place the old behaviour still lived was the build most
people run. Found by comparing two profile 7 remuxes with the released
`kiyas-gui.exe` and noticing the run had nothing to say about it.

It now reports the layer the way the VapourSynth engine does, and refuses
`dovi_el = "on"` outright rather than ignoring a request it cannot honour —
an explicit ask for a specific picture is the worst place to quietly produce a
different one.

## 0.1.5

### Dolby Vision profile 7 is composed instead of advertised

`dovi_tool` has been registered in `media/binaries.py`, reported by `doctor` as
"Dolby Vision enhancement layer" and accepted in `[tools]` since the tool
existed, and never once invoked. A profile 7 release carries its picture in two
layers; kiyas captured the base layer and said nothing about it, so a
comparison of two P7 sources showed neither what a Dolby Vision player renders
nor anything a viewer ever sees.

`dovi_el = "on"` composes it. The default, `auto`, detects the layer and says
so in the run's warnings without composing it — extracting it reads the whole
file, and doing that unasked is a worse surprise than a stated caveat. Measured
on a 78 GB profile 7 remux: 10.3 minutes, and a 4.64 GB layer that is kept and
reused.

vs-placebo does the composition and kiyas already depended on it, so this is
one keyword argument on a call the Dolby Vision branch already made.
`awsmfunc`'s `MapDolbyVision` is not used: the version PyPI has hard-fails
without `vs-nlq`, which has no distribution under any name and would have to be
built with cargo.

### The ffmpeg engine labels frames

The packaged build ships ffmpeg and mpv only, so every screenshot from a
release build came out unlabelled while a checkout labelled them. The blocker
was that `drawtext` needs a font by path and there is no portable one, so kiyas
now carries DejaVu Sans, unmodified, with its licence beside it.

The label text goes through a file rather than the `text=` option, because that
option cannot be escaped reliably: an apostrophe has no working form once any
escaped colon follows it, and "Director's Cut" plus "Picture type: B" in one
label is an ordinary thing to want.

### `kiyas align` measures what `trim` was guessing

The audio side has always measured how far apart two tracks are. The picture
side had `trim`, set by hand, and nothing that checked it — and a wrong trim
is wrong in every frame while every frame still looks like a frame.

`kiyas align project.toml` reports how far each source is from the first and
prints the `trim` lines to paste in; `kiyas run --check-sync` does it as part
of a run. The sign is stated and tested: positive means the source plays later.

Confidence is agreement between sampled positions rather than the audio
module's peak-to-floor ratio. That was tried first and does not transfer:
measured on a real 4K feature, a *correct* alignment scored 3.5 and two
completely different films scored 3.1. Agreement separates the same three
cases cleanly: an aligned pair 9 of 9, a deliberately mistrimmed pair 8 of 9
with the offset recovered exactly, two different films 1 of 9.

### Frames chosen for being dark or bright

`[frames] dark` and `light` add frames on top of the evenly spaced ones,
picked for being the darkest and brightest of a sample. Even spacing finds the
typical frame, and neither question people bring to a comparison lives there:
banding is in the dark scenes and highlight rolloff is in the bright ones.
`skip_dark` still applies as a floor, because the darkest frame of most films
is a fade to black.

Measured on a 4K WEB-DL, four evenly spaced frames plus two of each: the picks
landed at 0.10 and 0.10 against an evenly spaced range of 0.18 to 0.32, and at
0.37 and 0.63 above it.

### Combed frames can be skipped

`[frames] skip_combed` rejects frames showing interlacing combs, which compare
the deinterlacer's work rather than the encode's. VapourSynth only; the ffmpeg
engine says it cannot answer for a single frame and the rule turns itself off
with a warning rather than silently reporting every frame as clean.

Off by default, and the measurement is why: on a real film clip and an
interlaced copy of itself it caught 19 of 30 combed frames and flagged none of
the progressive original, but on ffmpeg's `testsrc2` and `mandelbrot` it
flagged 20 progressive frames out of 20. It reads hard horizontal detail as
combing, and animation has hard edges. That also means it cannot be
integration-tested on synthetic media, so the detector is checked by hand
against real material the way frame accuracy and tonemapping are, and the
tests here pin down everything around it.

### `--tmdb` takes a name

`--tmdb MOVIE_1275779` is the reference slow.pics wants and nobody knows that
number, so in practice an optional field went unfilled. Anything that is not a
reference is now looked up by name, with a key read from `KIYAS_TMDB_API_KEY`.

It refuses rather than guesses. One match resolves; several print the
candidates and ask, because "Dune" is two films twenty years apart and taking
whichever TMDB ranks higher attaches the comparison to one of them with a
number that looks perfectly correct. A bare number keeps its own refusal --
it is a reference missing the one thing that cannot be guessed, not a title.

### Also

- `trim` no longer accepts negative numbers. `clip[-5:]` is valid Python and
  takes the last five frames of the film.
- Both engines build the burnt-in label with the same function, so a
  comparison cannot have columns worded two different ways.
- `doctor` reports whether ffmpeg has `drawtext` and whether the label font is
  present, because both decide whether the output has labels on it.

## 0.1.4

### A second place to publish

`kiyas publish --to comppics` uploads to comp.pics, or with `--host-url` to any
instance of the software behind it. slow.pics stays the default and nothing
about that path changed.

What made it worth doing is that this API is documented. slow.pics has none, so
`publish/slowpics.py` was read off a working client and then checked against
the live service; comp.pics publishes an OpenAPI document and its server is
open source, so the shapes in `publish/comppics.py` could be read rather than
inferred.

### The markup formats finally have something to point at

`--format comparison`, `img` and `markdown` have existed since 0.1.0 and have
never once produced what they describe. Every image on slow.pics lives inside
the collection and the upload hands back no per-image address, so all three
degraded to a single link. comp.pics gives every image its own URL, so they now
emit the real thing: one tag holding the whole grid, frame by frame.

Which surfaced a bug in how they were printed. rich wraps to the console width,
and a comparison tag full of UUID-length URLs came out as eight lines instead
of four, broken mid-URL. Markup that exists to be copied has to survive being
copied.

### The transpose, from the other direction

slow.pics returns `images[frame][source]` while kiyas holds
`sources[source][frame]`, and getting that swap right took a while in 0.1.0.
comp.pics takes `row` and `column` as separate fields, so there is no swap —
which makes performing one out of habit the mistake available here. It is the
same silent one: every picture in the wrong cell, no error, and a result that
reads as a dramatic difference between the releases. Checked against the live
service by giving all nine cells of a 3x3 different file sizes and reading them
back out of the server's own JSON.

### Counted nouns now agree with their numbers

Publishing a single source printed "3 rows x 1 sources", and the same fault was
in the `run` and `audio` summaries, in the count of frames the picker marked,
and in the count of images the server already had. A length of one is not a
corner here — one source published on its own, one frame in a spot check — and
"1 sources" is how a tool looks like nobody ever ran it.

One helper now does the agreeing, including for the irregular "1 analysis / 3
analyses", and the expiry note that rounds 2 days down says "1 day" rather than
"1 days".

### What is different over there

- **There is no unlisted mode.** The API lists every comparison to anyone who
  asks, so publishing there is a more public act than the same command against
  slow.pics. It says so before it sends anything.
- **Nothing is kept forever.** The expiry is one of 1, 7, 30 or 90 days, so
  `--remove-after` is snapped to the nearest of those and the choice is printed.
- **The public instance does not apply the expiry it is given.** Asked for one
  day, it stored seven, twice. The field is in the spec and the current server
  source honours it, so the request is right and that deployment is behind.
  kiyas compares what came back and reports the difference rather than leaving
  someone to believe they got what they asked for.
- `--nsfw`, `--no-optimize` and `--tmdb` have no equivalent, and are named as
  ignored if passed.

An account is optional. Uploads work anonymously; `KIYAS_COMPPICS_API_KEY` and
`KIYAS_COMPPICS_URL` are read from the environment when they are set, so there
is still no credential store.

## 0.1.3

### One refusal now stops the whole upload

0.1.1 stopped retrying a refused image, which took a four-image comparison from
twenty requests against a blocked address down to four. Four was still one per
image, and the refusal is not about the image — it is about your address, so it
is the same answer for all of them. A 24-image comparison was still putting 48
requests into an edge that had already said no.

The first worker to be refused now tells the others, and they stop without
sending. Measured, with the change removed and put back: 48 requests against
at most six.

### A block is temporary, and 0.1.1 said it was not

0.1.1 read Cloudflare's error number so it could tell a ban from a rate limit,
on the understanding that a rate limit lapses and a ban does not — Cloudflare's
own 1006 page says the owner of the site "has banned your IP address", which
does not sound like something that expires. It expires. An address refused with
1006 was serving requests again the same day, without anyone being asked.

So the advice attached to it was wrong in the direction that costs the most:
someone whose block would have cleared on its own was told that another network
was the only way through. Both kinds now say to wait.

### Why this keeps happening

Nothing about the address or the client is special. slow.pics is one person's
free service behind Cloudflare, and what earns a block is a burst — which is
what an upload of two dozen 6 MB screenshots looks like when six of them start
at once, some time out, and each timeout is retried five times. Every fix since
0.1.0 has been a different multiplier on that same burst, and this is the last
of them.

## 0.1.2

Reads the Cloudflare error number correctly, which 0.1.1 did not.

0.1.1 added a plain-English message for a blocked address, and told the two
kinds of block apart by the number on the page — a rate limit lapses, a ban
does not, and the advice has to differ. Publishing from a blocked address to
check it showed the number was never being found: the heading that reads
`Error 1006` on screen is two separate elements in the source, so a pattern
written against the rendered text matches nothing. Every block came out with
the generic wording, and a permanent ban was told to wait for the block to
lapse.

The number is now read where it survives as a single token — the page's own
feedback script and its link to Cloudflare's documentation — and the test
fixture is markup copied from a real refusal rather than prose, because prose
is what got this wrong.

## 0.1.1

Publishing fixes, all of them found by publishing. The 0.1.0 build predates
every one of them.

### Uploads no longer make a block worse

Publishing a 24-image comparison could end with slow.pics banning the address
outright — Cloudflare error 1006, which no amount of retrying recovers from.
Three things caused it and all three are fixed.

Six workers opened six connections in the same instant and did it again each
time one finished; upload starts are now held a minimum interval apart across
every worker, and retries go through the same pacing, because a retry is
another request arriving at the same server. The retry backoff grows and is
jittered — identical waits are what put the workers back in lockstep.

A refused request is no longer retried. A 403 is the edge refusing your
address, not the server being busy, and the five further attempts per image
could not succeed: on a four-image comparison that was twenty requests sent to
something that had already said no. Retrying into a block is what turns a rate
limit into a ban.

And when the block does happen, it now reads as one sentence — which Cloudflare
error it is, whether waiting will clear it, and that the site never saw your
comparison — instead of six hundred characters of HTML about enabling cookies.
A ban and a rate limit are told apart, because waiting clears one and never
clears the other.

### Uploads finish on a slow connection

One timeout covered both the small API calls and the image bodies. Thirty
seconds is generous for the first and hopeless for the second: on a real
comparison of 24 screenshots at about 6 MB each, nine were lost to a write
timeout. Image uploads are now given time in proportion to the image.

### `--tmdb` takes the id you have

slow.pics wants `MOVIE_1275779` or `TV_1399`, and refused a bare number with an
empty 400 that cost the whole upload. Any of `MOVIE_1275779`, `TV_1399` or the
`movie/1275779` form Matroska tags carry is now accepted and normalised. A bare
number is still refused, before anything is sent: a film and a series can share
a number, and guessing wrong files the comparison under a different title.

### Rejections say what was rejected

A refused collection reported `400 Client Error: Bad Request` and stopped. The
server does explain, in the response body, which was being discarded.

### HDR10+ no longer promises something it does not do

The `hdr10plus` option said it followed the per-scene metadata HDR10+ carries.
Measured against vs-placebo 2.0.4 on a remux that carries it, it does not:
changing the metadata setting, or removing the metadata entirely, produces
identical output, while changing the curve does not. The curve is real and
still selectable — the metadata path was never live. The ffmpeg engine's
refusal was overpromising in the same way, sending you to the VapourSynth
engine "for HDR10+ metadata" that VapourSynth does not apply either. Both now
say what they actually do.

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
