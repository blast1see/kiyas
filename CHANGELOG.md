# Changelog

## 0.1.15

### Two columns could share one directory, and the pictures overwrote each other

`safe_directory_name` sanitises a source name into a folder name. Config
rejects duplicate source *names*, but nothing checked the folders, and four
realistic pairs of distinct names land on one folder:

    "Lionsgate GBR/USA" and "Lionsgate GBR_USA"    an illegal character
    "Remux." and "Remux"                           a trailing dot
    "REMUX" and "remux"                            case, on any Windows disk
    "...Atmos-GROUPONE" and "...Atmos-GROUPTWO"    truncation at 100 characters

The last one is the one that bites. Scene names routinely pass 100 characters
and differ only in the group tag at the very *end*, which is precisely the part
truncation removes.

When it happened the second column's `NNNNNN.png` overwrote the first's, the
manifest pointed both columns at the same files, and what got published was a
comparison of a release against itself. Nothing errored, and the result looks
exactly like a comparison — the same shape of failure as uploading the grid
transposed, and just as invisible.

The folder names are now decided over the whole set. The first column keeps the
name it asked for; a later collision takes `-2`, trimmed back into the length
limit rather than pushed past it. The audio side had solved this for column
*labels* and had the same hole in its *directories*; it uses the shared
function now.

### A file ffprobe could not read came out as a traceback

`run` promises `RunError` and every caller — the CLI, the window, `align` — is
written to that promise. `ProbeError` is a sibling of `RunError`, not a
subclass, so pointing kiyas at something that is not a video ended in a Python
stack trace, which reads as a crash in kiyas rather than a fact about the file.
It is wrapped at the module's own boundary now, next to where `EngineError`
already was, so every caller is covered rather than every caller being patched.

### A typo in `[tools]` deleted an engine instead of reporting itself

`find_binary` raises for a configured path that is relative or absent, and
`available_engines` wrapped every engine in a bare `except Exception`. So
`ffmpeg = "C:fmpginfmpeg.exe"` — one missing letter — became "no frame
engine is available. Run 'kiyas doctor'", and doctor, which resolves from PATH,
then reported ffmpeg as ok. The user was sent looking for a problem that was
not there. A bad configured path now comes out as itself.

### Dolby Vision profile 5 was tone mapped as HDR10, and the label agreed

`media/probe.py` has said all along why this is wrong: "a DoVi profile 5 file
carries no usable HDR10 layer at all — tonemapping it as HDR10 produces the
green/purple cast that makes those screenshots useless." The ffmpeg engine
mapped every Dolby Vision profile to the HDR10 chain regardless, and then burnt
"tonemapped hdr10" into the frame, so the picture claimed to be right. The
existing test covered profile 8, which has a real HDR10 base layer and is fine.

The ffmpeg engine now refuses profile 5 and points at the VapourSynth engine,
which reads the Dolby Vision metadata itself.

A refusal about how a frame would *look* has no business stopping a
measurement, so `prepare` gained `for_measurement`. Two callers set it: the mpv
engine, which borrows an ffmpeg source for frame counts and brightness and
draws the pictures itself, and `align`, which reads brightness and never draws
anything. Without the second, `kiyas align` and `run --check-sync` would have
started refusing profile 5 sources they handle perfectly well.

### `align` said how far it had looked

Measured on two clips 137 frames apart: on a 1000-frame source the search
window is 48 frames either way, so 137 was never a candidate. What came back
was a wrong number marked "weak match" — and "weak" on its own reads as "this
material is hard", not "the answer was outside the window". The window is now
named whenever the match is weak, and a block headed "Paste this into the
project file" no longer says that when nothing in it is a confident
measurement.

The ±1% window itself is unchanged and deliberate: past that a difference is a
different edition, and the source-length warning says that better. On the same
clips it did, exactly: "source lengths differ by 137 frames (5.7s)".

### Three ways a measurement could be believed when it had not happened

`mean_luma` returned `0.0` when the ffmpeg call behind it failed, which is a
real reading -- a frame that is genuinely black. So an ffmpeg that fell over
made a good frame look like a fade and `skip_dark` threw it away. Ranking made
it worse: `extremes` sorts on that number to find the darkest frame in the
film, so a failed measurement would have won. It returns `None` now, the same
way `combed` already did, and neither rule counts a frame it could not read.

The two halves of the B-frame rule disagreed about an unreadable picture type.
The strict half refused it (`kind != "B"`), and the fallback half -- "avoid
I-frames", used on an encode that has none -- accepted it, although an unknown
frame may well be one.

A `Retry-After` longer than the retry ceiling was slept on, up to five times.
An hour asked for that way is a run that looks hung for most of a day, and the
ceiling exists to stop exactly that for the local backoff. Clamping it is not
the answer either: retrying sooner than the server asked is how a rate limit
becomes a ban. This module already holds that a refusal is never retried, and a
wait that long is the same answer with a number attached, so it stops and says
what was asked for.

### Publishing to pixhost

A third destination, `--to pixhost`, and a fourth markup format to go with it.

pixhost is an image host rather than a comparison host, and that is the point
of it being here. slow.pics and comp.pics store a grid with a viewer that flips
between sources at one frame; pixhost stores pictures and hands back a page and
a thumbnail for each. That is what a forum post wants and what neither of the
other two gives:

    [b]0:21:14.083 / 30550[/b]
    UHD REMUX: [url=.../show/...][img]https://t3.pixhost.to/thumbs/...[/img][/url]

`--format thumbnails` is the one format that does not inline the full picture,
which is what makes a post of two dozen 4K screenshots readable. It is not a
pixhost format: any backend that fills in `thumbnail_urls` and `page_urls` can
use it.

Details that shaped it:

- **`show_url` is an HTML page, not a picture**, verified live — it answers
  `text/html` — and the direct address of the full-size file is not documented.
  So `image_urls` stays empty for this host rather than being filled with
  something that would be a broken picture inside `[img]`.
- **10 MB per image, and a 4K PNG is close to it**: 7.3 MB measured on a UHD
  HDR10 frame. Every file is checked before the first request, because the
  alternative is a half-filled gallery. Ten million bytes, not ten mebibytes:
  the documentation does not say which it means, and a file in the
  half-megabyte gap is exactly the one the check exists to stop.
- **A failed upload still hands back the gallery's management link.** By then
  some images are up, on a host with no expiry, and that token appears once and
  cannot be recovered.
- **`optimize_for_web` is never sent.** Off is its default and the only setting
  under which a lossless PNG survives being hosted.
- **No API key**, so nothing to store and nothing to configure.

The window has a destination selector now, instead of a button that said
"Publish to slow.pics" and a confirmation that promised the comparison would be
unlisted whatever the destination — true of exactly one of the three.

### Publishing is tested against the real hosts

The `live` marker had been registered since the beginning and nothing carried
it. `tests/test_publish_live.py` uploads a synthetic two-column set to all
three hosts, reads every returned address back, and checks that the picture at
the far end is the one that was meant to be there — brightness, not bytes,
because the hosts resize. That is the check a fake session cannot make, and the
transposed grid is the failure it exists for.

`live` is now deselected in `addopts`, which it needed to be: registering a
marker labels tests, it does not stop them running, so `pytest -q` — the
command the notes describe as "unit tests, no media needed" — published to all
three hosts. Three publishes to slow.pics in quick succession is how the rate
limiting in 0.1.1 was earned in the first place. `pytest -m live` opts in and
replaces the expression; CI's own filter is unaffected.

The pacing, backoff and timeout machinery had been written twice, byte for
byte. It lives in `publish/transport.py` now. The numbers that are actually
per-host stayed where they were measured.

## 0.1.14

### No more console windows — this time all of them

0.1.13 stopped mpv opening a console and said, on the strength of a
measurement, that ffmpeg did not need the same treatment. That was wrong, and
wrong in a way worth writing down: a console window belongs to *conhost.exe*,
not to the process that owns the console. Enumerating ffmpeg's own windows
found none, so the conclusion was that there were none.

Measured properly — watching every 20 ms for console windows that were not
there before — one three-frame comparison from a windowed process put up **176
console windows**. With the flag: **none**. At the twelve frames a real
comparison uses, that is closer to seven hundred.

Every subprocess in kiyas now passes `CREATE_NO_WINDOW`, and a test walks the
source to keep it that way, because this failure is invisible from inside: the
run succeeds, the pictures are right, and the only symptom is on the screen.

The single exception is `kiyas setup`, which inherits the console on purpose so
that pip's progress is visible. It is unreachable from a windowed build, which
refuses to run setup at all.

## 0.1.13

### mpv no longer opens a console window per column

Running a settings comparison from `kiyas-gui.exe` put up a black console
window for every variant it rendered.

`PATHEXT` places `.com` ahead of `.exe`, so on Windows `mpv` resolves to
`mpv.com` — a console *wrapper*, shipped so that the GUI build has somewhere to
write. Launched from a process that has no console of its own, it makes one.

Measured from a real windowed process rather than reasoned about, because the
answer is not the same for every child: launched with all three standard
handles redirected, ffmpeg gets no window with or without `CREATE_NO_WINDOW`.
`mpv.com` owned a visible console without the flag and none with it. So the
flag went where it was measured to matter, and the note next to it says the
others were checked and do not need it.

## 0.1.12

### The size warning now says which crop to use

Two columns of different shapes cannot be flipped between, and 0.1.9 started
saying so. It stopped short of the useful part: it told you the active picture
was in the Dolby Vision RPU's level 5 offsets and left you to go and extract an
RPU yourself.

It now reads them. When the sources are not the same size and one of them
carries Dolby Vision, the warning carries the line to paste —
`crop = [0, 0, 276, 276]` — and the size that leaves.

It costs about a second. `dovi_tool extract-rpu --limit` stops after a handful
of frames and closes the pipe, so a 78 GB remux is sampled in 0.2 seconds per
position and never read through.

Three things it refuses to do:

- **Guess.** If dovi_tool is missing, or the RPU will not parse, the warning
  is the one 0.1.9 printed. Enriching a message is not worth failing a run that
  has already written its images.
- **Answer from one place in the film.** A title with an IMAX sequence changes
  shape as it plays, and a reading taken inside either stretch looks perfectly
  constant. Five positions spread across the film are read; if they disagree
  the warning says the picture changes shape and suggests no crop at all.
- **Make the numbers match.** Measured on the pair this was built against: the
  disc's own metadata says 276 rows top and bottom, leaving 1608, while the
  online WEB-DL of the same film is 1606. The warning prints the crop the
  metadata asks for and then says it is still two rows taller than the other
  release. Moving those two rows to the bottom edge would make the sizes agree
  by inventing a framing nobody chose.

Sources that already carry a `crop` or a `resize` are skipped: the offsets
describe the frame as encoded, so against a transformed source they answer a
different question.

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
