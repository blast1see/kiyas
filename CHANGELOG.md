# Changelog

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
