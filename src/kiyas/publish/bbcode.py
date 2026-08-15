"""Turning a published comparison into forum markup.

The formats are named after the markup they produce, never after a site that
accepts it. kiyas is not tied to any particular forum, and a format called
after one would go stale the moment another site adopted the same tag.

``comparison``
    ``[comparison=A,B,C]url url url[/comparison]`` -- one tag holding every
    image, grouped so the reader can flip between sources at the same frame.
    Images are listed frame-major: all sources of frame 1, then all of frame 2.
``img``
    A plain ``[img]`` list, for software that has no comparison tag.
``markdown``
    For issue trackers and anywhere else that is not a forum.
"""

from __future__ import annotations

from collections.abc import Sequence

from .manifest import Comparison

FORMATS = ("comparison", "img", "markdown")


class BBCodeError(ValueError):
    """Raised when markup cannot be produced from what was given."""


def _check(comparison: Comparison, urls: Sequence[str]) -> None:
    expected = comparison.total_images
    if len(urls) != expected:
        raise BBCodeError(
            f"got {len(urls)} image URLs for {expected} images. The markup would "
            f"silently pair the wrong pictures with the wrong sources."
        )


def _row_major(comparison: Comparison, urls: Sequence[str]) -> list[list[str]]:
    """Regroup source-major URLs into one list per row.

    kiyas holds images as ``sources[source][row]`` because that is how they are
    captured and stored. Every forum comparison tag wants the opposite:
    consecutive images are the *same frame* from different sources, which is
    what makes flipping between them meaningful.
    """
    row_count = len(comparison.rows)
    source_count = len(comparison.sources)

    grouped: list[list[str]] = []
    for row_index in range(row_count):
        row = []
        for source_index in range(source_count):
            row.append(urls[source_index * row_count + row_index])
        grouped.append(row)
    return grouped


def comparison_tag(comparison: Comparison, urls: Sequence[str]) -> str:
    _check(comparison, urls)
    names = ",".join(source.name for source in comparison.sources)
    flat = [url for group in _row_major(comparison, urls) for url in group]
    return f"[comparison={names}]" + "\n".join(flat) + "[/comparison]"


def img_list(comparison: Comparison, urls: Sequence[str]) -> str:
    _check(comparison, urls)
    lines: list[str] = []
    for row, urls_for_row in zip(comparison.rows, _row_major(comparison, urls), strict=True):
        lines.append(f"[b]{row.label}[/b]")
        for source, url in zip(comparison.sources, urls_for_row, strict=True):
            lines.append(f"{source.name}: [img]{url}[/img]")
        lines.append("")
    return "\n".join(lines).strip()


def markdown(comparison: Comparison, urls: Sequence[str]) -> str:
    _check(comparison, urls)
    lines = [f"## {comparison.title}", ""]
    for row, urls_for_row in zip(comparison.rows, _row_major(comparison, urls), strict=True):
        lines.append(f"### {row.label}")
        for source, url in zip(comparison.sources, urls_for_row, strict=True):
            lines.append(f"- **{source.name}**: {url}")
        lines.append("")
    return "\n".join(lines).strip()


def render(comparison: Comparison, urls: Sequence[str], fmt: str) -> str:
    if fmt not in FORMATS:
        raise BBCodeError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    return {"comparison": comparison_tag, "img": img_list, "markdown": markdown}[fmt](
        comparison, urls
    )


def collection_link(comparison: Comparison, url: str) -> str:
    """The short form: a single link to the hosted comparison.

    Almost always the right thing to post. slow.pics already does the
    side-by-side flipping better than a forum's own tag, and one link does not
    go stale when a rehost expires.
    """
    return f"[url={url}]{comparison.title} — {' vs '.join(comparison.source_names)}[/url]"
