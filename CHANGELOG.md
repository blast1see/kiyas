# Changelog

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
