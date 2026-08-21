"""Reading a refused HTTP response, for any host kiyas publishes to.

Both publishing targets sit behind Cloudflare, and a Cloudflare refusal looks
nothing like an API error: the body is an HTML interstitial about browsers and
JavaScript, and quoting it verbatim hands someone six hundred characters of
markup describing a problem they do not have. Telling the two apart is the same
job whichever host is being talked to, so it lives here and takes the host name
as an argument rather than being written twice.
"""

from __future__ import annotations

import re

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
