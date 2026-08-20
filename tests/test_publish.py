"""Publishing tests.

Nothing here talks to slow.pics. The upload flow is driven against a fake
session that records what it was asked to send, because the failure that
matters is not "the request failed" -- it is "the request succeeded and put
the wrong picture in the wrong cell", which no amount of live testing catches
without a human looking at the result.
"""

from __future__ import annotations

import json
import threading
from fractions import Fraction

import pytest

from kiyas.publish import bbcode, load_manifest, slowpics
from kiyas.publish.bbcode import BBCodeError
from kiyas.publish.manifest import Comparison, ComparisonRow, ManifestError
from kiyas.publish.slowpics import UploadError

#: The pacing interval as shipped, read at import time -- before the autouse
#: fixture below sets it to zero. Without this the "is pacing on by default"
#: test would be reading the fixture's value and would pass no matter what the
#: module ships.
SHIPPED_MIN_UPLOAD_INTERVAL = slowpics.MIN_UPLOAD_INTERVAL

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


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Upload pacing is real time, and the suite should not spend it.

    The pacing itself is tested directly further down; here it only needs to
    be out of the way.
    """
    monkeypatch.setattr(slowpics, "MIN_UPLOAD_INTERVAL", 0.0)
    monkeypatch.setattr(slowpics.time, "sleep", lambda _seconds: None)


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
        self,
        *,
        image_status=200,
        image_headers=None,
        image_text="",
        collection_status=200,
        collection_text="",
        collection_headers=None,
        landing_status=200,
        landing_text="",
        landing_headers=None,
    ):
        self.headers = {}
        self.cookies = {"XSRF-TOKEN": "token-123", "BROWSER-ID": "browser-abc"}
        self.posts = []
        self.image_posts = []
        self._image_status = image_status
        self._image_headers = image_headers or {}
        self._image_text = image_text
        self._collection_status = collection_status
        self._collection_text = collection_text
        self._collection_headers = collection_headers or {}
        self._landing_status = landing_status
        self._landing_text = landing_text
        self._landing_headers = landing_headers or {}

    def get(self, url, **_kwargs):
        return _Response(
            status=self._landing_status,
            text=self._landing_text,
            headers=self._landing_headers,
        )

    def post(self, url, data=None, files=None, **_kwargs):
        if "/upload/image/" in url:
            self.image_posts.append((url, data, files))
            return _Response(
                self._image_status,
                headers=self._image_headers,
                text=self._image_text,
            )
        self.posts.append((url, data))
        return _Response(
            status=self._collection_status,
            text=self._collection_text,
            headers=self._collection_headers,
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


# --------------------------------------------------------------------------
# Cloudflare
# --------------------------------------------------------------------------

#: The genuine article, trimmed. Captured from slow.pics on 2026-08-20 while
#: the publishing address was banned. Two details here are why this is a copy
#: of a real page rather than something plausible written by hand: the number
#: is spelled "Error 1006" and not "error code: 1006" as Cloudflare's own
#: documentation writes it, and the page states the visitor's IP address in
#: the body. A hand-written fixture would have had neither, and the code was
#: wrong about the first one until this was captured.
CLOUDFLARE_1006 = """<!doctype html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<html class="no-js" lang="en-US">
<head><title>Access denied | slow.pics used Cloudflare to restrict access</title></head>
<body>
<h1>Error 1006 Ray ID: a2e1408ace72d0f8 &bull; 2026-08-20 12:02:58 UTC</h1>
<h2>Access denied</h2>
<p>The owner of this website (slow.pics) has banned your IP address
(203.0.113.7).</p>
<p>Cloudflare Ray ID: a2e1408ace72d0f8 &bull; Your IP: 203.0.113.7 &bull;
Performance &amp; security by Cloudflare</p>
</body></html>"""

CLOUDFLARE_1015 = CLOUDFLARE_1006.replace("Error 1006", "Error 1015").replace(
    "has banned your IP address", "is rate limiting your IP address"
)

CF_HEADERS = {"cf-ray": "a2e1408ace72d0f8-SOF", "server": "cloudflare"}


def test_a_cloudflare_block_is_explained_rather_than_quoted(tmp_path):
    """The likeliest real failure, and the one the raw quote serves worst.

    slow.pics is behind Cloudflare and bans addresses that upload too eagerly.
    What comes back is a page about enabling cookies, and pasting 600
    characters of it into the terminal tells the reader nothing they can act
    on -- least of all that the site never saw their comparison at all.
    """
    session = _FakeSession(
        collection_status=403, collection_text=CLOUDFLARE_1006, collection_headers=CF_HEADERS
    )

    with pytest.raises(UploadError) as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)

    message = str(excinfo.value)
    assert "banned this IP address" in message
    assert "error 1006" in message
    assert "a2e1408ace72d0f8-SOF" in message
    assert "<html" not in message
    assert "enable cookies" not in message


def test_a_cloudflare_block_does_not_repeat_the_visitors_ip(tmp_path):
    """The page prints it; an error someone pastes into a bug report should not."""
    session = _FakeSession(
        collection_status=403, collection_text=CLOUDFLARE_1006, collection_headers=CF_HEADERS
    )

    with pytest.raises(UploadError) as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)

    assert "203.0.113.7" not in str(excinfo.value)


def test_a_ban_and_a_rate_limit_are_not_given_the_same_advice(tmp_path):
    """Waiting clears one of them and never clears the other."""
    banned = _FakeSession(
        collection_status=403, collection_text=CLOUDFLARE_1006, collection_headers=CF_HEADERS
    )
    limited = _FakeSession(
        collection_status=429, collection_text=CLOUDFLARE_1015, collection_headers=CF_HEADERS
    )

    with pytest.raises(UploadError) as ban:
        slowpics.upload(_comparison(tmp_path), session=banned)
    with pytest.raises(UploadError) as limit:
        slowpics.upload(_comparison(tmp_path), session=limited)

    assert "Wait" not in str(ban.value)
    assert "different network" in str(ban.value)
    assert "Wait for it to lapse" in str(limit.value)
    assert "rate limited" in str(limit.value)


def test_a_blocked_address_is_named_at_the_landing_page(tmp_path):
    """The block usually lands before the collection is ever posted."""
    session = _FakeSession(
        landing_status=403, landing_text=CLOUDFLARE_1006, landing_headers=CF_HEADERS
    )

    with pytest.raises(UploadError) as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)

    assert "banned this IP address" in str(excinfo.value)
    assert "<html" not in str(excinfo.value)


def test_a_forbidden_image_is_not_retried(tmp_path):
    """Retrying into a block is the traffic that turns a limit into a ban.

    Five attempts per image across six workers is another twenty requests
    arriving at an edge that has already refused this address, and not one of
    them can succeed. The count is the assertion: four images, four requests.
    """
    session = _FakeSession(image_status=403, image_text=CLOUDFLARE_1006, image_headers=CF_HEADERS)

    with pytest.raises(UploadError) as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)

    assert len(session.image_posts) == 4
    assert "banned this IP address" in str(excinfo.value)


def test_a_field_error_is_still_quoted_word_for_word(tmp_path):
    """The Cloudflare path must not swallow the case it was built alongside."""
    session = _FakeSession(
        collection_status=400,
        collection_text='{"tmdbId":"must be MOVIE_<id> or TV_<id>"}',
        collection_headers=CF_HEADERS,
    )

    with pytest.raises(UploadError, match="must be MOVIE_") as excinfo:
        slowpics.upload(_comparison(tmp_path), session=session)

    assert "the server said" in str(excinfo.value)
    assert "Cloudflare" not in str(excinfo.value)


def test_missing_xsrf_token_is_explained(tmp_path):
    session = _FakeSession()
    session.cookies = {}

    with pytest.raises(UploadError, match="XSRF"):
        slowpics.upload(_comparison(tmp_path), session=session)


def test_tmdb_references_are_normalised_to_what_slow_pics_wants():
    """The reference client documents "TV_XXXXXX" or "MOVIE_XXXXXX".

    A bare number was being sent and refused with a bare 400 -- no body, no
    header, nothing to act on -- which cost a whole upload to discover.
    """
    assert slowpics.normalise_tmdb("MOVIE_1275779") == "MOVIE_1275779"
    assert slowpics.normalise_tmdb("TV_1399") == "TV_1399"
    # The shape Matroska tags carry, which is where the id usually comes from.
    assert slowpics.normalise_tmdb("movie/1275779") == "MOVIE_1275779"
    assert slowpics.normalise_tmdb("tv/1399") == "TV_1399"
    assert slowpics.normalise_tmdb("  movie 1275779  ") == "MOVIE_1275779"


def test_a_bare_number_is_refused_rather_than_guessed():
    """Guessing the type attaches the comparison to a different title.

    The person who passed the id knows which it is; this program does not, and
    a film and a series can share a number.
    """
    with pytest.raises(ValueError, match="MOVIE_1275779 or TV_1275779"):
        slowpics.normalise_tmdb("1275779")


def test_an_imdb_id_is_not_a_tmdb_id():
    with pytest.raises(ValueError, match="not a TMDB reference"):
        slowpics.normalise_tmdb("tt15047880")


def test_the_payload_carries_the_normalised_form(tmp_path):
    payload = slowpics.build_payload(
        _comparison(tmp_path),
        public=False,
        nsfw=False,
        optimize=True,
        remove_after_days=0,
        tmdb_id="movie/1275779",
    )
    assert payload["tmdbId"] == "MOVIE_1275779"


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
    """A number from the server is obeyed exactly; anything else backs off."""
    assert slowpics._retry_after("7", 0) == 7.0

    # No usable header falls through to the jittered backoff, so the contract
    # is a range rather than a number.
    assert 1.0 <= slowpics._retry_after(None, 0) <= 2.0
    assert 4.0 <= slowpics._retry_after("nonsense", 2) <= 8.0


def test_retry_after_never_returns_zero():
    """A zero would turn a rate limit into a tight loop against a free service."""
    assert slowpics._retry_after("0", 0) >= 1.0


# --------------------------------------------------------------------------
# Upload pacing
# --------------------------------------------------------------------------


def test_pacer_spaces_starts_apart(monkeypatch):
    """Each start is pushed one interval past the previous one.

    Asserted on the delay the pacer asks for rather than on elapsed wall time:
    a clock-watching test is slow when it passes and flaky on a loaded CI box.
    """
    asked: list[float] = []
    monkeypatch.setattr(slowpics.time, "sleep", lambda seconds: asked.append(seconds))

    pacer = slowpics._Pacer(5.0)
    for _ in range(4):
        pacer.wait()

    # The first caller owns the slot it finds free, so it does not wait; each
    # one after it waits for the slot the previous caller took.
    assert len(asked) == 3
    assert asked == pytest.approx([5.0, 10.0, 15.0], abs=0.5)


def test_pacer_gate_is_global_not_per_thread(monkeypatch):
    """Six workers arriving at once get six different slots.

    A per-thread delay would let all six through together, which is exactly
    the burst the interval exists to prevent.
    """
    asked: list[float] = []
    lock = threading.Lock()

    def record(seconds):
        with lock:
            asked.append(seconds)

    monkeypatch.setattr(slowpics.time, "sleep", record)

    pacer = slowpics._Pacer(5.0)
    threads = [threading.Thread(target=pacer.wait) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One worker goes straight through, the other five are staggered onto
    # slots of their own rather than all being handed the same delay.
    assert len(asked) == 5
    assert len(set(asked)) == 5, f"workers were not staggered: {sorted(asked)}"


def test_pacer_of_zero_does_not_sleep(monkeypatch):
    """Disabling the pacing has to actually disable it, not sleep zero."""
    slept: list[float] = []
    monkeypatch.setattr(slowpics.time, "sleep", lambda seconds: slept.append(seconds))

    pacer = slowpics._Pacer(0.0)
    for _ in range(5):
        pacer.wait()

    assert slept == []


def test_every_attempt_is_paced_not_just_the_first(tmp_path):
    """Retries go through the pacer too.

    This is the case that mattered: the first pass was paced, the retries were
    not, and the retries were what arrived as a burst.
    """
    waits = 0

    class CountingPacer:
        def wait(self):
            nonlocal waits
            waits += 1

    attempts = 0

    class FlakyClient:
        def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OSError("the write operation timed out")
            return _Response(200)

    image = tmp_path / "frame.png"
    image.write_bytes(b"x")

    slowpics._send_one(FlakyClient(), "col", "browser", "img", image, CountingPacer())

    assert attempts == 3
    assert waits == 3, "the two retries were not paced"


def test_pacing_is_on_by_default():
    """The shipped interval has to be a real one.

    Every other test in this section runs with pacing switched off, so nothing
    else here would notice the default being set to zero -- and a zero is
    invisible: uploads still succeed, right up until the address is banned.
    """
    assert SHIPPED_MIN_UPLOAD_INTERVAL > 0


def test_backoff_grows_and_stays_bounded():
    """Growing, jittered, and never past the ceiling."""
    for attempt in range(10):
        wait = slowpics._backoff(attempt)
        assert 0 < wait <= slowpics.MAX_BACKOFF

    early = [slowpics._backoff(0) for _ in range(20)]
    late = [slowpics._backoff(4) for _ in range(20)]
    assert max(early) < min(late), "a later attempt should wait longer"


def test_backoff_is_jittered():
    """Identical waits would make six workers retry in lockstep."""
    assert len({slowpics._backoff(3) for _ in range(20)}) > 1


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
