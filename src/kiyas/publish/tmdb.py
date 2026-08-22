"""Turning a film's name into the reference slow.pics wants.

``--tmdb`` takes ``MOVIE_1275779``, and nobody knows that number. Looking it up
means leaving the terminal, finding the title on a website and copying a
number out of its URL, for a field that is optional -- so in practice it does
not get filled in.

This resolves a name to that number, and refuses rather than guesses. Two
releases of "The Prestige" are the same film; "Dune" is two films twenty years
apart, and picking one of them silently would attach a comparison to the wrong
title. When the answer is not obvious the candidates are printed and the user
passes the id they meant.

The key comes from the environment for the same reason comp.pics' does: kiyas
has no credential store and is not gaining one for a field that is optional.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

BASE_URL = "https://api.themoviedb.org/3"

#: Where the key is read from. TMDB issues one free to any account.
API_KEY_ENV = "KIYAS_TMDB_API_KEY"

#: A search is one request against a documented API; it should not hang a run.
_TIMEOUT = 20.0

#: How many candidates are worth printing when the answer is not obvious.
#: More than this and the name was too vague to be worth searching for.
MAX_CANDIDATES = 8


class TmdbError(RuntimeError):
    """Raised when a title cannot be resolved to a reference."""


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: str
    tmdb_id: int
    title: str
    year: str

    @property
    def reference(self) -> str:
        """The form slow.pics wants, which is what `--tmdb` takes."""
        return f"{self.kind.upper()}_{self.tmdb_id}"

    def __str__(self) -> str:
        year = f" ({self.year})" if self.year else ""
        return f"{self.reference}  {self.title}{year}"


def api_key(configured: str | None = None) -> str | None:
    return configured or os.environ.get(API_KEY_ENV) or None


def _candidates(payload: dict, kind: str) -> list[Candidate]:
    out = []
    for item in payload.get("results") or []:
        identifier = item.get("id")
        if not isinstance(identifier, int):
            continue
        title = item.get("title") or item.get("name") or ""
        released = item.get("release_date") or item.get("first_air_date") or ""
        out.append(Candidate(kind, identifier, str(title), str(released)[:4]))
    return out


def search(title: str, *, key: str | None = None, session=None) -> list[Candidate]:
    """Films and series matching ``title``, best first.

    ``session`` exists so the tests can drive this without a network, matching
    what the publishing backends do.
    """
    resolved = api_key(key)
    if not resolved:
        raise TmdbError(
            f"searching TMDB by name needs an API key. TMDB issues one free to any "
            f"account; put it in {API_KEY_ENV}. Or pass the reference directly, as "
            f"--tmdb MOVIE_1275779."
        )

    text = title.strip()
    if not text:
        raise TmdbError("nothing to search for")

    import requests

    client = session or requests.Session()
    found: list[Candidate] = []
    # Both endpoints, because a comparison is as likely to be of a series as of
    # a film and slow.pics distinguishes them. The multi-search endpoint would
    # be one request, but it also returns people, which is never the answer.
    for kind, endpoint in (("movie", "search/movie"), ("tv", "search/tv")):
        try:
            response = client.get(
                f"{BASE_URL}/{endpoint}",
                params={"api_key": resolved, "query": text},
                timeout=_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - requests raises a family of these
            raise TmdbError(f"could not reach TMDB: {exc}") from exc

        if response.status_code == 401:
            raise TmdbError(f"TMDB refused the key in {API_KEY_ENV}.")
        if response.status_code != 200:
            raise TmdbError(f"TMDB answered {response.status_code} searching for {text!r}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TmdbError("TMDB returned something that is not JSON") from exc
        found.extend(_candidates(payload, kind))

    return found


def resolve(title: str, *, key: str | None = None, session=None) -> str:
    """The single reference for ``title``, or an error naming the alternatives.

    One candidate is the answer. Several is a question, and answering it by
    taking the first would attach the comparison to whichever film TMDB happens
    to rank higher -- which is exactly the kind of wrong that never gets
    noticed, because the number is correct-looking and nobody checks it.
    """
    found = search(title, key=key, session=session)
    if not found:
        raise TmdbError(f"TMDB has nothing called {title!r}")
    if len(found) == 1:
        return found[0].reference

    listed = "\n  ".join(str(candidate) for candidate in found[:MAX_CANDIDATES])
    more = "" if len(found) <= MAX_CANDIDATES else f"\n  ...and {len(found) - MAX_CANDIDATES} more"
    raise TmdbError(
        f"{len(found)} titles match {title!r}. Pass the one you mean as --tmdb:\n  {listed}{more}"
    )
