from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

from kiyas import setup_env

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_vs_extra() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]["vs"]


def test_fallback_list_matches_pyproject():
    """The hardcoded fallback exists for source checkouts; it must not drift.

    setup_env falls back to a literal list when kiyas' own metadata is not
    readable. If that list and the ``vs`` extra disagree, a source checkout
    installs a different stack than an installed copy -- and the difference
    would only surface as a missing plugin much later.
    """
    assert sorted(setup_env._FALLBACK_VS_PACKAGES) == sorted(_declared_vs_extra())


def test_vs_packages_returns_something_installable():
    packages = setup_env.vs_packages()

    assert packages
    assert any(p.startswith("vapoursynth") for p in packages)
    # Nothing may leak in from another extra.
    assert not any("PySide6" in p or "matplotlib" in p for p in packages)


def test_vs_packages_falls_back_when_metadata_missing(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def boom(_name):
        raise PackageNotFoundError("kiyas")

    monkeypatch.setattr(setup_env, "requires", boom)

    assert list(setup_env.vs_packages()) == list(setup_env._FALLBACK_VS_PACKAGES)


def test_vs_packages_ignores_other_extras(monkeypatch):
    monkeypatch.setattr(
        setup_env,
        "requires",
        lambda _name: [
            "rich>=13",
            'vapoursynth>=78; extra == "vs"',
            'vs-placebo; extra == "vs"',
            'PySide6>=6.7; extra == "gui"',
            'pytest>=8.3; extra == "dev"',
        ],
    )

    assert setup_env.vs_packages() == ["vapoursynth>=78", "vs-placebo"]


def test_is_virtualenv_detects_this_environment():
    expected = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    assert setup_env.is_virtualenv() is (expected or hasattr(sys, "real_prefix"))


def test_ensure_isolated_refuses_a_system_interpreter(monkeypatch):
    """Installing VapourSynth into a system Python affects every other project."""
    monkeypatch.setattr(setup_env, "is_virtualenv", lambda: False)
    monkeypatch.delenv("KIYAS_ALLOW_GLOBAL_INSTALL", raising=False)

    with pytest.raises(setup_env.SetupError):
        setup_env.ensure_isolated()


def test_ensure_isolated_allows_explicit_override(monkeypatch):
    monkeypatch.setattr(setup_env, "is_virtualenv", lambda: False)
    monkeypatch.setenv("KIYAS_ALLOW_GLOBAL_INSTALL", "1")

    setup_env.ensure_isolated()  # must not raise


def test_ensure_isolated_passes_inside_a_virtualenv(monkeypatch):
    monkeypatch.setattr(setup_env, "is_virtualenv", lambda: True)

    setup_env.ensure_isolated()  # must not raise


def _executable_string_literals(source: str) -> list[str]:
    """Every string constant in ``source`` except docstrings.

    Docstrings are excluded because setup_env's own docstring names the
    forbidden subcommands in order to explain why they are not used -- prose
    about a rule must not trip the test that enforces it. Ordinary string
    literals are kept, because that is exactly how a subcommand would reach a
    subprocess call.
    """
    tree = ast.parse(source)

    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                docstring_nodes.add(id(first.value))

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


def test_configure_never_registers_system_wide():
    """`vapoursynth register-install` writes HKCU\\SOFTWARE\\VapourSynth.

    kiyas keeps its whole stack inside the virtualenv, so those subcommands
    must never be passed to a subprocess. This is a source-level assertion on
    purpose: the failure it guards against is someone "helpfully" adding the
    call later, which would silently break the isolation guarantee the README
    makes.
    """
    source = Path(setup_env.__file__).read_text(encoding="utf-8")
    literals = _executable_string_literals(source)

    for forbidden in ("register-install", "register-legacy-install", "register-vfw"):
        offenders = [text for text in literals if forbidden in text]
        assert offenders == [], f"{forbidden} reachable from code: {offenders}"


def test_the_registry_guard_can_actually_fail():
    """Guard the guard.

    A source-level assertion that cannot fail is worse than none, because it
    reads as protection. This feeds the checker a module that does the
    forbidden thing and confirms it is caught.
    """
    bad = '"""Docstring mentioning register-vfw is fine."""\nrun(["vapoursynth", "register-vfw"])\n'

    literals = _executable_string_literals(bad)

    assert any("register-vfw" in text for text in literals)


def test_the_packaged_build_says_it_cannot_install(monkeypatch):
    """The generic virtualenv advice is impossible to follow when frozen.

    There is no pip and no interpreter to point one at, so the honest answer
    is that the packaged build has the engines it has.
    """
    monkeypatch.setattr(setup_env, "is_frozen", lambda: True)

    with pytest.raises(setup_env.SetupError, match="packaged build"):
        setup_env.ensure_isolated()


def test_the_packaged_build_is_not_told_to_run_setup(monkeypatch):
    from kiyas import doctor

    monkeypatch.setattr(setup_env, "is_frozen", lambda: True)

    hint = doctor._install_vs_hint()  # noqa: SLF001

    assert "kiyas setup" not in hint
    assert "bootstrap" in hint


def test_a_checkout_is_still_told_to_run_setup():
    from kiyas import doctor

    assert "kiyas setup" in doctor._install_vs_hint()  # noqa: SLF001
