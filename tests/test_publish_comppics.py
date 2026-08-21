"""Publishing to a Comps instance.

Nothing here talks to comp.pics. The upload flow is driven against a fake
session that records what it was asked to send, because the failure that
matters is not "the request failed" -- it is "the request succeeded and put the
wrong picture in the wrong cell", which no amount of live testing catches
without a human looking at the result.

This host takes ``row`` and ``column`` as separate fields, so the mapping from
what kiyas holds is direct rather than transposed. That makes the transpose the
tempting mistake here, and the reason several of these tests exist.
"""

from __future__ import annotations

import json

import pytest

from kiyas.publish import bbcode, comppics, load_manifest
from kiyas.publish.result import UploadError

#: The pacing interval as shipped, read at import time -- before the autouse
#: fixture below sets it to zero. Without this the "is pacing on by default"
#: test would be reading the fixture's value and would pass no matter what the
#: module ships.
SHIPPED_MIN_UPLOAD_INTERVAL = comppics.MIN_UPLOAD_INTERVAL


def _make_output(tmp_path, *, sources=("A", "B"), frames=(100, 200)):
    directory = tmp_path / "out"
    directory.mkdir(exist_ok=True)
    entries = []
    for source in sources:
        folder = directory / source
        folder.mkdir(exist_ok=True)
        names = []
        for frame in frames:
            name = f"{frame:06d}.png"
            names.append(name)
            (folder / name).write_bytes(
                b"\x89PNG\r\n\x1a\n" + source.encode() + str(frame).encode()
            )
        entries.append({"name": source, "directory": source, "files": names})

    (directory / "kiyas-manifest.json").write_text(
        json.dumps(
            {
                "kiyas": 1,
                "title": "Test comparison",
                "engine": "vapoursynth",
                "fps": "24000/1001",
                "frames": list(frames),
                "sources": entries,
            }
        ),
        encoding="utf-8",
    )
    return directory


def _comparison(tmp_path, **kwargs):
    return load_manifest(_make_output(tmp_path, **kwargs))


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Upload pacing is real time, and the suite should not spend it.

    The pacing itself is tested directly further down; here it only needs to be
    out of the way.
    """
    monkeypatch.setattr(comppics, "MIN_UPLOAD_INTERVAL", 0.0)
    monkeypatch.setattr(comppics.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A key or URL in the developer's own shell must not reach these tests."""
    monkeypatch.delenv(comppics.API_KEY_ENV, raising=False)
    monkeypatch.delenv(comppics.HOST_URL_ENV, raising=False)


class _Response:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Stands in for a Comps instance, recording what it was told.

    The stored filename it invents encodes the cell it was told to put the
    image in, so a test can read a returned URL and say which cell the server
    thought it was filling.
    """

    def __init__(
        self,
        *,
        create_status=201,
        create_payload=None,
        create_text="",
        image_status=200,
        image_text="",
        image_headers=None,
        image_filename=None,
        long_names=False,
    ):
        self.headers = {}
        self.creates = []
        self.image_posts = []
        self._create_status = create_status
        self._create_payload = {"id": "cmp-1"} if create_payload is None else create_payload
        self._create_text = create_text
        self._image_status = image_status
        self._image_text = image_text
        self._image_headers = image_headers or {}
        self._image_filename = image_filename
        # The real server names images after a UUID, which is what pushes a
        # comparison tag past the width a terminal will wrap at. A test about
        # wrapping has to use names that long or it measures nothing.
        self._long_names = long_names

    def post(self, url, data=None, files=None, json=None, headers=None, **_kwargs):
        if "/image" in url:
            self.image_posts.append(
                {"url": url, "data": data, "files": files, "headers": headers or {}}
            )
            name = self._image_filename
            if name is None:
                stem = f"col{data['column']}-row{data['row']}"
                if self._long_names:
                    stem = f"{stem}-{'a' * 32}"
                name = f"{stem}.png"
            return _Response(
                self._image_status,
                payload={"filename": name},
                headers=self._image_headers,
                text=self._image_text,
            )
        self.creates.append({"url": url, "json": json})
        return _Response(
            status=self._create_status,
            payload=self._create_payload,
            text=self._create_text,
        )


def _cells_by_position(session):
    """``(row, column) -> the bytes that were uploaded there``."""
    return {
        (int(post["data"]["row"]), int(post["data"]["column"])): post["files"]["file"][1]
        for post in session.image_posts
    }


#: Captured before any test can replace the module attribute, so a CLI test can
#: drive the *real* uploader against a fake session. Reading it back off the
#: module at call time would find whatever the monkeypatch had just installed.
_REAL_UPLOAD = comppics.upload


def _against(session):
    """A stand-in for ``comppics.upload`` that talks to ``session``.

    The CLI decides the options and does not accept a session, so this is where
    the two meet: everything the CLI chose is passed through untouched, and
    only the transport is swapped.
    """

    def upload(comparison, **kwargs):
        kwargs.pop("session", None)
        return _REAL_UPLOAD(comparison, session=session, **kwargs)

    return upload


# --------------------------------------------------------------------------
# Grid mapping -- the failure that does not look like one
# --------------------------------------------------------------------------


def test_row_is_the_frame_and_column_is_the_source(tmp_path):
    """The direct mapping, not the transposed one.

    slow.pics needs the two sides swapped and this host does not, so the
    mistake available here is transposing out of habit. It would not error:
    every image would upload, and the result would look like a dramatic
    difference between the releases.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    uploaded = _cells_by_position(session)
    for column, source in enumerate(comparison.sources):
        for row, path in enumerate(source.images):
            assert uploaded[(row, column)] == path.read_bytes()


def test_every_cell_is_sent_exactly_once(tmp_path):
    comparison = _comparison(tmp_path, sources=("A", "B", "C"), frames=(1, 2, 3))
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    assert len(session.image_posts) == 9
    assert len(_cells_by_position(session)) == 9


def test_the_column_label_is_the_source_name(tmp_path):
    """What the site prints above the column.

    It reads its headings off the first row's image names, so a frame label
    here would put "0:00:04.170 / 100" above the column and lose which release
    is which.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    labels = {
        (int(post["data"]["column"]), post["data"]["custom_name"]) for post in session.image_posts
    }
    assert labels == {(0, "A"), (1, "B")}


def test_the_original_filename_is_the_file_on_disk(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    assert {post["data"]["original_filename"] for post in session.image_posts} == {
        "000100.png",
        "000200.png",
    }


# --------------------------------------------------------------------------
# Per-image URLs, and the markup that depends on their order
# --------------------------------------------------------------------------


def test_image_urls_are_source_major(tmp_path):
    """The order ``bbcode`` indexes: ``urls[source * rows + row]``.

    Any other order pairs the wrong picture with the wrong source in the
    markup, without erroring.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = comppics.upload(comparison, session=session)

    assert [url.rsplit("/", 1)[-1] for url in result.image_urls] == [
        "col0-row0.png",
        "col0-row1.png",
        "col1-row0.png",
        "col1-row1.png",
    ]


def test_markup_walks_the_grid_frame_by_frame(tmp_path):
    """End to end: what comes back is what a forum tag can flip through.

    ``bbcode`` regroups source-major URLs into frame-major ones, so getting the
    upload order wrong shows up here as two pictures of the same release next
    to each other instead of the two releases at the same frame.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = comppics.upload(comparison, session=session)
    tag = bbcode.comparison_tag(comparison, result.image_urls)

    frame_major = ["col0-row0.png", "col1-row0.png", "col0-row1.png", "col1-row1.png"]
    positions = [tag.index(name) for name in frame_major]
    assert positions == sorted(positions)


def test_image_urls_point_at_the_uploads_path(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = comppics.upload(comparison, session=session)

    assert result.image_urls[0] == "https://comp.pics/uploads/cmp-1/col0-row0.png"


def test_upload_returns_the_comparison_url(tmp_path):
    comparison = _comparison(tmp_path)

    result = comppics.upload(comparison, session=_FakeSession())

    assert result.url == "https://comp.pics/compare/cmp-1"
    assert result.key == "cmp-1"
    assert result.uploaded == 4


def test_an_image_stored_under_no_name_is_an_error(tmp_path):
    """A 200 with no filename leaves the markup a URL short of its grid."""
    comparison = _comparison(tmp_path)
    session = _FakeSession(image_filename="")

    with pytest.raises(UploadError, match="under what name"):
        comppics.upload(comparison, session=session)


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def test_metadata_carries_the_grid_size(tmp_path):
    comparison = _comparison(tmp_path, sources=("A", "B", "C"), frames=(1, 2))

    body = comppics.build_metadata(comparison)

    assert body["total_rows"] == 2
    assert body["total_columns"] == 3
    assert body["name"] == "Test comparison"


def test_tags_are_passed_through(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, tags=("remux", "hdr"), session=session)

    assert session.creates[0]["json"]["tags"] == ["remux", "hdr"]


def test_an_unknown_expiration_type_is_refused_before_anything_is_sent(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    with pytest.raises(UploadError, match="unknown expiration type"):
        comppics.upload(comparison, expiration_type="whenever", session=session)

    assert session.creates == []


@pytest.mark.parametrize(
    ("asked", "sent"),
    [(1, 1), (7, 7), (30, 30), (90, 90), (14, 7), (60, 30), (365, 90), (2, 1)],
)
def test_expiration_snaps_to_what_the_server_accepts(asked, sent):
    """The server's day count is an enum, not a free integer.

    A value outside it fails validation *after* the comparison has been
    created, leaving an empty one behind.
    """
    assert comppics.snap_expiration(asked) == sent


def test_an_expiration_tie_goes_to_the_shorter_option():
    """60 days is exactly 30 from both 30 and 90, and takes the shorter.

    A comparison that goes away sooner than asked is a smaller surprise than
    one that lingers past when somebody expected it to be gone.
    """
    assert comppics.snap_expiration(60) == 30


# --------------------------------------------------------------------------
# Authentication, and the edit token that is not deployed yet
# --------------------------------------------------------------------------


def test_no_authorization_header_without_a_key(tmp_path):
    """Anonymous is a real mode here, not a degraded one."""
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    assert "Authorization" not in session.headers


def test_the_api_key_is_sent_raw(tmp_path):
    """No "Bearer" prefix.

    The server tries the header as a JWT only when it starts with "Bearer ",
    and otherwise looks it up as an API key. Prefixing it sends the key down
    the JWT path, where it fails as a malformed token.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, api_key="comps_secret", session=session)

    assert session.headers["Authorization"] == "comps_secret"


def test_the_api_key_can_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(comppics.API_KEY_ENV, "comps_from_env")
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    assert session.headers["Authorization"] == "comps_from_env"


def test_an_edit_token_is_sent_back_on_every_image(tmp_path):
    """Forward compatibility with instances newer than the public one.

    Newer builds gate writes to an ownerless comparison behind a token handed
    out at creation. The public instance neither sends one nor checks for it,
    so this has to be driven by what the server said rather than by a version.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession(create_payload={"id": "cmp-1", "edit_token": "tok-9"})

    comppics.upload(comparison, session=session)

    assert all(
        post["headers"].get(comppics.EDIT_TOKEN_HEADER) == "tok-9" for post in session.image_posts
    )


def test_no_edit_token_header_when_the_server_did_not_offer_one(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    comppics.upload(comparison, session=session)

    assert all(comppics.EDIT_TOKEN_HEADER not in post["headers"] for post in session.image_posts)


# --------------------------------------------------------------------------
# Limits and refusals
# --------------------------------------------------------------------------


def test_too_many_rows_is_refused_before_anything_is_sent(tmp_path):
    """The server clamps rather than refusing, which is the problem.

    Without this check the comparison uploads for minutes and comes out quietly
    missing rows.
    """
    comparison = _comparison(tmp_path, frames=tuple(range(comppics.MAX_ROWS + 1)))
    session = _FakeSession()

    with pytest.raises(UploadError, match="at most 200"):
        comppics.upload(comparison, session=session)

    assert session.creates == []
    assert session.image_posts == []


def test_a_refused_comparison_quotes_what_the_server_said(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession(create_status=422, create_text='{"detail":"expiration_days"}')

    with pytest.raises(UploadError, match="expiration_days") as excinfo:
        comppics.upload(comparison, session=session)

    assert "refused the comparison" in str(excinfo.value)


def test_a_create_response_without_an_id_is_an_error(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession(create_payload={"name": "no id here"})

    with pytest.raises(UploadError, match="unexpected response"):
        comppics.upload(comparison, session=session)


def test_a_refused_upload_names_the_api_key_variable(tmp_path):
    """A 403 on this host usually means the instance stopped taking anonymous
    writes, which is a thing the person can act on."""
    comparison = _comparison(tmp_path)
    session = _FakeSession(image_status=403)

    with pytest.raises(UploadError, match=comppics.API_KEY_ENV):
        comppics.upload(comparison, session=session)


def test_a_failed_image_names_the_file(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession(image_status=400, image_text="Unsupported image type")

    with pytest.raises(UploadError, match="Unsupported image type") as excinfo:
        comppics.upload(comparison, session=session)

    assert "000100.png" in str(excinfo.value)


# --------------------------------------------------------------------------
# Where the request goes
# --------------------------------------------------------------------------


def test_a_self_hosted_instance_gets_every_request(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = comppics.upload(comparison, base_url="https://comps.example/", session=session)

    assert session.creates[0]["url"] == "https://comps.example/api/v1/comparisons"
    assert all(
        post["url"].startswith("https://comps.example/api/v1/") for post in session.image_posts
    )
    assert result.url == "https://comps.example/compare/cmp-1"
    assert result.image_urls[0].startswith("https://comps.example/uploads/")


def test_the_instance_can_come_from_the_environment(tmp_path, monkeypatch):
    """``run --publish`` has no place to put a URL; this is how it reaches one."""
    monkeypatch.setenv(comppics.HOST_URL_ENV, "https://comps.example")
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = comppics.upload(comparison, session=session)

    assert result.url == "https://comps.example/compare/cmp-1"


def test_an_explicit_url_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(comppics.HOST_URL_ENV, "https://ignored.example")

    assert comppics.resolve_base_url("https://chosen.example") == "https://chosen.example"


def test_an_error_names_the_host_that_refused(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession(create_status=500)

    with pytest.raises(UploadError, match="comps.example") as excinfo:
        comppics.upload(comparison, base_url="https://comps.example", session=session)

    assert "comp.pics" not in str(excinfo.value)


def test_an_expiry_the_server_changed_is_reported(tmp_path):
    """Measured against the public instance: it stored 7 when asked for 1.

    The request is right -- the field is in the spec -- so the only place the
    substitution can be caught is the answer, and somebody who asked for one
    day should not have to go and look to find out they got seven.
    """
    comparison = _comparison(tmp_path)
    session = _FakeSession(create_payload={"id": "cmp-1", "expiration_days": 7})

    result = comppics.upload(comparison, expiration_days=1, session=session)

    assert any("asked comp.pics to keep this for 1 day" in note for note in result.notes)
    assert any("it stored 7 days" in note for note in result.notes)


def test_no_note_when_the_server_kept_what_it_was_given(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession(create_payload={"id": "cmp-1", "expiration_days": 30})

    result = comppics.upload(comparison, expiration_days=30, session=session)

    assert result.notes == ()


def test_no_note_when_the_server_says_nothing_about_expiry(tmp_path):
    comparison = _comparison(tmp_path)

    result = comppics.upload(comparison, session=_FakeSession())

    assert result.notes == ()


def test_pacing_is_on_by_default():
    """Guards the shipped value, not the one the fixture sets."""
    assert SHIPPED_MIN_UPLOAD_INTERVAL > 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_publish_to_is_parsed():
    from kiyas import cli

    args = cli.build_parser().parse_args(["publish", "out", "--to", "comppics"])

    assert args.to == "comppics"


def test_publish_defaults_to_slowpics():
    from kiyas import cli

    assert cli.build_parser().parse_args(["publish", "out"]).to == "slowpics"


def test_run_publish_to_is_parsed():
    from kiyas import cli

    args = cli.build_parser().parse_args(["run", "p.toml", "--publish", "--publish-to", "comppics"])

    assert args.publish_to == "comppics"


def test_publish_defaults_carry_the_chosen_target():
    from kiyas import cli

    assert cli._publish_defaults("comppics").to == "comppics"
    assert cli._publish_defaults().to == "slowpics"


def test_tag_flag_repeats():
    from kiyas import cli

    args = cli.build_parser().parse_args(
        ["publish", "out", "--to", "comppics", "--tag", "remux", "--tag", "hdr"]
    )

    assert args.tags == ["remux", "hdr"]


def test_host_url_with_slowpics_is_refused(tmp_path, capsys):
    """slow.pics is one site; pointing it somewhere else is a typo, not a wish."""
    from kiyas import cli

    _make_output(tmp_path)
    code = cli.main(["publish", str(tmp_path / "out"), "--host-url", "https://comps.example"])

    assert code == 1
    assert "--host-url" in capsys.readouterr().out


def test_tag_with_slowpics_is_refused(tmp_path, capsys):
    from kiyas import cli

    _make_output(tmp_path)
    code = cli.main(["publish", str(tmp_path / "out"), "--tag", "remux"])

    assert code == 1
    assert "--tag" in capsys.readouterr().out


def test_publishing_to_comppics_warns_that_there_is_no_unlisted_mode(tmp_path, capsys, monkeypatch):
    """Said before anything is sent, while it can still be stopped."""
    from kiyas import cli

    monkeypatch.setattr(comppics, "upload", _against(_FakeSession()))

    _make_output(tmp_path)
    code = cli.main(["publish", str(tmp_path / "out"), "--to", "comppics"])

    assert code == 0
    assert "no unlisted mode" in capsys.readouterr().out


def test_slowpics_only_flags_are_named_when_dropped(tmp_path, capsys, monkeypatch):
    from kiyas import cli

    monkeypatch.setattr(comppics, "upload", _against(_FakeSession()))

    _make_output(tmp_path)
    code = cli.main(["publish", str(tmp_path / "out"), "--to", "comppics", "--public", "--nsfw"])

    out = capsys.readouterr().out
    assert code == 0
    assert "--public" in out
    assert "--nsfw" in out


def test_a_zero_remove_after_says_it_cannot_be_forever(tmp_path, capsys, monkeypatch):
    from kiyas import cli

    monkeypatch.setattr(comppics, "upload", _against(_FakeSession()))

    _make_output(tmp_path)
    cli.main(["publish", str(tmp_path / "out"), "--to", "comppics"])

    assert "cannot keep a comparison forever" in capsys.readouterr().out


def test_a_snapped_remove_after_says_what_it_used(tmp_path, capsys, monkeypatch):
    from kiyas import cli

    monkeypatch.setattr(comppics, "upload", _against(_FakeSession()))

    _make_output(tmp_path)
    cli.main(["publish", str(tmp_path / "out"), "--to", "comppics", "--remove-after", "14"])

    out = capsys.readouterr().out
    assert "keeping this one 7 days" in out


def test_markup_uses_the_per_image_urls_when_the_host_gives_them(tmp_path, capsys, monkeypatch):
    """The formats were written for this and have never had it until now."""
    from kiyas import cli

    monkeypatch.setattr(comppics, "upload", _against(_FakeSession()))

    _make_output(tmp_path)
    cli.main(["publish", str(tmp_path / "out"), "--to", "comppics", "--format", "comparison"])

    out = capsys.readouterr().out
    assert "[comparison=A,B]" in out
    assert "/uploads/cmp-1/col0-row0.png" in out


def test_markup_urls_survive_the_terminal(tmp_path, capsys, monkeypatch):
    """Markup is copied, so a line break inside a URL is a dead link.

    rich wraps to the console width by default and breaks long image URLs
    mid-token, inserting real newlines: a four-line comparison tag came out as
    eight lines at width 80.
    """
    from kiyas import cli

    monkeypatch.setattr(comppics, "upload", _against(_FakeSession(long_names=True)))

    _make_output(tmp_path)
    cli.main(["publish", str(tmp_path / "out"), "--to", "comppics", "--format", "comparison"])

    out = capsys.readouterr().out
    padding = "a" * 32
    assert f"https://comp.pics/uploads/cmp-1/col0-row0-{padding}.png" in out
    assert f"https://comp.pics/uploads/cmp-1/col1-row1-{padding}.png" in out
