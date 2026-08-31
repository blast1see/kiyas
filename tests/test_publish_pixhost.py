"""Publishing to pixhost.

Nothing here talks to pixhost. The upload flow is driven against a fake session
that records what it was asked to send, because the failure that matters is not
"the request failed" -- it is "the request succeeded and paired the wrong
thumbnail with the wrong frame", which no amount of live testing catches
without a human opening every link.

This host answers with two addresses per image -- a page and a thumbnail -- and
neither of them is the picture. That is the difference from the other two
backends, and it is where most of these tests point: getting the two lists out
of step is invisible in the markup, and so is filling ``image_urls`` with a
page, which would put an HTML document inside an ``[img]`` tag.
"""

from __future__ import annotations

import json

import pytest

from kiyas import cli
from kiyas.publish import bbcode, load_manifest, pixhost
from kiyas.publish.result import UploadError

#: The pacing interval as shipped, read at import time -- before the autouse
#: fixture below sets it to zero. Without this the "is pacing on by default"
#: test would be reading the fixture's value and would pass no matter what the
#: module ships.
SHIPPED_MIN_UPLOAD_INTERVAL = pixhost.MIN_UPLOAD_INTERVAL


def _make_output(tmp_path, *, sources=("A", "B"), frames=(100, 200), size=64):
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
            body = b"\x89PNG\r\n\x1a\n" + source.encode() + str(frame).encode()
            (folder / name).write_bytes(body.ljust(size, b"\x00"))
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
    monkeypatch.setattr(pixhost, "MIN_UPLOAD_INTERVAL", 0.0)
    monkeypatch.setattr(pixhost.time, "sleep", lambda _seconds: None)


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
    """Stands in for pixhost, recording what it was told.

    The addresses it invents encode the *bytes it received*, so a test can read
    a returned URL and say which picture the server thought it was storing.
    Inventing them from a counter instead would make every ordering test pass
    on a backend that shuffled the grid.
    """

    def __init__(
        self,
        *,
        gallery_status=200,
        gallery_payload=None,
        image_status=200,
        image_payload="derive",
        image_headers=None,
        finalize_status=200,
        text="",
    ):
        self.headers = {}
        self.gallery_posts = []
        self.image_posts = []
        self.finalize_posts = []
        self._gallery_status = gallery_status
        self._gallery_payload = (
            {
                "gallery_name": "Test comparison",
                "gallery_hash": "gh-1",
                "gallery_upload_hash": "guh-1",
                "gallery_url": "https://pixhost.to/gallery/gh-1",
            }
            if gallery_payload is None
            else gallery_payload
        )
        self._image_status = image_status
        self._image_payload = image_payload
        self._image_headers = image_headers or {}
        self._finalize_status = finalize_status
        self._text = text

    def post(self, url, data=None, files=None, headers=None, **_kwargs):
        if url.endswith("/finalize"):
            self.finalize_posts.append({"url": url, "data": data})
            return _Response(self._finalize_status, payload={}, text=self._text)
        if url.endswith("/images"):
            self.image_posts.append({"url": url, "data": data, "files": files})
            payload = self._image_payload
            if payload == "derive":
                # The tail of the uploaded body is the source name and frame,
                # so the addresses name the picture they actually received.
                tag = files["img"][1].rstrip(b"\x00")[8:].decode()
                payload = {
                    "name": f"{tag}.png",
                    "show_url": f"https://pixhost.to/show/1/{tag}.png",
                    "th_url": f"https://t1.pixhost.to/thumbs/1/{tag}.png",
                    "thumb_width": 350,
                    "thumb_height": 197,
                }
            return _Response(
                self._image_status,
                payload=payload,
                headers=self._image_headers,
                text=self._text,
            )
        self.gallery_posts.append({"url": url, "data": data})
        return _Response(self._gallery_status, payload=self._gallery_payload, text=self._text)


#: Captured before any test can replace the module attribute, so a CLI test can
#: drive the *real* uploader against a fake session.
_REAL_UPLOAD = pixhost.upload


def _against(session):
    def upload(comparison, **kwargs):
        kwargs.pop("session", None)
        return _REAL_UPLOAD(comparison, session=session, **kwargs)

    return upload


def _unwrapped(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------
# The gallery
# --------------------------------------------------------------------------


def test_a_gallery_is_created_named_after_the_comparison(tmp_path):
    session = _FakeSession()

    result = pixhost.upload(_comparison(tmp_path), session=session)

    assert len(session.gallery_posts) == 1
    assert session.gallery_posts[0]["data"]["gallery_name"] == "Test comparison"
    assert result.url == "https://pixhost.to/gallery/gh-1"
    assert result.key == "gh-1"


def test_the_gallery_is_finalised_with_its_upload_hash(tmp_path):
    """Without this the gallery exists but was never published."""
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), session=session)

    assert len(session.finalize_posts) == 1
    assert session.finalize_posts[0]["url"].endswith("/galleries/gh-1/finalize")
    assert session.finalize_posts[0]["data"]["gallery_upload_hash"] == "guh-1"


def test_every_image_is_sent_into_the_gallery(tmp_path):
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), session=session)

    assert len(session.image_posts) == 4
    for post in session.image_posts:
        assert post["data"]["gallery_hash"] == "gh-1"
        assert post["data"]["gallery_upload_hash"] == "guh-1"


def test_a_manage_url_is_reported_because_nothing_else_can_recover_it(tmp_path):
    """The delete token is handed out once and never again."""
    session = _FakeSession(
        gallery_payload={
            "gallery_hash": "gh-1",
            "gallery_upload_hash": "guh-1",
            "gallery_url": "https://pixhost.to/gallery/gh-1",
            "manage_url": "https://pixhost.to/manage/tok",
        }
    )

    result = pixhost.upload(_comparison(tmp_path), session=session)

    assert any("https://pixhost.to/manage/tok" in note for note in result.notes)


def test_a_gallery_that_comes_back_incomplete_is_refused(tmp_path):
    session = _FakeSession(gallery_payload={"gallery_hash": "gh-1"})

    with pytest.raises(UploadError, match="gallery_upload_hash"):
        pixhost.upload(_comparison(tmp_path), session=session)


def test_a_refused_finalize_keeps_the_images_and_says_so(tmp_path):
    """Every address is already known; losing them to the last call is worse."""
    session = _FakeSession(finalize_status=417)

    result = pixhost.upload(_comparison(tmp_path), session=session)

    assert len(result.thumbnail_urls) == 4
    assert any("refused to publish the gallery" in note for note in result.notes)


# --------------------------------------------------------------------------
# What comes back, and in what order
# --------------------------------------------------------------------------


def test_pages_and_thumbnails_are_source_major(tmp_path):
    """The order the markup indexes. Getting it wrong does not error."""
    session = _FakeSession()

    result = pixhost.upload(_comparison(tmp_path), session=session)

    assert result.thumbnail_urls == (
        "https://t1.pixhost.to/thumbs/1/A100.png",
        "https://t1.pixhost.to/thumbs/1/A200.png",
        "https://t1.pixhost.to/thumbs/1/B100.png",
        "https://t1.pixhost.to/thumbs/1/B200.png",
    )
    assert result.page_urls == (
        "https://pixhost.to/show/1/A100.png",
        "https://pixhost.to/show/1/A200.png",
        "https://pixhost.to/show/1/B100.png",
        "https://pixhost.to/show/1/B200.png",
    )


def test_image_urls_is_left_empty_because_a_page_is_not_a_picture(tmp_path):
    """`image_urls` goes inside [img]. A page there is a broken picture.

    pixhost does not document the address of the full-size file, so there is
    nothing honest to put here -- and a caller reads emptiness as "this host
    has no per-image pictures", which is exactly true.
    """
    session = _FakeSession()

    result = pixhost.upload(_comparison(tmp_path), session=session)

    assert result.image_urls == ()


def test_the_markup_walks_the_grid_frame_by_frame(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = pixhost.upload(comparison, session=session)
    markup = bbcode.render(comparison, result.thumbnail_urls, "thumbnails", pages=result.page_urls)

    lines = [line for line in markup.splitlines() if line.startswith(("A:", "B:"))]
    assert lines[0].startswith("A:") and "A100" in lines[0]
    assert lines[1].startswith("B:") and "B100" in lines[1]
    assert lines[2].startswith("A:") and "A200" in lines[2]
    assert lines[3].startswith("B:") and "B200" in lines[3]


def test_each_thumbnail_links_to_its_own_page(tmp_path):
    comparison = _comparison(tmp_path)
    session = _FakeSession()

    result = pixhost.upload(comparison, session=session)
    markup = bbcode.render(comparison, result.thumbnail_urls, "thumbnails", pages=result.page_urls)

    for tag in ("A100", "A200", "B100", "B200"):
        expected = (
            f"[url=https://pixhost.to/show/1/{tag}.png]"
            f"[img]https://t1.pixhost.to/thumbs/1/{tag}.png[/img][/url]"
        )
        assert expected in markup


def test_an_image_stored_with_no_address_is_an_error(tmp_path):
    """A short list would silently describe a grid it does not fill."""
    session = _FakeSession(image_payload={"name": "x.png", "th_url": "https://t1/x.png"})

    with pytest.raises(UploadError, match="did not say where"):
        pixhost.upload(_comparison(tmp_path), session=session)


# --------------------------------------------------------------------------
# Refusing before the network
# --------------------------------------------------------------------------


def test_an_image_over_the_limit_stops_the_run_before_anything_is_sent(tmp_path):
    """A UHD PNG passes 10 MB regularly, and half a gallery is worse than none."""
    comparison = _comparison(tmp_path, size=pixhost.MAX_IMAGE_BYTES + 1)
    session = _FakeSession()

    with pytest.raises(UploadError, match="10 MB per image"):
        pixhost.upload(comparison, session=session)

    assert session.gallery_posts == []
    assert session.image_posts == []


def test_the_refusal_names_the_files_and_their_sizes(tmp_path):
    comparison = _comparison(tmp_path, size=pixhost.MAX_IMAGE_BYTES + 1)

    with pytest.raises(UploadError) as caught:
        pixhost.upload(comparison, session=_FakeSession())

    assert "000100.png" in str(caught.value)
    assert "MB" in str(caught.value)


def test_an_image_exactly_on_the_limit_is_allowed(tmp_path):
    comparison = _comparison(tmp_path, size=pixhost.MAX_IMAGE_BYTES)

    result = pixhost.upload(comparison, session=_FakeSession())

    assert result.uploaded == 4


def test_the_limit_is_megabytes_not_mebibytes(tmp_path):
    """The gap between the two is where a half-filled gallery comes from.

    The documentation says "10 MB" and does not say which it means. A file in
    the half-megabyte gap would pass a mebibyte check here and then be refused
    by the host on image twenty-one, which is the outcome the check exists to
    prevent -- and the 413 branch would report it as the host moving its limit.
    """
    assert pixhost.MAX_IMAGE_BYTES == 10_000_000

    comparison = _comparison(tmp_path, size=10_400_000)

    with pytest.raises(UploadError, match="10 MB per image"):
        pixhost.upload(comparison, session=_FakeSession())


def test_a_failed_upload_still_hands_back_the_delete_token(tmp_path):
    """Some images are up by now, on a host with no expiry.

    The token appears once, at gallery creation, and nothing can recover it. A
    failure that carries it away leaves pictures hosted that nobody can remove.
    """
    session = _FakeSession(
        gallery_payload={
            "gallery_hash": "gh-1",
            "gallery_upload_hash": "guh-1",
            "gallery_url": "https://pixhost.to/gallery/gh-1",
            "manage_url": "https://pixhost.to/manage/tok",
        },
        image_status=400,
    )

    with pytest.raises(UploadError, match="https://pixhost.to/manage/tok"):
        pixhost.upload(_comparison(tmp_path), session=session)


def test_a_thumbnail_size_outside_the_documented_range_is_refused(tmp_path):
    session = _FakeSession()

    with pytest.raises(UploadError, match="between 150 and 500"):
        pixhost.upload(_comparison(tmp_path), thumb_size=900, session=session)

    assert session.gallery_posts == []


# --------------------------------------------------------------------------
# What gets sent
# --------------------------------------------------------------------------


def test_content_type_is_always_sent_because_the_api_requires_it(tmp_path):
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), session=session)

    assert all(post["data"]["content_type"] == "0" for post in session.image_posts)


def test_nsfw_sets_the_adult_content_type(tmp_path):
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), nsfw=True, session=session)

    assert all(post["data"]["content_type"] == "1" for post in session.image_posts)


def test_the_thumbnail_size_asked_for_is_the_one_sent(tmp_path):
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), thumb_size=420, session=session)

    assert all(post["data"]["max_th_size"] == "420" for post in session.image_posts)


def test_optimize_for_web_is_never_sent(tmp_path):
    """Its default is off, and off is the only setting that keeps a PNG whole."""
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), session=session)

    assert all("optimize_for_web" not in post["data"] for post in session.image_posts)


def test_the_user_agent_names_kiyas(tmp_path):
    session = _FakeSession()

    pixhost.upload(_comparison(tmp_path), session=session)

    assert "kiyas/" in session.headers["User-Agent"]


def test_a_comparison_is_told_this_host_has_no_viewer(tmp_path):
    session = _FakeSession()

    result = pixhost.upload(_comparison(tmp_path), session=session)

    assert any("not comparisons" in note for note in result.notes)


def test_a_single_source_set_gets_no_such_note(tmp_path):
    session = _FakeSession()

    result = pixhost.upload(_comparison(tmp_path, sources=("A",)), session=session)

    assert not any("not comparisons" in note for note in result.notes)


# --------------------------------------------------------------------------
# Failures from the host
# --------------------------------------------------------------------------


def test_a_refused_gallery_names_the_host(tmp_path):
    session = _FakeSession(gallery_status=500)

    with pytest.raises(UploadError, match="pixhost.to refused the gallery"):
        pixhost.upload(_comparison(tmp_path), session=session)


def test_a_failed_image_names_the_file(tmp_path):
    session = _FakeSession(image_status=400)

    with pytest.raises(UploadError, match="000100.png"):
        pixhost.upload(_comparison(tmp_path), session=session)


def test_an_unsupported_format_is_reported_as_such(tmp_path):
    session = _FakeSession(image_status=414)

    with pytest.raises(UploadError, match="does not accept this image format"):
        pixhost.upload(_comparison(tmp_path), session=session)


def test_a_413_after_the_size_check_says_the_limit_moved(tmp_path):
    session = _FakeSession(image_status=413)

    with pytest.raises(UploadError, match="although it is under"):
        pixhost.upload(_comparison(tmp_path), session=session)


def test_pacing_is_on_by_default():
    """The fixture above turns it off; this reads the shipped value."""
    assert SHIPPED_MIN_UPLOAD_INTERVAL > 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_publish_to_pixhost_parses():
    args = cli.build_parser().parse_args(["publish", "out", "--to", "pixhost"])

    assert args.to == "pixhost"


def test_run_can_publish_to_pixhost():
    args = cli.build_parser().parse_args(["run", "p.toml", "--publish-to", "pixhost"])

    assert args.publish_to == "pixhost"


def test_the_defaults_carry_the_target_through():
    """`run --publish` builds its own Namespace; a missing field is a crash."""
    args = cli._publish_defaults("pixhost")

    assert args.to == "pixhost"
    assert cli._SENDERS[args.to] is cli._pixhost_sender


def test_run_publish_to_pixhost_has_every_field_the_sender_reads(tmp_path, monkeypatch):
    """The fabricated Namespace is the one that goes stale when a flag is added."""
    from rich.console import Console

    monkeypatch.setattr(pixhost, "upload", _against(_FakeSession()))
    args = cli._publish_defaults("pixhost")

    send = cli._pixhost_sender(args, _comparison(tmp_path), Console())

    assert send(lambda _text: None).key == "gh-1"


def test_a_comppics_only_flag_is_refused_rather_than_ignored(tmp_path, capsys):
    code = cli.main(["publish", str(_make_output(tmp_path)), "--to", "pixhost", "--tag", "x"])

    assert code == 1
    assert "--tag" in capsys.readouterr().out


def test_a_flag_this_host_has_no_equivalent_for_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pixhost, "upload", _against(_FakeSession()))

    code = cli.main(["publish", str(_make_output(tmp_path)), "--to", "pixhost", "--public"])

    assert code == 0
    assert "--public" in _unwrapped(capsys.readouterr().out)


def test_the_cli_prints_the_gallery_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pixhost, "upload", _against(_FakeSession()))

    code = cli.main(["publish", str(_make_output(tmp_path)), "--to", "pixhost"])

    assert code == 0
    assert "https://pixhost.to/gallery/gh-1" in capsys.readouterr().out


def test_the_cli_writes_thumbnail_markup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pixhost, "upload", _against(_FakeSession()))

    cli.main(
        [
            "publish",
            str(_make_output(tmp_path)),
            "--to",
            "pixhost",
            "--format",
            "thumbnails",
        ]
    )

    out = capsys.readouterr().out
    assert "[url=https://pixhost.to/show/1/A100.png]" in out
    assert "[img]https://t1.pixhost.to/thumbs/1/A100.png[/img][/url]" in out


def test_asking_for_thumbnails_from_a_host_that_makes_none_says_so(tmp_path, capsys):
    """slow.pics returns no thumbnails; the format has to say why, not crash."""
    from kiyas.publish.result import UploadResult

    comparison = _comparison(tmp_path)
    result = UploadResult(key="k", url="https://slow.pics/c/k", uploaded=4, skipped=0)

    with pytest.raises(bbcode.BBCodeError, match="does not make thumbnails"):
        cli._markup(comparison, result, "thumbnails")


def test_a_thumb_size_outside_the_range_is_refused_by_the_cli(tmp_path, capsys):
    code = cli.main(
        ["publish", str(_make_output(tmp_path)), "--to", "pixhost", "--thumb-size", "40"]
    )

    assert code == 1
    assert "--thumb-size" in capsys.readouterr().out


def test_a_thumb_size_of_zero_is_refused_rather_than_ignored(tmp_path, capsys):
    """`or` treated a supplied 0 as "not supplied", so it never reached the range check."""
    code = cli.main(
        ["publish", str(_make_output(tmp_path)), "--to", "pixhost", "--thumb-size", "0"]
    )

    assert code == 1
    assert "--thumb-size" in capsys.readouterr().out


def test_thumb_size_is_reported_as_ignored_on_a_host_that_makes_none(tmp_path, capsys):
    """The table exists so a dropped flag gets named. This was the flag it missed."""
    from rich.console import Console

    args = cli._publish_defaults("slowpics")
    args.thumb_size = 500

    cli._slowpics_sender(args, _comparison(tmp_path), Console())

    assert "--thumb-size" in _unwrapped(capsys.readouterr().out)
