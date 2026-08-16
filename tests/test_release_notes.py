"""The release workflow's only piece of logic, tested here rather than at tag time.

Everything else in `.github/workflows/release.yml` is a command that either
runs or does not. This one makes a decision, and it makes it once a version,
on a runner, with nobody watching -- so it is worth pinning down here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Not an installed module: packaging/ is build scaffolding that the frozen
# build must not contain, so it is loaded by path instead of imported.
_spec = importlib.util.spec_from_file_location(
    "release_notes", ROOT / "packaging" / "release_notes.py"
)
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)


@pytest.fixture
def repo(tmp_path):
    """A miniature checkout: a version and a changelog with two sections."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "kiyas"\nversion = "1.2.0"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## 1.2.0\n\nWhat is new.\n\n### A subsection\n\nStill 1.2.0.\n\n"
        "## 1.1.0\n\nThe one before.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_notes_are_the_section_for_that_tag(repo):
    body = release_notes.notes("v1.2.0", root=repo)

    assert body.startswith("What is new.")
    # A `###` heading is part of the section, not the start of the next one.
    assert "Still 1.2.0." in body
    assert "The one before." not in body


def test_the_v_is_optional(repo):
    assert release_notes.notes("1.2.0", root=repo) == release_notes.notes("v1.2.0", root=repo)


def test_a_tag_that_disagrees_with_the_package_is_refused(repo):
    """The failure this whole script exists to prevent."""
    with pytest.raises(ValueError, match="1.2.0"):
        release_notes.notes("v2.0.0", root=repo)


def test_a_missing_section_stops_the_release(repo):
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kiyas"\nversion = "9.9.9"\n', encoding="utf-8"
    )

    with pytest.raises(LookupError):
        release_notes.notes("v9.9.9", root=repo)


def test_this_repository_can_release_its_own_version():
    """The real files, so a forgotten changelog entry fails here first."""
    version = release_notes.declared_version(ROOT / "pyproject.toml")

    assert release_notes.notes(f"v{version}").strip()


def test_the_written_file_is_utf8(tmp_path):
    """The workflow hands this file to `gh release create`.

    Written by Python rather than by a shell redirect, because the changelog
    has em dashes in it and PowerShell 5 and 7 disagree about what `>` encodes.
    """
    out = tmp_path / "notes.md"
    version = release_notes.declared_version(ROOT / "pyproject.toml")

    assert release_notes.main(["release_notes.py", f"v{version}", str(out)]) == 0
    assert "—" in out.read_bytes().decode("utf-8")


def test_a_bad_tag_fails_the_run_rather_than_writing_notes(tmp_path):
    out = tmp_path / "notes.md"

    assert release_notes.main(["release_notes.py", "v0.0.0-nope", str(out)]) == 1
    assert not out.exists()
