"""Command line entry point.

Subcommands import their implementation lazily: importing ``kiyas.cli`` must not
import VapourSynth, matplotlib or PySide6, because ``kiyas doctor`` has to work
on an environment where none of those are installed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__

# Safe at module level: this one is stdlib-only, unlike everything the
# subcommands import.
from .media.binaries import BinaryNotFound

_DESCRIPTION = "Comparison workbench: screenshots, audio analysis, publishing."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiyas",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"kiyas {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor_help = "Report which engines and external tools are available."
    sub.add_parser("doctor", help=doctor_help, description=doctor_help)

    setup_help = "Install the VapourSynth stack into this environment."
    setup = sub.add_parser("setup", help=setup_help, description=setup_help)
    setup.add_argument(
        "--upgrade", action="store_true", help="Upgrade packages that are already installed."
    )

    init_help = "Write a starter project file."
    init = sub.add_parser("init", help=init_help, description=init_help)
    init.add_argument("path", nargs="?", default="kiyas.toml", type=Path, help="Where to write it.")
    init.add_argument("--title", default="Untitled comparison", help="Comparison title.")
    init.add_argument(
        "--settings",
        action="store_true",
        help="Start a settings comparison (one file, several render configurations).",
    )

    templates_help = "List the built-in settings-comparison templates."
    sub.add_parser("templates", help=templates_help, description=templates_help)

    gui_help = "Open the desktop interface."
    gui = sub.add_parser("gui", help=gui_help, description=gui_help)
    gui.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files to add, or a project file to open.",
    )

    pick_help = "Choose frames by watching the file in mpv."
    pick = sub.add_parser("pick", help=pick_help, description=pick_help)
    pick.add_argument("path", type=Path, help="The video file to scrub through.")
    pick.add_argument("--start", type=int, default=0, metavar="FRAME", help="Open at this frame.")

    run_help = "Produce the comparison described by a project file."
    run_cmd = sub.add_parser("run", help=run_help, description=run_help)
    run_cmd.add_argument("project", type=Path, help="Path to the project TOML file.")
    run_cmd.add_argument(
        "--no-overlay",
        action="store_true",
        help="Do not burn the source name into the frame.",
    )
    run_cmd.add_argument("--output", type=Path, default=None, help="Override the output directory.")
    run_cmd.add_argument(
        "--check-sync",
        action="store_true",
        help="Also measure how far each source is from the first. Costs a second pass.",
    )
    run_cmd.add_argument("--publish", action="store_true", help="Publish when the run finishes.")
    run_cmd.add_argument(
        "--publish-to",
        choices=("slowpics", "comppics"),
        default="slowpics",
        help="Where --publish sends the comparison. Default: slowpics.",
    )

    align_help = "Measure how far each source is from the first, in frames."
    alignment = sub.add_parser("align", help=align_help, description=align_help)
    alignment.add_argument("project", type=Path, help="The project file.")
    alignment.add_argument(
        "--samples",
        type=int,
        default=None,
        help="How many positions along the film to sample. More is slower and steadier.",
    )

    audio_help = "Compare the audio tracks of two or more files."
    audio = sub.add_parser("audio", help=audio_help, description=audio_help)
    audio.add_argument("paths", nargs="+", type=Path, help="The files to compare.")
    audio.add_argument(
        "--output", type=Path, default=Path("audio-out"), help="Where to write the comparison."
    )
    audio.add_argument("--title", default="", help="Comparison title.")
    audio.add_argument(
        "--track",
        type=int,
        default=0,
        metavar="N",
        help="Which audio stream to take from each file. Default: the first.",
    )
    audio.add_argument("--publish", action="store_true", help="Publish when the analysis finishes.")
    audio.add_argument(
        "--publish-to",
        choices=("slowpics", "comppics"),
        default="slowpics",
        help="Where --publish sends the comparison. Default: slowpics.",
    )

    publish_help = "Upload an already-produced comparison to a comparison host."
    publish = sub.add_parser("publish", help=publish_help, description=publish_help)
    publish.add_argument(
        "path",
        nargs="?",
        default=Path("out"),
        type=Path,
        help="Output directory or manifest file from a previous run.",
    )
    publish.add_argument(
        "--to",
        choices=("slowpics", "comppics"),
        default="slowpics",
        help="Which host to publish to. Default: slowpics.",
    )
    publish.add_argument(
        "--host-url",
        default=None,
        metavar="URL",
        help="Publish to a self-hosted comppics instance instead of comp.pics. "
        "Also read from KIYAS_COMPPICS_URL.",
    )
    publish.add_argument(
        "--tag",
        dest="tags",
        action="append",
        metavar="TAG",
        help="Tag the comparison. Repeatable. comppics only.",
    )
    publish.add_argument(
        "--expire-from",
        choices=("creation", "last-access"),
        default="last-access",
        help="Whether the expiry clock runs from when the comparison was made "
        "or from when it was last viewed. comppics only.",
    )
    publish.add_argument(
        "--public",
        action="store_true",
        help="List the comparison publicly. Unlisted by default. slowpics only.",
    )
    publish.add_argument(
        "--nsfw", action="store_true", help="Flag the collection as adult. slowpics only."
    )
    publish.add_argument(
        "--no-optimize",
        action="store_true",
        help="Ask the host not to recompress the PNGs. slowpics only.",
    )
    publish.add_argument(
        "--remove-after",
        type=int,
        default=0,
        metavar="DAYS",
        help="Delete the comparison after this many days. Zero means never, "
        "which comppics cannot do -- there it is snapped to the nearest of "
        "1, 7, 30 or 90.",
    )
    publish.add_argument(
        "--tmdb",
        default=None,
        metavar="ID_OR_NAME",
        help="Attach a TMDB reference, as MOVIE_1275779 or TV_1399. A bare number is "
        "refused: slow.pics needs to know which of the two it is. Anything that is not "
        "a reference is looked up by name, which needs a key in KIYAS_TMDB_API_KEY. "
        "slowpics only.",
    )
    publish.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=("comparison", "img", "markdown"),
        help="Also write forum markup. Repeatable.",
    )

    return parser


def _count(number: int, noun: str, plural: str | None = None) -> str:
    """`1 image`, `2 images`.

    Counted nouns are printed from lengths all over this file, and a length is
    1 often enough to matter: one source in a settings comparison, one frame in
    a spot check. "1 rows x 1 sources" is how a tool looks like nobody ran it.

    The explicit plural is for the irregular ones; everything else takes an s.
    """
    return f"{number} {noun}" if number == 1 else f"{number} {plural or noun + 's'}"


def _cmd_run(args) -> int:
    from rich.console import Console
    from rich.markup import escape

    from . import config, run

    console = Console()
    try:
        project = config.load(args.project)
    except config.ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    if args.output is not None:
        project.output = args.output.expanduser().resolve()

    status = console.status("starting")
    status.start()
    try:
        result = run.run(
            project,
            overlay=not args.no_overlay,
            progress=lambda text: status.update(escape(text)),
        )
    except run.RunError as exc:
        status.stop()
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    finally:
        status.stop()

    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")

    console.print()
    console.print(
        f"[green]{_count(result.image_count, 'image')}[/green] "
        f"({_count(len(result.frames), 'frame')} x {_count(len(result.sources), 'source')}) "
        f"using the {result.engine} engine"
    )
    for source in result.sources:
        console.print(f"  {escape(source.name)} -> {escape(str(source.directory))}")
    console.print(f"\nmanifest: {escape(str(result.manifest))}")

    if args.check_sync:
        console.print()
        _cmd_align(args)

    if args.publish:
        console.print()
        return _cmd_publish(_publish_defaults(args.publish_to), directory=project.output)
    return 0


def _cmd_audio(args) -> int:
    from rich.console import Console
    from rich.markup import escape

    from .audio import AnalysisError
    from .audio import run as audio_run
    from .media.probe import ProbeError

    console = Console()
    output = args.output.expanduser().resolve()

    status = console.status("starting")
    status.start()
    try:
        result = audio_run.run(
            [Path(path).expanduser() for path in args.paths],
            output=output,
            title=args.title,
            track_index=args.track,
            progress=lambda text: status.update(escape(text)),
        )
    except (AnalysisError, ProbeError) as exc:
        status.stop()
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    finally:
        status.stop()

    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")

    console.print()
    console.print(
        f"[green]{_count(result.image_count, 'image')}[/green] "
        f"({_count(len(audio_run.ANALYSES), 'analysis', 'analyses')} x "
        f"{_count(len(result.tracks), 'track')})"
    )
    for offset in result.offsets:
        console.print(f"  offset: {escape(offset.summary)}")
    for track in result.tracks:
        console.print(f"  {escape(track.name)} -> {escape(str(track.directory))}")
    console.print(f"\nspecifications: {escape(str(result.specifications))}")
    console.print(f"manifest: {escape(str(result.manifest))}")

    if args.publish:
        console.print()
        return _cmd_publish(_publish_defaults(args.publish_to), directory=output)
    return 0


def _publish_defaults(target: str = "slowpics"):
    """Publishing options for `run --publish` and `audio --publish`.

    Deliberately the conservative set: unlisted where the host has such a
    thing, no expiry, no markup. Anything that pushes a comparison further into
    the world than the person asked for should need them to say so.

    The target is the one thing taken from the command line, because there the
    person did say: it comes from `--publish-to`.
    """
    from argparse import Namespace

    return Namespace(
        path=None,
        to=target,
        host_url=None,
        tags=None,
        expire_from="last-access",
        public=False,
        nsfw=False,
        no_optimize=False,
        remove_after=0,
        tmdb=None,
        formats=None,
    )


#: Options only slow.pics understands, paired with the flag that sets them.
#: Naming them lets the comppics path say which ones it dropped rather than
#: dropping them quietly -- somebody who typed `--public` has a belief about
#: what is about to happen, and it is wrong on that host in both directions.
_SLOWPICS_ONLY = (
    ("--public", "public"),
    ("--nsfw", "nsfw"),
    ("--no-optimize", "no_optimize"),
    ("--tmdb", "tmdb"),
)


def _slowpics_sender(args, comparison, console):
    from .publish import slowpics

    if getattr(args, "host_url", None):
        raise ValueError("--host-url only applies to --to comppics; slow.pics is one site.")
    if getattr(args, "tags", None):
        raise ValueError("--tag only applies to --to comppics; slow.pics has no tags.")

    # Checked before anything is sent. A malformed id comes back from the
    # server as a bare 400 with no body, after the whole comparison has been
    # hashed and offered -- a slow way to learn about a typo.
    if args.tmdb:
        # Resolved here, before anything is hashed or offered. Learning that a
        # title was misspelt after the whole comparison has been uploaded is a
        # slow way to find out, which is why the reference was already
        # validated at this point rather than at the end.
        #
        # A bare number keeps its own refusal: it is a reference with the one
        # thing missing that cannot be guessed, not a title to go looking for.
        if slowpics.looks_like_tmdb(args.tmdb) or args.tmdb.strip().isdigit():
            args.tmdb = slowpics.normalise_tmdb(args.tmdb)
        else:
            from .publish import tmdb as tmdb_lookup

            args.tmdb = tmdb_lookup.resolve(args.tmdb)

    def send(progress):
        return slowpics.upload(
            comparison,
            public=args.public,
            nsfw=args.nsfw,
            optimize=not args.no_optimize,
            remove_after_days=args.remove_after,
            tmdb_id=args.tmdb,
            progress=progress,
        )

    return send


def _comppics_sender(args, comparison, console):
    from rich.markup import escape

    from .publish import comppics

    dropped = [flag for flag, attr in _SLOWPICS_ONLY if getattr(args, attr, None)]
    if dropped:
        console.print(
            f"[yellow]ignored:[/yellow] {escape(', '.join(dropped))} — comppics has no equivalent."
        )

    # Said before anything is sent rather than after. This host has no unlisted
    # mode at all: its own API hands the whole catalogue to anyone who asks, so
    # publishing here is a more public act than publishing to slow.pics with
    # the same flags. Somebody relying on the defaults being timid should hear
    # that while they can still stop it.
    console.print(
        "[yellow]note:[/yellow] comppics has no unlisted mode; this comparison "
        "will be listed publicly."
    )

    expiration_type = "from_creation" if args.expire_from == "creation" else "from_last_access"
    clock = "after it was created" if args.expire_from == "creation" else "after it was last viewed"

    days = args.remove_after
    if days <= 0:
        days = 7
        console.print(
            f"[yellow]note:[/yellow] comppics cannot keep a comparison forever; "
            f"this one goes away 7 days {clock}."
        )
    else:
        snapped = comppics.snap_expiration(days)
        if snapped != days:
            # The clock is left out of this one on purpose: --expire-from is
            # the person's own choice, so repeating it here is noise. What they
            # did not choose is the rounding, and that is what this says.
            head = ", ".join(str(day) for day in comppics.EXPIRATION_DAYS[:-1])
            console.print(
                f"[yellow]note:[/yellow] comppics only accepts "
                f"{head} or {comppics.EXPIRATION_DAYS[-1]} days; keeping this one "
                f"{_count(snapped, 'day')} instead of {_count(days, 'day')}."
            )
        days = snapped

    def send(progress):
        return comppics.upload(
            comparison,
            base_url=args.host_url,
            tags=tuple(args.tags or ()),
            expiration_days=days,
            expiration_type=expiration_type,
            progress=progress,
        )

    return send


_SENDERS = {"slowpics": _slowpics_sender, "comppics": _comppics_sender}


def _cmd_publish(args, *, directory: Path | None = None) -> int:
    from rich.console import Console
    from rich.markup import escape

    from .publish import bbcode, load_manifest
    from .publish.manifest import ManifestError
    from .publish.result import UploadError

    console = Console()
    target = directory if directory is not None else args.path

    try:
        comparison = load_manifest(target)
    except ManifestError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    console.print(
        f"publishing [bold]{escape(comparison.title)}[/bold]: "
        f"{_count(comparison.total_images, 'image')}, "
        f"{_count(len(comparison.rows), 'row')} x "
        f"{_count(len(comparison.sources), 'source')}"
    )

    try:
        send = _SENDERS[getattr(args, "to", "slowpics")](args, comparison, console)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1

    status = console.status("preparing")
    status.start()
    try:
        result = send(lambda text: status.update(escape(text)))
    except UploadError as exc:
        status.stop()
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    finally:
        status.stop()

    if result.skipped:
        console.print(f"[dim]the server already had {_count(result.skipped, 'image')}[/dim]")
    for note in result.notes:
        console.print(f"[yellow]note:[/yellow] {escape(note)}")
    console.print(f"\n[green]{escape(result.url)}[/green]")

    for fmt in args.formats or []:
        console.print(f"\n[bold]{fmt}[/bold]")
        try:
            # soft_wrap because this is markup somebody is about to copy. Left
            # to wrap at the console width, rich breaks the long image URLs
            # mid-token and inserts real newlines, so what gets pasted into a
            # forum is a list of dead links. Measured: a four-line comparison
            # tag came out as eight lines at width 80.
            console.print(escape(_markup(comparison, result, fmt)), soft_wrap=True)
        except bbcode.BBCodeError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")

    return 0


def _markup(comparison, result, fmt: str) -> str:
    """Forum markup for a published comparison, as good as the host allows.

    A host that gives every image its own address gets the real thing: one tag
    holding the whole grid, which is what a reader can actually flip through.
    slow.pics keeps its images inside the collection and hands back no
    per-image URLs, so there the link is what gets posted -- the markup formats
    stay available for anyone who rehosts elsewhere.
    """
    from .publish import bbcode

    if result.image_urls:
        return bbcode.render(comparison, result.image_urls, fmt)
    if fmt == "markdown":
        return f"[{comparison.title}]({result.url})"
    return bbcode.collection_link(comparison, result.url)


def _cmd_init(args) -> int:
    from rich.console import Console
    from rich.markup import escape

    from . import run

    console = Console()
    try:
        path = run.scaffold(args.path.expanduser(), args.title, settings=args.settings)
    except run.RunError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    console.print(f"[green]wrote {escape(str(path))}[/green]")
    if args.settings:
        console.print(escape("Point [[source]] at your file and pick a template, then run:"))
    else:
        console.print(escape("Edit the [[source]] entries, then run:"))
    console.print(f"  kiyas run {escape(str(path))}")
    return 0


def _cmd_templates() -> int:
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    from .mpvctl.variants import describe_templates

    console = Console()
    table = Table(title="Settings comparison templates", title_justify="left", expand=True)
    table.add_column("template", no_wrap=True)
    table.add_column("variants", overflow="fold")
    for name, summary in describe_templates():
        table.add_row(name, escape(summary))
    console.print(table)
    console.print(escape('Use one with mode = "settings" and [settings] template = "...".'))
    return 0


def _cmd_align(args) -> int:
    from rich.console import Console
    from rich.markup import escape

    from . import config, run
    from .frames import align

    console = Console()
    try:
        project = config.load(args.project)
    except config.ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    status = console.status("starting")
    status.start()
    try:
        fps, results = run.align_project(
            project,
            # `run --check-sync` reaches this without a --samples flag of its own.
            samples=getattr(args, "samples", None) or align.SAMPLES,
            progress=lambda text: status.update(escape(text)),
        )
    except run.RunError as exc:
        status.stop()
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    finally:
        status.stop()

    reference = project.sources[0]
    console.print(f"reference: [bold]{escape(reference.name)}[/bold]\n")

    for result in results:
        colour = "yellow" if result.is_weak else "green"
        console.print(f"[bold]{escape(result.name)}[/bold]")
        console.print(f"  [{colour}]{escape(result.summary(fps))}[/{colour}]")

    # The reference goes in with an offset of zero, because it is the source
    # that has to move when another one turns out to play earlier: there is no
    # negative trim to give that one instead.
    trims = align.suggested_trims(
        [0, *(result.frames for result in results)],
        [source.trim for source in project.sources],
    )
    if any(trim != source.trim for source, trim in zip(project.sources, trims)):
        console.print("\nPaste this into the project file:\n")
        for source, trim in zip(project.sources, trims):
            console.print(escape(f"[[source]]  # {source.name}"))
            console.print(escape(f"trim = {trim}"))
    else:
        console.print("\n[green]every source is already in step[/green]")
    return 0


def _cmd_pick(args) -> int:
    import tempfile

    from rich.console import Console
    from rich.markup import escape

    from .media import binaries
    from .media.probe import ProbeError, probe
    from .mpvctl import picker
    from .mpvctl.session import SessionError

    console = Console()
    path = args.path.expanduser()
    if not path.is_file():
        console.print(f"[red]no such file: {escape(str(path))}[/red]")
        return 2

    try:
        mpv = binaries.require_binary("mpv")
    except binaries.BinaryNotFound as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        console.print("The frame picker plays the file in mpv, so mpv has to be installed.")
        return 2

    try:
        info = probe(path)
    except ProbeError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    console.print(f"opening [bold]{escape(path.name)}[/bold] in mpv")
    for key, _, what in picker.BINDINGS:
        console.print(f"  [bold]{key}[/bold]  {what}")
    console.print("  [bold]q[/bold]  finish and print the frames\n")

    config_dir = Path(tempfile.gettempdir()) / "kiyas-mpv-profile"
    try:
        frames = picker.pick(mpv, path, config_dir=config_dir, fps=info.fps, start_frame=args.start)
    except (picker.PickerError, SessionError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1

    if not frames:
        console.print("[yellow]no frames were marked[/yellow]")
        return 0

    console.print(
        f"[green]{_count(len(frames), 'frame')} marked[/green]. Paste this into your project:\n"
    )
    console.print(escape(picker.as_toml(frames)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a subcommand, turning a missing external tool into an answer.

    The ``BinaryNotFound`` catch is here rather than in each subcommand so a
    new one cannot forget it. A missing ffmpeg is the single most likely thing
    to be wrong on a fresh machine, and a Python traceback is a poor way to say
    "install ffmpeg" -- it reads as a crash in kiyas rather than a fact about
    the environment.
    """
    try:
        return _dispatch(argv)
    except BinaryNotFound as exc:
        from rich.console import Console
        from rich.markup import escape

        console = Console()
        console.print(f"[red]{escape(str(exc))}[/red]")
        console.print("Run 'kiyas doctor' to see what is available and what is missing.")
        return 2


def _dispatch(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        print("\nNo command given. Try 'kiyas doctor'.")
        return 0

    if args.command == "doctor":
        from . import doctor

        return doctor.run()

    if args.command == "setup":
        from . import setup_env

        try:
            return setup_env.run(upgrade=args.upgrade)
        except setup_env.SetupError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.command == "init":
        return _cmd_init(args)

    if args.command == "templates":
        return _cmd_templates()

    if args.command == "gui":
        from .gui import main as gui_main

        return gui_main([str(path) for path in args.paths])

    if args.command == "pick":
        return _cmd_pick(args)

    if args.command == "run":
        return _cmd_run(args)

    if args.command == "align":
        return _cmd_align(args)

    if args.command == "audio":
        return _cmd_audio(args)

    if args.command == "publish":
        return _cmd_publish(args)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - argparse exits


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
