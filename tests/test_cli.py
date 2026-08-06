from __future__ import annotations

import pytest

from kiyas import cli


def test_version_flag_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert "kiyas" in capsys.readouterr().out


def test_no_command_prints_help_and_succeeds(capsys):
    assert cli.main([]) == 0
    assert "kiyas" in capsys.readouterr().out


def test_setup_reports_a_clear_error_outside_a_virtualenv(monkeypatch, capsys):
    from kiyas import setup_env

    monkeypatch.setattr(setup_env, "is_virtualenv", lambda: False)
    monkeypatch.delenv("KIYAS_ALLOW_GLOBAL_INSTALL", raising=False)

    assert cli.main(["setup"]) == 2

    stderr = capsys.readouterr().err
    assert "venv" in stderr.lower()


def test_importing_cli_does_not_pull_in_heavy_dependencies():
    """``kiyas doctor`` has to run before anything is installed.

    If importing the CLI imported VapourSynth or PySide6, the one command whose
    job is to report that they are missing would itself fail.
    """
    import subprocess
    import sys

    probe = (
        "import sys, kiyas.cli;"
        "heavy = {'vapoursynth', 'PySide6', 'matplotlib', 'scipy', 'numpy'};"
        "loaded = heavy & set(sys.modules);"
        "print(sorted(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"


def test_doctor_subcommand_is_registered():
    parser = cli.build_parser()
    args = parser.parse_args(["doctor"])

    assert args.command == "doctor"


def test_setup_upgrade_flag_is_parsed():
    parser = cli.build_parser()
    args = parser.parse_args(["setup", "--upgrade"])

    assert args.command == "setup"
    assert args.upgrade is True
