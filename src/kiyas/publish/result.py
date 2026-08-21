"""What a publish attempt hands back, whatever it published to.

These live apart from any one backend because the CLI catches a single error
type and prints a single result regardless of where the comparison went. Two
parallel hierarchies would mean every call site naming every backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class UploadError(RuntimeError):
    """Raised when a comparison cannot be published."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    key: str
    url: str
    uploaded: int
    skipped: int
    #: Direct URL per image, in the order kiyas holds them: source-major, so
    #: ``urls[source_index * row_count + row_index]``. That is the order
    #: ``bbcode`` indexes, and getting it wrong pairs the wrong picture with
    #: the wrong source without erroring.
    #:
    #: Empty when the host does not hand back per-image addresses, which is why
    #: it has a default: a backend that cannot fill it in should not have to
    #: say so. Callers branch on whether it is empty, never on the backend name.
    image_urls: tuple[str, ...] = field(default=())
    #: Things the server did differently from what was asked, in its own
    #: terms. Not warnings about the request -- those are raised before it is
    #: sent -- but observations about the answer, which is the only place a
    #: silent substitution can be caught.
    notes: tuple[str, ...] = field(default=())
