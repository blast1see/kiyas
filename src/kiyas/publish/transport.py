"""Shared machinery for talking to a publishing host, whichever one it is.

Two jobs live here, both of them the same job on every backend.

**Reading a refused response.**

Both publishing targets sit behind Cloudflare, and a Cloudflare refusal looks
nothing like an API error: the body is an HTML interstitial about browsers and
JavaScript, and quoting it verbatim hands someone six hundred characters of
markup describing a problem they do not have. Telling the two apart is the same
job whichever host is being talked to, so it lives here and takes the host name
as an argument rather than being written twice.

**Pacing, backoff and timeouts.** How fast a burst of uploads is allowed to
start, how long to wait after being refused, and how long one image body is
given, are properties of pushing a few dozen multi-megabyte PNGs down one
uplink -- not of any particular host. They were written twice and were
byte-identical both times; a third backend is what made copying them a third
time obviously wrong. The numbers that *are* host-specific -- how many workers,
how many attempts, how far apart -- stay in the backend that measured them.
"""

from __future__ import annotations

import random
import re
import threading
import time

#: How much of a failed response to quote back. Enough for a JSON field error,
#: short of an HTML error page.
_ERROR_BODY_LIMIT = 600


def _server_said(response) -> str:
    """The body of a failed response, which is where the reason lives.

    ``raise_for_status`` produces "400 Client Error: Bad Request for url: ...",
    which says a field was wrong and not which one. The server does say, in the
    body, and that was being thrown away -- leaving a caller with a failure they
    cannot act on and no way to find out more except reading this source.

    Truncated because a server having a bad day can answer with an HTML error
    page, and a wall of markup in a one-line error is its own kind of unhelpful.
    """
    if response is None:
        return ""
    body = (getattr(response, "text", "") or "").strip()
    if not body:
        return ""
    if len(body) > _ERROR_BODY_LIMIT:
        body = body[:_ERROR_BODY_LIMIT] + "..."
    return "\nthe server said: " + body


#: Cloudflare's ray id, which is the handle support asks for. Taken from the
#: header rather than the body: the body prints it beside the visitor's own IP
#: address, and an error message people paste into bug reports should not carry
#: that.
_CF_RAY = re.compile(r"[0-9a-f]{16}-[A-Z]{3}")

#: Cloudflare's own numbering, as it appears in the page *source* rather than
#: as it reads on screen. The heading says "Error 1006" to a person but is
#: markup to a regex -- ``<span data-translate="error">Error</span>`` and
#: ``<span>1006</span>`` are separate elements with a newline between them --
#: so a pattern written against the rendered text matches nothing at all. What
#: does survive is the number's machine-readable company: ``errorCode: 1006``
#: in the feedback script, and ``.../cloudflare-1xxx-errors/error-1006/`` in
#: the link to the documentation. Both are a single token.
#:
#: This was written against the rendered text the first time, shipped passing
#: its own tests, and matched nothing on the real page. It is why the fixture
#: in the tests is raw markup copied from a refusal and not prose.
_CF_CODE = re.compile(r"""error[\s_-]*(?:code)?["']?\s*[:=-]?\s*(1\d{3})\b""", re.IGNORECASE)

#: What Cloudflare's numbers mean, for the ones a publisher can actually hit.
#: ``{host}`` is filled in by the caller, because the reason belongs to the site
#: whose address was refused and naming the wrong one sends people to the wrong
#: place to ask about it.
#:
#: 1006 reads as a permanent decision -- Cloudflare's own page says "the owner
#: of this website has banned your IP address" -- and it was described that way
#: here at first. Measured instead: an address refused with 1006 was serving
#: 200s again the same day, without anyone being asked. On these sites it is an
#: automatic rule with a lifetime, not a person's decision, so the advice that
#: goes with it is to wait rather than to go and find another network.
_CF_MEANINGS = {
    "1006": "{host} has temporarily banned this IP address",
    "1007": "{host} has temporarily banned this IP address",
    "1008": "{host} has temporarily banned this IP address",
    "1015": "this IP address is being rate limited",
    "1020": "an access rule on {host} refused the request",
}

#: Fallback when the page carries no number: what each status means when it is
#: the edge answering rather than the site.
_EDGE_REASONS = {
    403: "this IP address is blocked or rate limited",
    429: "too many requests from this IP address",
    503: "the edge is asking for a browser challenge kiyas cannot answer",
}


def edge_block(response, *, host: str) -> str:
    """One sentence for a Cloudflare refusal, or "" if this is not one.

    The thing the caller needs to know is that the site never saw the request,
    that the refusal is keyed to their address rather than to anything in the
    comparison, and that sending it again is not the answer. Measured: a run
    that uploads cleanly can be refused minutes later from the same address.
    """
    if response is None:
        return ""
    status = getattr(response, "status_code", 0) or 0
    if status < 400:
        return ""
    body = getattr(response, "text", "") or ""
    head = body[:4000].lower()
    if "<html" not in head or "cloudflare" not in head:
        return ""

    code = _CF_CODE.search(body)
    number = code.group(1) if code else ""
    template = _CF_MEANINGS.get(number)
    reason = (
        template.format(host=host) if template else _EDGE_REASONS.get(status) or f"HTTP {status}"
    )

    marks = [f"error {number}"] if number else []
    ray = _CF_RAY.search(str((getattr(response, "headers", None) or {}).get("cf-ray", "")))
    if ray:
        marks.append(f"ray {ray.group(0)}")
    detail = f" ({', '.join(marks)})" if marks else ""

    # Both kinds lapse, so the advice is to wait either way; what differs is
    # how long, and a ban is worth naming as the more serious of the two so
    # nobody spends the wait re-running the command.
    remedy = (
        "It clears by itself -- an address refused this way was serving "
        "requests again the same day -- so leave it a while rather than "
        "retrying. Another network works in the meantime."
        if number in {"1006", "1007", "1008"}
        else "Wait for it to lapse, or publish from a different network."
    )
    return (
        f"\nThis is Cloudflare, not {host}: {reason}{detail}. "
        f"The site never saw the request, so nothing in the comparison caused it "
        f"and sending it again will not help. {remedy}"
    )


def explain(response, *, host: str) -> str:
    """Why a request was refused: the edge's reason if it was the edge, else the body."""
    return edge_block(response, host=host) or _server_said(response)


# ---------------------------------------------------------------------------
# Pacing, backoff and timeouts
# ---------------------------------------------------------------------------

#: Ceiling for the retry backoff. Without one, exponential growth puts the last
#: attempt minutes away and the run looks hung.
MAX_BACKOFF = 30.0


def upload_timeout(size_bytes: int) -> float:
    """Seconds to allow for one image, sized to the image.

    One constant used to cover both the small API calls and the image bodies,
    and thirty seconds is generous for the first and hopeless for the second.
    Measured on a real comparison: 24 screenshots of about 6 MB each, nine of
    them lost to "the write operation timed out".

    The arithmetic that matters is the parallelism. Six uploads share one
    uplink, so each gets roughly a sixth of it, and a 6 MB image at a sixth of
    a modest home connection is already at thirty seconds before anything goes
    wrong. This allows about 34 kB/s per stream -- slow, but a slow link should
    finish rather than fail, because failing costs the whole run.
    """
    return max(90.0, 30.0 * size_bytes / 1_000_000)


def backoff(attempt: int, *, ceiling: float = MAX_BACKOFF) -> float:
    """Seconds to wait before retry ``attempt``, growing and jittered.

    Jitter matters more than the growth. Workers that hit the same rate limit
    at the same moment will, with a fixed delay, wake up together and reproduce
    the burst that caused it. The random half spreads them out.
    """
    bound = min(ceiling, 2.0 * 2**attempt)
    return bound * (0.5 + random.random() / 2)


def retry_after(header: str | None, attempt: int, *, ceiling: float = MAX_BACKOFF) -> float | None:
    """Honour the server's Retry-After, or ``None`` if it asked for too long.

    The number cannot simply be clamped: retrying sooner than the server asked
    is how a rate limit becomes a ban, which is the outcome this whole module
    is arranged around. Waiting on it is not right either -- five attempts at
    an hour each is a run that looks hung for most of a day, which is exactly
    what `MAX_BACKOFF` exists to prevent for the local backoff.

    So a wait past the ceiling is neither retried nor slept on. This module
    already holds that a refusal is never retried, because further attempts
    against a block cannot succeed and are themselves the traffic that earns
    one; a Retry-After this long is the same answer with a number attached.

    Anything unparseable falls through to the jittered local backoff, which is
    bounded by the ceiling already.
    """
    try:
        wait = max(1.0, float(header))
    except (TypeError, ValueError):
        return backoff(attempt, ceiling=ceiling)
    return wait if wait <= ceiling else None


def too_long_to_wait(header: str | None, *, host: str) -> str:
    """What to say when a host asks for a longer wait than kiyas will make.

    Worded once here rather than in each backend, for the same reason reading a
    Cloudflare refusal is: the situation is identical whichever host it is.
    """
    return (
        f"{host} asked for {header} seconds before the next request. That is not a "
        f"burst to wait out, so nothing further was sent. What is on disk is "
        f"untouched -- publish it again later, or from another network."
    )


class Pacer:
    """Keeps request starts a minimum interval apart, across every worker.

    The thread pool limits how many uploads run at once; this limits how fast
    they are allowed to *begin*. Those are different things, and only the
    second one is visible to the server as a burst.

    The sleep happens outside the lock on purpose. Holding it while waiting
    would make the workers queue up behind each other and turn the pool back
    into a single stream.
    """

    __slots__ = ("_interval", "_lock", "_next")

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            time.sleep(delay)
