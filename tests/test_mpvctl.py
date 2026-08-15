"""The mpv layer: isolation, IPC framing, variants, and the frame picker.

Nothing here starts mpv. What is being checked is the reasoning around it --
what command line kiyas would run, what it does with the bytes mpv sends back,
and what it refuses to put on the command line in the first place.
"""

from __future__ import annotations

import ast
import json
from fractions import Fraction
from pathlib import Path

import pytest

from kiyas.mpvctl import picker, profile, variants
from kiyas.mpvctl.ipc import IpcError, MpvIpc
from kiyas.mpvctl.session import build_args
from kiyas.mpvctl.variants import Variant, VariantError

# --------------------------------------------------------------------------
# Isolation: invariant #2
# --------------------------------------------------------------------------


def test_every_invocation_names_a_config_dir(tmp_path):
    """The one thing that keeps the user's mpv profile out of a comparison."""
    args = build_args(Path("mpv"), config_dir=tmp_path / "profile", address="pipe")

    assert f"--config-dir={tmp_path / 'profile'}" in args
    # And it is not something a later argument can undo.
    assert not any(a.startswith("--config-dir") for a in args[2:])


def test_no_other_module_builds_an_mpv_command_line():
    """A guard on the guard.

    build_args is the only place that turns a path to mpv into a command line,
    so testing it is enough -- but only for as long as that stays true. This
    walks the source for anything else spawning a process with mpv in it.
    """
    root = Path(__file__).resolve().parent.parent / "src" / "kiyas"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "session.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if target.endswith(("subprocess.Popen", "subprocess.run")):
                rendered = ast.unparse(node)
                if "mpv" in rendered.lower():
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"mpv is launched outside MpvSession at {offenders}. Every invocation has to go "
        f"through build_args, or --config-dir can be forgotten."
    )


def test_profile_directory_is_self_describing(tmp_path):
    written = profile.write_profile(tmp_path / "p")

    conf = (written / "mpv.conf").read_text(encoding="utf-8")
    assert "kiyas" in conf
    assert (written / "input.conf").is_file()
    # mpv scans these; present and empty beats absent.
    assert (written / "scripts").is_dir()


def test_profile_is_rewritten_not_appended(tmp_path):
    directory = tmp_path / "p"
    profile.write_profile(directory)
    (directory / "mpv.conf").write_text("vo=null\n", encoding="utf-8")
    profile.write_profile(directory)

    assert "vo=null" not in (directory / "mpv.conf").read_text(encoding="utf-8")


def test_capture_defaults_disable_everything_that_would_land_in_the_picture():
    joined = " ".join(profile.BASE_ARGS)

    assert "--osd-level=0" in joined
    assert "--osc=no" in joined
    assert "--no-sub" in joined
    # Real pixels, not logical ones: a 150% display would otherwise silently
    # change the capture size.
    assert "--hidpi-window-scale=no" in joined


def test_width_only_geometry_so_nothing_is_letterboxed(tmp_path):
    args = build_args(Path("mpv"), config_dir=tmp_path, address="pipe", width=1920)

    assert "--geometry=1920" in args
    assert "--geometry=1920x1080" not in args


def test_fullscreen_wins_over_width(tmp_path):
    args = build_args(Path("mpv"), config_dir=tmp_path, address="pipe", width=1920, fullscreen=True)

    assert "--fullscreen" in args
    assert not any(a.startswith("--geometry") for a in args)


def test_extra_args_come_last_so_they_can_override(tmp_path):
    args = build_args(
        Path("mpv"), config_dir=tmp_path, address="pipe", extra_args=("--osd-level=1",)
    )

    assert args[-1] == "--osd-level=1"
    assert args.index("--osd-level=0") < args.index("--osd-level=1")


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------


def test_templates_all_expand_to_several_named_variants():
    for name in variants.TEMPLATES:
        table = {"shaders": ["a.glsl", "b.glsl"]} if name == "shaders" else {}
        built = variants.expand_template(name, table)

        assert len(built) >= 2, name
        assert len({v.name for v in built}) == len(built), f"{name} has duplicate labels"
        assert all(v.options for v in built), f"{name} has a variant that changes nothing"


def test_shader_template_includes_an_unshaded_control():
    built = variants.expand_template("shaders", {"shaders": ["ArtCNN.glsl"]})

    assert built[0].options["glsl-shaders"] == ""
    assert built[1].name == "ArtCNN"


def test_shader_template_without_a_list_says_what_is_missing():
    with pytest.raises(VariantError, match="shaders"):
        variants.expand_template("shaders", {})


def test_unknown_template_lists_the_real_ones():
    with pytest.raises(VariantError, match="tonemap"):
        variants.expand_template("tonemaps", {})


@pytest.mark.parametrize("option", ["config-dir", "include", "script", "profile"])
def test_options_that_would_break_isolation_are_refused(option):
    with pytest.raises(VariantError, match="never involved"):
        variants.normalise_options({option: "anything"}, "test")


def test_leading_dashes_do_not_smuggle_a_forbidden_option():
    with pytest.raises(VariantError, match="never involved"):
        variants.normalise_options({"--config-dir": "/somewhere"}, "test")


def test_values_become_mpv_strings():
    options = variants.normalise_options(
        {"deband": True, "deband-iterations": 2, "glsl-shaders": ["a.glsl", "b.glsl"]}, "test"
    )

    assert options == {
        "deband": "yes",
        "deband-iterations": "2",
        "glsl-shaders": "a.glsl,b.glsl",
    }


def test_a_variant_overrides_the_shared_base():
    merged = Variant("x", {"tone-mapping": "spline"}).merged({"tone-mapping": "clip", "hdr": "yes"})

    assert merged.options == {"tone-mapping": "spline", "hdr": "yes"}


def test_describe_templates_covers_every_template():
    described = dict(variants.describe_templates())

    assert set(described) == set(variants.TEMPLATES)
    assert all(text for text in described.values())


# --------------------------------------------------------------------------
# IPC framing
# --------------------------------------------------------------------------


class FakeTransport:
    """A scripted mpv. Replies are queued as objects, not bytes."""

    def __init__(self, script=None):
        self.sent: list[dict] = []
        self._out = b""
        self._script = script or (lambda command, request_id: {"error": "success", "data": None})

    def available(self) -> int:
        return len(self._out)

    def read(self, count: int) -> bytes:
        chunk, self._out = self._out[:count], self._out[count:]
        return chunk

    def write(self, payload: bytes) -> None:
        message = json.loads(payload.decode("utf-8"))
        self.sent.append(message)
        reply = self._script(message["command"], message["request_id"])
        if reply is not None:
            reply = {**reply, "request_id": message["request_id"]}
            self._out += json.dumps(reply).encode("utf-8") + b"\n"

    def push_event(self, name: str, **extra) -> None:
        self._out += json.dumps({"event": name, **extra}).encode("utf-8") + b"\n"

    def close(self) -> None:
        pass


def make_ipc(transport) -> MpvIpc:
    ipc = MpvIpc.__new__(MpvIpc)
    ipc._address = "fake"  # noqa: SLF001
    ipc._buffer = b""  # noqa: SLF001
    ipc._events = []  # noqa: SLF001
    ipc._replies = {}  # noqa: SLF001
    ipc._counter = 0  # noqa: SLF001
    ipc._transport = transport  # noqa: SLF001
    return ipc


def test_a_command_gets_its_own_reply():
    transport = FakeTransport(lambda command, rid: {"error": "success", "data": len(command)})
    ipc = make_ipc(transport)

    assert ipc.command("get_property", "time-pos") == 2
    assert transport.sent[0]["command"] == ["get_property", "time-pos"]


def test_a_reply_for_a_different_request_is_not_taken():
    """Somebody else's answer must not be handed back as ours.

    mpv can have several replies in flight, and matching on arrival order
    instead of request_id would return the wrong property's value -- which
    looks like a correct answer.
    """
    transport = FakeTransport(lambda command, rid: None)  # mpv stays silent
    ipc = make_ipc(transport)
    transport._out += json.dumps({"request_id": 99, "error": "success", "data": "theirs"}).encode()  # noqa: SLF001
    transport._out += b"\n"  # noqa: SLF001

    with pytest.raises(IpcError, match="did not answer"):
        ipc.command("get_property", "time-pos", timeout=0.2)


def test_an_mpv_error_is_raised_not_returned():
    transport = FakeTransport(lambda command, rid: {"error": "property not found"})
    ipc = make_ipc(transport)

    with pytest.raises(IpcError, match="property not found"):
        ipc.command("get_property", "nonsense")


def test_try_command_turns_an_unavailable_property_into_none():
    transport = FakeTransport(lambda command, rid: {"error": "property unavailable"})
    ipc = make_ipc(transport)

    assert ipc.try_command("get_property", "video-frame-info") is None


def test_events_and_replies_interleave_without_losing_either():
    def script(command, request_id):
        return {"error": "success", "data": "ok"}

    transport = FakeTransport(script)
    ipc = make_ipc(transport)
    transport.push_event("file-loaded")
    transport.push_event("playback-restart")

    assert ipc.command("anything") == "ok"
    assert ipc.wait_event("playback-restart", timeout=1)["event"] == "playback-restart"
    assert ipc.wait_event("file-loaded", timeout=1)["event"] == "file-loaded"


def test_a_split_message_is_reassembled():
    """mpv's pipe hands over whatever bytes happen to be there."""
    transport = FakeTransport()
    ipc = make_ipc(transport)
    blob = json.dumps({"event": "file-loaded"}).encode() + b"\n"
    transport._out = blob[:6]  # noqa: SLF001

    with pytest.raises(IpcError):
        ipc.wait_event("file-loaded", timeout=0.2)

    transport._out += blob[6:]  # noqa: SLF001
    assert ipc.wait_event("file-loaded", timeout=1)


def test_a_malformed_line_does_not_kill_the_session():
    transport = FakeTransport()
    ipc = make_ipc(transport)
    transport._out = b"{not json}\n" + json.dumps({"event": "file-loaded"}).encode() + b"\n"  # noqa: SLF001

    assert ipc.wait_event("file-loaded", timeout=1)


def test_waiting_for_an_event_that_never_comes_times_out():
    ipc = make_ipc(FakeTransport())

    with pytest.raises(IpcError, match="shutdown"):
        ipc.wait_event("shutdown", timeout=0.2)


# --------------------------------------------------------------------------
# The render barrier
# --------------------------------------------------------------------------


class LaggingMpv:
    """An mpv whose renderer runs behind its player, on purpose.

    A model of the failure that made every capture of a 4K remux come back one
    frame early: the player finishes a seek and answers questions about it long
    before the video output has drawn the picture. Render requests queue up and
    are serviced one every ``lag`` commands, which is enough to reproduce both
    shapes of the bug deterministically and without a video file.
    """

    def __init__(self, lag: int = 6, fps: float = 24.0):
        self.lag = lag
        self.fps = fps
        self.queue: list[int | None] = []
        self.drawn: int | None = None
        self.player_frame: int | None = None
        self.renders = 0
        self.ticks = 0
        self.captured: list[int | None] = []

    def _tick(self) -> None:
        self.ticks += 1
        if self.queue and self.ticks % self.lag == 0:
            self.drawn = self.queue.pop(0)
            self.renders += 1

    def command(self, *args, timeout=None):
        self._tick()
        name = args[0]
        if name == "seek":
            self.player_frame = round(args[1] * self.fps)
            self.queue.append(self.player_frame)
            return None
        if name == "get_property":
            if args[1] == "vo-passes":
                # The renderer's own record: only a drawn frame moves it.
                return {"fresh": [{"count": self.renders, "last": self.renders}]}
            if args[1] == "video-frame-info":
                # Player-side, and that is the trap: it knows about the seek
                # immediately, whether or not anything has been drawn.
                if self.player_frame is None:
                    raise IpcError("property unavailable")
                return {"frame": self.player_frame}
            return None
        if name == "set_property":
            if args[1] == "osd-msg1":
                # Changing the caption makes mpv redraw -- the picture it is
                # already showing, not the one that was seeked to.
                self.queue.append(self.drawn)
            return None
        if name == "screenshot-to-file":
            self.captured.append(self.drawn)
            Path(args[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[1]).write_bytes(b"png")
            return None
        if name == "loadfile":
            self.queue.append(0)
            return None
        return None

    def try_command(self, *args, timeout=None):
        try:
            return self.command(*args, timeout=timeout)
        except IpcError:
            return None

    def wait_event(self, name, timeout=None):
        self._tick()
        return {"event": name}

    def close(self) -> None:
        pass


def make_session(fake: LaggingMpv):
    from kiyas.mpvctl.session import MpvSession

    session = MpvSession.__new__(MpvSession)
    session._ipc = fake  # noqa: SLF001
    session._closed = False  # noqa: SLF001
    session._current_frame = None  # noqa: SLF001
    session._marker = None  # noqa: SLF001
    session._scratch = None  # noqa: SLF001
    session.best_effort = False
    session._log = []  # noqa: SLF001
    return session


def test_a_capture_waits_for_the_renderer_not_the_player(tmp_path):
    fake = LaggingMpv()
    session = make_session(fake)
    session.load(Path("clip.mkv"))

    session.capture(40, Fraction(24), tmp_path / "shot.png")

    assert fake.captured == [40], (
        f"captured frame {fake.captured}; the renderer had not drawn 40 yet"
    )


def test_a_caption_cannot_satisfy_the_barrier_on_the_wrong_frame(tmp_path):
    """The second shape of the bug, and the one that survived the first fix.

    Setting the caption makes mpv redraw the picture it is already showing. If
    that happens before the seek, the redraw moves the renderer's counter, the
    barrier accepts it, and the capture is of the previous frame -- with the
    new frame's number printed on it, which is what makes it so convincing.
    """
    fake = LaggingMpv()
    session = make_session(fake)
    session.load(Path("clip.mkv"))
    session.capture(10, Fraction(24), tmp_path / "a.png", label="first")

    session.capture(40, Fraction(24), tmp_path / "b.png", label="second")

    assert fake.captured == [10, 40], f"captured {fake.captured}, expected [10, 40]"


def test_several_captions_and_seeks_stay_in_step(tmp_path):
    fake = LaggingMpv(lag=3)
    session = make_session(fake)
    session.load(Path("clip.mkv"))

    wanted = [10, 40, 70, 100, 20]
    for frame in wanted:
        session.capture(frame, Fraction(24), tmp_path / f"{frame}.png", label=f"frame {frame}")

    assert fake.captured == wanted


def test_recapturing_the_same_frame_does_not_seek_again(tmp_path):
    fake = LaggingMpv()
    session = make_session(fake)
    session.load(Path("clip.mkv"))
    session.capture(40, Fraction(24), tmp_path / "a.png")
    seeks = fake.renders

    session.capture(40, Fraction(24), tmp_path / "b.png")

    assert fake.captured == [40, 40]
    assert fake.renders == seeks, "a redundant seek would cost a decode for the same picture"


class WedgedMpv(LaggingMpv):
    """A player that seeks happily and never draws anything again."""

    def _tick(self) -> None:
        self.ticks += 1  # the render queue is never serviced


def test_a_renderer_that_never_draws_is_an_error_not_a_wrong_picture(tmp_path, monkeypatch):
    """Failing loudly beats writing a picture of the wrong frame."""
    from kiyas.mpvctl import session as session_module

    monkeypatch.setattr(session_module, "RENDER_TIMEOUT", 0.05)
    fake = WedgedMpv()
    session = make_session(fake)
    session.load(Path("clip.mkv"))

    with pytest.raises(session_module.SessionError, match="drew nothing new"):
        session.capture(40, Fraction(24), tmp_path / "shot.png")

    assert fake.captured == [], "a capture was written despite the renderer being stuck"


def test_without_vo_passes_the_session_says_it_is_guessing(tmp_path):
    """No GPU video output means no render record, and that has to be said."""
    fake = LaggingMpv()
    original = fake.command

    def no_vo_passes(*args, timeout=None):
        if args[:2] == ("get_property", "vo-passes"):
            fake._tick()
            raise IpcError("property unavailable")
        return original(*args, timeout=timeout)

    fake.command = no_vo_passes
    session = make_session(fake)
    session.load(Path("clip.mkv"))

    session.capture(40, Fraction(24), tmp_path / "shot.png")

    assert session.best_effort is True


# --------------------------------------------------------------------------
# The frame picker
# --------------------------------------------------------------------------


class FakeSession:
    def __init__(self, position: float = 0.0):
        self.position = position
        self.messages: list[str] = []

    def get(self, name: str):
        return self.position if name == "time-pos" else None

    def show_text(self, text: str, milliseconds: int = 1500) -> None:
        self.messages.append(text)


def test_marking_converts_the_playhead_to_a_frame():
    session = FakeSession(2.0)
    marked: list[int] = []

    picker._apply(session, "kiyas-mark", marked, Fraction(24))  # noqa: SLF001

    assert marked == [48]


def test_marking_rounds_rather_than_truncates():
    """A paused mpv sits on a frame boundary and floats land either side of it."""
    marked: list[int] = []
    picker._apply(FakeSession(2.0833332), marked=marked, action="kiyas-mark", fps=Fraction(24))  # noqa: SLF001

    assert marked == [50]


def test_the_same_frame_cannot_be_marked_twice():
    marked = [50]
    picker._apply(FakeSession(50 / 24), "kiyas-mark", marked, Fraction(24))  # noqa: SLF001

    assert marked == [50]


def test_undo_removes_the_last_mark_and_says_so():
    session = FakeSession()
    marked = [10, 20]
    picker._apply(session, "kiyas-undo", marked, Fraction(24))  # noqa: SLF001

    assert marked == [10]
    assert "20" in session.messages[-1]


def test_undo_with_nothing_marked_is_harmless():
    marked: list[int] = []
    picker._apply(FakeSession(), "kiyas-undo", marked, Fraction(24))  # noqa: SLF001

    assert marked == []


def test_clear_empties_the_list():
    marked = [1, 2, 3]
    picker._apply(FakeSession(), "kiyas-clear", marked, Fraction(24))  # noqa: SLF001

    assert marked == []


def test_the_on_screen_help_mentions_every_binding():
    text = picker._help_text()  # noqa: SLF001

    for key, _, _ in picker.BINDINGS:
        assert f"{key}:" in text


def test_marked_frames_come_back_as_a_pasteable_block():
    text = picker.as_toml([30, 10, 10, 20])

    assert 'method = "manual"' in text
    assert "manual = [10, 20, 30]" in text


def test_no_marks_produces_a_comment_not_an_empty_list():
    """An empty `manual = []` in a project file is a confusing error later."""
    assert picker.as_toml([]).startswith("#")


class ScriptedSession(FakeSession):
    """A player that emits a fixed run of events and then quits."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.loaded: Path | None = None
        self.bindings: dict[str, str] = {}
        self.sought: float | None = None
        self.closed = False

    def load(self, path):
        self.loaded = path

    def bind(self, key, command):
        self.bindings[key] = command

    def seek_seconds(self, seconds):
        self.sought = seconds

    def poll_events(self):
        if not self.script:
            return []
        # The playhead moves while you watch, so successive marks land on
        # different frames.
        self.position += 1.0
        return [self.script.pop(0)]

    @property
    def alive(self):
        return bool(self.script)

    def close(self):
        self.closed = True


def run_picker(monkeypatch, script, **kwargs):
    session = ScriptedSession(script)
    monkeypatch.setattr(picker, "MpvSession", lambda *a, **k: session)
    frames = picker.pick(
        Path("mpv"), Path("clip.mkv"), config_dir=Path("cfg"), fps=Fraction(24), poll=0, **kwargs
    )
    return frames, session


def test_the_picker_collects_marks_until_mpv_quits(monkeypatch):
    frames, session = run_picker(
        monkeypatch,
        [
            {"event": "client-message", "args": ["kiyas-mark"]},
            {"event": "client-message", "args": ["kiyas-mark"]},
            {"event": "shutdown"},
        ],
    )

    assert frames == [24, 48]
    assert session.loaded == Path("clip.mkv")
    assert set(session.bindings) == {key for key, _, _ in picker.BINDINGS}


def test_the_picker_returns_what_was_marked_when_the_window_is_closed(monkeypatch):
    """Closing the window is how you finish, so the marks must survive it."""
    session_events = [
        {"event": "client-message", "args": ["kiyas-mark"]},
        {"event": "shutdown"},
        {"event": "client-message", "args": ["kiyas-mark"]},
    ]
    frames, session = run_picker(monkeypatch, session_events)

    assert frames == [24]
    assert session.closed


def test_the_picker_ignores_events_it_did_not_ask_for(monkeypatch):
    frames, _ = run_picker(
        monkeypatch,
        [
            {"event": "file-loaded"},
            {"event": "client-message", "args": ["something-else"]},
            {"event": "client-message", "args": []},
            {"event": "shutdown"},
        ],
    )

    assert frames == []


def test_the_picker_can_open_at_a_frame(monkeypatch):
    _, session = run_picker(monkeypatch, [{"event": "shutdown"}], start_frame=48)

    assert session.sought == pytest.approx(2.0)
