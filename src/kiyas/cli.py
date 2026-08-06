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

    run_help = "Produce the comparison described by a project file."
    run_cmd = sub.add_parser("run", help=run_help, description=run_help)
    run_cmd.add_argument("project", type=Path, help="Path to the project TOML file.")
    run_cmd.add_argument(
        "--no-overlay",
        action="store_true",
        help="Do not burn the source name into the frame.",
    )
    run_cmd.add_argument("--output", type=Path, default=None, help="Override the output directory.")

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
    return 0


def _cmd_init(args) -> int:
    from rich.console import Console
    from rich.markup import escape

    from . import run

    console = Console()
    try:
        path = run.scaffold(args.path.expanduser(), args.title)
    except run.RunError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2

    console.print(f"[green]wrote {escape(str(path))}[/green]")
    console.print("Edit the [[source]] entries, then run:")
    console.print(f"  kiyas run {escape(str(path))}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
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

    if args.command == "run":
        return _cmd_run(args)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - argparse exits


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
