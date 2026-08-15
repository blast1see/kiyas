"""Render variants: the second comparison axis.

A source comparison asks "which of these files is better". A settings
comparison asks "how should my player be configured", and it is the same
picture rendered several ways. The templates here are the questions people
actually argue about, pre-written so that the common case is one line of
project file instead of twelve.

Nothing here validates that an option exists -- mpv's own list changes between
versions and duplicating it would go stale. mpv rejects a bad option at startup
and the session reports what it said. What *is* validated is the small set of
options that would break kiyas' isolation guarantee if a project file set them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class VariantError(ValueError):
    """Raised when a variant or template cannot be understood."""


#: Options a project file may not set, because they would take mpv out of the
#: sandbox kiyas puts it in.
#:
#: This is not paranoia about hostile input; it is about a project file being
#: shareable. Someone else's ``config-dir`` line would silently pull that
#: person's whole player configuration into your comparison, and the result
#: would look fine.
FORBIDDEN_OPTIONS = frozenset(
    {
        "config",
        "config-dir",
        "no-config",
        "include",
        "input-conf",
        "input-ipc-server",
        "load-scripts",
        "script",
        "scripts",
        "script-opts",
        "load-auto-profiles",
        "profile",
        "term-playing-msg",
        "terminal",
    }
)


@dataclass(frozen=True, slots=True)
class Variant:
    """One named way of rendering the picture."""

    name: str
    options: dict[str, str] = field(default_factory=dict)

    def merged(self, base: Mapping[str, str]) -> Variant:
        """This variant's options laid over a shared base."""
        combined = dict(base)
        combined.update(self.options)
        return Variant(self.name, combined)


def normalise_options(table: Any, where: str) -> dict[str, str]:
    """Turn a TOML table into mpv option strings, rejecting unsafe names."""
    if not isinstance(table, dict):
        raise VariantError(f"{where}: options must be a table of mpv settings")
    options: dict[str, str] = {}
    for raw_name, raw_value in table.items():
        name = str(raw_name).strip().lstrip("-")
        if not name:
            raise VariantError(f"{where}: an option name cannot be empty")
        if name in FORBIDDEN_OPTIONS:
            raise VariantError(
                f"{where}: '{name}' cannot be set from a project file. It would change which "
                f"configuration mpv reads, and kiyas guarantees that your own mpv profile is "
                f"never involved in a comparison."
            )
        if isinstance(raw_value, bool):
            value = "yes" if raw_value else "no"
        elif isinstance(raw_value, list):
            value = ",".join(str(item) for item in raw_value)
        else:
            value = str(raw_value)
        if "\n" in value or "\r" in value:
            raise VariantError(f"{where}: the value for '{name}' must be a single line")
        options[name] = value
    return options


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------


def _simple(option: str, values: tuple[str, ...]):
    def build(table: Mapping[str, Any]) -> list[Variant]:  # noqa: ARG001 - uniform signature
        return [Variant(value, {option: value}) for value in values]

    return build


def _shaders(table: Mapping[str, Any]) -> list[Variant]:
    """One variant per shader file, plus an unshaded control.

    The control is not optional. A shader comparison without "none" in it can
    only tell you which shader you prefer, not whether any of them beat leaving
    it alone, and that second question is the one that decides whether the
    shader belongs in a config at all.
    """
    raw = table.get("shaders")
    if not isinstance(raw, list) or not raw:
        raise VariantError(
            "template = 'shaders' needs a 'shaders' list of .glsl paths in [settings]. "
            "Shader files are read where they are; kiyas never copies or edits them."
        )
    variants = [Variant("no shader", {"glsl-shaders": ""})]
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise VariantError(f"[settings]: every entry in 'shaders' must be a path, got {item!r}")
        path = Path(item.strip()).expanduser()
        variants.append(Variant(path.stem, {"glsl-shaders": path.as_posix()}))
    return variants


def _deband(table: Mapping[str, Any]) -> list[Variant]:  # noqa: ARG001 - uniform signature
    """Debanding off, then increasingly aggressive.

    Values follow mpv's own defaults (1 iteration, threshold 48, range 16) as
    the middle rung, so "default" here really is what a stock player does.
    """
    return [
        Variant("off", {"deband": "no"}),
        Variant("default", {"deband": "yes", "deband-iterations": "1", "deband-threshold": "48"}),
        Variant("strong", {"deband": "yes", "deband-iterations": "2", "deband-threshold": "64"}),
        Variant(
            "very strong", {"deband": "yes", "deband-iterations": "4", "deband-threshold": "96"}
        ),
    ]


#: name -> builder. Every builder takes the ``[settings]`` table and returns
#: variants; most ignore it.
TEMPLATES: dict[str, Any] = {
    # The curves people compare on HDR material. libplacebo's defaults change
    # between releases, so each variant names its curve explicitly rather than
    # leaving one of them as "whatever mpv does today".
    "tonemap": _simple("tone-mapping", ("spline", "bt.2390", "bt.2446a", "st2094-10", "st2094-40")),
    "gamut": _simple(
        "gamut-mapping-mode", ("clip", "perceptual", "relative", "desaturate", "darken")
    ),
    "scalers": _simple(
        "scale", ("spline36", "lanczos", "ewa_lanczossharp", "ewa_lanczos4sharpest", "bicubic")
    ),
    "dscale": _simple("dscale", ("mitchell", "catmull_rom", "hermite", "box", "lanczos")),
    "deband": _deband,
    "shaders": _shaders,
}


def expand_template(name: str, table: Mapping[str, Any] | None = None) -> list[Variant]:
    """Build the variants a named template stands for."""
    builder = TEMPLATES.get(name)
    if builder is None:
        raise VariantError(f"unknown template {name!r}. Available: {', '.join(sorted(TEMPLATES))}")
    return builder(table or {})


def describe_templates() -> list[tuple[str, str]]:
    """``(name, summary)`` for every template, for `kiyas templates`."""
    described = []
    for name in sorted(TEMPLATES):
        if name == "shaders":
            described.append((name, "one variant per .glsl file, plus an unshaded control"))
            continue
        variants = expand_template(name, {})
        option = next(iter(variants[0].options))
        described.append((name, f"--{option}: {', '.join(v.name for v in variants)}"))
    return described
