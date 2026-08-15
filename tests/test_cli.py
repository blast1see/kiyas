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


def test_templates_subcommand_is_registered():
    parser = cli.build_parser()
    args = parser.parse_args(["templates"])

    assert args.command == "templates"


def test_templates_lists_every_template(capsys):
    from kiyas.mpvctl.variants import TEMPLATES

    assert cli.main(["templates"]) == 0

    printed = capsys.readouterr().out
    for name in TEMPLATES:
        assert name in printed


def test_pick_subcommand_takes_a_file_and_a_start_frame():
    parser = cli.build_parser()
    args = parser.parse_args(["pick", "film.mkv", "--start", "1200"])

    assert args.command == "pick"
    assert args.path.name == "film.mkv"
    assert args.start == 1200


def test_pick_on_a_missing_file_fails_before_starting_a_player(tmp_path, capsys):
    code = cli.main(["pick", str(tmp_path / "nope.mkv")])

    assert code == 2
    assert "no such file" in capsys.readouterr().out


def test_init_can_scaffold_a_settings_comparison(tmp_path, capsys):
    path = tmp_path / "settings.toml"

    assert cli.main(["init", str(path), "--settings"]) == 0

    text = path.read_text(encoding="utf-8")
    assert 'mode = "settings"' in text
    assert "[settings]" in text
    assert "template" in text


def test_the_scaffolded_settings_project_is_valid_apart_from_the_paths(tmp_path):
    """A starter file that does not parse is worse than no starter file."""
    import tomllib

    from kiyas import config

    path = tmp_path / "settings.toml"
    cli.main(["init", str(path), "--settings"])
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    project = config.parse(data)

    assert project.mode.value == "settings"
    assert len(project.settings.variants) > 1
