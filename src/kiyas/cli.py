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

_DESCRIPTION = "Comparison workbench: screenshots, audio analysis, slow.pics publishing."


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
        "--publish", action="store_true", help="Upload to slow.pics when the run finishes."
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
    audio.add_argument(
        "--publish", action="store_true", help="Upload to slow.pics when the analysis finishes."
    )

    publish_help = "Upload an already-produced comparison to slow.pics."
    publish = sub.add_parser("publish", help=publish_help, description=publish_help)
    publish.add_argument(
        "path",
        nargs="?",
        default=Path("out"),
        type=Path,
        help="Output directory or manifest file from a previous run.",
    )
    publish.add_argument(
        "--public",
        action="store_true",
        help="List the comparison publicly. Unlisted by default.",
    )
    publish.add_argument("--nsfw", action="store_true", help="Flag the collection as adult.")
    publish.add_argument(
        "--no-optimize",
        action="store_true",
        help="Ask slow.pics not to recompress the PNGs.",
    )
    publish.add_argument(
        "--remove-after",
        type=int,
        default=0,
        metavar="DAYS",
        help="Have slow.pics delete the collection after this many days.",
    )
    publish.add_argument("--tmdb", default=None, metavar="ID", help="Attach a TMDB id.")
    publish.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=("comparison", "img", "markdown"),
        help="Also write forum markup. Repeatable.",
    )

    return parser


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
        f"[green]{result.image_count} images[/green] "
        f"({len(result.frames)} frames x {len(result.sources)} sources) "
        f"using the {result.engine} engine"
    )
    for source in result.sources:
        console.print(f"  {escape(source.name)} -> {escape(str(source.directory))}")
    console.print(f"\nmanifest: {escape(str(result.manifest))}")

    if args.publish:
        console.print()
        return _cmd_publish(_publish_defaults(), directory=project.output)
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
        f"[green]{result.image_count} images[/green] "
        f"({len(audio_run.ANALYSES)} analyses x {len(result.tracks)} tracks)"
    )
    for offset in result.offsets:
        console.print(f"  offset: {escape(offset.summary)}")
    for track in result.tracks:
        console.print(f"  {escape(track.name)} -> {escape(str(track.directory))}")
    console.print(f"\nspecifications: {escape(str(result.specifications))}")
    console.print(f"manifest: {escape(str(result.manifest))}")

    if args.publish:
        console.print()
        return _cmd_publish(_publish_defaults(), directory=output)
    return 0


def _publish_defaults():
    """Publishing options for `run --publish`.

    Deliberately the conservative set: unlisted, no expiry, no markup. Anything
    that pushes a comparison further into the world than the person asked for
    should need them to say so.
    """
    from argparse import Namespace

    return Namespace(
        path=None,
        public=False,
        nsfw=False,
        no_optimize=False,
        remove_after=0,
        tmdb=None,
        formats=None,
    )


def _cmd_publish(args, *, directory: Path | None = None) -> int:
    from rich.console import Console
    from rich.markup import escape

    from .publish import bbcode, load_manifest, slowpics
    from .publish.manifest import ManifestError

    console = Console()
    target = directory if directory is not None else args.path

    try:
        comparison = load_manifest(target)
    except ManifestError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    console.print(
        f"publishing [bold]{escape(comparison.title)}[/bold]: "
        f"{comparison.total_images} images, "
        f"{len(comparison.rows)} rows x {len(comparison.sources)} sources"
    )

    status = console.status("preparing")
    status.start()
    try:
        result = slowpics.upload(
            comparison,
            public=args.public,
            nsfw=args.nsfw,
            optimize=not args.no_optimize,
            remove_after_days=args.remove_after,
            tmdb_id=args.tmdb,
            progress=lambda text: status.update(escape(text)),
        )
    except slowpics.UploadError as exc:
        status.stop()
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    finally:
        status.stop()

    if result.skipped:
        console.print(f"[dim]{result.skipped} image(s) were already on the server[/dim]")
    console.print(f"\n[green]{escape(result.url)}[/green]")

    for fmt in args.formats or []:
        # Every image lives inside the collection, so the per-image URLs a
        # forum tag needs are not something the upload hands back. The link is
        # what gets posted; the markup formats stay available for anyone who
        # rehosts elsewhere.
        console.print(f"\n[bold]{fmt}[/bold]")
        if fmt == "markdown":
            console.print(f"[{escape(comparison.title)}]({escape(result.url)})")
        else:
            console.print(escape(bbcode.collection_link(comparison, result.url)))

    return 0


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

    console.print(f"[green]{len(frames)} frame(s) marked[/green]. Paste this into your project:\n")
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

    if args.command == "audio":
        return _cmd_audio(args)

    if args.command == "publish":
        return _cmd_publish(args)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - argparse exits


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
