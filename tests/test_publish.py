"""Publishing tests.

Nothing here talks to slow.pics. The upload flow is driven against a fake
session that records what it was asked to send, because the failure that
matters is not "the request failed" -- it is "the request succeeded and put
the wrong picture in the wrong cell", which no amount of live testing catches
without a human looking at the result.
"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from kiyas.publish import bbcode, load_manifest, slowpics
from kiyas.publish.bbcode import BBCodeError
from kiyas.publish.manifest import Comparison, ComparisonRow, ManifestError
from kiyas.publish.slowpics import UploadError

# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def _make_output(tmp_path, *, sources=("A", "B"), frames=(100, 200), fps="24000/1001", drop=None):
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
            if drop is not None and (source, frame) == drop:
                continue
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
                "fps": fps,
                "frames": list(frames),
                "sources": entries,
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_manifest_loads_from_a_directory(tmp_path):
    comparison = load_manifest(_make_output(tmp_path))

    assert comparison.title == "Test comparison"
    assert comparison.source_names == ["A", "B"]
    assert comparison.total_images == 4
    assert comparison.is_comparison is True


def test_manifest_loads_from_the_file_itself(tmp_path):
    directory = _make_output(tmp_path)

    assert load_manifest(directory / "kiyas-manifest.json").total_images == 4


def test_single_source_is_a_collection_not_a_comparison(tmp_path):
    comparison = load_manifest(_make_output(tmp_path, sources=("Only",)))

    assert comparison.is_comparison is False


def test_missing_manifest_is_reported(tmp_path):
    with pytest.raises(ManifestError, match="no manifest"):
        load_manifest(tmp_path)


def test_missing_image_is_refused(tmp_path):
    """A gap shifts every later row and turns into a fake visual difference."""
    directory = _make_output(tmp_path, drop=("B", 200))

    with pytest.raises(ManifestError, match="missing"):
        load_manifest(directory)


def test_uneven_sources_are_refused(tmp_path):
    directory = _make_output(tmp_path)
    manifest = directory / "kiyas-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sources"][1]["files"] = payload["sources"][1]["files"][:1]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestError, match="line up"):
        load_manifest(directory)


def test_timestamps_use_the_real_frame_rate():
    row = ComparisonRow.for_frame(24000, Fraction(24000, 1001))

    # 24000 frames at 24000/1001 fps is 1001 seconds, not 1000.
    assert row.label.startswith("0:16:41.000")


def test_frame_label_carries_both_time_and_number():
    label = ComparisonRow.for_frame(1234, Fraction(24)).label

    assert "1234" in label
    assert ":" in label


def test_a_manifest_with_labels_uses_them_instead_of_timestamps(tmp_path):
    """An audio comparison's rows are analyses; a timestamp would be a lie."""
    directory = _make_output(tmp_path, frames=(0, 1))
    manifest = directory / "kiyas-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["labels"] = ["Spectrogram", "Waveform"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    comparison = load_manifest(directory)

    assert [row.label for row in comparison.rows] == ["Spectrogram", "Waveform"]


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


def _comparison(tmp_path, **kwargs) -> Comparison:
    return load_manifest(_make_output(tmp_path, **kwargs))


def test_payload_defaults_to_unlisted(tmp_path):
    """A comparison is a working document until its author says otherwise."""
    payload = slowpics.build_payload(_comparison(tmp_path))

    assert payload["public"] == "false"
    assert payload["visibility"] == "LINK_ONLY"


def test_payload_can_be_made_public(tmp_path):
    payload = slowpics.build_payload(_comparison(tmp_path), public=True)

    assert payload["visibility"] == "PUBLIC"


def test_payload_names_every_cell_of_the_grid(tmp_path):
    payload = slowpics.build_payload(_comparison(tmp_path))

    assert payload["comparisons[0].images[0].name"] == "A"
    assert payload["comparisons[0].images[1].name"] == "B"
    assert payload["comparisons[1].images[0].name"] == "A"
    assert "100" in payload["comparisons[0].name"]


def test_single_source_uses_the_collection_field_names(tmp_path):
    payload = slowpics.build_payload(_comparison(tmp_path, sources=("Only",)))

    assert "images[0].name" in payload
    assert not any(key.startswith("comparisons[") for key in payload)


def test_payload_always_asks_for_png(tmp_path):
    """A comparison re-encoded to JPEG is not a comparison."""
    assert slowpics.build_payload(_comparison(tmp_path))["desiredFileType"] == "image/png"


def test_remove_after_is_blank_when_unset(tmp_path):
    assert slowpics.build_payload(_comparison(tmp_path))["removeAfter"] == ""
    assert slowpics.build_payload(_comparison(tmp_path), remove_after_days=7)["removeAfter"] == "7"


def test_hashes_cover_every_image_once(tmp_path):
    comparison = _comparison(tmp_path)

    hashes = slowpics.build_hashes(comparison)

    assert len(hashes) == comparison.total_images
    assert "comparisons[0].images[0].hashSum" in hashes
    assert all(len(digest) == 64 for digest in hashes.values())


def test_identical_files_hash_the_same(tmp_path):
    directory = tmp_path / "same"
    directory.mkdir()
    first = directory / "a.png"
    second = directory / "b.png"
    first.write_bytes(b"identical")
    second.write_bytes(b"identical")

    assert slowpics._digest(first) == slowpics._digest(second)


# --------------------------------------------------------------------------
# The grid transpose. This is the one that silently ruins a comparison.
# --------------------------------------------------------------------------


def test_server_grid_is_transposed_back_correctly(tmp_path):
    """slow.pics returns images[frame][source]; kiyas holds [source][frame].

    Get this backwards and every picture lands in the wrong cell: source A's
    frame 100 appears in source B's column, which reads as a genuine and
    dramatic difference between the two releases.
    """
    comparison = _comparison(tmp_path)
    response = {
        "images": [["uuid-f0-A", "uuid-f0-B"], ["uuid-f1-A", "uuid-f1-B"]],
        "completeImageUuids": [],
    }

    pending = slowpics._images_to_send(comparison, response)

    by_uuid = {image_uuid: path for image_uuid, path in pending}
    assert by_uuid["uuid-f0-A"] == comparison.sources[0].images[0]
    assert by_uuid["uuid-f0-B"] == comparison.sources[1].images[0]
    assert by_uuid["uuid-f1-A"] == comparison.sources[0].images[1]
    assert by_uuid["uuid-f1-B"] == comparison.sources[1].images[1]


def test_images_the_server_already_has_are_not_resent(tmp_path):
    """This is what makes re-running a failed upload cheap."""
    comparison = _comparison(tmp_path)
    response = {
        "images": [["a0", "b0"], ["a1", "b1"]],
        "completeImageUuids": ["a0", "b1"],
    }

    pending = slowpics._images_to_send(comparison, response)

    assert {u for u, _ in pending} == {"b0", "a1"}


def test_a_grid_that_does_not_match_is_an_error_not_a_guess(tmp_path):
    comparison = _comparison(tmp_path)

    with pytest.raises(UploadError, match="does not match"):
        slowpics._images_to_send(comparison, {"images": [["only-one"]], "completeImageUuids": []})


# --------------------------------------------------------------------------
# Upload flow, against a fake session
# --------------------------------------------------------------------------


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
    def __init__(
        self, *, image_status=200, image_headers=None, collection_status=200, collection_text=""
    ):
        self.headers = {}
        self.cookies = {"XSRF-TOKEN": "token-123", "BROWSER-ID": "browser-abc"}
        self.posts = []
        self.image_posts = []
        self._image_status = image_status
        self._image_headers = image_headers or {}
        self._collection_status = collection_status
        self._collection_text = collection_text

    def get(self, url, **_kwargs):
        return _Response()

    def post(self, url, data=None, files=None, **_kwargs):
        if "/upload/image/" in url:
            self.image_posts.append((url, data, files))
            return _Response(self._image_status, headers=self._image_headers)
        self.posts.append((url, data))
        return _Response(
            status=self._collection_status,
            text=self._collection_text,
            payload={
                "key": "ABC123",
                "collectionUuid": "coll-1",
                "images": [["a0", "b0"], ["a1", "b1"]],
                "completeImageUuids": [],
            },
        )


def test_upload_returns_the_collection_url(tmp_path):
    session = _FakeSession()

    result = slowpics.upload(_comparison(tmp_path), session=session)

    assert result.url == "https://slow.pics/c/ABC123"
    assert result.uploaded == 4
    assert result.skipped == 0


def test_upload_sends_the_xsrf_token_back(tmp_path):
    """Every write is rejected without it."""
    session = _FakeSession()

    slowpics.upload(_comparison(tmp_path), session=session)

    assert session.headers["X-XSRF-TOKEN"] == "token-123"


def test_upload_posts_to_the_comparison_endpoint(tmp_path):
    session = _FakeSession()

    slowpics.upload(_comparison(tmp_path), session=session)

    assert session.posts[0][0].endswith("/upload/comparison")


def test_single_source_posts_to_the_collection_endpoint(tmp_path):
    session = _FakeSession()

    slowpics.upload(_comparison(tmp_path, sources=("Only",)), session=session)

    assert session.posts[0][0].endswith("/upload/collection")


def test_upload_sends_one_request_per_image(tmp_path):
    session = _FakeSession()

    slowpics.upload(_comparison(tmp_path), session=session)

    assert len(session.image_posts) == 4


def test_image_already_on_the_server_is_success_not_failure(tmp_path):
    """A 400 saying IMAGE_IS_COMPLETE means the hash matched. Nothing to do."""
    session = _FakeSession(image_status=400, image_headers={"X-Error-Message": "IMAGE_IS_COMPLETE"})

    result = slowpics.upload(_comparison(tmp_path), session=session)

    assert result.url.endswith("ABC123")


def test_other_400s_stop_the_upload(tmp_path):
    session = _FakeSession(image_status=400, image_headers={"X-Error-Message": "TOO_LARGE"})

    with pytest.raises(UploadError, match="TOO_LARGE"):
        slowpics.upload(_comparison(tmp_path), session=session)


def test_a_rejected_collection_quotes_what_the_server_said(tmp_path):
    """A 400 with the body thrown away is a dead end.

    "400 Client Error: Bad Request" says a field was wrong and not which one,
    and the reason lives in the body. This came up on a real upload that was
    refused with no way to find out why except reading the source.
    """
    session = _FakeSession(
        collection_status=400,
        collection_text='{"tmdbId":"must be MOVIE_<id> or TV_<id>"}',
    )

    with pytest.raises(UploadError, match="must be MOVIE_"):
        slowpics.upload(_comparison(tmp_path), session=session)


def test_a_rejection_with_no_body_still_reads_as_one_sentence(tmp_path):
    session = _FakeSession(collection_status=400)

    with pytest.raises(UploadError, match="refused the collection") as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)
    assert "the server said" not in str(excinfo.value)


def test_an_html_error_page_is_truncated(tmp_path):
    """A server having a bad day answers with a page, not a field error."""
    session = _FakeSession(collection_status=400, collection_text="<html>" + "x" * 4000)

    with pytest.raises(UploadError) as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)
    assert len(str(excinfo.value)) < 1200
    assert str(excinfo.value).endswith("...")


def test_missing_xsrf_token_is_explained(tmp_path):
    session = _FakeSession()
    session.cookies = {}

    with pytest.raises(UploadError, match="XSRF"):
        slowpics.upload(_comparison(tmp_path), session=session)


def test_the_upload_timeout_grows_with_the_image():
    """One constant covered both a small API call and a 6 MB body.

    Thirty seconds is generous for the first and hopeless for the second: on a
    real comparison of 24 screenshots at about 6 MB each, nine were lost to
    "the write operation timed out". Six uploads share one uplink, so each gets
    roughly a sixth of it, and the timeout has to allow for that.
    """
    small = slowpics._upload_timeout(40_000)
    big = slowpics._upload_timeout(6_000_000)

    assert big > small
    assert big >= 180, "a 6 MB image needs more than the old 30 seconds"
    assert small >= 90, "a floor, so a tiny image is not on a hair trigger"


def test_retry_after_header_is_honoured():
    assert slowpics._retry_after("7", 0) == 7.0
    assert slowpics._retry_after(None, 0) == 2.0
    assert slowpics._retry_after("nonsense", 2) == 6.0


def test_retry_after_never_returns_zero():
    """A zero would turn a rate limit into a tight loop against a free service."""
    assert slowpics._retry_after("0", 0) >= 1.0


# --------------------------------------------------------------------------
# BBCode
# --------------------------------------------------------------------------


def _urls(comparison: Comparison) -> list[str]:
    return [
        f"https://img/{source.name}/{path.stem}.png"
        for source in comparison.sources
        for path in source.images
    ]


def test_comparison_tag_lists_every_source_name(tmp_path):
    comparison = _comparison(tmp_path)

    markup = bbcode.comparison_tag(comparison, _urls(comparison))

    assert markup.startswith("[comparison=A,B]")
    assert markup.endswith("[/comparison]")


def test_comparison_tag_is_frame_major(tmp_path):
    """Consecutive images must be the same frame from different sources.

    That adjacency is the whole point: it is what lets a reader flip between
    two releases at one moment of the film.
    """
    comparison = _comparison(tmp_path)

    markup = bbcode.comparison_tag(comparison, _urls(comparison))
    lines = markup.replace("[comparison=A,B]", "").replace("[/comparison]", "").split("\n")

    assert lines[0].startswith("https://img/A/000100")
    assert lines[1].startswith("https://img/B/000100")
    assert lines[2].startswith("https://img/A/000200")
    assert lines[3].startswith("https://img/B/000200")


def test_wrong_number_of_urls_is_refused(tmp_path):
    comparison = _comparison(tmp_path)

    with pytest.raises(BBCodeError, match="silently pair"):
        bbcode.comparison_tag(comparison, ["only", "two"])


def test_img_list_groups_by_frame(tmp_path):
    comparison = _comparison(tmp_path)

    markup = bbcode.img_list(comparison, _urls(comparison))

    assert "[img]" in markup
    assert "A:" in markup and "B:" in markup


def test_markdown_output(tmp_path):
    comparison = _comparison(tmp_path)

    text = bbcode.markdown(comparison, _urls(comparison))

    assert text.startswith("## Test comparison")
    assert "**A**" in text


def test_render_rejects_an_unknown_format(tmp_path):
    comparison = _comparison(tmp_path)

    with pytest.raises(BBCodeError, match="unknown format"):
        bbcode.render(comparison, _urls(comparison), "html")


@pytest.mark.parametrize("fmt", bbcode.FORMATS)
def test_every_declared_format_renders(tmp_path, fmt):
    comparison = _comparison(tmp_path)

    assert bbcode.render(comparison, _urls(comparison), fmt)


def test_collection_link_names_the_sources(tmp_path):
    comparison = _comparison(tmp_path)

    link = bbcode.collection_link(comparison, "https://slow.pics/c/X")

    assert "A vs B" in link
    assert "https://slow.pics/c/X" in link


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_publish_reports_a_missing_manifest(tmp_path, capsys):
    from kiyas import cli

    assert cli.main(["publish", str(tmp_path)]) == 2
    assert "manifest" in capsys.readouterr().out


def test_publish_defaults_are_conservative():
    from kiyas import cli

    defaults = cli._publish_defaults()

    assert defaults.public is False
    assert defaults.nsfw is False
    assert defaults.remove_after == 0


def test_run_publish_flag_is_parsed():
    from kiyas import cli

    args = cli.build_parser().parse_args(["run", "p.toml", "--publish"])

    assert args.publish is True


def test_publish_format_flag_repeats():
    from kiyas import cli

    args = cli.build_parser().parse_args(
        ["publish", "out", "--format", "comparison", "--format", "markdown"]
    )

    assert args.formats == ["comparison", "markdown"]
