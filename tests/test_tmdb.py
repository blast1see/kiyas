"""Resolving a film's name to the reference slow.pics wants.

Nothing here talks to TMDB, for the same reason nothing in ``test_publish.py``
talks to slow.pics: the failure that matters is not "the request failed", it is
"the request succeeded and attached the comparison to the wrong film". Two
titles come back for "Dune" and taking the first one silently is a number that
looks correct and is not.

The session is passed in rather than patched, matching the publishing
backends. The payloads are the shapes TMDB documents.
"""

from __future__ import annotations

import pytest

from kiyas.publish import tmdb
from kiyas.publish.tmdb import TmdbError


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """Answers the two search endpoints with whatever the test wants."""

    def __init__(self, *, movies=(), shows=(), status_code=200, payload=...):
        self.movies = list(movies)
        self.shows = list(shows)
        self.status_code = status_code
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.payload is not ...:
            return _Response(self.payload, self.status_code)
        results = self.movies if url.endswith("search/movie") else self.shows
        return _Response({"results": results}, self.status_code)


def _movie(identifier, title, released="2006-10-19"):
    return {"id": identifier, "title": title, "release_date": released}


def _show(identifier, name, first="2011-04-17"):
    return {"id": identifier, "name": name, "first_air_date": first}


KEY = "k" * 32


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------


def test_one_match_resolves_to_the_reference_slowpics_wants():
    session = _FakeSession(movies=[_movie(1124, "The Prestige")])

    assert tmdb.resolve("The Prestige", key=KEY, session=session) == "MOVIE_1124"


def test_a_series_keeps_its_own_kind():
    """slow.pics distinguishes them, so guessing 'movie' would be wrong."""
    session = _FakeSession(shows=[_show(1399, "Game of Thrones")])

    assert tmdb.resolve("Game of Thrones", key=KEY, session=session) == "TV_1399"


def test_several_matches_are_a_question_not_an_answer():
    """ "Dune" is two films twenty years apart.

    Taking whichever TMDB ranks higher attaches the comparison to one of them
    with a number that looks perfectly correct, and nobody goes back to check.
    """
    session = _FakeSession(
        movies=[_movie(841, "Dune", "1984-12-14"), _movie(438631, "Dune", "2021-09-15")]
    )

    with pytest.raises(TmdbError) as raised:
        tmdb.resolve("Dune", key=KEY, session=session)

    message = str(raised.value)
    assert "MOVIE_841" in message
    assert "MOVIE_438631" in message
    assert "1984" in message and "2021" in message


def test_a_film_and_a_series_of_the_same_name_are_also_a_question():
    session = _FakeSession(movies=[_movie(1, "Fargo")], shows=[_show(2, "Fargo")])

    with pytest.raises(TmdbError, match="2 titles match"):
        tmdb.resolve("Fargo", key=KEY, session=session)


def test_nothing_found_says_so():
    with pytest.raises(TmdbError, match="nothing called"):
        tmdb.resolve("qwertyuiop", key=KEY, session=_FakeSession())


def test_a_long_list_is_truncated_rather_than_printed_whole():
    session = _FakeSession(movies=[_movie(n, f"Love {n}") for n in range(1, 21)])

    with pytest.raises(TmdbError) as raised:
        tmdb.resolve("Love", key=KEY, session=session)

    assert "and 12 more" in str(raised.value)


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------


def test_no_key_names_the_variable_and_the_way_round_it(monkeypatch):
    """The reference form still works without a key, and the message says so."""
    monkeypatch.delenv(tmdb.API_KEY_ENV, raising=False)

    with pytest.raises(TmdbError) as raised:
        tmdb.resolve("The Prestige", session=_FakeSession())

    message = str(raised.value)
    assert tmdb.API_KEY_ENV in message
    assert "MOVIE_1275779" in message


def test_the_key_comes_from_the_environment(monkeypatch):
    """Same place comp.pics' key comes from: kiyas has no credential store."""
    monkeypatch.setenv(tmdb.API_KEY_ENV, KEY)
    session = _FakeSession(movies=[_movie(1124, "The Prestige")])

    tmdb.resolve("The Prestige", session=session)

    assert session.calls[0][1]["api_key"] == KEY


def test_a_refused_key_is_not_reported_as_no_results(monkeypatch):
    monkeypatch.delenv(tmdb.API_KEY_ENV, raising=False)
    session = _FakeSession(payload={}, status_code=401)

    with pytest.raises(TmdbError, match="refused the key"):
        tmdb.resolve("The Prestige", key=KEY, session=session)


def test_a_server_error_is_not_reported_as_no_results():
    session = _FakeSession(payload={}, status_code=503)

    with pytest.raises(TmdbError, match="503"):
        tmdb.resolve("The Prestige", key=KEY, session=session)


def test_a_response_that_is_not_json_is_named_as_such():
    session = _FakeSession(payload=None)

    with pytest.raises(TmdbError, match="not JSON"):
        tmdb.resolve("The Prestige", key=KEY, session=session)


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------


def test_both_endpoints_are_asked():
    """A comparison is as likely to be of a series as of a film."""
    session = _FakeSession(movies=[_movie(1124, "The Prestige")])

    tmdb.resolve("The Prestige", key=KEY, session=session)

    asked = {url.rsplit("/", 1)[-1] for url, _ in session.calls}
    assert asked == {"movie", "tv"}


def test_a_result_with_no_usable_id_is_ignored():
    """TMDB has returned entries with nulls in them."""
    session = _FakeSession(movies=[{"title": "Broken"}, _movie(1124, "The Prestige")])

    assert tmdb.resolve("The Prestige", key=KEY, session=session) == "MOVIE_1124"


def test_an_empty_name_is_refused_before_a_request_is_made():
    session = _FakeSession()

    with pytest.raises(TmdbError, match="nothing to search"):
        tmdb.resolve("   ", key=KEY, session=session)

    assert session.calls == []
